"""Lightweight structural metrics and warnings for generated test code."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TestStructureMetrics:
    """Diagnostic counts, not a claim about semantic test quality."""

    test_function_count: int
    assert_count: int


def analyze_test_structure(test_code: str) -> TestStructureMetrics:
    """Count test functions and assertions; invalid Python has zero counts."""
    try:
        tree = ast.parse(test_code)
    except SyntaxError:
        return TestStructureMetrics(test_function_count=0, assert_count=0)
    return TestStructureMetrics(
        test_function_count=sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            for node in ast.walk(tree)
        ),
        assert_count=sum(isinstance(node, ast.Assert) for node in ast.walk(tree)),
    )


def oracle_quality_warnings(previous_code: str | None, candidate_code: str) -> tuple[str, ...]:
    """Flag only obvious oracle degradation; candidates are not automatically rejected."""
    previous_metrics = analyze_test_structure(previous_code) if previous_code is not None else None
    try:
        tree = ast.parse(candidate_code)
    except SyntaxError:
        return ("INVALID_PYTHON",)

    warnings: set[str] = set()
    candidate_metrics = analyze_test_structure(candidate_code)
    if (
        previous_metrics is not None
        and previous_metrics.assert_count > 0
        and candidate_metrics.assert_count == 0
    ):
        warnings.add("ALL_ASSERTIONS_REMOVED")

    assignments = {
        node.targets[0].id: ast.dump(node.value, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            warnings.add("ASSERT_TRUE")
        if not isinstance(node.test, ast.Compare) or len(node.test.comparators) != 1:
            continue
        left = node.test.left
        right = node.test.comparators[0]
        left_dump = ast.dump(left, include_attributes=False)
        right_dump = ast.dump(right, include_attributes=False)
        if left_dump == right_dump:
            warnings.add("SELF_COMPARISON")
        if isinstance(left, ast.Call) and isinstance(right, ast.Name):
            if assignments.get(right.id) == left_dump:
                warnings.add("SELF_DERIVED_ORACLE")
        if isinstance(right, ast.Call) and isinstance(left, ast.Name):
            if assignments.get(left.id) == right_dump:
                warnings.add("SELF_DERIVED_ORACLE")
    return tuple(sorted(warnings))
