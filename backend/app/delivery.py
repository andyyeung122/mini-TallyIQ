"""Import ad-server delivery reports (CSV).

Expected columns: campaign_id, date (YYYY-MM-DD), impressions

- All-or-nothing: if any row is invalid, nothing is saved and every problem is
  reported with its line number, so the person can fix the file and re-upload.
- Idempotent: the same file (by SHA-256) can't be imported twice.
- Restatements: if a later file reports the same campaign and day again, the
  later number wins (ad servers do restate delivery).
"""
import csv
import hashlib
import io
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Campaign, DeliveryReport, DeliveryRow, User

REQUIRED_COLUMNS = {"campaign_id", "date", "impressions"}
MAX_ROWS = 50_000


def parse(text: str, known_campaigns: set[int]) -> tuple[list[DeliveryRow], list[dict]]:
    rows: list[DeliveryRow] = []
    errors: list[dict] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))  # tolerate Excel's BOM

    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    if missing := REQUIRED_COLUMNS - headers:
        return [], [{"line": 1, "message": f"Missing column(s): {', '.join(sorted(missing))}"}]

    seen: set[tuple[int, date]] = set()
    for raw in reader:
        line = reader.line_num
        # extra unnamed cells land under a None key; ignore them
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if isinstance(k, str)}
        if not any(row.values()):
            continue  # skip blank lines
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append({"line": line, "message": f"Files are limited to {MAX_ROWS:,} rows"})
            break

        try:
            campaign_id = int(row["campaign_id"])
        except ValueError:
            errors.append({"line": line, "message": f"campaign_id '{row['campaign_id']}' isn't a number"})
            continue
        if campaign_id not in known_campaigns:
            errors.append({"line": line, "message": f"Campaign {campaign_id} doesn't exist"})
            continue
        try:
            day = date.fromisoformat(row["date"])
        except ValueError:
            errors.append({"line": line, "message": f"date '{row['date']}' should look like 2026-10-31"})
            continue
        try:
            impressions = int(row["impressions"].replace(",", ""))
            if impressions < 0:
                raise ValueError
        except ValueError:
            errors.append({"line": line, "message": f"impressions '{row['impressions']}' should be a whole number, 0 or more"})
            continue
        if (campaign_id, day) in seen:
            errors.append({"line": line, "message": f"Campaign {campaign_id} on {day} appears more than once"})
            continue

        seen.add((campaign_id, day))
        rows.append(DeliveryRow(campaign_id=campaign_id, day=day, impressions=impressions))

    if not rows and not errors:
        errors.append({"line": 1, "message": "The file has no data rows"})
    return rows, errors


def import_report(db: Session, filename: str, text: str, user: User) -> DeliveryReport:
    checksum = hashlib.sha256(text.encode()).hexdigest()
    existing = db.scalar(select(DeliveryReport).where(DeliveryReport.checksum == checksum))
    if existing:
        raise HTTPException(
            409, f"This file was already imported as report #{existing.id} ({existing.filename})"
        )

    known = set(db.scalars(select(Campaign.id)).all())
    rows, errors = parse(text, known)
    if errors:
        raise HTTPException(
            422,
            {
                "message": f"Nothing was imported. Fix {len(errors)} problem(s) and upload again.",
                "errors": errors[:50],
            },
        )

    report = DeliveryReport(filename=filename, checksum=checksum, row_count=len(rows), uploaded_by=user.username)
    report.rows = rows
    db.add(report)
    db.flush()
    audit.record(
        db,
        entity_type="delivery_report",
        entity_id=report.id,
        invoice_id=None,
        action="import",
        actor=user.username,
        before=None,
        after={"filename": filename, "rows": len(rows), "impressions": sum(r.impressions for r in rows)},
    )
    db.commit()
    db.refresh(report)
    return report


def reported_impressions(db: Session) -> dict[int, int]:
    """Total delivered impressions per campaign, using the latest report for each day."""
    latest: dict[tuple[int, date], int] = {}
    for campaign_id, day, impressions in db.execute(
        select(DeliveryRow.campaign_id, DeliveryRow.day, DeliveryRow.impressions).order_by(DeliveryRow.report_id)
    ):
        latest[(campaign_id, day)] = impressions
    totals: dict[int, int] = {}
    for (campaign_id, _), impressions in latest.items():
        totals[campaign_id] = totals.get(campaign_id, 0) + impressions
    return totals
