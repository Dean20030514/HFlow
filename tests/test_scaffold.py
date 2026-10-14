"""Batch L: ``hflow init project | profile | task`` (``hflow.scaffold``).

A scaffold writes one new file, never overwrites one, never writes a ``kind: fake`` check, and
never answers what admission leaves to a human: a scaffolded task is refused by ``hflow prepare``
until its operator fills those fields in, and admitted once they do. Nothing here calls a model;
``prepare`` runs with the offline fake driver.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hflow.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from hflow.contracts import MachineProfile, ProjectConfig, TaskSpec
from hflow.hints import shell_word
from hflow.profiles import load_profile
from hflow.scaffold import NPM_PROGRAM, PYTEST_ARGV, infer_checks

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sample Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Sample Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        timeout=60, check=False, env={**os.environ, **_GIT_IDENTITY},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _repo(tmp_path: Path, *, pyproject: str | None = None, branch: str = "main") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.txt").write_text("value = 1\n", encoding="utf-8")
    (repo / "check.py").write_text("print('ok')\n", encoding="utf-8")
    if pyproject is not None:
        (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _contract(repo: Path) -> ProjectConfig:
    return ProjectConfig.model_validate_json(
        (repo / ".hflow" / "project.json").read_text(encoding="utf-8")
    )


def _files(directory: Path) -> set[str]:
    return {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    }


# --------------------------------------------------------------------------
# init project
# --------------------------------------------------------------------------


def test_init_project_infers_pytest_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, pyproject="[tool.pytest.ini_options]\naddopts = '-q'\n")
    assert main(["init", "project", "--project-root", str(repo)]) == EXIT_OK
    out = capsys.readouterr().out
    target = repo / ".hflow" / "project.json"
    assert f"wrote {target.resolve()}" in out
    assert "next: hflow init task" in out
    contract = _contract(repo)
    assert contract.project_id == "repo"
    assert [(c.id, c.kind, c.argv) for c in contract.checks] == [
        ("pytest", "command", list(PYTEST_ARGV))
    ]
    assert contract.write_deny == [".git/**", ".hflow/**", ".github/**"]

    # The operator's own edit is never replaced: a second init is refused, byte for byte.
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    before = target.read_bytes()
    assert main(["init", "project", "--project-root", str(repo)]) == EXIT_REFUSED
    assert "already exists; init never overwrites" in capsys.readouterr().err
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {"pyproject.toml": '[project]\nname = "x"\ndependencies = ["pytest>=9,<10"]\n'},
            ["pytest"],
        ),
        (
            {"pyproject.toml": '[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n'},
            ["pytest"],
        ),
        ({"pytest.ini": "[pytest]\n"}, ["pytest"]),
        ({"package.json": json.dumps({"scripts": {"test": "vitest run"}})}, ["npm-test"]),
        (
            {
                "pyproject.toml": "[tool.pytest.ini_options]\n",
                "package.json": json.dumps({"scripts": {"test": "jest"}}),
            },
            ["pytest", "npm-test"],
        ),
        # Nothing a repository declares as a test runner: nothing is guessed.
        ({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["pytest-cov"]\n'}, []),
        (
            {
                "package.json": json.dumps(
                    {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
                )
            },
            [],
        ),
        ({"pyproject.toml": "not toml ["}, []),
        ({}, []),
    ],
    ids=[
        "deps", "group", "ini", "npm", "both", "pytest-cov", "npm-placeholder", "bad-toml", "none",
    ],
)
def test_checks_are_inferred_only_from_what_the_repository_declares(
    tmp_path: Path, files: dict[str, str], expected: list[str]
) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    checks = infer_checks(tmp_path)
    assert [check.id for check in checks] == expected
    assert all(check.kind == "command" and check.argv for check in checks)
    for check in checks:
        if check.id == "npm-test":
            assert check.argv == [NPM_PROGRAM, "test"]


def test_nothing_inferable_requires_named_checks_and_never_a_fake_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    assert main(["init", "project", "--project-root", str(repo)]) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "--check ID=COMMAND" in err and "kind=fake" in err
    assert not (repo / ".hflow").exists(), "a refusal writes nothing"

    json_argv = json.dumps([sys.executable, "check.py"])
    assert main([
        "init", "project", "--project-root", str(repo), "--project-id", "demo",
        "--check", "unit=python -m pytest -q tests",
        "--check", f"script={json_argv}",
        "--check", "fake=fake",  # a command named "fake" is still a command, never kind=fake
    ]) == EXIT_OK
    capsys.readouterr()
    contract = _contract(repo)
    assert contract.project_id == "demo"
    assert [(c.id, c.kind, c.argv) for c in contract.checks] == [
        ("unit", "command", ["python", "-m", "pytest", "-q", "tests"]),
        ("script", "command", [sys.executable, "check.py"]),
        ("fake", "command", ["fake"]),
    ]
    assert '"kind": "fake"' not in (repo / ".hflow" / "project.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "check", ["no-equals", "=python", "unit=", "unit=[1, 2]", "unit='unterminated"]
)
def test_a_check_that_cannot_be_read_is_refused_and_nothing_written(
    tmp_path: Path, check: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "project", "--project-root", str(tmp_path), "--check", check]) == (
        EXIT_REFUSED
    )
    assert "refused: invalid_spec" in capsys.readouterr().err
    assert not (tmp_path / ".hflow").exists()


def test_duplicate_check_ids_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["init", "project", "--project-root", str(tmp_path)]
    assert main([*argv, "--check", "a=x", "--check", "a=y"]) == EXIT_REFUSED
    assert "check ids must be unique: a" in capsys.readouterr().err
    assert not (tmp_path / ".hflow").exists()


# --------------------------------------------------------------------------
# init profile
# --------------------------------------------------------------------------


def test_init_profile_writes_the_documented_two_role_profile_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    assert main(["init", "profile", "dsh-local", "--data-dir", str(data)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "next: hflow doctor --profile dsh-local --data-dir" in out
    profile = load_profile(data, "dsh-local")  # the loader every command uses
    assert profile.role_bindings == {"implementer": "dsh-worker", "reviewer": "dsh-reviewer"}
    for agent in profile.agents.values():
        assert (agent.harness, agent.driver, agent.model_selection) == (
            "dsh", "acpx-dsh", "native_profile"
        )
    assert profile.limits.max_parallel_workers == 1 and profile.limits.max_native_children == 0
    assert profile.security_mode == "trusted_local"

    path = data / "profiles" / "dsh-local.json"
    before = path.read_bytes()
    assert main(["init", "profile", "dsh-local", "--data-dir", str(data)]) == EXIT_REFUSED
    assert "never overwrites" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert MachineProfile.model_validate_json(before) == profile


@pytest.mark.parametrize("bad", ["../escape", "C:evil", "CON", "a b", ""])
def test_a_profile_id_that_is_not_a_plain_name_is_refused(
    tmp_path: Path, bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    assert main(["init", "profile", bad, "--data-dir", str(data)]) == EXIT_REFUSED
    assert "not a plain file name" in capsys.readouterr().err
    assert not data.exists() or _files(data) == set()


def test_the_default_data_dir_is_not_repeated_in_the_next_command(
    capsys: pytest.CaptureFixture[str], _isolated_default_data_dir: Path
) -> None:
    assert main(["init", "profile", "p1"]) == EXIT_OK
    assert "next: hflow doctor --profile p1\n" in capsys.readouterr().out
    assert (_isolated_default_data_dir / "profiles" / "p1.json").is_file()


# --------------------------------------------------------------------------
# init task
# --------------------------------------------------------------------------


def _prepare(task: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = main(["prepare", "--task", str(task), "--project-root", str(repo), "--driver", "fake"])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _edit(task: Path, **changes) -> None:  # noqa: ANN003
    document = json.loads(task.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        target = document
        *parents, leaf = dotted.split("__")
        for name in parents:
            target = target[name]
        target[leaf] = value
    task.write_text(json.dumps(document, indent=2), encoding="utf-8")


@pytest.fixture()
def scaffolded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """A Git repository with a scaffolded contract (one real command check)."""
    repo = _repo(tmp_path, branch="trunk")
    json_argv = json.dumps([sys.executable, "check.py"])
    assert main(
        ["init", "project", "--project-root", str(repo), "--check", f"unit={json_argv}"]
    ) == EXIT_OK
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    capsys.readouterr()
    return repo, work


def test_a_scaffolded_task_is_refused_until_its_human_fields_are_filled(
    scaffolded, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, work = scaffolded
    assert main(["init", "task", "T-1", "--project-root", str(repo)]) == EXIT_OK
    out = capsys.readouterr().out
    task = work / "T-1.task.json"
    assert f"wrote {task.resolve()}" in out
    for field in ("todo: goal", "todo: scope.write_allow", "todo: reuse: left unanswered"):
        assert field in out, field
    assert "next: hflow prepare --task" in out and "--plan-out" in out
    # A scaffold writes the task and nothing else: no plan, no budget, no authorization.
    assert _files(work) == {"T-1.task.json"}

    document = json.loads(task.read_text(encoding="utf-8"))
    assert document["goal"] == "" and document["scope"]["write_allow"] == []
    assert document["acceptance"] == [
        {
            "id": "AC-1",
            "statement": "the project's approved checks pass: unit",
            "check_ids": ["unit"],
        }
    ]
    assert document["workspace"] == {"mode": "worktree", "base_commit": "trunk", "keep": False}
    assert document["reuse"]["status"] == "decided"
    assert document["reuse"]["choice"] == "none"
    assert document["reuse"]["fit_test_status"] == "pending"
    assert "repair_policy" not in document

    code, text = _prepare(task, repo, capsys)
    assert code == EXIT_REFUSED and "goal must be non-empty" in text

    _edit(task, goal="Set value to 2 in app.txt")
    code, text = _prepare(task, repo, capsys)
    assert code == EXIT_REFUSED
    assert "write_allow must list at least one path" in text
    assert "reuse.choice" in text and "reuse.need" in text

    _edit(task, scope__write_allow=["app.txt"])
    code, text = _prepare(task, repo, capsys)
    assert code == EXIT_REFUSED and "write_allow must list at least one path" not in text
    assert "reuse.choice" in text

    # Choosing to reuse a component does not answer its fit test: still refused.
    _edit(task, reuse__choice="reuse", reuse__need="an existing parser")
    code, text = _prepare(task, repo, capsys)
    assert code == EXIT_REFUSED and "reuse.fit_test_status" in text

    _edit(task, reuse__fit_test_status="passed", reuse__required_fit_test="parses the fixtures")
    code, text = _prepare(task, repo, capsys)
    assert code == EXIT_OK, text
    assert "no admission problem found" in text
    TaskSpec.model_validate_json(task.read_text(encoding="utf-8"))
    assert _files(work) == {"T-1.task.json"}, "prepare without --plan-out writes nothing"


def test_a_task_with_goal_and_scope_waits_only_for_the_reuse_decision(
    scaffolded, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, work = scaffolded
    out_file = work / "tasks" / "first.task.json"
    assert main([
        "init", "task", "T-2", "--project-root", str(repo), "--goal", "Set value to 2",
        "--allow", "app.txt", "check.py", "--allow", "notes.txt", "--out", str(out_file),
    ]) == EXIT_OK
    out = capsys.readouterr().out
    assert "todo: goal" not in out and "todo: scope.write_allow" not in out
    assert "todo: reuse" in out
    document = json.loads(out_file.read_text(encoding="utf-8"))
    assert document["scope"]["write_allow"] == ["app.txt", "check.py", "notes.txt"]
    assert document["budget"] == {"max_agent_turns": 2, "max_repair_cycles": 0}

    code, text = _prepare(out_file, repo, capsys)
    assert code == EXIT_REFUSED and "reuse_not_approved" in text
    _edit(out_file, reuse__status="existing_decision", reuse__reference="project:none")
    code, text = _prepare(out_file, repo, capsys)
    assert code == EXIT_OK, text

    before = out_file.read_bytes()
    assert main([
        "init", "task", "T-2", "--project-root", str(repo), "--out", str(out_file)
    ]) == EXIT_REFUSED
    assert "never overwrites" in capsys.readouterr().err
    assert out_file.read_bytes() == before


def test_a_task_is_wired_only_to_command_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps({
            "project_id": "mixed",
            "checks": [
                {"id": "docs", "kind": "fake"},
                {"id": "unit", "kind": "command", "argv": [sys.executable, "check.py"]},
            ],
        }),
        encoding="utf-8",
    )
    assert main([
        "init", "task", "T-3", "--project-root", str(repo), "--project", str(contract),
    ]) == EXIT_OK
    out = capsys.readouterr().out
    assert "not wired (kind=fake, refused for a real delivery): docs" in out
    assert f"--project {shell_word(str(contract.resolve()))}" in out
    document = json.loads((tmp_path / "T-3.task.json").read_text(encoding="utf-8"))
    assert document["acceptance"][0]["check_ids"] == ["unit"]

    fake_only = tmp_path / "fake-only.json"
    fake_only.write_text(
        json.dumps({"project_id": "f", "checks": [{"id": "docs", "kind": "fake"}]}),
        encoding="utf-8",
    )
    assert main([
        "init", "task", "T-4", "--project-root", str(repo), "--project", str(fake_only),
    ]) == EXIT_REFUSED
    assert "approves no kind=command check" in capsys.readouterr().err
    assert not (tmp_path / "T-4.task.json").exists()


def test_init_task_needs_a_contract_and_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    # Git must not find a repository enclosing the temp directory.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["init", "task", "T-5", "--project-root", str(plain)]) == EXIT_REFUSED
    assert "run `hflow init project` first" in capsys.readouterr().err

    assert main([
        "init", "project", "--project-root", str(plain), "--check", "unit=python -V",
    ]) == EXIT_OK
    capsys.readouterr()
    assert main(["init", "task", "T-5", "--project-root", str(plain)]) == EXIT_REFUSED
    assert "is not a Git repository with a commit" in capsys.readouterr().err
    assert not (tmp_path / "T-5.task.json").exists()

    assert main(["init", "task", "../T-6", "--project-root", str(plain)]) == EXIT_REFUSED
    assert "not a plain name" in capsys.readouterr().err


def test_a_detached_head_is_named_by_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "switch", "-q", "--detach", head)
    assert main(["init", "project", "--project-root", str(repo), "--check", "u=python -V"]) == 0
    monkeypatch.chdir(tmp_path)
    assert main(["init", "task", "T-7", "--project-root", str(repo)]) == EXIT_OK
    capsys.readouterr()
    document = json.loads((tmp_path / "T-7.task.json").read_text(encoding="utf-8"))
    assert document["workspace"]["base_commit"] == head


def test_init_needs_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as usage:
        main(["init"])
    assert usage.value.code == EXIT_USAGE
    capsys.readouterr()
