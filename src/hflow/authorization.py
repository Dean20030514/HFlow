"""One-shot authorization for a real Harness run.

Why this exists: an earlier iteration accepted ``--live-authorized``, a bare flag. That is
worthless as a gate, because the same process that would run the task can also type the flag -
an agent could authorize itself. This module replaces it with an artifact that

* contains the **user's own words** (``provided_by: user``) rather than a summary written by
  the agent that wants to run;
* is **bound** to this exact execution: driver, task spec digest, project, repository root,
  base commit and the number of submissions allowed;
* is **single-use**, enforced transactionally in SQLite, so a restart, a new run id or a
  resubmitted identical spec cannot restore allowance.

The artifact is a plain JSON file the user's approval produces. It is not a signing service,
an approval database or a permission platform - it is the smallest thing that makes "the user
said yes" distinguishable from "the model said yes".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    EffectiveConfig,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    RunRequest,
    canonical_json,
    digest_of,
)

SCHEMA_VERSION = 1
#: Only this provenance may authorize a real model run. A model-authored note is refused.
USER_PROVENANCE = "user"
#: Modes kept separate on purpose: a stop trial never authorizes a business task.
ExecutionMode = Literal["stop-trial", "m2-live-change"]

#: Binding fields added after the first live rounds. They are omitted from ``binding_digest``
#: while empty so that an artifact written before they existed digests to exactly the same
#: value as it did then - a different digest for an already-consumed authorization would make
#: it look unused again, which is the one thing the single-use ledger must never allow.
POST_BINDING_FIELDS = ("profile_id", "effective_config_digest")


class AuthorizationBinding(BaseModel):
    """What this authorization is for. Any mismatch refuses the run.

    Beyond the task and the repository, a binding from this build also names the *effective
    configuration* - the resolved profile, each role's agent, driver and model selection, and
    the write permission. An approval therefore covers a configuration, not just a task:
    switching profile or model after approval changes the digest and the old approval stops
    applying. Artifacts written before that field existed still load; they simply cannot
    authorize a run that resolved a configuration (see :func:`verify_authorization`).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    driver: str
    project_id: str
    repo_path: str
    base_commit: str
    spec_digest: str
    spec_path: str
    roles: list[str] = Field(default_factory=lambda: ["implementer", "reviewer"])
    #: ``EffectiveConfig.digest()`` of the configuration this run will actually use.
    effective_config_digest: str = ""
    #: The profile the configuration came from, for a readable mismatch message. Empty when
    #: the bindings came from the command line alone.
    profile_id: str = ""

    def digest(self) -> str:
        """Identity of what this binding covers.

        Backward compatible on purpose: the post-batch-D fields are dropped while they are
        empty, so an artifact written by an earlier build digests to the value it always had
        and its single-use ledger row keeps matching. Dropping them is what stops an
        already-consumed approval from looking unused again - while a binding that *does*
        carry a configuration digests differently from the same task on another profile.
        """
        payload = self.model_dump(mode="json")
        for field in POST_BINDING_FIELDS:
            if not payload.get(field):
                payload.pop(field, None)
        return digest_of(payload)


