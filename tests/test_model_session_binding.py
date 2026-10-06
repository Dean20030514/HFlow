"""A model observation belongs to the created session and its first prompt."""

from __future__ import annotations

import pytest

from hflow.contracts import InvocationOutcome, ModelApplied
from hflow.drivers.acpx_dsh import AcpxDshDriver, _ModelWatch

from .test_output_boundaries import run_bytes as run_bytes, wire


def options(model="b", thought="low"):
    return [
        {"id": "model", "category": "model", "type": "select", "currentValue": model,
         "options": [{"value": "a"}, {"value": "b"}]},
        {"id": "reasoning_effort", "category": "thought_level", "type": "select",
         "currentValue": thought, "options": [{"value": "low"}, {"value": "high"}]},
    ]


def creation(session="S", model="b", *, request_id=1):
    return [
        {"id": request_id, "method": "session/new", "params": {}},
        {"id": request_id, "result": {"sessionId": session, "configOptions": options(model)}},
    ]


def update(session="S", model="a", thought="high"):
    params = {"update": {"sessionUpdate": "config_option_update",
                         "configOptions": options(model, thought)}}
    if session is not None:
        params["sessionId"] = session
    return {"method": "session/update", "params": params}


def setting(session="S", request_id=3, model="a"):
    params = {"configId": "model", "value": model}
    if session is not None:
        params["sessionId"] = session
    return [
        {"id": request_id, "method": "session/set_config_option", "params": params},
        {"id": request_id, "result": {"configOptions": options(model, "high")}},
    ]


def turn(session="S"):
    params = {} if session is None else {"sessionId": session}
    return [
        {"id": 2, "method": "session/prompt", "params": params},
        {"id": 2, "result": {"stopReason": "end_turn"}},
    ]


def run_messages(run_bytes, messages, *, model="a"):
    return run_bytes(b"".join(wire(message) for message in messages), model=model)


@pytest.mark.parametrize("other_session", ["other", None, "", 7])
def test_another_or_missing_session_cannot_upgrade_the_model_or_reasoning(
    run_bytes, other_session,
):
    # The genuine session started on b. An unrelated session advertises a and high,
    # then the genuine session's prompt settles; that entire ordered stream is collected.
    driver, handle, result = run_messages(
        run_bytes, creation() + [update(other_session)] + turn(),
    )
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.model_applied is ModelApplied.UNKNOWN
    observation = result.model_observation
    assert observation.initial_value == observation.effective_value == "b"
    assert observation.thought_level == "low"
    assert observation.changes == []
    assert driver._model_watches[handle.invocation_id].skipped_updates == 1
    assert any(note.startswith("model_other_session:") for note in result.limitations)


@pytest.mark.parametrize("other_session", ["other", None, "", 7])
def test_a_set_for_another_or_missing_session_does_not_consume_a_model_response(
    run_bytes, other_session,
):
    driver, handle, result = run_messages(
        run_bytes, creation() + setting(other_session) + turn(),
    )
    watch = driver._model_watches[handle.invocation_id]
    assert watch.set_requests == 0 and watch.set_results == []
    assert result.model_applied is ModelApplied.UNKNOWN
    assert result.model_observation.effective_value == "b"
    assert result.model_observation.thought_level == "low"


@pytest.mark.parametrize("prompt_session", ["other", None, ""])
def test_the_first_prompt_must_name_the_session_whose_configuration_was_read(
    run_bytes, prompt_session,
):
    _driver, _handle, result = run_messages(
        run_bytes, creation() + setting() + turn(prompt_session),
    )
    assert result.model_applied is ModelApplied.UNKNOWN
    assert any(note.startswith("model_binding_unknown:") for note in result.limitations)


@pytest.mark.parametrize("session", [None, "", 7])
def test_a_new_response_without_a_session_cannot_establish_model_acceptance(run_bytes, session):
    _driver, _handle, result = run_messages(
        run_bytes, creation(session, "a") + setting() + turn(),
    )
    assert result.model_applied is ModelApplied.UNKNOWN
    assert result.model_observation.advertised is False
    assert result.model_observation.effective_value is None


def test_only_the_first_effective_new_session_supplies_configuration(run_bytes):
    _driver, _handle, result = run_messages(
        run_bytes, creation("S", "b") + creation("other", "a", request_id=9) + turn(),
    )
    assert result.model_applied is ModelApplied.UNKNOWN
    assert result.model_observation.initial_value == result.model_observation.effective_value == "b"


