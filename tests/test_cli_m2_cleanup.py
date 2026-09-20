"""M2 through the real CLI, including guarded workspace cleanup.

Everything here goes through ``hflow``'s own entry point (``cli.main``), never by calling the
controller directly, so the tests exercise what a user actually types. The fixture project is
a real Git repository in a temp directory.

The cleanup tests are as much about *refusing* as about removing: a directory removal is the
one operation that cannot be repaired by re-running something, so the interesting cases are
the ones that must not happen.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from hflow.gitworkspace import GitRepo, parse_status_z

SCRIPT_SOURCE = '''"""Tiny text utilities used as the CLI M2 sample."""


def normalise(text):
    """Return a normalised form of ``text``."""
    parts = text.split()
    return " ".join(parts)
'''

TEST_SOURCE = '''import pytest

from textkit import normalise


def test_collapses_whitespace():
    assert normalise("  a   b ") == "a b"


def test_none_input_is_rejected_cleanly():
    with pytest.raises(TypeError):
        normalise(None)
'''

FIXED_SOURCE = '''"""Tiny text utilities used as the CLI M2 sample."""


def normalise(text):
    """Return a normalised form of ``text``."""
    if text is None:
        raise TypeError("normalise() expects str, got None")
    parts = text.split()
    return " ".join(parts)
'''


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "Sample Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Sample Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


@pytest.fixture()
def cli_project(tmp_path: Path) -> dict[str, Path]:
    """A real Git project plus the TaskSpec/project/plan files the CLI reads."""
    repo = tmp_path / "sample"
    (repo / "src" / "textkit").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "textkit" / "__init__.py").write_text(SCRIPT_SOURCE, encoding="utf-8")
    (repo / "tests" / "test_textkit.py").write_text(TEST_SOURCE, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    # A realistic project ignores its own caches and local secrets. This is what makes the
    # cleanup policy meaningful: `.env` is ignored *and* must be refused, while a bytecode
    # cache is ignored and named in the artifact allowlist.
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.env\n", encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "sample project with a None-input bug")
    base = _git(repo, "rev-parse", "HEAD").strip()

    task = {
        "schema_version": 1,
        "task_id": "T-cli-m2",
        "revision": 1,
        "goal": "Reject None input in normalise() with a clear TypeError",
        "acceptance": [
            {"id": "AC-1", "statement": "None input raises TypeError", "check_ids": ["unit"]},
            {"id": "AC-2", "statement": "existing behaviour unchanged", "check_ids": ["unit"]},
        ],
        "scope": {"write_allow": ["src/textkit/__init__.py"], "write_deny": [".git/**"]},
        "risk": "standard",
        "reuse": {"status": "existing_decision", "reference": "project:stdlib-only"},
        "review": {"required": False},
        "budget": {"max_agent_turns": 4, "max_repair_cycles": 0},
        "workspace": {"mode": "worktree", "base_commit": base, "keep": True},
    }
    project = {
        "schema_version": 1,
        "project_id": "cli-sample",
        "checks": [
            {
                "id": "unit",
                "kind": "command",
                "argv": [sys.executable, "-m", "pytest", "-q", "tests"],
                "timeout_seconds": 300,
            }
        ],
        "write_deny": [".git/**", ".hflow/**"],
        "limits": {"max_agent_turns": 4, "max_repair_cycles": 0},
        "review_required": False,
    }
    files = {
        "repo": repo,
        "data": tmp_path / "data",
        "task": tmp_path / "task.json",
        "project": tmp_path / "project.json",
        "plan": tmp_path / "plan.json",
        "base": tmp_path / "base.txt",
    }
    files["task"].write_text(json.dumps(task, indent=2), encoding="utf-8")
    files["project"].write_text(json.dumps(project, indent=2), encoding="utf-8")
    files["plan"].write_text(
        json.dumps({"src/textkit/__init__.py": FIXED_SOURCE}), encoding="utf-8"
    )
    files["base"].write_text(base, encoding="utf-8")
    return files


def _run_cli(files: dict[str, Path], *extra: str, plan: Path | None = None) -> tuple[int, dict]:
    argv = [
        "run",
        "--task",
        str(files["task"]),
        "--project",
        str(files["project"]),
        "--project-root",
        str(files["repo"]),
        "--driver",
        "fake",
        "--fake-write-plan",
        str(plan or files["plan"]),
        "--data-dir",
        str(files["data"]),
        "--json",
        *extra,
    ]
    code = main(argv)
    return code, {}


def _cli_json(capsys: pytest.CaptureFixture[str], files: dict[str, Path], *argv: str) -> tuple[int, dict]:
    code = main([*argv, "--data-dir", str(files["data"]), "--json"])
    captured = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(captured[-1]) if captured else {}
    return code, payload


# --------------------------------------------------------------------------
# group 1: a successful M2 candidate, driven entirely by the CLI
# --------------------------------------------------------------------------


def test_cli_produces_a_frozen_candidate_and_reports_it(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo = cli_project["repo"]
    before_status = _git(repo, "status", "--porcelain")
    before_head = _git(repo, "rev-parse", "HEAD").strip()
    before_stash = _git(repo, "stash", "list")

    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["task_state"] == "ACCEPTED", payload.get("block_reason")
    receipt = payload["receipt"]

    assert len(receipt["candidate"]["git_commit"]) == 40
    assert len(receipt["candidate"]["git_tree"]) == 40
    assert receipt["candidate"]["base_commit"] == cli_project["base"].read_text(encoding="utf-8").strip()
    assert receipt["candidate"]["fingerprint"].startswith("sha256:")
    assert receipt["candidate_paths"] == ["src/textkit/__init__.py"]
    assert receipt["verification"]["status"] == "passed"
    assert receipt["delivery_state"] == "LOCAL_CANDIDATE"
    run_id = payload["run_id"]

    # status / report read the same facts back without a model or a re-run
    code, status = _cli_json(capsys, cli_project, "status", run_id, "--project-root", str(repo))
    assert code == EXIT_OK
    assert status["run"]["task_state"] == "ACCEPTED"
    assert status["model_calls_made"] == 0
    code, report = _cli_json(capsys, cli_project, "report", run_id, "--project-root", str(repo))
    assert code == EXIT_OK
    assert report["receipt"]["candidate"]["git_commit"] == receipt["candidate"]["git_commit"]
    assert report["receipt"]["candidate"]["worktree"]

    # the source repository is exactly as the user left it
    assert _git(repo, "status", "--porcelain") == before_status
    assert _git(repo, "rev-parse", "HEAD").strip() == before_head
    assert _git(repo, "stash", "list") == before_stash
    assert "if text is None" not in (repo / "src" / "textkit" / "__init__.py").read_text(
        encoding="utf-8"
    )

    # the candidate is reachable through an HFlow-owned ref, not only through the worktree
    ref = f"refs/hflow/candidates/{run_id}/"
    refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/hflow/")
    matching = [line for line in refs.splitlines() if line.startswith(ref)]
    assert matching, f"no candidate ref under {ref}: {refs!r}"
    assert receipt["candidate"]["git_commit"] in matching[0]

    # user branches were not touched: only main exists
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert branches == ["main"], branches


def test_cli_failed_candidate_is_rejected_and_kept(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A change that does not fix the bug must not be delivered, and must survive."""
    bad_plan = tmp_path / "bad-plan.json"
    bad_plan.write_text(
        json.dumps({"src/textkit/__init__.py": SCRIPT_SOURCE + "\n# touched, not fixed\n"}),
        encoding="utf-8",
    )
    code, _ = _run_cli(cli_project, plan=bad_plan)
    assert code == EXIT_BLOCKED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["task_state"] == "BLOCKED"
    assert payload["block_code"] == "verification_failed"
    assert payload["receipt"] is None, "a failed candidate must not produce a delivery receipt"

    # the scene is preserved, and the run's worktree is still registered
    repo = GitRepo.discover(cli_project["repo"])
    assert len(repo.worktree_list()) >= 4  # the source plus at least one kept candidate

    # a second identical submission does not dispatch again
    code2, _ = _run_cli(cli_project, plan=bad_plan)
    assert code2 == EXIT_BLOCKED
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["run_id"] == payload["run_id"], "the same TaskSpec must reuse the run"
    assert second["implementer_invocations"] == 1, "no second dispatch"
    assert len(GitRepo.discover(cli_project["repo"]).worktree_list()) == len(
        repo.worktree_list()
    ), "no second worktree may be created for the same TaskSpec"


