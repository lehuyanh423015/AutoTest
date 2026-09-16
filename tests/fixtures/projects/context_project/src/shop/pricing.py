from decimal import Decimal

import unrelated_module  # noqa: F401

from .config import DEFAULT_DISCOUNT
from .models import Order
from .tax import compute_tax as tax_for
from .utils import *  # noqa: F403

raise RuntimeError("TARGET MODULE MUST NEVER BE IMPORTED")


def normalize_total(value: Decimal) -> Decimal:
    return max(value, Decimal("0"))


def unrelated_helper():
    return "unused"


def calculate_total(order: Order, discount=DEFAULT_DISCOUNT):
    subtotal = normalize_total(order.subtotal)
    return subtotal + tax_for(subtotal) - discount
