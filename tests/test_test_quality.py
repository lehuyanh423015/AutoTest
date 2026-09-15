import pytest

from autotest.test_quality import analyze_test_structure, oracle_quality_warnings


def test_structural_metrics() -> None:
    metrics = analyze_test_structure(
        "def test_one():\n    assert 1 == 1\n\ndef helper():\n    assert True\n"
    )

    assert metrics.test_function_count == 1
    assert metrics.assert_count == 2


@pytest.mark.parametrize(
    ("code", "warning"),
    [
        ("def test_x():\n    assert True\n", "ASSERT_TRUE"),
        ("def test_x():\n    assert x == x\n", "SELF_COMPARISON"),
        (
            "def test_x():\n    result = target(1)\n    assert target(1) == result\n",
            "SELF_DERIVED_ORACLE",
        ),
    ],
)
def test_obvious_oracle_degradation_is_flagged(code: str, warning: str) -> None:
    assert warning in oracle_quality_warnings(None, code)


def test_assertion_removal_and_invalid_python_are_flagged() -> None:
    previous = "def test_x():\n    assert target() == 1\n"

    assert "ALL_ASSERTIONS_REMOVED" in oracle_quality_warnings(
        previous, "def test_x():\n    target()\n"
    )
    assert oracle_quality_warnings(previous, "def broken(:\n") == ("INVALID_PYTHON",)
