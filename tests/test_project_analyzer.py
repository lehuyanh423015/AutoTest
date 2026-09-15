from pathlib import Path

import pytest

from autotest.errors import AnalyzerError, FunctionNotFoundError
from autotest.project_analyzer import ProjectAnalyzer

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project" / "calculator.py"


def test_discovers_only_top_level_functions(tmp_path: Path) -> None:
    source = tmp_path / "target.py"
    source.write_text(
        "def top():\n"
        "    def nested():\n"
        "        return 1\n"
        "    return nested()\n\n"
        "class Example:\n"
        "    def method(self):\n"
        "        return 2\n\n"
        "async def async_top():\n"
        "    return 3\n",
        encoding="utf-8",
    )

    assert ProjectAnalyzer().discover_functions(source) == ("top", "async_top")


def test_extracts_function_source_and_metadata() -> None:
    info = ProjectAnalyzer().analyze_function(FIXTURE, "divide")

    assert info.file_path == FIXTURE.resolve()
    assert info.module_name == "calculator"
    assert info.function_name == "divide"
    assert info.source_code == (
        "def divide(a: float, b: float) -> float:\n"
        '    """Divide a by non-zero b."""\n'
        "    if b == 0:\n"
        '        raise ValueError("b must not be zero")\n'
        "    return a / b"
    )
    assert info.start_line == 8
    assert info.end_line == 12
    assert info.docstring == "Divide a by non-zero b."


def test_extracts_decorated_function_completely(tmp_path: Path) -> None:
    source = tmp_path / "decorated.py"
    source.write_text("@decorator\ndef target():\n    return 1\n", encoding="utf-8")

    info = ProjectAnalyzer().analyze_function(source, "target")

    assert info.start_line == 1
    assert info.source_code == "@decorator\ndef target():\n    return 1"


def test_function_not_found() -> None:
    with pytest.raises(FunctionNotFoundError, match="missing"):
        ProjectAnalyzer().analyze_function(FIXTURE, "missing")


def test_syntax_error_is_wrapped(tmp_path: Path) -> None:
    source = tmp_path / "broken.py"
    source.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(AnalyzerError, match="Invalid Python syntax"):
        ProjectAnalyzer().analyze_function(source, "broken")


def test_nonexistent_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AnalyzerError, match="does not exist"):
        ProjectAnalyzer().analyze_function(tmp_path / "missing.py", "example")


def test_non_python_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target.txt"
    source.write_text("def example(): pass", encoding="utf-8")

    with pytest.raises(AnalyzerError, match=r"\.py extension"):
        ProjectAnalyzer().analyze_function(source, "example")


def test_analysis_does_not_execute_module_level_code(tmp_path: Path) -> None:
    marker = tmp_path / "should_not_exist.txt"
    source = tmp_path / "dangerous.py"
    source.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('module was imported')\n\n"
        "def safe_target():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    info = ProjectAnalyzer().analyze_function(source, "safe_target")

    assert info.source_code == "def safe_target():\n    return 42"
    assert not marker.exists()


def test_extracts_async_multiline_function_from_utf8_space_path(tmp_path: Path) -> None:
    directory = tmp_path / "source files"
    directory.mkdir()
    source = directory / "mô_đun.py"
    source.write_text(
        "@trace\n"
        "async def calculate(\n"
        "    value: int,\n"
        "    factor: int = 2,\n"
        ") -> int:\n"
        '    """Nhân giá trị."""\n'
        "    return value * factor\n",
        encoding="utf-8",
    )

    info = ProjectAnalyzer().analyze_function(source, "calculate")

    assert info.file_path == source.resolve()
    assert info.module_name == "mô_đun"
    assert info.start_line == 1
    assert info.end_line == 7
    assert info.docstring == "Nhân giá trị."
    assert info.source_code.startswith("@trace\nasync def calculate(\n")
    assert info.source_code.endswith("    return value * factor")


def test_relative_path_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "relative path"
    directory.mkdir()
    source = directory / "target.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    info = ProjectAnalyzer().analyze_function(Path("relative path") / "target.py", "target")

    assert info.file_path == source.resolve()
