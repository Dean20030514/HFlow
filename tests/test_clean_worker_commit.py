"""`clean` never drops the commit of a run that was refused after a worker committed.

The controller deliberately writes no candidate ref for a worker that moved HEAD: the commit is
refused, not delivered. It is still the only trace of what the worker did, and the scope-violation
guidance tells the operator to inspect it, so releasing the worktree must not quietly make it
unreachable. The CLI-level cases (base HEAD allowed, a branch makes the clean allowed) live in
``test_cli_m2_cleanup.py``; this one drives the real worker-commit path through the controller.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hflow.cleanup import apply_cleanup, plan_cleanup
from hflow.contracts import RefusalCode, TaskState
from hflow.store import Store
from hflow.verify import CheckRunners
from tests.test_batch_e_repair import (
    _WORKER_COMMITS_OUT_OF_SCOPE,
    FIXED_SOURCE,
    CommittingDriver,
    FailingOnceThenPassing,
    _controller,
    _git,
    _project,
    _request,
    _scoped_spec,
    scoped_repo,  # noqa: F401 - pytest fixture
)


def test_a_worker_commit_refused_as_a_scope_violation_is_not_dropped_by_clean(
    tmp_path: Path, scoped_repo: Path  # noqa: F811 - pytest fixture
) -> None:
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    driver = CommittingDriver(
        scoped_repo,
        base=base,
        first_git=_WORKER_COMMITS_OUT_OF_SCOPE,
        plant_out_of_scope=True,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "moved HEAD" in (outcome.block_reason or "")

        plan = plan_cleanup(store, outcome.run_id)
        worker_head = plan.head
        assert worker_head != base
        refs = subprocess.run(  # noqa: S603,S607 - fixed, test-local git command
            ["git", "for-each-ref", "--contains", worker_head],
            cwd=scoped_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert refs.strip() == "", "nothing but the worktree references the worker's commit"

        assert plan.allowed is False
        assert "unretained_commit" in [item["reason"] for item in plan.refusals], plan.refusals
        assert plan.candidate_ref == ""
        assert not any("candidate ref" in reason for reason in plan.reasons), plan.reasons

        result = apply_cleanup(store, outcome.run_id)
        assert result["applied"] is False and result["status"] == "REFUSED"
        worktree = Path(plan.path)
        assert worktree.exists()
        assert _git(worktree, "rev-parse", "HEAD").strip() == worker_head
    finally:
        store.close()
