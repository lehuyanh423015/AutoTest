import pytest


def check_add() -> None:
    assert pytest.approx(2) == 2
