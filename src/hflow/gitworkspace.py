"""Git-backed candidate isolation: per-run worktree, frozen candidate, real identity.

M2 needs a candidate that has a *real, explicit* identity rather than only a content
fingerprint. This module owns that and nothing else:

* an isolated worktree per run, created from a fixed base commit, so the user's own checkout
  is never written to (and never stashed, reset or overwritten);
* a freeze step that records the candidate as a **commit** with a canonical message and an
  explicit identity, so the receipt can name a commit and a tree object;
* a drift check that fails closed when the worktree no longer matches what was frozen;
* the plumbing a controlled integration is built from: ancestry, a three-way merge computed by
  ``merge-tree`` (objects only - no index, no working tree), ``commit-tree``, a compare-and-set
  branch update, and the question "is this branch checked out anywhere".

Deliberately absent: porcelain merging or rebasing in any working tree, creating or deleting
branches, remotes, publishing, and any "generic git platform" behaviour.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import NamedTuple

from .contracts import RefusalCode, RefusedError, digest_of
from .workspace import BUILTIN_WRITE_DENY, hash_regular_file, matches_pattern

#: Porcelain v1 status codes that this module refuses to interpret. Anything unmerged, or a
#: submodule change, changes what "the paths in this worktree" even means, so the caller gets
#: an explicit refusal instead of a guessed path.
UNSUPPORTED_STATUS_CODES = frozenset({"U", "DD", "AU", "UD", "UA", "DU", "AA", "UU"})


class GitStatusParseError(RuntimeError):
    """A porcelain record this module will not guess about."""


def brief_list(items: Sequence[str], limit: int = 5, separator: str = ", ") -> str:
    """The first ``limit`` items joined, with ``(+N more)`` when there are more."""
    shown = separator.join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _status_records(raw: str) -> Iterator[tuple[str, str, str, str | None]]:
    """``(record, code, path, source)`` per ``status --porcelain=v1 -z`` record, never trimmed.

    A record is ``XY<space><path>``. A rename or copy (``X`` is ``R`` or ``C``) is followed by a
    second NUL-terminated field, its source path: ``source`` is that path, or ``""`` when git
    printed none; it is ``None`` for every other record. A record shorter than four characters
    takes no source; the caller decides what it means.
    """
    fields = [field for field in raw.split("\0") if field]
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        source: str | None = None
        if len(record) >= 4 and record[0] in {"R", "C"}:
            source = fields[index] if index < len(fields) else ""
            index += 1
        yield record, record[:2], record[3:], source


@dataclass(frozen=True)
class StatusReport:
    """Parsed ``git status --porcelain=v1 -z --untracked-files=all --ignored`` output."""

    #: Paths tracked-and-changed, or untracked (rename/copy contributes BOTH paths).
    changed: tuple[str, ...]
    #: Paths git ignores, untracked ones included (``!!``). Ignored is not the same as
    #: disposable.
    ignored: tuple[str, ...]
    #: Records that could not be interpreted (unmerged conflicts, submodule changes, ...).
    unsupported: tuple[str, ...]


def parse_status_z(raw: str) -> StatusReport:
    """Parse ``-z`` porcelain output.

    Why ``-z``: the ordinary text form quotes and escapes unusual paths (C-style quoting for
    non-ASCII and for spaces with ``core.quotePath``), which forces a second parsing problem.
    ``-z`` emits raw bytes, NUL-terminated, so paths arrive exactly as git sees them.

    Offset note, because this was misdescribed once already: in porcelain v1 an ordinary
    record is ``XY<space><path>``, so ``record[3:]`` is the path. What corrupts it is
    *trimming the record first* - ``" M src/a.py".strip()`` loses the leading space and makes
    ``[3:]`` cut into the path. This function therefore never trims a record; it only splits
    on NUL.

    Renames and copies are a single record with **two** NUL-separated paths (new, then old);
    both are reported, so a scope check cannot miss the old path.

    ``??`` vs ``!!``: with ``--ignored``, git reports a path that is both untracked and
    ignored as ``!!``, so it lands in ``ignored`` rather than ``changed``. That distinction
    matters for cleanup: a bytecode cache must not look like an unfrozen source change.
    """
    changed: list[str] = []
    ignored: list[str] = []
    unsupported: list[str] = []

    for record, code, path, source in _status_records(raw):
        if len(record) < 4:
            unsupported.append(record[:40])
            continue
        if code == "!!":
            ignored.append(path)
            continue
        if code in UNSUPPORTED_STATUS_CODES or "U" in code:
            unsupported.append(f"{code} {path}"[:80])
            continue
        if source is not None:  # rename/copy: the old path is the next field
            changed.append(path)
            if source:
                changed.append(source)
                if "S" in code or "N" in code:
                    unsupported.append(f"{code} {path} (submodule)")
            else:
                unsupported.append(f"{code} {path} (missing rename source)")
            continue
        if "S" in code:
            unsupported.append(f"{code} {path} (submodule)")
            continue
        changed.append(path)

    return StatusReport(tuple(changed), tuple(ignored), tuple(unsupported))


#: Identity used for controller-created candidate commits. Fixed on purpose: the commit
#: records the controller, not the person, and must not depend on ambient git config.
CANDIDATE_AUTHOR_NAME = "HFlow Controller"
CANDIDATE_AUTHOR_EMAIL = "hflow@localhost"


class GitError(RuntimeError):
    """A git command failed. The message keeps git's own words."""


#: The oldest Git a controlled integration runs on: ``merge-tree --write-tree`` is 2.38 and its
#: ``--merge-base`` option is 2.40.
MIN_INTEGRATION_GIT: tuple[int, int] = (2, 40)

_GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?(?!\d)")
_FULL_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def parse_git_version(text: str) -> tuple[int, int, int]:
    """``git version 2.56.0.windows.2`` -> ``(2, 56, 0)``.

    A vendor suffix (``.windows.2``, `` (Apple Git-146)``, ``.rc1``) is ignored; a missing patch
    number (a ``2.40.GIT`` source build) reads as ``0``. Anything else raises :class:`GitError`.
    """
    match = _GIT_VERSION.match(text.strip())
    if match is None:
        raise GitError(f"unrecognised `git version` output: {text.strip()[:80]!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def is_full_oid(value: str) -> bool:
    """Is ``value`` a complete lowercase hex object id (SHA-1 or SHA-256), as git prints one?"""
    return isinstance(value, str) and _FULL_OID.fullmatch(value) is not None


def _revision(value: str, label: str) -> str:
    """A revision argument that git cannot read as an option or split into several arguments."""
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise GitError(f"refusing {label} {value!r}: not a revision")
    if any(char in value for char in "\0\n\r"):
        raise GitError(f"refusing {label} {value!r}: it holds a NUL or a line break")
    return value


@dataclass(frozen=True)
class MergeTreeResult:
    """``git merge-tree --write-tree`` in git's own terms."""

    clean: bool
    #: The toplevel tree OID git printed. Git prints one for a conflicted merge too, with conflict
    #: markers inside files; it is never to be committed then.
    tree: str
    #: Conflicted paths, unique and sorted; empty when clean. Git documents that a conflicted merge
    #: can list no path (some directory-rename conflicts), so ``clean`` is the verdict, not this.
    conflicts: tuple[str, ...]


def _merge_tree_result(stdout: str, *, clean: bool, args: tuple[str, ...]) -> MergeTreeResult:
    """Parse ``merge-tree --write-tree -z --name-only --no-messages`` output.

    The layout (``Documentation/git-merge-tree.adoc``, ``builtin/merge-tree.c``): the toplevel
    tree OID and a NUL; then, only for a conflicted merge, each conflicted path once, raw (``-z``
    never quotes) and NUL-terminated. ``--no-messages`` drops the informational section, so
    nothing follows. Anything else raises: a layout this cannot read is not a merge result.
    """
    fields = stdout.split("\0")
    if len(fields) < 2 or fields[-1] != "":
        raise GitError(f"git {' '.join(args)} printed an unreadable result (no NUL-terminated tree)")
    tree, paths = fields[0], fields[1:-1]
    if not is_full_oid(tree):
        raise GitError(f"git {' '.join(args)} printed {tree[:80]!r} where a tree id belongs")
    if clean and paths:
        raise GitError(f"git {' '.join(args)} reported a clean merge and conflicted paths")
    if any(not path for path in paths):
        raise GitError(f"git {' '.join(args)} printed an empty conflicted path")
    return MergeTreeResult(clean=clean, tree=tree, conflicts=tuple(sorted(set(paths))))


#: How git's output is decoded, on every platform. Git stores paths, ref names and configuration as
#: bytes and prints them as they are, and Git for Windows keeps them in UTF-8 whatever the ANSI
#: code page - which is what ``subprocess`` text mode would otherwise decode with, turning
#: ``naïve.txt`` into ``naÃ¯ve.txt`` (or an error) on a Western or Chinese Windows.
GIT_OUTPUT_ENCODING = "utf-8"


def _path_key(path: str | os.PathLike[str]) -> str:
    """One spelling per file system location: links resolved, letter case as the platform reads it."""
    return os.path.normcase(os.path.realpath(path))


def same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Do two paths name the same location, links and junctions resolved (:func:`_path_key`)?"""
    return _path_key(left) == _path_key(right)


def _read_state_file(path: Path) -> str | None:
    """A worktree state file the way git's ``get_branch`` reads it: ``None`` when absent or empty.

    Trailing line ends are dropped. A file that exists but cannot be read raises: whether a branch
    is checked out must not be answered "no" because the answer could not be read.
    """
    try:
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise GitError(f"cannot read {path}: {exc}") from exc
    text = data.decode(GIT_OUTPUT_ENCODING, errors="surrogateescape").rstrip("\r\n")
    return text or None


def _same_ref(left: str, right: str) -> bool:
    """Do two ref names name the same ref? Letter case is ignored - erring towards "the same".

    On a case-insensitive file system ``refs/heads/Main`` reads the loose file of
    ``refs/heads/main``: ``git checkout Main`` or ``git symbolic-ref HEAD refs/heads/Main`` leaves
    a HEAD that holds ``main`` under another spelling, and an exact comparison would let a
    compare-and-set move that checkout's branch under its index and files. On a case-sensitive
    one the two names are two refs, and calling one checked out because of the other only hands
    the operator a merge command instead of moving it.
    """
    return left.casefold() == right.casefold()


def _state_names_branch(name: str | None, branch: str) -> bool:
    """Does a rebase ``head-name`` or ``BISECT_START`` value name ``branch`` (no ``refs/heads/``)?

    Mirrors git's ``get_branch``: ``refs/heads/<b>`` names ``<b>``; any other ``refs/...`` value,
    or a bare branch name (``BISECT_START`` stores one), is kept as written; ``detached HEAD``
    names nothing. An object id is shown abbreviated by git, so it is matched as a prefix (four
    hex digits or more) - erring towards "checked out", as letter case is (:func:`_same_ref`).
    """
    if not name or name == "detached HEAD":
        return False
    if is_full_oid(name):
        folded = branch.casefold()
        return re.fullmatch(r"[0-9a-f]{4,}", folded) is not None and name.startswith(folded)
    held = name[len("refs/heads/") :] if name.casefold().startswith("refs/heads/") else name
    return _same_ref(held, branch)


def _update_refs_state(path: Path) -> list[str]:
    """The refs a ``rebase --update-refs`` will move, from its ``update-refs`` state file.

    The file is groups of three lines: a ref name, its old and its new object id. Every ref line is
    returned, a malformed group's included - erring towards "checked out".
    """
    text = _read_state_file(path)
    if text is None:
        return []
    lines = text.split("\n")
    return [lines[index].rstrip("\r") for index in range(0, len(lines), 3)]


def _worktree_state_holds(admin: Path, ref: str) -> bool:
    """Does the worktree whose git directory is ``admin`` hold ``ref`` through an operation?

    What git's ``prepare_checked_out_branches`` (``branch.c``) adds to the ``branch`` line of
    ``git worktree list``, read from the same files (``wt_status_check_rebase`` /
    ``wt_status_check_bisect``): a rebase in progress (``rebase-apply`` that is not an ``am``,
    or ``rebase-merge``) names its branch in ``head-name``; a bisect (``BISECT_LOG`` present)
    names the branch it started from in ``BISECT_START``; a ``rebase --update-refs`` lists the
    branches it will move in ``rebase-merge/update-refs``. Ref names are compared without letter
    case (:func:`_same_ref`).
    """
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
    if branch is not None:
        rebase_apply = admin / "rebase-apply"
        if rebase_apply.exists():
            if not (rebase_apply / "applying").exists() and _state_names_branch(
                _read_state_file(rebase_apply / "head-name"), branch
            ):
                return True
        elif (admin / "rebase-merge").exists() and _state_names_branch(
            _read_state_file(admin / "rebase-merge" / "head-name"), branch
        ):
            return True
        if (admin / "BISECT_LOG").exists() and _state_names_branch(
            _read_state_file(admin / "BISECT_START"), branch
        ):
            return True
    return any(_same_ref(held, ref) for held in _update_refs_state(admin / "rebase-merge" / "update-refs"))


#: Ignored paths a *check* may legitimately leave behind in a run worktree. Naming them is
#: the point: "ignored" alone never authorizes deletion, and anything not on this list makes
#: a freeze or a cleanup refuse. Deliberately narrow - bytecode and a test runner's cache.
#: User-owned ignored files (``.env``, local data, notes) are not on it, and neither is anything
#: else under ``__pycache__``: Python imports ``__pycache__/helper.py`` (a namespace package),
#: and an ignored file is neither committed nor fingerprinted. ``**/*.pyc`` matches bytecode in
#: a ``__pycache__`` directory at any depth; bytecode outside one is refused by the freeze on its
#: own (:func:`is_sourceless_bytecode`).
IGNORED_ARTIFACT_ALLOWLIST = (
    "**/*.pyc",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
)


def _bytecode(path: str) -> tuple[bool, bool]:
    """``(is path a .pyc file, does a __pycache__ directory hold it at any depth)``.

    Letter case is ignored, as Windows imports ignore it.
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1].casefold().endswith(".pyc"), any(
        part.casefold() == "__pycache__" for part in parts[:-1]
    )


