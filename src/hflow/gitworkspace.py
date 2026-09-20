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

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contracts import RefusalCode, RefusedError, digest_of
from .workspace import matches_pattern

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


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    # Deterministic commits: no ambient identity, no user-level hooks, no interactive prompts.
    env.update(
        {
            "GIT_AUTHOR_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": CANDIDATE_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": CANDIDATE_AUTHOR_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
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

    def run(self, *args: str, cwd: Path | None = None) -> str:
        completed = subprocess.run(  # noqa: S603,S607
            ["git", *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=_base_env(),
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

    def commit_exists(self, ref: str) -> bool:
        completed = subprocess.run(  # noqa: S603,S607
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_base_env(),
        )
        return completed.returncode == 0

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
        allow_ignored: list[str] | None = None,
    ) -> CandidateFreeze:
        """Commit the declared paths and return the candidate's explicit identity.

        Only the authorized paths are added, one by one - never ``git add -A``, so a stray
        log, credential file or runtime artifact in the worktree cannot be collected into the
        candidate by accident.

        ``allow_ignored`` lets a caller name ignored byproducts it accepts as check-generated
        noise (for example a bytecode cache). Anything ignored that is not named there makes
        the freeze refuse: ignored is not the same as disposable.
        """
        base_commit = self.worktree_commit(worktree)
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
        added: list[str] = []
        for entry in allow:
            target = worktree / entry
            if target.is_dir():
                self.run("add", "--", entry, cwd=worktree)
                added.append(entry)
            elif target.is_file():
                self.run("add", "--", entry, cwd=worktree)
                added.append(entry)
        staged = [
            path
            for path in self.run("diff", "--cached", "--name-only", cwd=worktree).splitlines()
            if path.strip()
        ]
        if not staged:
            return CandidateFreeze(
                base_commit=base_commit,
                candidate_commit=base_commit,
                tree=self.worktree_tree(worktree),
                paths=(),
                serial=int(self.run("rev-list", "--count", "HEAD", cwd=worktree).strip()),
                tree_digest=digest_of({"base": base_commit, "staged": []}),
            )
        self.run("commit", "-q", "-m", message, cwd=worktree)
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
