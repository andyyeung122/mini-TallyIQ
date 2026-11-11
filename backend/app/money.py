"""All money math lives here. Money is always integer cents; never floats."""
from decimal import ROUND_HALF_UP, Decimal


def line_amount_cents(impressions: int, cpm_cents: int) -> int:
    """Amount owed for `impressions` at a CPM (cost per 1,000) given in cents.

    Rounds half up to the nearest cent, the usual invoicing convention.
    """
    amount = Decimal(impressions) * Decimal(cpm_cents) / Decimal(1000)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
