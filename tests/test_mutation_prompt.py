from pathlib import Path

from autotest.mutation_runner import SurvivingMutant
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder


def test_mutation_feedback_prompt_is_deterministic_bounded_and_constrained(
    tmp_path: Path,
) -> None:
    function = FunctionInfo(
        (tmp_path / "boundary.py").resolve(),
        "boundary",
        "classify",
        'def classify(value):\n    if value >= 10:\n        return "large"\n    return "small"',
        10,
        13,
        None,
    )
    survivors = (
        SurvivingMutant(
            "boundary.x_classify__mutmut_1",
            "classify",
            "- if value >= 10:\n+ if value > 10:",
        ),
    )
    builder = PromptBuilder()

    first = builder.build_mutation_feedback(
        function,
        ('def test_existing():\n    assert classify(9) == "small"',),
        survivors,
        2,
    )
    second = builder.build_mutation_feedback(
        function,
        ('def test_existing():\n    assert classify(9) == "small"',),
        survivors,
        2,
    )

    assert first == second
    assert len(first) <= 12_000
    for text in (
        "Mutation feedback round: 2",
        "Original target module: boundary",
        "10 | def classify(value):",
        "Accepted test module 1",
        "boundary.x_classify__mutmut_1",
        "+ if value > 10:",
        "only ADDITIONAL pytest tests",
        "never mutant behavior",
        "behaviorally equivalent",
        "Do not create tautologies",
        "use assert True",
        "without Markdown fences",
    ):
        assert text in first
