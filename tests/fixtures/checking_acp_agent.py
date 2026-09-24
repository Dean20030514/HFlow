"""An ACP agent that answers *according to the prompt it receives*.

Why this exists (plan 5.3): the older stubs answered a fixed verdict, so a controller that
sent an empty or incomplete prompt still looked healthy end to end. This stub makes the
prompt itself the input under test. It is given a list of required fragments; it reviews the
candidate as ``accepted`` only when **every** fragment is present in the prompt it actually
received, and answers ``changes_requested`` naming the absent ones otherwise. A test that
drops an acceptance criterion, the candidate identity, a check result or the output contract
from the renderer therefore fails loudly instead of passing on a hard-coded verdict.

It is a program, not a model: no provider, no credential, no network. The fragments are
supplied as a JSON file by the test, so the stub stays a fixed program.

Invoked as: ``python checking_acp_agent.py --task-file - --inputs <file>`` with the ACP prompt
message on stdin. ``CHECKING_AGENT_PROMPT_DIR`` makes it also record, per role, the prompt it
received and which required fragments it did not find.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

#: Which section of the packet a fragment belongs to. The names are the packet's own section
#: headings, so a failure message points at the missing field rather than at a byte range.
SECTIONS = (
    "task",
    "acceptance",
    "scope",
    "candidate",
    "evidence",
    "output_contract",
    "reviewer_rules",
    "permission",
)

IMPLEMENTER_MARKER = "[HFlow implementer task]"
REVIEWER_MARKER = "[HFlow reviewer task]"


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


PROMPT_WAIT_SECONDS = 30.0
AGENT_NAME = "hflow-checking-acp"
AGENT_VERSION = "0.1.0"


def _respond(request_id: object, result: dict) -> None:
    emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def read_prompt_message() -> dict:
    """The ACP prompt message, answering the client's handshake on the way.

    Reading ``stdin.read()`` (until EOF) does *not* work through the real pinned client: it holds
    the agent's stdin open after sending the prompt, so an EOF-waiting agent never answers and the
    client gives up with "agent exited before initialize completed".

    Nor can the prompt be assumed to be the first message. The real client speaks first and in
    order - ``initialize``, then ``session/new``, then ``session/prompt`` - and waits for each
    result before it continues, so a stub that only looked for the prompt would deadlock the
    client after the first message. The handshake is therefore answered here, and every message
    the stub does not implement is refused with ``-32601`` rather than ignored.
    """
    deadline = time.monotonic() + PROMPT_WAIT_SECONDS
    session_id = f"checking-session-{os.getpid()}"
    while time.monotonic() < deadline:
        line = sys.stdin.readline()
        if not line:
            # EOF: the client closed stdin. Nothing further will arrive.
            return {}
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        if method == "session/prompt":
            return message
        if method == "initialize":
            _respond(
                message.get("id"),
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": False,
                        },
                    },
                    "agentInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
                },
            )
            continue
        if method == "session/new":
            _respond(message.get("id"), {"sessionId": session_id, "configOptions": []})
            continue
        if method is None and message.get("id") is not None:
            # A response to something this stub asked; it asks nothing, so there is nothing to do.
            continue
        if message.get("id") is not None:
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
    return {}


def read_prompt(message: dict) -> str:
    """The prompt text of one ACP ``session/prompt`` message."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    blocks = params.get("prompt") or []
    return "\n".join(
        str(block.get("text", "")) for block in blocks if isinstance(block, dict)
    )


def load_inputs(path: Path | None, role: str) -> dict[str, list[str]]:
    """Required fragments for one role.

    The file may either be a flat ``section -> [text]`` map (applied to both roles) or name the
    roles explicitly as ``{"common": {...}, "implementer": {...}, "reviewer": {...}}``. Role
    separation matters: an implementer packet legitimately has no candidate identity or check
    results, so applying the reviewer's expectations to it would make every run fail for the
    wrong reason.

    A role's own group is used alone; only ``common`` is merged into it.
    """
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("checking agent: --inputs must be a JSON object")
    if any(key in data for key in ("common", "implementer", "reviewer")):
        merged: dict[str, list[str]] = {}
        for group in ("common", role):
            section_map = data.get(group) or {}
            if not isinstance(section_map, dict):
                raise SystemExit(f"checking agent: --inputs.{group} must be an object")
            for section, needles in section_map.items():
                merged.setdefault(str(section), []).extend(str(needle) for needle in needles)
        return merged
    return {
        str(section): [str(needle) for needle in needles] for section, needles in data.items()
    }


def missing_sections(prompt: str, required: dict[str, list[str]]) -> dict[str, list[str]]:
    """Which required fragments are absent from the prompt that actually arrived.

    The key of each entry labels the field group; the values are the literal texts that group
    must contain. Only the texts are checked, never the label.
    """
    absent: dict[str, list[str]] = {}
    for section, needles in required.items():
        gone = [needle for needle in needles if needle and needle not in prompt]
        if gone:
            absent[section] = gone
    return absent


