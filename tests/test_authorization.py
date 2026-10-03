"""Authorized real-run gate: the positive path, and every way it must refuse.

The earlier CLI only had a negative test ("unauthorized is refused"), which does not show that
an authorized run works. These tests cover both directions with stubs, so no credential is read
and no real Harness is launched.

The authorization artifact is the user's own approval text bound to one execution. The tests
that matter most are the ones that would let an agent authorize itself, or reuse an approval
for a different task: both must be refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hflow.authorization import (
    AuthorizationBinding,
    AuthorizationRecord,
    current_binding,
    load_authorization,
    verify_authorization,
)
from hflow.contracts import (
    RefusalCode,
    RefusedError,
    ReviewRequirement,
    RunRequest,
    TaskState,
    digest_of,
)
from hflow.controller import Controller
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

USER_TEXT = "I approve one real M2 run on this exact task, driver and base commit."

#: A checked-in authorization written in the *old* format - the shape this repository produced
#: before effective-configuration binding existed. It is a synthetic fixture: it names no real
#: repository, and its `user_text` says so. It is deliberately not a copy of any real approval,
#: and no test reads a real one from disk.
LEGACY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_authorization.json"

#: The binding digest of that fixture, computed the way the pre-batch-D code computed it: over
#: exactly the eight fields that existed then. A single-use ledger row is keyed by this value,
#: so if it ever changes, an already-consumed approval looks unused again and can be spent
#: twice. Pinned by value, and cross-checked against the recipe rather than only against itself.
LEGACY_FIXTURE_DIGEST = "sha256:604316e4547082df38f60a6d971a30b74c65a2237fa949bfb4b654a17e03a30f"
#: The fields the old formula covered. Spelled out here so "the new fields are excluded while
#: empty" is checked against an independent statement of the old rule.
LEGACY_BINDING_FIELDS = (
    "mode",
    "driver",
    "project_id",
    "repo_path",
    "base_commit",
    "spec_digest",
    "spec_path",
    "roles",
)


class RecordingDriver:
    """A driver that reports a launch unlike the fake driver's, and records every call."""

    driver_id = "stub-real-driver"

    def __init__(self, *, probe_ok: bool = True) -> None:
        self.probe_ok = probe_ok
        self.invocations: list[str] = []

    def probe(self, binding):  # noqa: ANN001, ANN201
        from hflow.contracts import CapabilityReport, CapabilityState

        return CapabilityReport(
            driver_id=self.driver_id,
            driver_version="stub",
            harness=binding.harness,
            os="test",
            arch="test",
            capabilities={"prompt_turn": CapabilityState.PROBED},
        )

    def readonly_client_check(self, args: list[str] | None = None, *, timeout_seconds: int = 60) -> dict:
        return {
            "argv": ["stub", "--version"],
            "returncode": 0 if self.probe_ok else 1,
            "timed_out": False,
            "stdout": "9.9.9\n" if self.probe_ok else "",
            "stderr": "" if self.probe_ok else "stub client missing",
            "boundary_kind": "stub",
            "boundary_empty": True,
            "process_gone": True,
        }

    def start(self, request):  # noqa: ANN001, ANN201
        from hflow.contracts import CandidateRef, InvocationOutcome, InvocationResult

        self.invocations.append(request.role)
        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=InvocationOutcome.COMPLETED,
            candidate=CandidateRef(base_ref="base:stub", change_summary="stub change"),
            agent_turns=1,
            limitations=["stub driver: no model was invoked"],
        )

    def cancel(self, invocation_id: str):  # noqa: ANN001, ANN201
        from hflow.contracts import CancellationReceipt

        return CancellationReceipt(invocation_id=invocation_id, status="confirmed_stopped")

    def reconcile(self, invocation_id: str):  # noqa: ANN001, ANN201
        from hflow.contracts import ReconcileOutcome, ReconcileResult

        return ReconcileResult(invocation_id=invocation_id, outcome=ReconcileOutcome.UNKNOWN)


def _record(
    request: RunRequest,
    project,
    task_path: Path,
    *,
    max_submissions: int = 2,
    provided_by: str = "user",
    mode: str = "m2-live-change",
    driver: str = "acpx-dsh",
    **binding_overrides: str,
) -> AuthorizationRecord:
    binding = current_binding(
        mode=mode,  # type: ignore[arg-type]
        driver=driver,
        project=project,
        request=request,
        spec_path=task_path,
    )
    payload = binding.model_dump(mode="json")
    payload.update(binding_overrides)
    return AuthorizationRecord(
        authorization_id="AUTH-test-1",
        user_text=USER_TEXT,
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=max_submissions,
        binding=AuthorizationBinding.model_validate(payload),
        **({"provided_by": provided_by} if provided_by == "user" else {}),
    )


