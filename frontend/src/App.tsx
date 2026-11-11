import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import {
  api,
  Anomaly,
  AuditEvent,
  Campaign,
  CreditMemo,
  Customer,
  dollarsToCents,
  fetchBlob,
  Invoice,
  parseUtc,
  setCurrentUser,
  showDate,
  statusLabel,
  todayIso,
  usd,
  User,
} from "./api";
import { DashboardSection } from "./Dashboard";
import { DeliverySection, ReconciliationSection, SyncSection } from "./Phase3";

function ErrorText({ error }: { error: unknown }) {
  return error ? <p className="error">{(error as Error).message}</p> : null;
}

function useCustomers() {
  return useQuery({ queryKey: ["customers"], queryFn: () => api.get<Customer[]>("/customers") });
}

// Every invoice or memo response is the fresh invoice; drop it into the list cache.
function useInvoiceMutation<V>(fn: (v: V) => Promise<Invoice>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (inv) => {
      qc.setQueryData<Invoice[]>(["invoices"], (old) => old?.map((i) => (i.id === inv.id ? inv : i)));
      qc.invalidateQueries({ queryKey: ["audit", inv.id] });
    },
  });
}

/* ---------------- user switcher ---------------- */

function UserSwitcher({ users, value, onChange }: { users: User[]; value: string; onChange: (u: string) => void }) {
  const me = users.find((u) => u.username === value);
  return (
    <div className="user-switcher">
      <label htmlFor="acting-as">Acting as</label>
      <select id="acting-as" value={value} onChange={(e) => onChange(e.target.value)}>
        {users.map((u) => (
          <option key={u.username} value={u.username}>{u.display_name}</option>
        ))}
      </select>
      {me && <span className="muted">roles: {me.roles.replace(",", ", ")}</span>}
    </div>
  );
}

/* ---------------- customers ---------------- */

