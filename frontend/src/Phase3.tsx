import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, useState } from "react";
import {
  api,
  ApiError,
  DeliveryReport,
  MockNetSuite,
  parseUtc,
  ReconRow,
  statusLabel,
  SyncJob,
  usd,
} from "./api";

function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const rows = error instanceof ApiError ? error.rowErrors : [];
  return (
    <div className="error">
      <p>{(error as Error).message}</p>
      {rows.length > 0 && (
        <ul className="row-errors">
          {rows.map((r, i) => (
            <li key={i}>Line {r.line}: {r.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

const fmt = (n: number | null) => (n === null ? "—" : n.toLocaleString());
const signed = (n: number) => (n > 0 ? `+${n.toLocaleString()}` : n.toLocaleString());
const signedUsd = (c: number) => (c > 0 ? `+${usd(c)}` : c < 0 ? `−${usd(-c)}` : usd(0));

/* ---------------- delivery import ---------------- */

export function DeliverySection() {
  const qc = useQueryClient();
  const { data: reports = [] } = useQuery({
    queryKey: ["delivery-reports"],
    queryFn: () => api.get<DeliveryReport[]>("/delivery-reports"),
  });

  const upload = useMutation({
    mutationFn: async (file: File) =>
      api.post<DeliveryReport>("/delivery-reports", { filename: file.name, csv: await file.text() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["delivery-reports"] });
      qc.invalidateQueries({ queryKey: ["reconciliation"] });
    },
  });

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) upload.mutate(file);
    e.target.value = ""; // allow choosing the same file again
  };

  return (
    <section>
      <h2>Delivery reports</h2>
      <p className="muted">
        Upload the ad server's delivery CSV with columns <code>campaign_id, date, impressions</code>. A file
        with any bad row imports nothing. If a later file repeats a campaign and day, the later number wins.
      </p>
      <label className="file-button">
        <input type="file" accept=".csv,text/csv" onChange={onFile} disabled={upload.isPending} />
        {upload.isPending ? "Importing…" : "Upload delivery CSV"}
      </label>
      {upload.isSuccess && (
        <p className="success">Imported {upload.data.row_count.toLocaleString()} rows from {upload.data.filename}.</p>
      )}
      <ErrorText error={upload.error} />
      {reports.length > 0 && (
        <table>
          <thead>
            <tr><th>Report</th><th>File</th><th>Rows</th><th>Uploaded</th></tr>
          </thead>
          <tbody>
            {reports.map((r) => (
              <tr key={r.id}>
                <td>#{r.id}</td>
                <td>{r.filename}</td>
                <td className="num">{r.row_count.toLocaleString()}</td>
                <td>{r.uploaded_by}, {parseUtc(r.uploaded_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/* ---------------- reconciliation ---------------- */

function ResolveForm({ row, threshold }: { row: ReconRow; threshold: string }) {
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const resolve = useMutation({
    mutationFn: () =>
      api.post<ReconRow[]>(`/reconciliation/${row.campaign_id}/resolve`, { note, threshold_pct: threshold }),
    onSuccess: (rows) => qc.setQueryData(["reconciliation", threshold], rows),
  });
  return (
    <form
      className="resolve-form"
      onSubmit={(e) => {
        e.preventDefault();
        resolve.mutate();
      }}
    >
      <input
        placeholder="Explain this variance"
        aria-label={`Explanation for ${row.campaign_name}`}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        required
        minLength={3}
      />
      <button className="small" disabled={resolve.isPending}>Mark resolved</button>
      <ErrorText error={resolve.error} />
    </form>
  );
}

const RECON_LABELS: Record<string, string> = {
  matched: "matched",
  over_billed: "over-billed",
  under_billed: "under-billed",
  not_billed: "not invoiced",
  no_delivery_data: "no delivery data",
};

export function ReconciliationSection() {
  const [threshold, setThreshold] = useState("2");
  const valid = /^\d{1,2}(\.\d{1,2})?$|^100$/.test(threshold);
  const { data: rows = [], error } = useQuery({
    queryKey: ["reconciliation", threshold],
    queryFn: () => api.get<ReconRow[]>(`/reconciliation?threshold_pct=${threshold}`),
    enabled: valid,
  });

  const open = rows.filter((r) => r.flagged && !r.resolved);
  const overBilled = open.filter((r) => (r.variance_cents ?? 0) < 0).reduce((s, r) => s - (r.variance_cents ?? 0), 0);
  const unbilled = open.filter((r) => (r.variance_cents ?? 0) > 0).reduce((s, r) => s + (r.variance_cents ?? 0), 0);

  return (
    <section>
      <div className="section-head">
        <h2>Reconciliation</h2>
        <label className="threshold">
          Flag variances over
          <input value={threshold} onChange={(e) => setThreshold(e.target.value)} inputMode="decimal" aria-invalid={!valid} />
          %
        </label>
      </div>
      <p className={open.length ? "summary attention" : "summary"}>
        {rows.length === 0
          ? "Add campaigns and upload a delivery report to reconcile."
          : open.length === 0
            ? "Everything reconciles within the threshold."
            : `${open.length} open variance${open.length === 1 ? "" : "s"}: ${usd(overBilled)} possibly over-billed, ${usd(unbilled)} possibly unbilled.`}
      </p>
      <ErrorText error={error} />
      {rows.length > 0 && (
        <div className="table-scroll">
          <table className="recon">
            <thead>
              <tr>
                <th>Campaign</th>
                <th>Billed</th>
                <th>Ad server</th>
                <th>Variance</th>
                <th>Value</th>
                <th>Status and next step</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.campaign_id} className={r.flagged && !r.resolved ? "flag" : undefined}>
                  <td>
                    {r.campaign_name}
                    <div className="muted small-text">
                      {r.customer_name}
                      {r.invoice_id && ` · invoice #${r.invoice_id} (${statusLabel(r.invoice_status ?? "")})`}
                    </div>
                  </td>
                  <td className="num">{fmt(r.billed_impressions)}</td>
                  <td className="num">{fmt(r.reported_impressions)}</td>
                  <td className="num">
                    {r.variance_impressions === null ? "—" : signed(r.variance_impressions)}
                    {r.variance_pct !== null && <div className="muted small-text">{r.variance_pct}%</div>}
                  </td>
                  <td className="num">{r.variance_cents === null ? "—" : signedUsd(r.variance_cents)}</td>
                  <td>
                    <span className={`badge recon-${r.status}${r.resolved ? " resolved" : ""}`}>
                      {r.resolved ? "resolved" : RECON_LABELS[r.status] ?? r.status}
                    </span>
                    {r.resolved && (
                      <div className="muted small-text">{r.resolved_by}: “{r.resolution_note}”</div>
                    )}
                    {r.suggestion && <div className="small-text">{r.suggestion}</div>}
                    {r.flagged && !r.resolved && <ResolveForm row={r} threshold={threshold} />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/* ---------------- NetSuite sync ---------------- */

function MockControls() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["mock-netsuite"],
    queryFn: () => api.get<MockNetSuite>("/mock-netsuite"),
    refetchInterval: 3000,
  });
  const setMode = useMutation({
    mutationFn: (mode: string) => api.post<MockNetSuite>("/mock-netsuite/mode", { mode }),
    onSuccess: (state) => qc.setQueryData(["mock-netsuite"], state),
  });
  if (!data) return null;

  return (
    <section className="mock">
      <h2>Mock NetSuite</h2>
      <p className="muted">Demo controls for how the fake NetSuite behaves on the next calls.</p>
      <div className="modes" role="radiogroup" aria-label="NetSuite behavior">
        {Object.entries(data.modes).map(([mode, description]) => (
          <label key={mode} className={data.mode === mode ? "mode active" : "mode"}>
            <input
              type="radio"
              name="ns-mode"
              checked={data.mode === mode}
              onChange={() => setMode.mutate(mode)}
            />
            <strong>{mode.replace("_", " ")}</strong>
            <span className="muted small-text">{description}</span>
          </label>
        ))}
      </div>
      <ErrorText error={setMode.error} />
      <p className="muted small-text">
        {data.calls} API call{data.calls === 1 ? "" : "s"} received · {data.invoices.length} invoice
        {data.invoices.length === 1 ? "" : "s"} stored
      </p>
      {data.invoices.length > 0 && (
        <table>
          <thead>
            <tr><th>NetSuite id</th><th>Our ref</th><th>Total</th></tr>
          </thead>
          <tbody>
            {data.invoices.map((i) => (
              <tr key={i.netsuite_id}>
                <td>{i.netsuite_id}</td>
                <td>{i.external_ref}</td>
                <td className="num">{usd(i.total_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RetryButton({ job }: { job: SyncJob }) {
  const qc = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.post<SyncJob>(`/sync/jobs/${job.id}/retry`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sync-jobs"] }),
  });
  return (
    <>
      <button className="small" disabled={retry.isPending} onClick={() => retry.mutate()}>Retry</button>
      <ErrorText error={retry.error} />
    </>
  );
}

export function SyncSection() {
  const qc = useQueryClient();
  const { data: jobs = [] } = useQuery({
    queryKey: ["sync-jobs"],
    queryFn: () => api.get<SyncJob[]>("/sync/jobs"),
    refetchInterval: 3000, // the background worker runs every 5 seconds
  });
  const runNow = useMutation({
    mutationFn: () => api.post<{ synced: number; retrying: number; dead: number }>("/sync/run"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sync-jobs"] });
      qc.invalidateQueries({ queryKey: ["mock-netsuite"] });
      qc.invalidateQueries({ queryKey: ["invoices"] });
    },
  });

  const when = (j: SyncJob) => {
    if (j.status === "synced" && j.synced_at) return `synced ${parseUtc(j.synced_at).toLocaleTimeString()}`;
    if (j.status === "pending") {
      const secs = Math.round((parseUtc(j.next_attempt_at).getTime() - Date.now()) / 1000);
      return secs > 0 ? `next try in ${secs}s` : "due now";
    }
    return "needs attention";
  };

  return (
    <>
      <section>
        <div className="section-head">
          <h2>NetSuite sync</h2>
          <button onClick={() => runNow.mutate()} disabled={runNow.isPending}>Sync now</button>
        </div>
        <p className="muted">
          Posting an invoice queues it here. A background worker sends it every few seconds and retries
          failures with growing waits (10s, 20s, 40s…). After 5 failed tries, or a rejection, it stops and
          waits for an approver.
        </p>
        {runNow.data && (
          <p className="success">
            Sent: {runNow.data.synced} synced, {runNow.data.retrying} will retry, {runNow.data.dead} failed.
          </p>
        )}
        <ErrorText error={runNow.error} />
        {jobs.length === 0 ? (
          <p className="muted">Nothing queued yet. Post an invoice to send it to NetSuite.</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Invoice</th><th>Status</th><th>Tries</th><th>When</th><th>NetSuite id</th><th>Last error</th><th /></tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>#{j.invoice_id}</td>
                    <td><span className={`badge sync-${j.status}`}>{j.status === "dead" ? "failed" : j.status}</span></td>
                    <td className="num">{j.attempts}</td>
                    <td className="small-text">{when(j)}</td>
                    <td>{j.external_id ?? "—"}</td>
                    <td className="small-text">{j.last_error ?? ""}</td>
                    <td>{j.status === "dead" && <RetryButton job={j} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <MockControls />
    </>
  );
}
