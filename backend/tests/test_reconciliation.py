from tests.conftest import as_user, upload


def recon(client, threshold="2"):
    return client.get(f"/reconciliation?threshold_pct={threshold}", headers=as_user("priya")).json()


def test_no_delivery_data_yet(client, draft_invoice):
    row = recon(client)[0]
    assert row["status"] == "no_delivery_data"
    assert row["flagged"] is False


def test_over_billed_is_flagged_with_money_and_suggestion(client, posted_invoice):
    # billed 1,000,000 at $5 CPM; ad server says 950,000 delivered
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,950000\n")
    row = recon(client)[0]
    assert row["status"] == "over_billed"
    assert row["variance_impressions"] == -50_000
    assert row["variance_pct"] == "-5.00"
    assert row["variance_cents"] == -25_000  # 50,000 x $5 / 1000 = $250
    assert row["flagged"] is True
    assert "credit memo" in row["suggestion"]
    assert recon(client, threshold="10")[0]["flagged"] is False


def test_matched_is_not_flagged(client, draft_invoice):
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,1000000\n")
    row = recon(client)[0]
    assert row["status"] == "matched"
    assert row["flagged"] is False


def test_resolution_clears_flag_until_numbers_change(client, posted_invoice):
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,950000\n")
    r = client.post(
        "/reconciliation/1/resolve",
        json={"note": "Ad server dropped a day of logs; vendor confirmed full delivery"},
        headers=as_user("marcus"),
    )
    assert r.status_code == 200
    row = r.json()[0]
    assert row["resolved"] is True
    assert row["resolved_by"] == "marcus"

    # a restatement changes the variance, so the flag reopens
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,900000\n", filename="restated.csv")
    row = recon(client)[0]
    assert row["variance_impressions"] == -100_000
    assert row["flagged"] is True and row["resolved"] is False

    trail = client.get(f"/invoices/{posted_invoice['id']}/audit", headers=as_user("priya")).json()
    assert any(e["action"] == "resolve" and e["actor"] == "marcus" for e in trail)


def test_cannot_resolve_unflagged(client, draft_invoice):
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,1000000\n")
    r = client.post("/reconciliation/1/resolve", json={"note": "nothing to see"}, headers=as_user("marcus"))
    assert r.status_code == 409
