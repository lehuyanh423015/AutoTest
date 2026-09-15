import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from autotest.coverage_runner import CoverageRunner
from autotest.errors import CoverageError
from autotest.project_analyzer import ProjectAnalyzer

FIXTURES = Path(__file__).parent / "fixtures"


def test_extracts_target_line_and_branch_coverage(tmp_path: Path) -> None:
    function = ProjectAnalyzer().analyze_function(
        FIXTURES / "sample_project" / "branching.py", "classify_number"
    )
    result = CoverageRunner(timeout=10).run(
        function,
        [FIXTURES / "coverage_suite" / "baseline_classify.py"],
        tmp_path / "coverage output",
    )

    assert result.executed_lines
    assert result.missing_lines
    assert result.executed_branches
    assert result.missing_branches
    assert 0 < result.line_coverage_percent < 100
    assert result.branch_coverage_percent is not None
    assert 0 < result.branch_coverage_percent < 100
    assert result.raw_report_file == (tmp_path / "coverage output" / "coverage_raw.json")
    assert result.raw_report_file.is_file()
    assert (tmp_path / "coverage output" / "coverage_result.json").is_file()
    assert (tmp_path / "coverage output" / "stdout.txt").is_file()
    assert not (tmp_path / "coverage output" / ".coverage-autotest").exists()


def test_other_uncovered_function_does_not_reduce_target_percentage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "module.py"
    target.write_text(
        "def target(value):\n"
        "    return value * 2\n\n"
        "def completely_uncovered_other_function(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests with spaces" / "explicit_suite.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from module import target\n\ndef test_target():\n    assert target(3) == 6\n",
        encoding="utf-8",
    )
    function = ProjectAnalyzer().analyze_function(target, "target")

    result = CoverageRunner(timeout=10).run(function, [test_file], tmp_path / "report")

    assert result.line_coverage_percent == 100.0
    assert result.branch_coverage_percent is None
    assert result.missing_lines == ()


def test_unimported_target_reports_zero_coverage_instead_of_missing_data(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "never_imported.py"
    target.write_text(
        "def target(flag):\n    if flag:\n        return 1\n    return 0\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "suite.py"
    test_file.write_text("def test_unrelated():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    result = CoverageRunner(timeout=10).run(
        ProjectAnalyzer().analyze_function(target, "target"),
        [test_file],
        tmp_path / "coverage",
    )

    assert result.executed_lines == ()
    assert result.missing_lines == result.executable_lines
    assert result.line_coverage_percent == 0.0
    assert result.executed_branches == ()
    assert result.missing_branches
    assert result.branch_coverage_percent == 0.0


def test_utf8_target_and_space_paths_are_supported(tmp_path: Path) -> None:
    project = tmp_path / "prøject with spaces"
    project.mkdir()
    target = project / "unicode_target.py"
    target.write_text(
        "def greeting(name):\n"
        '    if name == "Việt":\n'
        '        return "xin chào"\n'
        '    return "hello"\n',
        encoding="utf-8",
    )
    test_file = tmp_path / "generated tests" / "suite.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from unicode_target import greeting\n\n"
        "def test_utf8():\n"
        '    assert greeting("Việt") == "xin chào"\n',
        encoding="utf-8",
    )

    result = CoverageRunner(timeout=10).run(
        ProjectAnalyzer().analyze_function(target, "greeting"),
        [test_file],
        tmp_path / "artifacts with spaces",
    )

    assert result.function_name == "greeting"
    assert result.line_coverage_percent > 0


def test_parse_report_rejects_malformed_shapes(tmp_path: Path) -> None:
    source = tmp_path / "target.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    function = ProjectAnalyzer().analyze_function(source, "value")

    with pytest.raises(CoverageError, match="files mapping"):
        CoverageRunner.parse_report(function, {"files": []})

    malformed = {
        "files": {
            str(source): {
                "executed_lines": [1, "two"],
                "missing_lines": [],
                "executed_branches": [],
                "missing_branches": [],
            }
        }
    }
    with pytest.raises(CoverageError, match="executed_lines"):
        CoverageRunner.parse_report(function, malformed)


def test_negative_branch_destinations_are_preserved(tmp_path: Path) -> None:
    source = tmp_path / "target.py"
    source.write_text("def value(flag):\n    if flag:\n        return 1\n", encoding="utf-8")
    function = ProjectAnalyzer().analyze_function(source, "value")
    raw = {
        "files": {
            str(source): {
                "executed_lines": [1, 2, 3],
                "missing_lines": [],
                "executed_branches": [[2, 3]],
                "missing_branches": [[2, -1]],
            }
        }
    }

    result = CoverageRunner.parse_report(function, raw)

    assert result.missing_branches == ((2, -1),)
    assert result.branch_coverage_percent == 50.0


def test_coverage_command_failure_preserves_output(tmp_path: Path) -> None:
    function = ProjectAnalyzer().analyze_function(
        FIXTURES / "sample_project" / "branching.py", "classify_number"
    )
    test_file = FIXTURES / "coverage_suite" / "baseline_classify.py"
    with patch(
        "autotest.coverage_runner.subprocess.run",
        return_value=subprocess.CompletedProcess([], 2, "run output", "run error"),
    ):
        with pytest.raises(CoverageError, match="exit code 2"):
            CoverageRunner().run(function, [test_file], tmp_path / "failed")

    assert (tmp_path / "failed" / "stdout.txt").read_text(encoding="utf-8") == "run output"
    assert (tmp_path / "failed" / "stderr.txt").read_text(encoding="utf-8") == "run error"


def test_malformed_coverage_json_is_reported(tmp_path: Path) -> None:
    function = ProjectAnalyzer().analyze_function(
        FIXTURES / "sample_project" / "branching.py", "classify_number"
    )
    test_file = FIXTURES / "coverage_suite" / "baseline_classify.py"

    def execute(command: list[str], *_args: object, **_kwargs: object):
        if "json" in command:
            output = Path(command[command.index("-o") + 1])
            output.write_text("not json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("autotest.coverage_runner.subprocess.run", side_effect=execute):
        with pytest.raises(CoverageError, match="Could not read coverage JSON"):
            CoverageRunner().run(function, [test_file], tmp_path / "malformed")


def test_coverage_timeout_is_bounded_and_reported(tmp_path: Path) -> None:
    function = ProjectAnalyzer().analyze_function(
        FIXTURES / "sample_project" / "branching.py", "classify_number"
    )
    test_file = FIXTURES / "coverage_suite" / "baseline_classify.py"
    with patch(
        "autotest.coverage_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired([], 0.01, output="partial"),
    ):
        with pytest.raises(CoverageError, match="timed out"):
            CoverageRunner(timeout=0.01).run(function, [test_file], tmp_path / "timeout")

    assert (tmp_path / "timeout" / "stdout.txt").read_text(encoding="utf-8") == "partial"


def test_coverage_artifacts_use_exclusive_writes(tmp_path: Path) -> None:
    function = ProjectAnalyzer().analyze_function(
        FIXTURES / "sample_project" / "branching.py", "classify_number"
    )
    test_file = FIXTURES / "coverage_suite" / "baseline_classify.py"
    output = tmp_path / "coverage"
    CoverageRunner(timeout=10).run(function, [test_file], output)

    with pytest.raises(CoverageError, match="Refusing to overwrite"):
        CoverageRunner(timeout=10).run(function, [test_file], output)

    raw = json.loads((output / "coverage_raw.json").read_text(encoding="utf-8"))
    assert raw["meta"]["branch_coverage"] is True
