from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    username: str
    display_name: str
    roles: str


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)
    payment_terms_days: int = Field(default=30, ge=0, le=120)


class CustomerOut(CustomerIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CampaignIn(BaseModel):
    customer_id: int
    name: str = Field(min_length=1, max_length=200)
    booked_impressions: int = Field(ge=0)
    delivered_impressions: int = Field(ge=0)
    cpm_cents: int = Field(ge=0)


class CampaignOut(CampaignIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    campaign_id: int
    description: str
    impressions: int
    cpm_cents: int
    amount_cents: int


class CreditMemoOut(BaseModel):
    id: int
    amount_cents: int
    reason: str
    status: str
    created_by: str
    decided_by: str | None
    created_at: datetime
    allowed_actions: list[str]


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount_cents: int
    received_on: date
    reference: str
    recorded_by: str


class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    received_on: date
    reference: str = Field(min_length=1, max_length=100)


class AnomalyOut(BaseModel):
    code: str
    severity: str
    message: str


class InvoiceOut(BaseModel):
    id: int
    customer_id: int
    status: str
    total_cents: int
    balance_cents: int
    created_at: datetime
    created_by: str
    submitted_by: str | None
    approved_by: str | None
    posted_at: datetime | None
    lines: list[InvoiceLineOut]
    credit_memos: list[CreditMemoOut]
    allowed_actions: list[str]
    sync_status: str | None  # None until posted; then pending | synced | dead
    netsuite_id: str | None
    due_on: date | None
    days_past_due: int | None  # only for posted invoices with a balance
    paid_cents: int
    payments: list[PaymentOut]
    anomalies: list[AnomalyOut]


class GenerateInvoiceIn(BaseModel):
    customer_id: int


class ActionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class CreditMemoIn(BaseModel):
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entity_type: str
    entity_id: int
    action: str
    actor: str
    before: dict | None
    after: dict | None
    reason: str | None
    at: datetime


# ---- Phase 3: delivery, reconciliation, NetSuite sync ----
class DeliveryImportIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    csv: str = Field(min_length=1, max_length=5_000_000)


class DeliveryReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    row_count: int
    uploaded_by: str
    uploaded_at: datetime


class ReconRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    campaign_id: int
    campaign_name: str
    customer_name: str
    cpm_cents: int
    booked_impressions: int
    reported_impressions: int | None
    billed_impressions: int | None
    invoice_id: int | None
    invoice_status: str | None
    variance_impressions: int | None
    variance_pct: Decimal | None
    variance_cents: int | None
    status: str
    flagged: bool
    resolved: bool
    resolution_note: str | None
    resolved_by: str | None
    suggestion: str | None


class ResolveIn(BaseModel):
    note: str = Field(min_length=3, max_length=500)
    threshold_pct: Decimal = Field(default=Decimal("2"), ge=0, le=100)


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_id: int
    idempotency_key: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    external_id: str | None
    synced_at: datetime | None


class SyncRunOut(BaseModel):
    synced: int
    retrying: int
    dead: int


class MockModeIn(BaseModel):
    mode: str


class MockNetSuiteOut(BaseModel):
    mode: str
    modes: dict[str, str]
    calls: int
    invoices: list[dict]
