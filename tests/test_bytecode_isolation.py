"""Checks verify the committed source, not bytecode a worker left beside it.

Ignored bytecode is never staged, so a check that reads it verifies bytes no candidate commit
holds. Two shapes were reproduced against a first round, which nothing refused before:

* a sourceless ``src/helper.pyc``: Python imports it as ``src.helper``, the scoped fingerprint
  hashed it, the commit did not hold it - a clean checkout of the candidate fails to import;
* a ``src/__pycache__/app.<tag>.pyc`` compiled with ``UNCHECKED_HASH`` from other source: Python
  loads it in place of the committed ``app.py`` without looking at the source.

The first is refused at the freeze, in every round. The second is removed: before every command
check of a worktree run, the ``.pyc`` files in the worktree's real ``__pycache__`` directories are
deleted (they are never committed and never fingerprinted). That holds for a check that runs
Python with ``-I`` or ``-E``, which an environment variable such as ``PYTHONPYCACHEPREFIX`` does
not. An in-place run works in the user's checkout and deletes nothing there.
"""
from __future__ import annotations

import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    EvidenceStatus,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.gitworkspace import (
    IGNORED_ARTIFACT_ALLOWLIST,
    GitRepo,
    GitStatusParseError,
    is_sourceless_bytecode,
)
from hflow.store import Store
from hflow.verify import (
    REASON_BYTECODE_NOT_CLEARED,
    CheckRunners,
    CommandCheckRunner,
    remove_worker_bytecode,
    verify_candidate,
)
from hflow.workspace import candidate_fingerprint, expand_scope
from tests.test_check_resources import _reason_spec, _seed_checking_run