def is_sourceless_bytecode(path: str) -> bool:
    """Is ``path`` a ``.pyc`` file that does not sit in a ``__pycache__`` directory?

    Python imports such a file as the module itself when no source file is beside it, and
    CPython 3 never writes one there on its own (its cache lives in ``__pycache__``). So it is
    allowlisted by ``**/*.pyc`` and yet cannot be a check byproduct.
    """
    is_pyc, cached = _bytecode(path)
    return is_pyc and not cached


@dataclass(frozen=True)
class CandidateFreeze:
    """What the controller froze, stated in git's own vocabulary."""

    base_commit: str
    candidate_commit: str
    tree: str
    paths: tuple[str, ...]


#: How a block reason that refuses on changed shared Git metadata begins (see
#: :meth:`GitRepo.metadata_snapshot`). Every such reason starts with it, so an operator, a test and
#: the docs grep the same words.
GIT_METADATA_CHANGED = "shared Git metadata changed"

#: The run note kind for the shared Git metadata HFlow's own git reads
#: (:meth:`GitRepo.metadata_snapshot`). The digest is taken before the first dispatch
#: (``git_metadata: snapshot before dispatch <digest> ...``); each later comparison that found a
#: change records the digests and the changed keys and files, never values.
NOTE_GIT_METADATA = "git_metadata"
#: The ``git_metadata`` notes that warn rather than record a baseline: a comparison that found the
#: metadata changed or unreadable. ``inspect_run`` projects exactly these for ``status``/``report``,
#: and ``hflow clean`` refuses a run that carries one until the metadata matches its baseline again.
GIT_METADATA_WARNING_PREFIXES = (
    f"{NOTE_GIT_METADATA}: changed",
    f"{NOTE_GIT_METADATA}: unreadable",
)


@dataclass(frozen=True)
class GitMetadataSnapshot:
    """What HFlow's own git commands read from outside the candidate's tree, as digests.

    ``entries`` are ``(label, digest)`` pairs, sorted by label. ``config@checkout:<key>`` and
    ``config@worktree:<key>`` cover one configuration key as ``git config --list`` reports it in
    the user's checkout and in the run's worktree - every scope, origin and value recorded for it,
    includes expanded. ``file:<path>`` covers one file that configuration or an attribute mapping
    is read from: ``sha256:<hex>`` of its bytes, ``absent``, or the type of a non-regular file.
    Values exist only inside a digest, because a configuration value can be a credential; the
    snapshot lives in the controller's memory for one run and is never stored.
    """

    entries: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        return digest_of([list(entry) for entry in self.entries])

    def changes_since(self, before: GitMetadataSnapshot) -> list[str]:
        """What differs from ``before`` - added, removed or changed - by label, sorted.

        A configuration key is named by its section and name only (``filter.*.clean``): a
        subsection can carry a URL with a credential in it (``url.<base>.insteadOf``).
        """
        old, new = dict(before.entries), dict(self.entries)
        changed = sorted(label for label in old.keys() | new.keys() if old.get(label) != new.get(label))
        return list(dict.fromkeys(_redacted_label(label) for label in changed))


def _redacted_label(label: str) -> str:
    kind, _, name = label.partition(":")
    if not kind.startswith("config@"):
        return label
    section, _, rest = name.partition(".")
    subsection, dot, key = rest.rpartition(".")
    return f"{kind}:{section}.*.{key}" if dot and subsection else label


def _undecodable(args: tuple[str, ...], reason: str) -> str:
    """The refusal text for git output that is not text: names the command, never the bytes."""
    return (
        f"git {' '.join(args)} printed output that could not be decoded as text ({reason}); "
        "a configuration value or path holds bytes that are not valid in this encoding"
    )


def _config_records(listing: str) -> list[tuple[str, str, str, str | None]]:
    """``git config --list -z --show-scope --show-origin`` as ``(scope, origin, key, value)``.

    Each record is three NUL-terminated fields: the scope, the origin, and the key followed by a
    newline and its value (no newline for a key written without ``=``). Anything else raises: a
    listing this cannot read must not pass for an empty configuration.
    """
    fields = listing.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3:
        raise GitError(f"unreadable git config listing ({len(fields)} fields)")
    records: list[tuple[str, str, str, str | None]] = []
    for index in range(0, len(fields), 3):
        scope, origin, entry = fields[index : index + 3]
        key, newline, value = entry.partition("\n")
        records.append((scope, origin, key, value if newline else None))
    return records


def _metadata_path(cwd: Path, text: str) -> str:
    """One spelling per file, whichever context named it; a relative origin is relative to ``cwd``."""
    return Path(_path_key(Path(cwd) / text.strip())).as_posix()


def _file_digest(path: Path) -> str:
    """``sha256:<hex>`` of a regular file's bytes, ``absent``, or the type of anything else.

    A FIFO, a device (``/dev/zero``) or a directory is recorded by its type and never opened:
    reading one can block forever or never end, before any refusal is written. A changed type
    still counts as a change. Present but unreadable raises, because it cannot be shown unchanged -
    and so does a file swapped for something else between the type check and the read
    (:func:`~hflow.workspace.hash_regular_file` checks the opened handle again).
    """
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return f"not-a-regular-file:{stat.S_IFMT(info.st_mode):o}"
        return hash_regular_file(path)[1]
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        raise GitError(f"cannot read {path}: {exc}") from exc