function CustomersSection() {
  const qc = useQueryClient();
  const { data: customers = [] } = useCustomers();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [terms, setTerms] = useState("30");

  const create = useMutation({
    mutationFn: () => api.post<Customer>("/customers", { name, email, payment_terms_days: Number(terms) }),
    onSuccess: () => {
      setName("");
      setEmail("");
      qc.invalidateQueries({ queryKey: ["customers"] });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <section>
      <h2>Customers</h2>
      <form onSubmit={submit}>
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
        <input placeholder="Billing email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <select value={terms} onChange={(e) => setTerms(e.target.value)} aria-label="Payment terms">
          {[15, 30, 45, 60].map((d) => (
            <option key={d} value={d}>Net {d}</option>
          ))}
        </select>
        <button disabled={create.isPending}>Add customer</button>
      </form>
      <ErrorText error={create.error} />
      <ul>
        {customers.map((c) => (
          <li key={c.id}>
            {c.name} <span className="muted">{c.email} · Net {c.payment_terms_days}</span>
          </li>
        ))}
      </ul>
      {customers.length === 0 && <p className="muted">No customers yet. Add one to get started.</p>}
    </section>
  );
}

/* ---------------- campaigns ---------------- */

function CampaignsSection() {
  const qc = useQueryClient();
  const { data: customers = [] } = useCustomers();
  const { data: campaigns = [] } = useQuery({
    queryKey: ["campaigns"],
    queryFn: () => api.get<Campaign[]>("/campaigns"),
  });
  const [form, setForm] = useState({ customer_id: "", name: "", booked: "", delivered: "", cpm: "" });
  const [formError, setFormError] = useState("");
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [k]: e.target.value });

  const create = useMutation({
    mutationFn: (cpm_cents: number) =>
      api.post<Campaign>("/campaigns", {
        customer_id: Number(form.customer_id),
        name: form.name,
        booked_impressions: Number(form.booked),
        delivered_impressions: Number(form.delivered),
        cpm_cents,
      }),
    onSuccess: () => {
      setForm({ ...form, name: "", booked: "", delivered: "", cpm: "" });
      qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cents = dollarsToCents(form.cpm);
    if (cents === null) return setFormError("Enter CPM as dollars, like 12.50");
    setFormError("");
    create.mutate(cents);
  };
  const customerName = (id: number) => customers.find((c) => c.id === id)?.name ?? id;

  return (
    <section>
      <h2>Campaigns</h2>
      <form onSubmit={submit}>
        <select value={form.customer_id} onChange={set("customer_id")} required>
          <option value="">Customer</option>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <input placeholder="Campaign name" value={form.name} onChange={set("name")} required />
        <input placeholder="Booked impressions" type="number" min="0" value={form.booked} onChange={set("booked")} required />
        <input placeholder="Delivered impressions" type="number" min="0" value={form.delivered} onChange={set("delivered")} required />
        <input placeholder="CPM ($)" inputMode="decimal" value={form.cpm} onChange={set("cpm")} required />
        <button disabled={create.isPending}>Add campaign</button>
      </form>
      {formError && <p className="error">{formError}</p>}
      <ErrorText error={create.error} />
      <table>
        <thead>
          <tr><th>Customer</th><th>Campaign</th><th>Booked</th><th>Delivered</th><th>CPM</th></tr>
        </thead>
        <tbody>
          {campaigns.map((c) => (
            <tr key={c.id}>
              <td>{customerName(c.customer_id)}</td>
              <td>{c.name}</td>
              <td className="num">{c.booked_impressions.toLocaleString()}</td>
              <td className="num">{c.delivered_impressions.toLocaleString()}</td>
              <td className="num">{usd(c.cpm_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ---------------- invoices ---------------- */

const ACTION_LABELS: Record<string, string> = {
  submit: "Submit for approval",
  approve: "Approve",
  reject: "Send back",
  post: "Post to ledger",
};

function InvoiceActions({ invoice }: { invoice: Invoice }) {
  const [reason, setReason] = useState("");
  const flagged = invoice.anomalies.length > 0;
  const needsNote = (a: string) => a === "reject" || (a === "approve" && flagged);
  const act = useInvoiceMutation((action: string) =>
    api.post<Invoice>(`/invoices/${invoice.id}/actions/${action}`, needsNote(action) ? { reason } : undefined)
  );
  const workflow = invoice.allowed_actions.filter((a) => a in ACTION_LABELS);
  if (workflow.length === 0) return null;
  const label = flagged && workflow.includes("approve")
    ? "Note: why it's OK to approve despite the flags, or why you're sending it back"
    : "Reason for sending back";

  return (
    <div className="actions">
      {workflow.includes("reject") && (
        <input placeholder={label} value={reason} onChange={(e) => setReason(e.target.value)} aria-label={label} />
      )}
      {workflow.map((a) => (
        <button
          key={a}
          className={a === "reject" ? "secondary" : undefined}
          disabled={act.isPending || (needsNote(a) && !reason.trim())}
          onClick={() => act.mutate(a)}
        >
          {ACTION_LABELS[a]}
        </button>
      ))}
      <ErrorText error={act.error} />
    </div>
  );
}

function CreditMemoForm({ invoice }: { invoice: Invoice }) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState("");
  const create = useInvoiceMutation((amount_cents: number) =>
    api.post<Invoice>(`/invoices/${invoice.id}/credit-memos`, { amount_cents, reason })
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cents = dollarsToCents(amount);
    if (!cents) return setFormError("Enter a credit amount in dollars, like 250.00");
    setFormError("");
    create.mutate(cents, {
      onSuccess: () => {
        setAmount("");
        setReason("");
      },
    });
  };

  return (
    <form onSubmit={submit} className="credit-form">
      <input placeholder="Credit ($)" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} required />
      <input placeholder="Reason, e.g. make-good for under-delivery" value={reason} onChange={(e) => setReason(e.target.value)} required minLength={3} />
      <button disabled={create.isPending}>Request credit memo</button>
      {formError && <p className="error">{formError}</p>}
      <ErrorText error={create.error} />
    </form>
  );
}

function CreditMemoRow({ memo }: { memo: CreditMemo }) {
  const act = useInvoiceMutation((action: string) =>
    api.post<Invoice>(`/credit-memos/${memo.id}/actions/${action}`)
  );
  return (
    <tr>
      <td>CM-{memo.id}</td>
      <td>{memo.reason}</td>
      <td>{memo.created_by}</td>
      <td><span className={`badge ${memo.status}`}>{statusLabel(memo.status)}</span></td>
      <td className="num">−{usd(memo.amount_cents)}</td>
      <td>
        {memo.allowed_actions.map((a) => (
          <button key={a} className={a === "reject" ? "secondary small" : "small"} disabled={act.isPending} onClick={() => act.mutate(a)}>
            {a === "approve" ? "Approve" : "Reject"}
          </button>
        ))}
        <ErrorText error={act.error} />
      </td>
    </tr>
  );
}

function AnomalyBanner({ anomalies, status }: { anomalies: Anomaly[]; status: string }) {
  if (anomalies.length === 0) return null;
  const beforeApproval = status === "draft" || status === "pending_approval";
  return (
    <div className="anomalies" role="note">
      <strong>
        <span aria-hidden="true" className="attention-icon">!</span>
        {beforeApproval ? "Check before approving" : "Flagged for review"}
      </strong>
      <ul>
        {anomalies.map((a, i) => (
          <li key={i}>{a.message}</li>
        ))}
      </ul>
      {status === "pending_approval" && (
        <p className="muted small-text">Approving a flagged invoice needs a note explaining why it's OK.</p>
      )}
    </div>
  );
}

function PaymentForm({ invoice }: { invoice: Invoice }) {
  const [amount, setAmount] = useState((invoice.balance_cents / 100).toFixed(2));
  const [receivedOn, setReceivedOn] = useState(todayIso());
  const [reference, setReference] = useState("");
  const [formError, setFormError] = useState("");
  const record = useInvoiceMutation((amount_cents: number) =>
    api.post<Invoice>(`/invoices/${invoice.id}/payments`, { amount_cents, received_on: receivedOn, reference })
  );
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cents = dollarsToCents(amount);
    if (!cents) return setFormError("Enter the amount received in dollars, like 1250.00");
    setFormError("");
    record.mutate(cents, { onSuccess: () => setReference("") });
  };
  return (
    <form onSubmit={submit} className="payment-form">
      <input aria-label="Amount received ($)" placeholder="Amount ($)" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} required />
      <input aria-label="Date received" type="date" value={receivedOn} max={todayIso()} onChange={(e) => setReceivedOn(e.target.value)} required />
      <input aria-label="Reference" placeholder="Check # or ACH reference" value={reference} onChange={(e) => setReference(e.target.value)} required />
      <button disabled={record.isPending}>Record payment</button>
      {formError && <p className="error">{formError}</p>}
      <ErrorText error={record.error} />
    </form>
  );
}

function PdfPreview({ invoice, onClose }: { invoice: Invoice; onClose: () => void }) {
  const filename = `INV-${String(invoice.id).padStart(6, "0")}${invoice.status === "posted" ? "" : "-draft"}.pdf`;
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => {
    // Fetch with the auth header, show it from a blob URL, and free the URL on close.
    let alive = true;
    let objectUrl: string | null = null;
    fetchBlob(`/invoices/${invoice.id}/pdf`)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (alive) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      })
      .catch((e) => alive && setError(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [invoice.id, invoice.status, invoice.balance_cents]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={`Preview of ${filename}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>{filename}</strong>
          <span>
            {url && <a className="button-link" href={url} download={filename}>Download</a>}
            <button className="secondary-neutral small" onClick={onClose} autoFocus>Close</button>
          </span>
        </div>
        {error && <p className="error">{error.message}</p>}
        {url ? <iframe title={filename} src={url} /> : !error && <p className="muted">Rendering…</p>}
      </div>
    </div>
  );
}

function describeEvent(e: AuditEvent): string {
  if (e.entity_type === "payment") {
    return `Payment of ${usd(e.after?.amount_cents as number)} received ${showDate(e.after?.received_on as string)}`;
  }
  if (e.entity_type === "sync_job") {
    const tries = e.after?.attempts as number | undefined;
    switch (e.action) {
      case "synced":
        return `Sent to NetSuite as ${e.after?.external_id}`;
      case "sync_retry_scheduled":
        return `NetSuite try ${tries} failed; will retry`;
      case "sync_failed":
        return "NetSuite sync failed and needs attention";
      case "sync_retry_requested":
        return "Retried the NetSuite sync";
    }
  }
  if (e.entity_type === "variance_resolution") {
    return "Resolved a delivery variance";
  }
  const from = e.before?.status as string | undefined;
  const to = e.after?.status as string | undefined;
  const subject = e.entity_type === "credit_memo" ? `Credit memo CM-${e.entity_id}` : "Invoice";
  if (e.action === "create") {
    const amount = e.after?.amount_cents ?? e.after?.total_cents;
    return `${subject} created${typeof amount === "number" ? ` for ${usd(amount)}` : ""}`;
  }
  return from && to ? `${subject}: ${statusLabel(from)} → ${statusLabel(to)}` : `${subject}: ${e.action}`;
}

function AuditTrail({ invoiceId }: { invoiceId: number }) {
  const { data: events = [], isLoading } = useQuery({
    queryKey: ["audit", invoiceId],
    queryFn: () => api.get<AuditEvent[]>(`/invoices/${invoiceId}/audit`),
  });
  if (isLoading) return <p className="muted">Loading history…</p>;
  return (
    <ol className="audit">
      {events.map((e) => (
        <li key={e.id}>
          <time dateTime={e.at}>{parseUtc(e.at).toLocaleString()}</time>
          <span>
            <strong>{e.actor}</strong> {describeEvent(e)}
            {e.reason && <span className="muted"> · “{e.reason}”</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}

function DueBadge({ invoice }: { invoice: Invoice }) {
  const d = invoice.days_past_due;
  if (d === null) {
    return invoice.status === "posted" && invoice.balance_cents === 0 ? <span className="badge paid">paid</span> : null;
  }
  if (d > 0) return <span className="badge overdue">{d} day{d === 1 ? "" : "s"} past due</span>;
  return <span className="badge due">due {d === 0 ? "today" : `in ${-d} day${d === -1 ? "" : "s"}`}</span>;
}

function InvoiceCard({ invoice, customerName }: { invoice: Invoice; customerName: string }) {
  const [open, setOpen] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const credited = invoice.total_cents - invoice.balance_cents - invoice.paid_cents;

  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        <span>Invoice #{invoice.id}: {customerName}</span>
        <span className={`badge ${invoice.status}`}>{statusLabel(invoice.status)}</span>
        {invoice.sync_status && (
          <span className={`badge sync-${invoice.sync_status}`}>
            {invoice.sync_status === "synced" ? "in NetSuite" : invoice.sync_status === "dead" ? "sync failed" : "syncing"}
          </span>
        )}
        <DueBadge invoice={invoice} />
        {invoice.anomalies.length > 0 && (
          <span className="badge flagged">{invoice.anomalies.length} flag{invoice.anomalies.length === 1 ? "" : "s"}</span>
        )}
        {invoice.allowed_actions.some((a) => a in ACTION_LABELS) && <span className="needs-you">needs you</span>}
        <strong>{usd(invoice.balance_cents)}</strong>
      </summary>

      <table>
        <thead>
          <tr><th>Campaign</th><th>Impressions</th><th>CPM</th><th>Amount</th></tr>
        </thead>
        <tbody>
          {invoice.lines.map((l) => (
            <tr key={l.id}>
              <td>{l.description}</td>
              <td className="num">{l.impressions.toLocaleString()}</td>
              <td className="num">{usd(l.cpm_cents)}</td>
              <td className="num">{usd(l.amount_cents)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><td colSpan={3}>Invoice total</td><td className="num">{usd(invoice.total_cents)}</td></tr>
          {credited > 0 && <tr><td colSpan={3}>Approved credits</td><td className="num">−{usd(credited)}</td></tr>}
          {invoice.paid_cents > 0 && <tr><td colSpan={3}>Payments received</td><td className="num">−{usd(invoice.paid_cents)}</td></tr>}
          <tr className="balance"><td colSpan={3}>Balance due</td><td className="num">{usd(invoice.balance_cents)}</td></tr>
        </tfoot>
      </table>

      <p className="muted">
        Prepared by {invoice.created_by}
        {invoice.submitted_by && `, submitted by ${invoice.submitted_by}`}
        {invoice.approved_by && `, approved by ${invoice.approved_by}`}
        {invoice.posted_at && `, posted ${parseUtc(invoice.posted_at).toLocaleDateString()}`}
        {invoice.netsuite_id && `, in NetSuite as ${invoice.netsuite_id}`}
        {invoice.due_on && `. Due ${showDate(invoice.due_on)}`}
      </p>

      <AnomalyBanner anomalies={invoice.anomalies} status={invoice.status} />
      <div className="actions">
        <button className="secondary-neutral" onClick={() => setPreviewing(true)}>
          {invoice.status === "posted" ? "View PDF" : "Preview PDF"}
        </button>
      </div>
      {previewing && <PdfPreview invoice={invoice} onClose={() => setPreviewing(false)} />}
      <InvoiceActions invoice={invoice} />

      {(invoice.payments.length > 0 || invoice.allowed_actions.includes("pay")) && (
        <>
          <h3>Payments</h3>
          {invoice.payments.length > 0 && (
            <table>
              <tbody>
                {invoice.payments.map((p) => (
                  <tr key={p.id}>
                    <td>{showDate(p.received_on)}</td>
                    <td>{p.reference}</td>
                    <td>{p.recorded_by}</td>
                    <td className="num">{usd(p.amount_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {invoice.allowed_actions.includes("pay") && <PaymentForm key={invoice.balance_cents} invoice={invoice} />}
        </>
      )}

      {(invoice.credit_memos.length > 0 || invoice.allowed_actions.includes("credit")) && (
        <>
          <h3>Credit memos</h3>
          {invoice.credit_memos.length > 0 && (
            <table>
              <tbody>
                {invoice.credit_memos.map((m) => <CreditMemoRow key={m.id} memo={m} />)}
              </tbody>
            </table>
          )}
          {invoice.allowed_actions.includes("credit") && <CreditMemoForm invoice={invoice} />}
        </>
      )}

      <h3>History</h3>
      {open && <AuditTrail invoiceId={invoice.id} />}
    </details>
  );
}

function InvoicesSection() {
  const qc = useQueryClient();
  const { data: customers = [] } = useCustomers();
  const { data: invoices = [] } = useQuery({
    queryKey: ["invoices"],
    queryFn: () => api.get<Invoice[]>("/invoices"),
    refetchInterval: (q) => (q.state.data?.some((i) => i.sync_status === "pending") ? 3000 : false),
  });
  const [customerId, setCustomerId] = useState("");

  const generate = useMutation({
    mutationFn: () => api.post<Invoice>("/invoices/generate", { customer_id: Number(customerId) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invoices"] }),
  });

  const customerName = (id: number) => customers.find((c) => c.id === id)?.name ?? `Customer ${id}`;

  return (
    <section>
      <h2>Invoices</h2>
      <form onSubmit={(e) => { e.preventDefault(); generate.mutate(); }}>
        <select value={customerId} onChange={(e) => setCustomerId(e.target.value)} required>
          <option value="">Customer</option>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <button disabled={generate.isPending}>Generate draft invoice</button>
      </form>
      <ErrorText error={generate.error} />
      {invoices.map((inv) => (
        <InvoiceCard key={inv.id} invoice={inv} customerName={customerName(inv.customer_id)} />
      ))}
      {invoices.length === 0 && <p className="muted">No invoices yet. Add a campaign with delivery, then generate one.</p>}
    </section>
  );
}

/* ---------------- app ---------------- */

const TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "invoices", label: "Invoices" },
  { id: "reconciliation", label: "Reconciliation" },
  { id: "sync", label: "NetSuite sync" },
  { id: "setup", label: "Customers & campaigns" },
] as const;
type Tab = (typeof TABS)[number]["id"];

export default function App() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("dashboard");
  const { data: users = [], error } = useQuery({ queryKey: ["users"], queryFn: () => api.get<User[]>("/users") });
  const [user, setUser] = useState("");

  useEffect(() => {
    if (!user && users.length > 0) {
      setCurrentUser(users[0].username);
      setUser(users[0].username);
    }
  }, [users, user]);

  const switchUser = (username: string) => {
    setCurrentUser(username);
    setUser(username);
    // allowed_actions depend on who is asking, so refetch everything as the new user
    qc.resetQueries({ predicate: (q) => q.queryKey[0] !== "users" });
  };

  return (
    <main>
      <header>
        <h1>Mini TallyIQ</h1>
        {users.length > 0 && <UserSwitcher users={users} value={user} onChange={switchUser} />}
      </header>
      <ErrorText error={error} />
      {user && (
        <>
          <nav className="tabs" role="tablist">
            {TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={tab === t.id}
                className={tab === t.id ? "tab active" : "tab"}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </nav>
          {tab === "dashboard" && <DashboardSection onNavigate={setTab} />}
          {tab === "invoices" && <InvoicesSection />}
          {tab === "reconciliation" && (
            <>
              <ReconciliationSection />
              <DeliverySection />
            </>
          )}
          {tab === "sync" && <SyncSection />}
          {tab === "setup" && (
            <>
              <CampaignsSection />
              <CustomersSection />
            </>
          )}
        </>
      )}
    </main>
  );
}
