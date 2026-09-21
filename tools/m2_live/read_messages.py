"""Extract the reviewer's prose and the implementer's final message from the recorded stream.

Read-only: it parses the invocation's own stdout NDJSON. Used because the reviewer returned
prose instead of the contract's structured object, and "review rejected" is not a useful report
without saying what the reviewer objected to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / ".probe" / "m2-live" / "attempt-2-data" / "invocations"
IMPLEMENTER = "I-xf3sez1lho"
REVIEWER = "I-vc5pcccfog"


def text_of(invocation: str) -> str:
    path = DATA / invocation / "stdout.ndjson"
    chunks: list[str] = []
    tools: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        update = (message.get("params") or {}).get("update")
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            chunks.append(str(content.get("text", "")))
        elif kind == "tool_call":
            tools.append(str(update.get("title") or update.get("kind")))
    return "".join(chunks), "; ".join(tools[:12])


def main() -> int:
    for label, invocation in (("IMPLEMENTER", IMPLEMENTER), ("REVIEWER", REVIEWER)):
        text, tools = text_of(invocation)
        print(f"\n{'=' * 30} {label} ({invocation}) {'=' * 30}")
        print(f"tools used: {tools or '(none observed)'}")
        print(f"final message ({len(text)} chars):")
        print(text.strip()[-2500:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
