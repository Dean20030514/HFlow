"""``hflow integrate <run-id>``: one re-entrant command for a run's whole integration.

And the cleanup that follows an integration: whenever ``integrate <run-id>``, ``integrate apply``
or ``integrate reconcile`` brings an integration to ``integrated``, the run's own worktree is
released in the same process through exactly the guarded cleanup ``hflow clean <run-id> --apply``
runs - unless ``--keep-worktree`` - and a cleanup that is refused never changes the exit code.

Every test builds a real Git repository and runs one offline task to ``ACCEPTED`` with the fake
driver and a real command check (``tests.test_integrate.Scene``). The person at a terminal is a
standard input that says it is one and types the code it reads from the screen (stderr).
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from hflow import integrate as integrate_module
from hflow.cli import main
from hflow.contracts import IntegrationState, RunRequest, TaskState
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.hints import ATTEST_PLACEHOLDER, shell_word
from hflow.integrate import CONFIRMATION_CODE_LENGTH, confirmation_code
from hflow.ownership import Probe
from hflow.verify import CheckRunners

from tests.test_integrate import BUILD, Scene, _commit_on, _git, _task


class BranchScene(Scene):
    """A scene whose task names its base by branch (``main``), as ``hflow init task`` writes it:
    the base branch is then the default target of ``integrate <run-id>``."""

    def accept(self, task_id: str = "T-integrate", text: str = "value = 2\n") -> str:
        controller = Controller(
            self.store,
            FakeDriver(self.repo, FakeScript(write_plan={"app.txt": text}, agent_turns=1)),
            controller_build=BUILD,
            runners=CheckRunners.offline_default(),
            data_dir=self.data_dir,
            production=False,
        )
        outcome = controller.run_task(
            RunRequest(
                task=_task("main", task_id),
                project=self.project,
                project_root=self.repo,
                workspace_root=self.repo,
            )
        )
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        return outcome.run_id


@pytest.fixture()
def scene(tmp_path: Path):
    """The user works on another branch, so ``main`` is not checked out anywhere."""
    scene = BranchScene(tmp_path, user_branch="work")
    yield scene
    scene.store.close()


@pytest.fixture()
def scene_on_main(tmp_path: Path):
    """The user's checkout has ``main`` checked out: the default case, a hand-off."""
    scene = BranchScene(tmp_path, user_branch="main")
    yield scene
    scene.store.close()


class _Piped(io.StringIO):
    """Standard input that is not a terminal, and must not be read."""

    def isatty(self) -> bool:
        return False

    def readline(self, *args: object) -> str:  # type: ignore[override]
        raise AssertionError("stdin was read")


class _Person(io.StringIO):
    """A person at a terminal: reads what the screen (stderr) shows so far, then types the code.

    ``answer`` turns the code shown into what is typed (a wrong code, for instance). ``screen``
    and ``out`` keep what was printed before the prompt was answered.
    """

    def __init__(self, capsys: pytest.CaptureFixture[str], answer=None) -> None:  # noqa: ANN001
        super().__init__()
        self.capsys = capsys
        self.answer = answer
        self.screen = ""
        self.out = ""
        self.codes: list[str] = []

    def isatty(self) -> bool:
        return True

    def readline(self, *args: object) -> str:  # type: ignore[override]
        captured = self.capsys.readouterr()
        self.screen += captured.err
        self.out += captured.out
        match = re.search(r"^confirm code  ([0-9a-f]+)$", self.screen, re.MULTILINE)
        assert match is not None, self.screen
        assert self.screen.rstrip().endswith("to integrate:"), self.screen
        code = match.group(1)
        self.codes.append(code)
        return (self.answer(code) if self.answer else code) + "\n"


def _project_file(scene: Scene, tmp_path: Path) -> Path:
    path = tmp_path / "project.json"
    path.write_text(scene.project.model_dump_json(), encoding="utf-8")
    return path


