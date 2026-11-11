"""Load six months of fictional demo data so the dashboard has something to show.

    python -m app.seed

Only runs on an empty database. Everything is invented: customers, amounts,
and payment habits. The same data comes out every time (fixed random seed),
with dates relative to today.
"""
import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from .auth import seed_users
from .database import Base, SessionLocal, engine
from .models import AuditEvent, Campaign, Customer, Invoice, InvoiceLine, Payment, SyncJob
from .money import line_amount_cents

# name, terms, typical impressions, CPM cents, days late on average, unpaid for the last N months
CUSTOMERS = [
    ("Riverside Auto Group", 30, 800_000, 600, 4, 0),
    ("Harbor Dental", 15, 150_000, 1_200, 0, 0),
    ("Maple Street Bakery", 30, 60_000, 900, 22, 0),
    ("Summit HVAC", 45, 400_000, 700, 38, 1),
    ("Lakeview Credit Union", 30, 500_000, 1_000, 1, 0),
    ("Crescent Furniture", 60, 300_000, 650, 10, 4),  # stopped paying a while ago
]


def at(d: date, hour: int = 15) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


def month_start(today: date, back: int) -> date:
    y, m = today.year, today.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    rng = random.Random(42)
    today = date.today()

    with SessionLocal() as db:
        seed_users(db)
        if db.scalar(select(Customer.id).limit(1)) is not None:
            print("The database already has data. Delete backend/tallyiq.db to start fresh.")
            return

        def audit(entity_type, entity_id, invoice_id, action, actor, when, after, reason=None):
            db.add(AuditEvent(entity_type=entity_type, entity_id=entity_id, invoice_id=invoice_id, action=action,
                              actor=actor, before=None, after=after, reason=reason, at=when))

        def add_invoice(customer, name, delivered, booked, cpm, created, status, posted=None):
            campaign = Campaign(customer_id=customer.id, name=name, booked_impressions=booked,
                                delivered_impressions=delivered, cpm_cents=cpm)
            db.add(campaign)
            db.flush()
            amount = line_amount_cents(delivered, cpm)
            inv = Invoice(
                customer_id=customer.id, status=status, total_cents=amount, created_at=at(created, 10),
                created_by="priya",
                submitted_by="priya" if status != "draft" else None,
                approved_by="marcus" if status in ("approved", "posted") else None,
                posted_at=at(posted) if posted else None,
                due_on=posted + timedelta(days=customer.payment_terms_days) if posted else None,
            )
            inv.lines.append(InvoiceLine(campaign_id=campaign.id, description=name, impressions=delivered,
                                         cpm_cents=cpm, amount_cents=amount))
            db.add(inv)
            db.flush()
            audit("invoice", inv.id, inv.id, "create", "priya", at(created, 10), {"status": "draft", "total_cents": amount})
            if posted:
                audit("invoice", inv.id, inv.id, "post", "marcus", at(posted), {"status": "posted"})
                db.add(SyncJob(invoice_id=inv.id, idempotency_key=f"tallyiq-invoice-{inv.id}", status="synced",
                               attempts=1, external_id=f"NS-{5000 + inv.id}", synced_at=at(posted, 16),
                               next_attempt_at=at(posted, 16)))
            return inv

        n_invoices = n_payments = 0
        for name, terms, base, cpm, late, unpaid_months in CUSTOMERS:
            customer = Customer(name=name, email=f"ap@{name.split()[0].lower()}.example", payment_terms_days=terms)
            db.add(customer)
            db.flush()

            for back in range(6, 0, -1):
                posted = month_start(today, back) + timedelta(days=2)
                delivered = round(base * rng.uniform(0.85, 1.15), -3)
                booked = round(delivered * 1.05, -3)
                flight = posted.replace(day=1) - timedelta(days=1)
                inv = add_invoice(customer, f"{flight:%B} flight", int(delivered), int(booked),
                                  cpm + rng.choice([-25, 0, 0, 25]), posted - timedelta(days=2), "posted", posted)
                n_invoices += 1

                paid_on = inv.due_on + timedelta(days=max(-terms + 3, late + rng.randint(-3, 3)))
                if back > unpaid_months and paid_on <= today:
                    db.add(Payment(invoice_id=inv.id, amount_cents=inv.total_cents, received_on=paid_on,
                                   reference=f"ACH {rng.randint(100000, 999999)}", recorded_by="priya",
                                   recorded_at=at(paid_on, 17)))
                    audit("payment", inv.id, inv.id, "payment", "priya", at(paid_on, 17),
                          {"amount_cents": inv.total_cents, "received_on": paid_on.isoformat()})
                    n_payments += 1

        customers = {c.name: c for c in db.scalars(select(Customer))}
        # Work in progress for the approval queue, including invoices the anomaly rules should catch.
        add_invoice(customers["Riverside Auto Group"], "Holiday Blitz", 3_200_000, 3_300_000, 1_500,
                    today - timedelta(days=1), "pending_approval")  # ~10x usual amount, 2.5x usual CPM
        add_invoice(customers["Harbor Dental"], "New Patient Push", 182_000, 150_000, 1_200,
                    today, "draft")  # bills more than booked
        add_invoice(customers["Lakeview Credit Union"], "Auto Loan Rates", 510_000, 525_000, 1_000,
                    today - timedelta(days=2), "pending_approval")  # normal
        db.commit()
        print(f"Seeded {len(CUSTOMERS)} customers, {n_invoices} posted invoices, {n_payments} payments, "
              "and 3 invoices in progress.")


if __name__ == "__main__":
    main()
