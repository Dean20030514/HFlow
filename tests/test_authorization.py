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
    RunRequest,
    TaskState,
)
from hflow.controller import Controller
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

USER_TEXT = "I approve one real M2 run on this exact task, driver and base commit."


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
    # implementer dispatch and the ledger record, so the project does not require review.
    no_review = project.model_copy(update={"review_required": False})
    controller = _controller(store, tmp_path, driver, authorization)
    try:
        outcome = controller.run_task(real_request.model_copy(update={"project": no_review}))

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


def test_an_agent_written_note_cannot_authorize_a_real_run(
    tmp_path: Path, project, task_spec, project_root: Path, real_request: RunRequest
) -> None:
    """The failure mode this gate exists for: the runner authorizing itself."""
    path = tmp_path / "auth.json"
    document = _record(real_request, project, tmp_path / "task.json").model_dump(mode="json")
    document["provided_by"] = "agent"
    document["user_text"] = "the agent decided this was approved"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        load_authorization(path)
    # pydantic rejects the provenance outright: only "user" is a legal value.
    assert "provided_by" in str(excinfo.value) or "agent" in str(excinfo.value)


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
