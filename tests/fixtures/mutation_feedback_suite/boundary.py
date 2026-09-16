"""Deterministic Phase 4B boundary-condition target."""


def classify_boundary(value: int) -> str:
    """Classify values at a deliberately testable inclusive boundary."""
    if value >= 10:
        return "large"
    return "small"
