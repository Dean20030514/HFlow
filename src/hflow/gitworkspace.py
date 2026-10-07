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

import atexit
import hashlib
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .contracts import RefusalCode, RefusedError, digest_of
from .workspace import BUILTIN_WRITE_DENY, matches_pattern

#: Porcelain v1 status codes that this module refuses to interpret. Anything unmerged, or a
#: submodule change, changes what "the paths in this worktree" even means, so the caller gets
#: an explicit refusal instead of a guessed path.
UNSUPPORTED_STATUS_CODES = frozenset({"U", "DD", "AU", "UD", "UA", "DU", "AA", "UU"})


class GitStatusParseError(RuntimeError):
    """A porcelain record this module will not guess about."""


@dataclass(frozen=True)
class StatusReport:
    """Parsed ``git status --porcelain=v1 -z --untracked-files=all --ignored`` output."""

    #: Paths tracked-and-changed, or untracked (rename/copy contributes BOTH paths).
    changed: tuple[str, ...]
    #: Paths git ignores. Ignored is not the same as disposable.
    ignored: tuple[str, ...]
    #: Untracked paths that git also ignores (a check's leftover cache, for example).
    #: Kept apart from ``changed`` so a caller can apply an artifact policy to them.
    untracked_ignored: tuple[str, ...]
    #: Records that could not be interpreted (unmerged conflicts, submodule changes, ...).
    unsupported: tuple[str, ...]

    @property
    def blocking_changes(self) -> tuple[str, ...]:
        """Changed paths that are *not* explainable by the ignored-artifact policy."""
        return self.changed


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
    untracked_ignored: list[str] = []
    unsupported: list[str] = []

    records = [record for record in raw.split("\0") if record]
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            unsupported.append(record[:40])
            continue
        code = record[:2]
        path = record[3:]
        if code == "!!":
            ignored.append(path)
            continue
        if code in UNSUPPORTED_STATUS_CODES or "U" in code:
            unsupported.append(f"{code} {path}"[:80])
            continue
        if code[0] in {"R", "C"}:  # rename/copy: the old path is the next record
            changed.append(path)
            if index < len(records):
                old = records[index]
                index += 1
                changed.append(old)
                if "S" in code or "N" in code:
                    unsupported.append(f"{code} {path} (submodule)")
            else:
                unsupported.append(f"{code} {path} (missing rename source)")
            continue
        if "S" in code:
            unsupported.append(f"{code} {path} (submodule)")
            continue
        changed.append(path)

    return StatusReport(tuple(changed), tuple(ignored), tuple(untracked_ignored), tuple(unsupported))


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


def _output_encoding() -> str:
    """The encoding ``subprocess`` text mode decodes git's output with, for every other call here."""
    return "utf-8" if sys.flags.utf8_mode else locale.getencoding()


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


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
    text = data.decode(_output_encoding(), errors="surrogateescape").rstrip("\r\n")
    return text or None


def _state_names_branch(name: str | None, branch: str) -> bool:
    """Does a rebase ``head-name`` or ``BISECT_START`` value name ``branch`` (no ``refs/heads/``)?

    Mirrors git's ``get_branch``: ``refs/heads/<b>`` names ``<b>``; any other ``refs/...`` value,
    or a bare branch name (``BISECT_START`` stores one), is kept as written; ``detached HEAD``
    names nothing. An object id is shown abbreviated by git, so it is matched as a prefix (four
    hex digits or more) - erring towards "checked out".
    """
    if not name or name == "detached HEAD":
        return False
    if is_full_oid(name):
        return re.fullmatch(r"[0-9a-f]{4,}", branch) is not None and name.startswith(branch)
    return name.removeprefix("refs/heads/") == branch


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
    branches it will move in ``rebase-merge/update-refs``.
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
    return ref in _update_refs_state(admin / "rebase-merge" / "update-refs")


#: Ignored paths a *check* may legitimately leave behind in a run worktree. Naming them is
#: the point: "ignored" alone never authorizes deletion, and anything not on this list makes
#: a freeze or a cleanup refuse. Deliberately narrow - a bytecode cache and a test runner's
#: cache. User-owned ignored files (``.env``, local data, notes) are not on it.
IGNORED_ARTIFACT_ALLOWLIST = (
    "**/__pycache__/**",
    "**/*.pyc",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
)


