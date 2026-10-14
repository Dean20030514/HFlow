"""An approval is spent in the ledger it names, and only there.

Single use is enforced per ledger (``<data-dir>/hflow.sqlite``). Before a binding named its
ledger, an artifact consumed in one data directory authorized the same task again from another
(``--data-dir`` or ``HFLOW_DATA_DIR``): the new ledger had no row for it. These tests drive
``prepare`` and ``run`` through ``hflow.cli.main`` with a real-driver profile whose client is the
checked-in stand-in. Every run here refuses before a driver is constructed: nothing is launched
and no model is called.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hflow.authorization import AuthorizationRecord, load_authorization
from hflow.cli import EXIT_OK, EXIT_REFUSED, main
from hflow.paths import database_path
from hflow.plan import load_plan
from hflow.prepare import pending_binding, resolve_arguments
from hflow.store import Store

from .conftest import write_profile, write_project, write_task
from .test_m2_slice import _git, _project, _task, sample_repo  # noqa: F401 - registers the shared fixture

APPROVAL_TEXT = "I approve one bounded run of T-m2-none-input, spent in this data directory."


@pytest.fixture()
def live(tmp_path, sample_repo, live_profile, acpx_client, monkeypatch):  # noqa: ANN001, ANN201, F811
    """One task, one real-driver profile, and two data directories that both hold the profile."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    project_file = write_project(tmp_path / "project.json", _project(sample_repo))
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    task_file = write_task(tmp_path / "task.json", _task(sample_repo, base))
    for name in ("data-a", "data-b"):
        write_profile(tmp_path / name, live_profile)

    def no_driver(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        pytest.fail("this refusal must happen before constructing a real driver")

    monkeypatch.setattr("hflow.prepare.role_drivers", no_driver)
    return {
        "tmp": tmp_path,
        "a": tmp_path / "data-a",
        "b": tmp_path / "data-b",
        "flags": [
            "--task", str(task_file), "--project", str(project_file),
            "--project-root", str(sample_repo), "--profile", live_profile.profile_id,
        ],
    }


def _prepare(flags: list[str], data_dir: Path | str, capsys: pytest.CaptureFixture[str]) -> dict:
    assert main(["prepare", *flags, "--data-dir", str(data_dir), "--json"]) == EXIT_OK
    return json.loads(capsys.readouterr().out)["authorization"]


def _approval_file(path: Path, pending: dict, *, binding: dict | None = None) -> Path:
    """The user's step: an artifact covering the binding ``prepare`` printed."""
    record = AuthorizationRecord(
        authorization_id="AUTH-ledger-cli",
        user_text=APPROVAL_TEXT,
        authorized_at="2026-10-14T00:00:00Z",
        max_top_level_submissions=pending["max_top_level_submissions_required"],
        binding=pending["binding"] if binding is None else binding,
    )
    path.write_text(record.model_dump_json(), encoding="utf-8")
    return path


def test_an_approval_consumed_in_one_data_dir_does_not_authorize_the_task_in_another(
    live, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pending = _prepare(live["flags"], live["a"], capsys)
    assert pending["binding"]["ledger_path"] == str(database_path(live["a"]).resolve())
    approval = _approval_file(live["tmp"] / "approval.json", pending)
    record = load_authorization(approval)
    assert record.binding_digest() == pending["binding_digest"]

    # Spend it in full where it was issued: the ledger of data directory A.
    store = Store(database_path(live["a"]))
    try:
        store.register_authorization(record.as_store_record())
        for _ in range(record.max_top_level_submissions):
            store.claim_authorized_submission(record.authorization_id)
        spent = store.authorization_state(record.authorization_id)
    finally:
        store.close()
    assert spent is not None
    assert spent["used_top_level_submissions"] == record.max_top_level_submissions

    # Data directory B's ledger has no row for it, which is why B must refuse it - however B is
    # named. The ledger check runs after the task, configuration, contract and launch checks, so
    # reaching it means everything else about this approval still matched.
    run = ["run", *live["flags"], "--authorization-file", str(approval), "--json"]
    assert main([*run, "--data-dir", str(live["b"])]) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "issued for another ledger" in err
    assert "not a second allowance" in err
    monkeypatch.setenv("HFLOW_DATA_DIR", str(live["b"]))
    assert main(run) == EXIT_REFUSED
    assert "issued for another ledger" in capsys.readouterr().err

    assert not database_path(live["b"]).exists(), "refused before any ledger was opened"
    store = Store(database_path(live["a"]))
    try:
        assert store.authorization_state(record.authorization_id) == spent
    finally:
        store.close()


def test_an_approval_that_names_no_ledger_cannot_authorize_a_real_run(
    live, capsys: pytest.CaptureFixture[str]
) -> None:
    pending = _prepare(live["flags"], live["a"], capsys)
    unbound = {key: value for key, value in pending["binding"].items() if key != "ledger_path"}
    approval = _approval_file(live["tmp"] / "approval.json", pending, binding=unbound)
    assert load_authorization(approval).binding.ledger_path == ""

    run = ["run", *live["flags"], "--authorization-file", str(approval), "--json"]
    assert main([*run, "--data-dir", str(live["a"])]) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "names no ledger" in err
    assert "predates ledger binding" in err
    assert not database_path(live["a"]).exists()


def test_prepare_names_one_ledger_however_the_data_dir_is_given(
    live, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default resolution (a relative ``HFLOW_DATA_DIR``), an explicit absolute
    ``--data-dir`` and a saved plan all bind one ledger string - the one ``run --plan`` resolves."""
    monkeypatch.chdir(live["tmp"])
    monkeypatch.setenv("HFLOW_DATA_DIR", "data-a")  # relative on purpose
    ledger = str(database_path(live["a"]).resolve())

    assert main(["prepare", *live["flags"], "--json"]) == EXIT_OK
    default = json.loads(capsys.readouterr().out)["authorization"]
    explicit = _prepare(live["flags"], os.path.abspath("data-a"), capsys)
    assert default["binding"]["ledger_path"] == explicit["binding"]["ledger_path"] == ledger
    assert default["binding_digest"] == explicit["binding_digest"]

    # The text a person approves from shows the ledger next to the binding digest.
    assert main(["prepare", *live["flags"]]) == EXIT_OK
    text = capsys.readouterr().out
    assert f"  binding     {default['binding_digest']}" in text
    assert f"  ledger      {ledger}  (spent here only" in text

    plan_file = live["tmp"] / "live.plan.json"
    assert main(["prepare", *live["flags"], "--plan-out", str(plan_file)]) == EXIT_OK
    capsys.readouterr()
    plan = load_plan(plan_file)
    assert plan.binding["ledger_path"] == ledger
    assert plan.binding_digest == default["binding_digest"]
    again = pending_binding(
        resolve_arguments(plan.arguments).resolved,
        authorization_mode=plan.arguments.authorization_mode,
        root_binding=None,
    )
    assert again.digest() == plan.binding_digest
    assert not database_path(live["a"]).exists(), "prepare named the ledger and created nothing"
