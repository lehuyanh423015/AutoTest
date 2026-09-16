"""The environment CLI remains separate from generation and inspection."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autotest.environment_provisioner import EnvironmentProvisionStatus
from autotest.main import main

FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "environment_no_deps"


def test_plan_mode_is_read_only_and_does_not_start_generation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "unused"
    with (
        patch("autotest.main.OllamaProvider") as ollama,
        patch("autotest.main.EnvironmentProvisioner") as provisioner,
    ):
        code = main(["--plan-environment", str(FIXTURE), "--environment-output-root", str(output)])
    stdout = capsys.readouterr().out
    assert code == 0
    assert "Environment support: SUPPORTED" in stdout
    assert "Dependency strategy: NONE" in stdout
    assert not output.exists()
    ollama.assert_not_called()
    provisioner.assert_not_called()


def test_prepare_mode_passes_timeout_offline_and_output_root(tmp_path: Path) -> None:
    output = tmp_path / "prepared"
    with (
        patch("autotest.main.OllamaProvider") as ollama,
        patch("autotest.main.EnvironmentProvisioner") as provisioner,
        patch("autotest.main._print_target_environment"),
    ):
        provisioner.return_value.provision.return_value = SimpleNamespace(
            status=EnvironmentProvisionStatus.READY
        )
        code = main(
            [
                "--prepare-environment",
                str(FIXTURE),
                "--target-python",
                sys.executable,
                "--environment-output-root",
                str(output),
                "--environment-timeout",
                "12",
                "--environment-offline",
            ]
        )
    assert code == 0
    assert provisioner.call_args.kwargs["timeout"] == 12.0
    project_root, profile, plan, output_root = provisioner.return_value.provision.call_args.args
    assert project_root == FIXTURE
    assert profile.root == FIXTURE.resolve()
    assert plan.offline is True
    assert output_root == output
    ollama.assert_not_called()


def test_unsupported_plan_does_not_provision(tmp_path: Path) -> None:
    with patch("autotest.main.EnvironmentProvisioner") as provisioner:
        assert main(["--plan-environment", str(tmp_path)]) == 0
        assert main(["--prepare-environment", str(tmp_path)]) == 2
    provisioner.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--plan-environment", str(FIXTURE), "--inspect-project", str(FIXTURE)],
        ["--prepare-environment", str(FIXTURE), "--file", "x.py", "--function", "f"],
        ["--plan-environment", str(FIXTURE), "--environment-timeout", "0"],
    ],
)
def test_invalid_environment_cli_combinations(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(arguments)
    assert exit_info.value.code == 2