# --------------------------------------------------------------------------
# group 2: routing and authorization
# --------------------------------------------------------------------------


def test_fake_driver_needs_no_dsh_or_acpx(
    cli_project: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The offline example must work with no Harness installed at all.

    A PATH holding only git and Python is the honest proof: the real client is a Node program
    resolved through PATH, so a fake run that still needed it would fail here.
    """
    git = shutil.which("git")
    assert git, "git must be installed for these tests"
    monkeypatch.setenv("PATH", str(Path(git).parent))
    monkeypatch.delenv("HFLOW_ACPX_CLI", raising=False)
    assert shutil.which("node") is None, "node must not be reachable in this test"
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["task_state"] == "ACCEPTED"
    assert any("fake driver" in note for note in payload["receipt"]["limitations"])


def test_real_driver_refuses_before_any_credential_or_workspace(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Without an authorization, a real driver refuses up front - and never falls back."""
    code = main(
        [
            "run",
            "--task",
            str(cli_project["task"]),
            "--project",
            str(cli_project["project"]),
            "--project-root",
            str(cli_project["repo"]),
            "--driver",
            "acpx-dsh",
            "--fake-write-plan",
            str(cli_project["plan"]),
            "--data-dir",
            str(cli_project["data"]),
            "--json",
        ]
    )
    assert code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["refused"] is True
    assert payload["reason"] == "live_authorization_missing"

    # nothing happened: no run, no worktree, no store writes for this task
    store_path = cli_project["data"] / "hflow.sqlite"
    if store_path.exists():
        import sqlite3

        connection = sqlite3.connect(str(store_path))
        try:
            rows = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
        finally:
            connection.close()
        assert rows == 0, "a refused real-driver run must not create run state"


def test_status_and_report_never_need_a_model(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """status/report are pure SQLite reads: they must not start any process at all."""
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("status/report must not start a process")

    monkeypatch.setattr(subprocess, "Popen", explode)
    code, status = _cli_json(capsys, cli_project, "status", run_id)
    assert code == EXIT_OK and status["model_calls_made"] == 0
    code, report = _cli_json(capsys, cli_project, "report", run_id)
    assert code == EXIT_OK
    assert report["receipt"]["candidate"]["git_commit"]
    # `clean` is allowed to shell out to git for read-only status queries; it must not need a
    # model, so the check here is that it works and reports a decision.
    monkeypatch.undo()
    code, preview = _cli_json(capsys, cli_project, "clean", run_id)
    assert code == EXIT_OK and "allowed" in preview


# --------------------------------------------------------------------------
# group 3: git status parsing
# --------------------------------------------------------------------------


def test_porcelain_parser_handles_real_repository_states(tmp_path: Path) -> None:
    """Every record kind is exercised against a real repository, not a fixed string."""
    repo = tmp_path / "g"
    (repo / "sub").mkdir(parents=True)
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "gone.txt").write_text("gone\n", encoding="utf-8")
    (repo / "sub" / "中文 文件.txt").write_text("unicode\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    # unstaged modification, staged addition, deletion, rename, unicode+space path, ignored
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")
    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    _git(repo, "mv", "sub/中文 文件.txt", "sub/renamed 文件.txt")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

    report = GitRepo.discover(repo).status_report(repo)
    changed = set(report.changed)

    assert "keep.txt" in changed, "unstaged modification lost"
    assert "added.txt" in changed, "new file lost"
    assert "gone.txt" in changed, "deletion lost"
    assert "sub/renamed 文件.txt" in changed, "rename target lost"
    assert "sub/中文 文件.txt" in changed, "rename source must participate in scope checks"
    assert report.ignored == (".env",), f"ignored set wrong: {report.ignored}"
    assert report.unsupported == ()
    assert all(not path.startswith((" ", "M ", "R ")) for path in report.changed), (
        "a status code leaked into a path"
    )


def test_porcelain_parser_refuses_unsupported_records() -> None:
    """Unmerged and submodule records are refused, never guessed."""
    report = parse_status_z("UU src/merged.py\0")
    assert report.changed == ()
    assert report.unsupported and "UU" in report.unsupported[0]
    submodule = parse_status_z("M  src/vendor\0")
    assert submodule.changed == ("src/vendor",), "a plain modified path is still a path"


# --------------------------------------------------------------------------
# group 4: cleanup preview
# --------------------------------------------------------------------------


def test_clean_preview_changes_nothing(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    repo = GitRepo.discover(cli_project["repo"])
    worktrees_before = repo.worktree_list()
    refs_before = _git(cli_project["repo"], "for-each-ref", "--format=%(refname)")

    code, preview = _cli_json(capsys, cli_project, "clean", run_id)
    assert code == EXIT_OK
    assert preview["allowed"] is True, preview["refusals"]
    worktree = Path(preview["path"])
    assert worktree.exists(), "the preview must not remove anything"
    assert preview["registered_worktree"] is True
    assert preview["head"] == preview["expected_candidate"]
    assert preview["candidate_ref_target"] == preview["expected_candidate"]
    assert any("candidate commit" in keep for keep in preview["keeps"])
    assert repo.worktree_list() == worktrees_before, "the preview must not touch git metadata"
    assert _git(cli_project["repo"], "for-each-ref", "--format=%(refname)") == refs_before, (
        "the preview must not create a ref"
    )

    # --dry-run is the same preview, and combining it with --apply is refused
    code, dry = _cli_json(capsys, cli_project, "clean", run_id, "--dry-run")
    assert code == EXIT_OK and dry["allowed"] is True
    code = main(["clean", run_id, "--apply", "--dry-run", "--data-dir", str(cli_project["data"])])
    assert code == EXIT_USAGE


# --------------------------------------------------------------------------
# group 5: dangerous cleanup is refused
# --------------------------------------------------------------------------


def _preview(files: dict[str, Path], capsys: pytest.CaptureFixture[str], run_id: str) -> dict:
    code, payload = _cli_json(capsys, files, "clean", run_id)
    assert code == EXIT_OK
    return payload


def test_clean_refuses_when_the_user_left_an_ignored_file(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])

    # An ignored file that is not a known build artifact: someone's local data.
    (worktree / ".env").write_text("DEEPSEEK_API_KEY=not-a-real-key\n", encoding="utf-8")

    refused = _preview(cli_project, capsys, run_id)
    assert refused["allowed"] is False
    assert any("ignored" in item["reason"] for item in refused["refusals"])
    assert any(".env" in item["detail"] for item in refused["refusals"])

    # apply also refuses, and the file is still there
    code = main(["clean", run_id, "--apply", "--data-dir", str(cli_project["data"]), "--json"])
    assert code == EXIT_BLOCKED
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["applied"] is False and result["status"] == "REFUSED"
    assert (worktree / ".env").exists(), "an unknown ignored file must never be deleted"
    assert worktree.exists()


def test_clean_refuses_on_unfrozen_changes_and_head_drift(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])
    scoped = worktree / "src" / "textkit" / "__init__.py"
    scoped.write_text(scoped.read_text(encoding="utf-8") + "\n# unfrozen edit\n", encoding="utf-8")

    refused = _preview(cli_project, capsys, run_id)
    assert refused["allowed"] is False
    assert any(item["reason"] == "unfrozen_changes" for item in refused["refusals"])

    # HEAD drift, with a clean tree, is also refused
    _git(worktree, "checkout", "--", ".")
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "drift")
    drifted = _preview(cli_project, capsys, run_id)
    assert drifted["allowed"] is False
    assert any(item["reason"] == "head_drift" for item in drifted["refusals"])


