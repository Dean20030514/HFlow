"""What DSH reads on its own at launch, besides HFlow's packet. Recorded, never enforced.

Documented upstream at dsh-v0.2.0-rc.2 (639ed015397290b3745d163aafe02ffee4aa3f84), not
observed: ``<cwd>/.env`` and ``$DSH_HOME/.env`` (app-boot ``loadLayeredEnv``), the ``$DSH_HOME``
and ``profiles/<profile>`` ``cordis.patch.yml`` layers, ``$DSH_HOME/AGENTS.md``, the
AGENTS.md/CLAUDE.md(.local) chain from the nearest ``.git`` marker down to the cwd, skills under
``<root>/.dsh/skills``, ``<root>/.agents/skills`` and ``<home>/skills``, and ambient ``DSH_*``
variables.

This module checks a FIXED list of paths. It never lists a directory, never opens a ``.env`` or
the home's stored-credentials file, never records a value, executes nothing, refuses nothing and
binds nothing. :func:`_read_regular` is the only place a file is opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from ..contracts import ClientIdentity, LaunchConfig, LaunchSurfaces, SurfaceFile
from ..ids import utc_now
from ..report import surface_summary
from .acpx_dsh import _client_module_tree

#: The paths DSH reads in its home (pinned to dsh-v0.2.0-rc.2). The profile entry is formatted
#: with the launch's profile and skipped when it has none.
DSH_HOME_SURFACE_NAMES = (
    "cordis.patch.yml",
    "profiles/{profile}/cordis.patch.yml",
    ".env",
    "AGENTS.md",
    "skills",
)
#: Instruction files DSH's agent-instructions source reads in each directory from the project
#: root down to the cwd (pinned to dsh-v0.2.0-rc.2). Whether DSH loads all four or only the
#: first per directory is not verified.
INSTRUCTION_FILE_NAMES = ("AGENTS.md", "CLAUDE.md", "AGENTS.local.md", "CLAUDE.local.md")
#: Project skill directories, relative to the project root (pinned to dsh-v0.2.0-rc.2).
PROJECT_SKILL_DIRS = (".dsh/skills", ".agents/skills")
#: What marks the project root for the instruction chain. A worktree's ``.git`` file counts.
PROJECT_ROOT_MARKER = ".git"
#: A ``.env`` may hold credentials, so it is examined by stat only, never opened.
ENV_FILE_NAME = ".env"
#: A regular surface file above this size is recorded without a digest.
SURFACE_HASH_LIMIT_BYTES = 4 * 1024 * 1024
#: A package.json, a shim or a manifest above this size is not read.
MANIFEST_READ_LIMIT_BYTES = 256 * 1024
#: DSH's credential variable. Only whether the name reaches the child is recorded.
DEEPSEEK_API_KEY_NAME = "DEEPSEEK_API_KEY"
#: The acpx version ADR 0001 recorded, and the @agentclientprotocol/sdk version observed with the
#: pinned acpx on 2026-10-02. A different version is noted, never refused.
RECORDED_ACPX_VERSION = "0.17.1"
RECORDED_SDK_VERSION = "1.4.0"
#: Only a Windows batch shim's text is classified; compared casefolded (the test stand-in is
#: ``dsh.CMD``, the Desktop shim ``dsh.cmd``).
BATCH_SHIM_SUFFIXES = (".cmd", ".bat")
#: Markers of the Desktop app's shim (it sets ELECTRON_RUN_AS_NODE and runs an absolute
#: ``DeepSeek Harness.exe``) and of the npm package's shim, compared after replacing
#: backslashes with forward slashes and casefolding. Pinned to dsh-v0.2.0-rc.2.
DESKTOP_SHIM_MARKERS = ("electron_run_as_node", "deepseek harness.exe")
NPM_SHIM_MARKER = "node_modules/@deepseek-ai/dsh/"
#: What a recorded version may look like; anything else is not recorded.
_VERSION_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


def _read_regular(path: Path, limit: int) -> tuple[bytes | None, int | None, str]:
    """``(bytes, size, detail)`` of a regular file of at most ``limit`` bytes.

    The only function in this module that opens a file. Non-blocking, so a FIFO cannot hold the
    spawn path; a file that is not regular, too large or unreadable gives ``None`` and why.
    """
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError as exc:
        return None, None, f"unreadable: {type(exc).__name__}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, None, "not a regular file"
        if info.st_size > limit:
            return None, info.st_size, f"larger than {limit} bytes: not read"
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            return None, total, f"larger than {limit} bytes: not read"
        return b"".join(chunks), total, ""
    except OSError as exc:
        return None, None, f"unreadable: {type(exc).__name__}"
    finally:
        os.close(fd)


def surface_file(base: Path, name: str, *, digest: bool = True) -> SurfaceFile:
    """What one fixed path is: absent, a directory, a file (size and digest), or unknown.

    Returns no bytes. ``digest=False`` (a ``.env``) records presence and size without opening it.
    """
    path = Path(base) / name
    link = os.path.islink(path)
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return SurfaceFile(name=name, path=str(path), present=False, kind="absent", link=link)
    except OSError as exc:
        return SurfaceFile(
            name=name,
            path=str(path),
            present=None,
            kind="unknown",
            link=link,
            detail=f"could not be examined: {type(exc).__name__}",
        )
    if stat.S_ISDIR(info.st_mode):
        return SurfaceFile(name=name, path=str(path), present=True, kind="directory", link=link)
    if not stat.S_ISREG(info.st_mode):
        return SurfaceFile(name=name, path=str(path), present=True, kind="other", link=link)
    if not digest:
        return SurfaceFile(
            name=name,
            path=str(path),
            present=True,
            kind="file",
            link=link,
            size=info.st_size,
            detail="a .env may hold credentials: not opened, size only",
        )
    data, _size, detail = _read_regular(path, SURFACE_HASH_LIMIT_BYTES)
    return SurfaceFile(
        name=name,
        path=str(path),
        present=True,
        kind="file",
        link=link,
        size=info.st_size,
        sha256="sha256:" + hashlib.sha256(data).hexdigest() if data is not None else "",
        detail=detail,
    )


def env_has_name(env: Mapping[str, str], name: str) -> bool:
    """Whether ``name`` is a variable of ``env`` (case-insensitive on Windows). Never reads it."""
    if os.name == "nt":
        return any(key.upper() == name.upper() for key in env)
    return name in env


def dsh_env_names(env: Mapping[str, str]) -> list[str]:
    """Names of the ``DSH_*`` variables in ``env``; ``DSH_HOME`` is reported as the home."""
    return sorted(
        key for key in env if key.upper().startswith("DSH_") and key.upper() != "DSH_HOME"
    )


def dsh_project_root(workspace: Path) -> Path | None:
    """The nearest directory at or above ``workspace`` holding a ``.git`` marker, or ``None``."""
    start = Path(os.path.abspath(workspace))
    for directory in (start, *start.parents):
        if os.path.exists(directory / PROJECT_ROOT_MARKER):
            return directory
    return None


def _inside(path: Path, root: Path) -> bool:
    """``path`` is ``root`` or below it (the rule ``acpx_dsh._inside_any`` applies)."""
    candidate = os.path.normcase(os.path.abspath(path))
    base = os.path.normcase(os.path.abspath(root))
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def _version(raw: object, label: str, notes: list[str]) -> str:
    """A plain version token, or ``""`` with a note. Nothing else is recorded."""
    if isinstance(raw, str) and _VERSION_TOKEN.fullmatch(raw):
        return raw
    notes.append(f"{label} version field is not a plain version token: not recorded")
    return ""


def _json_file(path: Path, notes: list[str]) -> dict | None:
    """A small JSON object file through :func:`_read_regular`, or ``None`` with a note."""
    data, _size, detail = _read_regular(path, MANIFEST_READ_LIMIT_BYTES)
    if data is None:
        notes.append(f"{path} could not be read ({detail})")
        return None
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        notes.append(f"{path} is not valid JSON")
        return None
    if not isinstance(parsed, dict):
        notes.append(f"{path} is not a JSON object")
        return None
    return parsed


def observe_client_identity(launch: LaunchConfig) -> ClientIdentity:
    """The acpx, SDK and dsh carrier versions, read from files. Nothing is executed."""
    notes: list[str] = []
    identity: dict[str, str] = {}
    entry = Path(launch.client_entry) if launch.client_entry else None
    tree = _client_module_tree(entry) if entry is not None else None
    if entry is None or tree is None:
        notes.append("the client entry is not inside a node_modules tree; no package.json was read")
    else:
        parts = Path(os.path.abspath(entry)).relative_to(tree).parts
        package_root = tree / parts[0]
        if parts[0].startswith("@") and len(parts) > 1:
            package_root = package_root / parts[1]
        manifest = package_root / "package.json"
        package = _json_file(manifest, notes)
        if package is not None:
            identity["acpx_package_json"] = str(manifest)
            identity["acpx_version"] = _version(package.get("version"), "acpx", notes)
            name = package.get("name")
            if name != "acpx":
                notes.append(f"the client package is named {str(name)[:64]!r}, not 'acpx'")
            if identity["acpx_version"] and identity["acpx_version"] != RECORDED_ACPX_VERSION:
                notes.append(
                    f"acpx {identity['acpx_version']} differs from {RECORDED_ACPX_VERSION}, the "
                    "version ADR 0001 recorded; nothing here re-verified it"
                )
        # Node resolves a bare import from the importing file's directory upwards, trying each
        # ``node_modules`` it passes (without NODE_PATH); a directory that is itself a
        # node_modules is not searched inside.
        start = Path(os.path.abspath(entry)).parent
        sdk_manifest = None
        for directory in (start, *start.parents):
            if directory.name.casefold() == "node_modules":
                continue
            candidate = directory / "node_modules" / "@agentclientprotocol" / "sdk" / "package.json"
            if os.path.isfile(candidate):
                sdk_manifest = candidate
                break
        if sdk_manifest is None:
            notes.append(
                "no @agentclientprotocol/sdk package.json on Node's node_modules walk from the "
                "client entry"
            )
        else:
            sdk = _json_file(sdk_manifest, notes)
            if sdk is not None:
                identity["sdk_package_json"] = str(sdk_manifest)
                identity["sdk_version"] = _version(
                    sdk.get("version"), "@agentclientprotocol/sdk", notes
                )
                if identity["sdk_version"] and identity["sdk_version"] != RECORDED_SDK_VERSION:
                    notes.append(
                        f"@agentclientprotocol/sdk {identity['sdk_version']} differs from "
                        f"{RECORDED_SDK_VERSION}, observed with the pinned acpx on 2026-10-02"
                    )

    carrier: Literal["desktop", "npm", "unknown", "not_applicable"] = "unknown"
    dsh = launch.dsh_executable
    if not dsh or dsh not in launch.agent_argv:
        carrier = "not_applicable"
        notes.append("the agent argv does not start the resolved dsh")
    elif Path(dsh).suffix.casefold() not in BATCH_SHIM_SUFFIXES:
        notes.append("only a Windows batch shim's text is classified")
    else:
        data, _size, detail = _read_regular(Path(dsh), MANIFEST_READ_LIMIT_BYTES)
        if data is None:
            notes.append(f"the dsh shim could not be read ({detail})")
        else:
            text = data.decode("utf-8", "replace").replace("\\", "/").casefold()
            shim = Path(dsh)
            if all(marker in text for marker in DESKTOP_SHIM_MARKERS):
                carrier = "desktop"
                if len(shim.parents) > 2:
                    manifest = shim.parents[2] / "primary-runtime" / "runtime.json"
                    runtime = _json_file(manifest, notes)
                    if runtime is not None:
                        # Only desktopVersion is read; payloadDigest is never recorded.
                        identity["dsh_version"] = _version(
                            runtime.get("desktopVersion"), "DeepSeek Desktop", notes
                        )
                        if identity["dsh_version"]:
                            identity["dsh_version_source"] = (
                                f"{manifest} desktopVersion (the Desktop README says the app and "
                                "@deepseek-ai/dsh share one version: documented, not verified)"
                            )
                else:
                    notes.append("the Desktop shim has no runtime manifest location")
            elif NPM_SHIM_MARKER in text:
                carrier = "npm"
                manifest = shim.parent / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
                package = _json_file(manifest, notes)
                if package is not None:
                    identity["dsh_version"] = _version(
                        package.get("version"), "@deepseek-ai/dsh", notes
                    )
                    if identity["dsh_version"]:
                        identity["dsh_version_source"] = f"{manifest} version"
            else:
                notes.append("the shim matches neither the Desktop nor the npm layout")
    return ClientIdentity(dsh_carrier=carrier, notes=notes, **identity)


def observe_launch_surfaces(
    launch: LaunchConfig,
    *,
    dsh_home: Path,
    dsh_home_kind: Literal["bound", "per_invocation"],
    child_env: Mapping[str, str],
    workspace: Path | None = None,
    look_in_home: bool = True,
) -> LaunchSurfaces:
    """What DSH would read at this launch besides the packet, from a fixed list of paths.

    ``workspace`` is the directory DSH starts in (``None`` before dispatch, when a worktree does
    not exist yet). Records only: it refuses nothing and changes nothing.
    """
    notes: list[str] = []
    home = Path(dsh_home)
    ws = Path(os.path.abspath(workspace)) if workspace is not None else None
    if not home.is_absolute():
        if ws is not None:
            given = home
            home = ws / home
            notes.append(
                f"DSH_HOME is the relative path {given}: looked up against the workspace, where "
                "DSH resolves it if it resolves against its working directory (not verified)"
            )
        else:
            look_in_home = False
            notes.append(
                f"DSH_HOME is the relative path {home}: where DSH resolves it depends on the "
                "workspace it starts in, so it was not looked into"
            )
    if ws is not None and home.is_absolute() and _inside(home, ws):
        notes.append(
            f"the DSH home {home} lies inside the workspace {ws}: the agent can write the patch "
            "files, AGENTS.md and skills DSH loads at the next launch (recorded, not refused)"
        )
    home_files: list[SurfaceFile] = []
    if look_in_home:
        for template in DSH_HOME_SURFACE_NAMES:
            if "{profile}" in template and not launch.profile:
                continue
            name = template.format(profile=launch.profile)
            home_files.append(
                surface_file(home, name, digest=Path(name).name != ENV_FILE_NAME)
            )
    workspace_env: SurfaceFile | None = None
    project_root = ""
    instruction_files: list[SurfaceFile] = []
    skill_dirs: list[SurfaceFile] = []
    if ws is not None:
        workspace_env = surface_file(ws, ENV_FILE_NAME, digest=False)
        root = dsh_project_root(ws)
        if root is not None:
            project_root = str(root)
            chain = [root]
            relative = ws.relative_to(root).parts if ws != root else ()
            for part in relative:
                chain.append(chain[-1] / part)
        else:
            chain = [ws]
            notes.append(
                "no .git marker at or above the workspace: only the workspace itself was checked"
            )
        base = root if root is not None else ws
        for directory in chain:
            prefix = directory.relative_to(base).as_posix()
            for name in INSTRUCTION_FILE_NAMES:
                entry = surface_file(directory, name)
                if entry.present is False:
                    continue
                relative_name = name if prefix == "." else f"{prefix}/{name}"
                instruction_files.append(entry.model_copy(update={"name": relative_name}))
        skill_dirs = [surface_file(base, rel) for rel in PROJECT_SKILL_DIRS]
    return LaunchSurfaces(
        observed_at=utc_now(),
        dsh_home_kind=dsh_home_kind,
        dsh_home=str(home),
        dsh_home_observed=look_in_home,
        dsh_home_files=home_files,
        workspace=str(ws) if ws is not None else "",
        workspace_env=workspace_env,
        deepseek_api_key_inherited=env_has_name(child_env, DEEPSEEK_API_KEY_NAME),
        project_root=project_root,
        instruction_files=instruction_files,
        skill_dirs=skill_dirs,
        dsh_env_names=dsh_env_names(child_env),
        client=observe_client_identity(launch),
        notes=notes,
    )


def probe_notes(surfaces: LaunchSurfaces) -> list[str]:
    """The probe's notes for one launch-surface record (see ``AcpxDshDriver.probe``)."""
    if surfaces.dsh_home_kind == "bound":
        files = "; ".join(surface_summary(entry) for entry in surfaces.dsh_home_files)
        home_note = f"DSH home: bound {surfaces.dsh_home} (set on every child)" + (
            f"; files DSH reads there: {files}"
            if surfaces.dsh_home_observed
            else " (not looked into)"
        )
    else:
        home_note = (
            f"DSH home: per-invocation {surfaces.dsh_home} - DSH_HOME is not bound, so the driver "
            "removes it and points USERPROFILE/HOME at the invocation's own home; DSH's default "
            "home is then this directory, created empty for each invocation (no stored "
            "credentials, no cordis.patch.yml, no AGENTS.md, no skills, a fresh anonymous id), so "
            "credentials come only from the launch environment or a workspace .env (inferred "
            "from upstream source, not observed)"
        )
    client = surfaces.client
    carrier = client.dsh_carrier
    if client.dsh_version:
        carrier += f" {client.dsh_version} ({client.dsh_version_source})"
    client_note = (
        "client identity (read from files, nothing executed): "
        f"acpx {client.acpx_version or 'unknown'}, "
        f"@agentclientprotocol/sdk {client.sdk_version or 'unknown'}, dsh carrier {carrier}"
        + (f"; {'; '.join(client.notes)}" if client.notes else "")
    )
    return [
        home_note,
        client_note,
        "DSH_* variables that reach the child (names only): "
        + (", ".join(surfaces.dsh_env_names) or "none"),
        "DEEPSEEK_API_KEY in the child environment: "
        + ("present" if surfaces.deepseek_api_key_inherited else "absent")
        + " (name only; the value is never recorded)",
        "a workspace's .env (presence and size only, never opened), AGENTS.md/CLAUDE.md chain "
        "and skills are observed at each invocation's spawn and shown by status/report; launch "
        "surfaces are recorded, not enforced, and not part of the approval digest",
        *surfaces.notes,
    ]
