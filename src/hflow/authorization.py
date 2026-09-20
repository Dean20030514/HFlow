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

from .contracts import ProjectConfig, RefusalCode, RefusedError, RunRequest, canonical_json, digest_of

SCHEMA_VERSION = 1
#: Only this provenance may authorize a real model run. A model-authored note is refused.
USER_PROVENANCE = "user"
#: Modes kept separate on purpose: a stop trial never authorizes a business task.
ExecutionMode = Literal["stop-trial", "m2-live-change"]


class AuthorizationBinding(BaseModel):
    """What this authorization is for. Any mismatch refuses the run."""

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    driver: str
    project_id: str
    repo_path: str
    base_commit: str
    spec_digest: str
    spec_path: str
    roles: list[str] = Field(default_factory=lambda: ["implementer", "reviewer"])


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
        return digest_of(self.binding.model_dump(mode="json"))

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
) -> AuthorizationBinding:
    """The binding of the run that is about to happen."""
    return AuthorizationBinding(
        mode=mode,
        driver=driver,
        project_id=project.project_id,
        repo_path=str(Path(request.project_root).resolve()),
        base_commit=request.task.workspace.base_commit,
        spec_digest=request.task.spec_digest(),
        spec_path=str(Path(spec_path).resolve()),
    )


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
