from pathlib import Path

from autotest.coverage_runner import CoverageResult
from autotest.project_analyzer import ProjectAnalyzer
from autotest.prompt_builder import PromptBuilder


def test_coverage_prompt_is_deterministic_and_complete(tmp_path: Path) -> None:
    source = tmp_path / "branching.py"
    source.write_text(
        "# heading\n\ndef classify(value):\n"
        "    if value < 0:\n"
        "        return 'negative'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    function = ProjectAnalyzer().analyze_function(source, "classify")
    coverage = CoverageResult(
        source.resolve(),
        "classify",
        (3, 4, 5, 6),
        (3, 4, 6),
        (5,),
        ((4, 6),),
        ((4, 5),),
        75.0,
        50.0,
        None,
        0.1,
    )

    prompt = PromptBuilder().build_coverage(
        function,
        coverage,
        ("def test_other():\n    assert classify(0) == 'other'\n",),
        2,
    )

    assert prompt == PromptBuilder().build_coverage(
        function,
        coverage,
        ("def test_other():\n    assert classify(0) == 'other'\n",),
        2,
    )
    assert "Coverage round: 2" in prompt
    assert "3 | def classify(value):" in prompt
    assert "Missing executable lines: 5" in prompt
    assert "4 -> 5" in prompt
    assert "Line coverage: 75.00%" in prompt
    assert "Branch coverage: 50.00%" in prompt
    assert "def test_other" in prompt
    assert "Generate only new tests" in prompt
    assert "Do not rewrite, reproduce, remove, weaken" in prompt
    assert "Do not use observed runtime output as the oracle" in prompt
    assert "Do not create tautological assertions" in prompt


def test_coverage_prompt_bounds_existing_test_context(tmp_path: Path) -> None:
    source = tmp_path / "target.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    function = ProjectAnalyzer().analyze_function(source, "target")
    coverage = CoverageResult(
        source.resolve(), "target", (1, 2), (1,), (2,), (), (), 50.0, None, None, 0.0
    )

    prompt = PromptBuilder().build_coverage(
        function, coverage, ("x" * 500,), 1, max_accepted_test_chars=100
    )

    assert "Branch coverage: N/A" in prompt
    assert "context truncated at configured character limit" in prompt
    assert len(prompt) < 3_000
