import json
from pathlib import Path
from unittest.mock import patch

import pytest

from autotest.errors import ProjectInspectionError
from autotest.main import main
from autotest.project_inspector import ProjectInspector

FIXTURES = Path(__file__).parent / "fixtures" / "projects"


def fixture(name: str) -> Path:
    return FIXTURES / name


def test_modern_project_metadata_modules_and_portable_json() -> None:
    profile = ProjectInspector().inspect(fixture("modern_project"))
    assert profile.is_python_project
    assert profile.project_name == "demo"
    assert profile.python_requirement == ">=3.10"
    assert profile.build_backend == "setuptools.build_meta"
    assert profile.package_manager == "uv"
    assert profile.test_framework == "pytest"
    assert [path.name for path in profile.lock_files] == ["uv.lock"]
    assert [path.name for path in profile.source_roots] == ["src"]
    assert [path.name for path in profile.test_roots] == ["tests"]
    assert {(item.value, item.group) for item in profile.dependencies} == {
        ("requests>=2.31", "runtime"),
        ("pytest==8.4.2", "optional:test"),
        ("setuptools>=70", "build"),
    }
    modules = {item.path.name: item for item in profile.modules}
    assert modules["__init__.py"].module_name == "demo"
    assert modules["math.py"].module_name == "demo.math"
    assert modules["check_math.py"].is_test_module
    assert [(item.name, item.is_async) for item in modules["math.py"].top_level_functions] == [
        ("add", False),
        ("async_add", True),
    ]
    assert profile.to_dict()["source_roots"] == ["src"]
    assert profile.to_dict()["root"] == "."
    assert profile.to_dict()["project_name"] == "demo"
    assert json.loads(profile.to_json())["modules"][0]["path"].startswith("src/")
    assert ProjectInspector().inspect(fixture("modern_project")).sha256 == profile.sha256


def test_requirements_are_raw_and_groups_follow_file_context() -> None:
    profile = ProjectInspector().inspect(fixture("requirements_project"))
    values = {(item.value, item.group) for item in profile.dependencies}
    assert ("requests>=2.31", "runtime") in values
    assert ("-r base.txt", "runtime") in values
    assert ("-e .", "runtime") in values
    assert ("git+https://example.invalid/demo.git", "runtime") in values
    assert ('colorama; python_version >= "3.10"', "runtime") in values
    assert ("pytest==8.4.2", "development") in values
    assert ("click>=8", "unknown") in values
    assert profile.package_manager == "pip/requirements"
    assert [path.relative_to(profile.root).as_posix() for path in profile.source_roots] == ["."]
    assert {item.module_name for item in profile.modules if not item.is_test_module} == {
        "app",
        "app.logic",
    }


def test_setup_cfg_static_fields_and_package_dir() -> None:
    profile = ProjectInspector().inspect(fixture("legacy_cfg_project"))
    assert profile.python_requirement == ">=3.9"
    assert [path.name for path in profile.source_roots] == ["lib"]
    assert [path.name for path in profile.test_roots] == ["test"]
    assert {(item.value, item.group) for item in profile.dependencies} == {
        ("requests>=2", "runtime"),
        ("click>=8", "runtime"),
        ("pytest>=8", "optional:test"),
    }


def test_static_setup_py_and_dynamic_setup_py_are_never_executed() -> None:
    static = ProjectInspector().inspect(fixture("static_setup_project"))
    assert static.python_requirement == ">=3.10"
    assert ("requests>=2", "runtime") in {(item.value, item.group) for item in static.dependencies}
    dynamic = ProjectInspector().inspect(fixture("dynamic_setup_project"))
    assert any("dynamic install_requires" in warning for warning in dynamic.warnings)
    assert any(
        item.name == "discover_me"
        for module in dynamic.modules
        for item in module.top_level_functions
    )


def test_no_target_import_or_setup_execution_even_with_side_effect(tmp_path: Path) -> None:
    project = tmp_path / "side effect repo"
    package = project / "src" / "untrusted"
    package.mkdir(parents=True)
    marker = tmp_path / "SHOULD_NOT_EXIST"
    (project / "setup.py").write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n'
        "def visible():\n    return 1\n",
        encoding="utf-8",
    )
    profile = ProjectInspector().inspect(project)
    assert not marker.exists()
    assert any(
        item.name == "visible" for module in profile.modules for item in module.top_level_functions
    )


