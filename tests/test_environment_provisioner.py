import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from autotest.environment_planner import EnvironmentPlanner, InterpreterInfo
from autotest.environment_provisioner import (
    EnvironmentCommandResult,
    EnvironmentCommandRunner,
    EnvironmentProvisioner,
    EnvironmentProvisionStatus,
)
from autotest.errors import EnvironmentProvisionError
from autotest.project_inspector import ProjectInspector

FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "environment_no_deps"
PYTHON = InterpreterInfo(Path(sys.executable), "3.12.10", "CPython")


class FakeRunner(EnvironmentCommandRunner):
    def __init__(
        self, *, fail: str | None = None, timeout: str | None = None, bad_versions: bool = False
    ) -> None:
        self.calls: list[tuple[str, tuple[str, ...], Path, dict[str, str]]] = []
        self.fail = fail
        self.timeout = timeout
        self.bad_versions = bad_versions

    def run(self, stage, argv, *, cwd, timeout, env):  # type: ignore[override]
        self.calls.append((stage, tuple(argv), cwd, dict(env)))
        if stage == "create-venv":
            python = Path(argv[-1]) / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.write_text("fake interpreter", encoding="utf-8")
        stdout = "uv 0.9.0\n" if stage == "uv-version" else ""
        if stage == "verify-tools":
            stdout = json.dumps(
                {
                    "python": "3.12.10",
                    "pytest": "8.4.2",
                    "coverage": "7.16.0",
                    "pytest-timeout": "0.0.0" if self.bad_versions else "2.4.0",
                }
            )
        return EnvironmentCommandResult(
            stage,
            tuple(argv),
            1 if stage == self.fail else 0,
            stdout,
            "simulated failure" if stage == self.fail else "",
            0.01,
            stage == self.timeout,
        )


def make_plan(root: Path = FIXTURE, *, offline: bool = True):
    profile = ProjectInspector().inspect(root)
    return profile, EnvironmentPlanner().plan(profile, PYTHON, offline=offline)


def test_tampered_plan_is_rejected_before_workspace_creation(tmp_path: Path) -> None:
    profile, plan = make_plan()
    output = tmp_path / "environments"
    tampered = replace(plan, dependencies=("--index-url", "https://example.invalid"))
    with pytest.raises(EnvironmentProvisionError, match="plan differs"):
        EnvironmentProvisioner(command_runner=FakeRunner(), uv_executable="uv").provision(
            FIXTURE, profile, tampered, output
        )
    assert not output.exists()


def test_successful_fake_provision_is_isolated_and_records_evidence(tmp_path: Path) -> None:
    profile, plan = make_plan()
    runner = FakeRunner()
    result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
        FIXTURE, profile, plan, tmp_path / "environments"
    )
    assert result.status is EnvironmentProvisionStatus.READY
    assert result.original_integrity_verified and result.copy_integrity_verified
    assert result.profile_copy_verified
    assert result.target_dependencies_installed and result.runner_tools_installed
    assert [item.stage for item in result.commands] == [
        "uv-version",
        "create-venv",
        "install-runner-tools",
        "verify-tools",
    ]
    assert result.source_root is not None and result.workspace_root is not None
    assert (result.source_root / "src" / "unsafe_demo" / "__init__.py").is_file()
    assert result.copy_file_count >= 2
    assert (result.workspace_root / "original_manifest.json").is_file()
    assert (result.workspace_root / "copied_manifest.json").is_file()
    assert (result.workspace_root / "provision_result.json").is_file()
    manifest = json.loads(
        (result.workspace_root / "original_manifest.json").read_text(encoding="utf-8")
    )
    assert all(not item["path"].startswith("E:") for item in manifest["files"])
    assert manifest == json.loads(
        (result.workspace_root / "copied_manifest.json").read_text(encoding="utf-8")
    )
    assert all(call[2] == result.workspace_root for call in runner.calls)
    assert all("PYTHONNOUSERSITE" in call[3] for call in runner.calls)
    assert "-m" in runner.calls[1][1] and "venv" in runner.calls[1][1]
    assert "--offline" in runner.calls[2][1]
    assert "pytest==8.4.2" in runner.calls[2][1]
    assert all("setup.py" not in " ".join(call[1]) for call in runner.calls)
    assert all("pip install ." not in " ".join(call[1]) for call in runner.calls)


def test_requirements_and_pyproject_dependencies_use_separate_argv_items(tmp_path: Path) -> None:
    for name in ("environment_requirements", "environment_pyproject"):
        root = FIXTURE.parent / name
        profile, plan = make_plan(root)
        runner = FakeRunner()
        result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
            root, profile, plan, tmp_path / name
        )
        assert result.status is EnvironmentProvisionStatus.READY
        install = next(call[1] for call in runner.calls if call[0] == "install-target-dependencies")
        assert install[-len(plan.dependencies) :] == plan.dependencies
        assert "--python" in install
        assert "--no-build" in install


