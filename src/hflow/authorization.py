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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    EffectiveConfig,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    RootBudgetBinding,
    RootBudgetLimits,
    RootBudgetPlan,
    RunRequest,
    canonical_json,
    digest_of,
)

SCHEMA_VERSION = 1
#: Only this provenance may authorize a real model run. A model-authored note is refused.
USER_PROVENANCE = "user"
#: The driver id of the offline fake. A record the CLI synthesized for it is never accepted for
#: any other driver; the constant lives here so the check and the CLI agree on one spelling.
OFFLINE_DRIVER_ID = "fake"
#: Modes kept separate on purpose: a stop trial never authorizes a business task.
ExecutionMode = Literal["stop-trial", "m2-live-change"]

#: Binding fields added after the first live rounds. They are omitted from ``binding_digest``
#: while empty so that an artifact written before they existed digests to exactly the same
#: value as it did then - a different digest for an already-consumed authorization would make
#: it look unused again, which is the one thing the single-use ledger must never allow.
POST_BINDING_FIELDS = ("profile_id", "effective_config_digest", "root_budget")


def resolve_root_binding(
    *,
    project_id: str,
    request: RunRequest,
    data_dir: Path | str,
) -> RootBudgetBinding:
    """The root binding for this run: derived from the project, repository, task and ledger.

    ``data_dir`` is only string work here - the ledger's path is computed, never opened, so
    ``hflow prepare`` can report a binding without creating the SQLite file the binding names.
    """
    from .paths import database_path

    ledger = database_path(Path(data_dir))
    if str(ledger) != ":memory:":
        ledger = ledger.resolve()
    return RootBudgetBinding.derive(
        project_id=project_id,
        repo_path=str(Path(request.project_root)),
        task_id=request.task.task_id,
        ledger_path=str(ledger),
    )


