import json
import sys
from pathlib import Path

import pytest

from autotest.environment_planner import (
    RUNNER_REQUIREMENTS,
    DependencyStrategy,
    EnvironmentPlanner,
    EnvironmentPlanStatus,
    InterpreterInfo,
    PythonCompatibility,
    check_python_compatibility,
    probe_interpreter,
)
from autotest.project_inspector import ProjectInspector

FIXTURES = Path(__file__).parent / "fixtures" / "projects"
PYTHON = InterpreterInfo(Path(sys.executable), "3.12.10", "CPython")


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (">=3.10", PythonCompatibility.COMPATIBLE),
        (">=3.10,<3.13", PythonCompatibility.COMPATIBLE),
        (">3.11", PythonCompatibility.COMPATIBLE),
        ("<=3.12", PythonCompatibility.INCOMPATIBLE),
        ("==3.12.*", PythonCompatibility.COMPATIBLE),
        ("~=3.12", PythonCompatibility.COMPATIBLE),
        ("~=3.11.0", PythonCompatibility.INCOMPATIBLE),
        ("==3.11.*", PythonCompatibility.INCOMPATIBLE),
        ("~=3", PythonCompatibility.UNKNOWN),
        (">=3.10 || <4", PythonCompatibility.UNKNOWN),
        (None, PythonCompatibility.UNKNOWN),
    ],
)
def test_python_compatibility_subset(
    requirement: str | None, expected: PythonCompatibility
) -> None:
    assert check_python_compatibility(requirement, PYTHON.version) is expected


@pytest.mark.parametrize(
    ("name", "strategy", "dependencies"),
    [
        ("environment_no_deps", DependencyStrategy.NONE, ()),
        (
            "environment_pyproject",
            DependencyStrategy.PYPROJECT_DECLARATIONS,
            ("requests>=2.31", "typing-extensions>=4.10; python_version < '3.13'"),
        ),
        (
            "environment_requirements",
            DependencyStrategy.REQUIREMENTS,
            ('typing-extensions>=4.10; python_version < "3.13"', "requests>=2.31"),
        ),
    ],
)
def test_supported_strategies(
    name: str, strategy: DependencyStrategy, dependencies: tuple[str, ...]
) -> None:
    profile = ProjectInspector().inspect(FIXTURES / name)
    plan = EnvironmentPlanner().plan(profile, PYTHON)
    assert plan.status is EnvironmentPlanStatus.SUPPORTED
    assert plan.dependency_strategy is strategy
    assert set(plan.dependencies) == set(dependencies)
    assert plan.runner_requirements == RUNNER_REQUIREMENTS
    assert not plan.install_target_project
    assert plan.project_profile_sha256 == profile.sha256
    assert plan.to_dict()["plan_sha256"] == plan.sha256
    assert json.loads(plan.to_json())["selected_python"]["version"] == "3.12.10"
    assert EnvironmentPlanner().plan(profile, PYTHON).sha256 == plan.sha256


@pytest.mark.parametrize(
    "requirement",
    [
        "-r other.txt",
        "-e .",
        "--index-url https://example.invalid",
        "git+https://example.invalid/x",
        "https://example.invalid/x.whl",
        "file:../x",
        "../package",
        "./package",
        "demo @ https://example.invalid/x.whl",
    ],
)
def test_unsafe_requirements_are_unsupported(tmp_path: Path, requirement: str) -> None:
    (tmp_path / "requirements.txt").write_text(requirement + "\n", encoding="utf-8")
    profile = ProjectInspector().inspect(tmp_path)
    plan = EnvironmentPlanner().plan(profile, PYTHON)
    assert plan.status is EnvironmentPlanStatus.UNSUPPORTED
    assert plan.dependency_strategy is DependencyStrategy.UNSUPPORTED
    assert any("Unsafe or unsupported" in reason for reason in plan.unsupported_reasons)


@pytest.mark.parametrize("name", ["poetry.lock", "Pipfile"])
def test_poetry_and_pipenv_only_are_unsupported(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("", encoding="utf-8")
    plan = EnvironmentPlanner().plan(ProjectInspector().inspect(tmp_path), PYTHON)
    assert plan.status is EnvironmentPlanStatus.UNSUPPORTED


def test_conflicting_managers_and_python_requirements_are_unsupported(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    (tmp_path / "setup.cfg").write_text("[options]\npython_requires = >=3.10\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    plan = EnvironmentPlanner().plan(ProjectInspector().inspect(tmp_path), PYTHON)
    assert plan.status is EnvironmentPlanStatus.UNSUPPORTED
    assert any("Conflicting Python" in item for item in plan.unsupported_reasons)
    assert any("Conflicting dependency-manager" in item for item in plan.unsupported_reasons)


def test_incompatible_unknown_and_non_cpython_are_unsupported(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nrequires-python = ">=3.13"\n', encoding="utf-8")
    plan = EnvironmentPlanner().plan(ProjectInspector().inspect(tmp_path), PYTHON)
    assert plan.compatibility is PythonCompatibility.INCOMPATIBLE
    assert plan.status is EnvironmentPlanStatus.UNSUPPORTED
    pyproject.write_text('[project]\nrequires-python = ">=3.11 || <4"\n', encoding="utf-8")
    plan = EnvironmentPlanner().plan(ProjectInspector().inspect(tmp_path), PYTHON)
    assert plan.compatibility is PythonCompatibility.UNKNOWN
    assert plan.status is EnvironmentPlanStatus.UNSUPPORTED
    pyproject.write_text('[project]\nrequires-python = ">=3.10"\n', encoding="utf-8")
    pypy = InterpreterInfo(PYTHON.executable, PYTHON.version, "PyPy")
    assert (
        EnvironmentPlanner().plan(ProjectInspector().inspect(tmp_path), pypy).status
        is EnvironmentPlanStatus.UNSUPPORTED
    )


def test_default_interpreter_probe_only_runs_selected_python() -> None:
    info = probe_interpreter()
    assert info.version == "3.12.10"
    assert info.implementation == "CPython"
    assert info.executable.is_file()
