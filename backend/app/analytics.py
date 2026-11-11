"""AR aging and dashboard numbers, computed as of a date.

As-of reporting is point-in-time: only invoices posted, payments received and
credits approved on or before the as-of date count. So the aging for Sept 30 is the
same whether you run it on Oct 1 or next March, which is what auditors expect.

Aging buckets are by days past due:
  current (not yet due) | 1-30 | 31-60 | 61+
"""
import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Customer, Invoice, SyncJob, local_date

BUCKETS = [("current", "Current"), ("1_30", "1–30 days"), ("31_60", "31–60 days"), ("61_plus", "61+ days")]


def bucket_for(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "1_30"
    if days_past_due <= 60:
        return "31_60"
    return "61_plus"


@dataclass
class OpenItem:
    invoice_id: int
    customer_id: int
    customer_name: str
    posted_on: date
    due_on: date
    days_past_due: int
    bucket: str
    balance_cents: int


def _day(dt) -> date:
    return local_date(dt)


def balance_as_of(inv: Invoice, as_of: date) -> int:
    credited = sum(
        m.amount_cents
        for m in inv.credit_memos
        if m.status == "approved" and m.decided_at is not None and _day(m.decided_at) <= as_of
    )
    paid = sum(p.amount_cents for p in inv.payments if p.received_on <= as_of)
    return inv.total_cents - credited - paid


def _posted_invoices(db: Session) -> list[Invoice]:
    return list(
        db.scalars(
            select(Invoice)
            .where(Invoice.status == "posted")
            .options(selectinload(Invoice.payments), selectinload(Invoice.credit_memos))
        ).all()
    )


def open_items(db: Session, as_of: date, invoices: list[Invoice] | None = None) -> list[OpenItem]:
    names = dict(db.execute(select(Customer.id, Customer.name)).all())
    items = []
    for inv in invoices if invoices is not None else _posted_invoices(db):
        if inv.posted_at is None or inv.due_on is None or _day(inv.posted_at) > as_of:
            continue
        bal = balance_as_of(inv, as_of)
        if bal <= 0:
            continue
        dpd = (as_of - inv.due_on).days
        items.append(
            OpenItem(
                invoice_id=inv.id,
                customer_id=inv.customer_id,
                customer_name=names.get(inv.customer_id, f"Customer {inv.customer_id}"),
                posted_on=_day(inv.posted_at),
                due_on=inv.due_on,
                days_past_due=dpd,
                bucket=bucket_for(dpd),
                balance_cents=bal,
            )
        )
    return sorted(items, key=lambda i: (-i.days_past_due, -i.balance_cents))


def aging(items: list[OpenItem]) -> dict:
    totals = {key: {"key": key, "label": label, "cents": 0, "count": 0} for key, label in BUCKETS}
    customers: dict[int, dict] = {}
    for it in items:
        totals[it.bucket]["cents"] += it.balance_cents
        totals[it.bucket]["count"] += 1
        row = customers.setdefault(
            it.customer_id,
            {"customer_id": it.customer_id, "customer_name": it.customer_name, "total_cents": 0}
            | {key: 0 for key, _ in BUCKETS},
        )
        row[it.bucket] += it.balance_cents
        row["total_cents"] += it.balance_cents
    return {
        "buckets": list(totals.values()),
        "by_customer": sorted(customers.values(), key=lambda r: -r["total_cents"]),
    }


def aging_csv(db: Session, as_of: date) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["customer", "invoice", "posted_on", "due_on", "days_past_due", "bucket", "balance"])
    for it in open_items(db, as_of):
        w.writerow(
            [it.customer_name, it.invoice_id, it.posted_on, it.due_on, it.days_past_due,
             dict(BUCKETS)[it.bucket], f"{it.balance_cents / 100:.2f}"]
        )
    return out.getvalue()


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def dashboard(db: Session, as_of: date, flagged_ids: set[int], open_variances: int) -> dict:
    posted = _posted_invoices(db)
    items = open_items(db, as_of, posted)

    def billed_between(start: date, end: date) -> int:  # end inclusive
        return sum(i.total_cents for i in posted if i.posted_at and start <= _day(i.posted_at) <= end)

    def collected_between(start: date, end: date) -> int:
        return sum(p.amount_cents for i in posted for p in i.payments if start <= p.received_on <= end)

    outstanding = sum(i.balance_cents for i in items)
    past_due = sum(i.balance_cents for i in items if i.days_past_due > 0)
    billed_90 = billed_between(as_of - timedelta(days=89), as_of)

    months = []
    for back in range(5, -1, -1):
        start = _month_start(as_of, back)
        end = min(_month_start(as_of, back - 1) - timedelta(days=1), as_of) if back > 0 else as_of
        months.append(
            {"month": start.strftime("%b %Y"), "billed_cents": billed_between(start, end),
             "collected_cents": collected_between(start, end)}
        )

    pipeline = {}
    for status in ("draft", "pending_approval", "approved"):
        rows = db.scalars(select(Invoice).where(Invoice.status == status)).all()
        pipeline[status] = {
            "count": len(rows),
            "cents": sum(r.total_cents for r in rows),
            "flagged": sum(1 for r in rows if r.id in flagged_ids),
        }

    return {
        "as_of": as_of,
        "kpis": {
            "outstanding_cents": outstanding,
            "past_due_cents": past_due,
            "open_invoices": len(items),
            "billed_30d_cents": billed_between(as_of - timedelta(days=29), as_of),
            "collected_30d_cents": collected_between(as_of - timedelta(days=29), as_of),
            # Days sales outstanding: how many days of billing are still unpaid.
            "dso_days": round(outstanding / billed_90 * 90) if billed_90 else None,
        },
        "monthly": months,
        "aging": aging(items),
        "past_due": [vars(i) for i in items if i.days_past_due > 0][:8],
        "pipeline": pipeline,
        "attention": {
            "awaiting_approval": pipeline["pending_approval"]["count"],
            "flagged_awaiting_approval": pipeline["pending_approval"]["flagged"],
            "sync_failures": len(db.scalars(select(SyncJob.id).where(SyncJob.status == "dead")).all()),
            "open_variances": open_variances,
        },
    }
