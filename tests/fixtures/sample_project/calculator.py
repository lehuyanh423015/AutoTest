"""Small standalone target module used by AutoTest's tests and examples."""


def add(a: int, b: int) -> int:
    return a + b


def divide(a: float, b: float) -> float:
    """Divide a by non-zero b."""
    if b == 0:
        raise ValueError("b must not be zero")
    return a / b
