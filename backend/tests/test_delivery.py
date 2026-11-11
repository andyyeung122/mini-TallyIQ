from tests.conftest import as_user, upload


def test_import_and_reject_duplicate_file(client, draft_invoice):
    csv_text = "campaign_id,date,impressions\n1,2026-10-01,500000\n1,2026-10-02,450000\n"
    r = upload(client, csv_text)
    assert r.status_code == 201
    assert r.json()["row_count"] == 2
    again = upload(client, csv_text, filename="renamed.csv")
    assert again.status_code == 409  # same bytes, even under a new name


def test_bad_rows_import_nothing_and_list_every_problem(client, draft_invoice):
    csv_text = (
        "campaign_id,date,impressions\n"
        "1,2026-10-01,500000\n"
        "99,2026-10-01,10\n"  # line 3: unknown campaign
        "1,10/02/2026,10\n"  # line 4: bad date
        "1,2026-10-03,-5\n"  # line 5: negative
        "1,2026-10-01,7\n"  # line 6: duplicate day
    )
    r = upload(client, csv_text)
    assert r.status_code == 422
    assert [e["line"] for e in r.json()["detail"]["errors"]] == [3, 4, 5, 6]
    assert client.get("/delivery-reports", headers=as_user("priya")).json() == []


def test_missing_column(client, draft_invoice):
    r = upload(client, "campaign_id,day,impressions\n1,2026-10-01,5\n")
    assert r.status_code == 422
    assert "date" in r.json()["detail"]["errors"][0]["message"]


def test_restated_day_uses_latest_report(client, draft_invoice):
    upload(client, "campaign_id,date,impressions\n1,2026-10-01,600000\n1,2026-10-02,400000\n")
    upload(client, "campaign_id,date,impressions\n1,2026-10-02,350000\n", filename="restated.csv")
    rows = client.get("/reconciliation", headers=as_user("priya")).json()
    assert rows[0]["reported_impressions"] == 950_000  # 600k + restated 350k
