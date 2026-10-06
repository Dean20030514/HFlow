"""Raw capture and protocol-state bounds, using a credential-free byte-emitting client."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from hflow.contracts import AgentBinding, InvocationOutcome, InvocationRequest, ModelApplied
from hflow.drivers import acpx_dsh as module
from hflow.drivers.acpx_dsh import AcpxDshDriver, DRIVER_ID


def wire(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def prompt(request_id: int = 2) -> bytes:
    return wire({"id": request_id, "method": "session/prompt", "params": {"sessionId": "S"}})


def settled(request_id: int = 2) -> bytes:
    return wire({"id": request_id, "result": {"stopReason": "end_turn"}})


@pytest.fixture
def run_bytes(tmp_path: Path):
    drivers = []

    def run(payload: bytes, *, budget: int = 8 * 1024 * 1024, records: int = 20000,
            model: str = "native_profile", role: str = "implementer"):
        root = tmp_path / str(len(drivers))
        root.mkdir()
        payload_file = root / "payload.bin"
        payload_file.write_bytes(payload)
        client = root / "client.py"
        client.write_text(
            "import sys\nfrom pathlib import Path\nsys.stdin.buffer.read()\n"
            f"sys.stdout.buffer.write(Path({str(payload_file)!r}).read_bytes())\n"
            "sys.stdout.buffer.flush()\n", encoding="utf-8",
        )
        workspace = root / "workspace"
        workspace.mkdir()
        driver = AcpxDshDriver(
            data_dir=root / "data", acpx_cli=client, python_executable=sys.executable,
            agent_argv_override=[sys.executable, "-c", "pass"],
            binding=AgentBinding(harness="dsh", driver=DRIVER_ID, model_selection=model),
            max_raw_log_bytes=budget,
        )
        driver.max_event_records = records
        drivers.append(driver)
        request = InvocationRequest(
            invocation_id="I-1", attempt_id="A-1", run_id="R-1", role=role,
            task_id="T-1", task_revision=1, goal="offline byte capture", acceptance=[],
            write_allow=[], write_deny=[], workspace=str(workspace), deadline_seconds=20,
            spec_digest="sha256:test", data_dir=str(root / "data"),
        )
        handle = driver.start_handle(request)
        return driver, handle, driver.collect(handle)

    yield run
    for driver in drivers:
        for invocation_id in driver._handles:
            driver.release(invocation_id)


@pytest.mark.parametrize("suffix", [b"\n", b""])
def test_capture_preserves_invalid_utf8_blank_lines_and_eof(run_bytes, suffix):
    payload = b"\n\r\n" + prompt() + b'\xff\xfeinvalid\n\n' + settled().rstrip(b"\n") + suffix
    driver, handle, result = run_bytes(payload)
    capture = driver._stdout_captures[handle.invocation_id]
    assert Path(handle.event_log).read_bytes() == payload
    assert capture.total_bytes == capture.retained_bytes == len(payload)
    assert capture.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert not capture.truncated
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"


def test_blank_lines_are_raw_bytes_but_not_stream_ordinals(run_bytes):
    payload = b"\n\n" + prompt() + b"\n" + settled().rstrip(b"\n")
    driver, handle, result = run_bytes(payload)
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.stream_order is not None
    assert result.stream_order.prompt_response_line == 1
    assert driver._line_counts[handle.invocation_id] == 2
    assert Path(handle.event_log).read_bytes() == payload


def test_invalid_utf8_inside_json_is_not_repaired_into_a_completion(run_bytes):
    payload = prompt() + b'{"id":2,"result":{"stopReason":"end_turn","_meta":{"note":"\xff"}}}\n'
    driver, handle, result = run_bytes(payload)
    assert Path(handle.event_log).read_bytes() == payload
    assert driver._stdout_captures[handle.invocation_id].total_bytes == len(payload)
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"
    assert driver._terminal_responses[handle.invocation_id] == []


@pytest.mark.parametrize("newline", [True, False])
def test_oversized_line_counts_every_byte_and_discards_its_fake_json_tail(
    run_bytes, monkeypatch, newline,
):
    monkeypatch.setattr(module, "MAX_PENDING_LINE_BYTES", 128)
    # The fake prompt begins at the next read boundary. Resetting an oversized pending
    # buffer used to parse this tail as a fresh prompt, despite there being no newline.
    fake_tail = prompt().rstrip(b"\n")
    payload = b"x" * module.CAPTURE_READ_CHUNK + fake_tail
    if newline:
        payload += b"\n" + settled()
    driver, handle, result = run_bytes(payload)
    capture = driver._stdout_captures[handle.invocation_id]
    assert capture.total_bytes == len(payload)
    assert Path(handle.event_log).read_bytes() == payload
    assert capture.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert driver._oversized[handle.invocation_id] == 1
    assert not handle.dispatched, "the tail of a discarded line cannot be a dispatch"
    assert result.error_code == "unparseable_output"


def test_discarded_oversized_line_recovers_at_the_next_newline(run_bytes, monkeypatch):
    monkeypatch.setattr(module, "MAX_PENDING_LINE_BYTES", 128)
    driver, handle, result = run_bytes(b"x" * 300 + b"\n" + prompt() + settled())
    assert handle.dispatched
    assert driver._prompt_request_ids[handle.invocation_id] == [2]
    assert result.error_code == "unparseable_output"


def test_oversized_unterminated_output_is_counted_even_after_byte_exhaustion(run_bytes, monkeypatch):
    monkeypatch.setattr(module, "MAX_PENDING_LINE_BYTES", 128)
    payload = b"x" * (module.CAPTURE_READ_CHUNK * 3 + 7)
    driver, handle, result = run_bytes(payload, budget=256)
    capture = driver._stdout_captures[handle.invocation_id]
    head = payload[:driver.protocol_share_bytes]
    assert capture.total_bytes == len(payload)
    assert Path(handle.event_log).read_bytes() == head
    assert capture.digest == "sha256:" + hashlib.sha256(head).hexdigest()
    assert driver._oversized[handle.invocation_id] == 1
    assert driver.raw_lines(handle.invocation_id) == []
    assert result.error_code == "output_limit_exceeded"
    assert result.agent_turns is None


def test_deep_json_after_exhaustion_cannot_stop_drain_or_hide_later_dispatch(run_bytes):
    deep_json = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
    payload = b"{}\n" * 1000 + deep_json + prompt() + settled()
    driver, handle, result = run_bytes(payload, budget=256)
    assert driver._stdout_captures[handle.invocation_id].total_bytes == len(payload)
    assert handle.invocation_id not in driver._reader_failures
    assert handle.dispatched
    assert result.error_code == "output_limit_exceeded"
    assert result.agent_turns == 1


@pytest.mark.parametrize("observed_dispatch", [True, False])
@pytest.mark.parametrize("requested_model", ["native_profile", "stub-model"])
def test_byte_limit_freezes_metadata_but_keeps_dispatch_observation(
    run_bytes, observed_dispatch, requested_model,
):
    # Fill the first chunk with complete records; later terminal, model and prompt
    # records must not mutate state, even when they could produce a plausible result.
    payload = b"{}\n" * 1000
    if observed_dispatch:
        payload += prompt()
    payload += wire({"id": 2, "result": {"stopReason": "end_turn"}})
    payload += wire({"method": "session/update", "params": {"sessionId": "S", "update": {
        "sessionUpdate": "config_option_update", "configOptions": [{
            "id": "model", "category": "model", "currentValue": requested_model,
        }],
    }}})
    driver, handle, result = run_bytes(payload, budget=256, model=requested_model)
    invocation_id = handle.invocation_id
    capture = driver._stdout_captures[invocation_id]
    head = payload[:driver.protocol_share_bytes]
    assert capture.total_bytes == len(payload)
    assert capture.retained_bytes == len(head)
    assert Path(handle.event_log).read_bytes() == head
    assert capture.digest == "sha256:" + hashlib.sha256(head).hexdigest()
    assert capture.truncated
    assert driver._terminal_responses[invocation_id] == []
    assert driver._prompt_request_ids[invocation_id] == []
    assert driver._model_watches[invocation_id].changes == []
    assert len(driver.raw_lines(invocation_id)) == driver.protocol_share_bytes // 3
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "output_limit_exceeded"
    assert "protocol_byte_limit" in result.error_message
    assert result.agent_turns == (1 if observed_dispatch else None)
    assert result.model_observation is None
    assert result.model_applied is (
        ModelApplied.NOT_PASSED if requested_model == "native_profile" else ModelApplied.UNKNOWN
    )
    assert result.stream_order is None


@pytest.mark.parametrize("neutral_events", [False, True])
def test_all_wire_records_share_the_record_budget(run_bytes, neutral_events):
    # Benign result records have no neutral events; terminal records do. Both must
    # exhaust the same record allowance before tail metadata can change.
    first = settled(123) if neutral_events else wire({"id": 123, "result": {}})
    payload = first * 20001 + prompt() + settled()
    driver, handle, result = run_bytes(payload)
    invocation_id = handle.invocation_id
    assert len(driver._events[invocation_id]) == (20000 if neutral_events else 0)
    assert len(driver._terminal_responses[invocation_id]) == (20000 if neutral_events else 0)
    assert driver._line_counts[invocation_id] == 20003
    assert len(driver.raw_lines(invocation_id)) == module.MAX_BUFFERED_LINES
    assert driver._prompt_request_ids[invocation_id] == []
    assert handle.dispatched, "dispatch remains observable after state stops growing"
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "output_limit_exceeded"
    assert "protocol_record_limit" in result.error_message
    assert "protocol_byte_limit" not in result.error_message
    assert result.agent_turns == 1
    assert result.stream_order is result.model_observation is None
    assert not driver._stdout_captures[invocation_id].truncated


def test_record_limit_freezes_prompt_error_model_and_transcript_containers(run_bytes):
    messages = [
        wire({"id": 10, "method": "session/new", "params": {}}),
        wire({"id": 10, "result": {"sessionId": "S"}}),
        prompt(1),
        wire({"id": 77, "method": "session/set_config_option",
              "params": {"sessionId": "S", "configId": "model"}}),
        wire({"id": 1, "error": {"code": -1, "message": "first error"}}),
        wire({"method": "session/update", "params": {"sessionId": "S", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "first"},
        }}}),
    ]
    tail = b"".join([
        prompt(2),
        wire({"id": 88, "method": "session/set_config_option",
              "params": {"sessionId": "S", "configId": "model"}}),
        wire({"id": 2, "error": {"code": -2, "message": "tail error"}}),
        wire({"method": "session/update", "params": {"sessionId": "S", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "tail"},
        }}}),
        settled(2),
    ])
    driver, handle, result = run_bytes(b"".join(messages) + tail * 100, records=6, role="reviewer")
    invocation_id = handle.invocation_id
    assert driver._prompt_request_ids[invocation_id] == [1]
    assert len(driver._prompt_errors[invocation_id]) == 1
    assert len(driver._model_watches[invocation_id]._pending) == 1
    assert driver._model_watches[invocation_id].set_requests == 1
    assert driver._prompt_session_updates[invocation_id] == {"S": [1, 1]}
    assert driver._transcripts[invocation_id].final_answer().text == "first"
    assert len(driver.raw_lines(invocation_id)) == 6
    assert driver._terminal_responses[invocation_id] == []
    assert result.error_code == "output_limit_exceeded"
    assert result.review is None
