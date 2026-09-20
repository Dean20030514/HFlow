"""Driver contract notes.

The protocol itself lives in :mod:`hflow.contracts` (``HarnessDriver``) so that a
driver author has exactly one file to read. This module only holds the shared
error type and a structural conformance check used by tests, avoiding a second
copy of the interface.
"""

from __future__ import annotations

from typing import Any


class DriverProtocolError(RuntimeError):
    """A driver returned something the controller cannot interpret."""


REQUIRED_METHODS = ("probe", "start", "cancel", "reconcile")


def assert_driver_shape(driver: Any) -> None:
    """Cheap structural check: drivers are duck-typed, not registered."""
    missing = [name for name in REQUIRED_METHODS if not callable(getattr(driver, name, None))]
    if missing:
        raise DriverProtocolError(
            f"{type(driver).__name__} is missing driver methods: {', '.join(missing)}"
        )
