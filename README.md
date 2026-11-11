# Mini TallyIQ

A practice internal finance app: customers, campaigns, invoices with an approval
workflow, credit memos, an append-only audit trail, delivery reconciliation, a
NetSuite sync with retries, payments, AR aging, a dashboard, anomaly flags, and
invoice PDFs.

## Run the backend
    cd backend
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    uvicorn app.main:app --reload        # API docs at http://localhost:8000/docs
    python -m pytest                     # run the tests

Upgrading from an earlier phase? Delete `backend/tallyiq.db` first. The tables
gained columns and `create_all` doesn't alter existing tables (Alembic would).

Want six months of demo data for the dashboard? On an empty database run
`python -m app.seed` (fictional customers, invoices and payments, dated relative
to today), then start the server.

A background worker sends queued invoices to the mock NetSuite every 5 seconds.
Set `SYNC_WORKER=0` to turn it off and use the "Sync now" button instead.

## Run the frontend
    cd frontend
    npm install
    npm run dev                          # http://localhost:5173

## Users
Pick who you're acting as in the top right. The backend reads it from the
`X-User` header, a stand-in for a real auth token.

| User   | Roles               | Notes                                           |
|--------|---------------------|-------------------------------------------------|
| priya  | preparer            | Generates invoices, requests credit memos       |
| marcus | approver            | Approves, sends back, posts                     |
| dana   | preparer, approver  | Can approve others' work, never her own         |

## Try it
1. As priya: add a customer and a campaign, generate a draft invoice, submit it.
2. Notice priya can't approve it. Switch to marcus, approve it, post it.
3. As priya: request a credit memo on the posted invoice. As marcus: approve it.
   The balance drops; the posted invoice total doesn't change.
4. Open History on the invoice to see every step, who did it, and why.

## How the rules are enforced
- **State machine** (`app/workflow.py`): draft → pending approval → approved → posted,
  with "send back" (reason required) returning to draft. Skipping steps is a 409.
- **Segregation of duties:** nobody approves an invoice they prepared or submitted,
  or a credit memo they created, even with both roles.
- **Server decides what's allowed:** each invoice comes back with
  `allowed_actions` for the current user, and the UI renders only those buttons.
  The API still re-checks every request.
- **Concurrency:** transitions are conditional updates
  (`UPDATE ... WHERE status = 'pending_approval'`), so if two approvers click at
  once, one wins and the other gets a 409.
- **Posted means immutable:** corrections go through credit memos. Credits can't
  exceed what's left on the invoice, counting ones still awaiting approval.
- **Audit log** (`app/audit.py`): every change writes before/after snapshots,
  actor and reason in the same transaction as the change itself.
- **Defense in depth** (`app/models.py`): a SQLAlchemy `before_flush` hook refuses
  to update or delete audit events, edit posted invoices, or delete financial
  records, even from code that bypasses the API. In Postgres you'd add DB
  permissions or triggers on top.

## Phase 3: reconciliation and NetSuite sync

### Try it
1. In Customers & campaigns, add two campaigns for a customer: campaign 1 with
   1,000,000 delivered impressions at $5.00 CPM, campaign 2 with 250,000 at $8.00.
   Generate, approve and post the invoice (Phase 2 steps).
2. Reconciliation tab: upload `sample_data/delivery_oct.csv`. Campaign 1 shows
   979,800 delivered vs. 1,000,000 billed: over-billed by 2.02%, about $101, flagged
   with a suggestion to issue a credit memo. Campaign 2 matches.
3. Upload the same file again: refused as a duplicate. Upload
   `sample_data/restatement_oct30.csv`: the restated day replaces the original and
   the variance grows.
4. Mark a variance resolved with a note, change the threshold, and watch flags
   come and go. New delivery data reopens a resolved variance.
5. NetSuite sync tab: pick "lost response", post another invoice, and watch it
   retry and land as a single NetSuite record. Try "down" until it fails, then
   switch back to "healthy" and retry it as marcus.

### Delivery import (`app/delivery.py`)
- CSV with `campaign_id, date, impressions`. All-or-nothing: one bad row and
  nothing is saved, and every problem comes back with its line number.
- Re-uploading the same file is refused (SHA-256 checksum).
- Restatements: a later report for the same campaign and day replaces the earlier
  number, while the original rows are kept for the record.

### Reconciliation (`app/reconciliation.py`)
- Compares ad-server delivery with billed impressions per campaign: matched,
  over-billed, under-billed, not invoiced yet, or no delivery data.
- Flags variances over a threshold (2% by default), values them in dollars with
  the same rounding as invoicing, and suggests the next step.
- A resolution is tied to the exact variance it explained, so new data reopens
  the flag instead of hiding a new problem behind an old note.

### NetSuite sync (`app/sync.py`, `app/netsuite.py`)
- **Transactional outbox:** posting an invoice and queueing its sync happen in one
  transaction, so an invoice is never posted without being queued, or queued
  without being posted.
- **Idempotency keys:** each invoice has a fixed key (`tallyiq-invoice-<id>`) and a
  deterministic payload. If NetSuite saved the invoice but the response was lost,
  the retry gets the same NetSuite id back instead of creating a duplicate. Reusing
  a key with a different payload is rejected.
- **Retries:** timeouts and 5xx retry after 10s, 20s, 40s, 80s (capped at 10 min).
  Validation errors fail immediately. After 5 tries the job stops and an approver
  can retry it once the cause is fixed.