def _default_contract(scene: Scene) -> None:
    """``<repo>/.hflow/project.json``: the contract the run was admitted under, where the run
    recorded it - so neither ``--project`` nor ``--target`` is needed."""
    default = scene.repo / ".hflow" / "project.json"
    default.parent.mkdir(exist_ok=True)
    default.write_text(scene.project.model_dump_json(), encoding="utf-8")


def _data(scene: Scene) -> list[str]:
    return ["--data-dir", str(scene.data_dir)]


def _printed(scene: Scene, *words: str) -> str:
    """A follow-up command as HFlow prints it for this (non-default) ledger."""
    return " ".join(["hflow", *words, "--data-dir", shell_word(os.path.abspath(scene.data_dir))])


def _next_lines(text: str) -> list[str]:
    return [line.removeprefix("next: ") for line in text.splitlines() if line.startswith("next: ")]


def _worktree(scene: Scene, run_id: str) -> Path:
    return Path(scene.store.get_run(run_id)["worktree_path"])


def _integrations(scene: Scene, run_id: str):
    return scene.store.integrations_for(run_id)


# --------------------------------------------------------------------------
# from nothing
# --------------------------------------------------------------------------


def test_from_nothing_without_a_terminal_it_prepares_and_stops_at_ready(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --target, no --project: the task's base branch and the recorded contract. Without a
    terminal nothing is asked and nothing moves; it says to run it again at one (exit 0)."""
    run_id = scene.accept()
    _default_contract(scene)
    user_before = scene.user_state()
    monkeypatch.setattr("sys.stdin", _Piped())

    assert main(["integrate", run_id, *_data(scene)]) == 0

    out = capsys.readouterr().out
    [record] = _integrations(scene, run_id)
    assert record.state is IntegrationState.READY
    assert record.target_ref == "refs/heads/main" and record.target_tip == scene.base
    for fact in (
        "state         ready",
        f"target        refs/heads/main checked against {scene.base}",
        f"commit        {record.integration_commit} (squash)",
        "paths         app.txt",
        f"evidence      {', '.join(record.evidence_ids)} (phase integration-check)",
    ):
        assert fact in out, out
    [step] = _next_lines(out)
    assert step.startswith(_printed(scene, "integrate", run_id) + "  # run it at an interactive")
    assert scene.tip() == scene.base, "nothing moves without the confirmation"
    assert scene.user_state() == user_before
    assert _worktree(scene, run_id).exists()

    # Run again without a terminal - the null device this time, which Windows calls a tty: the
    # same ready integration, nothing asked, nothing prepared twice.
    with open(os.devnull, encoding="utf-8") as null:
        monkeypatch.setattr("sys.stdin", null)
        assert main(["integrate", run_id, "--json", *_data(scene)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integration"]["integration_id"] == record.integration_id
    assert payload["integration"]["state"] == "ready"
    assert "cleanup" not in payload
    assert len(_integrations(scene, run_id)) == 1


def test_at_a_terminal_the_right_code_applies_and_releases_the_run_worktree(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    worktree = _worktree(scene, run_id)
    user_before = scene.user_state()
    person = _Person(capsys)
    monkeypatch.setattr("sys.stdin", person)

    code = main(["integrate", run_id, "--project", str(_project_file(scene, tmp_path)),
                 *_data(scene)])

    out = person.out + capsys.readouterr().out
    assert code == 0, out
    [record] = _integrations(scene, run_id)
    assert record.state is IntegrationState.INTEGRATED and record.basis == "hflow_ref_update"
    # What was confirmed: the facts and the code on stderr, before the one line was read.
    assert person.codes == [confirmation_code(record)]
    assert f"commit        {record.integration_commit} (squash)" in person.screen
    assert "paths         app.txt" in person.screen
    assert f"refs/heads/main {scene.base} -> {record.integration_commit}" in person.screen
    assert scene.tip() == record.integration_commit
    assert scene.user_state() == user_before
    assert "INTEGRATED (hflow_ref_update)" in out
    # Released in the same process, by clean's own guarded removal: nothing left to do.
    assert "cleanup       REMOVED: workspace removed" in out, out
    assert not worktree.exists()
    assert scene.store.get_run(run_id)["worktree_state"] == "REMOVED"
    assert _next_lines(out) == []
    assert scene.store.get_run(run_id)["receipt_json"], "the run's own receipt is kept"


def test_a_wrong_code_applies_nothing_and_writes_nothing(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    project = str(_project_file(scene, tmp_path))
    person = _Person(capsys, answer=lambda code: "y")
    monkeypatch.setattr("sys.stdin", person)

    assert main(["integrate", run_id, "--project", project, *_data(scene)]) == 2

    err = capsys.readouterr().err
    assert "the confirmation code did not match" in err and "nothing was applied" in err
    assert _next_lines(err) == [_printed(scene, "integrate", run_id)]
    [record] = _integrations(scene, run_id)
    assert record.state is IntegrationState.READY
    assert not record.apply_intent_at
    assert scene.tip() == scene.base
    assert _worktree(scene, run_id).exists()

    # Another integration's code (a remembered one) does not apply this one either.
    other = record.model_copy(update={"target_tip": "0" * 40})
    assert confirmation_code(other) != confirmation_code(record)
    monkeypatch.setattr("sys.stdin", _Person(capsys, answer=lambda code: confirmation_code(other)))
    assert main(["integrate", run_id, *_data(scene)]) == 2
    capsys.readouterr()
    assert scene.tip() == scene.base
    assert _integrations(scene, run_id)[0].state is IntegrationState.READY


def test_the_confirmation_code_is_short_hex_bound_to_the_record(scene: BranchScene) -> None:
    run_id = scene.accept()
    record = scene.prepare(run_id).record
    code = confirmation_code(record)
    assert re.fullmatch(r"[0-9a-f]+", code) and len(code) == CONFIRMATION_CODE_LENGTH == 8
    assert confirmation_code(record) == code
    for change in (
        {"integration_commit": "1" * 40},
        {"target_ref": "refs/heads/other"},
        {"evidence_ids": []},
        {"integration_id": "G-other"},
    ):
        assert confirmation_code(record.model_copy(update=change)) != code, change


# --------------------------------------------------------------------------
# the hand-off: the target is checked out
# --------------------------------------------------------------------------


def test_a_checked_out_target_is_handed_off_and_the_merge_is_recorded_on_the_next_run(
    scene_on_main: BranchScene, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    scene = scene_on_main
    run_id = scene.accept()
    project = str(_project_file(scene, tmp_path))
    worktree = _worktree(scene, run_id)
    user_before = scene.user_state()
    person = _Person(capsys)
    monkeypatch.setattr("sys.stdin", person)

    assert main(["integrate", run_id, "--project", project, *_data(scene)]) == 3

    out = person.out + capsys.readouterr().out
    [record] = _integrations(scene, run_id)
    assert record.state is IntegrationState.READY
    merge, again = _next_lines(out)
    # Exactly apply's hand-off, then `integrate <run-id>` instead of `integrate reconcile <id>`.
    assert merge.startswith("git -C ") and merge.endswith(
        f" merge --ff-only {record.integration_commit}"
    )
    assert again == _printed(scene, "integrate", run_id)
    assert "is checked out at" in out and "HFlow did not move it" in out
    assert "cleanup" not in out
    # Nothing was written to the checkout or the branch.
    assert scene.tip() == scene.base
    assert scene.user_state() == user_before
    assert (scene.repo / "app.txt").read_text(encoding="utf-8") == "value = 1\n"
    assert worktree.exists()

    # The operator runs the merge as printed; then `integrate <run-id>` again - no terminal
    # needed - records it and releases the worktree.
    subprocess.run(  # noqa: S603 - the printed command, as an operator would paste it
        shlex.split(merge),
        capture_output=True,
        check=True,
        timeout=60,
        env={
            **os.environ,
            "GIT_COMMITTER_NAME": "Sample Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )
    assert scene.tip() == record.integration_commit
    monkeypatch.setattr("sys.stdin", _Piped())
    assert main(["integrate", run_id, *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "INTEGRATED (operator_merge_observed)" in out, out
    assert "cleanup       REMOVED" in out, out
    assert not worktree.exists()
    [record] = _integrations(scene, run_id)
    assert record.state is IntegrationState.INTEGRATED
    assert record.basis == "operator_merge_observed"
    assert (scene.repo / "scratch.txt").exists(), "the user's own file survives the merge"

    # Once more: reported, nothing written, the cleanup already recorded.
    assert main(["integrate", run_id, *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "already integrated; nothing was written" in out
    assert "cleanup       REMOVED: already removed (idempotent)" in out


# --------------------------------------------------------------------------
# the cleanup after integrated
# --------------------------------------------------------------------------


def test_keep_worktree_leaves_the_worktree_and_a_later_run_releases_it(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    worktree = _worktree(scene, run_id)
    person = _Person(capsys)
    monkeypatch.setattr("sys.stdin", person)

    assert main(["integrate", run_id, "--project", str(_project_file(scene, tmp_path)),
                 "--keep-worktree", "--json", *_data(scene)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["integration"]["state"] == "integrated"
    assert payload["cleanup"]["status"] == "KEPT"
    assert worktree.exists()

    # Text: the kept worktree, and clean's own next lines.
    monkeypatch.setattr("sys.stdin", _Piped())
    assert main(["integrate", run_id, "--keep-worktree", *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "cleanup       KEPT" in out
    preview, release = _next_lines(out)
    assert preview == _printed(scene, "clean", run_id) + "  # preview; changes nothing"
    assert release == _printed(scene, "clean", run_id, "--apply")
    assert main(shlex.split(preview, comments=True)[1:]) == 0
    assert "decision      ALLOWED" in capsys.readouterr().out
    assert worktree.exists()

    # Without the flag the run's integration is finished, so the worktree is released.
    assert main(["integrate", run_id, *_data(scene)]) == 0
    assert "cleanup       REMOVED: workspace removed" in capsys.readouterr().out
    assert not worktree.exists()


def test_a_refused_cleanup_is_reported_and_does_not_change_the_exit_code(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    worktree = _worktree(scene, run_id)
    # Something in the run's worktree that was never frozen: clean refuses to delete it.
    (worktree / "left-behind.txt").write_text("not frozen\n", encoding="utf-8")
    person = _Person(capsys)
    monkeypatch.setattr("sys.stdin", person)

    code = main(["integrate", run_id, "--project", str(_project_file(scene, tmp_path)),
                 *_data(scene)])

    out = person.out + capsys.readouterr().out
    assert code == 0, out
    assert _integrations(scene, run_id)[0].state is IntegrationState.INTEGRATED
    assert "cleanup       REFUSED: the cleanup guards refused" in out, out
    assert "  refuse      unfrozen_changes: " in out and "left-behind.txt" in out
    assert _next_lines(out) == [
        _printed(scene, "clean", run_id) + "  # preview; changes nothing",
        _printed(scene, "clean", run_id, "--apply"),
    ]
    assert (worktree / "left-behind.txt").exists()


def test_a_cleanup_that_raises_is_reported_and_does_not_change_the_exit_code(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    scene.store.close()

    def broken(*args: object, **kwargs: object) -> dict:
        raise OSError("disk went away")

    monkeypatch.setattr("hflow.cleanup.apply_cleanup", broken)
    assert main(["integrate", "apply", prepared.integration_id, "--expect-target", scene.base,
                 "--json", *_data(scene)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integration"]["state"] == "integrated"
    assert payload["cleanup"]["status"] == "ERROR"
    assert "disk went away" in payload["cleanup"]["detail"]


# --------------------------------------------------------------------------
# the subcommands: unchanged, plus the cleanup after integrated
# --------------------------------------------------------------------------


def test_apply_and_reconcile_release_the_worktree_only_when_they_integrate(
    scene_on_main: BranchScene, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scene = scene_on_main
    run_id = scene.accept()
    worktree = _worktree(scene, run_id)
    project = str(_project_file(scene, tmp_path))
    scene.store.close()

    assert main(["integrate", "prepare", run_id, "--target", "main", "--project", project,
                 "--json", *_data(scene)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    integration_id = prepared["integration"]["integration_id"]
    commit = prepared["integration"]["integration_commit"]
    merge, reconcile = prepared["handoff"]
    assert reconcile == _printed(scene, "integrate", "reconcile", integration_id)

    # The hand-off of the old subcommand is unchanged; nothing integrated, nothing cleaned.
    assert main(["integrate", "apply", integration_id, "--expect-target", scene.base,
                 *_data(scene)]) == 3
    out = capsys.readouterr().out
    assert _next_lines(out) == [merge, reconcile]
    assert "cleanup" not in out and worktree.exists()
    assert main(["integrate", "reconcile", integration_id, "--json", *_data(scene)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integration"]["state"] == "ready" and "cleanup" not in payload

    _git(scene.repo, "merge", "--ff-only", "-q", commit)
    assert main(["integrate", "reconcile", integration_id, *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "INTEGRATED (operator_merge_observed)" in out
    assert "cleanup       REMOVED: workspace removed" in out
    assert not worktree.exists()

    # Already integrated before this process: reported, no second cleanup.
    assert main(["integrate", "reconcile", integration_id, "--json", *_data(scene)]) == 0
    assert "cleanup" not in json.loads(capsys.readouterr().out)
    assert main(["integrate", "apply", integration_id, "--expect-target", scene.base, "--json",
                 *_data(scene)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["notes"] == ["already integrated; nothing was written"]
    assert "cleanup" not in again


def test_apply_keep_worktree_opts_out(
    scene: BranchScene, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = scene.accept()
    worktree = _worktree(scene, run_id)
    prepared = scene.prepare(run_id).record
    scene.store.close()

    assert main(["integrate", "apply", prepared.integration_id, "--expect-target", scene.base,
                 "--keep-worktree", *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "INTEGRATED (hflow_ref_update)" in out
    assert "cleanup       KEPT" in out
    assert worktree.exists()


# --------------------------------------------------------------------------
# continuing from the other states
# --------------------------------------------------------------------------


def test_a_moved_target_goes_stale_and_the_next_run_prepares_again(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    _default_contract(scene)
    first = scene.prepare(run_id).record
    moved = _commit_on(scene.repo, "main", "notes.txt", "notes, updated\n", "unrelated")
    monkeypatch.setattr("sys.stdin", _Piped())

    assert main(["integrate", run_id, *_data(scene)]) == 3
    out = capsys.readouterr().out
    assert "state         stale" in out
    assert _next_lines(out) == [
        _printed(scene, "integrate", run_id)
        + "  # prepares again against the branch's current tip"
    ]
    assert scene.tip() == moved

    assert main(["integrate", run_id, *_data(scene)]) == 0
    capsys.readouterr()
    stale, again = _integrations(scene, run_id)
    assert stale.integration_id == first.integration_id
    assert stale.state is IntegrationState.STALE
    assert again.state is IntegrationState.READY
    assert again.target_tip == moved and again.mode == "replayed"


def test_a_conflict_is_reported_and_never_prepared_again(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    _commit_on(scene.repo, "main", "app.txt", "value = 3\n", "a conflicting change")
    project = str(_project_file(scene, tmp_path))
    monkeypatch.setattr("sys.stdin", _Piped())

    assert main(["integrate", run_id, "--project", project, *_data(scene)]) == 3
    assert "state         conflict" in capsys.readouterr().out

    assert main(["integrate", run_id, *_data(scene)]) == 3
    out = capsys.readouterr().out
    assert "state         conflict" in out
    assert "integrate <run-id>` does not" in out
    assert len(_integrations(scene, run_id)) == 1, "nothing was prepared again"


def _applying(scene: Scene, run_id: str):
    """A ready integration left ``applying`` by a process that is no longer there."""
    record = scene.prepare(run_id).record
    return scene.store.update_integration(
        record.integration_id,
        expected=IntegrationState.READY,
        state=IntegrationState.APPLYING,
        apply_intent_at="2026-10-16T00:00:00Z",
        applied_by="tester",
        owner_pid=4242,
        owner_created=133_000_000_000_000_000,
        owner_host="elsewhere",
    )


def test_an_applying_integration_is_reconciled_and_continues(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    record = _applying(scene, run_id)
    monkeypatch.setattr(integrate_module, "probe", lambda identity: Probe("gone", "test: exited"))
    monkeypatch.setattr("sys.stdin", _Piped())

    assert main(["integrate", run_id, *_data(scene)]) == 0
    out = capsys.readouterr().out
    assert "state         ready" in out
    assert scene.store.integration(record.integration_id).state is IntegrationState.READY
    assert scene.tip() == scene.base


def test_an_owner_that_may_run_is_never_overridden_and_an_attestation_is_never_fabricated(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    record = _applying(scene, run_id)
    monkeypatch.setattr("sys.stdin", _Piped())

    monkeypatch.setattr(
        integrate_module, "probe", lambda identity: Probe("matching", "test: it runs")
    )
    assert main(["integrate", run_id, *_data(scene)]) == 5
    err = capsys.readouterr().err
    assert "nothing was written" in err
    assert _next_lines(err) == [
        _printed(scene, "integrate", run_id) + "  # once the process working on it has exited"
    ]

    monkeypatch.setattr(
        integrate_module, "probe", lambda identity: Probe("unknown", "test: another host")
    )
    assert main(["integrate", run_id, *_data(scene)]) == 5
    [step] = _next_lines(capsys.readouterr().err)
    assert step.startswith(
        "hflow integrate " + run_id + f" --owner-gone --attest {ATTEST_PLACEHOLDER} --data-dir "
    )
    # The placeholder pasted as printed is refused; nothing is recorded.
    assert main(["integrate", run_id, "--owner-gone", "--attest", ATTEST_PLACEHOLDER.strip('"'),
                 *_data(scene)]) == 4
    capsys.readouterr()
    assert scene.store.integration(record.integration_id).state is IntegrationState.APPLYING

    assert main(["integrate", run_id, "--owner-gone", "--attest",
                 "the applying host was reimaged yesterday", *_data(scene)]) == 0
    capsys.readouterr()
    assert scene.store.integration(record.integration_id).state is IntegrationState.READY
    assert any("attested by" in note for note in scene.store.notes_for(run_id))


# --------------------------------------------------------------------------
# refusals and lookups
# --------------------------------------------------------------------------


def test_without_a_default_target_it_asks_for_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task based on a commit id has no base branch: --target is the operator's to name."""
    scene = Scene(tmp_path, user_branch="work")
    try:
        run_id = scene.accept()
        project = str(_project_file(scene, tmp_path))
        monkeypatch.setattr("sys.stdin", _Piped())
        assert main(["integrate", run_id, "--project", project, *_data(scene)]) == 4
        assert "pass --target <branch>" in capsys.readouterr().err
        assert _integrations(scene, run_id) == []

        assert main(["integrate", run_id, "--project", project, "--target", "main",
                     *_data(scene)]) == 0
        capsys.readouterr()
        assert [r.state for r in _integrations(scene, run_id)] == [IntegrationState.READY]
    finally:
        scene.store.close()


def test_unknown_runs_and_unknown_words_are_usage_errors_that_create_nothing(
    scene: BranchScene, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = scene.accept()
    scene.store.close()
    monkeypatch.setattr("sys.stdin", _Piped())
    # An unknown run, in an existing ledger and in a mistyped data directory.
    assert main(["integrate", "R-nope", *_data(scene)]) == 4
    assert "unknown run R-nope" in capsys.readouterr().err
    typo = tmp_path / "typo"
    assert main(["integrate", "R-nope", "--data-dir", str(typo)]) == 4
    assert "nothing was created" in capsys.readouterr().err
    assert not typo.exists()
    # A word that is neither a subcommand nor a run id stays a usage error.
    with pytest.raises(SystemExit) as caught:
        main(["integrate", "prepar", run_id, *_data(scene)])
    assert caught.value.code == 4
    assert "invalid choice: 'prepar'" in capsys.readouterr().err
