import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import analytics, anomalies, audit, delivery, pdf, reconciliation, schemas, sync, workflow
from .auth import current_user, require_role, seed_users
from .database import Base, SessionLocal, engine, get_db
from .models import (
    AuditEvent,
    Campaign,
    CreditMemo,
    Customer,
    DeliveryReport,
    Invoice,
    InvoiceLine,
    SyncJob,
    User,
)
from .money import line_amount_cents
from .netsuite import MODES, FakeNetSuite, get_netsuite, netsuite

log = logging.getLogger("tallyiq")
WORKER_INTERVAL_SECONDS = 5


def _sync_tick() -> None:
    with SessionLocal() as db:
        counts = sync.run_due_jobs(db, netsuite)
        if any(counts.values()):
            log.info("NetSuite sync: %s", counts)


async def sync_worker() -> None:
    """In production this would be its own Cloud Run job or a Cloud Tasks/Pub/Sub consumer."""
    while True:
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
        try:
            await run_in_threadpool(_sync_tick)
        except Exception:  # keep the worker alive; a stuck job's lease will expire
            log.exception("NetSuite sync worker failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)  # swap for Alembic migrations later
    with SessionLocal() as db:
        seed_users(db)
    if os.getenv("SEED_DEMO_DATA") == "1":  # hosted demo: the disk is wiped on restart, so reload demo data
        from .seed import main as seed_demo

        seed_demo()  # no-op if the database already has data
    worker = asyncio.create_task(sync_worker()) if os.getenv("SYNC_WORKER", "1") == "1" else None
    yield
    if worker:
        worker.cancel()


app = FastAPI(title="Mini TallyIQ", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


# ---- presenters ----
def memo_out(memo: CreditMemo, user: User) -> schemas.CreditMemoOut:
    return schemas.CreditMemoOut(
        id=memo.id,
        amount_cents=memo.amount_cents,
        reason=memo.reason,
        status=memo.status,
        created_by=memo.created_by,
        decided_by=memo.decided_by,
        created_at=memo.created_at,
        allowed_actions=workflow.memo_actions(memo, user),
    )


def invoice_out(inv: Invoice, user: User, flags: list[anomalies.Anomaly]) -> schemas.InvoiceOut:
    balance = workflow.balance_cents(inv)
    days_past_due = (
        (date.today() - inv.due_on).days if inv.status == "posted" and inv.due_on and balance > 0 else None
    )
    return schemas.InvoiceOut(
        id=inv.id,
        customer_id=inv.customer_id,
        status=inv.status,
        total_cents=inv.total_cents,
        balance_cents=balance,
        created_at=inv.created_at,
        created_by=inv.created_by,
        submitted_by=inv.submitted_by,
        approved_by=inv.approved_by,
        posted_at=inv.posted_at,
        lines=[schemas.InvoiceLineOut.model_validate(line) for line in inv.lines],
        credit_memos=[memo_out(m, user) for m in inv.credit_memos],
        allowed_actions=workflow.invoice_actions(inv, user),
        sync_status=inv.sync_job.status if inv.sync_job else None,
        netsuite_id=inv.sync_job.external_id if inv.sync_job else None,
        due_on=inv.due_on,
        days_past_due=days_past_due,
        paid_cents=workflow.paid_cents(inv),
        payments=[schemas.PaymentOut.model_validate(p) for p in inv.payments],
        anomalies=[schemas.AnomalyOut(**f.as_dict()) for f in flags],
    )


def present(db: Session, inv: Invoice, user: User) -> schemas.InvoiceOut:
    return invoice_out(inv, user, anomalies.detect_all(db).get(inv.id, []))


def load_invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return inv


# ---- users ----
@app.get("/users", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.id)).all()


# ---- customers ----
@app.get("/customers", response_model=list[schemas.CustomerOut])
def list_customers(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return db.scalars(select(Customer).order_by(Customer.name)).all()


@app.post("/customers", response_model=schemas.CustomerOut, status_code=201)
def create_customer(body: schemas.CustomerIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    customer = Customer(**body.model_dump())
    db.add(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A customer with that name already exists")
    db.refresh(customer)
    return customer


# ---- campaigns ----
@app.get("/campaigns", response_model=list[schemas.CampaignOut])
def list_campaigns(
    customer_id: int | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)
):
    query = select(Campaign).order_by(Campaign.id.desc())
    if customer_id is not None:
        query = query.where(Campaign.customer_id == customer_id)
    return db.scalars(query).all()


@app.post("/campaigns", response_model=schemas.CampaignOut, status_code=201)
def create_campaign(body: schemas.CampaignIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    if db.get(Customer, body.customer_id) is None:
        raise HTTPException(404, "Customer not found")
    campaign = Campaign(**body.model_dump())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


# ---- invoices ----
@app.get("/invoices", response_model=list[schemas.InvoiceOut])
def list_invoices(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = (
        select(Invoice)
        .options(
            selectinload(Invoice.lines),
            selectinload(Invoice.credit_memos),
            selectinload(Invoice.sync_job),
            selectinload(Invoice.payments),
        )
        .order_by(Invoice.id.desc())
    )
    flags = anomalies.detect_all(db)
    return [invoice_out(inv, user, flags.get(inv.id, [])) for inv in db.scalars(query).all()]


@app.get("/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return present(db, load_invoice(db, invoice_id), user)


@app.post("/invoices/generate", response_model=schemas.InvoiceOut, status_code=201)
def generate_invoice(
    body: schemas.GenerateInvoiceIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Create a draft invoice from a customer's delivered, not-yet-billed campaigns."""
    require_role(user, "preparer")
    if db.get(Customer, body.customer_id) is None:
        raise HTTPException(404, "Customer not found")

    already_billed = select(InvoiceLine.campaign_id)
    campaigns = db.scalars(
        select(Campaign).where(
            Campaign.customer_id == body.customer_id,
            Campaign.delivered_impressions > 0,
            Campaign.id.not_in(already_billed),
        )
    ).all()
    if not campaigns:
        raise HTTPException(409, "No unbilled campaigns with delivery for this customer")

    inv = Invoice(customer_id=body.customer_id, created_by=user.username)
    for c in campaigns:  # bill on delivered, not booked, impressions
        inv.lines.append(
            InvoiceLine(
                campaign_id=c.id,
                description=c.name,
                impressions=c.delivered_impressions,
                cpm_cents=c.cpm_cents,
                amount_cents=line_amount_cents(c.delivered_impressions, c.cpm_cents),
            )
        )
    inv.total_cents = sum(line.amount_cents for line in inv.lines)
    db.add(inv)
    db.flush()
    audit.record(
        db,
        entity_type="invoice",
        entity_id=inv.id,
        invoice_id=inv.id,
        action="create",
        actor=user.username,
        before=None,
        after=audit.invoice_snapshot(inv),
    )
    db.commit()
    db.refresh(inv)
    return present(db, inv, user)


@app.post("/invoices/{invoice_id}/actions/{action}", response_model=schemas.InvoiceOut)
def invoice_action(
    invoice_id: int,
    action: str,
    body: schemas.ActionIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    inv = load_invoice(db, invoice_id)
    flag_count = len(anomalies.detect_all(db).get(inv.id, [])) if action == "approve" else 0
    workflow.apply_invoice_action(db, inv, action, user, body.reason if body else None, flag_count)
    return present(db, inv, user)


@app.get("/invoices/{invoice_id}/audit", response_model=list[schemas.AuditEventOut])
def invoice_audit(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    load_invoice(db, invoice_id)
    return db.scalars(
        select(AuditEvent).where(AuditEvent.invoice_id == invoice_id).order_by(AuditEvent.id)
    ).all()


# ---- credit memos ----
@app.post("/invoices/{invoice_id}/credit-memos", response_model=schemas.InvoiceOut, status_code=201)
def create_credit_memo(
    invoice_id: int, body: schemas.CreditMemoIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    inv = load_invoice(db, invoice_id)
    workflow.create_credit_memo(db, inv, body.amount_cents, body.reason, user)
    db.refresh(inv)
    return present(db, inv, user)


@app.post("/credit-memos/{memo_id}/actions/{action}", response_model=schemas.InvoiceOut)
def credit_memo_action(
    memo_id: int,
    action: str,
    body: schemas.ActionIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    memo = db.get(CreditMemo, memo_id)
    if memo is None:
        raise HTTPException(404, "Credit memo not found")
    workflow.apply_memo_action(db, memo, action, user, body.reason if body else None)
    inv = load_invoice(db, memo.invoice_id)
    db.refresh(inv)
    return present(db, inv, user)


# ---- delivery reports ----
@app.get("/delivery-reports", response_model=list[schemas.DeliveryReportOut])
def list_delivery_reports(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return db.scalars(select(DeliveryReport).order_by(DeliveryReport.id.desc())).all()


@app.post("/delivery-reports", response_model=schemas.DeliveryReportOut, status_code=201)
def import_delivery_report(
    body: schemas.DeliveryImportIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return delivery.import_report(db, body.filename, body.csv, user)


# ---- reconciliation ----
@app.get("/reconciliation", response_model=list[schemas.ReconRowOut])
def get_reconciliation(
    threshold_pct: Decimal = Query(Decimal("2"), ge=0, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    return reconciliation.reconcile(db, threshold_pct)


@app.post("/reconciliation/{campaign_id}/resolve", response_model=list[schemas.ReconRowOut])
def resolve_variance(
    campaign_id: int, body: schemas.ResolveIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    reconciliation.resolve(db, campaign_id, body.note, body.threshold_pct, user)
    return reconciliation.reconcile(db, body.threshold_pct)


# ---- NetSuite sync ----
@app.get("/sync/jobs", response_model=list[schemas.SyncJobOut])
def list_sync_jobs(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return db.scalars(select(SyncJob).order_by(SyncJob.id.desc())).all()


@app.post("/sync/run", response_model=schemas.SyncRunOut)
def run_sync_now(
    db: Session = Depends(get_db),
    client: FakeNetSuite = Depends(get_netsuite),
    _: User = Depends(current_user),
):
    """Process due jobs right away instead of waiting for the background worker."""
    return sync.run_due_jobs(db, client)


@app.post("/sync/jobs/{job_id}/retry", response_model=schemas.SyncJobOut)
def retry_sync_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, "approver")
    job = db.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(404, "Sync job not found")
    sync.retry_dead_job(db, job, user)
    return job


# ---- mock NetSuite controls (demo only; this wouldn't exist in production) ----
def mock_state(client: FakeNetSuite) -> schemas.MockNetSuiteOut:
    return schemas.MockNetSuiteOut(
        mode=client.mode, modes=MODES, calls=client.calls, invoices=list(client.invoices.values())
    )


@app.get("/mock-netsuite", response_model=schemas.MockNetSuiteOut)
def get_mock_netsuite(client: FakeNetSuite = Depends(get_netsuite), _: User = Depends(current_user)):
    return mock_state(client)


@app.post("/mock-netsuite/mode", response_model=schemas.MockNetSuiteOut)
def set_mock_mode(
    body: schemas.MockModeIn, client: FakeNetSuite = Depends(get_netsuite), _: User = Depends(current_user)
):
    if body.mode not in MODES:
        raise HTTPException(422, f"Mode must be one of: {', '.join(MODES)}")
    client.set_mode(body.mode)
    return mock_state(client)


# ---- payments ----
@app.post("/invoices/{invoice_id}/payments", response_model=schemas.InvoiceOut, status_code=201)
def record_payment(
    invoice_id: int, body: schemas.PaymentIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    inv = load_invoice(db, invoice_id)
    workflow.record_payment(db, inv, body.amount_cents, body.received_on, body.reference, user)
    db.refresh(inv)
    return present(db, inv, user)


# ---- invoice PDF ----
@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    inv = load_invoice(db, invoice_id)
    customer = db.get(Customer, inv.customer_id)
    credits = sum(m.amount_cents for m in inv.credit_memos if m.status == "approved")
    content = pdf.render_invoice_pdf(inv, customer, credits_cents=credits, paid_cents=workflow.paid_cents(inv))
    suffix = "" if inv.status == "posted" else "-draft"
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{pdf.invoice_number(inv.id)}{suffix}.pdf"'},
    )


# ---- dashboard and reports ----
@app.get("/dashboard")
def get_dashboard(
    as_of: date | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)
):
    flagged = {inv_id for inv_id, flags in anomalies.detect_all(db).items() if flags}
    open_variances = sum(1 for r in reconciliation.reconcile(db, Decimal("2")) if r.flagged and not r.resolved)
    return analytics.dashboard(db, as_of or date.today(), flagged, open_variances)


@app.get("/reports/aging.csv")
def aging_report_csv(as_of: date | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    as_of = as_of or date.today()
    return Response(
        analytics.aging_csv(db, as_of),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ar-aging-{as_of}.csv"'},
    )
