"""Delivers posted invoices to NetSuite.

Transactional outbox: posting an invoice and creating its SyncJob happen in one
database transaction, so we never post without syncing or sync without posting
(no "dual write" problem). A worker then delivers jobs:

- At-least-once delivery, made safe by an idempotency key per invoice: if NetSuite
  saved the invoice but the response was lost, the retry gets the same NetSuite id back.
- Transient errors retry with exponential backoff; permanent errors, or too many
  attempts, move the job to "dead" for a person to look at and retry.
- Jobs are claimed with a short lease, so two workers never send the same job at
  once, and a worker that crashes mid-job releases it when the lease expires.
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from . import audit
from .models import Invoice, SyncJob, User, utcnow
from .netsuite import FakeNetSuite, PermanentError, TransientError

MAX_ATTEMPTS = 5
BASE_DELAY = timedelta(seconds=10)
MAX_DELAY = timedelta(minutes=10)
LEASE = timedelta(seconds=60)


def backoff(attempt: int) -> timedelta:
    """10s, 20s, 40s, 80s... capped at 10 minutes. (Add jitter in production.)"""
    return min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)


def enqueue(db: Session, inv: Invoice) -> None:
    """Call inside the transaction that posts the invoice."""
    db.add(SyncJob(invoice_id=inv.id, idempotency_key=f"tallyiq-invoice-{inv.id}"))


def build_payload(inv: Invoice) -> dict:
    """Deterministic, so a retry sends exactly the same request."""
    return {
        "external_ref": f"TIQ-{inv.id}",
        "customer_id": inv.customer_id,
        "currency": "USD",
        "tran_date": inv.posted_at.date().isoformat() if inv.posted_at else None,
        "total_cents": inv.total_cents,
        "lines": [
            {
                "description": line.description,
                "quantity": line.impressions,
                "rate_cpm_cents": line.cpm_cents,
                "amount_cents": line.amount_cents,
            }
            for line in sorted(inv.lines, key=lambda line: line.id)
        ],
    }


def _claimable(now: datetime):
    return (
        SyncJob.status == "pending",
        SyncJob.next_attempt_at <= now,
        or_(SyncJob.locked_until.is_(None), SyncJob.locked_until < now),
    )


def claim_due_jobs(db: Session, now: datetime, limit: int = 10) -> list[int]:
    ids = db.scalars(select(SyncJob.id).where(*_claimable(now)).order_by(SyncJob.id).limit(limit)).all()
    claimed = []
    for job_id in ids:
        # Same conditional-update trick as approvals: only one worker can win the claim.
        result = db.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id, *_claimable(now))
            .values(locked_until=now + LEASE)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            claimed.append(job_id)
    db.commit()
    return claimed


def _record(db: Session, job: SyncJob, action: str, actor: str = "system", reason: str | None = None) -> None:
    audit.record(
        db,
        entity_type="sync_job",
        entity_id=job.id,
        invoice_id=job.invoice_id,
        action=action,
        actor=actor,
        before=None,
        after={"status": job.status, "attempts": job.attempts, "external_id": job.external_id},
        reason=reason,
    )


def run_due_jobs(db: Session, client: FakeNetSuite, now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    counts = {"synced": 0, "retrying": 0, "dead": 0}

    for job_id in claim_due_jobs(db, now):
        job = db.get(SyncJob, job_id)
        job.attempts += 1
        try:
            external_id = client.create_invoice(build_payload(job.invoice), job.idempotency_key)
        except PermanentError as e:
            job.status, job.last_error = "dead", str(e)
            counts["dead"] += 1
            _record(db, job, "sync_failed", reason=str(e))
        except TransientError as e:
            job.last_error = str(e)
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "dead"
                counts["dead"] += 1
                _record(db, job, "sync_failed", reason=f"Gave up after {job.attempts} attempts: {e}")
            else:
                job.next_attempt_at = now + backoff(job.attempts)
                counts["retrying"] += 1
                _record(db, job, "sync_retry_scheduled", reason=str(e))
        else:
            job.status, job.external_id, job.synced_at, job.last_error = "synced", external_id, now, None
            counts["synced"] += 1
            _record(db, job, "synced")
        job.locked_until = None
        db.commit()

    return counts


def retry_dead_job(db: Session, job: SyncJob, user: User) -> None:
    if job.status != "dead":
        raise HTTPException(409, "Only failed syncs can be retried")
    result = db.execute(
        update(SyncJob)
        .where(SyncJob.id == job.id, SyncJob.status == "dead")
        .values(status="pending", attempts=0, next_attempt_at=utcnow(), locked_until=None)
    )
    if result.rowcount == 0:
        db.rollback()
        raise HTTPException(409, "This sync was just changed by someone else; refresh and try again")
    db.refresh(job)
    _record(db, job, "sync_retry_requested", actor=user.username)
    db.commit()