@cache
def _never_a_directory() -> str:
    """An existing file no worker can replace: nothing below it can ever be a directory or a file.

    ``core.hooksPath`` and ``GIT_GRAFT_FILE`` name paths below it. An empty directory of HFlow's
    own would not do: a worker runs as the same user, so it could write into one in the user's
    temporary directory - a ``post-commit`` or ``reference-transaction`` hook (``--no-verify`` stops
    neither), or a graft file. Below this file git finds no hook and no graft file, and says
    nothing about either (opening a path below a file fails with ``ENOENT`` / ``ENOTDIR``, which
    git takes for "absent").

    Windows: ``ntdll.dll`` in the system directory, which every process has loaded and only
    TrustedInstaller may replace. Elsewhere: ``/dev/null``, a device in a directory only root
    writes. Refuses (:class:`GitError`) when it is missing or a directory, rather than run git
    with hooks the forced configuration was meant to switch off.
    """
    if os.name == "nt":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        anchor = os.path.join(buffer.value, "ntdll.dll") if 0 < length < len(buffer) else ""
    else:
        anchor = os.devnull
    try:
        is_directory = stat.S_ISDIR(os.stat(anchor).st_mode) if anchor else True
    except OSError:
        is_directory = True
    if is_directory:
        raise GitError(
            f"no system file to anchor git's hooks path below ({anchor or 'system directory unknown'}"
            "); refusing to run git with hooks enabled"
        )
    return Path(anchor).as_posix()


def _forced_config() -> tuple[tuple[str, str], ...]:
    """Configuration forced on every git call HFlow makes.

    Passed through ``GIT_CONFIG_COUNT``, which git applies after the global, repository and
    worktree config files, so neither the user's global config nor a value a worker wrote into
    the shared ``.git/config`` from inside its worktree overrides it:

    * ``core.hooksPath`` names a path below a system file (:func:`_never_a_directory`), so no hook
      runs - not ``post-checkout`` on ``worktree add``, not ``pre-commit`` / ``commit-msg`` /
      ``post-commit`` on the freeze, not ``reference-transaction`` on any ref write - and no
      worker can create one there;
    * ``maintenance.auto`` is off and ``gc.auto`` is ``0``: ``git commit`` (the freeze) otherwise
      starts ``git maintenance run --auto``, which Windows cannot detach, so a repack or a gc
      could run in the foreground of a freeze;
    * ``core.fsmonitor`` is off, so a status read never runs a configured monitor command;
    * ``commit.gpgsign`` is off, so a freeze never waits on gpg;
    * ``core.ignoreStat`` and ``core.sparseCheckout`` are off, so HFlow's own ``worktree add``
      never checks entries out with the assume-unchanged or skip-worktree flag, which would hide
      a worker's edit from the freeze (a flag set any other way refuses the freeze instead);
    * ``safe.bareRepository`` is ``explicit``, so git never adopts a directory it would only
      *implicitly* take for a bare repository - one holding ``HEAD``, ``objects/`` and ``refs/``
      but no ``.git``. A worker that deletes its worktree's ``.git`` file and leaves that layout
      in the worktree root would otherwise have HFlow's next git call there (the freeze, the
      metadata snapshot, a cleanup) read the worker's ``config`` as the repository's, with every
      command-running key in it (openai/codex PR #36924: ``core.fsmonitor``, which is forced off
      above anyway). Git honours this key only from protected configuration, and these keys are
      ``command`` scope, which is protected. Every HFlow call names its repository by the
      ``cwd`` of a checkout or linked worktree (found through its ``.git``, also when the main
      repository is bare) or by ``--git-dir``; git allows both, and a ``cwd`` inside a ``.git``
      directory too. Only a bare repository found by discovery from the ``cwd`` is refused, and
      HFlow never runs in one (``rev-parse --show-toplevel`` refuses a bare repository anyway).

    Being ``command`` scope, none of these keys enters :meth:`GitRepo.metadata_snapshot`, so
    adding one does not change a snapshot taken before.

    :func:`_base_env` also appends these to an inherited ``GIT_CONFIG_PARAMETERS``, which git
    reads after ``GIT_CONFIG_COUNT``, so a caller's ``git -c`` cannot override them either.
    """
    return (
        ("core.hooksPath", f"{_never_a_directory()}/hflow-no-hooks"),
        ("maintenance.auto", "false"),
        ("gc.auto", "0"),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
        ("core.ignoreStat", "false"),
        ("core.sparseCheckout", "false"),
        ("safe.bareRepository", "explicit"),
    )


def _absent_graft_file() -> str:
    """A path that can never exist, for ``GIT_GRAFT_FILE``: git then reads no graft file at all.

    Below the file of :func:`_never_a_directory`, so no worker can create it. A missing graft file
    is skipped silently; an existing empty one (``os.devnull``) would make git print its grafts
    deprecation hint on every call.
    """
    return f"{_never_a_directory()}/hflow-no-graft-file"


#: Variables that point git at a repository, index or object store other than the one the
#: command's ``cwd`` names, or that widen how far git looks for one. Inherited when HFlow is
#: started from inside a git hook or alias (git exports its repository-local state to its
#: children: ``git rev-parse --local-env-vars``); every HFlow call names its repository by
#: ``cwd``, so they are dropped rather than obeyed. ``GIT_IMPLICIT_WORK_TREE`` and ``GIT_PREFIX``
#: are what git sets beside ``GIT_DIR`` for a hook or a shell alias (the latter is the alias's
#: subdirectory). ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` lets discovery climb across a mount point
#: into a repository on another file system.
#:
#: ``GIT_CEILING_DIRECTORIES`` is deliberately kept: it can only stop discovery from climbing, so
#: with it a ``cwd`` resolves to the same repository or to none - never to another one. Dropping
#: it would let a project directory outside any repository resolve to an enclosing repository the
#: user fenced off with it (a home directory under version control, for example).
_REPOSITORY_LOCATING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