def test_clean_refuses_an_active_run(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """An active attempt is not removable, whatever the task state is called."""
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]

    import sqlite3

    connection = sqlite3.connect(str(cli_project["data"] / "hflow.sqlite"))
    try:
        connection.execute("UPDATE attempts SET state = 'ACTIVE' WHERE run_id = ?", (run_id,))
        connection.commit()
    finally:
        connection.close()

    refused = _preview(cli_project, capsys, run_id)
    assert refused["allowed"] is False
    assert any(item["reason"] == "execution_active" for item in refused["refusals"])


def test_clean_refuses_a_missing_or_foreign_workspace(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])

    # Pretend the path was redirected at something unrelated (a string prefix would match).
    import sqlite3

    other = cli_project["repo"].parent / "not-our-worktree"
    other.mkdir(exist_ok=True)
    connection = sqlite3.connect(str(cli_project["data"] / "hflow.sqlite"))
    try:
        connection.execute(
            "UPDATE runs SET worktree_path = ? WHERE run_id = ?", (str(other), run_id)
        )
        connection.commit()
    finally:
        connection.close()

    refused = _preview(cli_project, capsys, run_id)
    assert refused["allowed"] is False
    assert any(
        item["reason"] in {"not_registered", "git_query_failed", "not_a_worktree"}
        for item in refused["refusals"]
    ), refused["refusals"]
    assert worktree.exists(), "the real worktree is untouched by a foreign path claim"