- **Leases:** a worker claims a job for 60 seconds with a conditional update, so
  two workers never send the same job, and a crashed worker's job is picked up
  again when the lease runs out.
- Every attempt shows up in the invoice's history.

In production the worker would be its own Cloud Run job or a Pub/Sub or Cloud
Tasks consumer, and the fake client would be swapped for one that calls
NetSuite's REST API with the same `create_invoice(payload, idempotency_key)` shape.

## Phase 4: dashboard, aging, anomalies and PDFs

### Try it
1. Delete `backend/tallyiq.db`, run `python -m app.seed`, start both servers.
2. Dashboard: open receivables, past due, DSO and collections; billed vs. collected
   for six months; receivables by age; the most overdue invoices.
3. Change "As of" to a past date: everything recomputes as it stood that day. Move
   it forward a few months to see unpaid invoices slide into older buckets.
   Download the aging as CSV.
4. "Needs attention" links to the Riverside invoice waiting for approval. It's
   flagged as about 10× the customer's usual amount and at an unusual CPM. As
   marcus, try to approve it: you need to write a note first.
5. Open a posted invoice: record a payment, then "View PDF". Open a draft:
   "Preview PDF" shows a DRAFT watermark.

### Payments and due dates
- Posting sets the due date from the customer's terms (Net 15/30/45/60).
- Payments are recorded by preparers, can't be in the future or exceed what's
  open (credits awaiting approval are held back), and can't be edited.
- Balance due = total − approved credits − payments.

### AR aging and the dashboard (`app/analytics.py`)
- Buckets by days past due: current, 1–30, 31–60, 61+.
- **Point-in-time:** only invoices posted, payments received and credits approved
  on or before the as-of date count, so Sept 30's aging is the same whether you
  run it on Oct 1 or next March.
- DSO = open receivables ÷ the last 90 days of billing × 90.
- Dates are local business dates, not UTC, so an invoice posted at 9pm in New
  York isn't counted as tomorrow's.

### Anomaly flags (`app/anomalies.py`)
Rules a finance person would check by eye, each with a plain-English reason:
- **Unusual amount:** modified z-score over 3.5 using median and MAD, which one
  huge past invoice can't skew the way mean and standard deviation can. Needs 3+
  posted invoices of history; otherwise it stays quiet rather than guess.
- **Unusual CPM:** a line's CPM more than 50% off the customer's usual rate.
- **Billed more than booked.**
- **Possible duplicate:** same customer and total within 31 days.

Flags don't block anything. Approving a flagged invoice requires a note, which goes
into the audit trail: the system surfaces the risk and a person owns the decision.

### Invoice PDF (`app/pdf.py`)
ReportLab, rendered on the server. Anything not posted carries a DRAFT watermark
and footer so a preview can't be sent as a real invoice. Set `SELLER_NAME`,
`SELLER_ADDRESS` and `REMIT_TEXT` to change the header and payment instructions.

### Charts
Recharts, with a color-blind-checked palette: blue and orange for billed vs.
collected, and a one-hue ramp for aging where darker means older. Each chart has
hover tooltips; billed vs. collected has a table view, and aging has the
customer table.

## Deploy for free (Render)

The whole app runs as one container: FastAPI serves the API at `/api` and the
built React app at `/` (`backend/app/server.py`, `Dockerfile`).

### 1. Check it builds locally
    cd backend && python -m pytest
    cd ../frontend && npm run build

The Docker build runs the same `npm run build`, including the TypeScript check,
so fix any errors here first. With Docker installed you can also run the real
container:

    docker build -t mini-tallyiq .
    docker run -p 8080:8080 -e SEED_DEMO_DATA=1 mini-tallyiq    # http://localhost:8080

### 2. Push the project to GitHub
Any repo works, public or private, with this README's folder at the root.

### 3. Create the service on Render
Sign up at render.com (free), then **New > Blueprint** and pick your repo. Render
reads `render.yaml`: a Docker web service on the free plan with
`SEED_DEMO_DATA=1`. (Or **New > Web Service**, choose Docker and the Free
instance, and add that environment variable yourself.) The first build takes a
few minutes, then the app is live at `https://<name>.onrender.com`.

### What to expect on the free plan
- It **sleeps after 15 minutes** with no visitors; the next visit takes about a
  minute to wake it. Open it a minute before you demo.
- **Data resets** on every restart, redeploy or sleep: the disk is wiped and the
  demo data is loaded again. Handy for demos; it also means changes don't last.
- 750 free hours a month per workspace covers one service all month.
- **Anyone with the link can use it as any demo user**, because auth is still the
  `X-User` stub. Only ever put fictional data in it.

### Google Cloud Run instead
Closer to the job's stack, but it needs a billing account with a card. New
accounts get free credits, and there's no charge while nobody is using it, but
storing the image can cost a little.

    gcloud services enable run.googleapis.com cloudbuild.googleapis.com
    gcloud run deploy mini-tallyiq --source . --region us-east1 \
      --allow-unauthenticated --max-instances 1 --set-env-vars SEED_DEMO_DATA=1

`--max-instances 1` keeps everyone on the same SQLite file and mock NetSuite. By
default Cloud Run only gives the container CPU while it's handling a request, so
the 5-second sync worker only runs while someone is using the app; "Sync now"
always works.
