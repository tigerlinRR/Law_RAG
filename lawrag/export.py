"""Export batch due-diligence reviews to Excel (comparison matrix) or Word (memo).

Excel is the classic DD "summary chart" — one row per contract, one column per
checklist clause, plus a risk count and a separate Risks sheet. Word is the
narrative memo form — full per-contract review with clause tables and risk lists.

Input `reviews` is a list of the dicts returned by summarize.review_contract().
"""
from __future__ import annotations

import io

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .summarize import CHECKLIST

_HDR_FILL = PatternFill("solid", fgColor="1A2238")
_HDR_FONT = Font(bold=True, color="FFFFFF")


def _clause_map(review: dict) -> dict:
    return {c.get("name", ""): c.get("value", "") for c in review.get("clauses", [])}


def to_excel(reviews: list[dict]) -> bytes:
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    headers = ["File", "Type", "Parties"] + CHECKLIST + ["# Risks"]
    ws.append(headers)
    for review in reviews:
        cmap = _clause_map(review)
        row = [review.get("_source", ""), review.get("doc_type", ""),
               "; ".join(review.get("parties", []))]
        row += [cmap.get(clause, "") for clause in CHECKLIST]
        row.append(len(review.get("key_risks", [])))
        ws.append(row)

    risks = wb.create_sheet("Risks")
    risks.append(["File", "Risk"])
    for review in reviews:
        for r in review.get("key_risks", []):
            risks.append([review.get("_source", ""), r])

    for sheet in (ws, risks):
        for col, name in enumerate(sheet[1], start=1):
            name.fill = _HDR_FILL
            name.font = _HDR_FONT
            name.alignment = Alignment(vertical="top", wrap_text=True)
            width = 16 if col > 1 else 28
            sheet.column_dimensions[get_column_letter(col)].width = width
        sheet.freeze_panes = "A2"
        for r in sheet.iter_rows(min_row=2):
            for cell in r:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_word(reviews: list[dict]) -> bytes:
    doc = docx.Document()
    doc.add_heading("Due Diligence Report", 0)
    intro = doc.add_paragraph(
        f"{len(reviews)} document(s) reviewed. AI-assisted summary — verify each "
        "finding against the source document before relying on it.")
    intro.alignment = WD_ALIGN_PARAGRAPH.LEFT

    for i, review in enumerate(reviews):
        title = review.get("_source", "Contract")
        if review.get("doc_type"):
            title += f"  ({review['doc_type']})"
        doc.add_heading(title, level=1)

        if review.get("summary"):
            doc.add_paragraph(review["summary"])
        if review.get("parties"):
            doc.add_paragraph("Parties: " + "; ".join(review["parties"]))

        clauses = review.get("clauses", [])
        if clauses:
            table = doc.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            hdr = table.rows[0].cells
            hdr[0].text, hdr[1].text, hdr[2].text = "Clause", "Value", "Source quote"
            for c in clauses:
                cells = table.add_row().cells
                cells[0].text = c.get("name", "")
                cells[1].text = c.get("value", "")
                cells[2].text = c.get("quote", "")

        risks = review.get("key_risks", [])
        if risks:
            doc.add_heading("Key risks to review", level=2)
            for r in risks:
                doc.add_paragraph(r, style="List Bullet")

        if i < len(reviews) - 1:
            doc.add_page_break()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------- 8-K draft export (experimental) ----------

def draft_to_word(draft: dict) -> bytes:
    doc = docx.Document()
    doc.add_heading(f"Item {draft.get('item', '')} — {draft.get('item_title', '')}", 0)
    doc.add_paragraph(
        f"Source contract: {draft.get('_source_contract', '—')}\n"
        "Experimental AI-drafted disclosure — verify every fact below against the "
        "source contract before relying on it. This is not a finished filing."
    )

    doc.add_heading("Disclosure (draft)", level=1)
    for para in (draft.get("disclosure") or "").split("\n\n"):
        if para.strip():
            doc.add_paragraph(para.strip())

    precedents = draft.get("_precedents_used") or []
    if precedents:
        doc.add_heading("Precedents used (style reference only)", level=1)
        for p in precedents:
            doc.add_paragraph(p, style="List Bullet")

    facts = draft.get("facts_used") or []
    if facts:
        doc.add_heading("Fact -> source trace", level=1)
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text = "Fact", "Source quote", "Verified"
        for f in facts:
            cells = table.add_row().cells
            cells[0].text = f.get("fact", "")
            cells[1].text = f.get("source_quote", "")
            cells[2].text = "Yes" if f.get("verified") else "⚠ UNVERIFIED"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _draft_html(draft: dict) -> str:
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    facts_rows = "".join(
        f"<tr{' class=\"unverified\"' if not f.get('verified') else ''}>"
        f"<td>{esc(f.get('fact',''))}</td><td>{esc(f.get('source_quote',''))}</td>"
        f"<td>{'Yes' if f.get('verified') else '⚠ UNVERIFIED'}</td></tr>"
        for f in draft.get("facts_used") or []
    )
    precedents = "".join(f"<li>{esc(p)}</li>" for p in draft.get("_precedents_used") or [])
    disclosure_html = "".join(
        f"<p>{esc(para.strip())}</p>" for para in (draft.get("disclosure") or "").split("\n\n")
        if para.strip()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: Georgia, 'Times New Roman', serif; color: #1c2430; padding: 40px; }}
      h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
      p {{ line-height: 1.5; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 10px; }}
      th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
      th {{ background: #1a2238; color: #fff; }}
      tr.unverified td {{ color: #b4232a; font-weight: 600; }}
      .note {{ color: #667; font-size: 12px; }}
    </style></head><body>
      <h1>Item {esc(draft.get('item',''))} — {esc(draft.get('item_title',''))}</h1>
      <p class="note">Source contract: {esc(draft.get('_source_contract','—'))}<br>
      Experimental AI-drafted disclosure — verify every fact below against the source
      contract before relying on it. This is not a finished filing.</p>
      <h2>Disclosure (draft)</h2>
      {disclosure_html}
      {"<h2>Precedents used (style reference only)</h2><ul>" + precedents + "</ul>" if precedents else ""}
      {"<h2>Fact -&gt; source trace</h2><table><thead><tr><th>Fact</th><th>Source quote</th><th>Verified</th></tr></thead><tbody>" + facts_rows + "</tbody></table>" if facts_rows else ""}
    </body></html>"""


def draft_to_pdf(draft: dict) -> bytes:
    """Renders the draft to PDF via a local, offscreen Chromium (Playwright) —
    no network access, same engine already used to convert reference filings."""
    from playwright.sync_api import sync_playwright

    html = _draft_html(draft)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        pdf_bytes = page.pdf(format="Letter", margin={"top": "0.6in", "bottom": "0.6in",
                                                        "left": "0.6in", "right": "0.6in"})
        browser.close()
    return pdf_bytes
