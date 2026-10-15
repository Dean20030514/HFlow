"""Deny rules mean the same thing however they are spelled, and a rule that can never match is refused.

Git reports a changed path without a leading ``./``. A ``write_deny`` entry written
``./config/secrets/**`` used to reach the matcher unchanged, so it never matched, and admission
accepted it: a deny rule that silently did nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hflow.admission import validate_task_spec
from hflow.contracts import ProjectConfig, RefusalCode, Scope, TaskSpec
from hflow.workspace import matches_pattern, paths_outside_scope


@pytest.mark.parametrize(
    "pattern",
    ["./config/secrets/**", "././config/secrets/**", ".//config/./secrets/**", ".\\config\\secrets"],
)
def test_a_dot_slash_spelled_deny_pattern_matches_the_path_git_reports(pattern: str) -> None:
    assert matches_pattern("config/secrets/k", [pattern])


def test_a_dot_slash_spelled_deny_blocks_a_path_inside_an_allowed_directory() -> None:
    scope = Scope(write_allow=["config"], write_deny=["./config/prod.json"])
    assert paths_outside_scope(["config/prod.json"], scope) == ["config/prod.json"]
    assert paths_outside_scope(
        ["config/secrets/k"], Scope(write_allow=["config"]), ["./config/secrets/**"]
    ) == ["config/secrets/k"]
    assert paths_outside_scope(["config/app.json"], scope, ["./config/secrets/**"]) == []


@pytest.mark.parametrize(
    "entry",
    ["", "   ", "./", ".", "/etc/passwd", "\\\\server\\share", "C:/repo/config", "C:config", "../x",
     "config/../../x"],
)
@pytest.mark.parametrize("owner", ["task", "project"])
def test_a_deny_entry_that_could_never_match_is_refused_at_admission(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path, entry: str, owner: str
) -> None:
    if owner == "task":
        spec = task_spec.model_copy(
            update={
                "scope": Scope(write_allow=list(task_spec.scope.write_allow), write_deny=[entry])
            }
        )
        contract = project
    else:
        spec = task_spec
        contract = project.model_copy(update={"write_deny": [*project.write_deny, entry]})

    report = validate_task_spec(spec, contract, project_root)

    assert not report.ok
    refusals = [issue for issue in report.issues if f"write_deny entry {entry!r}" in issue.detail]
    assert refusals, report.issues
    assert all(issue.code is RefusalCode.SCOPE_VIOLATION for issue in refusals)
    assert all(issue.detail.startswith(f"{owner} write_deny") for issue in refusals)


def test_well_formed_deny_entries_are_still_admitted(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(
        update={
            "scope": Scope(
                write_allow=list(task_spec.scope.write_allow),
                write_deny=["./src/generated/**", "**/*.lock", "docs"],
            )
        }
    )
    report = validate_task_spec(spec, project, project_root)
    assert [issue for issue in report.issues if "write_deny" in issue.detail] == []


@pytest.mark.parametrize(
    ("entry", "lead", "suggestion"),
    [
        ("/config/secrets/**", "'/'", "'config/secrets/**'"),
        (r"\config\secrets", repr("\\"), "'config/secrets'"),
    ],
)
def test_a_root_anchored_deny_entry_is_refused_with_its_own_message(
    entry: str, lead: str, suggestion: str
) -> None:
    """An earlier build silently stripped the leading slash; the refusal says how to rewrite it."""
    from hflow.workspace import write_deny_problems

    problems = write_deny_problems([entry], owner="project")
    assert problems == [
        f"project write_deny entry {entry!r} starts with {lead}; write it relative to the "
        f"project root, e.g. {suggestion}"
    ]
    assert "absolute or drive-qualified" not in problems[0]


@pytest.mark.parametrize("entry", [r"\\server\share\x", "//server/share/x", "C:/repo/config"])
def test_a_unc_or_drive_deny_entry_keeps_the_absolute_message(entry: str) -> None:
    from hflow.workspace import write_deny_problems

    (problem,) = write_deny_problems([entry], owner="task")
    assert "is absolute or drive-qualified" in problem


# --------------------------------------------------------------------------
# a write_allow entry cannot name a denied path by another Windows spelling
# --------------------------------------------------------------------------


def _deny_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in (".git", ".hflow", "secrets_directory", "src"):
        (root / name).mkdir(parents=True)
    (root / ".acpxrc.json").write_text("{}", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "entry",
    [
        ".git.",
        ".hflow.",
        ".acpxrc.json.",
        "secrets_directory.",
        "secrets_directory ",
        "secrets_directory./key.txt",
        ".git /config",
        "src/a.py:hidden",
        "src/...",
    ],
)
def test_a_segment_windows_would_open_under_another_name_is_refused(
    tmp_path: Path, entry: str
) -> None:
    """``.git.`` opens ``.git`` on Windows, so a deny rule matched on the text never saw it.

    Each of these passed ``check_scope`` before: the deny lists were matched against the entry as
    written while ``resolve_within`` resolved it physically, to the denied directory.
    """
    from hflow.workspace import check_scope

    problems = check_scope(
        Scope(write_allow=[entry]), _deny_root(tmp_path), ["secrets_directory/**"]
    )

    assert len(problems) == 1, problems
    assert entry in problems[0]
    assert "ending in a dot or a space" in problems[0] or "containing ':'" in problems[0]


@pytest.mark.parametrize("entry", ["C:/repo/src", "C:src", "a:b", r"\\server\share\x"])
def test_a_drive_is_still_refused_as_drive_qualified(tmp_path: Path, entry: str) -> None:
    """The ``:`` of a drive is left to ``resolve_within``, which names the real problem."""
    from hflow.workspace import check_scope

    (problem,) = check_scope(Scope(write_allow=[entry]), _deny_root(tmp_path), [])
    assert "absolute or drive-qualified" in problem


@pytest.mark.parametrize("entry", ["src", "./src", "src/notes~draft.txt", "src/v1.2/a.py", "~src"])
def test_ordinary_entries_and_a_tilde_in_a_name_are_still_admitted(tmp_path: Path, entry: str) -> None:
    from hflow.workspace import check_scope

    assert check_scope(Scope(write_allow=[entry]), _deny_root(tmp_path), ["secrets_directory/**"]) == []


def _short_name(path: Path) -> str:
    """The 8.3 short name Windows generated for ``path``, or ``""`` when there is none."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))  # type: ignore[attr-defined]
    short = Path(buffer.value).name if 0 < length < len(buffer) else ""
    return "" if short.casefold() == path.name.casefold() else short


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows spelling")
@pytest.mark.parametrize(
    ("denied", "deny", "suffix", "message"),
    [
        ("secrets_directory", ["secrets_directory"], "", "covered by project write_deny"),
        ("secrets_directory", ["secrets_directory/**"], "/key.txt", "covered by project write_deny"),
        (".hflow", [], "/state.json", "HFlow never lets a worker write"),
    ],
)
def test_a_short_name_of_a_denied_directory_is_matched_by_its_resolved_spelling(
    tmp_path: Path, denied: str, deny: list[str], suffix: str, message: str
) -> None:
    """``SECRET~1`` resolves to ``secrets_directory``; the deny lists are matched on both."""
    from hflow.workspace import check_scope

    root = _deny_root(tmp_path)
    short = _short_name(root / denied)
    if not short:
        pytest.skip("this volume does not generate 8.3 short names")
    entry = short + suffix

    problems = check_scope(Scope(write_allow=[entry]), root, deny)

    assert any(message in problem and entry in problem for problem in problems), problems
