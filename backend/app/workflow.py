"""Invoice and credit memo state machines.

    draft --submit--> pending_approval --approve--> approved --post--> posted
      ^                    |
      +-------reject-------+   (reason required)

Credit memos (posted invoices only): pending_approval --approve/reject--> approved/rejected
"""
from dataclasses import dataclass
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from . import audit, sync
from .auth import require_role
from .models import CreditMemo, Customer, Invoice, Payment, User, local_date, utcnow


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    role: str


INVOICE_TRANSITIONS = {
    "submit": Transition("draft", "pending_approval", "preparer"),
    "approve": Transition("pending_approval", "approved", "approver"),
    "reject": Transition("pending_approval", "draft", "approver"),
    "post": Transition("approved", "posted", "approver"),
}

MEMO_TRANSITIONS = {
    "approve": Transition("pending_approval", "approved", "approver"),
    "reject": Transition("pending_approval", "rejected", "approver"),
}


# ---- balances ----
def paid_cents(inv: Invoice) -> int:
    return sum(p.amount_cents for p in inv.payments)


def balance_cents(inv: Invoice) -> int:
    """What the customer owes: total - approved credits - payments."""
    credited = sum(m.amount_cents for m in inv.credit_memos if m.status == "approved")
    return inv.total_cents - credited - paid_cents(inv)


def creditable_cents(inv: Invoice) -> int:
    """What can still be credited or paid, also holding back credits awaiting approval,
    so a pending credit plus a payment can never exceed the invoice."""
    held = sum(m.amount_cents for m in inv.credit_memos if m.status in ("approved", "pending_approval"))
    return inv.total_cents - held - paid_cents(inv)


# ---- segregation of duties ----
def _invoice_sod_violation(action: str, inv: Invoice, user: User) -> str | None:
    if action in ("approve", "reject") and user.username in (inv.created_by, inv.submitted_by):
        return "You can't approve or reject an invoice you prepared or submitted"
    return None


def _memo_sod_violation(memo: CreditMemo, user: User) -> str | None:
    if user.username == memo.created_by:
        return "You can't approve or reject a credit memo you created"
    return None


# ---- what the current user may do (the UI renders exactly this) ----
def invoice_actions(inv: Invoice, user: User) -> list[str]:
    actions = [
        name
        for name, t in INVOICE_TRANSITIONS.items()
        if inv.status == t.source and user.has_role(t.role) and not _invoice_sod_violation(name, inv, user)
    ]
    if inv.status == "posted" and user.has_role("preparer") and creditable_cents(inv) > 0:
        actions += ["credit", "pay"]
    return actions


def memo_actions(memo: CreditMemo, user: User) -> list[str]:
    return [
        name
        for name, t in MEMO_TRANSITIONS.items()
        if memo.status == t.source and user.has_role(t.role) and not _memo_sod_violation(memo, user)
    ]


# ---- transitions ----
def apply_invoice_action(
    db: Session, inv: Invoice, action: str, user: User, reason: str | None, anomaly_count: int = 0
) -> None:
    t = INVOICE_TRANSITIONS.get(action)
    if t is None:
        raise HTTPException(400, f"Unknown action: {action}")
    if inv.status != t.source:
        raise HTTPException(409, f"Can't {action} an invoice that is {inv.status.replace('_', ' ')}")
    require_role(user, t.role)
    if msg := _invoice_sod_violation(action, inv, user):
        raise HTTPException(403, msg)
    if action == "reject" and not (reason and reason.strip()):
        raise HTTPException(422, "Give a reason when sending an invoice back")
    if action == "approve" and anomaly_count and not (reason and reason.strip()):
        # Human in the loop: the flags don't block approval, but someone has to say why it's OK.
        raise HTTPException(
            422, f"This invoice has {anomaly_count} flag(s). Add a note explaining why it's OK to approve."
        )

    extra: dict = {}
    if action == "submit":
        extra = {"submitted_by": user.username}
    elif action == "approve":
        extra = {"approved_by": user.username}
    elif action == "reject":
        extra = {"approved_by": None}
    elif action == "post":
        now = utcnow()
        terms = db.get(Customer, inv.customer_id).payment_terms_days
        extra = {"posted_at": now, "due_on": local_date(now) + timedelta(days=terms)}

    before = audit.invoice_snapshot(inv)
    # Conditional update: if two people act at once, only one wins; the other gets a 409.
    result = db.execute(
        update(Invoice)
        .where(Invoice.id == inv.id, Invoice.status == t.source)
        .values(status=t.target, **extra)
    )
    if result.rowcount == 0:
        db.rollback()
        raise HTTPException(409, "This invoice was just changed by someone else; refresh and try again")
    db.refresh(inv)
    audit.record(
        db,
        entity_type="invoice",
        entity_id=inv.id,
        invoice_id=inv.id,
        action=action,
        actor=user.username,
        before=before,
        after=audit.invoice_snapshot(inv),
        reason=reason,
    )
    if action == "post":
        sync.enqueue(db, inv)  # same transaction: posted <=> queued for NetSuite
    db.commit()