@pytest.mark.parametrize(
    ("failure", "timed_out", "bad_versions", "expected"),
    [
        ("install-runner-tools", None, False, EnvironmentProvisionStatus.ERROR),
        (None, "install-runner-tools", False, EnvironmentProvisionStatus.TIMEOUT),
        (None, None, True, EnvironmentProvisionStatus.ERROR),
    ],
)
def test_failures_still_verify_original_integrity(
    tmp_path: Path,
    failure: str | None,
    timed_out: str | None,
    bad_versions: bool,
    expected: EnvironmentProvisionStatus,
) -> None:
    profile, plan = make_plan()
    runner = FakeRunner(fail=failure, timeout=timed_out, bad_versions=bad_versions)
    result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
        FIXTURE, profile, plan, tmp_path / "environments"
    )
    assert result.status is expected
    assert result.original_integrity_verified and result.copy_integrity_verified
    assert result.workspace_root is not None
    assert (result.workspace_root / "provision_result.json").is_file()


def test_source_mutation_during_command_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "original"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="original"\n', encoding="utf-8")
    source = root / "target.py"
    source.write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
    profile, plan = make_plan(root)

    class MutatingRunner(FakeRunner):
        def run(self, stage, argv, *, cwd, timeout, env):  # type: ignore[override]
            if stage == "install-runner-tools":
                source.write_text("changed\n", encoding="utf-8")
            return super().run(stage, argv, cwd=cwd, timeout=timeout, env=env)

    result = EnvironmentProvisioner(command_runner=MutatingRunner(), uv_executable="uv").provision(
        root, profile, plan, tmp_path / "environments"
    )
    assert result.status is EnvironmentProvisionStatus.SOURCE_INTEGRITY_ERROR
    assert not result.original_integrity_verified


def test_copy_tamper_stops_before_install(tmp_path: Path) -> None:
    profile, plan = make_plan()
    runner = FakeRunner()
    real_copy = __import__("shutil").copyfile

    def tamper(source, destination, *, follow_symlinks=True):
        real_copy(source, destination, follow_symlinks=follow_symlinks)
        if Path(destination).name == "pyproject.toml":
            Path(destination).write_text("tampered", encoding="utf-8")

    with patch("autotest.environment_provisioner.shutil.copyfile", side_effect=tamper):
        result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
            FIXTURE, profile, plan, tmp_path / "environments"
        )
    assert result.status is EnvironmentProvisionStatus.COPY_INTEGRITY_ERROR
    assert not runner.calls
    assert result.original_integrity_verified


@pytest.mark.parametrize(
    "bounds", [{"max_files": 1}, {"max_file_bytes": 5}, {"max_total_bytes": 20}]
)
def test_copy_bounds_fail_before_commands(tmp_path: Path, bounds: dict[str, int]) -> None:
    profile, plan = make_plan()
    runner = FakeRunner()
    result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv", **bounds).provision(
        FIXTURE, profile, plan, tmp_path / "environments"
    )
    assert result.status is EnvironmentProvisionStatus.ERROR
    assert not runner.calls


def test_copy_excludes_runtime_dirs_and_external_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "original"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="original"\n', encoding="utf-8")
    for name in (".git", ".venv", "workspace", "__pycache__"):
        directory = root / name
        directory.mkdir()
        (directory / "ignored.txt").write_text("ignored", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    profile, plan = make_plan(root)
    result = EnvironmentProvisioner(command_runner=FakeRunner(), uv_executable="uv").provision(
        root, profile, plan, tmp_path / "environments"
    )
    assert result.status is EnvironmentProvisionStatus.READY
    assert result.source_root is not None
    assert not any(
        (result.source_root / name).exists()
        for name in (".git", ".venv", "workspace", "__pycache__", "linked")
    )


def test_unsupported_and_inside_original_do_not_create_workspace(tmp_path: Path) -> None:
    profile, plan = make_plan(FIXTURE.parent / "requirements_project")
    runner = FakeRunner()
    result = EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
        profile.root, profile, plan, tmp_path / "environments"
    )
    assert result.status is EnvironmentProvisionStatus.UNSUPPORTED
    assert not (tmp_path / "environments").exists()
    assert not runner.calls
    profile, plan = make_plan()
    with pytest.raises(EnvironmentProvisionError):
        EnvironmentProvisioner(command_runner=runner, uv_executable="uv").provision(
            FIXTURE, profile, plan, FIXTURE / "workspace" / "environments"
        )


def test_real_command_runner_passes_argv_without_shell(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(["python"], 0, "ok", "")
    with patch("autotest.environment_provisioner.subprocess.run", return_value=completed) as run:
        result = EnvironmentCommandRunner().run(
            "sample", ["python", "--version"], cwd=tmp_path, timeout=2, env={"PATH": "safe"}
        )
    assert result.exit_code == 0
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.args[0] == ["python", "--version"]
