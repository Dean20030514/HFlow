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
from typing import TypeVar

from pydantic import BaseModel, ValidationError

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


def available_profile_ids(data_dir: Path) -> list[str]:
    """The ids of the profile files in ``<data_dir>/profiles``, sorted. Listed, never opened."""
    try:
        names = [path.stem for path in profiles_dir(data_dir).glob("*.json") if path.is_file()]
    except OSError:
        return []
    return sorted(name for name in names if _is_plain_profile_id(name))


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
    existing = ", ".join(available_profile_ids(data_dir)) or "none"
    profile = load_model(
        path,
        MachineProfile,
        what=f"profile {name!r}",
        invalid=f"profile {name!r} at {path} does not match the MachineProfile contract",
        missing_hint=(
            f"Profiles in {profiles_dir(data_dir)}: {existing}. Nothing falls back to a default "
            "binding; create the profile or name one that exists."
        ),
    )
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


# --------------------------------------------------------------------------
# reading an input file: the one loader every hand-written JSON document goes through
# --------------------------------------------------------------------------

#: UTF-16 byte-order marks. Windows PowerShell 5.1's ``>`` and ``Out-File`` write UTF-16 LE with
#: one by default, so a task, profile or approval saved that way is common - and never JSON HFlow
#: reads.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
#: How to save a file as UTF-8 from the shell that most often wrote it as UTF-16.
_UTF8_FIX = (
    "save it as UTF-8 (in Windows PowerShell 5.1: `Get-Content FILE | Set-Content -Encoding utf8 "
    "FILE`, or write it with PowerShell 7, whose default is UTF-8)"
)


def load_json_file(
    path: Path,
    *,
    what: str,
    missing_hint: str = "",
    missing_code: RefusalCode = RefusalCode.INVALID_SPEC,
) -> object:
    """Read one hand-written JSON input file, refusing rather than defaulting.

    The one reader for every input document (task, project contract, machine profile, plan,
    authorization, root budget, repair policy, fake write plan), so each fails the same way: a
    missing file is ``missing_code`` (with ``missing_hint`` after it: how to supply the file),
    and anything else that keeps the file from being read - a directory, a permission, an I/O
    error, UTF-16 or other non-UTF-8 text, invalid JSON - is an ``invalid_spec`` refusal naming
    the file. Never a traceback.

    The text is decoded as ``utf-8-sig``: a UTF-8 byte-order mark (what Windows PowerShell 5.1's
    ``Set-Content -Encoding utf8`` writes) is accepted and dropped. Nothing is digested from the
    bytes - every digest is over the parsed, validated document - so the mark changes no binding.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise RefusedError(
            missing_code,
            f"{what} not found: {path}" + (f". {missing_hint}" if missing_hint else ""),
        ) from exc
    except OSError as exc:
        why = "it is a directory, not a file" if path.is_dir() else str(exc)
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"{what} {path} could not be read: {why}"
        ) from exc
    if data.startswith(_UTF16_BOMS) or b"\x00" in data[:4]:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{what} {path} could not be read: it is UTF-16 text, not UTF-8 JSON; {_UTF8_FIX}",
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{what} {path} could not be read: it is not valid UTF-8 JSON ({exc}); {_UTF8_FIX}",
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"{what} {path} is not valid JSON: {exc}"
        ) from exc
    except (ValueError, RecursionError) as exc:  # an over-long integer, nesting past the stack
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"{what} {path} could not be decoded as JSON: {exc}"
        ) from exc


_Model = TypeVar("_Model", bound=BaseModel)


def load_model(
    path: Path,
    model: type[_Model],
    *,
    what: str,
    invalid: str,
    summarize: bool = True,
    not_object: str = "",
    missing_hint: str = "",
    missing_code: RefusalCode = RefusalCode.INVALID_SPEC,
) -> _Model:
    """Read one input file (:func:`load_json_file`) as ``model``; a mismatch is ``invalid_spec``.

    ``invalid`` starts the refusal for a document that does not match, followed by the rejected
    fields (:func:`summarize_validation_error`) or, with ``summarize=False``, pydantic's own text.
    ``not_object``, when given, refuses a document that is not a JSON object before validation.
    """
    raw = load_json_file(
        Path(path), what=what, missing_hint=missing_hint, missing_code=missing_code
    )
    if not_object and not isinstance(raw, dict):
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"{not_object}, not a JSON {type(raw).__name__}"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        reason = summarize_validation_error(exc) if summarize else str(exc)
        raise RefusedError(RefusalCode.INVALID_SPEC, f"{invalid}: {reason}") from exc


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
