"""Leaf helpers the driver modules share. Imports no other driver module.

Environment lookup the way the OS reads it, program lookup on PATH without the current directory,
path containment, the nearest ``node_modules`` of a file, the drivers' one bounded file reader,
and two names (``DSH_AGENTS_HOME``, the invocation-id placeholder). ``launch``, ``launch_content``,
``dsh_surfaces`` and ``acpx_dsh`` all build on these, so keeping them here keeps those modules
free of import cycles.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

#: How much the bounded reader (:func:`read_regular`) reads at a time by default.
HASH_CHUNK_BYTES = 1024 * 1024

#: Stands in for an invocation id that does not exist yet (prepare, doctor, probe). A path that
#: contains it is a name, never a directory: nothing resolves or creates it.
INVOCATION_ID_PLACEHOLDER = "<invocation-id>"


def env_keys(env: Mapping[str, str], name: str) -> list[str]:
    """Every key of ``env`` that spells ``name``, in any case.

    Windows reads variable names case-insensitively, so a lookup or a removal that knew only one
    spelling could be bypassed by another. OS-following callers apply it on Windows only and match
    the exact name elsewhere, where ``dsh_home`` is not ``DSH_HOME``: :func:`env_key` and
    ``launch.child_environment``'s removal of the DSH variables. ``launch.system_command_processor``
    and ``launch.child_environment``'s handling of ``NoDefaultCurrentDirectoryInExePath`` and
    ``PATH`` apply it everywhere.
    """
    wanted = name.upper()
    return [key for key in env if key.upper() == wanted]


def env_key(env: Mapping[str, str], name: str, *, exact_first: bool = False) -> str | None:
    """The key ``env`` holds ``name`` under, or ``None`` when it is unset. Names only.

    Matched as the OS does: any letter case on Windows (:func:`env_keys`), the exact name
    elsewhere. With several spellings present on Windows, the first in ``env``'s order is
    returned - or, with ``exact_first``, the exact spelling when it is one of them. The value is
    never touched, so a presence check (``is not None``) does not read a credential.
    """
    if os.name != "nt":
        return name if name in env else None
    if exact_first and name in env:
        return name
    return next(iter(env_keys(env, name)), None)


def find_on_path(name: str, env: Mapping[str, str]) -> str:
    """The absolute file that PATH lookup finds for ``name``, or ``""``.

    Only the absolute entries of ``env``'s PATH are searched - never the current directory and
    never a relative entry. ``shutil.which`` cannot be used: on Windows it searches the current
    directory first, even when given an explicit ``path``, and then returns a *relative* result
    that a child would resolve against its own cwd - for the agent, the workspace. On Windows the
    name is tried with each ``PATHEXT`` extension unless it already carries one.
    """
    path = env_key(env, "PATH")
    for entry in (env[path] if path is not None else "").split(os.pathsep):
        found = find_in_directory(name, entry.strip().strip('"'), env)
        if found:
            return found
    return ""


def find_in_directory(name: str, directory: str | Path, env: Mapping[str, str]) -> str:
    """The file ``name`` names in one absolute ``directory``, by :func:`find_on_path`'s rules.

    ``""`` for a relative or empty directory and when nothing matches: on Windows each ``PATHEXT``
    extension is tried unless ``name`` carries one, elsewhere the file must be executable.
    """
    if not str(directory) or not Path(directory).is_absolute():
        return ""
    if os.name == "nt":
        pathext = env_key(env, "PATHEXT")
        listed = env[pathext] if pathext is not None else ""
        extensions = [ext for ext in (listed or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
        names = (
            [name]
            if name.lower().endswith(tuple(ext.lower() for ext in extensions))
            else [name + ext for ext in extensions]
        )
    else:
        names = [name]
    for candidate_name in names:
        candidate = Path(directory) / candidate_name
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return ""


def _client_module_tree(entry: Path) -> Path | None:
    """The nearest ``node_modules`` directory enclosing ``entry``, or ``None`` for a loose file.

    That is where the client's dependencies are installed and resolved from, so a workspace
    inside it would let the agent rewrite code the client runs.
    """
    for parent in Path(os.path.abspath(entry)).parents:
        if parent.name.casefold() == "node_modules":
            return parent
    return None


#: DSH >= 0.2.1-alpha.2 reads ``$DSH_AGENTS_HOME/AGENTS.md`` as user-level instructions (else
#: ``~/.agents/AGENTS.md``; documented from upstream source, not observed). Removed from every
#: child environment and never set (``launch.child_environment``).
DSH_AGENTS_HOME_ENV = "DSH_AGENTS_HOME"


def _path_forms(path: str | Path) -> set[str]:
    """``path`` as written (made absolute) and with links resolved, both case-folded.

    Raises ``OSError``/``ValueError`` when the path cannot be resolved; a caller refuses then.
    A NUL is refused here: ``realpath`` does not reject one, and no OS path can hold it.
    """
    if "\0" in str(path):
        raise ValueError("the path contains a NUL character")
    return {
        os.path.normcase(os.path.abspath(path)).casefold(),
        os.path.normcase(os.path.realpath(path)).casefold(),
    }


def _same_or_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def path_inside(path: str | Path, root: str | Path) -> bool:
    """``path`` is ``root`` or lies below it, as written or with links resolved.

    The one containment rule of the launch checks. Both forms of both paths are compared
    case-folded (:func:`_path_forms`), so a refusal built on it cannot be bypassed by spelling
    a path in another case. Raises like :func:`_path_forms`.
    """
    return any(
        _same_or_inside(form, root_form)
        for form in _path_forms(path)
        for root_form in _path_forms(root)
    )


class RegularFileError(Exception):
    """Why :func:`read_regular` gave up. ``kind``: ``open``, ``not_regular``, ``too_large``,
    ``grew`` or ``read``; ``cause`` names the ``OSError`` class for ``open``/``read``; ``size`` is
    the size seen, for ``too_large``/``grew``."""

    def __init__(self, kind: str, *, cause: str = "", size: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.cause = cause
        self.size = size


def read_regular(
    path: str | os.PathLike[str],
    limit: int,
    consume: Callable[[bytes], object],
    *,
    chunk_bytes: int = HASH_CHUNK_BYTES,
) -> int:
    """Stream one regular file of at most ``limit`` bytes into ``consume``; return its size.

    The one bounded reader of the drivers (the launch content binding hashes through it,
    ``dsh_surfaces`` reads manifests and shims through it). The open is non-blocking, so a FIFO
    cannot hold the caller; the type and size are taken from the open descriptor, and a file that
    grows past ``limit`` while it is read is refused rather than cut. Raises
    :class:`RegularFileError`; nothing past ``limit`` is ever passed to ``consume``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise RegularFileError("open", cause=type(exc).__name__) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RegularFileError("not_regular")
        if info.st_size > limit:
            raise RegularFileError("too_large", size=info.st_size)
        total = 0
        while True:
            chunk = os.read(fd, min(chunk_bytes, limit + 1 - total))
            if not chunk:
                return total
            total += len(chunk)
            if total > limit:
                raise RegularFileError("grew", size=total)
            consume(chunk)
    except OSError as exc:
        raise RegularFileError("read", cause=type(exc).__name__) from exc
    finally:
        os.close(fd)
