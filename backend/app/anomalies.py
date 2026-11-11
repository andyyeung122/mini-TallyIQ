"""Explainable anomaly flags for invoices.

Each rule is something a finance person would check by eye, written down so it
runs on every invoice. Flags never block anything by themselves: an approver can
still approve a flagged invoice, but has to leave a note saying why (see workflow).

Rules
- amount_outlier: total is far from this customer's usual invoice. Uses the median
  and MAD (median absolute deviation), which one huge past invoice can't skew the
  way a mean and standard deviation can. Modified z-score > 3.5 (Iglewicz-Hoaglin).
- cpm_outlier: a line's CPM is more than 50% off this customer's usual CPM.
- billed_over_booked: a line bills more impressions than the campaign booked.
- possible_duplicate: another invoice for the same customer has the same total
  within 31 days.
History-based rules need at least 3 earlier data points; otherwise they stay quiet
rather than guess.
"""
from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Campaign, Invoice

MIN_HISTORY = 3
Z_THRESHOLD = Decimal("3.5")
CPM_TOLERANCE = Decimal("0.5")
DUPLICATE_WINDOW_DAYS = 31


@dataclass
class Anomaly:
    code: str
    severity: str  # "warning" | "info"
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _usd(cents: float) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def _ratio_text(value: int, typical: float) -> str:
    if typical <= 0:
        return ""
    r = Decimal(value) / Decimal(typical)
    if r >= 1:
        return f"about {r.quantize(Decimal('0.1'), ROUND_HALF_UP)}× "
    return f"about {(r * 100).quantize(Decimal('1'), ROUND_HALF_UP)}% of "


def amount_outlier(total: int, history: list[int]) -> Anomaly | None:
    if len(history) < MIN_HISTORY:
        return None
    m = median(history)
    mad = median(abs(x - m) for x in history)
    if mad > 0:
        z = Decimal("0.6745") * Decimal(total - m) / Decimal(mad)
        unusual = abs(z) > Z_THRESHOLD
    else:  # every past invoice was identical; fall back to a plain ratio
        unusual = m > 0 and (total > 2 * m or total < m / 2)
    if not unusual:
        return None
    return Anomaly(
        "amount_outlier",
        "warning",
        f"{_usd(total)} is {_ratio_text(total, m)}this customer's typical invoice "
        f"({_usd(m)}, median of {len(history)} posted invoices).",
    )


def detect_all(db: Session) -> dict[int, list[Anomaly]]:
    """Flags for every invoice, computed in one pass (no per-invoice queries)."""
    invoices = db.scalars(select(Invoice).options(selectinload(Invoice.lines)).order_by(Invoice.id)).all()
    booked = dict(db.execute(select(Campaign.id, Campaign.booked_impressions)).all())

    by_customer: dict[int, list[Invoice]] = defaultdict(list)
    for inv in invoices:
        by_customer[inv.customer_id].append(inv)

    result: dict[int, list[Anomaly]] = {}
    for inv in invoices:
        flags: list[Anomaly] = []
        others = [o for o in by_customer[inv.customer_id] if o.id != inv.id]
        posted_others = [o for o in others if o.status == "posted"]

        # 1. amount vs this customer's history
        if a := amount_outlier(inv.total_cents, [o.total_cents for o in posted_others]):
            flags.append(a)

        # 2. CPM vs this customer's usual rate
        past_cpms = [line.cpm_cents for o in posted_others for line in o.lines]
        if len(past_cpms) >= MIN_HISTORY:
            typical_cpm = median(past_cpms)
            for line in inv.lines:
                if typical_cpm and abs(Decimal(line.cpm_cents) - Decimal(typical_cpm)) > CPM_TOLERANCE * Decimal(typical_cpm):
                    flags.append(
                        Anomaly(
                            "cpm_outlier",
                            "warning",
                            f"{line.description} is billed at {_usd(line.cpm_cents)} CPM; this customer "
                            f"usually pays {_usd(typical_cpm)} (median of {len(past_cpms)} lines).",
                        )
                    )

        # 3. billing more than was booked
        for line in inv.lines:
            b = booked.get(line.campaign_id)
            if b is not None and line.impressions > b:
                flags.append(
                    Anomaly(
                        "billed_over_booked",
                        "warning",
                        f"{line.description} bills {line.impressions:,} impressions but only {b:,} were booked.",
                    )
                )

        # 4. same amount, same customer, close together
        for o in others:
            if o.total_cents == inv.total_cents and abs((o.created_at - inv.created_at).days) <= DUPLICATE_WINDOW_DAYS:
                flags.append(
                    Anomaly(
                        "possible_duplicate",
                        "info",
                        f"Same total as invoice #{o.id} from {o.created_at.date():%b %d}. Check it isn't a duplicate.",
                    )
                )
                break

        result[inv.id] = flags
    return result