APP_SOURCE = "def answer():\n    return 1\n"
FORGED_SOURCE = "def answer():\n    return 42\n"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
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
def py_repo(tmp_path: Path) -> Path:
    """A committed package ``src`` whose repository ignores bytecode, as most Python repos do."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".gitignore").write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _compile(source: str, target: Path, tmp_path: Path, *, unchecked: bool = False) -> None:
    scratch = tmp_path / f"scratch-{target.stem}.py"
    scratch.write_text(source, encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (
        py_compile.PycInvalidationMode.UNCHECKED_HASH
        if unchecked
        else py_compile.PycInvalidationMode.TIMESTAMP
    )
    py_compile.compile(str(scratch), cfile=str(target), doraise=True, invalidation_mode=mode)


def _fingerprinted(worktree: Path, scope: Scope) -> set[str]:
    root = Path(os.path.realpath(worktree))
    return {path.relative_to(root).as_posix() for path in expand_scope(root, scope)}


def _cache_file(module: Path) -> Path:
    return module.parent / "__pycache__" / f"{module.stem}.{sys.implementation.cache_tag}.pyc"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/helper.pyc", True),
        ("helper.PYC", True),
        ("src/__pycache__/app.cpython-314.pyc", False),
        ("src/__PyCache__/app.cpython-314.pyc", False),
        ("src/app.py", False),
        ("src/__pycache__/", False),
    ],
)
def test_sourceless_bytecode_is_a_pyc_outside_any_pycache_directory(
    path: str, expected: bool
) -> None:
    assert is_sourceless_bytecode(path) is expected


def test_sourceless_bytecode_inside_write_allow_refuses_the_freeze(
    py_repo: Path, tmp_path: Path
) -> None:
    repo = GitRepo.discover(py_repo)
    base = repo.head
    worktree = repo.create_worktree("R-sourceless-in-scope", base)
    (worktree / "src" / "app.py").write_text(APP_SOURCE + "# edited\n", encoding="utf-8")
    _compile("def helper():\n    return 'ok'\n", worktree / "src" / "helper.pyc", tmp_path)
    scope = Scope(write_allow=["src"])
    assert "src/helper.pyc" in _fingerprinted(worktree, scope), "the fingerprint hashes it"

    with pytest.raises(GitStatusParseError, match=r"ignored file\(s\) inside write_allow"):
        repo.freeze_candidate(
            worktree,
            ["src"],
            allow_ignored=list(IGNORED_ARTIFACT_ALLOWLIST),
            fingerprinted=_fingerprinted(worktree, scope),
            expected_head=base,
        )
    assert repo.worktree_commit(worktree) == base, "nothing was committed"


def test_sourceless_bytecode_outside_the_scope_refuses_the_freeze_too(
    py_repo: Path, tmp_path: Path
) -> None:
    repo = GitRepo.discover(py_repo)
    base = repo.head
    worktree = repo.create_worktree("R-sourceless-out-of-scope", base)
    (worktree / "src" / "app.py").write_text(APP_SOURCE + "# edited\n", encoding="utf-8")
    _compile("def helper():\n    return 'ok'\n", worktree / "tools" / "helper.pyc", tmp_path)

    with pytest.raises(GitStatusParseError, match="sourceless bytecode outside __pycache__"):
        repo.freeze_candidate(
            worktree,
            ["src"],
            allow_ignored=list(IGNORED_ARTIFACT_ALLOWLIST),
            fingerprinted=_fingerprinted(worktree, Scope(write_allow=["src"])),
            expected_head=base,
        )
    assert repo.worktree_commit(worktree) == base


def test_a_bytecode_cache_in_pycache_is_still_allowed_at_the_freeze(
    py_repo: Path, tmp_path: Path
) -> None:
    """A worker legitimately runs the tests, which leaves ``__pycache__`` behind."""
    repo = GitRepo.discover(py_repo)
    base = repo.head
    worktree = repo.create_worktree("R-pycache-allowed", base)
    (worktree / "src" / "app.py").write_text(APP_SOURCE + "# edited\n", encoding="utf-8")
    _compile(APP_SOURCE, _cache_file(worktree / "src" / "app.py"), tmp_path)

    freeze = repo.freeze_candidate(
        worktree,
        ["src"],
        allow_ignored=list(IGNORED_ARTIFACT_ALLOWLIST),
        fingerprinted=_fingerprinted(worktree, Scope(write_allow=["src"])),
        expected_head=base,
    )
    assert freeze.paths == ("src/app.py",)


def test_a_first_round_with_sourceless_bytecode_is_blocked_before_any_check(
    py_repo: Path, tmp_path: Path
) -> None:
    """The controller's first round applies the same refusal a repair round always had."""
    marker = tmp_path / "check-ran"
    project = ProjectConfig(
        project_id="bytecode-project",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('x')",
                ],
                timeout_seconds=120,
            )
        ],
        write_deny=[".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )
    task = TaskSpec(
        task_id="T-bytecode",
        revision=1,
        goal="Edit app.py",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"])],
        scope=Scope(write_allow=["src"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:stdlib",
            reason="standard library only",
        ),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
        workspace=WorkspaceSpec(
            mode="worktree", base_commit=_git(py_repo, "rev-parse", "HEAD").strip()
        ),
    )
    script = FakeScript(
        write_plan={
            "src/app.py": APP_SOURCE + "# edited\n",
            # Content does not matter to the refusal; the name and the ignore rule do.
            "src/helper.pyc": "not really bytecode\n",
        },
        agent_turns=1,
    )
    store = Store(tmp_path / "data" / "hflow.sqlite")
    controller = Controller(
        store,
        FakeDriver(py_repo, script),
        controller_build="bytecode-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(task=task, project=project, project_root=py_repo, workspace_root=py_repo)
        )
    finally:
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert "candidate freeze refused" in (outcome.block_reason or "")
    assert "src/helper.pyc" in (outcome.block_reason or "")
    assert outcome.receipt is None
    assert not marker.exists(), "no check ran on a candidate the commit does not hold"


FLAG_VARIANTS = pytest.mark.parametrize(
    "flags", [[], ["-I"], ["-E"]], ids=["plain", "isolated", "ignore-environment"]
)


def _import_probe(flags: list[str]) -> list[str]:
    # ``-I`` puts neither the script directory nor the working directory on ``sys.path``, so the
    # probe adds the working directory itself, as a test runner's rootdir handling would.
    return [
        sys.executable,
        *flags,
        "-c",
        "import sys; sys.path.insert(0, '.'); import src.app; "
        "sys.exit(0 if src.app.answer() == 1 else 3)",
    ]


def _make_link(link: Path, target: Path) -> None:
    """A directory junction on Windows (no privilege needed), a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@FLAG_VARIANTS
def test_a_planted_cache_is_loaded_whatever_the_flags_until_it_is_removed(
    py_repo: Path, tmp_path: Path, flags: list[str]
) -> None:
    """The threat, and the defence, at the runner level: no environment variable is relied on."""
    cache = _cache_file(py_repo / "src" / "app.py")
    _compile(FORGED_SOURCE, cache, tmp_path, unchecked=True)
    check = CheckDef(id="unit", kind="command", argv=_import_probe(flags), timeout_seconds=60)

    planted = CommandCheckRunner().run(check, py_repo, 60)
    assert planted.status is EvidenceStatus.FAILED, "the forged bytecode stood in for app.py"
    assert planted.exit_code == 3

    # The check may have cached ``src/__init__`` as well; every .pyc in the cache goes.
    removal = remove_worker_bytecode(py_repo)
    assert removal.files >= 1 and not removal.refused
    assert not cache.exists()

    cleaned = CommandCheckRunner().run(check, py_repo, 60)
    assert cleaned.status is EvidenceStatus.PASSED, cleaned.detail
    assert "PYTHONPYCACHEPREFIX" not in str(cleaned.environment)


class _PlantingDriver(FakeDriver):
    """A fake worker that also leaves forged ``UNCHECKED_HASH`` bytecode for ``src/app.py``."""

    def __init__(self, project_root: Path, script: FakeScript, scratch: Path) -> None:
        super().__init__(project_root, script)
        self.scratch = scratch
        self.planted: list[tuple[Path, bytes]] = []
        self.fingerprints: list[str] = []

    def start(self, request):  # type: ignore[no-untyped-def]
        result = super().start(request)
        if self.planted:
            return result  # only the implementer's first turn plants; later calls are other roles
        worktree = Path(request.workspace)
        cache = _cache_file(worktree / "src" / "app.py")
        _compile(FORGED_SOURCE, cache, self.scratch, unchecked=True)
        self.planted.append((cache, cache.read_bytes()))
        self.fingerprints.append(candidate_fingerprint(worktree, Scope(write_allow=["src"])))
        return result


def test_a_worktree_run_removes_planted_bytecode_before_every_check(
    py_repo: Path, tmp_path: Path
) -> None:
    """Plain, ``-I`` and ``-E`` checks all see the committed ``app.py``; neither identity moves."""
    checks = [
        CheckDef(id=name, kind="command", argv=_import_probe(flags), timeout_seconds=120)
        for name, flags in (("plain", []), ("isolated", ["-I"]), ("noenv", ["-E"]))
    ]
    project = ProjectConfig(
        project_id="bytecode-project",
        checks=checks,
        write_deny=[".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )
    task = TaskSpec(
        task_id="T-bytecode-removed",
        revision=1,
        goal="Edit app.py",
        acceptance=[
            AcceptanceCriterion(
                id="AC-1", statement="every check passes", check_ids=[c.id for c in checks]
            )
        ],
        scope=Scope(write_allow=["src"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:stdlib",
            reason="standard library only",
        ),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
        workspace=WorkspaceSpec(
            mode="worktree", base_commit=_git(py_repo, "rev-parse", "HEAD").strip(), keep=True
        ),
    )
    driver = _PlantingDriver(
        py_repo,
        FakeScript(write_plan={"src/app.py": APP_SOURCE + "# edited\n"}, agent_turns=1),
        tmp_path,
    )
    store = Store(tmp_path / "data" / "hflow.sqlite")
    controller = Controller(
        store,
        driver,
        controller_build="bytecode-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(task=task, project=project, project_root=py_repo, workspace_root=py_repo)
        )
        run_id = outcome.run_id
        rows = [dict(row) for row in store.evidence_for(run_id, "verification")]
        notes = store.notes_for(run_id)
    finally:
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    receipt = outcome.receipt
    assert receipt is not None
    assert {row["check_id"]: row["status"] for row in rows} == {
        "plain": "passed",
        "isolated": "passed",
        "noenv": "passed",
    }
    # The removal is recorded with the evidence and in the run's notes.
    assert "worker-left bytecode removed before this check: 1 ignored .pyc file(s)" in str(
        rows[0]["detail"]
    )
    assert all("worker-left bytecode removed before this check" in str(r["detail"]) for r in rows)
    assert any(note.startswith("bytecode_removed: ") for note in notes)

    # Neither identity of the candidate moved: the fingerprint is the one taken while the forged
    # cache was present, and the worktree is still exactly the frozen commit.
    worktree = Path(receipt.candidate.worktree)
    # Whatever sits at the planted path now is the checks' own cache of the committed source.
    assert len(driver.planted) == 1
    planted_path, planted_bytes = driver.planted[0]
    assert not planted_path.exists() or planted_path.read_bytes() != planted_bytes
    assert receipt.candidate.fingerprint == driver.fingerprints[0]
    assert receipt.candidate.fingerprint == candidate_fingerprint(
        worktree, Scope(write_allow=["src"])
    )
    assert _git(worktree, "rev-parse", "HEAD").strip() == receipt.candidate.git_commit
    assert _git(worktree, "status", "--porcelain").strip() == ""
    committed = _git(worktree, "ls-tree", "-r", "--name-only", receipt.candidate.git_commit)
    assert ".pyc" not in committed


def test_an_in_place_run_deletes_no_bytecode_in_the_users_checkout(
    py_repo: Path, tmp_path: Path
) -> None:
    """In-place runs get no bytecode protection: the user's checkout is never cleaned."""
    cache = _cache_file(py_repo / "src" / "app.py")
    _compile(APP_SOURCE, cache, tmp_path, unchecked=True)
    project = ProjectConfig(
        project_id="bytecode-project",
        checks=[CheckDef(id="unit", kind="command", argv=_import_probe([]), timeout_seconds=120)],
        write_deny=[".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )
    task = TaskSpec(
        task_id="T-bytecode-in-place",
        revision=1,
        goal="Edit app.py",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"])],
        scope=Scope(write_allow=["src"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:stdlib",
            reason="standard library only",
        ),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
    )
    store = Store(tmp_path / "data" / "hflow.sqlite")
    controller = Controller(
        store,
        FakeDriver(py_repo, FakeScript(write_plan={"src/app.py": APP_SOURCE}, agent_turns=1)),
        controller_build="bytecode-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(task=task, project=project, project_root=py_repo, workspace_root=py_repo)
        )
        rows = [dict(row) for row in store.evidence_for(outcome.run_id, "verification")]
    finally:
        store.close()

    assert rows, outcome.block_reason
    assert cache.exists(), "nothing in the user's checkout was deleted"
    assert all("worker-left bytecode" not in str(row["detail"]) for row in rows)


def test_a_pycache_junction_is_neither_followed_nor_emptied(py_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    forged = outside / f"app.{sys.implementation.cache_tag}.pyc"
    _compile(FORGED_SOURCE, forged, tmp_path, unchecked=True)
    _make_link(py_repo / "src" / "__pycache__", outside)
    real = _cache_file(py_repo / "pkg" / "mod.py")
    _compile("VALUE = 1\n", real, tmp_path)
    (real.parent / "notes.txt").write_text("not bytecode\n", encoding="utf-8")

    removal = remove_worker_bytecode(py_repo)

    assert forged.exists(), "nothing behind the junction was touched"
    assert (py_repo / "src" / "__pycache__").exists()
    assert len(removal.refused) == 1 and removal.refused[0].startswith("src/__pycache__ ")
    # A real cache next to it is still cleaned, and only its .pyc files.
    assert removal.files == 1 and not real.exists()
    assert (real.parent / "notes.txt").exists() and removal.directories == 0


def test_a_check_is_not_started_while_a_pycache_link_remains(
    py_repo: Path, tmp_path: Path
) -> None:
    """Python would follow the junction to the forged bytecode, so the check refuses to run."""
    outside = tmp_path / "outside"
    forged = outside / f"app.{sys.implementation.cache_tag}.pyc"
    _compile(FORGED_SOURCE, forged, tmp_path, unchecked=True)
    _make_link(py_repo / "src" / "__pycache__", outside)
    marker = tmp_path / "check-ran"
    project = ProjectConfig(
        project_id="bytecode-link",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('x')",
                ],
                timeout_seconds=60,
            )
        ],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=1),
        review_required=False,
    )
    spec = _reason_spec(["unit"])
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        result = verify_candidate(
            store=store,
            spec=spec,
            project=project,
            project_root=py_repo,
            project_checks_digest=project.checks_digest(),
            candidate_fingerprint="sha256:link",
            attempt_id="A-reason",
            run_id=run_id,
            runners=CheckRunners.offline_default(),
            remove_bytecode=True,
        )
        row = dict(store.evidence_for(run_id, "verification")[0])
    finally:
        store.close()

    assert result.status == "failed"
    assert row["status"] == EvidenceStatus.ERROR.value
    assert row["exit_reason"] == REASON_BYTECODE_NOT_CLEARED
    assert "src/__pycache__" in str(row["detail"])
    assert not marker.exists(), "the check was not started"
    assert forged.exists()


def test_bytecode_the_candidate_commit_tracks_is_kept(py_repo: Path, tmp_path: Path) -> None:
    tracked = _cache_file(py_repo / "src" / "app.py")
    _compile(APP_SOURCE, tracked, tmp_path)
    _git(py_repo, "add", "-f", tracked.relative_to(py_repo).as_posix())
    _git(py_repo, "commit", "-q", "-m", "commit a cache")
    stray = _cache_file(py_repo / "src" / "other.py")
    _compile("X = 1\n", stray, tmp_path)

    committed = GitRepo.discover(py_repo).committed_bytecode_paths("HEAD")
    assert committed == [tracked.relative_to(py_repo).as_posix()]

    removal = remove_worker_bytecode(py_repo, keep=committed)
    assert tracked.exists() and not stray.exists()
    assert removal.files == 1 and removal.directories == 0
    assert _git(py_repo, "status", "--porcelain").strip() == ""


PARSER_SOURCE = "def parse(text):\n    return text\n"
PARSER_FIXED = "def parse(text):\n    if text is None:\n        return ''\n    return text\n"
PARSER_FORGED = "def parse(text):\n    return ''\n"


def test_a_repair_round_still_reconciles_after_the_caches_were_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round one's planted cache is removed, so its check fails honestly and buys the repair.

    The repair's reconcile and freeze accept the caches round one's check wrote, and the cache
    the repair worker planted is removed before its own check too.
    """
    from tests.test_batch_e_repair import RepairingDriver, _controller, _policy, _request

    # Command checks make this a production-shaped run, which needs the write opt-in.
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "1")
    repo = tmp_path / "sample"
    (repo / "src").mkdir(parents=True)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "parser.py").write_text(PARSER_SOURCE, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    probe = (
        "import sys; sys.path.insert(0, '.'); from src.parser import parse; "
        "sys.exit(0 if parse(None) == '' else 1)"
    )
    project = ProjectConfig(
        project_id="repair-project",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[sys.executable, "-I", "-c", probe],
                timeout_seconds=120,
            )
        ],
        write_deny=[".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=1),
        review_required=False,
    )
    spec = TaskSpec(
        task_id="T-REPAIR",
        revision=1,
        goal="Make parse(None) return an empty string",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"])],
        scope=Scope(write_allow=["src/parser.py"], write_deny=[".hflow/**"]),
        risk="standard",
        reuse=ReuseDecision(status=ReuseStatus.EXISTING_DECISION, reference="local", reason="t"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=1),
        workspace=WorkspaceSpec(mode="worktree", base_commit=base, keep=True),
        repair_policy=_policy(allow_reviewer_changes=False),
    )

    class PlantingRepairDriver(RepairingDriver):
        def start(self, request):  # type: ignore[no-untyped-def]
            result = super().start(request)
            if request.role == "implementer":
                cache = _cache_file(Path(request.workspace) / "src" / "parser.py")
                _compile(PARSER_FORGED, cache, tmp_path, unchecked=True)
            return result

    driver = PlantingRepairDriver(repo, first_plan={}, repair_plan={"src/parser.py": PARSER_FIXED})
    store = Store(tmp_path / "hflow.sqlite")
    controller = _controller(
        store,
        project_root=repo,
        spec=spec,
        driver=driver,
        runners=CheckRunners.offline_default(),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=repo))
        rows = [dict(row) for row in store.evidence_for(outcome.run_id, "verification")]
    finally:
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert driver.labels[:2] == ["implementer", "implementer-repair"]
    assert [row["status"] for row in rows] == ["failed", "passed"]
    # Round one removes the planted file; round two also removes what round one's check cached.
    removed = [
        int(re.search(r"removed before this check: (\d+) ignored", str(r["detail"])).group(1))  # type: ignore[union-attr]
        for r in rows
    ]
    assert removed[0] == 1 and removed[1] >= 1