@pytest.fixture()
def real_request(project, task_spec, project_root: Path) -> RunRequest:
    return RunRequest(
        task=task_spec,
        project=project,
        project_root=project_root,
        workspace_root=project_root,
    )


def _controller(store: Store, tmp_path: Path, driver, authorization, *, preflight_ok: bool = True):
    return Controller(
        store,
        driver,
        controller_build="auth-test",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=tmp_path / "data",
        authorization=authorization,
        preflight=(lambda: (preflight_ok, "client reports 9.9.9" if preflight_ok else "stub client missing")),
    )


# --------------------------------------------------------------------------
# the positive path
# --------------------------------------------------------------------------


def test_authorized_run_dispatches_and_records_the_consumed_submission(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    authorization = _record(real_request, project, tmp_path / "task.json")
    # Review is a separate submission and is covered by its own test; here the point is the
    # implementer dispatch and the ledger record. Both the project floor *and* the task request
    # have to say no for a run to skip review (plan 6), so the task is narrowed too.
    no_review = project.model_copy(update={"review_required": False})
    task_without_review = real_request.task.model_copy(
        update={"review": ReviewRequirement(required=False)}
    )
    controller = _controller(store, tmp_path, driver, authorization)
    try:
        outcome = controller.run_task(
            real_request.model_copy(update={"project": no_review, "task": task_without_review})
        )

        assert driver.invocations == ["implementer"], driver.invocations
        assert outcome.task_state in {TaskState.BLOCKED, TaskState.ACCEPTED}
        state = store.authorization_state("AUTH-test-1")
        assert state is not None
        assert state["used_top_level_submissions"] == 1
        assert state["max_top_level_submissions"] == 2
        assert state["provided_by"] == "user"
        assert state["user_text"] == USER_TEXT, "the user's own words are recorded verbatim"
        assert any(
            "consumed top-level submission 1/2" in note for note in store.notes_for(outcome.run_id)
        ), store.notes_for(outcome.run_id)
    finally:
        store.close()


def test_reviewer_is_a_separate_consumed_submission(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """Implementer and reviewer are two invocations and two submissions."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    authorization = _record(real_request, project, tmp_path / "task.json", max_submissions=2)
    controller = _controller(store, tmp_path, driver, authorization)
    try:
        outcome = controller.run_task(real_request)
        assert driver.invocations == ["implementer", "reviewer"], (
            "the reviewer must be its own invocation, not a continuation"
        )
        state = store.authorization_state("AUTH-test-1")
        assert state is not None and state["used_top_level_submissions"] == 2
    finally:
        store.close()


# --------------------------------------------------------------------------
# refusals: self-authorization, mismatch, exhaustion, preflight
# --------------------------------------------------------------------------


def test_a_recorded_authorization_from_an_earlier_build_still_loads_and_digests_the_same(
    tmp_path: Path,
) -> None:
    """Read compatibility, pinned by value against a checked-in fixture.

    The fixture is the old format: no ``effective_config_digest``, no ``profile_id``, no root
    budget. It must still load, and its binding digest must still be the value its
    consumed-allowance row is keyed by. The digest is checked twice on purpose - against the
    pinned constant, and against an independent restatement of the old formula - so a change to
    either the stored value or the computation fails here.

    Batch E1 adds ``root_budget`` to the binding. It is asserted to be *absent in meaning* here
    (``None``), not merely defaulted: the digest drops it while it is empty, which is what keeps
    an already-consumed legacy approval matching its ledger row.
    """
    record = load_authorization(LEGACY_FIXTURE)

    assert record.authorization_id == "AUTH-legacy-fixture-1"
    assert record.provided_by == "user"
    assert record.binding.effective_config_digest == ""
    assert record.binding.profile_id == ""
    assert record.binding.root_budget is None
    assert record.binding.project_contract_digest == ""
    assert record.binding.launch_content_digest == ""
    assert record.root_limits is None
    assert record.binding_digest() == LEGACY_FIXTURE_DIGEST

    binding_document = record.binding.model_dump(mode="json")
    assert set(binding_document) == set(LEGACY_BINDING_FIELDS) | {
        "effective_config_digest",
        "profile_id",
        "root_budget",
        "project_contract_digest",
        "launch_content_digest",
    }
    old_recipe = digest_of({field: binding_document[field] for field in LEGACY_BINDING_FIELDS})
    assert old_recipe == LEGACY_FIXTURE_DIGEST

    # Re-serializing and reloading it is still the same artifact, not a re-bound one.
    copy = tmp_path / "copy.json"
    copy.write_text(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    assert load_authorization(copy).binding_digest() == LEGACY_FIXTURE_DIGEST


def test_a_configuration_digest_separates_two_otherwise_identical_approvals(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """Two approvals for the same task on different configurations are different approvals."""
    from hflow.contracts import EffectiveConfig, RoleConfig

    def config(model: str) -> EffectiveConfig:
        return EffectiveConfig(
            source="machine_profile",
            profile_id="dsh-local",
            profile_digest="sha256:" + "0" * 64,
            roles=[
                RoleConfig(
                    role=role,
                    agent="a",
                    harness="dsh",
                    driver="acpx-dsh",
                    driver_id="acpx-dsh-acp",
                    model_selection=model,
                )
                for role in ("implementer", "reviewer")
            ],
        )

    spec_path = tmp_path / "task.json"
    first = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=real_request,
        spec_path=spec_path,
        effective=config("model-one"),
    )
    second = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=real_request,
        spec_path=spec_path,
        effective=config("model-two"),
    )
    assert first.digest() != second.digest()

    record = AuthorizationRecord(
        authorization_id="AUTH-config-1",
        user_text=USER_TEXT,
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=2,
        binding=first,
    )
    verify_authorization(record, expected=first)  # the configuration it names is accepted
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(record, expected=second)
    assert "different configuration" in excinfo.value.message

    # A store row for one configuration does not silently cover the other under one id.
    store = Store(tmp_path / "hflow.sqlite")
    try:
        store.register_authorization(record.as_store_record())
        other = record.model_copy(update={"binding": second})
        with pytest.raises(StoreError) as excinfo:
            store.register_authorization(other.as_store_record())
        assert "different target" in str(excinfo.value)
    finally:
        store.close()


def test_a_root_approval_does_not_cover_another_data_dir(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """The ledger path is part of what a root approval covers, and both directions are refused.

    Why this is not merely a path check: an approval spent against one ledger would look unused
    again if the same task could be pointed at another ``--data-dir``, which is exactly the
    "restart or rename and get a second allowance" hole the single-use rule exists to close. The
    other direction matters too - an artifact written *for* a root must not be spent as if it
    were a plain approval, because then the consumption it names would be recorded nowhere.
    """
    from hflow.authorization import resolve_root_binding
    from hflow.contracts import RootBudgetLimits

    spec_path = tmp_path / "task.json"
    first_dir, second_dir = tmp_path / "data-one", tmp_path / "data-two"
    first_root = resolve_root_binding(
        project_id=project.project_id, request=real_request, data_dir=first_dir
    )
    second_root = resolve_root_binding(
        project_id=project.project_id, request=real_request, data_dir=second_dir
    )
    assert first_root.root_id == second_root.root_id, (
        "the root identity must follow the task, not the ledger: otherwise a data-dir change "
        "would also be a new root"
    )
    assert first_root.ledger_path != second_root.ledger_path
    assert not first_dir.exists(), "deriving a binding must not create the database"

    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=1)
    record = AuthorizationRecord(
        authorization_id="AUTH-root-1",
        user_text=USER_TEXT,
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=4,
        binding=current_binding(
            mode="m2-live-change",
            driver="acpx-dsh",
            project=project,
            request=real_request,
            spec_path=spec_path,
            root_binding=first_root,
        ),
        root_limits=limits,
    )

    verify_authorization(record, expected=record.binding)  # the ledger it names is accepted
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(
            record,
            expected=current_binding(
                mode="m2-live-change",
                driver="acpx-dsh",
                project=project,
                request=real_request,
                spec_path=spec_path,
                root_binding=second_root,
            ),
        )
    assert "root budget does not cover this run" in excinfo.value.message
    assert "ledger_path" in excinfo.value.message

    # A root artifact cannot be spent as a plain approval, and a plain approval cannot be spent
    # as a root run: either mistake would leave the consumption unaccounted for.
    plain = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=real_request,
        spec_path=spec_path,
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(record, expected=plain)
    assert "resolves no root" in excinfo.value.message
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(
            AuthorizationRecord(
                authorization_id="AUTH-plain-1",
                user_text=USER_TEXT,
                authorized_at="2026-09-20T00:00:00Z",
                max_top_level_submissions=2,
                binding=plain,
            ),
            expected=record.binding,
        )
    assert "carries no root budget binding" in excinfo.value.message

    # A root approval without a ceiling is refused at load: the allowance must be part of the
    # approval, never a default the build happens to use.
    with pytest.raises(Exception) as excinfo:
        AuthorizationRecord.model_validate(
            {
                **json.loads(record.model_dump_json()),
                "root_limits": None,
            }
        )
    assert "declares no root_limits" in str(excinfo.value)


def test_an_unchanged_legacy_record_keeps_one_ledger_row(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """The compatibility rule exists for the ledger: same digest, so a re-register is a no-op."""
    store = Store(tmp_path / "hflow.sqlite")
    authorization = _record(real_request, project, tmp_path / "task.json", max_submissions=1)
    try:
        store.register_authorization(authorization.as_store_record())
        store.claim_authorized_submission("AUTH-test-1")
        # Re-registering the identical artifact returns the existing row rather than a new one.
        again = store.register_authorization(authorization.as_store_record())
        assert again["used_top_level_submissions"] == 1
        with pytest.raises(StoreError):
            store.claim_authorized_submission("AUTH-test-1")
    finally:
        store.close()


def test_schema_rejects_any_provenance_other_than_user(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """``provided_by`` is a **value constraint**, not proof of authorship.

    It stops an artifact that *labels itself* as agent-authored. It cannot stop the executing
    agent from writing the bytes ``provided_by: "user"`` - nothing in this codebase can, because
    there is no issuer or protected store outside the executor's reach. The declared trust model
    is trusted-local, user-attested operation; see ``docs/m2-live-acceptance-result.md``.
    """
    path = tmp_path / "auth.json"
    document = _record(real_request, project, tmp_path / "task.json").model_dump(mode="json")
    document["provided_by"] = "agent"
    document["user_text"] = "the agent decided this was approved"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RefusedError) as excinfo:
        load_authorization(path)
    # The contract rejects the provenance outright: only "user" is a legal value. It surfaces as
    # an admission refusal naming the field, not as a pydantic traceback.
    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert "is not a valid authorization" in excinfo.value.message
    assert "provided_by" in excinfo.value.message


def test_a_forged_user_provenance_is_accepted_which_is_the_stated_trust_limit(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """The limit, asserted rather than implied: same-user forgery is not prevented.

    If this test ever starts failing because a real issuer exists, the trust model changed and
    the documentation must say so. Until then it documents exactly what is *not* protected.
    """
    path = tmp_path / "forged.json"
    document = _record(real_request, project, tmp_path / "task.json").model_dump(mode="json")
    document["user_text"] = "written by the executor, claiming to be the user"
    path.write_text(json.dumps(document), encoding="utf-8")

    record = load_authorization(path)  # accepted: provenance is only a field value
    binding = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=real_request,
        spec_path=tmp_path / "task.json",
    )
    verify_authorization(record, expected=binding)


def test_a_fresh_authorization_id_resets_allowance_which_is_also_a_trust_limit(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """Recorded so the cap is not over-read: it bounds one id, it does not bound a person."""
    store = Store(tmp_path / "hflow.sqlite")
    try:
        first = _record(real_request, project, tmp_path / "task.json", max_submissions=1)
        store.register_authorization(first.as_store_record())
        store.claim_authorized_submission(first.authorization_id)
        with pytest.raises(StoreError):
            store.claim_authorized_submission(first.authorization_id)

        # A different id - same user, same task, same everything else - has fresh allowance.
        second = first.model_copy(update={"authorization_id": "AUTH-test-2"})
        store.register_authorization(second.as_store_record())
        assert store.claim_authorized_submission("AUTH-test-2") == 1
    finally:
        store.close()


def test_authorization_for_a_different_task_is_refused(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    authorization = _record(real_request, project, tmp_path / "task.json")
    revised = task_spec.model_copy(update={"goal": "a different task entirely"})
    other_request = real_request.model_copy(update={"task": revised})
    other_binding = current_binding(
        mode="m2-live-change", driver="acpx-dsh", project=project, request=other_request, spec_path=tmp_path / "task2.json"
    )

    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(authorization, expected=other_binding)
    assert excinfo.value.code is RefusalCode.RISK_DOWNGRADE
    assert "spec_digest" in excinfo.value.message
    assert "spec_path" in excinfo.value.message


def test_authorization_for_another_mode_or_driver_is_refused(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    authorization = _record(real_request, project, tmp_path / "task.json", mode="stop-trial", driver="acpx-dsh")
    business = current_binding(
        mode="m2-live-change", driver="acpx-dsh", project=project, request=real_request, spec_path=tmp_path / "task.json"
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(authorization, expected=business)
    assert "mode" in excinfo.value.message, "a stop-trial approval must not cover a business task"

    other_driver = current_binding(
        mode="stop-trial", driver="some-other-harness", project=project, request=real_request, spec_path=tmp_path / "task.json"
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(authorization, expected=other_driver)
    assert "driver" in excinfo.value.message


def test_authorization_allowance_is_durable_and_exhausts(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """A restart, a new run id or a resubmission cannot restore allowance."""
    path = tmp_path / "hflow.sqlite"
    authorization = _record(real_request, project, tmp_path / "task.json", max_submissions=1)

    first = Store(path)
    first.register_authorization(authorization.as_store_record())
    assert first.claim_authorized_submission("AUTH-test-1") == 1
    with pytest.raises(StoreError) as excinfo:
        first.claim_authorized_submission("AUTH-test-1")
    assert "exhausted" in str(excinfo.value)
    first.close()

    # A fresh process reading the same database still sees the spent allowance.
    second = Store(path)
    try:
        state = second.authorization_state("AUTH-test-1")
        assert state is not None and state["used_top_level_submissions"] == 1
        with pytest.raises(StoreError):
            second.claim_authorized_submission("AUTH-test-1")
    finally:
        second.close()


def test_same_authorization_id_bound_elsewhere_is_refused(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    store = Store(tmp_path / "hflow.sqlite")
    authorization = _record(real_request, project, tmp_path / "task.json")
    other = authorization.model_copy(
        update={
            "binding": authorization.binding.model_copy(
                update={"base_commit": "0" * 40}
            )
        }
    )
    try:
        store.register_authorization(authorization.as_store_record())
        with pytest.raises(StoreError) as excinfo:
            store.register_authorization(other.as_store_record())
        assert "different target" in str(excinfo.value)
    finally:
        store.close()


def test_failed_preflight_refuses_before_consuming_allowance(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """A broken launch binding must not cost a submission."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(probe_ok=False)
    authorization = _record(real_request, project, tmp_path / "task.json")
    controller = _controller(store, tmp_path, driver, authorization, preflight_ok=False)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(real_request)
        assert excinfo.value.code is RefusalCode.NOT_IMPLEMENTED
        assert "preflight failed" in excinfo.value.message
        assert driver.invocations == [], "nothing may be dispatched after a failed preflight"
        state = store.authorization_state("AUTH-test-1")
        assert state is not None, "the artifact is recorded so the attempt is auditable"
        assert state["used_top_level_submissions"] == 0, "no allowance may be consumed"
        assert store.list_runs() == []
    finally:
        store.close()


def test_missing_authorization_file_is_a_refusal_not_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hflow.cli import EXIT_REFUSED, main

    code = main(
        [
            "run",
            "--task",
            str(tmp_path / "task.json"),
            "--project-root",
            str(tmp_path),
            "--driver",
            "acpx-dsh",
            "--data-dir",
            str(tmp_path / "data"),
            "--json",
        ]
    )
    assert code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["reason"] == "live_authorization_missing"
    assert "--authorization-file" in payload["detail"]
    assert not (tmp_path / "data" / "hflow.sqlite").exists(), "a refusal must not create state"


def test_bare_flag_no_longer_authorizes_anything(tmp_path: Path) -> None:
    """The removed `--live-authorized` flag must not exist in any form."""
    from hflow.cli import build_parser

    parser = build_parser()
    for action in parser._subparsers._group_actions[0].choices["run"]._actions:  # type: ignore[union-attr]
        assert action.dest != "live_authorized", "a model-writable flag must never authorize a real run"


# --------------------------------------------------------------------------
# the project contract and the dispatched roles are part of the approval
# --------------------------------------------------------------------------


def _configured_binding(project, task_spec, project_root: Path, spec_path: Path):  # noqa: ANN001, ANN202
    from hflow.contracts import EffectiveConfig

    request = RunRequest(
        task=task_spec, project=project, project_root=project_root, workspace_root=project_root
    )
    return current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=request,
        spec_path=spec_path,
        effective=EffectiveConfig(source="command_line"),
    )


def test_editing_the_project_contract_after_approval_refuses_the_artifact(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """The reproduced gap: a no-op check, a dropped write_deny or review floor kept the digest.

    Every edit below leaves the task, the profile and the repository unchanged; only the
    contract ``prepare`` showed differs. Each one must refuse the approval written for it.
    """
    from hflow.contracts import CheckDef

    spec_path = tmp_path / "task.json"
    shown = project.model_copy(
        update={
            "checks": [
                CheckDef(id="unit", kind="command", argv=["python", "-m", "pytest", "-q"]),
                CheckDef(id="docs-check", kind="fake"),
            ]
        }
    )
    approved = _configured_binding(shown, task_spec, project_root, spec_path)
    assert approved.project_contract_digest.startswith("sha256:")
    record = AuthorizationRecord(
        authorization_id="AUTH-contract-1",
        user_text=USER_TEXT,
        authorized_at="2026-10-03T00:00:00Z",
        max_top_level_submissions=2,
        binding=approved,
    )
    verify_authorization(record, expected=approved)  # the contract it names is accepted

    edits = {
        "check argv": {
            "checks": [
                CheckDef(id="unit", kind="command", argv=["python", "-c", "pass"]),
                CheckDef(id="docs-check", kind="fake"),
            ]
        },
        "write_deny": {"write_deny": []},
        "review_required": {"review_required": False},
    }
    for label, update in edits.items():
        edited = shown.model_copy(update=update)
        expected = _configured_binding(edited, task_spec, project_root, spec_path)
        assert expected.digest() != approved.digest(), label
        with pytest.raises(RefusedError) as excinfo:
            verify_authorization(record, expected=expected)
        assert excinfo.value.code is RefusalCode.RISK_DOWNGRADE, label
        assert "project contract changed since approval" in excinfo.value.message, label


def test_dropping_the_reviewer_after_approval_is_refused_by_roles(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """An approval shown a reviewer does not cover a run that skips it.

    With ``review_required`` off in the contract and the task not asking for a review, the run
    dispatches the implementer alone; the binding says so and the old artifact is refused for
    its roles, not only for its contract digest.
    """
    spec_path = tmp_path / "task.json"
    approved = _configured_binding(project, task_spec, project_root, spec_path)
    assert approved.roles == ["implementer", "reviewer"]
    record = AuthorizationRecord(
        authorization_id="AUTH-roles-1",
        user_text=USER_TEXT,
        authorized_at="2026-10-03T00:00:00Z",
        max_top_level_submissions=2,
        binding=approved,
    )
    no_review_task = task_spec.model_copy(update={"review": ReviewRequirement(required=False)})
    no_review_project = project.model_copy(update={"review_required": False})
    expected = _configured_binding(no_review_project, no_review_task, project_root, spec_path)
    assert expected.roles == ["implementer"]
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(record, expected=expected)
    assert "roles: authorized ['implementer', 'reviewer'] != actual ['implementer']" in (
        excinfo.value.message
    )


def test_an_artifact_without_a_contract_binding_cannot_authorize_a_configured_run(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """Like a pre-configuration artifact: it still loads, but it says nothing about the contract."""
    spec_path = tmp_path / "task.json"
    expected = _configured_binding(project, task_spec, project_root, spec_path)
    legacy = expected.model_copy(update={"project_contract_digest": ""})
    record = AuthorizationRecord(
        authorization_id="AUTH-legacy-contract-1",
        user_text=USER_TEXT,
        authorized_at="2026-10-03T00:00:00Z",
        max_top_level_submissions=2,
        binding=legacy,
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(record, expected=expected)
    assert "carries no project contract binding" in excinfo.value.message


def test_a_binding_without_a_resolved_configuration_keeps_its_legacy_shape(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """The historical tools pass no configuration; their bindings must digest as before."""
    binding = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=real_request,
        spec_path=tmp_path / "task.json",
    )
    assert binding.project_contract_digest == ""
    assert binding.roles == ["implementer", "reviewer"]
    document = binding.model_dump(mode="json")
    assert binding.digest() == digest_of({field: document[field] for field in LEGACY_BINDING_FIELDS})


def test_a_malformed_authorization_is_an_admission_refusal(tmp_path: Path) -> None:
    """Missing binding fields name the field; no pydantic traceback reaches the operator."""
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "authorization_id": "a",
                "user_text": "yes",
                "authorized_at": "now",
                "max_top_level_submissions": 1,
                "binding": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RefusedError) as excinfo:
        load_authorization(path)
    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert f"{path} is not a valid authorization" in excinfo.value.message
    assert "binding.mode" in excinfo.value.message
