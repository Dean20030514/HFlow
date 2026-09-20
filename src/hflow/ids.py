"""Deterministic identity helpers: one place that mints IDs and timestamps.

ID prefixes are part of the readable contract (`R-...` runs, `A-...` attempts,
`E-...` evidence) so a log line or receipt is traceable without a lookup table.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def utc_now() -> str:
    """Second-precision UTC timestamp: stable enough to store and diff in tests."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str, length: int = 10) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}"


def new_run_id() -> str:
    return new_id("R")


def new_attempt_id() -> str:
    return new_id("A")


def new_evidence_id() -> str:
    return new_id("E")


def new_invocation_id() -> str:
    return new_id("I")


def new_reservation_id() -> str:
    return new_id("B")
