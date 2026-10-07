"""One small task from approval to a moved branch, driven only through ``python -m hflow``.

Every step is a separate ``[sys.executable, "-m", "hflow", ...]`` process with an explicit
``--data-dir`` under ``tmp_path``, exactly as an operator would type it. Nothing is called
in-process: what one command hands the next travels through files, SQLite and Git.

1. ``prepare --json`` previews the task and prints the binding an approval must cover;
2. the test writes the authorization artifact from that ``authorization.binding`` - the user's
   action, which no command performs;
3. ``run`` with the identical flags admits it, dispatches the implementer and the reviewer (the
   offline fake driver for both), runs a real command check and ends ``ACCEPTED`` /
   ``LOCAL_CANDIDATE``;
4. ``integrate prepare`` builds and checks one integration commit, ``integrate apply`` moves the
   branch by compare-and-set - or, when the branch is checked out, hands off a ``git merge
   --ff-only`` that the operator runs, which ``integrate reconcile`` then observes;
5. ``status`` / ``report`` (text and JSON) show the integration beside the run's own receipt.

The run spends against a root budget (``--root-budget-file``): without one the offline driver
reads no authorization file at all, and step 2 would prove nothing. ``HFLOW_ALLOW_WRITES`` is not
set: the CLI requires it only for a real driver, and leaving it unset in every process keeps the
effective configuration ``prepare`` binds identical to the one ``run`` verifies. Nothing here calls
a model; the fake driver is not a compatibility proof of anything.

The helpers here are shared with ``test_fault_drills.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from hflow.authorization import AuthorizationRecord
from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    ReviewRequirement,
    Scope,
    TaskSpec,
    WorkspaceSpec,
)

from hflow.integrate import _shell_word

from .conftest import write_project, write_task

#: The worktree's own sources: a child process must import this tree, never an installed copy.
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
#: Fixed so every process of one test records the same controller build.
BUILD = "e2e-cli-loop-build"
ROOT_LIMITS = {"max_top_level_submissions": 4, "max_repairs": 0, "deadline_seconds": 3600}
SUBPROCESS_TIMEOUT = 180

CHECK_SOURCE = """import pathlib, sys
app = pathlib.Path("app.txt").read_text(encoding="utf-8")
notes = pathlib.Path("notes.txt").read_text(encoding="utf-8")
if "value = 2" not in app:
    print("app.txt does not set value = 2"); sys.exit(1)
if "broken" in notes:
    print("notes.txt says broken"); sys.exit(1)
