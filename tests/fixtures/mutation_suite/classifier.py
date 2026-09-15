def classify_number(value: int) -> str:
    if value < 0:
        return "negative"
    if value == 0:
        return "zero"
    return "positive"


def unrelated(value: int) -> int:
    return value * 2
