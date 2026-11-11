export type User = { username: string; display_name: string; roles: string };
export type Customer = { id: number; name: string; email: string; payment_terms_days: number };

export type Campaign = {
  id: number;
  customer_id: number;
  name: string;
  booked_impressions: number;
  delivered_impressions: number;
  cpm_cents: number;
};

export type InvoiceLine = {
  id: number;
  campaign_id: number;
  description: string;
  impressions: number;
  cpm_cents: number;
  amount_cents: number;
};

export type CreditMemo = {
  id: number;
  amount_cents: number;
  reason: string;
  status: string;
  created_by: string;
  decided_by: string | null;
  created_at: string;
  allowed_actions: string[];
};

export type Invoice = {
  id: number;
  customer_id: number;
  status: string;
  total_cents: number;
  balance_cents: number;
  created_at: string;
  created_by: string;
  submitted_by: string | null;
  approved_by: string | null;
  posted_at: string | null;
  lines: InvoiceLine[];
  credit_memos: CreditMemo[];
  allowed_actions: string[];
  sync_status: string | null;
  netsuite_id: string | null;
  due_on: string | null;
  days_past_due: number | null;
  paid_cents: number;
  payments: Payment[];
  anomalies: Anomaly[];
};

export type Payment = {
  id: number;
  amount_cents: number;
  received_on: string;
  reference: string;
  recorded_by: string;
};

export type Anomaly = { code: string; severity: "warning" | "info"; message: string };

export type AgingBucket = { key: string; label: string; cents: number; count: number };

export type Dashboard = {
  as_of: string;
  kpis: {
    outstanding_cents: number;
    past_due_cents: number;
    open_invoices: number;
    billed_30d_cents: number;
    collected_30d_cents: number;
    dso_days: number | null;
  };
  monthly: { month: string; billed_cents: number; collected_cents: number }[];
  aging: {
    buckets: AgingBucket[];
    by_customer: ({ customer_id: number; customer_name: string; total_cents: number } & Record<string, number | string>)[];
  };
  past_due: {
    invoice_id: number;
    customer_name: string;
    due_on: string;
    days_past_due: number;
    balance_cents: number;
  }[];
  pipeline: Record<string, { count: number; cents: number; flagged: number }>;
  attention: {
    awaiting_approval: number;
    flagged_awaiting_approval: number;
    sync_failures: number;
    open_variances: number;
  };
};

export type DeliveryReport = {
  id: number;
  filename: string;
  row_count: number;
  uploaded_by: string;
  uploaded_at: string;
};

export type ReconRow = {
  campaign_id: number;
  campaign_name: string;
  customer_name: string;
  cpm_cents: number;
  booked_impressions: number;
  reported_impressions: number | null;
  billed_impressions: number | null;
  invoice_id: number | null;
  invoice_status: string | null;
  variance_impressions: number | null;
  variance_pct: string | null; // Decimal from the server, e.g. "-2.02"
  variance_cents: number | null;
  status: string;
  flagged: boolean;
  resolved: boolean;
  resolution_note: string | null;
  resolved_by: string | null;
  suggestion: string | null;
};

export type SyncJob = {
  id: number;
  invoice_id: number;
  idempotency_key: string;
  status: string;
  attempts: number;
  next_attempt_at: string;
  last_error: string | null;
  external_id: string | null;
  synced_at: string | null;
};

export type MockNetSuite = {
  mode: string;
  modes: Record<string, string>;
  calls: number;
  invoices: { netsuite_id: string; external_ref: string; total_cents: number }[];
};

// The server sends naive UTC timestamps from SQLite; mark them as UTC before parsing.
export const parseUtc = (s: string) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : `${s}Z`);

export type AuditEvent = {
  id: number;
  entity_type: string;
  entity_id: number;
  action: string;
  actor: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string | null;
  at: string;
};

// Stand-in for a real auth token: the backend reads the acting user from X-User.
let currentUser = "";
export const setCurrentUser = (username: string) => {
  currentUser = username;
};

export type RowError = { line: number; message: string };

export class ApiError extends Error {
  constructor(message: string, public rowErrors: RowError[] = []) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", "X-User": currentUser },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const d = body.detail;
    if (typeof d === "string") throw new ApiError(d);
    if (d && typeof d.message === "string") throw new ApiError(d.message, d.errors ?? []);
    if (Array.isArray(d)) throw new ApiError(d.map((e: { msg?: string }) => e.msg).join("; "));
    throw new ApiError(`Request failed (${res.status})`);
  }
  return res.json();
}

// For binary responses (PDFs, CSVs): same auth header, returns a Blob.
export async function fetchBlob(path: string): Promise<Blob> {
  const res = await fetch(`/api${path}`, { headers: { "X-User": currentUser } });
  if (!res.ok) throw new ApiError(`Download failed (${res.status})`);
  return res.blob();
}

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
};

// Display only. The server owns all money math and stores integer cents.
export const usd = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

// Dollars typed by a person -> integer cents, without float drift ("19.99" -> 1999).
export function dollarsToCents(input: string): number | null {
  const m = input.trim().match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
}

export const statusLabel = (s: string) => s.replace(/_/g, " ");

// Local calendar date as YYYY-MM-DD (toISOString would give the UTC date).
export const todayIso = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

// A plain date from the server ("2026-11-02") shown without time-zone drift.
export const showDate = (iso: string) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