print("ok")
"""

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sample Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Sample Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        env={**os.environ, **_GIT_IDENTITY},
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def hflow_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every ``hflow`` child: this tree's sources, a fixed build id.

    ``HFLOW_DATA_DIR`` (set per test by ``conftest``) and the stand-in ``dsh`` first on ``PATH``
    are inherited; every command still names ``--data-dir`` explicitly.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SRC_DIR), env.get("PYTHONPATH", "")) if part
    )
    env["HFLOW_BUILD_ID"] = BUILD
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("HFLOW_ALLOW_WRITES", None)
    env.pop("HFLOW_PROFILE", None)
    env.update(extra or {})
    return env


@dataclass
class Call:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> dict:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure diagnostics only
            raise AssertionError(f"not JSON ({exc}): {self}") from exc

    def __str__(self) -> str:
        return (
            f"{' '.join(self.argv[2:])}\n-> exit {self.returncode}\n"
            f"stdout: {self.stdout[-4000:]}\nstderr: {self.stderr[-4000:]}"
        )


def project_contract() -> ProjectConfig:
    return ProjectConfig(
        project_id="e2e-cli-project",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[sys.executable, "check.py"],
                timeout_seconds=120,
            )
        ],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=True,
    )


def task_spec(base_commit: str) -> TaskSpec:
    return TaskSpec(
        task_id="T-e2e",
        revision=1,
        goal="Set value to 2 in app.txt",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="value is 2", check_ids=["unit"])],
        scope=Scope(write_allow=["app.txt"], write_deny=[".git/**"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:none",
            reason="a one-line change",
        ),
        review=ReviewRequirement(required=True),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
        workspace=WorkspaceSpec(mode="worktree", base_commit=base_commit, keep=True),
    )


class CliProject:
    """A real Git repository, the hand-written input files and a way to run ``hflow``."""

    def __init__(self, tmp_path: Path, *, user_branch: str) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "project"
        self.repo.mkdir()
        (self.repo / "app.txt").write_text("value = 1\n", encoding="utf-8")
        (self.repo / "notes.txt").write_text("notes\n", encoding="utf-8")
        (self.repo / "check.py").write_text(CHECK_SOURCE, encoding="utf-8")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        if user_branch != "main":
            git(self.repo, "switch", "-q", "-c", user_branch)
        # The user's own uncommitted work, which nothing in this loop may touch.
        (self.repo / "scratch.txt").write_text("user's own uncommitted file\n", encoding="utf-8")

        self.data_dir = tmp_path / "data"
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        self.project_file = write_project(inputs / "project.json", project_contract())
        self.task_file = write_task(inputs / "task.json", task_spec(self.base))
        self.plan_file = inputs / "fake-write-plan.json"
        self.plan_file.write_text(json.dumps({"app.txt": "value = 2\n"}), encoding="utf-8")
        self.root_file = inputs / "root-budget.json"
        self.root_file.write_text(
            json.dumps({"limits": ROOT_LIMITS, "note": "one accepted delivery of T-e2e"}),
            encoding="utf-8",
        )
        self.authorization_file = inputs / "authorization.json"

    # -- running hflow ------------------------------------------------------------------------

    def hflow(
        self,
        *args: str,
        script: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> Call:
        """One command in a fresh process: ``python -m hflow`` or, for a fault drill, ``script``."""
        argv = [sys.executable, str(script) if script else "-m", *([] if script else ["hflow"])]
        argv += [*args, "--data-dir", str(self.data_dir)]
        completed = subprocess.run(  # noqa: S603 - the interpreter running this suite
            argv,
            cwd=str(self.tmp_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            env=hflow_env(extra_env),
            check=False,
        )
        return Call(argv, completed.returncode, completed.stdout, completed.stderr)

    def printed(self, *words: str) -> str:
        """A follow-up command exactly as HFlow prints it for this non-default ledger.

        ``integrate`` appends ``--data-dir`` (absolute, quoted for the operator's shell) whenever the
        ledger is not the default one, so the printed line can be pasted as it stands.
        """
        return " ".join(
            ["hflow", *words, "--data-dir", _shell_word(os.path.abspath(self.data_dir))]
        )

    def admission_flags(self) -> list[str]:
        """The flags ``prepare`` and ``run`` share: the binding is computed from exactly these."""
        return [
            "--task", str(self.task_file),
            "--project", str(self.project_file),
            "--project-root", str(self.repo),
            "--driver", "fake",
            "--root-budget-file", str(self.root_file),
        ]

    def run_argv(self) -> list[str]:
        return [
            "run", *self.admission_flags(),
            "--authorization-file", str(self.authorization_file),
            "--fake-write-plan", str(self.plan_file),
            "--json",
        ]

    def prepare_task(self) -> dict:
        call = self.hflow("prepare", *self.admission_flags(), "--json")
        assert call.returncode == 0, call
        prepared = call.json()
        assert prepared["model_calls_made"] == 0
        assert prepared["authorization"]["required"] is True
        assert prepared["authorization"]["creates_authorization"] is False
        assert not (self.data_dir / "hflow.sqlite").exists(), "prepare created the ledger"
        return prepared

    def authorize(self, prepared: dict) -> Path:
        """The user's step: an artifact covering exactly the binding ``prepare`` printed."""
        pending = prepared["authorization"]
        record = AuthorizationRecord.model_validate(
            {
                "authorization_id": "AUTH-e2e-cli-loop",
                "user_text": "I approve this one bounded offline task and nothing else.",
                "authorized_at": "2026-10-07T00:00:00Z",
                "max_top_level_submissions": pending["max_top_level_submissions_required"],
                "binding": pending["binding"],
                "root_limits": prepared["root_budget"]["limits"],
            }
        )
        assert record.binding_digest() == pending["binding_digest"]
        self.authorization_file.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return self.authorization_file

    def accept(self) -> dict:
        """prepare -> authorize -> run; returns the run's JSON payload (ACCEPTED)."""
        self.authorize(self.prepare_task())
        call = self.hflow(*self.run_argv())
        assert call.returncode == 0, call
        payload = call.json()
        assert payload["task_state"] == "ACCEPTED", payload
        assert payload["delivery_state"] == "LOCAL_CANDIDATE", payload
        assert payload["implementer_invocations"] == 1
        assert payload["reviewer_invocations"] == 1
        return payload

    def status_json(self, run_id: str) -> dict:
        call = self.hflow("status", run_id, "--project-root", str(self.repo), "--json")
        assert call.returncode == 0, call
        return call.json()

    def integrate_prepare(self, run_id: str, target: str = "main") -> dict:
        call = self.hflow(
            "integrate", "prepare", run_id, "--target", target,
            "--project", str(self.project_file), "--json",
        )
        assert call.returncode == 0, call
        prepared = call.json()
        assert prepared["integration"]["state"] == "ready", prepared
        return prepared

    # -- reading Git -------------------------------------------------------------------------

    def tip(self, branch: str = "main") -> str:
        return git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()

    def user_state(self) -> tuple[str, str, str]:
        return (
            git(self.repo, "symbolic-ref", "HEAD").strip(),
            git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            (self.repo / "scratch.txt").read_text(encoding="utf-8"),
        )

    def integration_evidence(self, run_id: str) -> list[dict]:
        return [
            item for item in self.status_json(run_id)["evidence"]
            if item["kind"] == "integration-check"
        ]


@pytest.fixture()
def cli_project(tmp_path: Path) -> CliProject:
    """The user works on another branch, so ``main`` is not checked out anywhere."""
    return CliProject(tmp_path, user_branch="work")


@pytest.fixture()
def cli_project_on_main(tmp_path: Path) -> CliProject:
    """The user's own checkout has ``main`` checked out."""
    return CliProject(tmp_path, user_branch="main")


def test_python_dash_m_hflow_is_the_cli(tmp_path: Path) -> None:
    """``python -m hflow`` is ``hflow.cli.main``: the same parser and the same exit codes."""

    def hflow(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the interpreter running this suite
            [sys.executable, "-m", "hflow", *args, "--data-dir", str(tmp_path / "data")],
            cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=SUBPROCESS_TIMEOUT, env=hflow_env(), check=False,
        )

    usage = hflow("status")
    assert usage.returncode == 4, usage.stderr  # EXIT_USAGE, not argparse's own 2
    assert "usage: hflow status" in usage.stderr
    missing = hflow("status", "R-missing")
    assert missing.returncode == 4, missing.stderr
    assert "unknown run R-missing" in missing.stderr
    help_text = hflow("--help")
    assert help_text.returncode == 0, help_text.stderr
    assert "integrate" in help_text.stdout


def test_authorization_to_integration_through_the_cli_alone(cli_project: CliProject) -> None:
    project = cli_project
    user_before = project.user_state()

    payload = project.accept()
    run_id = payload["run_id"]
    candidate = payload["receipt"]["candidate"]
    assert candidate["base_commit"] == project.base
    assert payload["receipt"]["verification"]["status"] == "passed"
    # Both dispatches were bought under the user's artifact, not a CLI-minted offline record.
    status = project.status_json(run_id)
    assert {item["authorization_id"] for item in status["invocations"]} == {"AUTH-e2e-cli-loop"}
    assert [item["role"] for item in status["invocations"]] == ["implementer", "reviewer"]
    assert project.tip() == project.base, "a run never moves a branch"

    prepared = project.integrate_prepare(run_id)
    integration = prepared["integration"]
    integration_id = integration["integration_id"]
    assert integration["mode"] == "squash"
    assert integration["target_tip"] == project.base
    assert prepared["handoff"] == [
        project.printed("integrate", "apply", integration_id, "--expect-target", project.base)
    ]
    assert project.tip() == project.base, "integrate prepare never moves the branch"

    applied = project.hflow(
        "integrate", "apply", integration_id, "--expect-target", project.base, "--json"
    )
    assert applied.returncode == 0, applied
    result = applied.json()
    assert result["integration"]["state"] == "integrated"
    assert result["integration"]["basis"] == "hflow_ref_update"
    assert result["receipt"]["delivery_state"] == "INTEGRATED"

    # The branch moved by exactly one commit, on the old tip, carrying the candidate's tree.
    tip = project.tip()
    assert tip == integration["integration_commit"]
    assert git(project.repo, "rev-list", "--count", f"{project.base}..{tip}").strip() == "1"
    assert git(project.repo, "rev-parse", f"{tip}^").strip() == project.base
    assert git(project.repo, "rev-parse", f"{tip}^{{tree}}").strip() == candidate["git_tree"]
    assert git(project.repo, "show", f"{tip}:app.txt") == "value = 2\n"
    # The user's checkout - branch, index, files, the uncommitted file - is untouched.
    assert project.user_state() == user_before

    # status and report, text and JSON: the run's own receipt unchanged, the integration beside it.
    status_text = project.hflow("status", run_id, "--project-root", str(project.repo))
    assert status_text.returncode == 0, status_text
    assert "delivery      LOCAL_CANDIDATE" in status_text.stdout
    assert (
        f"{integration_id}  state=integrated target=refs/heads/main" in status_text.stdout
    ), status_text
    assert "basis=hflow_ref_update" in status_text.stdout
    status = project.status_json(run_id)
    assert status["run"]["delivery_state"] == "LOCAL_CANDIDATE"
    assert [(item["integration_id"], item["state"]) for item in status["integrations"]] == [
        (integration_id, "integrated")
    ]
    report_text = project.hflow("report", run_id, "--project-root", str(project.repo))
    assert report_text.returncode == 0, report_text
    assert f"{integration_id}  state=integrated" in report_text.stdout, report_text
    report = project.hflow("report", run_id, "--project-root", str(project.repo), "--json")
    assert report.returncode == 0, report
    report_payload = report.json()
    assert report_payload["receipt"]["delivery_state"] == "LOCAL_CANDIDATE"
    assert [item["state"] for item in report_payload["integrations"]] == ["integrated"]
    assert report_payload["integrations"][0]["integration_commit"] == tip
    # Integration never dispatches: the run still has exactly its two invocations.
    assert report_payload["invocation_counts"] == status["invocation_counts"]
    assert len(report_payload["invocations"]) == 2


def test_a_checked_out_target_is_handed_off_and_the_operator_merge_is_reconciled(
    cli_project_on_main: CliProject,
) -> None:
    project = cli_project_on_main

    run_id = project.accept()["run_id"]
    prepared = project.integrate_prepare(run_id)
    integration = prepared["integration"]
    integration_id = integration["integration_id"]
    commit = integration["integration_commit"]

    applied = project.hflow(
        "integrate", "apply", integration_id, "--expect-target", project.base, "--json"
    )
    assert applied.returncode == 3, applied
    handed_off = applied.json()
    assert handed_off["integration"]["state"] == "ready"
    assert handed_off["receipt"] is None
    assert any("checked out" in note for note in handed_off["notes"]), handed_off
    merge, reconcile = handed_off["handoff"]
    assert merge.startswith("git -C ") and merge.endswith(f"merge --ff-only {commit}"), merge
    assert reconcile == project.printed("integrate", "reconcile", integration_id)
    assert project.tip() == project.base, "a checked-out branch is never moved by HFlow"
    assert (project.repo / "app.txt").read_text(encoding="utf-8") == "value = 1\n"

    # The operator runs the hand-off in their own checkout; their uncommitted file survives.
    git(project.repo, "merge", "--ff-only", "-q", commit)
    assert project.tip() == commit
    assert (project.repo / "scratch.txt").exists()

    reconciled = project.hflow("integrate", "reconcile", integration_id, "--json")
    assert reconciled.returncode == 0, reconciled
    settled = reconciled.json()
    assert settled["integration"]["state"] == "integrated"
    assert settled["integration"]["basis"] == "operator_merge_observed"
    assert settled["receipt"]["applied_by"] == "", "HFlow did not move it, so names nobody"
    assert any(
        "did not move the target" in item for item in settled["receipt"]["limitations"]
    )
    status = project.status_json(run_id)
    assert [(item["state"], item["basis"]) for item in status["integrations"]] == [
        ("integrated", "operator_merge_observed")
    ]