def is_sourceless_bytecode(path: str) -> bool:
    """Is ``path`` a ``.pyc`` file that does not sit in a ``__pycache__`` directory?

    Python imports such a file as the module itself when no source file is beside it, and
    CPython 3 never writes one there on its own (its cache lives in ``__pycache__``). So it is
    allowlisted by ``**/*.pyc`` and yet cannot be a check byproduct. Letter case is ignored,
    as Windows imports ignore it.
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1].casefold().endswith(".pyc") and not any(
        part.casefold() == "__pycache__" for part in parts[:-1]
    )


@dataclass(frozen=True)
class CandidateFreeze:
    """What the controller froze, stated in git's own vocabulary."""

    base_commit: str
    candidate_commit: str
    tree: str
    paths: tuple[str, ...]
    serial: int
    tree_digest: str


#: How a block reason that refuses on changed shared Git metadata begins (see
#: :meth:`GitRepo.metadata_snapshot`). Every such reason starts with it, so an operator, a test and
#: the docs grep the same words.
GIT_METADATA_CHANGED = "shared Git metadata changed"


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
    return Path(os.path.normcase(os.path.abspath(Path(cwd) / text.strip()))).as_posix()


def _file_digest(path: Path) -> str:
    """``sha256:<hex>`` of a regular file's bytes, ``absent``, or the type of anything else.

    A FIFO, a device (``/dev/zero``) or a directory is recorded by its type and never opened:
    reading one can block forever or never end, before any refusal is written. A changed type
    still counts as a change. Present but unreadable raises, because it cannot be shown unchanged.
    """
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        raise GitError(f"cannot read {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        return f"not-a-regular-file:{stat.S_IFMT(info.st_mode):o}"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        raise GitError(f"cannot read {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            # Swapped for something else between the stat and the open: record what is there now.
            opened = os.fstat(handle.fileno()).st_mode
            if not stat.S_ISREG(opened):
                return f"not-a-regular-file:{stat.S_IFMT(opened):o}"
            return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise GitError(f"cannot read {path}: {exc}") from exc


_EMPTY_HOOKS_DIR: str | None = None


def _empty_hooks_dir() -> str:
    """An empty, HFlow-owned directory that ``core.hooksPath`` names for every git call.

    Created once per process with :func:`tempfile.mkdtemp` (private to the user, never inside a
    repository or a worktree, so no worker can put a hook in it) and removed at exit.
    """
    global _EMPTY_HOOKS_DIR
    if _EMPTY_HOOKS_DIR is None or not os.path.isdir(_EMPTY_HOOKS_DIR):
        _EMPTY_HOOKS_DIR = tempfile.mkdtemp(prefix="hflow-no-hooks-")
        atexit.register(shutil.rmtree, _EMPTY_HOOKS_DIR, ignore_errors=True)
    return _EMPTY_HOOKS_DIR


def _forced_config() -> tuple[tuple[str, str], ...]:
    """Configuration forced on every git call HFlow makes.

    Passed through ``GIT_CONFIG_COUNT``, which git applies after the global, repository and
    worktree config files, so neither the user's global config nor a value a worker wrote into
    the shared ``.git/config`` from inside its worktree overrides it:

    * ``core.hooksPath`` names an empty directory, so no hook runs - not ``post-checkout`` on
      ``worktree add``, not ``pre-commit`` / ``commit-msg`` / ``post-commit`` on the freeze, not
      ``reference-transaction`` on any ref write;
    * ``core.fsmonitor`` is off, so a status read never runs a configured monitor command;
    * ``commit.gpgsign`` is off, so a freeze never waits on gpg;
    * ``core.ignoreStat`` and ``core.sparseCheckout`` are off, so HFlow's own ``worktree add``
      never checks entries out with the assume-unchanged or skip-worktree flag, which would hide
      a worker's edit from the freeze (a flag set any other way refuses the freeze instead).

    :func:`_base_env` also appends these to an inherited ``GIT_CONFIG_PARAMETERS``, which git
    reads after ``GIT_CONFIG_COUNT``, so a caller's ``git -c`` cannot override them either.
    """
    return (
        ("core.hooksPath", _empty_hooks_dir()),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
        ("core.ignoreStat", "false"),
        ("core.sparseCheckout", "false"),
    )


#: Variables that point git at a repository, index or object store other than the one the
#: command's ``cwd`` names. Inherited when HFlow is started from inside a git hook or alias; every
#: HFlow call names its repository by ``cwd``, so they are dropped rather than obeyed.
_REPOSITORY_LOCATING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def _sq_quote(text: str) -> str:
    """Quote ``text`` the way git's ``sq_quote`` does for ``GIT_CONFIG_PARAMETERS``."""
    return "'" + text.replace("'", "'\\''") + "'"


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    # Deterministic, inert git: a fixed identity (no ambient one), no system config, no
    # interactive prompts, no replacement objects (``refs/replace`` cannot make one commit read as
    # another), no inherited repository-locating variables, and the forced configuration above:
    # no hooks (the repository's, the user's, a worker's or a caller's ``git -c``), no fsmonitor
    # command, no signing, no flagged checkout. The user's global config is still read for
    # everything else.
    env.update(
        {
            "GIT_AUTHOR_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    for name in _REPOSITORY_LOCATING_ENV:
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


class GitRepo:
    """Read-mostly handle on a repository. Never mutates the user's checkout."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.git_dir = self._rev_parse("--git-dir")
        self.common_dir = self._rev_parse("--git-common-dir")
        self.head = self._rev_parse("HEAD")

    # -- construction --------------------------------------------------------

    @classmethod
    def discover(cls, candidate: Path) -> GitRepo:
        probe = subprocess.run(  # noqa: S603,S607 - fixed read-only query
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(candidate),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_base_env(),
        )
        if probe.returncode != 0:
            raise RefusedError(
                RefusalCode.SCOPE_VIOLATION,
                f"{candidate} is not inside a git repository: {probe.stderr.strip()[:200]}",
            )
        return cls(Path(probe.stdout.strip()))

    # -- queries -------------------------------------------------------------

    def _rev_parse(self, *args: str) -> str:
        completed = subprocess.run(  # noqa: S603,S607
            ["git", "rev-parse", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_base_env(),
        )
        if completed.returncode != 0:
            raise GitError(f"git rev-parse {' '.join(args)} failed: {completed.stderr.strip()[:200]}")
        return completed.stdout.strip()

    def run(self, *args: str, cwd: Path | None = None, literal_pathspecs: bool = False) -> str:
        env = _base_env()
        if literal_pathspecs:
            # A scope entry is a path, never a pattern: '*', '?', '[' and ':(magic)' mean themselves.
            env["GIT_LITERAL_PATHSPECS"] = "1"
        try:
            completed = subprocess.run(  # noqa: S603,S607
                ["git", *args],
                cwd=str(cwd or self.root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=env,
            )
        except UnicodeDecodeError as exc:
            # POSIX decodes in this thread: git printed bytes that are not text in this encoding
            # (a configuration value is raw bytes, so a worker can write any).
            raise GitError(_undecodable(args, exc.reason)) from exc
        if completed.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {(completed.stderr or '').strip()[:300]}")
        if completed.stdout is None:
            # Windows decodes in a reader thread; the decode error ends that thread and the
            # output comes back as None. Reading it as empty would make "unreadable" look like
            # "nothing there".
            raise GitError(_undecodable(args, "decoding failed"))
        return completed.stdout

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

    def status_porcelain(self, cwd: Path | None = None) -> list[str]:
        """Changed paths (never ignored, never unsupported)."""
        return list(self.status_report(cwd).changed)

    def ignored_artifacts(self, cwd: Path) -> list[str]:
        """Paths git ignores here. Ignored does not mean disposable - see the clean policy."""
        return list(self.status_report(cwd).ignored)

    def ignored_artifact_fingerprint(self, cwd: Path) -> str:
        """Content hash of ignored artifacts, so a change in them is still detectable."""
        entries: list[dict[str, object]] = []
        for relative in sorted(self.ignored_artifacts(cwd)):
            target = Path(cwd) / relative.rstrip("/")
            if target.is_file():
                entries.append({"path": relative, "size": target.stat().st_size})
            else:
                entries.append({"path": relative, "size": None})
        return digest_of(entries)

    def is_dirty(self) -> bool:
        """Does the user's own checkout have changes? Read-only: nothing is touched."""
        return bool(self.status_porcelain())

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
        stash = self._git_query(self.root, "rev-parse", "-q", "--verify", "refs/stash")
        head_name = self._git_query(self.root, "symbolic-ref", "-q", "HEAD")
        index = self.run("ls-files", "-s", "-z")
        raw = self.run(
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignored",
        )
        records = [record for record in raw.split("\0") if record]
        entries: list[list[object]] = []
        position = 0
        while position < len(records):
            record = records[position]
            position += 1
            code, paths = record[:2], [record[3:]]
            if code[:1] in {"R", "C"} and position < len(records):
                paths.append(records[position])
                position += 1
            for path in paths:
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
                "head": self._rev_parse("HEAD"),
                "head_name": head_name.stdout.strip() if head_name.returncode == 0 else "",
                "stash": stash.stdout.strip() if stash.returncode == 0 else "",
                "index": "sha256:"
                + hashlib.sha256(index.encode("utf-8", "surrogateescape")).hexdigest(),
                "status": entries,
            }
        )

    def resolve_commit(self, ref: str = "HEAD") -> str:
        return self._rev_parse(f"{ref}^{{commit}}")

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
        if actual.root != target.resolve() or (actual.root / actual.common_dir).resolve() != (self.root / self.common_dir).resolve():
            raise GitError("worktree reuse refused: repository identity differs")
        if self.worktree_registration(target) is None or self.is_main_worktree(target):
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

        The worktree is added at a staging path and then moved into place, and git is asked to
        repair the moved worktree: ``mv`` alone would leave the administrative files pointing
        at the staging path (``git status`` then reports the worktree as prunable), so the
        repair is part of creating it, not an optional extra.
        """
        parent = self.worktree_parent()
        if parent.is_symlink() or parent.is_junction():
            raise GitError(f"worktree creation refused: linked parent {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / run_id
        if target.exists() or target.is_symlink():
            self.validate_reusable_worktree(target, base_commit)
            return target
        staging = parent / f"{run_id}.staging-{os.getpid()}"
        if staging.exists():
            raise GitError(f"worktree creation refused: staging path already exists: {staging}")
        self.run("worktree", "add", "--detach", str(staging), base_commit)
        staging.replace(target)
        self.run("worktree", "repair", str(target))
        return target

    def worktree_commit(self, worktree: Path) -> str:
        out = self.run("rev-parse", "HEAD", cwd=worktree)
        return out.strip()

    def worktree_tree(self, worktree: Path) -> str:
        return self.run("rev-parse", "HEAD^{tree}", cwd=worktree).strip()

    def commit_exists(self, commit: str) -> bool:
        """Is this commit an object in this repository?

        Used where a caller must not assume a recorded SHA is still present: a base commit can be
        named by a task file that was written against a different clone, and a delivery diff that
        silently produced an empty path list would look like "this delivery changed nothing".
        """
        if not commit:
            return False
        completed = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=_base_env(),
        )
        return completed.returncode == 0

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
        out = self.run(
            "diff", "--no-renames", "--name-only", "-z", f"{base}..{candidate}", cwd=cwd
        )
        return [path for path in out.split("\0") if path.strip()]

    def committed_bytecode_paths(self, commit: str) -> list[str]:
        """The ``.pyc`` paths under a ``__pycache__`` directory that ``commit`` itself tracks.

        Read from the commit's tree object (``ls-tree``), never from a working tree or the index,
        so no filter, hook or fsmonitor configured in a worktree runs. These are committed bytes:
        the bytecode removal before a worktree run's checks keeps them.
        """
        out = self.run("ls-tree", "-r", "-z", "--name-only", commit)
        return [
            path
            for path in out.split("\0")
            if path.casefold().endswith(".pyc")
            and any(part.casefold() == "__pycache__" for part in path.split("/")[:-1])
        ]

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
            proc = subprocess.Popen(  # noqa: S603,S607 - fixed argv, no shell
                ["git", *args],
                cwd=str(cwd or self.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                env=env,
            )
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

    def _git_query(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        """A read-only git query whose exit code the caller interprets (``run`` raises on any).

        Output that cannot be decoded raises :class:`GitError`, as in ``run``, on either platform.
        """
        try:
            completed = subprocess.run(  # noqa: S603,S607 - fixed read-only query
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=_base_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out after {exc.timeout}s") from exc
        except UnicodeDecodeError as exc:
            raise GitError(_undecodable(args, exc.reason)) from exc
        if completed.stdout is None or completed.stderr is None:
            raise GitError(_undecodable(args, "decoding failed"))
        return completed

    def _global_attributes_file(self, cwd: Path) -> str:
        """The global attributes file git reads in ``cwd``, or ``""`` when it reads none."""
        query = self._git_query(cwd, "var", "GIT_ATTR_GLOBAL")
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
        configured = self._git_query(cwd, "config", "--type=path", "--get", "core.attributesFile")
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
        default = self._git_query(
            cwd,
            "config",
            "--type=path",
            "--default",
            "~/.config/git/attributes",
            "--get",
            "core.attributesFile",
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
        bytes a worker can write): ``run`` and ``_git_query`` convert the decode failure on both
        platforms, and the ``UnicodeDecodeError`` branch below keeps the contract if a read
        bypasses them.

        Needs Git 2.31+ (``rev-parse --path-format=absolute``; ``--show-scope`` is 2.26). The
        global attributes file comes from ``git var GIT_ATTR_GLOBAL`` on Git 2.42+ and from git's
        own rule on older Git.
        """
        entries: dict[str, str] = {}
        files: set[str] = set()
        try:
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
        except subprocess.TimeoutExpired as exc:
            # A hung read fails closed like any unreadable metadata.
            raise GitError(
                f"git {' '.join(str(part) for part in exc.cmd[1:4])} timed out after {exc.timeout}s"
            ) from exc
        except UnicodeDecodeError as exc:
            # Not ``surrogateescape``: a lone surrogate would only move the failure into
            # ``digest_of`` and the note writes, which encode as strict UTF-8.
            raise GitError(f"git metadata output is not decodable as text ({exc.reason})") from exc
        for path in files:
            entries[f"file:{path}"] = _file_digest(Path(path))
        return GitMetadataSnapshot(tuple(sorted(entries.items())))

    def worktree_changes(self, worktree: Path, allow: list[str]) -> list[str]:
        """Changed paths in the worktree that the TaskSpec did not authorize.

        Raises :class:`GitStatusParseError` when the status cannot be interpreted safely: an
        unreadable status must fail the run, not silently shrink the change set.
        """
        report = self.status_report(worktree)
        if report.unsupported:
            raise GitStatusParseError(
                "unsupported git status records: " + "; ".join(report.unsupported[:3])
            )
        return [path for path in report.changed if not matches_pattern(path, allow)]

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
                "skip-worktree): "
                + ", ".join(flagged[:5])
                + (f" (+{len(flagged) - 5} more)" if len(flagged) > 5 else "")
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
                "refusing to freeze with unrecognised ignored paths: "
                + ", ".join(unexpected_ignored[:5])
            )
        # An allowlisted ignored file is never staged. Inside the scoped fingerprint it would be
        # hashed and checked without any candidate commit holding it.
        fingerprinted_set = set(fingerprinted)
        covered = [path for path in report.ignored if path in fingerprinted_set]
        if covered:
            raise GitStatusParseError(
                "ignored file(s) inside write_allow ("
                + ", ".join(covered[:5])
                + (f" (+{len(covered) - 5} more)" if len(covered) > 5 else "")
                + "); the scoped fingerprint would cover bytes no candidate commit holds, so the "
                "freeze is refused rather than deleting them"
            )
        # Bytecode outside ``__pycache__`` is imported in place of a missing source file, and
        # CPython 3 never writes it there itself: it is a worker's file, not a check byproduct.
        sourceless = [path for path in report.ignored if is_sourceless_bytecode(path)]
        if sourceless:
            raise GitStatusParseError(
                "ignored sourceless bytecode outside __pycache__ ("
                + ", ".join(sourceless[:5])
                + (f" (+{len(sourceless) - 5} more)" if len(sourceless) > 5 else "")
                + "); Python imports it in place of a missing source file and no candidate commit "
                "holds it, so the freeze is refused rather than deleting it"
            )
        outside = [path for path in report.changed if not matches_pattern(path, allow)]
        if outside:
            raise GitStatusParseError(
                "worker changed unauthorised paths: " + ", ".join(outside[:5])
            )
        deny_rules = [*(deny or []), *BUILTIN_WRITE_DENY]
        denied = [path for path in report.changed if matches_pattern(path, deny_rules)]
        if denied:
            raise GitStatusParseError("worker changed denied paths: " + ", ".join(denied[:5]))
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
                "refusing to commit paths outside the write scope: " + ", ".join(stray[:5])
            )
        if staged:
            # ``--no-verify`` on top of the empty ``core.hooksPath``: no pre-commit or commit-msg
            # hook runs even if the forced configuration were ever lost.
            self.run("commit", "--no-verify", "-q", "-m", message, cwd=worktree)
        leftover = self.status_report(worktree)
        if leftover.changed or leftover.unsupported:
            raise GitError(
                "freeze incomplete: the worktree still has changes the candidate commit does not "
                "hold: " + ", ".join([*leftover.changed, *leftover.unsupported][:5])
            )
        candidate_commit = self.worktree_commit(worktree)
        tree = self.worktree_tree(worktree)
        return CandidateFreeze(
            base_commit=base_commit,
            candidate_commit=candidate_commit,
            tree=tree,
            paths=tuple(sorted(staged)),
            serial=int(self.run("rev-list", "--count", "HEAD", cwd=worktree).strip()),
            tree_digest=digest_of({"base": base_commit, "staged": sorted(staged)}),
        )

    def remove_worktree(self, worktree: Path) -> None:
        self.run("worktree", "remove", "--force", str(worktree))

    def worktree_list(self) -> list[str]:
        return [line for line in self.run("worktree", "list", "--porcelain").splitlines() if line.strip()]

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
        completed = subprocess.run(  # noqa: S603,S607 - read-only query
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_base_env(),
        )
        return completed.stdout.strip() or None

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

    def ensure_candidate_ref(self, ref: str, commit: str) -> str:
        """Create the candidate ref only if absent; reuse it if it already points at the candidate."""
        return self.ensure_ref(ref, commit)

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

    # -- integration primitives ----------------------------------------------
    #
    # Plumbing only: none of these reads or writes an index or a working tree, so the user's
    # checkout is never touched. Revisions are refused when they could read as an option.

    def _git_call(
        self, *args: str, stdin: bytes | None = None, timeout: float = 300.0
    ) -> tuple[int, str, str]:
        """Run ``git <args>`` in the repository root; ``(returncode, stdout, stderr)``.

        For callers that read the exit code themselves (``run`` raises on any non-zero) or feed
        stdin. The pipes are binary: input reaches git byte for byte (a text-mode stdin on Windows
        writes ``\\r\\n`` for ``\\n``), and stdout is decoded strictly with the encoding the
        module's text-mode calls use, without newline translation, so a path holding ``\\r``
        arrives as git printed it. Output that is not text raises :class:`GitError` on every
        platform - the caveat in ``run``: unreadable must never look like empty.
        """
        try:
            completed = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
                ["git", *args],
                cwd=str(self.root),
                input=stdin,
                # Nothing to feed: git reads no terminal and no inherited pipe.
                stdin=subprocess.DEVNULL if stdin is None else None,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=_base_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out after {exc.timeout}s") from exc
        encoding = _output_encoding()
        try:
            stdout = completed.stdout.decode(encoding)
        except UnicodeDecodeError as exc:
            raise GitError(_undecodable(args, exc.reason)) from exc
        stderr = completed.stderr.decode(encoding, errors="replace")
        return completed.returncode, stdout, stderr

    def _git_checked(self, *args: str, stdin: bytes | None = None) -> str:
        """:meth:`_git_call` that raises :class:`GitError`, with git's words, on a non-zero exit."""
        returncode, stdout, stderr = self._git_call(*args, stdin=stdin)
        if returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()[:300]}")
        return stdout

    @staticmethod
    def _object_id(stdout: str, args: tuple[str, ...]) -> str:
        oid = stdout.strip()
        if not is_full_oid(oid):
            raise GitError(f"git {' '.join(args)} printed {oid[:80]!r} where an object id belongs")
        return oid

    def git_version(self) -> tuple[int, int, int]:
        """The running git's version, ``(major, minor, patch)``; see :func:`parse_git_version`."""
        return parse_git_version(self._git_checked("version"))

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
        returncode, _, stderr = self._git_call(*args)
        if returncode in (0, 1):
            return returncode == 0
        raise GitError(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()[:300]}")

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
        returncode, stdout, stderr = self._git_call("check-ref-format", "--branch", branch)
        if returncode != 0 or stdout.removesuffix("\n") != branch:
            raise GitError(f"refusing branch name {branch!r}: {stderr.strip()[:200] or stdout.strip()[:200]}")
        return f"refs/heads/{branch}"

    def tree_of(self, commit: str) -> str:
        """The tree object id of ``commit`` (``rev-parse --verify <commit>^{tree}``)."""
        args = ("rev-parse", "--verify", "--end-of-options", f"{_revision(commit, 'commit')}^{{tree}}")
        return self._object_id(self._git_checked(*args), args)

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
        returncode, stdout, stderr = self._git_call(*args)
        if returncode not in (0, 1):
            raise GitError(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()[:300]}")
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
        return self._object_id(self._git_checked(*args, stdin=body), tuple(args))

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
        returncode, stdout, stderr = self._git_call("symbolic-ref", "-q", ref)
        if returncode == 0:
            raise GitError(f"refusing to update {ref}: it is a symbolic ref to {stdout.strip()}")
        if returncode != 1:
            raise GitError(f"git symbolic-ref -q {ref} failed ({returncode}): {stderr.strip()[:300]}")
        self._git_checked("update-ref", "--no-deref", "-m", reason, ref, new, expected_old)

    def _worktree_records(self) -> list[dict[str, str]]:
        """``git worktree list --porcelain -z`` as one dict per worktree, in git's order.

        ``-z`` (Git 2.36) prints every path raw and NUL-terminated, so a worktree path holding a
        space, a non-ASCII letter or a line break is read as git holds it.
        """
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in self._git_checked("worktree", "list", "--porcelain", "-z").split("\0"):
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

    def checked_out_at(self, ref: str) -> list[Path]:
        """Every worktree that holds ``ref`` checked out, as git itself counts it.

        A worktree holds a branch when its ``HEAD`` names it (the ``branch`` line of ``git
        worktree list``) or when an operation in it will return to or move the branch: a rebase
        (``head-name``), a bisect (``BISECT_START``) or a ``rebase --update-refs`` - git's
        ``prepare_checked_out_branches`` in ``branch.c``, a superset of what ``worktree.c``'s
        ``is_worktree_being_rebased`` / ``is_worktree_being_bisected`` / ``find_shared_symref``
        check. The main worktree's state lives in the common git directory, a linked worktree's in
        ``worktrees/<id>``, found as git finds it: by the ``gitdir`` file there. A bare main
        worktree is skipped, as git skips it.

        Returns resolved paths, deduplicated, in ``git worktree list`` order; a worktree whose
        state names the branch but that the listing does not show comes last. Read-only.
        """
        records = self._worktree_records()
        common = Path(
            self._git_checked("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
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
            if record.get("branch") == ref or (admin is not None and _worktree_state_holds(admin, ref)):
                add(path_text)
        for key, (admin, path_text) in linked.items():
            if key not in listed and _worktree_state_holds(admin, ref):
                add(path_text)
        return found

    # -- cleanup helpers ------------------------------------------------------

    def worktree_blocks(self) -> list[dict[str, str]]:
        """All worktree registrations, in git's order (the main worktree comes first)."""
        blocks: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in self.run("worktree", "list", "--porcelain").splitlines():
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            blocks.append(current)
        return blocks

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

    def is_main_worktree(self, candidate: Path) -> bool:
        main = self.main_worktree()
        return main is not None and Path(candidate).resolve() == main

    def worktree_registration(self, worktree: Path) -> dict[str, str] | None:
        """The registration git actually holds for this path, or ``None`` when absent."""
        target = Path(worktree).resolve()
        for block in self.worktree_blocks():
            recorded = block.get("worktree")
            if recorded and Path(recorded).resolve() == target:
                return block
        return None

    def remove_worktree_checked(self, worktree: Path) -> None:
        """``git worktree remove`` with no force and no fallback deletion."""
        self.run("worktree", "remove", str(worktree))
