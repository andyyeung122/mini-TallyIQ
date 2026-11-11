from datetime import date, timedelta

from app.anomalies import amount_outlier
from app.models import Payment
from tests.conftest import as_user

import pytest
from app.models import IntegrityViolation


def pay(client, invoice_id, cents, user="priya", received_on=None, ref="ACH 123"):
    return client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount_cents": cents, "received_on": (received_on or date.today()).isoformat(), "reference": ref},
        headers=as_user(user),
    )


def dashboard(client, as_of):
    return client.get(f"/dashboard?as_of={as_of.isoformat()}", headers=as_user("marcus")).json()


# ---- payments ----
def test_posting_sets_due_date_from_terms(posted_invoice):
    assert posted_invoice["due_on"] == (date.today() + timedelta(days=30)).isoformat()
    assert posted_invoice["days_past_due"] == -30


def test_partial_then_full_payment(client, posted_invoice):
    i = posted_invoice["id"]
    inv = pay(client, i, 200_000).json()
    assert inv["paid_cents"] == 200_000
    assert inv["balance_cents"] == 300_000
    assert "pay" in inv["allowed_actions"]
    inv = pay(client, i, 300_000, ref="ACH 456").json()
    assert inv["balance_cents"] == 0
    assert inv["days_past_due"] is None
    assert "pay" not in inv["allowed_actions"]


def test_payment_rules(client, draft_invoice, posted_invoice):
    i = posted_invoice["id"]
    assert pay(client, i, 500_001).status_code == 422  # more than owed
    assert pay(client, i, 100, received_on=date.today() + timedelta(days=1)).status_code == 422  # future
    assert pay(client, i, 100, user="marcus").status_code == 403  # approvers don't apply cash


def test_pending_credit_plus_payment_cannot_exceed_total(client, posted_invoice):
    i = posted_invoice["id"]
    client.post(f"/invoices/{i}/credit-memos", json={"amount_cents": 100_000, "reason": "Make-good"},
                headers=as_user("priya"))
    assert pay(client, i, 500_000).status_code == 422
    assert pay(client, i, 400_000).status_code == 201


def test_payments_are_immutable(session_factory, client, posted_invoice):
    pay(client, posted_invoice["id"], 1_000)
    with session_factory() as db:
        p = db.query(Payment).one()
        p.amount_cents = 1
        with pytest.raises(IntegrityViolation):
            db.flush()


# ---- aging ----
def test_aging_buckets_move_with_as_of_date(client, posted_invoice):
    today = date.today()
    buckets = lambda d: {b["key"]: b["cents"] for b in dashboard(client, d)["aging"]["buckets"]}
    assert buckets(today)["current"] == 500_000
    assert buckets(today + timedelta(days=45))["1_30"] == 500_000  # 15 days past due
    assert buckets(today + timedelta(days=75))["31_60"] == 500_000
    assert buckets(today + timedelta(days=100))["61_plus"] == 500_000
    assert sum(buckets(today - timedelta(days=1)).values()) == 0  # not posted yet on that date


def test_aging_is_point_in_time(client, posted_invoice):
    today = date.today()
    pay(client, posted_invoice["id"], 200_000)
    later = dashboard(client, today + timedelta(days=45))
    assert later["kpis"]["outstanding_cents"] == 300_000
    assert later["kpis"]["past_due_cents"] == 300_000
    assert later["past_due"][0]["days_past_due"] == 15
    assert later["aging"]["by_customer"][0]["customer_name"] == "Acme Auto"


def test_dashboard_numbers(client, posted_invoice):
    d = dashboard(client, date.today())
    assert d["kpis"]["billed_30d_cents"] == 500_000
    assert d["kpis"]["dso_days"] == 90  # nothing collected yet: all 90 days of billing outstanding
    assert d["monthly"][-1]["billed_cents"] == 500_000
    assert len(d["monthly"]) == 6


def test_aging_csv(client, posted_invoice):
    r = client.get("/reports/aging.csv", headers=as_user("priya"))
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("customer,invoice")
    assert lines[1].endswith(",Current,5000.00")


# ---- anomalies ----
def test_amount_outlier_rule():
    history = [100_000, 110_000, 95_000, 105_000]
    assert amount_outlier(102_000, history) is None
    flag = amount_outlier(1_000_000, history)
    assert flag and "about 9.8×" in flag.message
    assert amount_outlier(1_000_000, history[:2]) is None  # too little history to judge


def make_history(client, n=3, cpm=500, impressions=100_000):
    h = as_user("priya")
    cust = client.post("/customers", json={"name": "Hist Co", "email": "a@h.test"}, headers=h).json()
    for k in range(n):
        client.post("/campaigns", json={"customer_id": cust["id"], "name": f"Month {k}", "booked_impressions": impressions + 10_000,
                                        "delivered_impressions": impressions + k * 1000, "cpm_cents": cpm}, headers=h)
        inv = client.post("/invoices/generate", json={"customer_id": cust["id"]}, headers=h).json()
        client.post(f"/invoices/{inv['id']}/actions/submit", headers=h)
        client.post(f"/invoices/{inv['id']}/actions/approve", headers=as_user("marcus"))
        client.post(f"/invoices/{inv['id']}/actions/post", headers=as_user("marcus"))
    return cust


def test_flagged_invoice_needs_a_note_to_approve(client):
    cust = make_history(client)
    h = as_user("priya")
    client.post("/campaigns", json={"customer_id": cust["id"], "name": "Big One", "booked_impressions": 900_000,
                                    "delivered_impressions": 1_000_000, "cpm_cents": 1_500}, headers=h)
    inv = client.post("/invoices/generate", json={"customer_id": cust["id"]}, headers=h).json()
    codes = {a["code"] for a in inv["anomalies"]}
    assert codes == {"amount_outlier", "cpm_outlier", "billed_over_booked"}

    client.post(f"/invoices/{inv['id']}/actions/submit", headers=h)
    r = client.post(f"/invoices/{inv['id']}/actions/approve", headers=as_user("marcus"))
    assert r.status_code == 422
    r = client.post(f"/invoices/{inv['id']}/actions/approve", json={"reason": "Holiday campaign, rate confirmed by sales"},
                    headers=as_user("marcus"))
    assert r.status_code == 200
    trail = client.get(f"/invoices/{inv['id']}/audit", headers=h).json()
    assert trail[-1]["reason"] == "Holiday campaign, rate confirmed by sales"
    assert dashboard(client, date.today())["pipeline"]["approved"]["flagged"] == 1


def test_possible_duplicate(client):
    cust = make_history(client, n=1)
    h = as_user("priya")
    client.post("/campaigns", json={"customer_id": cust["id"], "name": "Again", "booked_impressions": 100_000,
                                    "delivered_impressions": 100_000, "cpm_cents": 500}, headers=h)
    inv = client.post("/invoices/generate", json={"customer_id": cust["id"]}, headers=h).json()
    assert [a["code"] for a in inv["anomalies"]] == ["possible_duplicate"]


# ---- PDF ----
def test_pdf_preview(client, draft_invoice, posted_invoice):
    r = client.get(f"/invoices/{posted_invoice['id']}/pdf", headers=as_user("priya"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert 'filename="INV-000001.pdf"' in r.headers["content-disposition"]