#: Variables that change what HFlow's git reads from the repository it found - its history, its
#: configuration, its attributes, its diff output or what a path argument matches - from outside
#: anything the metadata snapshot or a recorded commit describes. Dropped, never obeyed:
#:
#: * ``GIT_CONFIG`` makes ``git config`` read that one file instead of the system, global,
#:   repository and worktree files, listed as ``command`` scope - which
#:   :meth:`GitRepo.metadata_snapshot` skips, so the snapshot would digest no configuration at all
#:   and a worker's change to the shared config would go unseen;
#: * ``GIT_SHALLOW_FILE`` names a list of commits whose parents git then ignores, so ancestry
#:   (``merge-base --is-ancestor``, which an integration decides on) and ``rev-list`` change.
#:   The repository's own ``shallow`` file is still read - a shallow clone needs it - and it can
#:   only take parents away: an ancestry check it changes turns a yes into a no, never a no into
#:   a yes. ``GIT_GRAFT_FILE``, which can also *add* parents, is not dropped but replaced: see
#:   :func:`_base_env`;
#: * ``GIT_REPLACE_REF_BASE`` moves the replace refs, already ignored (``GIT_NO_REPLACE_OBJECTS``);
#: * ``GIT_ATTR_SOURCE`` (Git 2.42+) makes git read ``.gitattributes`` from a tree-ish instead of
#:   the working tree, which changes which filter or diff driver applies, outside the attribute
#:   files the metadata snapshot digests;
#: * ``GIT_EXTERNAL_DIFF`` names a program git runs for patch output and ``GIT_DIFF_OPTS``
#:   overrides the context a ``--unified=3`` asked for. The one patch HFlow reads,
#:   :meth:`GitRepo.diff_text_bounded`, passes ``--no-ext-diff --no-textconv`` as well; every
#:   other diff HFlow runs is ``--name-only``, which runs no diff program;
#: * ``GIT_LITERAL_PATHSPECS``, ``GIT_GLOB_PATHSPECS``, ``GIT_NOGLOB_PATHSPECS`` and
#:   ``GIT_ICASE_PATHSPECS`` change what a path argument matches. HFlow sets the literal one
#:   itself on every call that passes a path; an inherited glob or icase one makes such a call
#:   fail outright (git refuses to combine it with literal), so a freeze could never stage
#:   anything. None of them is left to apply to a call HFlow did not mark.
_CONTENT_REDIRECTING_ENV = (
    "GIT_CONFIG",
    "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE",
    "GIT_ATTR_SOURCE",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


def _sq_quote(text: str) -> str:
    """Quote ``text`` the way git's ``sq_quote`` does for ``GIT_CONFIG_PARAMETERS``."""
    return "'" + text.replace("'", "'\\''") + "'"


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    # Deterministic, inert git: a fixed identity (no ambient one), no system config, no
    # interactive prompts, no replacement objects (``refs/replace`` cannot make one commit read as
    # another), no grafts, no inherited repository-locating or content-redirecting variables, and
    # the forced configuration above: no hooks (the repository's, the user's, a worker's or a
    # caller's ``git -c``), no fsmonitor command, no signing, no flagged checkout, no implicitly
    # bare repository, no automatic maintenance. The user's global config is still read for
    # everything else.
    #
    # ``GIT_NO_LAZY_FETCH``: in a partial clone a missing object is fetched from the promisor
    # remote on demand - a network call any read (``cat-file``, ``diff``, ``merge-tree``) could
    # make. HFlow reaches no remote: a missing object is an error instead.
    #
    # Grafts: ``GIT_NO_REPLACE_OBJECTS`` does not switch them off. Git reads ``GIT_GRAFT_FILE``,
    # else ``info/grafts`` in the common git directory - which a worker can write from its
    # worktree - and a graft can give a commit any parents, so ``merge-base --is-ancestor`` would
    # answer about a history that is not in the objects (an integration could then take a
    # target tip for an ancestor of the candidate). Naming a graft file that never exists makes
    # git read none, inherited or written.
    env.update(
        {
            "GIT_AUTHOR_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_GRAFT_FILE": _absent_graft_file(),
        }
    )
    for name in (*_REPOSITORY_LOCATING_ENV, *_CONTENT_REDIRECTING_ENV):
        env.pop(name, None)
    # Appended after any ``GIT_CONFIG_COUNT`` entries the caller's environment already carries, so
    # those still apply and these, later in the same list, win. Git reads
    # ``GIT_CONFIG_PARAMETERS`` (what ``git -c`` exports) after that list, so when the caller
    # carries one the forced keys are appended to it as well, again last.
    try:
        count = max(int(env.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        count = 0
    for key, value in _forced_config():
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    inherited = env.get("GIT_CONFIG_PARAMETERS", "").strip()
    if inherited:
        forced = " ".join(
            f"{_sq_quote(key)}={_sq_quote(value)}" for key, value in _forced_config()
        )
        env["GIT_CONFIG_PARAMETERS"] = f"{inherited} {forced}"
    return env


def exit_status(returncode: int) -> str:
    """A process exit status as an operator can look it up.

    A Windows status that did not come from the program itself (``0xC0000142``: the process could
    not initialise, seen under load) reads as a huge decimal number; it is shown in hex. A negative
    POSIX status is the signal that ended the process.
    """
    if returncode < 0:
        return f"signal {-returncode}"
    if returncode > 255:
        return f"0x{returncode & 0xFFFFFFFF:08X}"
    return str(returncode)


def git_process(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    timeout: float = 300.0,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """``git <args>`` with binary pipes: the one place HFlow runs git to completion.

    Git that cannot start (a missing ``cwd``, no ``git`` at all) and a timeout raise
    :class:`GitError`. Input reaches git byte for byte (a text-mode stdin on Windows writes
    ``\\r\\n`` for ``\\n``); with nothing to feed, git reads no terminal and no inherited pipe - Git
    for Windows would otherwise ask "Should I try again? (y/n)" on the operator's terminal when it
    cannot delete a file. The output is returned as git wrote it: :func:`git_query` and
    :func:`git_run` decode it, and a caller that passes git's bytes through unread (``hflow diff``)
    takes them as they are. ``env`` defaults to :func:`_base_env`.
    """
    try:
        return subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["git", *args],
            cwd=None if cwd is None else str(cwd),
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_base_env() if env is None else env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise GitError(f"git {' '.join(args)} could not start: {exc}") from exc


class GitResult(NamedTuple):
    """One finished git call: its exit code and its decoded output."""

    returncode: int
    stdout: str
    stderr: str


def git_query(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    timeout: float = 300.0,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> GitResult:
    """``git <args>`` decoded; the caller interprets the exit code (:func:`git_run` raises on any).

    Standard output is decoded strictly as :data:`GIT_OUTPUT_ENCODING`, without newline
    translation: a path or a configuration value holding ``\\r`` arrives as git printed it (a
    text-mode pipe reads ``a\\r\\nb`` as ``a\\nb``, so a changed value could look unchanged), and
    output that is not UTF-8 raises :class:`GitError` - read as empty, "unreadable" would look like
    "nothing there", and a configuration value is raw bytes a worker can write. Standard error is
    only ever quoted in a message, so bytes in it that are not UTF-8 are replaced instead of
    failing a call that succeeded.
    """
    completed = git_process(args, cwd=cwd, timeout=timeout, stdin=stdin, env=env)
    try:
        stdout = completed.stdout.decode(GIT_OUTPUT_ENCODING)
    except UnicodeDecodeError as exc:
        raise GitError(_undecodable(tuple(args), exc.reason)) from exc
    stderr = (completed.stderr or b"").decode(GIT_OUTPUT_ENCODING, errors="replace")
    return GitResult(completed.returncode, stdout, stderr)


def git_run(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    timeout: float = 300.0,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """:func:`git_query` that raises :class:`GitError`, with git's own words, on a non-zero exit."""
    result = git_query(args, cwd=cwd, timeout=timeout, stdin=stdin, env=env)
    if result.returncode != 0:
        raise _git_failure(args, result.returncode, result.stderr)
    return result.stdout


def _git_failure(args: Sequence[str], returncode: int, stderr: str) -> GitError:
    """The error for a git call that exited non-zero: the exit status and git's own words."""
    detail = stderr.strip()[:300] or "no message"
    return GitError(f"git {' '.join(args)} failed (exit {exit_status(returncode)}): {detail}")


def _worktree_list_records(listing: str) -> list[dict[str, str]]:
    """``git worktree list --porcelain -z`` output as one dict per worktree, in git's order.

    ``-z`` (Git 2.36) prints every path raw and NUL-terminated, so a worktree path holding a
    space, a non-ASCII letter or a line break is read as git holds it. A record is its
    ``key value`` fields, each NUL-terminated, and ends with an empty field.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in listing.split("\0"):
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def registered_worktrees(common_dir: str) -> list[dict[str, str]]:
    """Every worktree registered in the git directory ``common_dir``, as git lists it.

    Named by ``--git-dir`` - explicit, so a bare repository is read too - and asked from inside that
    directory, so the answer never depends on any worktree, the one being removed included.
    """
    args = ("--git-dir", common_dir, "worktree", "list", "--porcelain", "-z")
    return _worktree_list_records(git_run(args, cwd=common_dir, timeout=60))


def _registered_at(records: list[dict[str, str]], path: str | os.PathLike[str]) -> int | None:
    """The position in ``records`` of the worktree registered at ``path`` (0 is the main one)."""
    for index, record in enumerate(records):
        recorded = record.get("worktree")
        if recorded and same_path(recorded, path):
            return index
    return None


def registration_for(common_dir: str, path: str | os.PathLike[str]) -> dict[str, str] | None:
    """The registration the git directory ``common_dir`` holds for ``path``, or ``None``.

    Asked of the common directory alone (:func:`registered_worktrees`), so the answer holds when
    the worktree's own ``.git`` file is gone.
    """
    records = registered_worktrees(common_dir)
    index = _registered_at(records, path)
    return None if index is None else records[index]


#: How git says "there is no repository here" (``setup.c``), read with git's messages untranslated.
_NOT_A_REPOSITORY = "not a git repository"


class GitRepo:
    """Read-mostly handle on a repository. Never mutates the user's checkout."""

    def __init__(self, root: Path, *, _identity: tuple[str, str] | None = None) -> None:
        """Open the repository whose work tree is ``root``: one git call.

        ``common_dir`` is the common git directory as git printed it, relative to ``root`` or
        absolute (:attr:`absolute_common_dir` resolves it), and ``head`` is HEAD's commit when the
        handle was made. Any failure, an unborn HEAD included, raises :class:`GitError`.
        ``_identity`` is ``(common_dir, head)`` as :meth:`discover` already read them.
        """
        self.root = Path(root).resolve()
        if _identity is None:
            _toplevel, common_dir, head = self._read_identity(self.root, discovering=False)
        else:
            common_dir, head = _identity
        self.common_dir = common_dir
        self.head = head

    # -- construction --------------------------------------------------------

    @staticmethod
    def _read_identity(cwd: Path, *, discovering: bool) -> tuple[str, str, str]:
        """``(top level or "", common git directory, HEAD commit)``: one ``rev-parse`` in ``cwd``.

        ``--verify`` makes HEAD exactly one full object id (an unborn HEAD fails) and
        ``--end-of-options`` keeps it a revision. When ``discovering``, git's own "not a git
        repository" answer (exit 128) is a ``SCOPE_VIOLATION`` refusal - read with git's messages
        in the C locale, so a translation cannot hide it. Anything else that is not a clean answer
        (git could not start, was killed, timed out, or printed something unexpected) is a
        :class:`GitError` with the exit status, never "not a repository".
        """
        args = (
            "rev-parse",
            *(("--show-toplevel",) if discovering else ()),
            "--git-common-dir",
            "--verify",
            "--end-of-options",
            "HEAD",
        )
        env = {**_base_env(), "LC_ALL": "C"} if discovering else None
        completed = git_query(args, cwd=cwd, timeout=30, env=env)
        if (
            discovering
            and completed.returncode == 128
            and _NOT_A_REPOSITORY in completed.stderr.casefold()
        ):
            raise RefusedError(
                RefusalCode.SCOPE_VIOLATION,
                f"{cwd} is not inside a git repository: {completed.stderr.strip()[:200]}",
            )
        if completed.returncode != 0:
            raise _git_failure(args, completed.returncode, completed.stderr)
        values = completed.stdout.splitlines()
        expected = 3 if discovering else 2
        if len(values) != expected or not all(values) or not is_full_oid(values[-1]):
            raise GitError(
                f"git {' '.join(args)} printed an unexpected answer: {completed.stdout[:200]!r}"
            )
        return (values[0] if discovering else "", values[-2], values[-1])

    @classmethod
    def discover(cls, candidate: Path) -> GitRepo:
        """The repository ``candidate`` is inside, by git's own discovery: one git call.

        Refuses (``SCOPE_VIOLATION``) only when git answers that there is no repository; any other
        failure raises :class:`GitError`.
        """
        toplevel, common_dir, head = cls._read_identity(Path(candidate), discovering=True)
        # Git prints a relative common directory relative to where it ran, not to the top level.
        common = os.path.join(os.path.abspath(candidate), common_dir)
        return cls(Path(toplevel), _identity=(common, head))

    @property
    def absolute_common_dir(self) -> str:
        """The common git directory, absolute and resolved: what two worktrees of it share."""
        return str((self.root / self.common_dir).resolve())

    # -- queries -------------------------------------------------------------

    def _verified_revision(self, revision: str, *, cwd: Path | None = None) -> str:
        """``git rev-parse --verify --end-of-options <revision>``: one object id, or GitError.

        ``--verify`` refuses anything but exactly one object (a range such as ``a..b`` prints two
        lines otherwise); ``--end-of-options`` keeps a value spelled like an option a revision
        (``rev-parse`` echoes an unknown option and exits 0).
        """
        return self.run("rev-parse", "--verify", "--end-of-options", revision, cwd=cwd).strip()

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        literal_pathspecs: bool = False,
        stdin: bytes | None = None,
    ) -> str:
        """``git <args>`` in ``cwd`` (the repository root by default): :func:`git_run`."""
        env = _base_env()
        if literal_pathspecs:
            # A scope entry is a path, never a pattern: '*', '?', '[' and ':(magic)' mean themselves.
            env["GIT_LITERAL_PATHSPECS"] = "1"
        return git_run(args, cwd=cwd or self.root, stdin=stdin, env=env)

    def query(self, *args: str, cwd: Path | None = None, timeout: float = 300.0) -> GitResult:
        """``git <args>`` in ``cwd`` whose exit code the caller interprets: :func:`git_query`."""
        return git_query(args, cwd=cwd or self.root, timeout=timeout)

    def status_report(self, cwd: Path | None = None) -> StatusReport:
        """Raw, machine-parsed status. Raises rather than guessing on odd records."""
        raw = self.run(
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
            cwd=cwd,
        )
        return parse_status_z(raw)

    def is_dirty(self) -> bool:
        """Does the user's own checkout have changes? Read-only: nothing is touched."""
        return bool(self.status_report().changed)

    def user_change_fingerprint(self, exclude: Iterable[Path] = ()) -> str:
        """A stat-level digest of the user's checkout, compared before and after a run.

        What it covers, and nothing more: HEAD as read now (its commit and the name it points
        at, never the value cached when this handle was made), the ``refs/stash`` value, a digest
        of ``git ls-files -s`` (every index entry's mode, blob id, stage and path), and for every
        entry of ``git status --porcelain --ignored`` - changed, untracked and ignored, with
        directories collapsed as git reports them - its path, its ``XY`` code, and the size and
        ``mtime_ns`` that ``os.stat`` reports (``missing`` when there is nothing to stat). No
        working file is opened, and the status read takes no optional lock, so it does not
        rewrite ``.git/index``.

        Not detected: a rewrite that keeps both a file's size and its mtime, and a change inside
        a collapsed untracked or ignored directory that leaves the directory's own stat as it
        was. ``exclude`` names HFlow's own directories (its data directory, for example); an
        entry at or under one is left out, and a collapsed directory holding one is recorded
        without its stat, since HFlow's writes change it legitimately.
        """
        root = Path(os.path.realpath(self.root))
        fold_case = os.name == "nt"
        excluded: list[str] = []
        for path in exclude:
            try:
                relative = Path(os.path.realpath(path)).relative_to(root).as_posix()
            except ValueError:
                continue
            if relative != ".":
                excluded.append(relative.casefold() if fold_case else relative)
        stash = self.query("rev-parse", "-q", "--verify", "refs/stash", timeout=30)
        head_name = self.query("symbolic-ref", "-q", "HEAD", timeout=30)
        index = self.run("ls-files", "-s", "-z")
        raw = self.run(
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignored",
        )
        entries: list[list[object]] = []
        for _record, code, new_path, source in _status_records(raw):
            for path in (new_path, source) if source else (new_path,):
                key = path.rstrip("/").casefold() if fold_case else path.rstrip("/")
                if any(key == prefix or key.startswith(prefix + "/") for prefix in excluded):
                    continue
                if any(prefix.startswith(key + "/") for prefix in excluded):
                    entries.append([path, code, "holds an HFlow directory"])
                    continue
                try:
                    info = os.stat(root / path.rstrip("/"), follow_symlinks=False)
                except OSError:
                    entries.append([path, code, "missing"])
                    continue
                entries.append([path, code, info.st_size, info.st_mtime_ns])
        return digest_of(
            {
                "head": self._verified_revision("HEAD"),
                "head_name": head_name.stdout.strip() if head_name.returncode == 0 else "",
                "stash": stash.stdout.strip() if stash.returncode == 0 else "",
                "index": "sha256:"
                + hashlib.sha256(index.encode("utf-8", "surrogateescape")).hexdigest(),
                "status": entries,
            }
        )

    def resolve_commit(self, ref: str = "HEAD") -> str:
        return self._verified_revision(f"{ref}^{{commit}}")

    # -- worktrees -----------------------------------------------------------

    def worktree_parent(self) -> Path:
        """Where a run's worktree goes: beside the repository, never inside it."""
        return self.root.parent / f"{self.root.name}.hflow-worktrees"

    def validate_reusable_worktree(self, target: Path, base_commit: str) -> None:
        """Refuse a foreign, modified or linked target before it is given to a worker."""
        for entry in (target.parent, target, target / ".git"):
            if entry.is_symlink() or entry.is_junction():
                raise GitError(f"worktree reuse refused: linked path {entry}")
        if not target.is_dir() or not (target / ".git").is_file():
            raise GitError(f"worktree reuse refused: {target} is not a linked Git worktree")
        actual = GitRepo.discover(target)
        if not same_path(actual.root, target) or not same_path(
            actual.absolute_common_dir, self.absolute_common_dir
        ):
            raise GitError("worktree reuse refused: repository identity differs")
        registration, is_main = self.registration(target)
        if registration is None or is_main:
            raise GitError("worktree reuse refused: missing registration or source checkout")
        if actual.resolve_commit("HEAD") != base_commit:
            raise GitError("worktree reuse refused: HEAD differs from the admitted base")
        if actual.index_flagged_paths(target):
            raise GitError("worktree reuse refused: index flags can hide worktree changes")
        status = actual.status_report()
        if status.changed or status.ignored or status.unsupported:
            raise GitError("worktree reuse refused: worktree is not clean")

    def create_worktree(self, run_id: str, base_commit: str) -> Path:
        """Attach a detached worktree at ``base_commit``.

        Created beside the repository rather than inside it, so the user's working tree never
        contains run scaffolding and a candidate snapshot cannot accidentally include it.

        The worktree is added at a staging path - ``<run>.staging-<pid>-<random>``, so neither a
        reused process id nor a leftover from a crashed process collides with it - and then
        ``git worktree move`` puts it in place: a rename of a complete checkout, so the target is
        never half made, and git updates that one worktree's administrative files. Never
        ``git worktree repair``, which rewrites *every* linked worktree's ``.git`` file - the
        user's own included, absolute to relative under ``worktree.useRelativePaths`` (rule 12).

        Any failure, an ``OSError`` from the file system included (an antivirus or indexer holding
        the staging directory), raises :class:`GitError`; the staging worktree is then removed
        through git, without force, and named in the error when it could not be.
        """
        parent = self.worktree_parent()
        target = parent / run_id
        try:
            if parent.is_symlink() or parent.is_junction():
                raise GitError(f"worktree creation refused: linked parent {parent}")
            parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                self.validate_reusable_worktree(target, base_commit)
                return target
            staging = parent / f"{run_id}.staging-{os.getpid()}-{secrets.token_hex(4)}"
            if os.path.lexists(staging):
                raise GitError(f"worktree creation refused: staging path already exists: {staging}")
        except OSError as exc:
            raise GitError(f"worktree creation failed: {exc}") from exc
        revision = _revision(base_commit, "base")
        try:
            # ``--`` ends the options, so neither a path nor the commit can read as one.
            self.run("worktree", "add", "--detach", "--", str(staging), revision)
            self.run("worktree", "move", "--", str(staging), str(target))
        except GitError as exc:
            raise GitError(f"worktree creation failed: {exc}{self._discard_staging(staging)}") from exc
        return target

    def _discard_staging(self, staging: Path) -> str:
        """Remove a staging worktree a failed :meth:`create_worktree` left, through git only.

        Best effort and never forced: ``git worktree remove`` refuses anything but a clean
        checkout. Returns ``""`` when nothing is left, else a note for the error message.
        """
        try:
            if self.worktree_registration(staging) is not None:
                self.run("worktree", "remove", "--", str(staging))
            if not os.path.lexists(staging):
                return ""
            return f"; the staging directory {staging} was left in place"
        except (GitError, OSError) as exc:
            return f"; the staging worktree {staging} could not be removed: {exc}"

    def worktree_commit(self, worktree: Path) -> str:
        return self._verified_revision("HEAD", cwd=worktree)

    def worktree_tree(self, worktree: Path) -> str:
        return self._verified_revision("HEAD^{tree}", cwd=worktree)

    def commit_exists(self, commit: str) -> bool:
        """Is this commit an object in this repository?

        Used where a caller must not assume a recorded SHA is still present: a base commit can be
        named by a task file that was written against a different clone, and a delivery diff that
        silently produced an empty path list would look like "this delivery changed nothing".
        """
        if not commit:
            return False
        return self.query("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0

    def diff_paths(self, base: str, candidate: str, *, cwd: Path | None = None) -> list[str]:
        """Every path in which ``candidate`` differs from ``base``.

        This is the *cumulative* change, which is what a delivery has to name: a repaired run's
        change is the whole thing from the task's original base to the final candidate, not the
        last round's patch. Computed by Git rather than by remembering earlier rounds, so a round
        that changed nothing, reverted something or touched a path twice still yields one answer
        that a reader can reproduce with the same two commits.
        """
        if not base or not candidate:
            raise GitError("a delivery diff needs both a base and a candidate commit")
        # ``--no-renames``: with rename detection (git's default, or whatever ``diff.renames`` the
        # user configured) ``--name-only`` prints only the new name of a moved file, and the
        # deleted source path would silently drop out of the change.
        #
        # ``--``: without it git also checks that the range does not name a file, by stat()ing it
        # in the work tree. A file spelled like the range makes the range "ambiguous", and on
        # Windows a deep work tree makes the stat itself fail ("Filename too long"); either way
        # the diff died after the implementer was paid.
        rev_range = f"{_revision(base, 'base')}..{_revision(candidate, 'candidate')}"
        out = self.run("diff", "--no-renames", "--name-only", "-z", rev_range, "--", cwd=cwd)
        return [path for path in out.split("\0") if path.strip()]

    def committed_bytecode_paths(self, commit: str) -> list[str]:
        """The ``.pyc`` paths under a ``__pycache__`` directory that ``commit`` itself tracks.

        Read from the commit's tree object (``ls-tree``), never from a working tree or the index,
        so no filter, hook or fsmonitor configured in a worktree runs. These are committed bytes:
        the bytecode removal before a worktree run's checks keeps them.
        """
        out = self.run(
            "ls-tree", "-r", "-z", "--name-only", "--end-of-options", _revision(commit, "commit")
        )
        return [path for path in out.split("\0") if path and all(_bytecode(path))]

    def diff_text_bounded(
        self,
        base: str,
        candidate: str,
        path: str,
        *,
        max_bytes: int,
        cwd: Path | None = None,
        timeout: float = 60.0,
    ) -> tuple[str, bool]:
        """The unified diff of one path between two commits, read up to ``max_bytes`` bytes.

        Returns ``(text, truncated)``. At most ``max_bytes + 1`` bytes are read from git; when
        there are more, git is stopped and ``truncated`` is true, so a worker that wrote a huge
        file cannot make HFlow hold it in memory. Bytes that are not UTF-8 are replaced, never
        guessed. No external diff program and no textconv filter runs (``--no-ext-diff
        --no-textconv``), renames are not detected, and the path is a literal pathspec. Used only
        to show a reviewer a bounded excerpt; the full change stays reachable by its commits.
        """
        if not base or not candidate:
            raise GitError("a bounded diff needs both a base and a candidate commit")
        args = (
            "diff",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--unified=3",
            f"{base}..{candidate}",
            "--",
            path,
        )
        env = _base_env()
        env["GIT_LITERAL_PATHSPECS"] = "1"
        limit = max(int(max_bytes), 0)
        with tempfile.TemporaryFile() as stderr_file:
            try:
                proc = subprocess.Popen(  # noqa: S603,S607 - fixed argv, no shell
                    ["git", *args],
                    cwd=str(cwd or self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    env=env,
                )
            except OSError as exc:
                raise GitError(f"git {' '.join(args[:-2])} could not start: {exc}") from exc
            timer_fired: list[bool] = []

            def _expire() -> None:
                timer_fired.append(True)
                proc.kill()

            timer = threading.Timer(timeout, _expire)
            timer.start()
            chunks: list[bytes] = []
            read = 0
            truncated = False
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(min(65536, limit + 1 - read))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    read += len(chunk)
                    if read > limit:
                        truncated = True
                        proc.kill()
                        break
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
                proc.wait()
                timer.cancel()
            if timer_fired:
                raise GitError(f"git {' '.join(args[:-2])} timed out after {timeout}s")
            if not truncated and proc.returncode != 0:
                stderr_file.seek(0)
                detail = stderr_file.read(300).decode("utf-8", errors="replace").strip()
                raise GitError(f"git {' '.join(args[:-2])} failed: {detail}")
        data = b"".join(chunks)[:limit]
        return data.decode("utf-8", errors="replace"), truncated

    def index_flagged_paths(self, worktree: Path) -> list[str]:
        """Index entries whose worktree changes git does not look at.

        ``git ls-files -v`` tags an assume-unchanged entry with a lowercase letter and a
        skip-worktree entry with ``S``. For either, status and ``git add`` trust the index instead
        of the file, so an edit to it is invisible to every read the freeze relies on.
        """
        flagged: list[str] = []
        for record in self.run("ls-files", "-v", "-z", cwd=worktree).split("\0"):
            if len(record) < 3 or record[1] != " ":
                continue
            tag, path = record[0], record[2:]
            if tag.islower() or tag == "S":
                flagged.append(path)
        return flagged

    def _global_attributes_file(self, cwd: Path) -> str:
        """The global attributes file git reads in ``cwd``, or ``""`` when it reads none."""
        query = self.query("var", "GIT_ATTR_GLOBAL", cwd=cwd, timeout=30)
        if query.returncode == 0:
            return query.stdout.strip()
        if query.returncode == 1 and not query.stdout.strip():
            # Git 2.42+ names no file when ``core.attributesFile`` is unset and neither
            # XDG_CONFIG_HOME nor HOME is set; git then reads none.
            return ""
        if query.returncode != 129:
            raise GitError(f"git var GIT_ATTR_GLOBAL failed: {query.stderr.strip()[:200]}")
        # 129: a Git older than 2.42, which does not know the variable. Git's own rule, by hand:
        # ``core.attributesFile``, else ``$XDG_CONFIG_HOME/git/attributes``, else
        # ``~/.config/git/attributes``.
        configured = self.query(
            "config", "--type=path", "--get", "core.attributesFile", cwd=cwd, timeout=30
        )
        if configured.returncode == 0:
            return configured.stdout.strip()
        if configured.returncode != 1:
            raise GitError(
                f"git config --get core.attributesFile failed: {configured.stderr.strip()[:200]}"
            )
        xdg = _base_env().get("XDG_CONFIG_HOME", "")
        if xdg:
            return (Path(xdg) / "git" / "attributes").as_posix()
        # Git expands ``~`` with its own idea of HOME, which Git for Windows derives when HOME is
        # unset; with no home at all the expansion fails and git reads no global file.
        default = self.query(
            "config",
            "--type=path",
            "--default",
            "~/.config/git/attributes",
            "--get",
            "core.attributesFile",
            cwd=cwd,
            timeout=30,
        )
        return default.stdout.strip() if default.returncode == 0 else ""

    def metadata_snapshot(self, worktree: Path) -> GitMetadataSnapshot:
        """Digest the shared metadata HFlow's own git commands read from outside the candidate.

        A worker can write all of it from inside its worktree - the repository's config is shared
        by every worktree - and it decides what HFlow's later ``git add`` / ``git status`` execute
        and stage: a ``filter.<driver>.clean`` / ``smudge`` / ``process`` program, the attribute
        mapping that selects one (``info/attributes`` or ``core.attributesFile``; no
        ``.gitattributes`` is needed), ``include.path`` / ``includeIf`` files, and
        ``extensions.worktreeConfig`` with the ``config.worktree`` files it enables. Filters cannot
        be switched off generically without breaking legitimate ones such as LFS, so the controller
        compares this snapshot instead and refuses to run git on metadata that changed.

        Read in both places HFlow runs git after a worker: the user's checkout (the acceptance's
        status read, ref writes) and the run's worktree (the freeze). Per place: every key
        ``git config --list`` reports, includes expanded - ``command``-scope entries are skipped,
        being HFlow's own environment (the forced keys) rather than a file - and every file it
        names as an origin, plus ``config``, ``config.worktree`` and ``info/attributes`` under
        ``git rev-parse --git-path`` and the global attributes file, present or not. Read-only:
        none of these commands runs a filter. Any failure, a timeout included, raises
        :class:`GitError` - and so does output that is not text (a configuration value is raw
        bytes a worker can write): every read goes through ``run`` or ``query``
        (:func:`git_query`).

        Needs Git 2.31+ (``rev-parse --path-format=absolute``; ``--show-scope`` is 2.26). The
        global attributes file comes from ``git var GIT_ATTR_GLOBAL`` on Git 2.42+ and from git's
        own rule on older Git.
        """
        entries: dict[str, str] = {}
        files: set[str] = set()
        for context, cwd in (("checkout", self.root), ("worktree", Path(worktree))):
            by_key: dict[str, list[list[str | None]]] = {}
            listing = self.run("config", "--list", "-z", "--show-scope", "--show-origin", cwd=cwd)
            for scope, origin, key, value in _config_records(listing):
                if scope == "command":
                    continue
                by_key.setdefault(key, []).append([scope, origin, value])
                if origin.startswith("file:"):
                    files.add(_metadata_path(cwd, origin[len("file:") :]))
            for key, records in by_key.items():
                entries[f"config@{context}:{key}"] = digest_of(records)
            located = self.run(
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "config",
                "--git-path",
                "config.worktree",
                "--git-path",
                "info/attributes",
                cwd=cwd,
            )
            files.update(_metadata_path(cwd, line) for line in located.splitlines() if line.strip())
            attributes = self._global_attributes_file(cwd)
            if attributes:
                files.add(_metadata_path(cwd, attributes))
        for path in files:
            entries[f"file:{path}"] = _file_digest(Path(path))
        return GitMetadataSnapshot(tuple(sorted(entries.items())))

    def freeze_candidate(
        self,
        worktree: Path,
        allow: list[str],
        message: str = "hflow: candidate",
        *,
        deny: list[str] | None = None,
        allow_ignored: list[str] | None = None,
        fingerprinted: Iterable[str] = (),
        expected_head: str,
    ) -> CandidateFreeze:
        """Commit the declared paths and return the candidate's explicit identity.

        ``expected_head`` is the commit this round started from: the task's original base in the
        first round, the previous candidate in a repair. The worktree's HEAD must still be that
        commit. A worker that ran ``git commit``, ``--amend``, ``reset`` or ``checkout`` moved
        HEAD, and its commit never passed through the scope and deny checks below (they read the
        uncommitted status, which a commit leaves clean), so the freeze is refused
        (:class:`GitStatusParseError`) rather than building on, or delivering, that commit. The
        returned ``base_commit`` is therefore always ``expected_head``.

        Only the authorized entries are staged, one by one - never a whole-worktree
        ``git add -A``, so a stray log, credential file or runtime artifact in the worktree cannot
        be collected into the candidate by accident. Each entry is staged with
        ``git add -A -- <entry>`` under literal pathspecs, whether or not it still exists on disk,
        so a listed file the worker deleted is committed as a deletion and an entry is never read
        as a pattern.

        ``deny`` carries the task's and the project's ``write_deny`` rules; the built-in deny list
        (:data:`~hflow.workspace.BUILTIN_WRITE_DENY`) always applies on top. A changed path under
        any of them refuses the freeze before anything is staged, so a denied change never enters
        a candidate commit, even inside an allowed directory.

        The commit must be the tree the checks are about to see. If the worktree still reports a
        change after staging and committing, the freeze is refused as incomplete (a
        :class:`GitError`) instead of naming a commit that lacks part of the checked change.

        ``allow_ignored`` lets a caller name ignored byproducts it accepts as check-generated
        noise (for example a bytecode cache). Anything ignored that is not named there makes
        the freeze refuse: ignored is not the same as disposable. Two kinds of ignored file
        refuse even when ``allow_ignored`` names them, in every round, because the checks would
        read bytes the commit does not hold: one listed in ``fingerprinted`` (the worktree-relative
        paths :func:`~hflow.workspace.expand_scope` hashes into the scoped fingerprint), and
        sourceless bytecode (:func:`is_sourceless_bytecode`).

        An index entry flagged assume-unchanged or skip-worktree (see
        :meth:`index_flagged_paths`) makes status, ``git add`` and the staged diff skip that
        file's edits, so the commit would hold old bytes while the checks read new ones. The
        freeze is refused (:class:`GitStatusParseError`) rather than clearing the flag.
        """
        head = self.worktree_commit(worktree)
        if not expected_head or head != expected_head:
            raise GitStatusParseError(
                f"the worker moved HEAD from {expected_head or '(no recorded start)'} to {head}; "
                "a commit made inside the worktree is never frozen as a candidate"
            )
        base_commit = expected_head
        flagged = self.index_flagged_paths(worktree)
        if flagged:
            raise GitStatusParseError(
                "index flags hide worktree changes from the freeze (assume-unchanged or "
                "skip-worktree): " + brief_list(flagged)
            )
        report = self.status_report(worktree)
        if report.unsupported:
            raise GitStatusParseError(
                "unsupported git status records: " + "; ".join(report.unsupported[:3])
            )
        permitted = list(allow_ignored or [])
        unexpected_ignored = [
            path for path in report.ignored if not matches_pattern(path, permitted)
        ]
        if unexpected_ignored:
            raise GitStatusParseError(
                "refusing to freeze with unrecognised ignored paths: " + brief_list(unexpected_ignored)
            )
        # An allowlisted ignored file is never staged. Inside the scoped fingerprint it would be
        # hashed and checked without any candidate commit holding it.
        fingerprinted_set = set(fingerprinted)
        covered = [path for path in report.ignored if path in fingerprinted_set]
        if covered:
            raise GitStatusParseError(
                f"ignored file(s) inside write_allow ({brief_list(covered)}); the scoped "
                "fingerprint would cover bytes no candidate commit holds, so the freeze is refused "
                "rather than deleting them"
            )
        # Bytecode outside ``__pycache__`` is imported in place of a missing source file, and
        # CPython 3 never writes it there itself: it is a worker's file, not a check byproduct.
        sourceless = [path for path in report.ignored if is_sourceless_bytecode(path)]
        if sourceless:
            raise GitStatusParseError(
                f"ignored sourceless bytecode outside __pycache__ ({brief_list(sourceless)}); "
                "Python imports it in place of a missing source file and no candidate commit "
                "holds it, so the freeze is refused rather than deleting it"
            )
        outside = [path for path in report.changed if not matches_pattern(path, allow)]
        if outside:
            raise GitStatusParseError(
                "worker changed unauthorised paths: " + brief_list(outside)
            )
        deny_rules = [*(deny or []), *BUILTIN_WRITE_DENY]
        denied = [path for path in report.changed if matches_pattern(path, deny_rules)]
        if denied:
            raise GitStatusParseError("worker changed denied paths: " + brief_list(denied))
        for entry in allow:
            # git refuses a pathspec that matches nothing, so an entry the worker never created
            # (absent on disk and unknown to the index) has nothing to stage. A tracked file that
            # was deleted is still known to the index, and staging it records the deletion.
            known = os.path.lexists(worktree / entry) or bool(
                self.run("ls-files", "-z", "--", entry, cwd=worktree, literal_pathspecs=True)
            )
            if known:
                self.run("add", "-A", "--", entry, cwd=worktree, literal_pathspecs=True)
        # ``--no-renames``: a staged move lists its deleted source too, so the guard below sees a
        # denied path that was moved away, and ``paths`` names both sides.
        staged = [
            path
            for path in self.run(
                "diff",
                "--cached",
                "--no-renames",
                "--name-only",
                "-z",
                cwd=worktree,
                literal_pathspecs=True,
            ).split("\0")
            if path.strip()
        ]
        # Whatever reached the index between the status read and the staging is held to the same
        # rules: nothing outside the scope or under a deny rule is ever committed.
        stray = [
            path
            for path in staged
            if not matches_pattern(path, allow) or matches_pattern(path, deny_rules)
        ]
        if stray:
            raise GitStatusParseError(
                "refusing to commit paths outside the write scope: " + brief_list(stray)
            )
        if staged:
            # ``--no-verify`` on top of the empty ``core.hooksPath``: no pre-commit or commit-msg
            # hook runs even if the forced configuration were ever lost.
            self.run("commit", "--no-verify", "-q", "-m", message, cwd=worktree)
        leftover = self.status_report(worktree)
        if leftover.changed or leftover.unsupported:
            raise GitError(
                "freeze incomplete: the worktree still has changes the candidate commit does not "
                "hold: " + brief_list([*leftover.changed, *leftover.unsupported])
            )
        return CandidateFreeze(
            base_commit=base_commit,
            candidate_commit=self.worktree_commit(worktree),
            tree=self.worktree_tree(worktree),
            paths=tuple(sorted(staged)),
        )

    # -- refs -----------------------------------------------------------------

    @staticmethod
    def _ref_components(*labelled: tuple[str, str]) -> None:
        """Refuse an identifier that is not letters, digits, ``-`` and ``_`` as a ref component."""
        for label, value in labelled:
            if not value or not value.replace("-", "").replace("_", "").isalnum():
                raise GitError(f"refusing to build a ref from an invalid {label}: {value!r}")

    def candidate_ref(self, run_id: str, attempt_id: str) -> str:
        """The HFlow-owned ref that keeps a candidate reachable after its worktree is gone.

        The name is built from controller-validated identifiers, never from model text. It is
        *not* an acceptance mark: failed candidates are kept too.
        """
        self._ref_components(("run id", run_id), ("attempt id", attempt_id))
        return f"refs/hflow/candidates/{run_id}/{attempt_id}"

    def integration_ref(self, run_id: str, integration_id: str) -> str:
        """The HFlow-owned ref that keeps an integration commit reachable, whatever the target does.

        Built like :meth:`candidate_ref`, from controller-validated identifiers only.
        """
        self._ref_components(("run id", run_id), ("integration id", integration_id))
        return f"refs/hflow/integrations/{run_id}/{integration_id}"

    def ref_target(self, ref: str) -> str | None:
        """The object id ``ref`` resolves to, or ``None`` when it resolves to nothing.

        ``None`` is only git's documented "no such object" answer: ``rev-parse --verify --quiet``
        exits 1 and prints nothing. Anything else - git that could not start or was killed, a
        timeout, output that is not an object id - raises :class:`GitError`: a failed read must
        never pass for a deleted branch (an integration would be recorded stale on it).
        """
        args = ("rev-parse", "--verify", "--quiet", "--end-of-options", ref)
        completed = self.query(*args, timeout=30)
        target = completed.stdout.strip()
        if completed.returncode == 1 and not target:
            return None
        if completed.returncode != 0:
            raise _git_failure(args, completed.returncode, completed.stderr)
        if not is_full_oid(target):
            raise GitError(
                f"git {' '.join(args)} printed {target[:80]!r} where an object id belongs"
            )
        return target

    def ref_spelled_exactly(self, ref: str) -> bool:
        """Does a ref with exactly this name - byte for byte, case included - exist?

        ``rev-parse`` is not enough on a case-insensitive file system: ``refs/heads/MAIN`` resolves
        through the loose file ``refs/heads/main``, so a lookup by a differently cased name finds
        the branch while every comparison against the real name (which worktree has it checked
        out, which ref an update locks) silently misses it. ``for-each-ref`` matches the stored
        names, loose and packed, case-sensitively.
        """
        listing = self.run("for-each-ref", "--format=%(refname)", ref)
        return ref in listing.splitlines()

    def ensure_ref(self, ref: str, commit: str) -> str:
        """Create ``ref`` at ``commit`` only if absent; ``"reused"`` if it already points there.

        ``git update-ref`` with the empty old value is a create-if-absent operation, so an
        existing ref - including a user's - is never overwritten. A conflicting value is
        refused instead. ``ref`` must be a full name under ``refs/`` (never ``HEAD`` or another
        pseudo-ref) and ``commit`` a full object id, so "already points there" is a plain
        comparison. Returns ``"created"`` or ``"reused"``.
        """
        if not isinstance(ref, str) or not ref.startswith("refs/") or any(c in ref for c in "\0\n\r"):
            raise GitError(f"refusing to create {ref!r}: not a full ref name under refs/")
        if not is_full_oid(commit):
            raise GitError(f"refusing to point {ref} at {commit!r}: not a full object id")
        existing = self.ref_target(ref)
        if existing is not None:
            if existing == commit:
                return "reused"
            raise GitError(f"ref {ref} already exists at {existing} and is not {commit}")
        try:
            self.run("update-ref", ref, commit, "")
        except GitError as exc:
            # Lost a race, or the ref appeared: re-read and treat a match as success.
            current = self.ref_target(ref)
            if current == commit:
                return "reused"
            raise GitError(f"could not create {ref}: {exc}") from exc
        return "created"

    #: The candidate ref is created exactly like any other HFlow-owned ref.
    ensure_candidate_ref = ensure_ref

    # -- integration primitives ----------------------------------------------
    #
    # Plumbing only: none of these reads or writes an index or a working tree, so the user's
    # checkout is never touched. Revisions are refused when they could read as an option.

    @staticmethod
    def _object_id(stdout: str, args: tuple[str, ...]) -> str:
        oid = stdout.strip()
        if not is_full_oid(oid):
            raise GitError(f"git {' '.join(args)} printed {oid[:80]!r} where an object id belongs")
        return oid

    def git_version(self) -> tuple[int, int, int]:
        """The running git's version, ``(major, minor, patch)``; see :func:`parse_git_version`."""
        return parse_git_version(self.run("version"))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """``git merge-base --is-ancestor``: exit 0 is yes, 1 is no, anything else raises.

        A commit is its own ancestor. A missing or non-commit object is an error, never a "no".
        """
        args = (
            "merge-base",
            "--is-ancestor",
            "--end-of-options",
            _revision(ancestor, "ancestor"),
            _revision(descendant, "descendant"),
        )
        returncode, _, stderr = self.query(*args)
        if returncode in (0, 1):
            return returncode == 0
        raise _git_failure(args, returncode, stderr)

    def branch_ref(self, branch: str) -> str:
        """``refs/heads/<branch>`` for a plain branch name, validated by git itself.

        ``git check-ref-format --branch`` must print the name back unchanged: it expands
        ``@{-1}`` and similar shorthands, and those never name a fixed branch. Refused before git
        is asked: empty, ``-`` prefixed (an option), ``refs/`` prefixed (a full ref, which would
        become ``refs/heads/refs/...``), ``HEAD``, ``@`` (HEAD's shorthand) and anything holding
        ``@{``, NUL or a line break.
        """
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith(("-", "refs/"))
            or branch in {"HEAD", "@"}
            or "@{" in branch
            or any(char in branch for char in "\0\n\r")
        ):
            raise GitError(f"refusing branch name {branch!r}")
        returncode, stdout, stderr = self.query("check-ref-format", "--branch", branch)
        if returncode != 0 or stdout.removesuffix("\n") != branch:
            raise GitError(f"refusing branch name {branch!r}: {stderr.strip()[:200] or stdout.strip()[:200]}")
        return f"refs/heads/{branch}"

    def tree_of(self, commit: str) -> str:
        """The tree object id of ``commit`` (``rev-parse --verify <commit>^{tree}``)."""
        args = ("rev-parse", "--verify", "--end-of-options", f"{_revision(commit, 'commit')}^{{tree}}")
        return self._object_id(self.run(*args), args)

    def merge_tree(self, *, base: str, ours: str, theirs: str) -> MergeTreeResult:
        """Three-way merge of ``ours`` and ``theirs`` over ``base``, as objects only.

        ``git merge-tree --write-tree -z --name-only --no-messages --merge-base=<base>``: no index
        and no working tree is read or written; the result tree and its blobs are written to the
        object store. Exit 0 is a clean merge, 1 a conflicted one (git still prints a tree, which
        holds conflict markers and must not be committed), anything else raises. Requires Git 2.40
        (:data:`MIN_INTEGRATION_GIT`).
        """
        args = (
            "merge-tree",
            "--write-tree",
            "-z",
            "--name-only",
            "--no-messages",
            f"--merge-base={_revision(base, 'merge base')}",
            "--end-of-options",
            _revision(ours, "ours"),
            _revision(theirs, "theirs"),
        )
        returncode, stdout, stderr = self.query(*args)
        if returncode not in (0, 1):
            raise _git_failure(args, returncode, stderr)
        return _merge_tree_result(stdout, clean=returncode == 0, args=args)

    def commit_tree(self, tree: str, *, parents: Sequence[str], message: str) -> str:
        """Write a commit of ``tree`` with ``parents`` and ``message``; return its object id.

        ``git commit-tree <tree> -p <parent>... -F -``: the message travels on stdin as UTF-8,
        never on the command line; author and committer are HFlow's fixed identity from
        :func:`_base_env`; ``i18n.commitEncoding`` is pinned to UTF-8 so a user's setting cannot
        add an encoding header. Refused: an empty message, one holding NUL, and a repeated
        parent (git would drop it with only a warning). No ref moves.
        """
        if isinstance(parents, str):
            raise GitError("parents must be a sequence of revisions, not one string")
        parent_list = [_revision(parent, "parent") for parent in parents]
        if len(set(parent_list)) != len(parent_list):
            raise GitError(f"refusing a commit with a repeated parent: {parent_list}")
        if not isinstance(message, str) or not message.strip():
            raise GitError("refusing a commit with an empty message")
        if "\0" in message:
            raise GitError("refusing a commit message that holds a NUL")
        try:
            body = message.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GitError(f"refusing a commit message that is not valid text ({exc.reason})") from exc
        args = ["-c", "i18n.commitEncoding=UTF-8", "commit-tree", _revision(tree, "tree")]
        for parent in parent_list:
            args += ["-p", parent]
        args += ["-F", "-"]
        return self._object_id(self.run(*args, stdin=body), tuple(args))

    def update_ref_cas(self, ref: str, new: str, expected_old: str, *, reason: str) -> None:
        """Move branch ``ref`` from ``expected_old`` to ``new``, atomically, or raise.

        ``git update-ref --no-deref -m <reason> <ref> <new> <expected_old>``: git takes the ref's
        lock, checks the current value is ``expected_old`` and writes ``new`` under that lock - a
        compare-and-set. Only an existing branch (``refs/heads/``, validated as by
        :meth:`branch_ref`) moves, and only between two full object ids: a zero id (create or
        delete), an abbreviated id and a symbolic ref are refused, so this never creates, deletes
        or retargets a ref. Any failure - a stale ``expected_old`` included - raises
        :class:`GitError` and leaves the ref as it was. Git does not check whether the branch is
        checked out anywhere; that is :meth:`checked_out_at`, the caller's question.
        """
        if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
            raise GitError(f"refusing to update {ref!r}: only a branch under refs/heads/ moves")
        if self.branch_ref(ref.removeprefix("refs/heads/")) != ref:
            raise GitError(f"refusing to update {ref!r}: not a valid branch ref")
        for label, value in (("new", new), ("expected old", expected_old)):
            if not is_full_oid(value) or set(value) == {"0"}:
                raise GitError(f"refusing to update {ref}: {label} value {value!r} is not a full object id")
        if not isinstance(reason, str) or not reason.strip() or any(c in reason for c in "\0\n\r"):
            raise GitError(f"refusing to update {ref}: the reflog reason must be one non-empty line")
        args = ("symbolic-ref", "-q", ref)
        returncode, stdout, stderr = self.query(*args)
        if returncode == 0:
            raise GitError(f"refusing to update {ref}: it is a symbolic ref to {stdout.strip()}")
        if returncode != 1:
            raise _git_failure(args, returncode, stderr)
        self.run("update-ref", "--no-deref", "-m", reason, ref, new, expected_old)

    def worktree_blocks(self) -> list[dict[str, str]]:
        """All worktree registrations, in git's order (the main worktree comes first).

        One dict per worktree from ``git worktree list --porcelain -z``
        (:func:`_worktree_list_records`).
        """
        return _worktree_list_records(self.run("worktree", "list", "--porcelain", "-z"))

    def checked_out_at(self, ref: str) -> list[Path]:
        """Every worktree that holds ``ref`` checked out, as git itself counts it.

        A worktree holds a branch when its ``HEAD`` names it (the ``branch`` line of ``git
        worktree list``) or when an operation in it will return to or move the branch: a rebase
        (``head-name``), a bisect (``BISECT_START``) or a ``rebase --update-refs`` - git's
        ``prepare_checked_out_branches`` in ``branch.c``, a superset of what ``worktree.c``'s
        ``is_worktree_being_rebased`` / ``is_worktree_being_bisected`` / ``find_shared_symref``
        check. The main worktree's state lives in the common git directory, a linked worktree's in
        ``worktrees/<id>``, found as git finds it: by the ``gitdir`` file there. A bare main
        worktree is skipped, as git skips it. A ref name that differs only in letter case counts
        as ``ref`` (:func:`_same_ref`): on NTFS a HEAD naming ``refs/heads/Main`` holds ``main``.

        Returns resolved paths, deduplicated, in ``git worktree list`` order; a worktree whose
        state names the branch but that the listing does not show comes last. Read-only.
        """
        records = self.worktree_blocks()
        common = Path(
            self.run("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
        # Linked worktree administrative directories, by the worktree path git derives from each
        # ``gitdir`` file (``worktree.c`` ``get_linked_worktree``). One whose file is unreadable
        # or empty is not a worktree to git either.
        linked: dict[str, tuple[Path, str]] = {}
        try:
            entries = sorted(os.scandir(common / "worktrees"), key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            entries = []
        except OSError as exc:
            raise GitError(f"cannot list {common / 'worktrees'}: {exc}") from exc
        for entry in entries:
            if not entry.is_dir():
                continue
            admin_dir = Path(entry.path)
            recorded = _read_state_file(admin_dir / "gitdir")
            if recorded is None:
                continue
            path_text = recorded.rstrip().removesuffix("/.git")
            if not os.path.isabs(path_text):
                path_text = os.path.realpath(os.path.join(admin_dir, path_text))
            linked.setdefault(_path_key(path_text), (admin_dir, path_text))

        found: list[Path] = []
        seen: set[str] = set()
        listed: set[str] = set()

        def add(path_text: str) -> None:
            key = _path_key(path_text)
            if key not in seen:
                seen.add(key)
                found.append(Path(path_text).resolve())

        for index, record in enumerate(records):
            path_text = record.get("worktree", "")
            if not path_text:
                continue
            key = _path_key(path_text)
            listed.add(key)
            if "bare" in record:
                continue
            admin = common if index == 0 else linked.get(key, (None, ""))[0]
            if _same_ref(record.get("branch", ""), ref) or (
                admin is not None and _worktree_state_holds(admin, ref)
            ):
                add(path_text)
        for key, (admin, path_text) in linked.items():
            if key not in listed and _worktree_state_holds(admin, ref):
                add(path_text)
        return found

    # -- cleanup helpers ------------------------------------------------------

    def main_worktree(self) -> Path | None:
        """The repository's main worktree.

        This is the only reliable way to identify the source checkout: inside a linked
        worktree, ``git rev-parse --show-toplevel`` returns *that worktree*, so comparing it
        to the resolved path would misidentify every worktree as the source.
        """
        blocks = self.worktree_blocks()
        if not blocks:
            return None
        recorded = blocks[0].get("worktree")
        return Path(recorded).resolve() if recorded else None

    def registration(self, worktree: Path) -> tuple[dict[str, str] | None, bool]:
        """``(the registration git holds for this path or None, whether it is the main worktree)``.

        One ``git worktree list``: the main worktree is the first one git lists.
        """
        records = self.worktree_blocks()
        index = _registered_at(records, worktree)
        return (None, False) if index is None else (records[index], index == 0)

    def is_main_worktree(self, candidate: Path) -> bool:
        return self.registration(candidate)[1]

    def worktree_registration(self, worktree: Path) -> dict[str, str] | None:
        """The registration git actually holds for this path, or ``None`` when absent."""
        return self.registration(worktree)[0]

    def remove_worktree_checked(self, worktree: Path) -> None:
        """``git worktree remove`` with no force and no fallback deletion.

        Git deletes the directory first and the registration after it, the registration even when
        the directory could not be deleted (Windows keeps an open file undeletable): a failure can
        leave a directory git no longer knows, with its ``.git`` file gone too.
        """
        self.run("worktree", "remove", "--", str(worktree))
