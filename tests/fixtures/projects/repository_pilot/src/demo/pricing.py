from .tax import compute_tax


def normalize_amount(amount):
    return max(amount, 0)


def calculate_total(amount):
    subtotal = normalize_amount(amount)
    if subtotal >= 10:
        return subtotal + compute_tax(subtotal)
    return subtotal
