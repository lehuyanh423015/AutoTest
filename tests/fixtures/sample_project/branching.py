"""Deterministic branch target for coverage-guidance tests and demonstrations."""


def classify_number(value: int) -> str:
    """Classify an integer by its sign."""
    if value < 0:
        return "negative"
    if value == 0:
        return "zero"
    return "positive"
