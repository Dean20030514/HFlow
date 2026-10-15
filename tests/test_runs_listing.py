"""Batch L: ``hflow runs`` and ``hflow diff`` (``hflow.listing``), read-only views of stored runs.

Runs come from the offline fake driver in a real Git repository (``tests.test_integrate.Scene``)
and from seeded ledger rows. Nothing here calls a model, and neither command writes anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hflow.cli import EXIT_OK, EXIT_RECORD_UNREADABLE, EXIT_REFUSED, EXIT_USAGE, main
from hflow.contracts import IntegrationState, RefusalCode, TaskSpec, TaskState
from hflow.hints import RunFacts, next_steps
from hflow.listing import CandidateRange, attention, diff_argv
from hflow.store import Store

from tests.test_batch_e_ledger import _seed_ready_run
from tests.test_hints import _accepted, _record
from tests.test_integrate import Scene, _git


@pytest.fixture()
def scene(tmp_path: Path) -> Scene:
    scene = Scene(tmp_path, user_branch="work")
    yield scene
    scene.store.close()


def _runs(data_dir: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(["runs", *flags, "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _status_next(data_dir: Path, run_id: str, capsys: pytest.CaptureFixture[str]) -> list[str]:
    capsys.readouterr()
    assert main(["status", run_id, "--data-dir", str(data_dir)]) == EXIT_OK
    return [
        line.removeprefix("next: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("next: ")
    ]


# --------------------------------------------------------------------------
# attention: explicit stored facts only
# --------------------------------------------------------------------------


def test_attention_names_only_explicit_stored_facts() -> None:
    assert attention(RunFacts(run_id="R-1", task_state=TaskState.RUNNING)) == [
        "live RUNNING, no owner process recorded"
    ]
    # An integrated candidate whose worktree is still there needs nothing but housekeeping.
    assert attention(_accepted(integrations=(_record(IntegrationState.INTEGRATED),))) == []
    assert attention(_accepted(git_candidate=False)) == [], "in place: nothing to integrate"

    assert attention(_accepted()) == ["accepted candidate not integrated"]
    assert attention(_accepted(integrations=(_record(IntegrationState.READY),))) == [
        "integration G-1 ready: apply it, or merge it yourself and reconcile"
    ]
    assert attention(_accepted(integrations=(_record(IntegrationState.APPLYING),))) == [
        "integration G-1 applying: reconcile once its process has exited"
    ]
    assert attention(_accepted(integrations=(_record(IntegrationState.CONFLICT),))) == [
        "integration G-1 ended conflict; the candidate is not integrated"
    ]
    blocked = RunFacts(
        run_id="R-1", task_state=TaskState.BLOCKED, block_code="outcome_unknown",
        open_entries=("I-1",), settleable_entries=(("I-2", "launch_unknown"),),
    )
    assert attention(blocked) == [
        "blocked: outcome_unknown",
        "open ledger entries: I-1",
        "unresolved ledger entries: I-2 (launch_unknown)",
    ]
    assert attention(
        RunFacts(run_id="R-1", task_state=TaskState.ACCEPTED, worktree_state="REMOVING")
    ) == ["cleanup interrupted"]


def test_a_live_run_names_its_owner_and_a_deadline_the_clock_has_passed() -> None:
    """A controller killed hard (during checks, say) leaves a live run with no open ledger
    entry: it is listed from stored facts and the clock alone - no process is probed, so a passed
    deadline reads "probably interrupted" and leaves the verdict to ``resume``."""
    live = RunFacts(
        run_id="R-1", task_state=TaskState.CHECKING, owner_pid=4242,
        owner_since="2026-10-14T10:00:00Z", updated_at="2026-10-14T10:05:00Z",
        deadline_seconds=900,
    )
    before = datetime(2026, 10, 14, 10, 19, 59, tzinfo=UTC)
    after = datetime(2026, 10, 14, 10, 20, 1, tzinfo=UTC)
    owner = "live CHECKING, recorded owner pid 4242 since 2026-10-14T10:00:00Z"
    assert attention(live, now=before) == [owner]
    [first, second] = attention(live, now=after)
    assert first == owner
    assert second.startswith("past its deadline: nothing recorded since 2026-10-14T10:05:00Z")
    assert "900s" in second and "probably interrupted" in second
    assert "`hflow resume R-1` decides (exit 5 while the owner may be alive)" in second
    # No deadline recorded (no readable admission binding): the clock decides nothing.
    unbound = RunFacts(
        run_id="R-1", task_state=TaskState.RUNNING, updated_at="2026-10-14T10:05:00Z"
    )
    assert attention(unbound, now=after) == ["live RUNNING, no owner process recorded"]


# --------------------------------------------------------------------------
# hflow runs
# --------------------------------------------------------------------------


def test_runs_lists_each_run_with_the_hint_status_prints_and_needs_you_filters(
    scene: Scene, task_spec: TaskSpec, capsys: pytest.CaptureFixture[str]
) -> None:
    accepted = scene.accept()
    # A live run and a blocked one, seeded straight into the ledger after it (so newer).
    live = _seed_ready_run(scene.store, run_id="R-live", project_id="p", spec=task_spec)
    blocked = _seed_ready_run(
        scene.store, run_id="R-blocked", project_id="p",
        spec=task_spec.model_copy(update={"task_id": "T-blocked"}),
    )
    scene.store.set_blocked(blocked, RefusalCode.OUTCOME_UNKNOWN, "seeded: the controller died")
    scene.store.close()
    sqlite_before = (scene.data_dir / "hflow.sqlite").read_bytes()

    code, out, err = _runs(scene.data_dir, capsys)
    assert code == EXIT_OK, err
    header, *rows = out.splitlines()
    assert header.split() == ["RUN", "TASK", "STATE", "UPDATED", "NEXT"]
    assert [row.split()[0] for row in rows] == [blocked, live, accepted], "newest first"
    states = {row.split()[0]: row.split()[2] for row in rows}
    assert states == {
        blocked: "BLOCKED/outcome_unknown", live: "READY", accepted: "ACCEPTED/LOCAL_CANDIDATE",
    }
    for row in rows:
        hints = _status_next(scene.data_dir, row.split()[0], capsys)
        # A seeded blocked run with no attempt, entry or worktree has nothing to run next.
        assert bool(hints) is (row.split()[0] != blocked)
        assert row.endswith(hints[0] if hints else "  -"), (row, hints)

    code, out, _ = _runs(scene.data_dir, capsys, "--json")
    assert code == EXIT_OK
    listed = json.loads(out)
    assert [item["run_id"] for item in listed] == [blocked, live, accepted]
    store = Store(scene.data_dir / "hflow.sqlite")
    try:
        for item in listed:
            assert item["next"] == next_steps(store, item["run_id"])
            assert set(item) == {
                "run_id", "task_id", "task_state", "delivery_state", "block_code", "updated_at",
                "needs_you", "next",
            }
    finally:
        store.close()
    assert [item["needs_you"] for item in listed] == [
        ["blocked: outcome_unknown"],
        ["live READY, no owner process recorded"],
        ["accepted candidate not integrated"],
    ], "a live run names its recorded owner (here: none)"

    code, out, _ = _runs(scene.data_dir, capsys, "--limit", "1", "--json")
    assert [item["run_id"] for item in json.loads(out)] == [blocked]
    code, out, _ = _runs(scene.data_dir, capsys, "--needs-you", "--json")
    assert [item["run_id"] for item in json.loads(out)] == [blocked, live, accepted]
    code, out, _ = _runs(scene.data_dir, capsys, "--needs-you", "--limit", "1", "--json")
    assert [item["run_id"] for item in json.loads(out)] == [blocked]
    code, out, _ = _runs(scene.data_dir, capsys, "--needs-you")
    assert "  needs you: blocked: outcome_unknown" in out
    assert "  needs you: accepted candidate not integrated" in out
    assert "  needs you: live READY, no owner process recorded" in out
    assert (scene.data_dir / "hflow.sqlite").read_bytes() == sqlite_before, "runs wrote nothing"

    # Once the candidate reaches a branch it needs nothing more from the operator here.
    store = Store(scene.data_dir / "hflow.sqlite")
    scene.store = store
    prepared = scene.prepare(accepted).record
    code, out, _ = _runs(scene.data_dir, capsys, "--needs-you", "--json")
    assert json.loads(out)[-1]["needs_you"] == [
        f"integration {prepared.integration_id} ready: apply it, or merge it yourself and "
        "reconcile"
    ]
    applied = scene.apply(prepared.integration_id, scene.tip()).record
    assert applied.state is IntegrationState.INTEGRATED
    code, out, _ = _runs(scene.data_dir, capsys, "--needs-you", "--json")
    assert [item["run_id"] for item in json.loads(out)] == [blocked, live]


def test_an_unreadable_run_is_named_and_the_rest_still_listed(
    store: Store, task_spec: TaskSpec, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _seed_ready_run(store, run_id="R-good", project_id="p", spec=task_spec)
    bad = _seed_ready_run(
        store, run_id="R-bad", project_id="p",
        spec=task_spec.model_copy(update={"task_id": "T-bad"}),
    )
    store.set_blocked(bad, RefusalCode.REVIEW_REJECTED, "seeded")
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET receipt_json = ? WHERE run_id = ?", ("{not json", bad))
    store.close()
    data = tmp_path / "data"

    for flags in ((), ("--needs-you",)):
        code, out, err = _runs(data, capsys, *flags)
        assert code == EXIT_RECORD_UNREADABLE, flags
        assert f"stored record unreadable for run {bad}" in err
        assert bad not in out
        assert good in out, "the readable (live) run is still listed"
    # --json names the unreadable run as an entry of its own, never only on stderr.
    for flags in (("--json",), ("--json", "--needs-you")):
        code, out, err = _runs(data, capsys, *flags)
        assert code == EXIT_RECORD_UNREADABLE, flags
        assert f"stored record unreadable for run {bad}" in err
        entries = {item["run_id"]: item for item in json.loads(out)}
        assert set(entries) == {good, bad}
        assert set(entries[bad]) == {"run_id", "error"}
        assert entries[bad]["error"].startswith("stored record unreadable: not valid JSON")
        assert "error" not in entries[good]

    code, out, err = _diff(data, bad, capsys)
    assert code == EXIT_RECORD_UNREADABLE and out == ""
    assert f"stored record unreadable for run {bad}" in err


def test_runs_on_an_empty_data_dir_creates_no_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "empty"
    code, out, _ = _runs(data, capsys)
    assert (code, out.strip()) == (EXIT_OK, "no runs recorded")
    code, out, _ = _runs(data, capsys, "--json")
    assert (code, json.loads(out)) == (EXIT_OK, [])
    code, out, _ = _runs(data, capsys, "--needs-you")
    assert out.strip() == "no run needs you"
    assert not data.exists()
    with pytest.raises(SystemExit) as usage:
        main(["runs", "--limit", "0"])
    assert usage.value.code == EXIT_USAGE


# --------------------------------------------------------------------------
# hflow diff
# --------------------------------------------------------------------------


def _diff(data_dir: Path, run_id: str, capsys, *flags: str) -> tuple[int, str, str]:  # noqa: ANN001
    capsys.readouterr()
    code = main(["diff", run_id, *flags, "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_diff_prints_the_recorded_commits_never_a_working_tree(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = scene.accept()
    receipt = json.loads(scene.store.get_run(run_id)["receipt_json"])["candidate"]
    worktree = Path(scene.store.get_run(run_id)["worktree_path"])
    scene.store.close()
    # Later edits in the kept worktree and in the user's checkout are not the candidate.
    (worktree / "app.txt").write_text("value = 99\n", encoding="utf-8")
    (scene.repo / "app.txt").write_text("value = 42\n", encoding="utf-8")
    user_before = scene.user_state()

    code, out, err = _diff(scene.data_dir, run_id, capsys)
    assert code == EXIT_OK, err
    header, note, *patch = out.splitlines()
    assert header.startswith(
        f"run {run_id}: git diff {receipt['base_commit']}..{receipt['git_commit']}"
    )
    assert "receipt" in header
    assert "the candidate's Git identity, not its content fingerprint" in note
    assert receipt["fingerprint"] in note
    expected = _git(
        scene.repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
        f"{receipt['base_commit']}..{receipt['git_commit']}", "--",
    )
    assert "\n".join(patch) == expected.rstrip("\n")
    assert "+value = 2" in patch and "-value = 1" in patch
    assert "value = 99" not in out and "value = 42" not in out
    assert scene.user_state() == user_before

    code, out, _ = _diff(scene.data_dir, run_id, capsys, "--stat")
    assert code == EXIT_OK
    assert "app.txt | 2 +-" in out and "1 file changed" in out

    # A run that froze a candidate and did not accept it (a rejected review, a failed check)
    # has no receipt: its newest frozen candidate is what it recorded.
    store = Store(scene.data_dir / "hflow.sqlite")
    try:
        frozen = store.dsh_context_for(run_id)[-1]
        with store.transaction() as conn:
            conn.execute(
                "UPDATE runs SET task_state = 'BLOCKED', block_code = 'review_rejected', "
                "receipt_json = NULL, delivery_state = 'NONE' WHERE run_id = ?",
                (run_id,),
            )
    finally:
        store.close()
    code, out, err = _diff(scene.data_dir, run_id, capsys)
    assert code == EXIT_OK, err
    header = out.splitlines()[0]
    assert f"{frozen.base_commit}..{frozen.candidate_commit}" in header
    assert "did not accept it" in header
    assert "+value = 2" in out


def test_diff_refuses_a_run_with_no_candidate(
    controller, run_request, task_spec: TaskSpec, store: Store, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    in_place = controller.run_task(run_request)
    assert in_place.task_state is TaskState.ACCEPTED, in_place.block_reason
    ready = _seed_ready_run(
        store, run_id="R-ready", project_id="p",
        spec=task_spec.model_copy(update={"task_id": "T-ready"}),
    )
    data = tmp_path / "data"
    assert Path(store.path).parent == data
    store.close()

    code, _, err = _diff(data, in_place.run_id, capsys)
    assert code == EXIT_REFUSED
    assert "without a Git candidate" in err and "content fingerprint" in err
    code, _, err = _diff(data, ready, capsys)
    assert code == EXIT_REFUSED and "recorded no frozen Git candidate" in err
    code, _, err = _diff(data, "R-missing", capsys)
    assert code == EXIT_USAGE and "unknown run R-missing" in err
    code, _, err = _diff(tmp_path / "no-ledger", "R-any", capsys)
    assert code == EXIT_USAGE and not (tmp_path / "no-ledger").exists()


def test_the_revision_range_is_followed_by_the_path_separator(tmp_path: Path) -> None:
    rng = CandidateRange(
        run_id="R-1", base_commit="b" * 40, candidate_commit="c" * 40, source="test",
        fingerprint="", git_dir=str(tmp_path), project_root="",
    )
    for stat in (False, True):
        argv, cwd = diff_argv(rng, stat=stat)
        assert argv[-2:] == [f"{'b' * 40}..{'c' * 40}", "--"]
        assert argv[:3] == ["git", "--git-dir", str(tmp_path)] and cwd == tmp_path
        assert {"--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"} <= set(argv)
        assert ("--stat" in argv) is stat
