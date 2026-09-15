from branching import classify_number


def test_positive() -> None:
    assert classify_number(7) == "positive"