# --------------------------------------------------------------------------
# group 6: apply, idempotency, reconcile
# --------------------------------------------------------------------------


def test_clean_apply_removes_only_this_runs_worktree_and_keeps_the_delivery(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    run_id = payload["run_id"]
    candidate = payload["receipt"]["candidate"]["git_commit"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])
    source = cli_project["repo"]
    other = source.parent / "unrelated-dir"
    other.mkdir(exist_ok=True)
    (other / "keep.txt").write_text("do not delete\n", encoding="utf-8")

    code = main(["clean", run_id, "--apply", "--data-dir", str(cli_project["data"]), "--json"])
    assert code == EXIT_OK
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["applied"] is True and result["status"] == "REMOVED"
    assert not worktree.exists(), "the run's worktree is gone"
    assert other.exists() and (other / "keep.txt").exists(), "nothing else was touched"
    assert source.exists()

    # the delivery survives: the ref still resolves and the commit is readable
    ref = result["candidate_ref"]
    assert _git(source, "rev-parse", ref).strip() == candidate
    assert _git(source, "cat-file", "-t", candidate).strip() == "commit"
    assert "if text is None" in _git(source, "show", f"{candidate}:src/textkit/__init__.py")

    # report still works, and reports the workspace as removed
    code, report = _cli_json(capsys, cli_project, "report", run_id, "--project-root", str(source))
    assert code == EXIT_OK
    assert report["receipt"]["candidate"]["git_commit"] == candidate

    # applying again is idempotent and does not remove anything else
    code = main(["clean", run_id, "--apply", "--data-dir", str(cli_project["data"]), "--json"])
    assert code == EXIT_OK
    again = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert again["applied"] is False and again["status"] == "REMOVED"
    assert other.exists(), "a repeated clean must not wander to other directories"

    # a repeated identical submission still returns the historical run: no rebuild
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    repeated = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert repeated["run_id"] == run_id
    assert repeated["implementer_invocations"] == 1
    assert not worktree.exists(), "the removed workspace must not be silently recreated"


