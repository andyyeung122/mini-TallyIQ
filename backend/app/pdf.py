"""Customer-facing invoice PDF (ReportLab).

Anything that isn't posted gets a DRAFT watermark, so a preview can never be
mistaken for (or sent as) a real invoice.
"""
import io
import os
from datetime import date
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SELLER_NAME = os.getenv("SELLER_NAME", "Sample Media Co.")
SELLER_ADDRESS = os.getenv("SELLER_ADDRESS", "100 Example Street, New York, NY 10007")
REMIT_TEXT = os.getenv(
    "REMIT_TEXT", "Pay by ACH or check. Include the invoice number with your payment."
)

INK = colors.HexColor("#1c2430")
MUTED = colors.HexColor("#6b7686")
LINE = colors.HexColor("#d5dbe3")

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=8.5, leading=11, textColor=MUTED)
TITLE = ParagraphStyle("title", parent=BASE, fontName="Helvetica-Bold", fontSize=20, leading=24, alignment=TA_RIGHT)
BOLD = ParagraphStyle("bold", parent=BASE, fontName="Helvetica-Bold")


def local_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def invoice_number(invoice_id: int) -> str:
    return f"INV-{invoice_id:06d}"


def render_invoice_pdf(inv, customer, *, credits_cents: int, paid_cents: int) -> bytes:
    """`inv` is an Invoice with lines loaded; `customer` its Customer."""
    is_final = inv.status == "posted"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"{invoice_number(inv.id)} {customer.name}",
        author=SELLER_NAME,
    )
    width = doc.width
    invoice_date = local_date(inv.posted_at) if inv.posted_at else date.today()

    header = Table(
        [
            [
                Paragraph(f"<b>{escape(SELLER_NAME)}</b><br/>{escape(SELLER_ADDRESS)}", BASE),
                Paragraph("Invoice" if is_final else "Invoice preview", TITLE),
            ]
        ],
        colWidths=[width * 0.55, width * 0.45],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))

    meta_rows = [
        ["Invoice number", invoice_number(inv.id)],
        ["Invoice date", invoice_date.strftime("%B %d, %Y") if is_final else "Not posted yet"],
        ["Terms", f"Net {customer.payment_terms_days}"],
        ["Due date", inv.due_on.strftime("%B %d, %Y") if inv.due_on else "Set when posted"],
    ]
    meta = Table(
        [[Paragraph("<b>Bill to</b>", BASE), "", ""]]
        + [
            [Paragraph(escape(customer.name if i == 0 else (customer.email if i == 1 else "")), BASE), k, v]
            for i, (k, v) in enumerate(meta_rows)
        ],
        colWidths=[width * 0.5, width * 0.22, width * 0.28],
    )
    meta.setStyle(
        TableStyle(
            [
                ("FONT", (1, 0), (-1, -1), "Helvetica", 9.5),
                ("TEXTCOLOR", (1, 0), (1, -1), MUTED),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    rows = [["Description", "Impressions", "CPM", "Amount"]]
    for line in sorted(inv.lines, key=lambda l: l.id):
        rows.append([Paragraph(escape(line.description), BASE), f"{line.impressions:,}", usd(line.cpm_cents), usd(line.amount_cents)])
    n_lines = len(rows)
    rows.append(["", "", "Subtotal", usd(inv.total_cents)])
    if credits_cents:
        rows.append(["", "", "Credits", usd(-credits_cents)])
    if paid_cents:
        rows.append(["", "", "Payments received", usd(-paid_cents)])
    rows.append(["", "", "Balance due", usd(inv.total_cents - credits_cents - paid_cents)])

    lines = Table(rows, colWidths=[width * 0.46, width * 0.18, width * 0.16, width * 0.20], repeatRows=1)
    lines.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                ("FONT", (1, 1), (-1, -1), "Helvetica", 9.5),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, INK),
                ("LINEBELOW", (0, 1), (-1, n_lines - 1), 0.5, LINE),
                ("TEXTCOLOR", (2, n_lines), (2, -1), MUTED),
                ("TEXTCOLOR", (2, -1), (2, -1), INK),
                ("LINEABOVE", (2, -1), (-1, -1), 1, INK),
                ("FONT", (2, -1), (-1, -1), "Helvetica-Bold", 11),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    story = [
        header,
        Spacer(1, 0.35 * inch),
        meta,
        Spacer(1, 0.35 * inch),
        lines,
        Spacer(1, 0.4 * inch),
        Paragraph("<b>How to pay</b>", BASE),
        Paragraph(escape(REMIT_TEXT), SMALL),
    ]

    def watermark(canvas, _doc):
        if is_final:
            return
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 96)
        canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.12))
        canvas.translate(LETTER[0] / 2, LETTER[1] / 2)
        canvas.rotate(35)
        canvas.drawCentredString(0, -30, "DRAFT")
        canvas.restoreState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"Draft preview, status: {inv.status.replace('_', ' ')}. Not a valid invoice.")

    doc.build(story, onFirstPage=watermark, onLaterPages=watermark)
    return buf.getvalue()
