"""Git-backed candidate isolation: per-run worktree, frozen candidate, real identity.

M2 needs a candidate that has a *real, explicit* identity rather than only a content
fingerprint. This module owns that and nothing else:

* an isolated worktree per run, created from a fixed base commit, so the user's own checkout
  is never written to (and never stashed, reset or overwritten);
* a freeze step that records the candidate as a **commit** with a canonical message and an
  explicit identity, so the receipt can name a commit and a tree object;
* a drift check that fails closed when the worktree no longer matches what was frozen.

Deliberately absent: branch management, merging, rebasing, remotes, publishing, and any
"generic git platform" behaviour. Only what a local candidate needs.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
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
        completed = subprocess.run(  # noqa: S603,S607
            ["git", *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=env,
        )
        if completed.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {completed.stderr.strip()[:300]}")
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

    def user_change_fingerprint(self) -> str:
        """Hash of the user's uncommitted state, so a run can prove it left it alone."""
        return digest_of(
            {
                "head": self.head,
                "status": self.status_porcelain(),
            }
        )

    def resolve_commit(self, ref: str = "HEAD") -> str:
        return self._rev_parse(f"{ref}^{{commit}}")

    # -- worktrees -----------------------------------------------------------

    def worktree_parent(self) -> Path:
        """Where a run's worktree goes: beside the repository, never inside it."""
        return self.root.parent / f"{self.root.name}.hflow-worktrees"

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
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / run_id
        if target.exists() and any(target.iterdir()):
            # This run already has a worktree (a resumed run, or a kept failed candidate).
            return target
        if target.exists():
            target.rmdir()
        staging = parent / f"{run_id}.staging-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
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
        """A read-only git query whose exit code the caller interprets (``run`` raises on any)."""
        try:
            return subprocess.run(  # noqa: S603,S607 - fixed read-only query
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
        :class:`GitError`.

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
        the freeze refuse: ignored is not the same as disposable.

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

    def candidate_ref(self, run_id: str, attempt_id: str) -> str:
        """The HFlow-owned ref that keeps a candidate reachable after its worktree is gone.

        The name is built from controller-validated identifiers, never from model text. It is
        *not* an acceptance mark: failed candidates are kept too.
        """
        for label, value in (("run id", run_id), ("attempt id", attempt_id)):
            if not value or not value.replace("-", "").replace("_", "").isalnum():
                raise GitError(f"refusing to build a ref from an invalid {label}: {value!r}")
        return f"refs/hflow/candidates/{run_id}/{attempt_id}"

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

    def ensure_candidate_ref(self, ref: str, commit: str) -> str:
        """Create the ref only if absent; reuse it if it already points at the candidate.

        ``git update-ref`` with the empty old value is a create-if-absent operation, so an
        existing ref - including a user's - is never overwritten. A conflicting value is
        refused instead.
        """
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
