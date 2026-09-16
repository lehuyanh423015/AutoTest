"""Non-obvious arithmetic boundary used by the real Phase 4B CLI demonstration."""


def calculate_fee(amount: float) -> float:
    """Calculate a fee with a threshold applied after an affine transformation."""
    adjusted = amount * 3 + 7
    if adjusted >= 101:
        return round((adjusted - 101) * 0.25, 2)
    return round(adjusted * 0.1, 2)