class AuthorizationRecord(BaseModel):
    """The artifact. `user_text` is the approval quote, verbatim."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    authorization_id: str
    provided_by: Literal["user"] = "user"
    user_text: str
    authorized_at: str
    max_top_level_submissions: int = Field(ge=1, le=4)
    binding: AuthorizationBinding

    def binding_digest(self) -> str:
        return self.binding.digest()

    def as_store_record(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "mode": self.binding.mode,
            "binding_digest": self.binding_digest(),
            "user_text": self.user_text,
            "provided_by": self.provided_by,
            "authorized_at": self.authorized_at,
            "max_top_level_submissions": self.max_top_level_submissions,
        }


def load_authorization(path: Path) -> AuthorizationRecord:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefusedError(
            RefusalCode.NOT_IMPLEMENTED,
            f"authorization file not found: {path}. A real Harness run needs an explicit, "
            "bound user authorization; there is no flag that substitutes for one.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise RefusedError(RefusalCode.INVALID_SPEC, f"{path} is not valid JSON: {exc}") from exc
    record = AuthorizationRecord.model_validate(document)
    if not record.user_text.strip():
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the authorization carries no user text; an empty note is not an approval",
        )
    return record


def current_binding(
    *,
    mode: ExecutionMode,
    driver: str,
    project: ProjectConfig,
    request: RunRequest,
    spec_path: Path,
    effective: EffectiveConfig | None = None,
) -> AuthorizationBinding:
    """The binding of the run that is about to happen.

    ``effective`` is the configuration that was actually resolved. ``prepare`` and ``run``
    pass the same object from the same resolution, so a user approves the configuration they
    were shown. Callers that do not resolve one (the historical re-verification tools) get the
    legacy binding: identical to what those artifacts already carry.
    """
    binding = AuthorizationBinding(
        mode=mode,
        driver=driver,
        project_id=project.project_id,
        repo_path=str(Path(request.project_root).resolve()),
        base_commit=request.task.workspace.base_commit,
        spec_digest=request.task.spec_digest(),
        spec_path=str(Path(spec_path).resolve()),
        roles=list(effective_roles(effective)),
    )
    if effective is not None:
        binding.effective_config_digest = effective.digest()
        binding.profile_id = effective.profile_id
    return binding


def effective_roles(effective: EffectiveConfig | None) -> tuple[str, ...]:
    """Which roles the approval has to cover: the ones the configuration resolves."""
    if effective is None:
        return ("implementer", "reviewer")
    return tuple(entry.role for entry in effective.roles) or ("implementer", "reviewer")


def verify_authorization(
    record: AuthorizationRecord,
    *,
    expected: AuthorizationBinding,
) -> None:
    """Refuse unless the artifact authorizes exactly this run."""
    if record.provided_by != USER_PROVENANCE:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            f"authorization provenance is {record.provided_by!r}, not {USER_PROVENANCE!r}: a note "
            "written by the agent that wants to run is not an approval",
        )
    actual = record.binding
    mismatches: list[str] = []
    for field in (
        "mode",
        "driver",
        "project_id",
        "repo_path",
        "base_commit",
        "spec_digest",
        "spec_path",
    ):
        left, right = getattr(actual, field), getattr(expected, field)
        if left != right:
            mismatches.append(f"{field}: authorized {left!r} != actual {right!r}")
    if mismatches:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the authorization does not cover this run: " + "; ".join(mismatches),
        )
    _verify_effective_config(actual, expected)


def _verify_effective_config(actual: AuthorizationBinding, expected: AuthorizationBinding) -> None:
    """The configuration half of the check, checked only when a configuration was resolved.

    An artifact written before this field existed carries no configuration digest. It stays
    *readable* - it loads, it is listed, its ledger row still matches - but it cannot start a
    run that resolved a configuration, because nothing in it says which profile, model or
    permission the user approved. Re-issue it instead of reusing it.
    """
    if not expected.effective_config_digest:
        return
    if not actual.effective_config_digest:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "this authorization carries no effective-configuration binding, so it does not say "
            "which profile, model selection or write permission it approves. It predates "
            "configuration binding and cannot authorize this run; re-issue it against the "
            "current configuration.",
        )
    if actual.effective_config_digest != expected.effective_config_digest:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the authorization covers a different configuration than this run resolved: "
            f"authorized profile={actual.profile_id or '(command line)'} "
            f"digest={actual.effective_config_digest} != actual "
            f"profile={expected.profile_id or '(command line)'} "
            f"digest={expected.effective_config_digest}. Switching profile, model selection "
            "or write permission after approval does not extend the approval.",
        )


def describe(record: AuthorizationRecord) -> str:
    return canonical_json(
        {
            "authorization_id": record.authorization_id,
            "mode": record.binding.mode,
            "max_top_level_submissions": record.max_top_level_submissions,
            "binding_digest": record.binding_digest(),
            "user_text": record.user_text,
        }
    )
