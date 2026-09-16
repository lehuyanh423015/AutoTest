from decimal import Decimal

raise RuntimeError("DEPENDENCY MODULE MUST NEVER BE IMPORTED")

TAX_RATE = Decimal("0.1")


def compute_tax(amount):
    return amount * TAX_RATE


def unused_tax_helper():
    return 999