def emit_chunk(session_id: str, text: str, message_id: str) -> None:
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": message_id,
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def implementer_turn(session_id: str, prompt: str, absent: dict[str, list[str]]) -> int:
    """An implementer answers by doing the scoped edit, or by reporting what it lacked."""
    scratch = Path(os.environ.get("STUB_SCRATCH_DIR", "."))
    scratch.mkdir(parents=True, exist_ok=True)
    if absent and os.environ.get("STUB_IMPLEMENTER_MODE", "check") != "skip":
        # Never guess a scope. Without the packet the stub does nothing and says why, which is
        # exactly the behaviour an implementer should have when its instructions are missing.
        report = scratch / "implementer-refused.json"
        report.write_text(json.dumps({"absent": sorted(absent)}, indent=2), encoding="utf-8")
        emit_chunk(session_id, f"I cannot start: the task packet is incomplete ({sorted(absent)}).", "m-1")
        emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        return 0

    relative = os.environ.get("STUB_IMPLEMENTER_PATH", "")
    if relative:
        target = Path(os.getcwd()) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "def parse(text):\n    if not text:\n        return None\n    return text\n",
            encoding="utf-8",
        )
    emit_chunk(session_id, f"Applied the change inside the allowed write path ({relative}).", "m-2")
    emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
    return 0


def reviewer_turn(session_id: str, prompt: str, absent: dict[str, list[str]]) -> int:
    """A reviewer votes on the *input it received*, not on a fixed answer."""
    scratch = Path(os.environ.get("STUB_SCRATCH_DIR", "."))
    scratch.mkdir(parents=True, exist_ok=True)
    findings = [
        {
            "id": section,
            "status": "missing_input",
            "detail": "the review packet did not contain: " + "; ".join(needles),
        }
        for section, needles in sorted(absent.items())
    ]
    verdict = "changes_requested" if absent else "accepted"
    # The decision is written down so a test can tell "the reviewer refused" apart from
    # "the wire lost the verdict" without guessing from the exit code.
    (scratch / "reviewer-input-check.json").write_text(
        json.dumps(
            {"verdict": verdict, "absent": sorted(absent), "prompt_bytes": len(prompt.encode("utf-8"))},
            indent=2,
        ),
        encoding="utf-8",
    )
    document = json.dumps({"verdict": verdict, "findings": findings}, indent=2)
    answer = (
        "I reviewed the frozen candidate against the packet I received.\n\n"
        f"```json\n{document}\n```\n"
    )
    # Delivered in two chunks, like the recorded runtime: reassembly stays exercised.
    cut = len(answer) // 2
    emit_chunk(session_id, answer[:cut], "m-3")
    emit_chunk(session_id, answer[cut:], "m-3")
    emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
    return 0


def record_prompt(
    directory: Path | None,
    role: str,
    prompt: str,
    absent: dict[str, list[str]],
    *,
    inputs_path: Path | None = None,
    required: dict[str, list[str]] | None = None,
) -> None:
    """Write what this invocation received, so a test can assert on the real prompt.

    One file per role: the same agent program serves both invocations, so a single shared log
    would only ever show the last writer. ``CHECKING_AGENT_PROMPT_DIR`` points here; a missing
    directory is reported rather than silently skipped, because a test that asserts on an absent
    prompt would otherwise pass for the wrong reason.
    """
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{role}.json").write_text(
        json.dumps(
            {
                "role": role,
                "prompt": prompt,
                "prompt_bytes": len(prompt.encode("utf-8")),
                "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "absent": absent,
                "required": required,
                "input_file": str(inputs_path) if inputs_path else "",
                "input_digest": hashlib.sha256(inputs_path.read_bytes()).hexdigest()
                if inputs_path and inputs_path.is_file()
                else "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accepted and ignored: the client passes the task body on stdin (`-f -`).
    parser.add_argument("-f", "--file", dest="task_file", default=None)
    parser.add_argument("--task-file", dest="task_file_alt", default=None)
    parser.add_argument("--inputs", default=None, help="JSON file of required prompt fragments")
    args, _unknown = parser.parse_known_args(argv)

    # A marker on the very first thing that happens: when a client hangs, this is what tells
    # "the agent never ran" apart from "the agent ran and read no prompt". It lives with the
    # other scratch files, never in the workspace under test.
    scratch = Path(os.environ.get("STUB_SCRATCH_DIR", "."))
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / f"agent-started-{os.getpid()}.txt").write_text("started\n", encoding="utf-8")

    prompt_message = read_prompt_message()
    prompt = read_prompt(prompt_message)
    session_id = str(
        (prompt_message.get("params") or {}).get("sessionId") or f"checking-{os.getpid()}"
    )
    role = "reviewer" if REVIEWER_MARKER in prompt else "implementer"
    inputs_path = Path(args.inputs) if args.inputs else None
    required = load_inputs(inputs_path, role)
    absent = missing_sections(prompt, required)
    directory = os.environ.get("CHECKING_AGENT_PROMPT_DIR")
    record_prompt(
        Path(directory) if directory else None,
        role,
        prompt,
        absent,
        inputs_path=inputs_path,
        required=required,
    )
    # Diagnostics on stderr only (stdout carries ACP traffic): this is how a failing case shows
    # *why* the stub voted the way it did.
    print(
        f"checking-agent: pid={os.getpid()} role={role} inputs={args.inputs!r} "
        f"sections={sorted(required)} prompt_bytes={len(prompt.encode('utf-8'))} "
        f"absent_keys={sorted(absent)}",
        file=sys.stderr,
        flush=True,
    )

    if role == "reviewer":
        return reviewer_turn(session_id, prompt, absent)
    if IMPLEMENTER_MARKER in prompt:
        return implementer_turn(session_id, prompt, absent)
    emit_chunk(session_id, "The prompt is not an HFlow role packet.", "m-0")
    emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
