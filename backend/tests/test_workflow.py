from tests.conftest import as_user


def act(client, invoice_id, action, user, reason=None):
    body = {"reason": reason} if reason else None
    return client.post(f"/invoices/{invoice_id}/actions/{action}", json=body, headers=as_user(user))


def test_happy_path_with_audit_trail(client, draft_invoice):
    i = draft_invoice["id"]
    assert act(client, i, "submit", "priya").json()["status"] == "pending_approval"
    assert act(client, i, "approve", "marcus").json()["status"] == "approved"
    posted = act(client, i, "post", "marcus").json()
    assert posted["status"] == "posted"
    assert posted["approved_by"] == "marcus"
    assert posted["posted_at"] is not None

    trail = client.get(f"/invoices/{i}/audit", headers=as_user("priya")).json()
    assert [e["action"] for e in trail] == ["create", "submit", "approve", "post"]
    assert [(e["before"] or {}).get("status") for e in trail] == [None, "draft", "pending_approval", "approved"]
    assert [e["after"]["status"] for e in trail] == ["draft", "pending_approval", "approved", "posted"]
    assert [e["actor"] for e in trail] == ["priya", "priya", "marcus", "marcus"]


def test_cannot_skip_states(client, draft_invoice):
    assert act(client, draft_invoice["id"], "approve", "marcus").status_code == 409


def test_preparer_cannot_approve(client, draft_invoice):
    i = draft_invoice["id"]
    act(client, i, "submit", "priya")
    assert act(client, i, "approve", "priya").status_code == 403


def test_segregation_of_duties_applies_even_with_both_roles(client):
    h = as_user("dana")  # dana is preparer AND approver
    cust = client.post("/customers", json={"name": "Bay Bakery", "email": "a@b.test"}, headers=h).json()
    client.post(
        "/campaigns",
        json={"customer_id": cust["id"], "name": "Spring", "booked_impressions": 10_000,
              "delivered_impressions": 10_000, "cpm_cents": 1_000},
        headers=h,
    )
    inv = client.post("/invoices/generate", json={"customer_id": cust["id"]}, headers=h).json()
    act(client, inv["id"], "submit", "dana")

    mine = client.get(f"/invoices/{inv['id']}", headers=h).json()
    assert mine["allowed_actions"] == []  # the UI won't offer it...
    assert act(client, inv["id"], "approve", "dana").status_code == 403  # ...and the API refuses it
    assert act(client, inv["id"], "approve", "marcus").status_code == 200


def test_reject_needs_reason_and_returns_to_draft(client, draft_invoice):
    i = draft_invoice["id"]
    act(client, i, "submit", "priya")
    assert act(client, i, "reject", "marcus").status_code == 422
    r = act(client, i, "reject", "marcus", reason="Wrong CPM on Fall Promo")
    assert r.json()["status"] == "draft"
    trail = client.get(f"/invoices/{i}/audit", headers=as_user("priya")).json()
    assert trail[-1]["reason"] == "Wrong CPM on Fall Promo"


def test_second_approval_is_rejected(client, draft_invoice):
    i = draft_invoice["id"]
    act(client, i, "submit", "priya")
    assert act(client, i, "approve", "marcus").status_code == 200
    assert act(client, i, "approve", "dana").status_code == 409
