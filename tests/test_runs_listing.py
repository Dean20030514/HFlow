"""Batch L: ``hflow runs`` and ``hflow diff`` (``hflow.listing``), read-only views of stored runs;
batch N: the inbox ``hflow status`` prints without a run id.

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
from hflow.listing import CandidateRange, attention, diff_argv, inbox_group
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


def _status(
    data_dir: Path, capsys: pytest.CaptureFixture[str], *flags: str
) -> tuple[int, str, str]:
    """``hflow status`` without a run id: the inbox."""
    capsys.readouterr()
    code = main(["status", *flags, "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _inbox(out: str) -> dict[str, list[tuple[str, str]]]:
    """The text inbox as ``{group title: [(facts line, next command)]}``.

    Asserts its shape on the way: a ``Title (count)`` heading per group holding ``count``
    entries, and each entry exactly one facts line followed by exactly one ``next:`` line.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    counts: dict[str, int] = {}
    lines = out.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith(" "):
            title, count = line.rsplit(" ", 1)
            groups[title], counts[title] = [], int(count.strip("()"))
        else:
            assert line.startswith("  ") and not line.startswith("    "), line
            following = lines[index + 1]
            assert following.startswith("    next: "), (line, following)
            groups[title].append((line.strip(), following.removeprefix("    next: ")))
            index += 1
        index += 1
    assert {title: len(entries) for title, entries in groups.items()} == counts
    return groups


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
        "integration G-1 ready: integrate applies it or hands the merge over"
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


