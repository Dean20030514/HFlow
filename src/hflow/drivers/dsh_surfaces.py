"""What DSH reads on its own at launch, besides HFlow's packet. Recorded, never enforced.

Documented upstream at dsh-v0.2.0-rc.2 (639ed015397290b3745d163aafe02ffee4aa3f84), not
observed: ``<cwd>/.env`` and ``$DSH_HOME/.env`` (app-boot ``loadLayeredEnv``), the ``$DSH_HOME``
and ``profiles/<profile>`` ``cordis.patch.yml`` layers, ``$DSH_HOME/AGENTS.md``, the
AGENTS.md/CLAUDE.md(.local) chain from the nearest ``.git`` marker down to the cwd, skills under
``<root>/.dsh/skills``, ``<root>/.agents/skills`` and ``<home>/skills``, and ambient ``DSH_*``
variables.

Documented upstream at dsh-v0.2.1-alpha.2 (d7432673, commit 21d0c2e2), not observed, and not read
by 0.2.0-rc.2: after ``$DSH_HOME/AGENTS.md`` DSH also reads ``$DSH_AGENTS_HOME/AGENTS.md``, the
environment-only ``DSH_AGENTS_HOME`` defaulting to ``~/.agents`` (:func:`user_agents_file`).

This module checks a FIXED list of paths. It never lists a directory, never opens a ``.env`` or
the home's stored-credentials file, never records a value (``DSH_AGENTS_HOME`` is a directory, and
is recorded only as the location of the file it names), executes nothing, refuses nothing and
binds nothing. The home's patch layers are read as bounded text for two notes (turn-time git,
:func:`turn_git_notes`; the sandbox mode, :func:`sandbox_mode_notes`), from which only fixed
tokens are recorded. :func:`_read_regular` is the only place a file is opened. :func:`carrier_files`
and :func:`package_manifest_for` only *name* files; the binding by content is
``launch_content``'s. :func:`credential_sources` reports which credential sources a launch would
have (a variable's name, two home files by stat); the refusal built on it lives in the driver
(``acpx_dsh.credential_source_problem``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from ..contracts import ClientIdentity, LaunchConfig, LaunchSurfaces, SurfaceFile
from ..ids import utc_now
from ..report import surface_summary
from ..workspace import DSH_INSTRUCTION_FILE_NAMES, DSH_SKILL_DIRS, DSH_WORKSPACE_ENV_FILE
from .acpx_dsh import DSH_AGENTS_HOME_ENV, _client_module_tree, env_key, path_inside

#: The paths DSH reads in its home (pinned to dsh-v0.2.0-rc.2). The profile entry is formatted
#: with the launch's profile and skipped when it has none.
DSH_HOME_SURFACE_NAMES = (
    "cordis.patch.yml",
    "profiles/{profile}/cordis.patch.yml",
    ".env",
    "AGENTS.md",
    "skills",
)
#: DSH's user-level agents file: ``$DSH_AGENTS_HOME/AGENTS.md``, the environment-only
#: ``DSH_AGENTS_HOME`` (``acpx_dsh.DSH_AGENTS_HOME_ENV``) defaulting to ``<home>/.agents`` (see
#: :data:`USER_AGENTS_SOURCE`).
USER_AGENTS_DEFAULT_DIR = ".agents"
USER_AGENTS_FILE_NAME = "AGENTS.md"
#: Where the user-level agents file comes from, as far as it is known.
USER_AGENTS_SOURCE = (
    "read by DSH >= 0.2.1-alpha.2 after $DSH_HOME/AGENTS.md, per upstream source (commit "
    "21d0c2e2, tag dsh-v0.2.1-alpha.2): documented, not observed; dsh-v0.2.0-rc.2 does not read it"
)
# The instruction files DSH reads in each directory from the project root down to the cwd
# (``workspace.DSH_INSTRUCTION_FILE_NAMES``) and the project skill directories relative to the
# project root (``workspace.DSH_SKILL_DIRS``) are defined once, in ``hflow.workspace``, which
# classifies a candidate's changed paths against the same list (pinned to dsh-v0.2.0-rc.2).
# Whether DSH loads all four instruction files or only the first per directory is not verified
# here.

#: What marks the project root for the instruction chain. A worktree's ``.git`` file counts.
PROJECT_ROOT_MARKER = ".git"
#: The env file DSH reads in its cwd and in its home (``workspace.DSH_WORKSPACE_ENV_FILE``). A
#: ``.env`` may hold credentials, so it is examined by stat only, never opened.
ENV_FILE_NAME = DSH_WORKSPACE_ENV_FILE
#: A regular surface file above this size is recorded without a digest.
SURFACE_HASH_LIMIT_BYTES = 4 * 1024 * 1024
#: A package.json, a shim or a manifest above this size is not read.
MANIFEST_READ_LIMIT_BYTES = 256 * 1024
#: DSH's credential variable. Only whether the name reaches the child is recorded.
DEEPSEEK_API_KEY_NAME = "DEEPSEEK_API_KEY"
#: DSH's stored-credentials file in its home (documented at dsh-v0.2.0-rc.2, not observed). Like a
#: ``.env`` it is never opened, sized or hashed: only whether it exists is checked, by stat.
STORED_CREDENTIALS_NAME = ".credentials.yaml"
#: The files in a DSH home that DSH takes a credential from, in its documented precedence order
#: (the launch environment comes before both, ``<cwd>/.env`` between them; pinned to
#: dsh-v0.2.0-rc.2, not observed).
HOME_CREDENTIAL_FILE_NAMES = (STORED_CREDENTIALS_NAME, ENV_FILE_NAME)
#: :func:`stat_presence`'s answer for a regular file - the only answer that counts as a source.
PRESENT = "present"
#: The acpx version ADR 0001 recorded. A different version is noted, never refused.
RECORDED_ACPX_VERSION = "0.17.1"
#: The @agentclientprotocol/sdk versions the pinned acpx has passed the model-free client checks
#: with (``tools/m0_probe/real_client_checks.py all``: the real client against the mock agent -
#: not a live compatibility proof). The SDK is deliberately not pinned (user ruling 2026-10-14): a
#: fresh acpx install resolves whatever ``^1.4.0`` allows, the launch content binding covers the
#: SDK's package.json, and a version outside this list is noted - never refused - with the check
#: to run before a live task.
CHECKED_SDK_VERSIONS = {
    "1.4.0": "2026-10-02, the checkout's .probe/acpx install",
    "1.8.0": "2026-10-14, a fresh acpx 0.17.1 install",
}
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
#: A script a batch shim runs, written relative to the shim's own directory - npm's
#: ``"%dp0%\node_modules\...\x.js"`` or ``"%~dp0..\...\x.js"`` - read by the launch content
#: binding (``launch_content``) to name the carrier entry file.
_SHIM_SCRIPT = re.compile(
    r"(?:%~dp0|%dp0%)[\\/]*([^\"'%*\r\n]+?\.(?:c|m)?js)(?=[\"'\s]|$)", re.IGNORECASE
)
#: An Electron archive (a regular file the Desktop shim's script path passes through) and the
#: directory Electron keeps the archive's unpacked files in, next to it.
ARCHIVE_SUFFIX = ".asar"
ARCHIVE_UNPACKED_SUFFIX = ".unpacked"
#: What a node_modules package's entry file is: the package root holds its package.json.
NODE_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")


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


def stat_presence(path: Path) -> str:
    """``present`` / ``absent`` / ``present (not a file)`` / ``unknown (<error>)``, by one stat.

    Links are followed (a broken one is ``absent``). The file is never opened, and the size the
    stat returns is neither kept nor reported.
    """
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except (OSError, ValueError) as exc:
        return f"unknown ({type(exc).__name__})"
    return PRESENT if stat.S_ISREG(info.st_mode) else "present (not a file)"


class CredentialSources(NamedTuple):
    """Which credential sources DSH would have at one launch. Names and presence only.

    No value, size, digest or content is part of it. ``home_files`` is ``(name, presence)`` for
    each :data:`HOME_CREDENTIAL_FILE_NAMES` entry of a *bound* home (:func:`stat_presence`), and
    empty for the per-invocation home, which is created empty and never looked into.
    """

    api_key_in_env: bool
    dsh_home_kind: Literal["bound", "per_invocation"]
    dsh_home: str
    home_files: tuple[tuple[str, str], ...]

    @property
    def visible(self) -> bool:
        """The variable's name is in the launch environment, or a home file is a regular file.

        A presence check, not a validity check: an empty or revoked key, or a home file holding
        no credential, counts as visible. A directory, a broken link or an entry that could not
        be examined does not count: DSH could not read a credential from it either.
        """
        return self.api_key_in_env or any(state == PRESENT for _, state in self.home_files)


def credential_sources(
    child_env: Mapping[str, str],
    *,
    dsh_home_kind: Literal["bound", "per_invocation"],
    dsh_home: Path,
) -> CredentialSources:
    """The credential sources visible to DSH started with ``child_env`` and this home.

    DSH's precedence (documented at dsh-v0.2.0-rc.2, not observed): the launch environment, then
    ``$DSH_HOME/.credentials.yaml``, then ``<cwd>/.env``, then ``$DSH_HOME/.env``. The workspace
    ``.env`` is not looked at here: a real launch never starts on one (``workspace_env_file``).
    ``child_env`` is the environment the child is (or would be) started with
    (``acpx_dsh.child_environment``); ``DEEPSEEK_API_KEY`` is looked up by name only
    (``acpx_dsh.env_key``). A bound home that is not absolute is not looked into - where DSH
    resolves it depends on its working directory - so its files read ``unknown``. Records only:
    the refusal built on it is ``acpx_dsh.credential_source_problem``.
    """
    home = Path(dsh_home)
    files: tuple[tuple[str, str], ...] = ()
    if dsh_home_kind == "bound":
        files = tuple(
            (
                name,
                stat_presence(home / name)
                if home.is_absolute()
                else "unknown (relative DSH_HOME: not looked into)",
            )
            for name in HOME_CREDENTIAL_FILE_NAMES
        )
    return CredentialSources(
        api_key_in_env=env_key(child_env, DEEPSEEK_API_KEY_NAME) is not None,
        dsh_home_kind=dsh_home_kind,
        dsh_home=str(home),
        home_files=files,
    )


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
    except (ValueError, RecursionError):
        # A file under the read limit can still nest deeply enough to exhaust the parser's
        # recursion; that is a note like any other unreadable manifest, never an exception.
        notes.append(f"{path} is not valid JSON (or is nested too deeply to parse)")
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
        sdk_manifest = sdk_manifest_for(Path(entry))
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
                if identity["sdk_version"] and identity["sdk_version"] not in CHECKED_SDK_VERSIONS:
                    notes.append(
                        f"@agentclientprotocol/sdk {identity['sdk_version']} has not passed the "
                        "model-free client checks (checked: "
                        f"{', '.join(CHECKED_SDK_VERSIONS)}); run `python "
                        "tools/m0_probe/real_client_checks.py all --acpx-cli <this entry>` "
                        "before a live task"
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


def sdk_manifest_for(entry: Path) -> Path | None:
    """The ``@agentclientprotocol/sdk`` package.json Node would resolve from ``entry``, or ``None``.

    Node resolves a bare import from the importing file's directory upwards, trying each
    ``node_modules`` it passes (without NODE_PATH); a directory that is itself a node_modules is
    not searched inside.
    """
    start = Path(os.path.abspath(entry)).parent
    for directory in (start, *start.parents):
        if directory.name.casefold() == "node_modules":
            continue
        candidate = directory / "node_modules" / "@agentclientprotocol" / "sdk" / "package.json"
        if os.path.isfile(candidate):
            return candidate
    return None


def package_manifest_for(entry: Path) -> Path | None:
    """The package.json of the ``node_modules`` package that holds ``entry``, or ``None``.

    The same package root :func:`observe_client_identity` reads the acpx version from
    (``node_modules/<name>`` or ``node_modules/@scope/<name>``). ``None`` for a loose file and for
    a file directly inside a ``node_modules`` directory, which belongs to no package.
    """
    tree = _client_module_tree(entry)
    if tree is None:
        return None
    parts = Path(os.path.abspath(entry)).relative_to(tree).parts
    if parts and parts[0].startswith("@"):
        if len(parts) < 3:
            return None
        return tree / parts[0] / parts[1] / "package.json"
    if len(parts) < 2:
        return None
    return tree / parts[0] / "package.json"


class CarrierFiles(NamedTuple):
    """The dsh carrier's launch entry files, as the launch content binding names them.

    ``launcher`` is the dsh file the agent argv starts (``None`` when it starts something
    else). ``entry`` and ``package_json`` are named only for a classified carrier (Desktop or
    npm), from the script its shim runs relative to itself. ``archive`` is named instead of
    them when that script's path passes through a regular file named ``*.asar`` (the installed
    Desktop app runs ``resources/app.asar/dsh/.../cli.js``): the entry and its package.json
    exist only inside that Electron archive, which is not parsed, so the archive file itself is
    what the content binding hashes. ``problem`` is non-empty when a classified carrier's files
    cannot be named - the content binding then refuses rather than binding less than it says.
    """

    carrier: Literal["desktop", "npm", "unknown", "not_applicable"]
    launcher: Path | None
    entry: Path | None
    package_json: Path | None
    problem: str
    notes: list[str]
    archive: Path | None = None


def _enclosing_archive(entry: Path) -> Path | None:
    """The regular file named ``*.asar`` that ``entry``'s path passes through, else ``None``.

    Only the nearest existing ancestor of ``entry`` is considered: a path cannot continue
    below a regular file, so that ancestor is the archive or there is none. Links are followed
    by ``stat``; the content binding resolves the archive's final path itself. Nothing is
    opened and the archive is not parsed.
    """
    for parent in entry.parents:
        try:
            info = os.stat(parent)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode) and parent.name.casefold().endswith(ARCHIVE_SUFFIX):
            return parent
        return None
    return None


def carrier_files(launch: LaunchConfig) -> CarrierFiles:
    """Name the carrier entry file and its package.json from the dsh shim's text. Runs nothing.

    Classified the way :func:`observe_client_identity` classifies the carrier (same markers):
    the Desktop shim runs ``DeepSeek Harness.exe`` on a script, the npm shim runs ``node`` on
    ``node_modules/@deepseek-ai/dsh/...``. The script is the one path ending in ``.js``/``.mjs``/
    ``.cjs`` that the shim writes relative to its own directory (``%~dp0`` / ``%dp0%``).
    """
    dsh = launch.dsh_executable
    if not dsh or dsh not in launch.agent_argv:
        return CarrierFiles(
            "not_applicable",
            None,
            None,
            None,
            "",
            ["the agent argv does not start the resolved dsh: no launcher or carrier file is "
             "bound by content"],
        )
    shim = Path(dsh)
    if shim.suffix.casefold() not in BATCH_SHIM_SUFFIXES:
        return CarrierFiles(
            "unknown",
            shim,
            None,
            None,
            "",
            ["the dsh launcher is not a batch shim: the launcher file itself is bound by "
             "content; whatever it loads is bound by path only"],
        )
    data, _size, detail = _read_regular(shim, MANIFEST_READ_LIMIT_BYTES)
    if data is None:
        return CarrierFiles(
            "unknown", shim, None, None, f"the dsh shim {shim} could not be read ({detail})", []
        )
    raw = data.decode("utf-8", "replace")
    text = raw.replace("\\", "/").casefold()
    carrier: Literal["desktop", "npm"]
    if all(marker in text for marker in DESKTOP_SHIM_MARKERS):
        carrier = "desktop"
        interpreter_note = (
            "the Desktop shim's DeepSeek Harness.exe (the program that runs the carrier entry) is "
            "bound by path only"
        )
    elif NPM_SHIM_MARKER in text:
        carrier = "npm"
        interpreter_note = (
            "the npm shim starts node from its own directory or from PATH; that node is bound by "
            "path only"
        )
    else:
        return CarrierFiles(
            "unknown",
            shim,
            None,
            None,
            "",
            ["the dsh shim matches neither the Desktop nor the npm layout: only the shim itself "
             "is bound by content; what it starts is bound by path only"],
        )
    scripts: dict[str, Path] = {}
    for match in _SHIM_SCRIPT.finditer(raw):
        relative = match.group(1).strip().replace("\\", "/")
        candidate = Path(os.path.normpath(os.path.join(str(shim.parent), relative)))
        scripts.setdefault(os.path.normcase(str(candidate)), candidate)
    if len(scripts) != 1:
        return CarrierFiles(
            carrier,
            shim,
            None,
            None,
            f"the {carrier} dsh shim {shim} names {len(scripts)} script(s) relative to itself, "
            "so its carrier entry file cannot be named and the launch cannot be bound by content",
            [],
        )
    entry = next(iter(scripts.values()))
    archive = _enclosing_archive(entry)
    if archive is not None:
        inside = entry.relative_to(archive)
        unpacked = Path(str(archive) + ARCHIVE_UNPACKED_SUFFIX) / inside
        if os.path.lexists(unpacked):
            # Electron runs the unpacked copy only when the archive header marks the file as
            # unpacked; the header is not parsed, so which of the two runs cannot be named.
            return CarrierFiles(
                carrier,
                shim,
                None,
                None,
                f"the carrier entry {entry} that the {carrier} dsh shim runs lies inside the "
                f"archive {archive} and also exists unpacked at {unpacked}; which of the two "
                "Electron runs is decided by the archive header, which is not parsed, so the "
                "launch cannot be bound by content",
                [],
            )
        return CarrierFiles(
            carrier,
            shim,
            None,
            None,
            "",
            [
                f"the carrier entry inside {archive} is bound through the archive's digest",
                f"files Electron reads from {archive}{ARCHIVE_UNPACKED_SUFFIX} are bound by "
                "path only",
                interpreter_note,
            ],
            archive,
        )
    if not entry.is_file():
        return CarrierFiles(
            carrier,
            shim,
            entry,
            None,
            f"the carrier entry {entry} that the {carrier} dsh shim runs is not a file",
            [],
        )
    manifest = package_manifest_for(entry)
    if manifest is None:
        return CarrierFiles(
            carrier,
            shim,
            entry,
            None,
            f"the carrier entry {entry} lies in no node_modules package, so it has no "
            "package.json to bind",
            [],
        )
    return CarrierFiles(carrier, shim, entry, manifest, "", [interpreter_note])


def child_user_home(child_env: Mapping[str, str], child_home: Path | None = None) -> Path:
    """The directory DSH's ``os.homedir()`` returns in a child started with ``child_env``.

    ``child_home`` when given: the driver points every child's USERPROFILE/HOME at its
    invocation's own home (``acpx_dsh.child_home_for``). Otherwise Node's rule - USERPROFILE on
    Windows, HOME elsewhere - read from ``child_env`` (``acpx_dsh.env_key``, an exact spelling
    first), and failing that this process's home.
    """
    if child_home is not None:
        return Path(child_home)
    key = env_key(child_env, "USERPROFILE" if os.name == "nt" else "HOME", exact_first=True)
    value = child_env[key] if key is not None else ""
    return Path(value) if value else Path.home()


def user_agents_file(
    child_env: Mapping[str, str], *, child_home: Path | None = None
) -> SurfaceFile:
    """DSH's user-level agents file at this launch (:data:`USER_AGENTS_SOURCE`). Records only.

    ``$DSH_AGENTS_HOME/AGENTS.md`` when that variable reaches the child (``acpx_dsh.env_key``, an
    exact spelling first), else ``<home>/.agents/AGENTS.md`` with ``<home>`` from
    :func:`child_user_home`. Examined like every other surface (:func:`surface_file`: presence,
    size, and a digest within :data:`SURFACE_HASH_LIMIT_BYTES`); never enforced and never a
    reason to refuse. An empty or relative ``DSH_AGENTS_HOME`` is not looked into - how DSH
    resolves one is not verified - and neither is a default path in a per-invocation home that
    does not exist yet (its path holds ``acpx_dsh.INVOCATION_ID_PLACEHOLDER``).
    """
    from .acpx_dsh import INVOCATION_ID_PLACEHOLDER

    configured_key = env_key(child_env, DSH_AGENTS_HOME_ENV, exact_first=True)
    if configured_key is not None:
        configured = child_env[configured_key]
        name = f"${DSH_AGENTS_HOME_ENV}/{USER_AGENTS_FILE_NAME}"
        base = Path(configured)
        if not configured or not base.is_absolute():
            return SurfaceFile(
                name=name,
                path=str(base / USER_AGENTS_FILE_NAME) if configured else "",
                present=None,
                kind="unknown",
                detail=(
                    f"{DSH_AGENTS_HOME_ENV} is "
                    + ("empty" if not configured else "a relative path")
                    + ": how DSH resolves it is not verified, so it was not looked into"
                ),
            )
    else:
        name = f"~/{USER_AGENTS_DEFAULT_DIR}/{USER_AGENTS_FILE_NAME}"
        try:
            base = child_user_home(child_env, child_home) / USER_AGENTS_DEFAULT_DIR
        except (RuntimeError, OSError) as exc:  # Path.home() with no home to name
            return SurfaceFile(
                name=name,
                path="",
                present=None,
                kind="unknown",
                detail=f"the child's home could not be determined: {type(exc).__name__}",
            )
        if INVOCATION_ID_PLACEHOLDER in str(base):
            return SurfaceFile(
                name=name,
                path=str(base / USER_AGENTS_FILE_NAME),
                present=None,
                kind="unknown",
                detail=(
                    "in the per-invocation home, which is created empty for each invocation and "
                    "does not exist before dispatch: named, not looked into"
                ),
            )
    return surface_file(base, USER_AGENTS_FILE_NAME).model_copy(update={"name": name})


#: What loads DSH's ``workspace-changes`` plugin: its own id, or the Web/Desktop bundle that lists
#: it (``packages/bundle/web-app/cordis.patch.yml``, dsh-v0.2.0-rc.2 and dsh-v0.2.1-alpha.2).
TURN_GIT_MARKERS = ("workspace-changes", "dsh-web-app")


def turn_git_notes(home: Path, profile: str) -> list[str]:
    """A note when a bound home's profile layers name what runs git at every turn. Records only.

    DSH's ``workspace-changes`` plugin runs ``git add --all`` and ``git write-tree`` in the
    workspace repository at each top-level turn start and end, with a private index but the
    repository's own config, so its hooks, fsmonitor and clean/process filters can run (DSH #9382,
    unconfirmed by maintainers). Only the Web/Desktop bundle loads it; the ACP profile template is
    ``[dsh-base, dsh-acp-app]`` and a per-invocation home always gets that template (upstream
    source, documented - not observed). A bound home's ``profiles/<profile>/package.json`` (its
    bundle list) or a patch layer can add it, so those files are searched - bounded, as text,
    for :data:`TURN_GIT_MARKERS` - and a hit is noted with what it would mean. A mention may also
    be an entry that disables it; the note says "names", not "loads".
    """
    candidates = ["cordis.patch.yml"]
    if profile:
        candidates += [f"profiles/{profile}/cordis.patch.yml", f"profiles/{profile}/package.json"]
    notes: list[str] = []
    for name in candidates:
        data, _size, _detail = _read_regular(home / name, MANIFEST_READ_LIMIT_BYTES)
        if data is None:
            continue
        text = data.decode("utf-8", errors="replace").casefold()
        found = [marker for marker in TURN_GIT_MARKERS if marker in text]
        if found:
            notes.append(
                f"the bound DSH home's {name} names {', '.join(found)}: DSH's workspace-changes "
                "plugin runs `git add --all` and `git write-tree` in the workspace repository at "
                "every turn start and end, which can run that repository's hooks, fsmonitor and "
                "clean/process filters (DSH #9382; documented from dsh-v0.2.0-rc.2 and "
                "-alpha.2 source, not observed). The default acp profile does not load it; check "
                "whether this entry enables it (recorded, never enforced)"
            )
    return notes


#: DSH's file-sandbox modes (``SANDBOX_MODES``,
#: packages/sandbox/sandbox-policy/src/session-mode.ts:42, blob 0bc7110e, the same at
#: dsh-v0.2.0-rc.2 and dsh-v0.2.1-alpha.2).
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
#: The mode DSH's base bundle composes when no patch layer changes it and DSH_PERMISSION_MODE is
#: unset: its ``sandbox-policy`` row reads ``mode: !!js process.env.DSH_PERMISSION_MODE ??
#: 'workspace-write'`` (packages/bundle/base/cordis.patch.yml:232 at dsh-v0.2.0-rc.2, :241 at
#: dsh-v0.2.1-alpha.2), and a new session is pinned to the preset matching it (permission-presets
#: ``pinInitialPermission``). Documented from upstream source, not observed.
DSH_DEFAULT_SANDBOX_MODE = "workspace-write"
#: The variable that fallback reads. The driver removes it from every child
#: (``acpx_dsh.STRIPPED_DSH_ENV``), and DSH refuses a ``DSH_*`` name from any ``.env``
#: (bootstrap-only names, packages/boot/app-boot/src/index.ts:156-177 at dsh-v0.2.0-rc.2).
DSH_PERMISSION_MODE_NAME = "DSH_PERMISSION_MODE"
#: The base-bundle rows that decide a new session's mode. The ``permission`` row's
#: ``defaultPreset`` pins a new session to that preset's mode; without one the preset matching the
#: ``sandbox-policy`` row's ``mode`` is used. A patch row replaces the targeted row's whole
#: ``config`` (apps/cli/reference/README.md:9 at dsh-v0.2.0-rc.2), so a ``permission`` row
#: patched without ``presets`` keeps only the schema-default table, whose two presets
#: (workspace-write, danger-full-access) bundle the mode of the same name
#: (packages/interaction/permission-presets/src/index.ts:182-199, blob 8135d742).
SANDBOX_POLICY_ROW = "sandbox-policy"
PERMISSION_ROW = "permission"
SCHEMA_DEFAULT_PRESET_MODES = ("workspace-write", "danger-full-access")
#: How an ACP client could change the mode: it cannot. At dsh-v0.2.0-rc.2 and -alpha.2 DSH's ACP
#: server handles initialize, authenticate, session/new|list|resume|close|prompt|cancel and
#: session/set_config_option, whose only options are ``model`` and ``reasoning_effort``
#: (packages/acp/acp/src/index.ts:379-390 and model-control.ts:8-9, 103-130 at rc.2).
ACP_SANDBOX_MODE_FACT = (
    "an ACP client cannot change it per session: DSH's ACP server offers no session mode, and "
    "session/set_config_option takes only model and reasoning_effort (dsh-v0.2.0-rc.2 and "
    "-alpha.2 source)"
)
#: Shell tool calls under the Desktop carrier with the workspace-write sandbox (DSH discussion
#: #9626, 2026-10-13, three independent reproductions; duplicates #9467, #9507, #9548, #9621,
#: #9493; no maintainer confirmation).
DSH_9626_NOTE = (
    "DSH #9626 (community reports, three independent reproductions on Desktop 0.2.0-rc.2; not "
    "confirmed by maintainers, not observed here): the Desktop build starts its Windows ACL "
    "sandbox runner as [process.execPath, runner], and under the Desktop shim process.execPath "
    "is DeepSeek Harness.exe (ELECTRON_RUN_AS_NODE=1), so with the workspace-write sandbox every "
    "sandboxed shell tool call is reported to exit 0xC0000142 (STATUS_DLL_INIT_FAILED) with no "
    "output; read-only and danger-full-access are reported unaffected (upstream "
    "packages/sandbox/sandbox-local/src/index.ts:571-580, blob 45028746, the same at "
    "dsh-v0.2.0-rc.2 and dsh-v0.2.1-alpha.2). Workarounds: start DSH from the npm-installed CLI "
    "(@deepseek-ai/dsh), whose runner is node.exe; or bind a DSH home whose sandbox mode the "
    "user chooses in its cordis.patch.yml. Documented, unconfirmed; recorded, never enforced"
)
#: A ``- id: <row>`` line of a patch file; the row's block is every following line indented
#: deeper than its dash (blank and comment lines included).
_PATCH_ROW = re.compile(
    r"^(?P<indent>[ \t]*)-[ \t]+id[ \t]*:[ \t]*(?P<quote>['\"]?)(?P<id>[A-Za-z0-9_.@/-]+)"
    r"(?P=quote)[ \t]*(?:#.*)?$"
)
#: A preset name worth naming in a note; anything else is "not a plain name".
_PRESET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: A YAML comment: ``#`` at a line's start or after whitespace, to the end of the line.
_YAML_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$")


def _uncommented(text: str) -> str:
    """``text`` with YAML comments cut and tabs expanded, so a commented-out key never counts.

    Conservative: a ``#`` inside a quoted value is cut too, which can only turn a value into one
    that is not recognised.
    """
    return "\n".join(_YAML_COMMENT.sub("", line.expandtabs()) for line in text.splitlines())


def _patch_rows(text: str) -> dict[str, str]:
    """The block of each ``- id: <row>`` entry of a patch file, by row id (YAML-free).

    ``text`` is :func:`_uncommented`. A block runs from its ``- id:`` line to the next non-blank
    line indented no deeper than its dash. Only that spelling is recognised (an ``id`` that is
    not a row's first key is not); a row named twice in one file keeps its last block.
    """
    rows: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _PATCH_ROW.match(lines[index])
        if match is None:
            index += 1
            continue
        depth = len(match.group("indent"))
        block = [lines[index]]
        index += 1
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= depth:
                break
            block.append(line)
            index += 1
        rows[match.group("id")] = "\n".join(block)
    return rows


def _row_values(block: str, key: str) -> list[str]:
    """Every value written for ``key`` inside one row block: block or flow style, quotes off.

    An expression (``!!js ...``) is returned as its tag, so it never matches a plain value.
    """
    pattern = re.compile(
        rf"(?:^|[\s{{,]){re.escape(key)}[ \t]*:[ \t]*"
        r"(?P<value>!![A-Za-z]+|'[^'\r\n]*'|\"[^\"\r\n]*\"|[^\s,}\]#]+)",
        re.MULTILINE,
    )
    return [match.group("value").strip("'\"") for match in pattern.finditer(block)]


def _has_key(block: str, key: str) -> bool:
    return re.search(rf"(?:^|[\s{{,]){re.escape(key)}[ \t]*:", block, re.MULTILINE) is not None


class SandboxModeFinding(NamedTuple):
    """The sandbox mode a new ACP session would get, as far as text can say. Records only.

    ``mode`` is one of :data:`SANDBOX_MODES`, or ``""`` when it could not be determined;
    ``source`` says where it comes from (or why it is unknown); ``default`` is True when no
    layer sets it and DSH's own default applies.
    """

    mode: str
    source: str
    default: bool = False


def configured_sandbox_mode(
    home: Path,
    profile: str,
    *,
    dsh_home_kind: Literal["bound", "per_invocation"],
    look_in_home: bool,
    child_env: Mapping[str, str],
) -> SandboxModeFinding:
    """Which sandbox mode DSH would start a new ACP session in, read from the home's patch layers.

    DSH composes its tree from bundle layers, then ``profiles/<profile>/cordis.patch.yml``, then
    ``$DSH_HOME/cordis.patch.yml`` (which outranks the profile layer); later layers win per row
    (apps/cli/src/profile-boot.ts:185-195 and apps/cli/reference/README.md:9 at dsh-v0.2.0-rc.2).
    Those two files are read - bounded, as text - for a ``permission`` row's ``defaultPreset``
    and a ``sandbox-policy`` row's ``mode``; nothing is evaluated, so an expression, a redefined
    preset table, an unrecognised spelling or a file that cannot be read gives ``""`` with why.
    Neither file naming either row means DSH's default (:data:`DSH_DEFAULT_SANDBOX_MODE`). A
    profile whose bundle list replaces ``dsh-base`` is not considered. Never a refusal.
    """
    which = "bound DSH home" if dsh_home_kind == "bound" else "per-invocation DSH home"
    if not look_in_home and dsh_home_kind == "bound":
        return SandboxModeFinding("", f"the {which} was not looked into")
    # Per row, the highest-precedence layer naming it: ("row", file, block), or ("unread", file,
    # why) for a layer that exists but could not be read - it may name either row.
    winners: dict[str, tuple[str, str, str]] = {}
    unrecognised = ""
    layers = ([f"profiles/{profile}/cordis.patch.yml"] if profile else []) + ["cordis.patch.yml"]
    for name in layers if look_in_home else ():  # lowest precedence first
        path = home / name
        if not os.path.exists(path):
            continue
        data, _size, detail = _read_regular(path, MANIFEST_READ_LIMIT_BYTES)
        if data is None:
            for row in (PERMISSION_ROW, SANDBOX_POLICY_ROW):
                winners[row] = ("unread", name, detail)
            continue
        text = _uncommented(data.decode("utf-8", errors="replace"))
        rows = _patch_rows(text)
        for row in (PERMISSION_ROW, SANDBOX_POLICY_ROW):
            if row in rows:
                winners[row] = ("row", name, rows[row])
        folded = text.casefold()
        if (SANDBOX_POLICY_ROW not in rows and SANDBOX_POLICY_ROW in folded) or (
            PERMISSION_ROW not in rows and "defaultpreset" in folded
        ):
            unrecognised = (
                f"the {which}'s {name} mentions the sandbox-policy row or a defaultPreset in a "
                "form this text scan does not recognise: not evaluated"
            )

    def unread(entry: tuple[str, str, str]) -> SandboxModeFinding:
        _kind, name, detail = entry
        return SandboxModeFinding(
            "", f"the {which}'s {name} could not be read ({detail}), and it may set the mode"
        )

    permission = winners.get(PERMISSION_ROW)
    if permission is not None:
        kind, name, block = permission
        if kind == "unread":
            return unread(permission)
        presets = _row_values(block, "defaultPreset")
        if presets:
            preset = presets[-1]
            shown = preset if _PRESET_NAME.fullmatch(preset) else "a value that is not a plain name"
            where = f"the {which}'s {name} (permission row, defaultPreset {shown})"
            if (
                len(set(presets)) == 1
                and preset in SCHEMA_DEFAULT_PRESET_MODES
                and not _has_key(block, "presets")
            ):
                return SandboxModeFinding(preset, f"set by {where}, read as text")
            return SandboxModeFinding(
                "", f"{where} decides it; which mode that preset bundles is not evaluated here"
            )
    policy = winners.get(SANDBOX_POLICY_ROW)
    if policy is not None:
        kind, name, block = policy
        if kind == "unread":
            return unread(policy)
        modes = _row_values(block, "mode")
        where = f"the {which}'s {name} (sandbox-policy row, mode)"
        if len(set(modes)) == 1 and modes[0] in SANDBOX_MODES:
            return SandboxModeFinding(modes[0], f"set by {where}, read as text")
        return SandboxModeFinding(
            "",
            f"{where} names the row without one plain mode (an expression, or a config that "
            "omits it): not evaluated",
        )
    if unrecognised:
        return SandboxModeFinding("", unrecognised)
    if env_key(child_env, DSH_PERMISSION_MODE_NAME) is not None:
        return SandboxModeFinding(
            "",
            f"no patch layer sets it and {DSH_PERMISSION_MODE_NAME} reaches the child, so DSH's "
            "fallback is that variable (its value is not recorded)",
        )
    if dsh_home_kind == "per_invocation":
        why = "the per-invocation DSH home is created empty, so no patch layer changes it"
    else:
        why = "no patch layer of the bound DSH home names the permission or sandbox-policy row"
    # The variable is not named here: the probe's own note on DSH's mode variables says which
    # ones it found and removed.
    return SandboxModeFinding(
        DSH_DEFAULT_SANDBOX_MODE,
        f"not set (DSH default, documented as {DSH_DEFAULT_SANDBOX_MODE}): {why}, and the driver "
        "keeps DSH's permission-mode variable out of the child",
        default=True,
    )


def sandbox_mode_notes(
    home: Path,
    profile: str,
    *,
    dsh_home_kind: Literal["bound", "per_invocation"],
    look_in_home: bool,
    child_env: Mapping[str, str],
    carrier: str,
) -> list[str]:
    """The sandbox-mode note, and DSH #9626's when the carrier is the Desktop shim. Records only.

    The mode is :func:`configured_sandbox_mode`'s. The #9626 note (:data:`DSH_9626_NOTE`) is
    added for the Desktop carrier when the mode is workspace-write - set, or DSH's default - or
    could not be determined; never for read-only or danger-full-access, and never for the npm
    carrier. Nothing is refused and no capability changes.
    """
    finding = configured_sandbox_mode(
        home,
        profile,
        dsh_home_kind=dsh_home_kind,
        look_in_home=look_in_home,
        child_env=child_env,
    )
    if finding.default:
        head = f"DSH sandbox mode: {finding.source}"
    elif finding.mode:
        head = f"DSH sandbox mode: {finding.mode}, {finding.source}"
    else:
        head = f"DSH sandbox mode: unknown - {finding.source}"
    notes = [
        f"{head}; {ACP_SANDBOX_MODE_FACT}. Documented from upstream source, not observed; "
        "recorded, never enforced"
    ]
    if carrier == "desktop" and finding.mode in ("", "workspace-write"):
        prefix = (
            "the DSH sandbox mode could not be determined; if it is workspace-write: "
            if not finding.mode
            else "the dsh carrier is the Desktop shim and the DSH sandbox mode is workspace-write: "
        )
        notes.append(prefix + DSH_9626_NOTE)
    return notes


def observe_launch_surfaces(
    launch: LaunchConfig,
    *,
    dsh_home: Path,
    dsh_home_kind: Literal["bound", "per_invocation"],
    child_env: Mapping[str, str],
    workspace: Path | None = None,
    look_in_home: bool = True,
    child_home: Path | None = None,
) -> LaunchSurfaces:
    """What DSH would read at this launch besides the packet, from a fixed list of paths.

    ``workspace`` is the directory DSH starts in (``None`` before dispatch, when a worktree does
    not exist yet). ``child_home`` is the home the child's USERPROFILE/HOME point at, when the
    caller knows it (see :func:`child_user_home`). Records only: it refuses nothing and changes
    nothing.
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
    if ws is not None and home.is_absolute() and path_inside(home, ws):
        notes.append(
            f"the DSH home {home} lies inside the workspace {ws}: the agent can write the patch "
            "files, AGENTS.md and skills DSH loads at the next launch (recorded here; a real "
            "launch is refused at the driver's spawn gate, dsh_home_in_workspace)"
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
        notes.extend(turn_git_notes(home, launch.profile))
    client = observe_client_identity(launch)
    notes.extend(
        sandbox_mode_notes(
            home,
            launch.profile,
            dsh_home_kind=dsh_home_kind,
            look_in_home=look_in_home,
            child_env=child_env,
            carrier=client.dsh_carrier,
        )
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
            for name in DSH_INSTRUCTION_FILE_NAMES:
                entry = surface_file(directory, name)
                if entry.present is False:
                    continue
                relative_name = name if prefix == "." else f"{prefix}/{name}"
                instruction_files.append(entry.model_copy(update={"name": relative_name}))
        skill_dirs = [surface_file(base, rel) for rel in DSH_SKILL_DIRS]
    return LaunchSurfaces(
        observed_at=utc_now(),
        dsh_home_kind=dsh_home_kind,
        dsh_home=str(home),
        dsh_home_observed=look_in_home,
        dsh_home_files=home_files,
        user_agents_file=user_agents_file(child_env, child_home=child_home),
        workspace=str(ws) if ws is not None else "",
        workspace_env=workspace_env,
        deepseek_api_key_inherited=env_key(child_env, DEEPSEEK_API_KEY_NAME) is not None,
        project_root=project_root,
        instruction_files=instruction_files,
        skill_dirs=skill_dirs,
        dsh_env_names=dsh_env_names(child_env),
        client=client,
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
            "from upstream source, not observed); a real launch refuses a workspace .env, and "
            "refuses one without DEEPSEEK_API_KEY in its environment (no_credential_source)"
        )
    agents = surfaces.user_agents_file
    agents_note = f"user-level agents file ({USER_AGENTS_SOURCE}): " + (
        surface_summary(agents) + (f" at {agents.path}" if agents.path else "")
        if agents is not None
        else "not recorded"
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
        agents_note,
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
