from datetime import timedelta

from app import sync
from app.models import SyncJob, utcnow
from tests.conftest import as_user


def run(session_factory, client, now):
    with session_factory() as db:
        return sync.run_due_jobs(db, client, now=now)


def job(session_factory):
    with session_factory() as db:
        j = db.query(SyncJob).one()
        db.expunge(j)
        return j


def test_posting_queues_a_sync_and_worker_delivers_it(client, session_factory, fake_netsuite, posted_invoice):
    assert posted_invoice["sync_status"] == "pending"
    assert run(session_factory, fake_netsuite, utcnow()) == {"synced": 1, "retrying": 0, "dead": 0}
    inv = client.get(f"/invoices/{posted_invoice['id']}", headers=as_user("priya")).json()
    assert inv["sync_status"] == "synced"
    assert inv["netsuite_id"] == "NS-1001"
    assert fake_netsuite.invoices["NS-1001"]["total_cents"] == 500_000


def test_lost_response_retry_does_not_duplicate(session_factory, fake_netsuite, posted_invoice):
    """NetSuite saved the invoice but we never heard back. The retry must not create a second one."""
    fake_netsuite.set_mode("lost_response")
    now = utcnow()
    assert run(session_factory, fake_netsuite, now)["retrying"] == 1
    assert len(fake_netsuite.invoices) == 1  # it really was saved

    # not due yet: backoff is 10s after the first failure
    assert run(session_factory, fake_netsuite, now + timedelta(seconds=5)) == {"synced": 0, "retrying": 0, "dead": 0}

    assert run(session_factory, fake_netsuite, now + timedelta(seconds=11))["synced"] == 1
    assert len(fake_netsuite.invoices) == 1  # idempotency key returned the same record
    assert job(session_factory).external_id == "NS-1001"


def test_flaky_recovers_after_backoff(session_factory, fake_netsuite, posted_invoice):
    fake_netsuite.set_mode("flaky")  # 2 timeouts, then healthy
    now = utcnow()
    assert run(session_factory, fake_netsuite, now)["retrying"] == 1
    assert run(session_factory, fake_netsuite, now + timedelta(seconds=11))["retrying"] == 1  # next wait is 20s
    assert run(session_factory, fake_netsuite, now + timedelta(seconds=40))["synced"] == 1
    assert job(session_factory).attempts == 3


def test_gives_up_after_max_attempts_then_manual_retry(client, session_factory, fake_netsuite, posted_invoice):
    fake_netsuite.set_mode("down")
    now = utcnow()
    for _ in range(sync.MAX_ATTEMPTS):
        now += timedelta(hours=1)  # always past the backoff
        run(session_factory, fake_netsuite, now)
    j = job(session_factory)
    assert j.status == "dead"
    assert j.attempts == sync.MAX_ATTEMPTS
    assert "503" in j.last_error

    assert client.post(f"/sync/jobs/{j.id}/retry", headers=as_user("priya")).status_code == 403  # approvers only
    fake_netsuite.set_mode("healthy")
    assert client.post(f"/sync/jobs/{j.id}/retry", headers=as_user("marcus")).json()["status"] == "pending"
    assert client.post("/sync/run", headers=as_user("marcus")).json()["synced"] == 1


def test_permanent_error_goes_straight_to_dead(session_factory, fake_netsuite, posted_invoice):
    fake_netsuite.set_mode("reject")
    assert run(session_factory, fake_netsuite, utcnow())["dead"] == 1
    assert job(session_factory).attempts == 1


def test_leased_job_is_not_claimed_twice(session_factory, posted_invoice):
    now = utcnow()
    with session_factory() as db:
        assert len(sync.claim_due_jobs(db, now)) == 1
    with session_factory() as db:
        assert sync.claim_due_jobs(db, now) == []  # another worker sees it locked
    with session_factory() as db:
        assert len(sync.claim_due_jobs(db, now + sync.LEASE + timedelta(seconds=1))) == 1  # lease expired


def test_sync_events_show_in_invoice_history(client, session_factory, fake_netsuite, posted_invoice):
    fake_netsuite.set_mode("flaky")
    run(session_factory, fake_netsuite, utcnow())
    trail = client.get(f"/invoices/{posted_invoice['id']}/audit", headers=as_user("priya")).json()
    assert trail[-1]["action"] == "sync_retry_scheduled"
    assert "timed out" in trail[-1]["reason"]
