from classifier import classify_number


def test_negative_number() -> None:
    assert classify_number(-1) == "negative"


def test_zero() -> None:
    assert classify_number(0) == "zero"
