"""A run's owning controller exits - simulated, for tests that ``resume`` the run afterwards.

``hflow resume`` acts on a run whose owner this build recorded only once that owner is provably
gone: its lock can be taken **and** its identity reads ``gone`` (batch J extends this to the
reconcile of an ``outcome_unknown`` run - the block, a cross-process ``hflow cancel`` for one, does
not prove the controller and the agent it launched stopped). A test's owner lives in the test
process itself, which cannot exit, so the exit is simulated the way the owner-lease tests do it:
the controller releases its lock (``close``) and the run's recorded owner identity is replaced by
one that reads ``gone``.

* On Windows that identity is a short Python child's, read while it ran and then exited, and the
  real probe (``hflow.ownership.probe``, process handles) reads it ``gone``. Nothing is patched.
* Elsewhere this build reads no process identity at all - its probe answers ``unknown`` for every
  owner (documented in ``hflow.ownership``) - so no real process can stand in. The recorded
  identity is a synthetic one no process can have, and ``hflow.ownership.probe`` is patched
  through the test's own ``monkeypatch`` to read exactly that identity ``gone`` and to hand every
  other identity to the real probe. ``monkeypatch`` undoes it at the test's teardown, so it never
  reaches another test (an xdist worker is its own process and runs one test at a time).

Only the identity verdict is simulated off Windows; the lock half of the rule is the real one on
every platform. A test that asserts what the real probe observes (``identity=matching`` for a live
process, an access-denied read) is still ``windows_only``.

``successor`` is the controller of the process that resumes afterwards. It answers through the
owner's own driver objects, so a test can still see what the reconcile asked them; a real successor
process holds no handle to the owner's children (``hflow.cli._observer_controller``).
"""

from __future__ import annotations

import pytest

import hflow.ownership as ownership
from hflow.controller import Controller, assess_run_owner
from hflow.store import Store

windows_only = pytest.mark.skipif(
    not ownership.winjob.IS_WINDOWS, reason="the identity probe reads Windows process handles"
)

#: ``True``: a real exited child stands in for the owner (Windows). ``False``: the probe is
#: patched to read a synthetic identity ``gone`` (every other platform).
REAL_EXIT = ownership.winjob.IS_WINDOWS
#: Off Windows: a pid no process can have (Linux caps ``pid_max`` at 2**22, macOS lower) and a
#: fixed creation FILETIME (2022-06-18T04:26:40Z).
SIMULATED_PID = 2**22 + 1
SIMULATED_CREATED = 133_000_000_000_000_000


def owner_exits(
    store: Store, run_id: str, owner: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``owner`` is gone: its lock released, the run's recorded identity one that reads ``gone``."""
    owner.close()
    if REAL_EXIT:
        # Imported here: test_owner_lease imports test_batch_e_dispatch, which imports this module.
        from .test_owner_lease import _dead_identity

        dead = _dead_identity()
    else:
        dead = _simulated_dead_identity(monkeypatch)
    with store.transaction() as conn:
        updated = conn.execute(
            "UPDATE runs SET owner_pid = ?, owner_created = ?, owner_host = ?"
            " WHERE run_id = ? AND owner_token = ?",
            (dead.pid, dead.created, dead.host, run_id, owner.owner_token),
        ).rowcount
    assert updated == 1, f"run {run_id} is not owned by this controller"
    assert assess_run_owner(store, store.get_run(run_id)).gone


def _simulated_dead_identity(monkeypatch: pytest.MonkeyPatch) -> ownership.ProcessIdentity:
    """A synthetic identity the probe reads ``gone``, patched for this test only."""
    dead = ownership.ProcessIdentity(
        pid=SIMULATED_PID, created=SIMULATED_CREATED, host=ownership.current_host()
    )
    real = ownership.probe  # a probe an earlier call in this test patched stays in the chain

    def probe(identity: ownership.ProcessIdentity) -> ownership.Probe:
        if identity == dead:
            return ownership.Probe(
                "gone",
                f"pid {identity.pid} has exited (simulated by tests/owner_exit.py: this build "
                "reads no process identity off Windows)",
            )
        return real(identity)

    monkeypatch.setattr(ownership, "probe", probe)
    return dead


def successor(owner: Controller) -> Controller:
    """A controller with its own owner token, answering through ``owner``'s driver objects."""
    return Controller(
        owner.store,
        owner.driver,
        reviewer_driver=owner.reviewer_driver,
        controller_build=owner.controller_build,
        runners=owner.runners,
        controller_id="successor-process",
    )
