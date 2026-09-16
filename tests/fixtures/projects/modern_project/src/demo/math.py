def add(left: int, right: int) -> int:
    def nested() -> int:
        return left

    return nested() + right


async def async_add(left: int, right: int) -> int:
    return add(left, right)


class Calculator:
    def method(self) -> int:
        return 1
