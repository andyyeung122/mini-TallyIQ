from sqlalchemy.orm import Session

from .models import AuditEvent, CreditMemo, Invoice


def invoice_snapshot(inv: Invoice) -> dict:
    return {
        "status": inv.status,
        "total_cents": inv.total_cents,
        "submitted_by": inv.submitted_by,
        "approved_by": inv.approved_by,
    }


def memo_snapshot(memo: CreditMemo) -> dict:
    return {"status": memo.status, "amount_cents": memo.amount_cents, "decided_by": memo.decided_by}


def record(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    invoice_id: int | None,
    action: str,
    actor: str,
    before: dict | None,
    after: dict | None,
    reason: str | None = None,
) -> None:
    """Adds the event to the caller's transaction, so the change and its audit
    record commit together or not at all."""
    db.add(
        AuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            invoice_id=invoice_id,
            action=action,
            actor=actor,
            before=before,
            after=after,
            reason=reason,
        )
    )