def root_budget_from_plan(plan: RootBudgetPlan) -> RootBudgetPlan:
    """Validate a parsed root budget file, refusing anything this build cannot honour.

    Only the shape is validated here. Whether the root still has *allowance* is decided by the
    dispatch transaction against the ledger, because a preview cannot know what earlier
    revisions of the same task consumed.
    """
    if plan.limits.max_top_level_submissions < 1:
        raise RefusedError(
            RefusalCode.BUDGET_EXHAUSTED,
            "a root budget must allow at least one top-level submission",
        )
    return plan



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
    #: Batch E1. Present only for a run that is spent against a root ledger. It is dropped
    #: from the digest while it is absent, so an artifact written before roots existed keeps
    #: the digest it always had and its consumed ledger row keeps matching.
    root_budget: RootBudgetBinding | None = None

    def digest(self) -> str:
        """Identity of what this binding covers.

        Backward compatible on purpose: the post-batch-D fields are dropped while they are
        empty, so an artifact written by an earlier build digests to the value it always had
        and its single-use ledger row keeps matching. Dropping them is what stops an
        already-consumed approval from looking unused again - while a binding that *does*
        carry a configuration digests differently from the same task on another profile.

        ``root_budget`` follows the same rule, one level deeper: with no root the key is
        absent from the payload entirely, so a legacy artifact's digest is unchanged rather
        than merely recomputed from a default. Inside a root binding every field is always
        present, so two E-mode artifacts cannot collide by omitting one of them.
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
    #: Batch E1. The immutable ceilings of the root this artifact names. Required whenever the
    #: binding carries a root: an approval that named a root but not what it may spend would
    #: otherwise get whatever default the build happens to use, which is a limit nobody asked
    #: for. Absent for a legacy artifact, which is then simply not a root run.
    root_limits: RootBudgetLimits | None = None
    #: Where this artifact came from. ``provided_by`` says who approved it, and for a real
    #: artifact that is the user. This field says whether the *record* was written by a user or
    #: synthesized by the CLI for the offline driver, which reaches no model and therefore has no
    #: approval to give. Without it a CLI-computed record would carry ``provided_by="user"`` - a
    #: structurally false statement, however clearly its ``user_text`` disclaims being one.
    #:
    #: Nothing in the dispatch path upgrades this: a synthetic record authorizes no real run, and
    #: :func:`verify_authorization` refuses it outside an offline binding.
    origin: Literal["user_artifact", "cli_offline_synthetic"] = "user_artifact"

    @model_validator(mode="after")
    def _root_shape(self) -> AuthorizationRecord:
        if self.binding.root_budget is not None and self.root_limits is None:
            raise ValueError(
                "this authorization binds a root budget but declares no root_limits; the "
                "allowance a root spends must be part of the approval, not a build default"
            )
        return self

    def binding_digest(self) -> str:
        return self.binding.digest()

    def root_budget(self) -> RootBudgetBinding | None:
        return self.binding.root_budget

    def as_store_record(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "mode": self.binding.mode,
            "binding_digest": self.binding_digest(),
            "user_text": self.user_text,
            "provided_by": self.provided_by,
            "authorized_at": self.authorized_at,
            "max_top_level_submissions": self.max_top_level_submissions,
            # Recorded so a reader of the ledger can see which root an artifact was spent
            # against without re-deriving it from a file that may since have changed.
            "root_id": self.binding.root_budget.root_id if self.binding.root_budget else "",
            "root_budget_json": canonical_json(self.binding.root_budget.model_dump(mode="json"))
            if self.binding.root_budget
            else "",
            "root_limits_json": canonical_json(self.root_limits.model_dump(mode="json"))
            if self.root_limits
            else "",
            # Recorded so a reader of the ledger sees the provenance of the *record* and not only
            # of the approval: a synthetic offline artifact must stay distinguishable after the
            # fact, not just at the moment the CLI minted it.
            "origin": self.origin,
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
    root_binding: RootBudgetBinding | None = None,
) -> AuthorizationBinding:
    """The binding of the run that is about to happen.

    ``effective`` is the configuration that was actually resolved. ``prepare`` and ``run``
    pass the same object from the same resolution, so a user approves the configuration they
    were shown. Callers that do not resolve one (the historical re-verification tools) get the
    legacy binding: identical to what those artifacts already carry.

    ``root_binding`` is batch E1. When a root budget file was given, ``prepare`` and ``run``
    pass the *same* derived binding here, so the digest the user is shown is the digest their
    artifact must carry and ``verify_authorization`` accepts it. It stays ``None`` only when no
    root budget file was supplied, which is the legacy shape: an artifact with no root, spent
    against no ledger.
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
    if root_binding is not None:
        binding.root_budget = root_binding
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
    if record.origin != "user_artifact" and expected.driver != OFFLINE_DRIVER_ID:
        # The offline record exists so the offline path can charge a root at all. It is not an
        # approval and must never cover a real transport: the binding names the fake driver, so
        # this is a second, structural check rather than a matter of reading user_text.
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            f"this artifact is {record.origin!r}, not a user-written approval: it was synthesized "
            "for the offline driver, which reaches no model. It cannot authorize a run on driver "
            f"{expected.driver!r}; write your own authorization file for a real run.",
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
    _verify_root_budget(actual, expected)


def _verify_root_budget(actual: AuthorizationBinding, expected: AuthorizationBinding) -> None:
    """The root half of the check, on the same terms as the configuration half.

    Two directions, and both matter:

    * the run resolves a root but the artifact carries none -> the artifact authorizes a task
      whose spending is not accounted anywhere, so it is refused rather than spent outside a
      ledger;
    * the artifact carries a root but this run resolves none -> the artifact was written for a
      root run and is being used as if it were a plain one; refusing is the only reading that
      cannot leak an allowance.

    A mismatch in any field - including ``ledger_path`` - is a mismatch: the same task pointed
    at another data directory is not the same ledger, and treating it as one would hand the
    task a second unused allowance.
    """
    if actual.root_budget is None and expected.root_budget is None:
        return
    if actual.root_budget is None:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "this authorization carries no root budget binding, but this run resolves one: the "
            "approval would cover a task whose consumption no ledger accounts for. Re-issue the "
            "authorization against the root budget file, or drop the root budget.",
        )
    if expected.root_budget is None:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "this authorization is bound to a root budget ledger, but this run resolves no root: "
            "it was written for a run that spends against that root, and reusing it here would "
            "spend an approval outside the ledger it names.",
        )
    left, right = actual.root_budget, expected.root_budget
    fields = ("root_id", "project_id", "repo_path", "task_id", "ledger_path")
    differences = [
        f"{field}: authorized {getattr(left, field)!r} != actual {getattr(right, field)!r}"
        for field in fields
        if getattr(left, field) != getattr(right, field)
    ]
    if differences:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the authorization's root budget does not cover this run: " + "; ".join(differences),
        )


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
