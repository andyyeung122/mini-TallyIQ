import pytest
from sqlalchemy import select

from app.models import AuditEvent, IntegrityViolation, Invoice
from tests.conftest import as_user


def credit(client, invoice_id, amount_cents, user="priya", reason="Make-good for under-delivery"):
    return client.post(
        f"/invoices/{invoice_id}/credit-memos",
        json={"amount_cents": amount_cents, "reason": reason},
        headers=as_user(user),
    )


def test_credit_only_on_posted(client, draft_invoice):
    assert credit(client, draft_invoice["id"], 1_000).status_code == 409


def test_credit_flow_reduces_balance(client, posted_invoice):
    i = posted_invoice["id"]
    assert "credit" not in posted_invoice["allowed_actions"]  # marcus isn't a preparer
    inv = credit(client, i, 50_000).json()
    memo = inv["credit_memos"][0]
    assert memo["status"] == "pending_approval"
    assert inv["balance_cents"] == 500_000  # pending credits don't reduce the balance yet

    r = client.post(f"/credit-memos/{memo['id']}/actions/approve", headers=as_user("priya"))
    assert r.status_code == 403  # creator can't approve their own memo
    inv = client.post(f"/credit-memos/{memo['id']}/actions/approve", headers=as_user("marcus")).json()
    assert inv["credit_memos"][0]["status"] == "approved"
    assert inv["balance_cents"] == 450_000
    assert inv["total_cents"] == 500_000  # the posted invoice itself is untouched


def test_credits_cannot_exceed_invoice(client, posted_invoice):
    i = posted_invoice["id"]
    assert credit(client, i, 400_000).status_code == 201
    assert credit(client, i, 100_001).status_code == 422  # 400k pending, only 100k left
    assert credit(client, i, 100_000).status_code == 201


def test_posted_invoice_is_immutable_below_the_api(session_factory, posted_invoice):
    with session_factory() as db:
        inv = db.get(Invoice, posted_invoice["id"])
        inv.total_cents = 1
        with pytest.raises(IntegrityViolation):
            db.flush()


def test_audit_log_is_append_only(session_factory, posted_invoice):
    with session_factory() as db:
        event = db.scalars(select(AuditEvent).limit(1)).one()
        event.actor = "someone-else"
        with pytest.raises(IntegrityViolation):
            db.flush()
        db.rollback()
        db.delete(db.scalars(select(AuditEvent).limit(1)).one())
        with pytest.raises(IntegrityViolation):
            db.flush()
