from boundary import classify_boundary


def test_values_away_from_boundary() -> None:
    assert classify_boundary(9) == "small"
    assert classify_boundary(11) == "large"
