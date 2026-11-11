import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, Dashboard, fetchBlob, saveBlob, showDate, todayIso, usd } from "./api";

// Chart colors (reference data-viz palette): two categorical slots for billed vs.
// collected, and a one-hue blue ordinal ramp for aging (darker = older).
const SERIES_BILLED = "#2a78d6";
const SERIES_COLLECTED = "#eb6834";
const AGING_RAMP = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"];
const GRID = "#e6eaf0";
const AXIS_TEXT = "#6b7686";

type Nav = (tab: "invoices" | "reconciliation" | "sync") => void;

const compactUsd = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });

function Kpi({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {note && <div className="kpi-note">{note}</div>}
    </div>
  );
}

function MoneyTooltip({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-title">{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} className="chart-tooltip-row">
          <span className="swatch" style={{ background: p.color ?? p.payload?.fill }} />
          <span>{p.name}</span>
          <strong>{usd(p.value)}</strong>
        </div>
      ))}
    </div>
  );
}

function BilledVsCollected({ data }: { data: Dashboard["monthly"] }) {
  return (
    <figure className="chart">
      <figcaption>
        <h3>Billed vs. collected by month</h3>
      </figcaption>
      <div className="chart-box">
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={data} barGap={2} barCategoryGap="28%" margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
            <CartesianGrid vertical={false} stroke={GRID} />
            <XAxis dataKey="month" tickLine={false} axisLine={{ stroke: GRID }} tick={{ fill: AXIS_TEXT, fontSize: 12 }} />
            <YAxis tickFormatter={compactUsd} tickLine={false} axisLine={false} tick={{ fill: AXIS_TEXT, fontSize: 12 }} width={56} />
            <Tooltip content={<MoneyTooltip />} cursor={{ fill: "rgba(28,36,48,0.05)" }} />
            <Legend iconType="square" wrapperStyle={{ fontSize: 13, color: "#1c2430" }} />
            <Bar dataKey="billed_cents" name="Billed" fill={SERIES_BILLED} radius={[4, 4, 0, 0]} maxBarSize={28} />
            <Bar dataKey="collected_cents" name="Collected" fill={SERIES_COLLECTED} radius={[4, 4, 0, 0]} maxBarSize={28} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <details className="table-view">
        <summary>View as table</summary>
        <table>
          <thead>
            <tr><th>Month</th><th>Billed</th><th>Collected</th></tr>
          </thead>
          <tbody>
            {data.map((m) => (
              <tr key={m.month}>
                <td>{m.month}</td>
                <td className="num">{usd(m.billed_cents)}</td>
                <td className="num">{usd(m.collected_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

function AgingChart({ buckets }: { buckets: Dashboard["aging"]["buckets"] }) {
  const data = buckets.map((b) => ({ ...b, name: b.label }));
  return (
    <figure className="chart">
      <figcaption>
        <h3>Open receivables by age</h3>
        <p className="muted small-text">Days past the due date. Darker bars are older.</p>
      </figcaption>
      <div className="chart-box">
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={data} barCategoryGap="30%" margin={{ top: 24, right: 8, bottom: 0, left: 8 }}>
            <CartesianGrid vertical={false} stroke={GRID} />
            <XAxis dataKey="name" tickLine={false} axisLine={{ stroke: GRID }} tick={{ fill: AXIS_TEXT, fontSize: 12 }} />
            <YAxis tickFormatter={compactUsd} tickLine={false} axisLine={false} tick={{ fill: AXIS_TEXT, fontSize: 12 }} width={56} />
            <Tooltip
              cursor={{ fill: "rgba(28,36,48,0.05)" }}
              content={({ active, payload }) =>
                active && payload?.length ? (
                  <div className="chart-tooltip">
                    <div className="chart-tooltip-title">{payload[0].payload.label}</div>
                    <div className="chart-tooltip-row">
                      <span>{payload[0].payload.count} invoice{payload[0].payload.count === 1 ? "" : "s"}</span>
                      <strong>{usd(payload[0].payload.cents)}</strong>
                    </div>
                  </div>
                ) : null
              }
            />
            <Bar dataKey="cents" name="Open balance" radius={[4, 4, 0, 0]} maxBarSize={56}>
              {data.map((b, i) => (
                <Cell key={b.key} fill={AGING_RAMP[i]} />
              ))}
              <LabelList dataKey="cents" position="top" formatter={(v: unknown) => (typeof v === "number" && v ? compactUsd(v) : "")} style={{ fill: "#1c2430", fontSize: 12 }} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

function AgingTable({ data, asOf }: { data: Dashboard; asOf: string }) {
  const download = useMutation({
    mutationFn: async () => saveBlob(await fetchBlob(`/reports/aging.csv?as_of=${asOf}`), `ar-aging-${asOf}.csv`),
  });
  const cols = data.aging.buckets;
  const totals = Object.fromEntries(cols.map((c) => [c.key, c.cents]));
  const grand = cols.reduce((s, c) => s + c.cents, 0);
  return (
    <section>
      <div className="section-head">
        <h2>AR aging by customer</h2>
        <button className="secondary-neutral" onClick={() => download.mutate()} disabled={download.isPending}>
          Download CSV
        </button>
      </div>
      {download.error && <p className="error">{(download.error as Error).message}</p>}
      {data.aging.by_customer.length === 0 ? (
        <p className="muted">No open receivables as of {showDate(asOf)}.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Customer</th>
                {cols.map((c) => <th key={c.key}>{c.label}</th>)}
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              {data.aging.by_customer.map((r) => (
                <tr key={r.customer_id}>
                  <td>{r.customer_name}</td>
                  {cols.map((c) => (
                    <td key={c.key} className="num">{(r[c.key] as number) ? usd(r[c.key] as number) : "—"}</td>
                  ))}
                  <td className="num"><strong>{usd(r.total_cents)}</strong></td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="balance">
                <td>Total</td>
                {cols.map((c) => <td key={c.key} className="num">{usd(totals[c.key])}</td>)}
                <td className="num">{usd(grand)}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </section>
  );
}

function Attention({ data, onNavigate }: { data: Dashboard; onNavigate: Nav }) {
  const a = data.attention;
  const items: { text: string; tab: Parameters<Nav>[0]; action: string }[] = [];
  if (a.awaiting_approval)
    items.push({
      text: `${a.awaiting_approval} invoice${a.awaiting_approval === 1 ? "" : "s"} awaiting approval${a.flagged_awaiting_approval ? `, ${a.flagged_awaiting_approval} flagged for review` : ""}`,
      tab: "invoices",
      action: "Review",
    });
  if (a.open_variances)
    items.push({
      text: `${a.open_variances} delivery variance${a.open_variances === 1 ? "" : "s"} over 2%`,
      tab: "reconciliation",
      action: "Reconcile",
    });
  if (a.sync_failures)
    items.push({
      text: `${a.sync_failures} invoice${a.sync_failures === 1 ? "" : "s"} failed to sync to NetSuite`,
      tab: "sync",
      action: "Fix sync",
    });
  return (
    <section className="attention-panel">
      <h2>Needs attention</h2>
      {items.length === 0 ? (
        <p className="muted">Nothing waiting on you right now.</p>
      ) : (
        <ul className="attention">
          {items.map((it) => (
            <li key={it.text}>
              <span aria-hidden="true" className="attention-icon">!</span>
              <span>{it.text}</span>
              <button className="small" onClick={() => onNavigate(it.tab)}>{it.action}</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function DashboardSection({ onNavigate }: { onNavigate: Nav }) {
  const [asOf, setAsOf] = useState(todayIso());
  const valid = /^\d{4}-\d{2}-\d{2}$/.test(asOf);
  const { data, error, isLoading } = useQuery({
    queryKey: ["dashboard", asOf],
    queryFn: () => api.get<Dashboard>(`/dashboard?as_of=${asOf}`),
    enabled: valid,
  });
  const isToday = asOf === todayIso();

  return (
    <>
      <div className="dash-head">
        <label className="as-of">
          As of
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </label>
        {!isToday && (
          <button className="secondary-neutral small" onClick={() => setAsOf(todayIso())}>Back to today</button>
        )}
      </div>
      {error && <p className="error">{(error as Error).message}</p>}
      {isLoading && <p className="muted">Loading…</p>}
      {data && (
        <>
          <div className="kpis">
            <Kpi
              label="Open receivables"
              value={usd(data.kpis.outstanding_cents)}
              note={`${data.kpis.open_invoices} open invoice${data.kpis.open_invoices === 1 ? "" : "s"}`}
            />
            <Kpi
              label="Past due"
              value={usd(data.kpis.past_due_cents)}
              note={
                data.kpis.outstanding_cents
                  ? `${Math.round((data.kpis.past_due_cents / data.kpis.outstanding_cents) * 100)}% of open receivables`
                  : "Nothing open"
              }
            />
            <Kpi
              label="Days sales outstanding"
              value={data.kpis.dso_days === null ? "—" : `${data.kpis.dso_days} days`}
              note="Open receivables ÷ last 90 days of billing"
            />
            <Kpi
              label="Collected, last 30 days"
              value={usd(data.kpis.collected_30d_cents)}
              note={`${usd(data.kpis.billed_30d_cents)} billed in the same period`}
            />
          </div>

          {isToday && <Attention data={data} onNavigate={onNavigate} />}

          <section className="charts">
            <BilledVsCollected data={data.monthly} />
            <AgingChart buckets={data.aging.buckets} />
          </section>

          {data.past_due.length > 0 && (
            <section>
              <h2>Most overdue</h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr><th>Invoice</th><th>Customer</th><th>Due</th><th>Days late</th><th>Balance</th></tr>
                  </thead>
                  <tbody>
                    {data.past_due.map((p) => (
                      <tr key={p.invoice_id}>
                        <td>#{p.invoice_id}</td>
                        <td>{p.customer_name}</td>
                        <td>{showDate(p.due_on)}</td>
                        <td className="num">{p.days_past_due}</td>
                        <td className="num">{usd(p.balance_cents)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          <AgingTable data={data} asOf={asOf} />
        </>
      )}
    </>
  );
}
