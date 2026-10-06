"""An I/O failure after a valid terminal prefix cannot be reported as a drained stream."""

from __future__ import annotations

from pathlib import Path

import pytest

from hflow.contracts import InvocationOutcome, ModelApplied
from hflow.drivers import acpx_dsh as driver_module
from hflow.drivers.acpx_dsh import AcpxDshDriver

from .test_driver_acpx_dsh import DriverHarness, STAND_IN_OTHER_MODEL, _model_binding
from .test_driver_turn_settlement import _undrained_reviewer
from .test_model_session_binding import creation, setting, turn
from .test_output_boundaries import run_bytes as run_bytes, wire


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
@pytest.mark.parametrize("model", ["a", "native_profile"])
@pytest.mark.parametrize("failure_point", ["open", "read", "seek", "exit_tail_open"])
def test_a_valid_prompt_and_terminal_prefix_cannot_hide_a_later_io_failure(
    run_bytes, monkeypatch, role, model, failure_point,
):
    messages = creation() + setting() + [
        turn()[0],
        {"method": "session/update", "params": {"sessionId": "S", "update": {
            "sessionUpdate": "agent_message_chunk", "messageId": "M",
            "content": {"type": "text", "text": '{"verdict":"accepted","findings":[]}'},
        }}},
        turn()[1],
    ]
    state = {"terminal_read": False, "opens_after_terminal": 0, "failed": False}
    original_project = AcpxDshDriver._project_line
    original_open = Path.open

    def project_then_arm(self, invocation_id, sink, raw):
        original_project(self, invocation_id, sink, raw)
        if b'"stopReason"' in raw:
            # Force the complete ordering: the valid prompt/answer/terminal was consumed,
            # the writer exits, and only then can an ordinary read or exit-tail probe fail.
            self._processes[invocation_id].wait(timeout=10)
            state["terminal_read"] = True

    class FailingFile:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def seek(self, offset):
            if failure_point == "seek":
                state["failed"] = True
                raise OSError("injected seek failure after terminal prefix")
            return self.source.seek(offset)

        def read(self, size):
            state["failed"] = True
            raise OSError("injected read failure after terminal prefix")

    def fail_after_terminal(path, *args, **kwargs):
        if path.name == "stdout.ndjson" and state["terminal_read"] and args == ("rb",):
            state["opens_after_terminal"] += 1
            if failure_point == "exit_tail_open" and state["opens_after_terminal"] == 1:
                return original_open(path, *args, **kwargs)
            if failure_point in {"open", "exit_tail_open"}:
                state["failed"] = True
                raise PermissionError("injected open failure after terminal prefix")
            return FailingFile(original_open(path, *args, **kwargs))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(AcpxDshDriver, "_project_line", project_then_arm)
    monkeypatch.setattr(Path, "open", fail_after_terminal)
    driver, handle, result = run_bytes(
        b"".join(wire(message) for message in messages), model=model, role=role,
    )
    assert state["terminal_read"] and state["failed"], "the ordered I/O failure was not injected"
    assert driver._terminal_responses[handle.invocation_id][0].stop_reason == "end_turn"
    assert driver._prompt_request_ids[handle.invocation_id] == [2]
    assert driver._transcripts[handle.invocation_id].final_answer().text.startswith('{"verdict"')
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "reader_failed"
    assert result.agent_turns == 1
    assert result.review is None
    assert result.stream_order is None
    assert result.model_observation is None
    assert result.model_applied is (
        ModelApplied.UNKNOWN if model == "a" else ModelApplied.NOT_PASSED
    )
    assert any(note.startswith("reader_failed:") for note in result.limitations)
    assert not any(note.startswith("review_decoded:") for note in result.limitations)


def test_a_clean_complete_stream_still_reports_its_model_and_terminal_order(run_bytes):
    messages = creation() + setting() + turn()
    _driver, _handle, result = run_bytes(
        b"".join(wire(message) for message in messages), model="a",
    )
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.model_applied is ModelApplied.ACCEPTED
    assert result.model_observation.effective_value == "a"
    assert result.stream_order.prompt_response_line == len(messages) - 1


@pytest.mark.parametrize("requested", [False, True])
def test_timeout_after_a_valid_model_set_does_not_publish_final_model_metadata(tmp_path, requested):
    binding = _model_binding(STAND_IN_OTHER_MODEL) if requested else None
    harness = DriverHarness(tmp_path, "stubborn", binding=binding)
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    harness.driver.completion_timeout_seconds = 0.2
    handle, _request = harness.start()
    try:
        assert harness.wait_for_dispatch(handle)
        watch = harness.driver._model_watches[handle.invocation_id]
        assert watch.session_created
        if requested:
            assert watch.applied(rejected=False) is ModelApplied.ACCEPTED
        result = harness.driver.collect(handle)
        assert result.error_code == "completion_timeout"
        assert result.model_observation is None
        assert result.model_applied is (
            ModelApplied.UNKNOWN if requested else ModelApplied.NOT_PASSED
        )
    finally:
        harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_an_undrained_stream_does_not_publish_final_model_metadata(tmp_path, monkeypatch, role):
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.1)
    harness = DriverHarness(tmp_path, "cooperative", binding=_model_binding(STAND_IN_OTHER_MODEL))
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    handle = _undrained_reviewer(harness, "I-undrained-model", role=role)
    try:
        watch = harness.driver._model_watches[handle.invocation_id]
        assert watch.applied(rejected=False) is ModelApplied.ACCEPTED
        result = harness.driver.collect(handle)
        assert result.model_observation is None
        assert result.model_applied is ModelApplied.UNKNOWN
        assert result.stream_order is None
        assert result.review is None
    finally:
        harness.driver.release(handle.invocation_id)
