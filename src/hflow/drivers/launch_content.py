"""Bind a resolved launch to the content of its entry files (user ruling 2026-10-03, H6).

What is bound, and nothing more: SHA-256 of the client interpreter (``node.exe`` for the real
acpx; the Python interpreter for the test stand-in), the acpx entry file and the package.json of
the package it lies in, the dsh launcher file the agent argv starts, and - for a Desktop or npm
carrier - the carrier entry file its shim runs and that file's package.json. When that entry's
path passes through a regular file named ``*.asar`` (the installed Desktop app's
``resources/app.asar``), the entry and its package.json exist only inside that Electron archive;
the archive file itself is bound in their place (``carrier_archive``) and is not parsed.

What is NOT bound: ``node_modules`` trees, the modules Node resolves at runtime, DSH's own code
loading, the Desktop carrier's ``DeepSeek Harness.exe`` / the npm shim's ``node``, the files
Electron reads from an archive's ``.unpacked`` directory, and ``cmd.exe``. Those remain bound by path. The label everywhere is therefore "launch entry files
bound by content", never "binary verified".

Each file's final path is resolved once (links and junctions followed once); that path is the
one hashed and the one the launch spawns or names. The driver re-derives the list and re-hashes
just before spawn and refuses on any difference. Windows has no exec-by-handle, so a file swapped
between that check and ``CreateProcess`` is not caught: a known, documented limit.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ..contracts import LaunchConfig, LaunchFileDigest

#: The label prepare, doctor, the probe and the README use. Exact wording matters: it states the
#: limit in the same sentence as the claim.
CONTENT_BINDING_LABEL = (
    "launch entry files bound by content; transitive modules and anything Node loads later "
    "remain bound by path"
)
#: A launch entry file above this size is refused rather than hashed (node.exe is ~90 MB).
LAUNCH_HASH_LIMIT_BYTES = 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class LaunchContentError(Exception):
    """A launch entry file could not be hashed (missing, not regular, unreadable, too large)."""


def final_path(path: str) -> str:
    """``path`` with links and junctions followed once.

    The spelling given is kept when it already names the final file, so an ordinary path is
    recorded exactly as resolved before. A path that cannot be resolved is returned unchanged;
    hashing it then refuses.
    """
    try:
        real = os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        return path
    if os.path.normcase(real) == os.path.normcase(os.path.abspath(path)):
        return path
    return real


def hash_file(path: str) -> tuple[str, int]:
    """``("sha256:<hex>", size)`` of one regular file, streamed. Raises :class:`LaunchContentError`."""
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError as exc:
        raise LaunchContentError(f"{path} could not be opened ({type(exc).__name__})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LaunchContentError(f"{path} is not a regular file")
        if info.st_size > LAUNCH_HASH_LIMIT_BYTES:
            raise LaunchContentError(
                f"{path} is larger than {LAUNCH_HASH_LIMIT_BYTES} bytes: not hashed"
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > LAUNCH_HASH_LIMIT_BYTES:
                raise LaunchContentError(
                    f"{path} grew past {LAUNCH_HASH_LIMIT_BYTES} bytes while it was hashed"
                )
            digest.update(chunk)
        return "sha256:" + digest.hexdigest(), total
    except OSError as exc:
        raise LaunchContentError(f"{path} could not be read ({type(exc).__name__})") from exc
    finally:
        os.close(fd)


def launch_entry_files(
    launch: LaunchConfig,
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """``(files, problems, notes)``: which files this launch binds, as ``(kind, final path)``.

    A problem means the binding cannot cover what it says it covers (a Node client entry with no
    package.json, a classified carrier whose entry cannot be named); the launch is then not
    resolvable. A note says what is deliberately left bound by path.
    """
    from .dsh_surfaces import NODE_SCRIPT_SUFFIXES, carrier_files, package_manifest_for

    files: list[tuple[str, str]] = []
    problems: list[str] = []
    notes: list[str] = []
    if launch.client_argv_prefix and launch.client_argv_prefix[0]:
        files.append(("client_interpreter", final_path(launch.client_argv_prefix[0])))
    if launch.client_entry:
        entry = final_path(launch.client_entry)
        files.append(("client_entry", entry))
        manifest = package_manifest_for(Path(entry))
        if manifest is not None:
            files.append(("client_package_json", final_path(str(manifest))))
        elif Path(entry).suffix.casefold() in NODE_SCRIPT_SUFFIXES:
            problems.append(
                f"the acpx client entry {entry} is a Node script outside any node_modules "
                "package, so it has no package.json to bind by content; point HFLOW_ACPX_CLI at "
                "the dist/cli.js of an installed acpx package"
            )
        else:
            notes.append(
                f"the client entry {entry} lies in no node_modules package: no package.json is "
                "bound for it"
            )
    carrier = carrier_files(launch)
    if carrier.launcher is not None:
        files.append(("dsh_launcher", final_path(str(carrier.launcher))))
    if carrier.problem:
        problems.append(carrier.problem)
    else:
        if carrier.archive is not None:
            files.append(("carrier_archive", final_path(str(carrier.archive))))
        if carrier.entry is not None:
            files.append(("carrier_entry", final_path(str(carrier.entry))))
        if carrier.package_json is not None:
            files.append(("carrier_package_json", final_path(str(carrier.package_json))))
    notes.extend(carrier.notes)
    if launch.agent_argv and launch.agent_argv[0] != launch.dsh_executable and (
        launch.dsh_executable in launch.agent_argv
    ):
        notes.append(f"the command processor {launch.agent_argv[0]} is bound by path only")
    return files, problems, notes


def bind_launch_content(
    launch: LaunchConfig,
) -> tuple[list[LaunchFileDigest], list[str], list[str]]:
    """``(digests, problems, notes)`` for this launch. Nothing is executed."""
    files, problems, notes = launch_entry_files(launch)
    digests: list[LaunchFileDigest] = []
    for kind, path in files:
        try:
            sha256, size = hash_file(path)
        except LaunchContentError as exc:
            problems.append(f"the launch entry file ({kind}) cannot be bound by content: {exc}")
            continue
        digests.append(LaunchFileDigest(kind=kind, path=path, sha256=sha256, size=size))  # type: ignore[arg-type]
    return digests, problems, notes


def launch_content_changes(launch: LaunchConfig) -> list[str]:
    """What differs now from the content this launch recorded; empty when nothing does.

    The list of files is derived again from the launch (so a link swapped into a path, or a shim
    edited to run another script, shows up as a different path) and every file is hashed again.
    A launch that recorded no content (built by hand, not by ``resolve_launch_config``) has
    nothing to compare and returns an empty list; the caller states that it is unbound.
    """
    if not launch.content_digests:
        return []
    current, problems, _notes = bind_launch_content(launch)
    changes = list(problems)
    # Each kind appears at most once in a launch.
    now = {item.kind: item for item in current}
    recorded = {item.kind: item for item in launch.content_digests}
    for kind, item in recorded.items():
        fresh = now.get(kind)
        if fresh is None:
            if not problems:  # a file that failed to hash is already named in ``problems``
                changes.append(f"{kind} {item.path} is no longer part of the launch")
        elif os.path.normcase(fresh.path) != os.path.normcase(item.path):
            changes.append(f"{kind} now resolves to {fresh.path}, not {item.path}")
        elif fresh.sha256 != item.sha256 or fresh.size != item.size:
            changes.append(
                f"{kind} {item.path}: {item.sha256} ({item.size} bytes) when the launch was "
                f"resolved, {fresh.sha256} ({fresh.size} bytes) now"
            )
    for kind, item in now.items():
        if kind not in recorded:
            changes.append(f"{kind} {item.path} is now part of the launch but was not bound")
    return changes


def content_lines(launch: LaunchConfig) -> list[str]:
    """One line per bound file, for prepare, doctor and the probe."""
    return [
        f"{item.kind} {item.sha256} {item.size}B {item.path}" for item in launch.content_digests
    ]
