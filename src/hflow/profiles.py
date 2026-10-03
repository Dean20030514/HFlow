"""Machine-local profile loading (plan 9.1, 16.2).

A profile is the *machine* half of a run: which harness, which driver and which model
selection each role uses. It lives outside any project checkout, beside the runtime data
(``<data_dir>/profiles/<profile_id>.json``), because it describes this machine and not the
repository under test. ``contracts.MachineProfile`` already declares the shape; this module
is the loader that was missing.

Three rules, and they are the reason this is a module rather than three lines in the CLI:

* an unknown, unreadable or invalid profile **refuses** - there is no default binding and no
  fallback to a cheaper transport;
* a role that the profile does not bind **refuses** instead of silently inheriting another
  role's agent, and both roles a run can dispatch are required up front;
* the profile document is digested *as loaded*, so an approval can name the exact
  configuration it covers (``EffectiveConfig.digest``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from .contracts import AgentBinding, MachineProfile, RefusalCode, RefusedError, digest_of

#: Environment variable naming the profile to use when ``--profile`` is absent. Declared here
#: and nowhere else: the CLI/profile/environment precedence is defined exactly once, in
#: :func:`requested_profile_id`.
ENV_PROFILE = "HFLOW_PROFILE"

#: Every role a run in this build can dispatch. Both must resolve, even for a task that will
#: not ask for a review: a task revision can start asking, and a profile that cannot bind the
#: reviewer is incomplete rather than "review-optional".
RUN_ROLES = ("implementer", "reviewer")

#: A profile id is a plain file name: an allowlist, not a blacklist of separators. A blacklist
#: missed ``C:evil`` - on Windows ``profiles / "C:evil.json"`` discards the data-dir prefix and
#: names a drive-relative file in the current directory, typically a checkout - and ``a:b``,
#: which names an NTFS alternate data stream.
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: Windows device names. ``NUL.json`` or ``CON`` opens the device, not a file, whatever the
#: directory in front of it.
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _is_plain_profile_id(name: str) -> bool:
    if not _PROFILE_ID.fullmatch(name):
        return False
    return name.split(".", 1)[0].upper() not in _RESERVED_DEVICE_NAMES


def profiles_dir(data_dir: Path) -> Path:
    """Where profiles for this runtime data directory live."""
    return Path(data_dir) / "profiles"


def profile_path(data_dir: Path, profile_id: str) -> Path:
    return profiles_dir(data_dir) / f"{profile_id}.json"


def requested_profile_id(cli_value: str | None, env: Mapping[str, str] | None = None) -> str:
    """Which profile was asked for: ``--profile`` beats ``HFLOW_PROFILE``.

    That is the whole precedence. There is no compiled-in default profile and no
    "first profile in the directory" guess, so "no profile selected" stays distinguishable
    from "this profile was selected". A blank value is not a name: whitespace around one is
    stripped, and a value that is empty either way falls through to the next source.
    """
    source = env if env is not None else os.environ
    from_cli = (cli_value or "").strip()
    if from_cli:
        return from_cli
    return (source.get(ENV_PROFILE) or "").strip()


def profile_digest(profile: MachineProfile) -> str:
    """Content identity of the loaded profile document."""
    return digest_of(profile.model_dump(mode="json"))


def load_profile(data_dir: Path, profile_id: str) -> MachineProfile:
    """Read one profile from disk, or refuse.

    Every failure mode is a refusal rather than a default: a missing id, a missing file, a
    file that is not JSON, and a document that does not match ``MachineProfile`` (unknown
    fields included - the contract is ``extra="forbid"``).
    """
    name = (profile_id or "").strip()
    if not name:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "no machine profile was named. Pass --profile <id> or set "
            f"{ENV_PROFILE}; this build has no default profile and does not pick one "
            "from the directory.",
        )
    if name != profile_id or not _is_plain_profile_id(name):
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile id {profile_id!r} is not a plain file name (letters, digits, '.', '_' "
            f"and '-', not a Windows device name); profiles live at "
            f"{profiles_dir(data_dir)}/<id>.json",
        )
    path = profile_path(data_dir, name)
    # Second, structural check: whatever the id, the file read must sit directly in the
    # profiles directory. The allowlist should make this unreachable; it is here so a future
    # relaxation of the allowlist cannot quietly reopen a read from inside a checkout. The
    # parent is resolved, not the file, so a profile file that is itself a link still loads.
    if path.parent.resolve() != profiles_dir(data_dir).resolve():
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile id {profile_id!r} is not a plain file name: it resolves to {path}, "
            f"outside {profiles_dir(data_dir)}",
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile {name!r} not found at {path}. Nothing falls back to a default "
            "binding; create the profile or name one that exists.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"profile {name!r} at {path} is not valid JSON: {exc}"
        ) from exc
    except OSError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"profile {name!r} at {path} could not be read: {exc}"
        ) from exc
    try:
        profile = MachineProfile.model_validate(document)
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile {name!r} at {path} does not match the MachineProfile contract: "
            f"{summarize_validation_error(exc)}",
        ) from exc
    if profile.profile_id != name:
        # A file whose contents name a different profile would make the recorded identity
        # disagree with the file the user named, and the digest would then cover a profile
        # nobody asked for.
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile file {path.name} declares profile_id {profile.profile_id!r}; the file "
            f"name and the declared id must agree",
        )
    return profile


def summarize_validation_error(exc: ValidationError) -> str:
    """One line per rejected field: the reader needs the field, not the pydantic traceback.

    Shared by every loader that turns a malformed input file into a refusal (profile, task
    spec, project contract, authorization), so they all read the same way.
    """
    parts = []
    for error in exc.errors()[:6]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "(document)"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    if len(exc.errors()) > 6:
        parts.append(f"... and {len(exc.errors()) - 6} more")
    return "; ".join(parts)


def role_binding(profile: MachineProfile, role: str) -> tuple[str, AgentBinding]:
    """The agent id and binding one role actually uses.

    An unbound role refuses; it is never quietly pointed at another role's agent. That is the
    difference between "this profile says both roles use DSH" and "nobody said, so it must be
    fine".
    """
    agent_id = profile.role_bindings.get(role)
    if not agent_id:
        known = ", ".join(sorted(profile.role_bindings)) or "none"
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile {profile.profile_id!r} does not bind role {role!r} (it binds: {known}). "
            "A run dispatches an implementer and, when review is required, a reviewer; both "
            "must be bound explicitly rather than inheriting another role's agent.",
        )
    binding = profile.agents.get(agent_id)
    if binding is None:
        known = ", ".join(sorted(profile.agents)) or "none"
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile {profile.profile_id!r} binds role {role!r} to agent {agent_id!r}, which "
            f"is not declared under 'agents' (declared: {known})",
        )
    return agent_id, binding


def resolve_role_bindings(profile: MachineProfile) -> dict[str, tuple[str, AgentBinding]]:
    """Both run roles, resolved, or a refusal naming the role that could not be resolved."""
    return {role: role_binding(profile, role) for role in RUN_ROLES}
