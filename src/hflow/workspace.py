"""Workspace helpers: scope containment, path safety, and frozen candidate hashes.

There is deliberately no Git plumbing here yet. M1 freezes a candidate by hashing
the declared write scope under the project root; real Git base commits and diff
verification are M2 work and are *not* claimed by this module.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path, PureWindowsPath

from .contracts import RefusalCode, RefusedError, Scope, digest_of

# Directories that are never part of a candidate snapshot.
_SNAPSHOT_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".hflow"}
#: The most one ``os.read`` of a hashed file asks for: a candidate file is never read at once.
_HASH_CHUNK_BYTES = 64 * 1024

#: Paths no worker may change, whatever a task or a project declares. ``.git`` and ``.hflow``
#: hold the repository and HFlow's own state. ``.acpxrc.json`` is the client config acpx loads
#: from its working directory over HFlow's own, and the reviewer runs in the worktree the
#: implementer wrote, so it is denied at any depth. A bare entry also covers everything beneath
#: it (see :func:`matches_pattern`).
BUILTIN_WRITE_DENY: tuple[str, ...] = (".git", ".hflow", ".acpxrc.json", "**/.acpxrc.json")

# The DSH context list below is read from upstream DSH dsh-v0.2.0-rc.2 (639ed015; master da00f7f
# is identical for these packages). It is not checked against earlier tags and not observed in a
# DSH run: it classifies a candidate's changed paths, it neither watches DSH nor enforces anything.
# These names are defined only here: the launch-surface record (``drivers.dsh_surfaces``) and the
# driver's spawn gate (``drivers.acpx_dsh``) import them, so the three cannot drift apart.

#: DSH's agent-instructions loads every one of these present in each directory from its project
#: root (the nearest ``.git``, which a worktree's ``.git`` file is) down to its cwd, and in a deeper
#: directory after a tool touches a file there.
DSH_INSTRUCTION_FILE_NAMES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS.local.md",
    "CLAUDE.local.md",
)
#: The project-root directories DSH's skill-filesystem scans.
DSH_SKILL_DIRS: tuple[str, ...] = (".dsh/skills", ".agents/skills")
#: The file DSH's loadLayeredEnv reads from its cwd at start-up.
DSH_WORKSPACE_ENV_FILE = ".env"
#: Stored in every record, so a later build that widens the list cannot make an old "none
#: changed" read as if it had applied the new list.
DSH_CONTEXT_LIST_SOURCE: str = (
    "upstream DSH dsh-v0.2.0-rc.2 (639ed015) source, not observed in a DSH run: "
    f"{', '.join(DSH_INSTRUCTION_FILE_NAMES)} at any depth; "
    f"{', '.join(d + '/**' for d in DSH_SKILL_DIRS)} at the root; "
    f"a root {DSH_WORKSPACE_ENV_FILE}; any letter case"
)

#: Characters that make a ``write_allow`` entry a pattern rather than a path.
_GLOB_CHARS = ("*", "?", "[")


def _normalize(pattern: str) -> str:
    """Forward slashes, no leading ``/`` or ``./``, no ``.`` segments, no repeated ``/``.

    Git spells a changed path without any of those, so a rule written ``./config/**`` must reach
    the matcher as ``config/**``, or it would never match anything.
    """
    text = re.sub(r"/{2,}", "/", pattern.replace("\\", "/").strip()).lstrip("/")
    while text.startswith("./"):
        text = text[2:].lstrip("/")
    while "/./" in text:
        text = text.replace("/./", "/")
    if text.endswith("/."):
        text = text[:-2]
    return "" if text == "." else text


def write_deny_problems(entries: Iterable[str], *, owner: str) -> list[str]:
    """``write_deny`` entries that could never match, or are ambiguous about what they match.

    A deny rule that never matches silently does nothing, so it is refused instead of accepted:
    empty after :func:`_normalize`, absolute, drive-qualified, or holding a ``..`` segment. A
    leading ``/`` or ``\\`` with no drive or UNC prefix (gitignore-style ``/config/secrets/**``)
    is refused with its own message: it may mean a host path or a root-anchored rule, and an
    earlier build silently stripped the slash. ``owner`` ("project" or "task") names where the
    entry came from.
    """
    problems: list[str] = []
    for entry in entries:
        raw = str(entry)
        stripped = raw.strip()
        normalized = _normalize(raw)
        if not normalized:
            problems.append(f"{owner} write_deny entry {raw!r} is empty after normalization")
        elif stripped.startswith(("/", "\\")) and not PureWindowsPath(stripped).drive:
            problems.append(
                f"{owner} write_deny entry {raw!r} starts with {stripped[0]!r}; write it relative "
                f"to the project root, e.g. {normalized!r}"
            )
        elif (
            stripped.startswith(("/", "\\"))
            or Path(stripped).is_absolute()
            or PureWindowsPath(stripped).drive
        ):
            problems.append(
                f"{owner} write_deny entry {raw!r} is absolute or drive-qualified; write_deny takes "
                "paths or globs relative to the project root"
            )
        elif ".." in normalized.split("/"):
            problems.append(
                f"{owner} write_deny entry {raw!r} contains a parent traversal ('..'), so it could "
                "never match a path inside the project root"
            )
    return problems


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


def dsh_context_paths(paths: Iterable[str]) -> list[str]:
    """Return the ``paths`` that are on the DSH context list, in order, as Git spelled them.

    The input is workspace-root-relative paths as :meth:`GitRepo.diff_paths` returns them
    (``--no-renames``, so a deleted or moved-away file counts). Matching ignores case, like the
    ``.acpxrc.json`` gate. A file or link at ``.dsh``, ``.agents`` or a skill directory itself
    counts, because DSH would scan through it. This classifies against a documented list
    (:data:`DSH_CONTEXT_LIST_SOURCE`); it neither observes DSH nor enforces anything.
    """
    names = {name.casefold() for name in DSH_INSTRUCTION_FILE_NAMES}
    skill_dirs = [d.casefold() for d in DSH_SKILL_DIRS]
    env_file = DSH_WORKSPACE_ENV_FILE.casefold()
    found: list[str] = []
    for raw in paths:
        folded = _normalize(raw).casefold()
        if not folded:
            continue
        if (
            folded.rsplit("/", 1)[-1] in names
            or folded == env_file
            or any(
                folded == d or d.startswith(folded + "/") or folded.startswith(d + "/")
                for d in skill_dirs
            )
        ):
            found.append(raw)
    return list(dict.fromkeys(found))


def dsh_root_env_paths(paths: Iterable[str]) -> list[str]:
    """The ``paths`` that are the workspace-root ``.env`` (any letter case), as Git spelled them.

    DSH reads that file from its cwd at start-up as environment, so a candidate that adds,
    changes or deletes it is refused outright, whatever the task's scope says (user ruling,
    2026-10-03). A ``.env`` in a subdirectory is not on the DSH list and is not matched here.
    """
    env_file = DSH_WORKSPACE_ENV_FILE.casefold()
    return list(dict.fromkeys(raw for raw in paths if _normalize(raw).casefold() == env_file))


def dsh_context_declared(path: str, write_allow: Iterable[str]) -> bool:
    """Does some ``write_allow`` entry *name* this DSH context file, rather than merely contain it?

    Declared means one of, after :func:`_normalize` and ignoring letter case:

    * the entry is the path itself (``docs/AGENTS.md``);
    * the entry is a glob whose last segment is the file's own name, without a wildcard, and that
      matches the path (``**/AGENTS.md``). Admission refuses globs in ``write_allow`` today, so
      only the literal form reaches a run; the rule is kept so that relaxing admission cannot
      turn a broad pattern into a declaration;
    * for a file under a root skill directory, the entry is that skill directory or lies inside it
      (``.agents/skills``, ``.dsh/skills/review``) and the path is the entry or lies under it.

    A broad entry that only contains the file (``src``, ``.``, ``docs``) does not declare it.
    The root ``.env`` is refused whether or not an entry names it: see
    :func:`dsh_root_env_paths`.
    """
    folded = _normalize(path).casefold()
    if not folded:
        return False
    name = folded.rsplit("/", 1)[-1]
    skill_dirs = [d.casefold() for d in DSH_SKILL_DIRS]
    for raw in write_allow:
        entry = _normalize(str(raw)).casefold()
        if not entry:
            continue
        if entry == folded:
            return True
        last = entry.rsplit("/", 1)[-1]
        if (
            any(char in entry for char in _GLOB_CHARS)
            and not any(char in last for char in _GLOB_CHARS)
            and last == name
            and fnmatch.fnmatchcase(folded, entry)
        ):
            return True
        if any(entry == d or entry.startswith(d + "/") for d in skill_dirs) and (
            folded == entry or folded.startswith(entry + "/")
        ):
            return True
    return False


def undeclared_dsh_context_paths(paths: Iterable[str], write_allow: Iterable[str]) -> list[str]:
    """The DSH context files among ``paths`` that no ``write_allow`` entry names explicitly.

    The root ``.env`` is left out: it is refused unconditionally (see :func:`dsh_root_env_paths`).
    Like :func:`dsh_context_paths`, a check against a documented list, not an enforcement.
    """
    allow = [str(entry) for entry in write_allow]
    changed = list(paths)
    env = set(dsh_root_env_paths(changed))
    return [
        path
        for path in dsh_context_paths(changed)
        if path not in env and not dsh_context_declared(path, allow)
    ]


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root`` and refuse any escape (plan A18).

    Two checks, in this order. As written: an empty, absolute, drive-qualified or root-anchored
    path, or one with a ``..`` segment, is refused before anything is resolved. Then
    ``root / relative`` is resolved with :func:`os.path.realpath` and refused when the result lies
    outside the resolved root - that is what catches a symbolic link or junction (a broken one
    included) that leads out of the root.

    What it does not check: a link that stays inside the root (admission refuses a ``write_allow``
    entry through any link on its own), and a link created after this call. Resolution also maps
    the other Windows spellings of an existing entry - an 8.3 short name, a trailing dot or space
    - to its real name, so the returned path can spell the entry differently from ``relative``; a
    caller that matches rules against the path must match the returned spelling as well (see
    :func:`check_scope`).
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


def _alias_segment_problem(entry: str) -> str:
    """Why ``entry`` has a segment Windows would read as another name (``""`` when none does).

    Windows drops a trailing dot or space from a path segment (``.git.`` opens ``.git``), and a
    ``:`` names an alternate data stream or a drive. Such a spelling would pass a deny rule written
    for the real name, so it is refused outright. ``~`` is not refused: it is legitimate in a file
    name, and an 8.3 short name of an existing entry is caught by matching the resolved spelling.
    A drive or root is left to :func:`resolve_within`, which refuses it with its own message.
    """
    path = PureWindowsPath(entry)
    for segment in path.parts[1:] if path.anchor else path.parts:
        if segment in {".", ".."}:
            continue
        if ":" in segment:
            return f"write_allow entry {entry!r} has a segment containing ':' ({segment!r})"
        if segment.endswith((".", " ")):
            return (
                f"write_allow entry {entry!r} has a segment ending in a dot or a space "
                f"({segment!r}); Windows would open it under another name"
            )
    return ""


def check_scope(scope: Scope, root: Path, write_deny: list[str]) -> list[str]:
    """Return scope problems; empty list means the declared scope is admissible.

    ``write_allow`` entries are literal file or directory paths. The candidate freeze stages each
    entry as a path and the scoped fingerprint reads each one as a file or a directory, so a glob
    there would match the worker's writes while freezing and fingerprinting nothing. ``write_deny``
    keeps its globs: it is only ever matched against changed paths. A ``write_deny`` entry that
    could never match one (see :func:`write_deny_problems`) is refused, the project's and the
    task's alike.

    Every deny list is matched against the entry as written *and* as resolved (relative to the
    resolved root), so a spelling that names a denied path another way - an 8.3 short name such
    as ``SECRET~1`` for an existing directory - is still covered. A segment ending in a dot or a
    space, or holding a ``:``, is refused before that (:func:`_alias_segment_problem`).
    """
    problems: list[str] = [
        *write_deny_problems(write_deny, owner="project"),
        *write_deny_problems(scope.write_deny, owner="task"),
    ]
    if not scope.write_allow:
        problems.append("write_allow must list at least one path")
    root_resolved = Path(os.path.realpath(root))
    for entry in scope.write_allow:
        if any(char in entry for char in _GLOB_CHARS):
            problems.append(
                f"write_allow entry {entry!r} is a glob pattern; write_allow takes literal file or "
                "directory paths (write_deny may use globs)"
            )
            continue
        if alias := _alias_segment_problem(entry):
            problems.append(alias)
            continue
        try:
            resolved = resolve_within(root, entry)
        except RefusedError as exc:
            problems.append(str(exc))
            continue
        spellings = (entry, resolved.relative_to(root_resolved).as_posix())
        for patterns, problem in (
            (write_deny, "is covered by project write_deny"),
            (scope.write_deny, "contradicts the task's own write_deny"),
            (
                BUILTIN_WRITE_DENY,
                f"is a path HFlow never lets a worker write ({', '.join(BUILTIN_WRITE_DENY)})",
            ),
        ):
            if any(matches_pattern(spelling, list(patterns)) for spelling in spellings):
                problems.append(f"write_allow entry {entry!r} {problem}")
    return problems


def _walk_files(top: Path) -> Iterator[tuple[Path, str | None]]:
    """Every file under ``top``, plus every directory link or junction as an entry of its own.

    Yields ``(path, link)``. ``link`` is ``None`` for an entry whose content is to be hashed (a
    file, a link to one, a broken symbolic link, anything that is not a directory) and the recorded
    value for a directory link: ``link:<target>`` as :func:`os.readlink` reads it, or
    ``link:unreadable``. So a link that changes target still changes a fingerprint or a manifest.

    The walk never enters a junction or a symbolic link. ``os.walk(followlinks=False)`` is not
    enough for that: an NTFS junction is not ``is_symlink()``, so ``os.walk`` descends into it, and
    two junctions pointing back at their parent made one walk run forever. Directories named in
    :data:`_SNAPSHOT_SKIP_DIRS` (a link of that name too) are skipped; a *file* of that name - a
    worktree's ``.git`` file - is an ordinary file. A directory that cannot be listed is skipped,
    as ``os.walk`` skips it.
    """
    pending = [str(top)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        for entry in entries:
            path = entry.path
            try:
                is_junction = entry.is_junction()
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir()  # follows a link: is this a directory, or a link to one?
            except OSError:
                is_junction = is_symlink = is_dir = False
            if not (is_dir or is_junction):
                yield Path(path), None
                continue
            if entry.name in _SNAPSHOT_SKIP_DIRS:
                continue
            if is_junction or is_symlink:
                try:
                    yield Path(path), "link:" + os.readlink(path)
                except OSError:
                    yield Path(path), "link:unreadable"
                continue
            pending.append(path)


def _scoped_entries(root: Path, scope: Scope) -> list[tuple[Path, str | None]]:
    """The entries :func:`expand_scope` names, sorted, each with its directory-link record."""
    found: dict[Path, str | None] = {}
    for entry in scope.write_allow:
        target = resolve_within(root, entry)
        if target.is_dir():
            found.update(_walk_files(target))
        elif target.is_file():
            found[target] = None
    root_resolved = Path(os.path.realpath(root))
    return [
        (path, link)
        for path, link in sorted(found.items())
        if not matches_pattern(path.relative_to(root_resolved).as_posix(), scope.write_deny)
    ]


def expand_scope(root: Path, scope: Scope) -> list[Path]:
    """Files a candidate snapshot covers: declared paths, denied paths removed.

    A directory link or junction inside a declared directory is listed as itself and not entered
    (see :func:`_walk_files`).
    """
    return [path for path, _link in _scoped_entries(root, scope)]


def hash_regular_file(path: str | os.PathLike[str], limit: int | None = None) -> tuple[int, str]:
    """``(size, "sha256:<hex>")`` of one regular file, read in bounded chunks.

    The size is what was actually read, not a prior stat. The trees this reads are written by a
    worker, so anything that is not a regular file - a FIFO, a device, a link to either - raises
    :class:`OSError` (recorded as ``unreadable``) instead of being opened: a blocking open or read
    of one could hang the controller or never end. The type is checked before the open and again
    on the opened handle (something swapped in between), and the open is non-blocking, as in
    ``launch_content.hash_file``. A link to a regular file is still followed, so a regular file's
    facts are what they always were. ``limit`` (bytes) refuses a larger file, by its stat or by
    what was read; ``None`` reads the whole file.
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise OSError(f"{path} is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if limit is not None and info.st_size > limit:
            raise OSError(f"{path} is larger than {limit} bytes")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _HASH_CHUNK_BYTES):
            size += len(chunk)
            if limit is not None and size > limit:
                raise OSError(f"{path} grew past {limit} bytes while it was read")
            digest.update(chunk)
    finally:
        os.close(fd)
    return size, "sha256:" + digest.hexdigest()