def test_clean_reconcile_after_an_apply_that_did_not_finish_recording(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Simulate: files removed, state update lost. Reconcile decides from recorded facts."""
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])

    # Claim the intent, remove the directory behind HFlow's back, then reconcile.
    import sqlite3

    connection = sqlite3.connect(str(cli_project["data"] / "hflow.sqlite"))
    try:
        connection.execute(
            "UPDATE runs SET cleanup_intent_at = ?, worktree_state = 'REMOVING' WHERE run_id = ?",
            ("2026-01-01T00:00:00Z", run_id),
        )
        connection.commit()
    finally:
        connection.close()
    import shutil

    shutil.rmtree(worktree, ignore_errors=True)

    code, reconciled = _cli_json(capsys, cli_project, "clean", run_id, "--reconcile")
    assert code == EXIT_OK
    assert reconciled["status"] == "REMOVED"

    # a repeated identical submission still returns the historical run and does not rebuild
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    again = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert again["run_id"] == run_id
    assert again["implementer_invocations"] == 1
    assert not worktree.exists(), "a removed workspace must not be recreated by a resubmission"


def test_clean_reports_missing_when_the_path_vanished_without_a_cleanup(
    cli_project: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A vanished path with no cleanup record is MISSING, never a claimed success."""
    code, _ = _run_cli(cli_project)
    assert code == EXIT_OK
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    preview = _preview(cli_project, capsys, run_id)
    worktree = Path(preview["path"])

    shutil.rmtree(worktree, ignore_errors=True)

    code, reconciled = _cli_json(capsys, cli_project, "clean", run_id, "--reconcile")
    assert code == EXIT_OK
    assert reconciled["status"] == "MISSING"
    assert "unknown" in reconciled["detail"]

    refused = _preview(cli_project, capsys, run_id)
    assert refused["allowed"] is False
    assert refused["workspace_state"] == "MISSING"
