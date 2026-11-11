import pytest

from app.money import line_amount_cents
from tests.conftest import as_user


@pytest.mark.parametrize(
    "impressions, cpm_cents, expected",
    [
        (1_000_000, 500, 500_000),  # $5.00 CPM x 1M impressions = $5,000.00
        (1_500, 500, 750),
        (1, 500, 1),  # 0.5 cents rounds half up
        (0, 500, 0),
        (333, 999, 333),  # 332.667 rounds to 333
    ],
)
def test_line_amount_cents(impressions, cpm_cents, expected):
    assert line_amount_cents(impressions, cpm_cents) == expected


def test_generate_bills_delivered_once(client, draft_invoice):
    assert draft_invoice["total_cents"] == 500_000  # billed on delivered, not booked
    assert draft_invoice["status"] == "draft"
    assert draft_invoice["allowed_actions"] == ["submit"]
    again = client.post(
        "/invoices/generate", json={"customer_id": draft_invoice["customer_id"]}, headers=as_user("priya")
    )
    assert again.status_code == 409


def test_requests_need_a_user(client):
    assert client.get("/invoices").status_code == 401


def test_approver_only_cannot_generate(client, draft_invoice):
    r = client.post(
        "/invoices/generate", json={"customer_id": draft_invoice["customer_id"]}, headers=as_user("marcus")
    )
    assert r.status_code == 403
