"""Workspace helpers: scope containment, path safety, and frozen candidate hashes.

There is deliberately no Git plumbing here yet. M1 freezes a candidate by hashing
the declared write scope under the project root; real Git base commits and diff
verification are M2 work and are *not* claimed by this module.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from .contracts import RefusalCode, RefusedError, Scope, digest_of

# Directories that are never part of a candidate snapshot.
_SNAPSHOT_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".hflow"}

#: Paths no worker may change, whatever a task or a project declares. ``.git`` and ``.hflow``
#: hold the repository and HFlow's own state. ``.acpxrc.json`` is the client config acpx loads
#: from its working directory over HFlow's own, and the reviewer runs in the worktree the
#: implementer wrote, so it is denied at any depth. A bare entry also covers everything beneath
#: it (see :func:`matches_pattern`).
BUILTIN_WRITE_DENY: tuple[str, ...] = (".git", ".hflow", ".acpxrc.json", "**/.acpxrc.json")

#: Characters that make a ``write_allow`` entry a pattern rather than a path.
_GLOB_CHARS = ("*", "?", "[")


def _normalize(pattern: str) -> str:
    return pattern.replace("\\", "/").strip().lstrip("/")


def matches_pattern(path: str, patterns: list[str]) -> bool:
    """Glob matching with `**` semantics, applied to forward-slash relative paths."""
    candidate = _normalize(path)
    for raw in patterns:
        pattern = _normalize(raw)
        if not pattern:
            continue
        if fnmatch.fnmatch(candidate, pattern):
            return True
        if pattern.endswith("/**") and candidate.startswith(pattern[:-3].rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(candidate, pattern.rstrip("/") + "/**"):
            return True
    return False


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root`` and refuse any escape (plan A18).

    Uses physical resolution, so `..`, absolute paths, and Windows junction or
    symlink indirection that leaves the root are all rejected.
    """
    if not relative or not relative.strip():
        raise RefusedError(RefusalCode.SCOPE_VIOLATION, "empty path in write scope")
    raw = Path(relative)
    if raw.is_absolute() or raw.drive or relative.startswith(("\\", "/")):
        raise RefusedError(
            RefusalCode.SCOPE_VIOLATION, f"absolute or drive-qualified path not allowed: {relative}"
        )
    if ".." in raw.parts:
        raise RefusedError(RefusalCode.SCOPE_VIOLATION, f"parent traversal not allowed: {relative}")
    root_resolved = Path(os.path.realpath(root))
    candidate = Path(os.path.realpath(root_resolved / raw))
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise RefusedError(
            RefusalCode.SCOPE_VIOLATION,
            f"path {relative!r} resolves outside the project root ({candidate})",
        ) from exc
    return candidate


def check_scope(scope: Scope, root: Path, write_deny: list[str]) -> list[str]:
    """Return scope problems; empty list means the declared scope is admissible.

    ``write_allow`` entries are literal file or directory paths. The candidate freeze stages each
    entry as a path and the scoped fingerprint reads each one as a file or a directory, so a glob
    there would match the worker's writes while freezing and fingerprinting nothing. ``write_deny``
    keeps its globs: it is only ever matched against changed paths.
    """
    problems: list[str] = []
    if not scope.write_allow:
        problems.append("write_allow must list at least one path")
    for entry in scope.write_allow:
        if any(char in entry for char in _GLOB_CHARS):
            problems.append(
                f"write_allow entry {entry!r} is a glob pattern; write_allow takes literal file or "
                "directory paths (write_deny may use globs)"
            )
            continue
        try:
            resolve_within(root, entry)
        except RefusedError as exc:
            problems.append(str(exc))
            continue
        if matches_pattern(entry, write_deny):
            problems.append(f"write_allow entry {entry!r} is covered by project write_deny")
        if matches_pattern(entry, list(scope.write_deny)):
            problems.append(f"write_allow entry {entry!r} contradicts the task's own write_deny")
        if matches_pattern(entry, list(BUILTIN_WRITE_DENY)):
            problems.append(
                f"write_allow entry {entry!r} is a path HFlow never lets a worker write "
                f"({', '.join(BUILTIN_WRITE_DENY)})"
            )
    return problems


def expand_scope(root: Path, scope: Scope) -> list[Path]:
    """Files a candidate snapshot covers: declared paths, denied paths removed."""
    files: set[Path] = set()
    for entry in scope.write_allow:
        target = resolve_within(root, entry)
        if target.is_dir():
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [name for name in dirnames if name not in _SNAPSHOT_SKIP_DIRS]
                for name in filenames:
                    files.add(Path(dirpath) / name)
        elif target.is_file():
            files.add(target)
    kept: list[Path] = []
    for path in sorted(files):
        rel = path.relative_to(Path(os.path.realpath(root))).as_posix()
        if matches_pattern(rel, scope.write_deny):
            continue
        kept.append(path)
    return kept


def manifest(root: Path) -> dict[str, str]:
    """Path -> content digest for every ``.gitignore``-free file under ``root``.

    Build artefacts and VCS metadata are skipped; everything else is included, so a
    write to an *undeclared* path is visible instead of silently ignored.
    """
    root_resolved = Path(os.path.realpath(root))
    entries: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [name for name in dirnames if name not in _SNAPSHOT_SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root_resolved).as_posix()
            try:
                entries[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                entries[relative] = "unreadable"
    return entries


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Relative paths added, removed or modified between two manifests."""
    added_or_removed = set(before) ^ set(after)
    modified = {path for path in set(before) & set(after) if before[path] != after[path]}
    return sorted(added_or_removed | modified)


def candidate_fingerprint(root: Path, scope: Scope) -> str:
    """Content hash of the frozen candidate: path + bytes for every scoped file.

    A change to any covered file changes this value, which is what makes
    verification evidence invalidate instead of silently staying valid
    (acceptance A09).
    """
    root_resolved = Path(os.path.realpath(root))
    entries: list[dict[str, object]] = []
    for path in expand_scope(root_resolved, scope):
        relative = path.relative_to(root_resolved).as_posix()
        try:
            data = path.read_bytes()
            entries.append(
                {
                    "path": relative,
                    "size": len(data),
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                }
            )
        except OSError:
            # Present but unreadable: record the fact instead of hashing nothing.
            entries.append({"path": relative, "size": None, "digest": "unreadable"})
    return digest_of(entries)


def paths_outside_scope(
    changed: list[str], scope: Scope, project_deny: Iterable[str] = ()
) -> list[str]:
    """Changed paths that the TaskSpec did not authorize.

    A path is unauthorized when no ``write_allow`` entry covers it, or when any deny rule does
    (the task's ``write_deny``, the project's, or :data:`BUILTIN_WRITE_DENY`): a denied file
    inside an allowed directory is a violation, not part of the candidate.

    This is a *detection* mechanism, not a sandbox: the controller notices the write
    after the fact and refuses to accept the candidate. Real write confinement is M2
    work and is not claimed here.
    """
    deny = [*scope.write_deny, *project_deny, *BUILTIN_WRITE_DENY]
    return [
        path
        for path in changed
        if not matches_pattern(path, scope.write_allow) or matches_pattern(path, deny)
    ]
