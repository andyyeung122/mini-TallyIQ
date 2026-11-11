from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, event, inspect
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_date(dt: datetime) -> date:
    """The business date (server's local time zone) for a stored timestamp.
    SQLite hands back naive datetimes, which we always store in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    roles: Mapped[str] = mapped_column(String(100))  # comma-separated: "preparer,approver"

    def has_role(self, role: str) -> bool:
        return role in self.roles.split(",")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    email: Mapped[str] = mapped_column(String(200))
    payment_terms_days: Mapped[int] = mapped_column(default=30)  # Net 30


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    name: Mapped[str] = mapped_column(String(200))
    booked_impressions: Mapped[int]
    delivered_impressions: Mapped[int]
    cpm_cents: Mapped[int]  # price per 1,000 impressions, in cents


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    total_cents: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(50))
    submitted_by: Mapped[str | None] = mapped_column(String(50))
    approved_by: Mapped[str | None] = mapped_column(String(50))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_on: Mapped[date | None]  # set when posted: posting date + customer's payment terms

    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    credit_memos: Mapped[list["CreditMemo"]] = relationship(
        back_populates="invoice", order_by="CreditMemo.id"
    )
    sync_job: Mapped[Optional["SyncJob"]] = relationship(back_populates="invoice")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", order_by="Payment.id")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    # unique: a campaign can only be billed once
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), unique=True)
    description: Mapped[str] = mapped_column(String(300))
    impressions: Mapped[int]
    cpm_cents: Mapped[int]
    amount_cents: Mapped[int]

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class CreditMemo(Base):
    """Corrections to a posted invoice. Posted invoices are never edited."""

    __tablename__ = "credit_memos"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    amount_cents: Mapped[int]
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")
    created_by: Mapped[str] = mapped_column(String(50))
    decided_by: Mapped[str | None] = mapped_column(String(50))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # for as-of reporting
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="credit_memos")


class Payment(Base):
    """Cash received against a posted invoice. Never edited: mistakes are reversed, not rewritten."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    amount_cents: Mapped[int]
    received_on: Mapped[date]
    reference: Mapped[str] = mapped_column(String(100))  # check number, ACH trace, etc.
    recorded_by: Mapped[str] = mapped_column(String(50))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class AuditEvent(Base):
    """Append-only: rows are inserted, never updated or deleted."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int]
    invoice_id: Mapped[int | None]  # lets an invoice's trail include its credit memos
    action: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(50))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryReport(Base):
    """One uploaded ad-server delivery file. The checksum makes re-uploads idempotent."""

    __tablename__ = "delivery_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    checksum: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the file
    row_count: Mapped[int]
    uploaded_by: Mapped[str] = mapped_column(String(50))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rows: Mapped[list["DeliveryRow"]] = relationship(back_populates="report", cascade="all, delete-orphan")


class DeliveryRow(Base):
    """Impressions the ad server reports for one campaign on one day.
    If a later report restates the same campaign and day, the later one wins."""

    __tablename__ = "delivery_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("delivery_reports.id"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    day: Mapped[date]
    impressions: Mapped[int]

    report: Mapped[DeliveryReport] = relationship(back_populates="rows")


class VarianceResolution(Base):
    """A person explained a variance. It only clears the flag while the variance
    is unchanged; new delivery data reopens it."""

    __tablename__ = "variance_resolutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    variance_impressions: Mapped[int]  # the variance that was explained
    note: Mapped[str] = mapped_column(String(500))
    resolved_by: Mapped[str] = mapped_column(String(50))
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncJob(Base):
    """Transactional outbox: written in the same transaction that posts the invoice,
    then delivered to NetSuite by a worker with retries."""

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | synced | dead
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    external_id: Mapped[str | None] = mapped_column(String(50))  # NetSuite's internal id
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="sync_job")


class IntegrityViolation(Exception):
    pass


@event.listens_for(Session, "before_flush")
def enforce_financial_invariants(session, _ctx, _instances):
    """Last line of defense below the API: no ORM code path can rewrite history."""
    for obj in session.deleted:
        if isinstance(obj, AuditEvent):
            raise IntegrityViolation("Audit events can't be deleted")
        if isinstance(obj, (Invoice, InvoiceLine, CreditMemo, Payment)):
            raise IntegrityViolation("Financial records can't be deleted")
    for obj in session.dirty:
        if not session.is_modified(obj):
            continue
        if isinstance(obj, AuditEvent):
            raise IntegrityViolation("Audit events can't be modified")
        if isinstance(obj, Payment):
            raise IntegrityViolation("Payments can't be edited; record a reversal instead")
        if isinstance(obj, Invoice):
            old = inspect(obj).attrs.status.history.deleted
            status_before = old[0] if old else obj.status
            if status_before == "posted":
                raise IntegrityViolation("Posted invoices can't be modified; issue a credit memo")
        if isinstance(obj, InvoiceLine) and obj.invoice.status != "draft":
            raise IntegrityViolation("Invoice lines can only change while the invoice is a draft")
