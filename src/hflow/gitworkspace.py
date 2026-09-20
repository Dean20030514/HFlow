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

#: Identity used for controller-created candidate commits. Fixed on purpose: the commit
#: records the controller, not the person, and must not depend on ambient git config.
CANDIDATE_AUTHOR_NAME = "HFlow Controller"
CANDIDATE_AUTHOR_EMAIL = "hflow@localhost"


class GitError(RuntimeError):
    """A git command failed. The message keeps git's own words."""


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

    def status_porcelain(self, cwd: Path | None = None, *, include_ignored: bool = False) -> list[str]:
        """Changed paths, with git's ``XY <path>`` prefix removed.

        Porcelain v1 pads the two status characters and then a space, so the path starts at
        index 3. Ignored entries (``!!``) are excluded unless asked for: a check that runs a
        test suite legitimately produces ignored artifacts, and those are not candidate drift.
        """
        out = self.run("status", "--porcelain", *(["--ignored"] if include_ignored else []), cwd=cwd)
        paths: list[str] = []
        for line in out.splitlines():
            if len(line) < 4 or not line.strip():
                continue
            if line.startswith("!!") and not include_ignored:
                continue
            path = line[3:]
            if " -> " in path:  # renames are reported as "old -> new"
                path = path.split(" -> ", 1)[1]
            paths.append(path.strip().strip('"'))
        return paths

    def ignored_artifacts(self, cwd: Path) -> list[str]:
        """Paths git ignores in this worktree (a check's own byproducts, not the candidate)."""
        out = self.run("status", "--porcelain", "--ignored", cwd=cwd)
        return [line[3:].strip() for line in out.splitlines() if line.startswith("!!")]

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
        """Changed paths in the worktree that the TaskSpec did not authorize."""
        return [path for path in self.status_porcelain(cwd=worktree) if not matches_pattern(path, allow)]

    def freeze_candidate(
        self, worktree: Path, allow: list[str], message: str = "hflow: candidate"
    ) -> CandidateFreeze:
        """Commit the declared paths and return the candidate's explicit identity.

        Only the authorized paths are added, one by one - never ``git add -A``, so a stray
        log, credential file or runtime artifact in the worktree cannot be collected into the
        candidate by accident.
        """
        base_commit = self.worktree_commit(worktree)
        changed = self.worktree_changes(worktree, allow)
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
