"""Machine profile loading: what a profile must resolve to, and every way it must refuse.

The point of these tests is the refusal half. A loader that quietly falls back to "the driver
named on the command line" or "whatever agent looks closest" would make a run report a
configuration nobody selected, so each wrong input is asserted to stop instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import AgentBinding, MachineProfile, RefusalCode, RefusedError
from hflow.prepare import resolve_machine_bindings, resolve_run
from hflow.profiles import (
    ENV_PROFILE,
    load_profile,
    profile_digest,
    requested_profile_id,
    resolve_role_bindings,
    role_binding,
)

from .conftest import write_profile, write_project, write_task


def test_a_profile_resolves_each_role_independently(
    tmp_path: Path, live_profile: MachineProfile
) -> None:
    write_profile(tmp_path, live_profile)
    profile = load_profile(tmp_path, "dsh-local")

    implementer_agent, implementer = role_binding(profile, "implementer")
    reviewer_agent, reviewer = role_binding(profile, "reviewer")

    assert (implementer_agent, reviewer_agent) == ("dsh-implementer", "dsh-reviewer")
    assert implementer.model_selection == "implementer-model"
    assert reviewer.model_selection == "reviewer-model"
    assert implementer is not reviewer
    assert profile_digest(profile).startswith("sha256:")


def test_both_run_roles_are_required_not_defaulted(
    tmp_path: Path, live_profile: MachineProfile
) -> None:
    """A profile that binds only the implementer is refused, not completed by a guess."""
    partial = live_profile.model_copy(
        update={"role_bindings": {"implementer": "dsh-implementer"}}
    )
    write_profile(tmp_path, partial)

    with pytest.raises(RefusedError) as excinfo:
        resolve_role_bindings(load_profile(tmp_path, "dsh-local"))
    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert "reviewer" in excinfo.value.message
    assert "inheriting another role's agent" in excinfo.value.message


def test_an_unknown_profile_id_refuses_without_falling_back(tmp_path: Path) -> None:
    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "does-not-exist")
    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert "not found" in excinfo.value.message
    assert "Nothing falls back to a default binding" in excinfo.value.message


def test_an_empty_profile_id_says_no_profile_was_selected(tmp_path: Path) -> None:
    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "")
    assert ENV_PROFILE in excinfo.value.message


def test_a_profile_id_cannot_escape_the_profile_directory(tmp_path: Path) -> None:
    for bad in ("../escape", "sub/dir", ".hidden"):
        with pytest.raises(RefusedError) as excinfo:
            load_profile(tmp_path, bad)
        assert "plain file name" in excinfo.value.message, bad


def test_a_malformed_profile_names_the_field_that_is_wrong(tmp_path: Path) -> None:
    path = tmp_path / "profiles" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "broken")
    assert "not valid JSON" in excinfo.value.message

    path.write_text(
        json.dumps(
            {
                "profile_id": "broken",
                "role_bindings": {"implementer": "a", "reviewer": "a"},
                "agents": {"a": {"harness": "dsh", "driver": "acpx-dsh", "invented": True}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "broken")
    # The strict contract is what catches an invented field; the message says which one.
    assert "agents.a.invented" in excinfo.value.message


def test_a_file_whose_contents_name_another_profile_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "profiles" / "named-a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "profile_id": "named-b",
                "role_bindings": {"implementer": "a", "reviewer": "a"},
                "agents": {"a": {"harness": "dsh", "driver": "fake"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "named-a")
    assert "must agree" in excinfo.value.message


def test_an_unimplemented_driver_name_refuses_rather_than_downgrading(tmp_path: Path) -> None:
    profile = MachineProfile(
        profile_id="odd",
        role_bindings={"implementer": "a", "reviewer": "a"},
        agents={"a": {"harness": "other", "driver": "some-other-harness"}},
    )
    write_profile(tmp_path, profile)
    with pytest.raises(RefusedError) as excinfo:
        resolve_machine_bindings(data_dir=tmp_path, profile_id="odd")
    assert "no runtime fallback" in excinfo.value.message


def test_a_profile_may_not_mix_the_offline_fake_with_a_real_transport(tmp_path: Path) -> None:
    """Half a run scripted and half a model is not a configuration a receipt could describe."""
    profile = MachineProfile(
        profile_id="mixed",
        role_bindings={"implementer": "fake-agent", "reviewer": "real-agent"},
        agents={
            "fake-agent": {"harness": "dsh", "driver": "fake"},
            "real-agent": {"harness": "dsh", "driver": "acpx-dsh"},
        },
    )
    write_profile(tmp_path, profile)
    with pytest.raises(RefusedError) as excinfo:
        resolve_machine_bindings(data_dir=tmp_path, profile_id="mixed")
    assert "mixes the offline fake driver" in excinfo.value.message


# --------------------------------------------------------------------------
# precedence: one definition, three sources
# --------------------------------------------------------------------------


def test_cli_profile_beats_the_environment_variable() -> None:
    assert requested_profile_id("from-cli", {ENV_PROFILE: "from-env"}) == "from-cli"
    assert requested_profile_id(None, {ENV_PROFILE: "from-env"}) == "from-env"
    assert requested_profile_id(None, {}) == ""
    assert requested_profile_id("  ", {ENV_PROFILE: "from-env"}) == "from-env"


def test_the_environment_variable_is_honoured_by_resolution(
    tmp_path: Path, project, task_spec, project_root: Path, live_profile: MachineProfile,
    acpx_client,
) -> None:
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)

    resolved = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        env={ENV_PROFILE: "dsh-local"},
    )
    assert resolved.effective.profile_id == "dsh-local"
    assert resolved.effective.source == "machine_profile"
    assert resolved.is_real_driver is True


def test_driver_and_profile_must_agree(
    tmp_path: Path, project, task_spec, project_root: Path, live_profile: MachineProfile
) -> None:
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)

    # The same driver, named through an alias: accepted, because it resolves to one driver.
    same = resolve_machine_bindings(
        data_dir=data_dir, profile_id="dsh-local", driver="acpx"
    )
    assert same.is_real_driver is True

    with pytest.raises(RefusedError) as excinfo:
        resolve_machine_bindings(data_dir=data_dir, profile_id="dsh-local", driver="fake")
    assert "disagree" in excinfo.value.message
    assert "drop" in excinfo.value.message, "the message must say how to resolve the conflict"


def test_a_harness_no_driver_serves_is_refused(tmp_path: Path) -> None:
    """A driver name alone is not enough: the declared harness has to be one it launches.

    ``harness="codex", driver="acpx-dsh"`` used to resolve happily, and every process the run
    started was DSH while the record said Codex.
    """
    profile = MachineProfile(
        profile_id="wrong-harness",
        role_bindings={"implementer": "a", "reviewer": "a"},
        agents={"a": {"harness": "codex", "driver": "acpx-dsh"}},
    )
    write_profile(tmp_path, profile)

    with pytest.raises(RefusedError) as excinfo:
        resolve_machine_bindings(data_dir=tmp_path, profile_id="wrong-harness")
    assert excinfo.value.code is RefusalCode.NOT_IMPLEMENTED
    assert "'codex'" in excinfo.value.message
    assert "dsh" in excinfo.value.message, "the message names the harnesses it does serve"
    # ...and the same pair is refused by the resolution `run` performs, not only by the
    # role/agent step that deliberately says nothing about drivers.
    assert resolve_role_bindings(profile)["implementer"][0] == "a"


def test_the_offline_driver_is_not_a_loophole_for_an_unimplemented_harness(
    tmp_path: Path,
) -> None:
    """Binding `codex` to the offline stand-in would still record a harness nothing runs."""
    profile = MachineProfile(
        profile_id="fake-codex",
        role_bindings={"implementer": "a", "reviewer": "a"},
        agents={"a": {"harness": "codex", "driver": "fake"}},
    )
    write_profile(tmp_path, profile)
    with pytest.raises(RefusedError) as excinfo:
        resolve_machine_bindings(data_dir=tmp_path, profile_id="fake-codex")
    assert "codex" in excinfo.value.message


def test_every_implemented_driver_declares_the_harnesses_it_serves() -> None:
    """The mapping is the contract: a new driver without one cannot be resolved."""
    from hflow.drivers.selected import DRIVER_HARNESSES, FAKE_ALIASES, ACPX_DSH_ALIASES, resolve_driver_id

    assert set(DRIVER_HARNESSES) == {"acpx-dsh-acp", "fake"}
    for name in sorted(FAKE_ALIASES | ACPX_DSH_ALIASES):
        assert resolve_driver_id(AgentBinding(harness="dsh", driver=name)) in DRIVER_HARNESSES
        with pytest.raises(RefusedError):
            resolve_driver_id(AgentBinding(harness="codex", driver=name))


def test_no_profile_and_no_driver_is_the_offline_default(tmp_path: Path) -> None:
    machine = resolve_machine_bindings(data_dir=tmp_path, driver=None)
    assert machine.profile is None
    assert machine.is_real_driver is False
    assert machine.bindings["implementer"][0] == "command-line"


# --------------------------------------------------------------------------
# model_selection values (C2b): what may become the client's --model flag
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("native_profile", "native_profile"),
        ("deepseek-v4-pro", "deepseek-v4-pro"),
        ("provider/model:v1.2_x", "provider/model:v1.2_x"),
        # DSH's opaque value ids are JSON [provider, model] pairs. Both spellings a person may
        # write are kept as the one compact form acpx compares against the advertised catalog.
        ('["deepseek-official","deepseek-v4-pro"]', '["deepseek-official","deepseek-v4-pro"]'),
        ('[ "deepseek-official" , "deepseek-v4-pro" ]', '["deepseek-official","deepseek-v4-pro"]'),
        (["deepseek-official", "deepseek-v4-pro"], '["deepseek-official","deepseek-v4-pro"]'),
    ],
)
def test_a_model_selection_is_a_token_or_a_provider_model_pair(
    tmp_path: Path, written: object, stored: str
) -> None:
    document = {
        "profile_id": "models",
        "role_bindings": {"implementer": "a", "reviewer": "a"},
        "agents": {"a": {"harness": "dsh", "driver": "acpx-dsh", "model_selection": written}},
    }
    path = tmp_path / "profiles" / "models.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    profile = load_profile(tmp_path, "models")
    assert profile.agents["a"].model_selection == stored


@pytest.mark.parametrize(
    "written",
    [
        "",
        "two words",
        "a;b",
        'quote"inside',
        "x" * 129,
        "--approve-all",
        '["only-one"]',
        '["a","b","c"]',
        '["a b","c"]',
        '["a",2]',
        '{"provider":"a"}',
        "[not json",
        ["a"],
        ["a", "b", "c"],
        ["a", 1],
        17,
    ],
)
def test_an_invalid_model_selection_is_refused_at_profile_load(
    tmp_path: Path, written: object
) -> None:
    """Anything that is not a token or a pair never reaches a command line: the load refuses."""
    document = {
        "profile_id": "models",
        "role_bindings": {"implementer": "a", "reviewer": "a"},
        "agents": {"a": {"harness": "dsh", "driver": "acpx-dsh", "model_selection": written}},
    }
    path = tmp_path / "profiles" / "models.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RefusedError) as excinfo:
        load_profile(tmp_path, "models")
    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert "model_selection" in excinfo.value.message