def _entry_facts(path: Path, link: str | None) -> tuple[int | None, str]:
    """``(size, digest)`` of one snapshot entry: a link's record, or a file's content facts."""
    if link is not None:
        return None, link
    try:
        return hash_regular_file(path)
    except OSError:
        # Present but unreadable: record the fact instead of hashing nothing.
        return None, "unreadable"


def manifest(root: Path) -> dict[str, str]:
    """Path -> content digest for every ``.gitignore``-free file under ``root``.

    Build artefacts and VCS metadata are skipped; everything else is included, so a
    write to an *undeclared* path is visible instead of silently ignored. A directory link or
    junction is one entry, recorded by its target (see :func:`_walk_files`).
    """
    root_resolved = Path(os.path.realpath(root))
    return {
        path.relative_to(root_resolved).as_posix(): _entry_facts(path, link)[1]
        for path, link in _walk_files(root_resolved)
    }


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Relative paths added, removed or modified between two manifests."""
    added_or_removed = set(before) ^ set(after)
    modified = {path for path in set(before) & set(after) if before[path] != after[path]}
    return sorted(added_or_removed | modified)


def candidate_fingerprint(root: Path, scope: Scope) -> str:
    """Content hash of the frozen candidate: path + bytes for every scoped file.

    A change to any covered file changes this value, which is what makes
    verification evidence invalidate instead of silently staying valid
    (acceptance A09). A directory link or junction in the scope is covered by its target.
    """
    root_resolved = Path(os.path.realpath(root))
    entries: list[dict[str, object]] = []
    for path, link in _scoped_entries(root_resolved, scope):
        size, content_digest = _entry_facts(path, link)
        entries.append(
            {
                "path": path.relative_to(root_resolved).as_posix(),
                "size": size,
                "digest": content_digest,
            }
        )
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