@pytest.mark.parametrize("peer_first", [False, True])
@pytest.mark.parametrize("collision_target", ["new", "set"])
def test_request_id_collision_cannot_attribute_model_acceptance(
    run_bytes, peer_first, collision_target,
):
    target = creation("S", "a") if collision_target == "new" else setting()
    peer = {"id": target[0]["id"], "method": "session/request_permission",
            "params": {"sessionId": "S"}}
    messages = ([peer] + target) if peer_first else (target + [peer])
    if collision_target == "set":
        messages = creation() + messages
    _driver, _handle, result = run_messages(run_bytes, messages + turn())
    assert result.model_applied is ModelApplied.UNKNOWN
    assert any(note.startswith("model_binding_unknown:") for note in result.limitations)


def test_typed_request_ids_do_not_create_a_false_collision(run_bytes):
    messages = creation() + [
        {"id": "3", "method": "session/request_permission", "params": {"sessionId": "S"}},
    ] + setting(request_id=3) + turn()
    _driver, _handle, result = run_messages(run_bytes, messages)
    assert result.model_applied is ModelApplied.ACCEPTED


def test_a_valid_set_changes_an_initially_different_model_to_the_requested_one(run_bytes):
    _driver, _handle, result = run_messages(run_bytes, creation() + setting() + turn())
    assert result.model_observation.initial_value == "b"
    assert result.model_observation.effective_value == "a"
    assert result.model_applied is ModelApplied.ACCEPTED


def test_updates_without_a_set_cannot_turn_a_different_initial_model_into_passed(run_bytes):
    _driver, _handle, result = run_messages(run_bytes, creation() + [update()] + turn())
    assert result.model_observation.effective_value == "a"
    assert result.model_applied is ModelApplied.UNKNOWN


def test_a_matching_initial_and_final_model_without_a_set_is_passed(run_bytes):
    _driver, _handle, result = run_messages(run_bytes, creation(model="a") + turn())
    assert result.model_applied is ModelApplied.PASSED


def test_same_session_updates_after_the_response_keep_the_whole_stream_observation(run_bytes):
    _driver, _handle, result = run_messages(
        run_bytes, creation() + setting() + turn() + [update("S", "b", "high")],
    )
    assert result.model_observation.effective_value == "b"
    assert result.model_observation.thought_level == "high"
    assert result.model_applied is ModelApplied.UNKNOWN
    assert result.stream_order.updates_after_prompt_response == 1


def test_another_sessions_trailing_configuration_does_not_change_an_accepted_model(run_bytes):
    _driver, _handle, result = run_messages(
        run_bytes, creation() + setting() + turn() + [update("other", "b", "low")],
    )
    assert result.model_applied is ModelApplied.ACCEPTED
    assert result.model_observation.effective_value == "a"
    assert result.model_observation.thought_level == "high"
    assert result.stream_order.updates_after_prompt_response == 0


def test_a_collision_cannot_be_classified_as_a_pre_prompt_model_rejection():
    watch = _ModelWatch("a")
    for message in creation():
        watch.observe(message)
    request = setting()[0]
    watch.observe(request)
    watch.observe({"id": 3, "method": "session/request_permission", "params": {}})
    watch.observe({"id": 3, "error": {"code": -1, "message": "denied"}})
    assert watch.refused() is False
    assert watch.applied(rejected=False) is ModelApplied.UNKNOWN


def test_a_foreign_response_during_a_real_set_cannot_consume_the_pending_change(
    run_bytes, monkeypatch,
):
    own_request, own_response = setting(request_id=3)
    foreign_request, foreign_response = setting("other", request_id=4)
    messages = creation() + [
        own_request, foreign_request, foreign_response, own_response,
    ] + turn()
    checkpoints = []
    original_note = AcpxDshDriver._note_message

    def record_checkpoint(self, invocation_id, message, *, line_index=-1):
        original_note(self, invocation_id, message, line_index=line_index)
        if message == own_request or message == foreign_response or message == own_response:
            watch = self._model_watches[invocation_id]
            checkpoints.append((watch.applied(rejected=False), watch.effective_value))

    monkeypatch.setattr(AcpxDshDriver, "_note_message", record_checkpoint)
    _driver, _handle, result = run_messages(run_bytes, messages)
    # The foreign answer must neither upgrade the in-flight change nor lose its real answer.
    # These checkpoints are taken while consuming the same stream later folded by collect.
    assert checkpoints == [
        (ModelApplied.UNKNOWN, "b"),
        (ModelApplied.UNKNOWN, "b"),
        (ModelApplied.ACCEPTED, "a"),
    ]
    assert result.model_applied is ModelApplied.ACCEPTED
    assert result.model_observation.effective_value == "a"
