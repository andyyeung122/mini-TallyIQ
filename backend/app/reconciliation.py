"""Compare what the ad server says was delivered with what was invoiced.

Variance = reported impressions - billed impressions
  negative -> over-billed (we charged for impressions that didn't deliver)
  positive -> under-billed (revenue we haven't invoiced)
A row is flagged when |variance| is more than the threshold percent of what was billed.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .delivery import reported_impressions
from .models import Campaign, Customer, Invoice, InvoiceLine, User, VarianceResolution
from .money import line_amount_cents


@dataclass
class ReconRow:
    campaign_id: int
    campaign_name: str
    customer_name: str
    cpm_cents: int
    booked_impressions: int
    reported_impressions: int | None
    billed_impressions: int | None
    invoice_id: int | None
    invoice_status: str | None
    variance_impressions: int | None
    variance_pct: Decimal | None
    variance_cents: int | None
    status: str  # matched | over_billed | under_billed | not_billed | no_delivery_data
    flagged: bool
    resolved: bool
    resolution_note: str | None
    resolved_by: str | None
    suggestion: str | None


def _pct(variance: int, base: int) -> Decimal | None:
    if base == 0:
        return None
    return (Decimal(variance) * 100 / Decimal(base)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _signed_amount(variance: int, cpm_cents: int) -> int:
    amount = line_amount_cents(abs(variance), cpm_cents)
    return -amount if variance < 0 else amount


def _suggestion(status: str, variance_cents: int | None, invoice_id: int | None, invoice_status: str | None) -> str | None:
    dollars = f"${abs(variance_cents or 0) / 100:,.2f}"
    if status == "over_billed":
        if invoice_status == "posted":
            return f"Request a {dollars} credit memo on invoice #{invoice_id}"
        return f"Send invoice #{invoice_id} back and correct the impressions before it posts"
    if status == "under_billed":
        return f"Confirm the billing count; about {dollars} may be unbilled"
    if status == "not_billed":
        return f"About {dollars} delivered but not invoiced yet; generate an invoice"
    return None


def reconcile(db: Session, threshold_pct: Decimal) -> list[ReconRow]:
    reported = reported_impressions(db)
    billed = {
        line.campaign_id: (line, status)
        for line, status in db.execute(
            select(InvoiceLine, Invoice.status).join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        )
    }
    latest_resolution: dict[int, VarianceResolution] = {}
    for res in db.scalars(select(VarianceResolution).order_by(VarianceResolution.id)):
        latest_resolution[res.campaign_id] = res

    rows: list[ReconRow] = []
    for campaign, customer_name in db.execute(
        select(Campaign, Customer.name).join(Customer, Customer.id == Campaign.customer_id)
    ):
        rep = reported.get(campaign.id)
        line, inv_status = billed.get(campaign.id, (None, None))
        billed_imps = line.impressions if line else None

        variance = pct = cents = None
        flagged = False
        if rep is None:
            status = "no_delivery_data"
        elif line is None:
            status = "not_billed"
            variance = rep
            cents = _signed_amount(variance, campaign.cpm_cents)
            flagged = rep > 0
        else:
            variance = rep - line.impressions
            pct = _pct(variance, line.impressions)
            cents = _signed_amount(variance, campaign.cpm_cents)
            status = "matched" if variance == 0 else ("over_billed" if variance < 0 else "under_billed")
            flagged = variance != 0 and (pct is None or abs(pct) > threshold_pct)

        res = latest_resolution.get(campaign.id)
        resolved = bool(flagged and res and res.variance_impressions == variance)
        rows.append(
            ReconRow(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                customer_name=customer_name,
                cpm_cents=campaign.cpm_cents,
                booked_impressions=campaign.booked_impressions,
                reported_impressions=rep,
                billed_impressions=billed_imps,
                invoice_id=line.invoice_id if line else None,
                invoice_status=inv_status,
                variance_impressions=variance,
                variance_pct=pct,
                variance_cents=cents,
                status=status,
                flagged=flagged,
                resolved=resolved,
                resolution_note=res.note if resolved else None,
                resolved_by=res.resolved_by if resolved else None,
                suggestion=_suggestion(status, cents, line.invoice_id if line else None, inv_status)
                if flagged and not resolved
                else None,
            )
        )

    # Open problems first, biggest money first.
    rows.sort(key=lambda r: (not (r.flagged and not r.resolved), -abs(r.variance_cents or 0), r.campaign_id))
    return rows


def resolve(db: Session, campaign_id: int, note: str, threshold_pct: Decimal, user: User) -> None:
    row = next((r for r in reconcile(db, threshold_pct) if r.campaign_id == campaign_id), None)
    if row is None:
        raise HTTPException(404, "Campaign not found")
    if not row.flagged:
        raise HTTPException(409, "This campaign has no flagged variance to resolve")
    if row.resolved:
        raise HTTPException(409, "This variance is already resolved")

    res = VarianceResolution(
        campaign_id=campaign_id,
        variance_impressions=row.variance_impressions,
        note=note,
        resolved_by=user.username,
    )
    db.add(res)
    db.flush()
    audit.record(
        db,
        entity_type="variance_resolution",
        entity_id=res.id,
        invoice_id=row.invoice_id,
        action="resolve",
        actor=user.username,
        before={"status": row.status, "variance_impressions": row.variance_impressions},
        after={"status": "resolved", "variance_cents": row.variance_cents},
        reason=note,
    )
    db.commit()
