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