def test_invalid_metadata_and_source_are_warnings(tmp_path: Path) -> None:
    project = tmp_path / "broken"
    source = project / "src" / "sample"
    source.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (project / "setup.py").write_text("def broken(:\n", encoding="utf-8")
    (source / "broken.py").write_text(
        (fixture("malformed_project") / "src" / "sample" / "broken.py.txt").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (source / "valid.py").write_text("def valid():\n    return 1\n", encoding="utf-8")
    profile = ProjectInspector().inspect(project)
    assert len(profile.modules) == 2
    assert any(
        item.name == "valid" for module in profile.modules for item in module.top_level_functions
    )
    assert any("malformed TOML" in warning for warning in profile.warnings)
    assert any("setup.py: syntax error" in warning for warning in profile.warnings)
    assert any("Syntax error in Python module" in warning for warning in profile.warnings)


def test_conflicting_requirements_and_multiple_managers_warn(tmp_path: Path) -> None:
    project = tmp_path / "mixed"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    (project / "setup.cfg").write_text("[options]\npython_requires = >=3.10\n", encoding="utf-8")
    (project / "poetry.lock").write_text("", encoding="utf-8")
    (project / "uv.lock").write_text("", encoding="utf-8")
    profile = ProjectInspector().inspect(project)
    assert profile.python_requirement is None
    assert len(profile.python_requirements) == 2
    assert profile.package_manager is None
    assert any("Conflicting Python requirements" in warning for warning in profile.warnings)
    assert any("Multiple dependency workflows" in warning for warning in profile.warnings)


def test_project_root_cases_and_unicode_space_path(tmp_path: Path) -> None:
    inspector = ProjectInspector()
    with pytest.raises(ProjectInspectionError):
        inspector.inspect(tmp_path / "missing")
    file = tmp_path / "file.txt"
    file.write_text("not directory", encoding="utf-8")
    with pytest.raises(ProjectInspectionError):
        inspector.inspect(file)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not inspector.inspect(empty).is_python_project
    assert any("empty" in item for item in inspector.inspect(empty).warnings)
    assert not inspector.inspect(fixture("non_python_project")).is_python_project
    unicode = tmp_path / "dự án có dấu"
    unicode.mkdir()
    (unicode / "module.py").write_text("def café():\n    return 1\n", encoding="utf-8")
    assert inspector.inspect(unicode).is_python_project


def test_configured_testpaths_and_ambiguous_sources(tmp_path: Path) -> None:
    project = tmp_path / "paths"
    for name in ("src/pkg", "lib/other", "checks", "tests"):
        path = project / name
        path.mkdir(parents=True)
        (path / "__init__.py").write_text("", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["checks", "tests"]\n',
        encoding="utf-8",
    )
    profile = ProjectInspector().inspect(project)
    assert {p.name for p in profile.source_roots} == {"src", "lib"}
    assert {p.name for p in profile.test_roots} == {"checks", "tests"}
    assert any("Multiple possible source roots" in warning for warning in profile.warnings)


def test_pytest_ini_tox_ini_and_multiple_framework_evidence(tmp_path: Path) -> None:
    project = tmp_path / "test config"
    for name in ("checks", "tests"):
        (project / name).mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = checks\n", encoding="utf-8")
    (project / "tox.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (project / "tests" / "checks.py").write_text("import unittest\n", encoding="utf-8")
    profile = ProjectInspector().inspect(project)
    assert {path.name for path in profile.test_roots} == {"checks", "tests"}
    assert profile.test_framework is None
    assert any("Multiple test frameworks" in warning for warning in profile.warnings)


def test_exclusions_symlinks_and_file_size_bound(tmp_path: Path) -> None:
    project = tmp_path / "safe"
    source = project / "src" / "pkg"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "small.py").write_text("def small():\n    return 1\n", encoding="utf-8")
    (source / "large.py").write_text("x = '" + "x" * 200 + "'\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "ignored.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("def external(): pass\n", encoding="utf-8")
    try:
        (source / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass  # Windows without symlink privilege still tests file bounds and exclusions.
    profile = ProjectInspector(max_source_file_bytes=100).inspect(project)
    assert {item.path.name for item in profile.modules} == {"__init__.py", "small.py"}
    assert any("oversized" in warning for warning in profile.warnings)
    if (source / "link").is_symlink():
        assert any("Skipped symlink" in warning for warning in profile.warnings)


def test_json_stability_relative_paths_and_utf8(tmp_path: Path) -> None:
    project = tmp_path / "résumé project"
    project.mkdir()
    (project / "módulo.py").write_text("def función():\n    return 1\n", encoding="utf-8")
    first = ProjectInspector().inspect(project)
    second = ProjectInspector().inspect(project)
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert "résumé" in first.to_json()
    assert "módulo.py" in first.to_json()
    assert str(tmp_path) not in first.to_json()
    assert json.loads(first.to_json())["schema_version"] == 1


def test_cli_inspection_is_static_and_writes_explicit_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "profiles" / "project_profile.json"
    with (
        patch("autotest.main.OllamaProvider", side_effect=AssertionError("Ollama created")),
        patch("autotest.main.TestRunner", side_effect=AssertionError("pytest runner created")),
    ):
        exit_code = main(
            [
                "--inspect-project",
                str(fixture("modern_project")),
                "--profile-output",
                str(output),
            ]
        )
    assert exit_code == 0
    assert "Python project: yes" in capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["source_roots"] == ["src"]
    assert (
        main(
            [
                "--inspect-project",
                str(fixture("modern_project")),
                "--profile-output",
                str(output),
            ]
        )
        == 2
    )
    assert main(["--inspect-project", str(tmp_path / "missing")]) == 2
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["--profile-output", str(output)])
    assert exc.value.code == 2