def create_credit_memo(db: Session, inv: Invoice, amount_cents: int, reason: str, user: User) -> CreditMemo:
    if inv.status != "posted":
        raise HTTPException(409, "Credit memos apply to posted invoices only")
    require_role(user, "preparer")
    available = creditable_cents(inv)
    if amount_cents > available:
        raise HTTPException(422, f"Credit can't exceed the remaining ${available / 100:,.2f} on this invoice")

    memo = CreditMemo(invoice_id=inv.id, amount_cents=amount_cents, reason=reason, created_by=user.username)
    db.add(memo)
    db.flush()  # assigns memo.id for the audit record
    audit.record(
        db,
        entity_type="credit_memo",
        entity_id=memo.id,
        invoice_id=inv.id,
        action="create",
        actor=user.username,
        before=None,
        after=audit.memo_snapshot(memo),
        reason=reason,
    )
    db.commit()
    db.refresh(memo)
    return memo


def apply_memo_action(db: Session, memo: CreditMemo, action: str, user: User, reason: str | None) -> None:
    t = MEMO_TRANSITIONS.get(action)
    if t is None:
        raise HTTPException(400, f"Unknown action: {action}")
    if memo.status != t.source:
        raise HTTPException(409, f"Can't {action} a credit memo that is {memo.status.replace('_', ' ')}")
    require_role(user, t.role)
    if msg := _memo_sod_violation(memo, user):
        raise HTTPException(403, msg)

    before = audit.memo_snapshot(memo)
    result = db.execute(
        update(CreditMemo)
        .where(CreditMemo.id == memo.id, CreditMemo.status == t.source)
        .values(status=t.target, decided_by=user.username, decided_at=utcnow())
    )
    if result.rowcount == 0:
        db.rollback()
        raise HTTPException(409, "This credit memo was just changed by someone else; refresh and try again")
    db.refresh(memo)
    audit.record(
        db,
        entity_type="credit_memo",
        entity_id=memo.id,
        invoice_id=memo.invoice_id,
        action=action,
        actor=user.username,
        before=before,
        after=audit.memo_snapshot(memo),
        reason=reason,
    )
    db.commit()


def record_payment(
    db: Session, inv: Invoice, amount_cents: int, received_on: date, reference: str, user: User
) -> Payment:
    if inv.status != "posted":
        raise HTTPException(409, "Payments apply to posted invoices only")
    require_role(user, "preparer")
    if received_on > date.today():
        raise HTTPException(422, "The received date can't be in the future")
    if inv.posted_at and received_on < local_date(inv.posted_at):
        raise HTTPException(422, "The received date is before the invoice was posted")
    open_cents = creditable_cents(inv)
    if amount_cents > open_cents:
        raise HTTPException(
            422,
            f"Payment is more than the ${open_cents / 100:,.2f} still open on this invoice "
            "(credits awaiting approval are held back). Record the overpayment separately.",
        )

    payment = Payment(
        invoice_id=inv.id,
        amount_cents=amount_cents,
        received_on=received_on,
        reference=reference,
        recorded_by=user.username,
    )
    db.add(payment)
    db.flush()
    audit.record(
        db,
        entity_type="payment",
        entity_id=payment.id,
        invoice_id=inv.id,
        action="payment",
        actor=user.username,
        before=None,
        after={"amount_cents": amount_cents, "received_on": received_on.isoformat(), "reference": reference},
    )
    db.commit()
    return payment