def test_runs_lists_each_run_with_the_hint_status_prints(
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
    assert (scene.data_dir / "hflow.sqlite").read_bytes() == sqlite_before, "runs wrote nothing"


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

    # `runs` and the inbox (`status` without a run id) alike.
    for read in (_runs, _status):
        code, out, err = read(data, capsys)
        assert code == EXIT_RECORD_UNREADABLE, read
        assert f"stored record unreadable for run {bad}" in err
        assert bad not in out
        assert good in out, "the readable (live) run is still listed"
        # --json names the unreadable run as an entry of its own, never only on stderr.
        code, out, err = read(data, capsys, "--json")
        assert code == EXIT_RECORD_UNREADABLE, read
        assert f"stored record unreadable for run {bad}" in err
        entries = {item["run_id"]: item for item in json.loads(out)}
        assert set(entries) == {good, bad}
        grouped = {"group"} if read is _status else set()
        assert set(entries[bad]) == {"run_id", "error"} | grouped
        assert entries[bad]["error"].startswith("stored record unreadable: not valid JSON")
        assert "error" not in entries[good]
    assert entries[bad]["group"] == "attention" and entries[good]["group"] == "running"

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
    code, out, _ = _status(data, capsys)
    assert (code, out.strip()) == (EXIT_OK, "nothing needs you")
    code, out, _ = _status(data, capsys, "--json")
    assert (code, json.loads(out)) == (EXIT_OK, [])
    assert not data.exists()
    with pytest.raises(SystemExit) as usage:
        main(["runs", "--limit", "0"])
    assert usage.value.code == EXIT_USAGE


# --------------------------------------------------------------------------
# hflow status without a run id: the inbox
# --------------------------------------------------------------------------

_BEFORE = datetime(2026, 10, 14, 10, 19, 59, tzinfo=UTC)
_AFTER = datetime(2026, 10, 14, 10, 20, 1, tzinfo=UTC)
_TIMED = {"updated_at": "2026-10-14T10:05:00Z", "deadline_seconds": 900}


@pytest.mark.parametrize(
    ("facts", "now", "group"),
    [
        (RunFacts(run_id="R-1", task_state=TaskState.RUNNING), _AFTER, "running"),
        (RunFacts(run_id="R-1", task_state=TaskState.DRAFT), _AFTER, "running"),
        (RunFacts(run_id="R-1", task_state=TaskState.CHECKING, **_TIMED), _BEFORE, "running"),
        (RunFacts(run_id="R-1", task_state=TaskState.CHECKING, **_TIMED), _AFTER, "attention"),
        # A live run's own open entries are not what files it under attention.
        (
            RunFacts(run_id="R-1", task_state=TaskState.RUNNING, open_entries=("I-1",)),
            _AFTER, "running",
        ),
        (
            RunFacts(run_id="R-1", task_state=TaskState.BLOCKED, block_code="outcome_unknown"),
            _AFTER, "attention",
        ),
        (
            RunFacts(run_id="R-1", task_state=TaskState.BLOCKED, block_code="review_rejected"),
            _AFTER, "attention",
        ),
        (
            RunFacts(run_id="R-1", task_state=TaskState.CANCELLED, open_entries=("I-1",)),
            _AFTER, "attention",
        ),
        (_accepted(settleable_entries=(("I-2", "launch_unknown"),)), _AFTER, "attention"),
        (_accepted(), _AFTER, "ready"),
        (_accepted(integrations=(_record(IntegrationState.READY),)), _AFTER, "ready"),
        (_accepted(integrations=(_record(IntegrationState.STALE),)), _AFTER, "ready"),
        (_accepted(integrations=(_record(IntegrationState.SUPERSEDED),)), _AFTER, "ready"),
        (_accepted(integrations=(_record(IntegrationState.PREPARING),)), _AFTER, "running"),
        (_accepted(integrations=(_record(IntegrationState.CHECKING),)), _AFTER, "running"),
        (_accepted(integrations=(_record(IntegrationState.APPLYING),)), _AFTER, "running"),
        (_accepted(integrations=(_record(IntegrationState.CONFLICT),)), _AFTER, "attention"),
        (_accepted(integrations=(_record(IntegrationState.CHECKS_FAILED),)), _AFTER, "attention"),
        (_accepted(integrations=(_record(IntegrationState.INTERRUPTED),)), _AFTER, "attention"),
        (_accepted(integrations=(_record(IntegrationState.FAILED),)), _AFTER, "attention"),
        # An interrupted cleanup, whether or not the candidate reached a branch.
        (_accepted(worktree_state="REMOVING"), _AFTER, "attention"),
        (
            _accepted(
                worktree_state="REMOVING",
                integrations=(_record(IntegrationState.INTEGRATED),),
            ),
            _AFTER, "attention",
        ),
    ],
)
def test_the_inbox_files_each_state_it_holds_under_one_group(
    facts: RunFacts, now: datetime, group: str
) -> None:
    assert attention(facts, now=now), "the inbox holds exactly the runs attention names"
    assert inbox_group(facts, now=now) == group


def test_status_without_a_run_id_is_the_inbox(
    scene: Scene, task_spec: TaskSpec, capsys: pytest.CaptureFixture[str]
) -> None:
    accepted = scene.accept()
    live = _seed_ready_run(scene.store, run_id="R-live", project_id="p", spec=task_spec)
    blocked = _seed_ready_run(
        scene.store, run_id="R-blocked", project_id="p",
        spec=task_spec.model_copy(update={"task_id": "T-blocked"}),
    )
    scene.store.set_blocked(blocked, RefusalCode.OUTCOME_UNKNOWN, "seeded: the controller died")
    scene.store.close()
    sqlite_before = (scene.data_dir / "hflow.sqlite").read_bytes()

    code, out, err = _status(scene.data_dir, capsys)
    assert code == EXIT_OK, err
    inbox = _inbox(out)
    assert list(inbox) == ["Running", "Ready", "Attention"]
    [(running, running_next)] = inbox["Running"]
    [(ready, ready_next)] = inbox["Ready"]
    [(attention_line, attention_next)] = inbox["Attention"]
    assert running.split()[:3] == [live, "T-001", "READY"]
    assert running.endswith("live READY, no owner process recorded")
    assert ready.split()[:3] == [accepted, "T-integrate", "ACCEPTED/LOCAL_CANDIDATE"]
    assert ready.endswith("accepted candidate not integrated")
    assert attention_line.split()[:3] == [blocked, "T-blocked", "BLOCKED/outcome_unknown"]
    assert attention_line.endswith("blocked: outcome_unknown")
    # Exactly one next: per entry - the first command `status <run>` prints for it ...
    live_hints = _status_next(scene.data_dir, live, capsys)
    assert len(live_hints) == 2 and running_next == live_hints[0]
    assert ready_next == _status_next(scene.data_dir, accepted, capsys)[0]
    assert ready_next.startswith(f"hflow integrate {accepted} ")
    # ... or, for a run the hints name nothing for, its own detailed view.
    assert _status_next(scene.data_dir, blocked, capsys) == []
    assert attention_next.startswith(f"hflow status {blocked} --data-dir ")
    assert attention_next.endswith("  # nothing left for HFlow to run; read what it recorded")

    # --json: `runs --json`'s entry for each run, plus its group, in the same order.
    code, out, _ = _status(scene.data_dir, capsys, "--json")
    assert code == EXIT_OK
    listed = json.loads(out)
    assert [(item["group"], item["run_id"]) for item in listed] == [
        ("running", live), ("ready", accepted), ("attention", blocked),
    ]
    _, runs_out, _ = _runs(scene.data_dir, capsys, "--json")
    every = {item["run_id"]: item for item in json.loads(runs_out)}
    for item in listed:
        assert {key: value for key, value in item.items() if key != "group"} == every[
            item["run_id"]
        ]
    assert (scene.data_dir / "hflow.sqlite").read_bytes() == sqlite_before, "status wrote nothing"

    # A prepared integration is still ready (apply it next); an integrated candidate leaves.
    scene.store = Store(scene.data_dir / "hflow.sqlite")
    prepared = scene.prepare(accepted).record
    inbox = _inbox(_status(scene.data_dir, capsys)[1])
    [(ready, ready_next)] = inbox["Ready"]
    assert ready.endswith(
        f"integration {prepared.integration_id} ready: integrate applies it or hands the merge "
        "over"
    )
    assert ready_next == _status_next(scene.data_dir, accepted, capsys)[0]
    # The one command for a ready integration is `integrate <run-id>` (it confirms, then applies
    # or hands the merge over).
    assert ready_next.startswith(f"hflow integrate {accepted} ")
    applied = scene.apply(prepared.integration_id, scene.tip()).record
    assert applied.state is IntegrationState.INTEGRATED
    inbox = _inbox(_status(scene.data_dir, capsys)[1])
    assert list(inbox) == ["Running", "Attention"]
    assert [line.split()[0] for line, _ in inbox["Running"] + inbox["Attention"]] == [
        live, blocked,
    ]


def test_an_inbox_with_nothing_for_you_is_one_line(
    controller, run_request, store: Store, tmp_path: Path,  # noqa: ANN001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An accepted in-place run has nothing to integrate or release: the ledger holds a run, and
    the inbox is still empty."""
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    store.close()
    data = tmp_path / "data"
    code, out, err = _status(data, capsys)
    assert (code, out, err) == (EXIT_OK, "nothing needs you\n", "")
    code, out, _ = _status(data, capsys, "--json")
    assert (code, json.loads(out)) == (EXIT_OK, [])
    # The run is still listed by `runs`, and `status <run>` still shows it in detail.
    assert outcome.run_id in _runs(data, capsys)[1]
    capsys.readouterr()
    assert main(["status", outcome.run_id, "--data-dir", str(data)]) == EXIT_OK
    assert f"run           {outcome.run_id}" in capsys.readouterr().out.splitlines()


def test_the_inbox_constructs_no_driver_and_probes_no_process(
    store: Store, task_spec: TaskSpec, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTS rule 6: ``status`` never calls a model. The inbox builds no driver or controller,
    starts no process (no Git either) and asks no owner process whether it is alive."""
    import subprocess

    from hflow import ownership
    from hflow.controller import Controller
    from hflow.drivers.acpx_dsh import AcpxDshDriver
    from hflow.drivers.fake import FakeDriver

    _seed_ready_run(store, run_id="R-live", project_id="p", spec=task_spec)
    blocked = _seed_ready_run(
        store, run_id="R-blocked", project_id="p",
        spec=task_spec.model_copy(update={"task_id": "T-blocked"}),
    )
    store.set_blocked(blocked, RefusalCode.OUTCOME_UNKNOWN, "seeded")
    store.close()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the inbox must not build a driver, start a process or probe one")

    for owner, name in (
        (FakeDriver, "__init__"), (AcpxDshDriver, "__init__"), (Controller, "__init__"),
        (subprocess.Popen, "__init__"), (ownership, "probe"), (ownership, "probe_recorded"),
        (ownership, "assess_owner"), (ownership, "lock_is_free"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    for flags in ((), ("--json",)):
        code, out, err = _status(tmp_path / "data", capsys, *flags)
        assert code == EXIT_OK, err
        assert "R-live" in out and blocked in out


def test_removed_spellings_name_their_replacement(
    store: Store, task_spec: TaskSpec, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``runs --needs-you`` and ``status --last`` are gone, not aliases: a usage error (exit 4)
    whose one line names what replaced them, before any ledger is opened or created."""
    run_id = _seed_ready_run(store, run_id="R-live", project_id="p", spec=task_spec)
    store.close()
    for data in (tmp_path / "data", tmp_path / "no-ledger"):
        for argv, replacement in (
            (["runs", "--needs-you"], "`hflow status` (no run id)"),
            (["runs", "--json", "--needs"], "`hflow status` (no run id)"),
            (["status", "--last"], "`hflow report --last`"),
            (["status", run_id, "--last", "--json"], "`hflow report --last`"),
        ):
            capsys.readouterr()
            with pytest.raises(SystemExit) as usage:
                main([*argv, "--data-dir", str(data)])
            assert usage.value.code == EXIT_USAGE, argv
            captured = capsys.readouterr()
            assert captured.out == ""
            [line] = captured.err.splitlines()
            flag = "--needs-you" if argv[0] == "runs" else "--last"
            assert line.startswith(f"hflow {argv[0]}: error: {flag} was removed; "), line
            assert replacement in line, line
    assert not (tmp_path / "no-ledger").exists()
    # The spellings that stay: `runs`, `report --last`, and a run id given to `status`.
    data = tmp_path / "data"
    assert _runs(data, capsys)[0] == EXIT_OK
    capsys.readouterr()
    assert main(["report", "--last", "--data-dir", str(data)]) == EXIT_OK
    assert capsys.readouterr().out.splitlines()[0] == f"run: {run_id}"
    # The inbox reads no workspace, so it takes no --project-root.
    code, out, err = _status(data, capsys, "--project-root", str(tmp_path))
    assert code == EXIT_USAGE and out == ""
    assert err.strip() == (
        "hflow status: error: --project-root reads one run's workspace: name the run id"
    )
    with pytest.raises(SystemExit):
        main(["status", "--help"])
    assert "--last" not in capsys.readouterr().out, "a removed spelling is not offered"


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
