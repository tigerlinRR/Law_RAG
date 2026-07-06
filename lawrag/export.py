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
#
# Mirrors the actual SEC Form 8-K structure (cover page -> Item disclosure ->
# Item 9.01 exhibit index -> signature) rather than a generic report layout, so
# a lawyer sees something recognizable as a filing draft. Cover-page fields
# below (registrant/EIN/address/securities) are Richtech-specific -- this tool
# currently drafts for one registrant; parameterize per-client if that changes.
# Our own transparency material (precedents, fact trace) is kept as a clearly
# separate appendix, never mixed into the filing-shaped section.

REGISTRANT = {
    "name": "Richtech Robotics Inc.",
    "state": "Nevada",
    "file_number": "001-41866",
    "irs_ein": "88-2870106",
    "address": ["2975 Lincoln Rd,", "Las Vegas, NV 89115"],
    "phone": "(866) 236-3835",
    "securities": [("Class B Common Stock, par value $0.0001 per share", "RR",
                     "The Nasdaq Stock Market LLC")],
    "emerging_growth_company": True,
    "signer_name": "Zhenwu (Wayne) Huang",
    "signer_title": "Chief Executive Officer and Director",
}


def _report_date(draft: dict) -> str:
    """The 8-K 'date of earliest event reported' — the transaction date, which
    is almost always the first date stated in the disclosure ('On <date>, ...
    entered into ...'). Fall back to any date in the cited facts."""
    import re
    m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", draft.get("disclosure", ""))
    if m:
        return m.group(1)
    for f in draft.get("facts_used") or []:
        m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", f.get("fact", ""))
        if m:
            return m.group(1)
    return "[DATE]"


def _disclosure_paragraphs(draft: dict) -> list[str]:
    """Split the disclosure into paragraphs, dropping a leading 'Item X.XX...'
    line if the model repeated the heading we already render separately."""
    paras = [p.strip() for p in (draft.get("disclosure") or "").split("\n\n") if p.strip()]
    item = str(draft.get("item", ""))
    if paras and item and paras[0].lower().startswith(f"item {item}".lower()):
        paras = paras[1:]
    return paras


def draft_to_word(draft: dict) -> bytes:
    r = REGISTRANT
    date = _report_date(draft)
    doc = docx.Document()

    def centered(text: str, bold=False, size=None):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.bold = bold
        if size:
            run.font.size = docx.shared.Pt(size)
        return p

    warn = doc.add_paragraph()
    warn.alignment = WD_ALIGN_PARAGRAPH.CENTER
    wr = warn.add_run("DRAFT — FOR INTERNAL REVIEW ONLY — NOT FILED WITH THE SEC")
    wr.bold = True
    doc.add_paragraph()

    centered("UNITED STATES", bold=True)
    centered("SECURITIES AND EXCHANGE COMMISSION", bold=True)
    centered("Washington, D.C. 20549", bold=True)
    doc.add_paragraph()
    centered("FORM 8-K", bold=True, size=14)
    doc.add_paragraph()
    centered("CURRENT REPORT", bold=True)
    centered("Pursuant to Section 13 or 15(d) of the", bold=True)
    centered("Securities Exchange Act of 1934", bold=True)
    doc.add_paragraph()
    centered(f"Date of Report (Date of earliest event reported): {date}")
    doc.add_paragraph()
    centered(r["name"], bold=True)
    centered("(Exact name of registrant as specified in its charter)")
    doc.add_paragraph()

    t = doc.add_table(rows=2, cols=3)
    t.style = "Table Grid"
    for i, v in enumerate([r["state"], r["file_number"], r["irs_ein"]]):
        t.rows[0].cells[i].paragraphs[0].add_run(v).bold = True
        t.rows[0].cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for i, v in enumerate(["(State or other jurisdiction of incorporation)",
                            "(Commission File Number)", "(IRS Employer Identification No.)"]):
        p = t.rows[1].cells[i].paragraphs[0]
        p.add_run(v).font.size = docx.shared.Pt(9)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph()

    for line in r["address"]:
        centered(line, bold=True)
    centered("(Address of principal executive offices, including zip code)")
    doc.add_paragraph()
    centered(f"Registrant's telephone number, including area code: {r['phone']}")
    doc.add_paragraph()
    centered("Not Applicable")
    centered("(Former name or former address, if changed since last report)")
    doc.add_paragraph()

    doc.add_paragraph(
        "Check the appropriate box below if the Form 8-K filing is intended to "
        "simultaneously satisfy the filing obligation of the registrant under any of "
        "the following provisions:")
    doc.add_paragraph("☐  Written communications pursuant to Rule 425 under the Securities Act (17 CFR 230.425)")
    doc.add_paragraph("☐  Soliciting material pursuant to Rule 14a-12 under the Exchange Act (17 CFR 240.14a-12)")
    doc.add_paragraph("☐  Pre-commencement communications pursuant to Rule 14d-2(b) under the Exchange Act (17 CFR 240.14d-2(b))")
    doc.add_paragraph("☐  Pre-commencement communications pursuant to Rule 13e-4(c) under the Exchange Act (17 CFR 240.13e-4(c))")
    doc.add_paragraph()

    centered("Securities registered pursuant to Section 12(b) of the Act:")
    t2 = doc.add_table(rows=1 + len(r["securities"]), cols=3)
    t2.style = "Table Grid"
    for i, h in enumerate(["Title of each class", "Trading Symbol(s)", "Name of each exchange on which registered"]):
        t2.rows[0].cells[i].paragraphs[0].add_run(h).bold = True
    for row_i, (cls, sym, exch) in enumerate(r["securities"], start=1):
        vals = (cls, sym, exch)
        for i, v in enumerate(vals):
            t2.rows[row_i].cells[i].text = v
    doc.add_paragraph()

    egc = "☒" if r["emerging_growth_company"] else "☐"
    doc.add_paragraph(
        "Indicate by check mark whether the registrant is an emerging growth company as "
        "defined in Rule 405 of the Securities Act of 1933 (§230.405 of this chapter) or "
        "Rule 12b-2 of the Securities Exchange Act of 1934 (§240.12b-2 of this chapter).")
    doc.add_paragraph(f"Emerging growth company {egc}")
    doc.add_paragraph(
        "If an emerging growth company, indicate by check mark if the registrant has "
        "elected not to use the extended transition period for complying with any new or "
        "revised financial accounting standards provided pursuant to Section 13(a) of the "
        "Exchange Act. ☐")

    doc.add_page_break()

    hdr = doc.add_paragraph()
    hdr.add_run(f"Item {draft.get('item','')}. {draft.get('item_title','')}").bold = True
    for para in _disclosure_paragraphs(draft):
        p = doc.add_paragraph(para)
        p.paragraph_format.first_line_indent = docx.shared.Inches(0.4)

    doc.add_paragraph().add_run("Item 9.01. Financial Statements and Exhibits.").bold = True
    doc.add_paragraph("(d) Exhibits")
    t3 = doc.add_table(rows=3, cols=2)
    t3.style = "Table Grid"
    t3.rows[0].cells[0].paragraphs[0].add_run("Exhibit").bold = True
    t3.rows[0].cells[1].paragraphs[0].add_run("Description").bold = True
    t3.rows[1].cells[0].text = "10.1"
    t3.rows[1].cells[1].text = f"{draft.get('_doc_type') or 'Agreement'}, dated {date}"
    t3.rows[2].cells[0].text = "104"
    t3.rows[2].cells[1].text = "Cover Page Interactive Data File (embedded within the Inline XBRL documents)"

    doc.add_page_break()
    centered("SIGNATURE", bold=True)
    doc.add_paragraph(
        "Pursuant to the requirements of the Securities Exchange Act of 1934, the "
        "registrant has duly caused this report to be signed on its behalf by the "
        "undersigned hereunto duly authorized.")
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(r["name"]).bold = True
    doc.add_paragraph()
    p = doc.add_paragraph(f"By: /s/ {r['signer_name']}  [DRAFT — NOT YET SIGNED]")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Name: {r['signer_name']}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Title: {r['signer_title']}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph(f"Dated: {date}")

    # NOTE: the filing document ends here — no review/QC material is mixed in, so
    # this file is the clean 8-K ready for counsel to finalize. The review pack
    # (precedents, fact trace, SEC checks, full extraction) is a SEPARATE document,
    # produced by review_to_word() / review_to_pdf().
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def review_to_word(draft: dict) -> bytes:
    """The review pack — a SEPARATE document (never mixed into the filing) that
    lets counsel check the draft: SEC-requirement checks, precedents used, the
    fact -> source-quote trace, and the full set of extracted contract terms."""
    doc = docx.Document()
    doc.add_heading(f"8-K Draft — Review Materials (Item {draft.get('item', '')})", 0)
    doc.add_paragraph(
        f"Companion to the drafted 8-K for source contract "
        f"{draft.get('_source_contract', '—')}. NOT part of the filing — for "
        "internal review only. Verify every fact below against the source contract.")

    compliance = draft.get("_compliance") or []
    if compliance:
        doc.add_heading("SEC requirement checks", level=1)
        for c in compliance:
            mark = "✓" if c.get("satisfied") else "✗ MISSING"
            doc.add_paragraph(f"{mark}  {c.get('requirement', '')}", style="List Bullet")

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
        fhdr = table.rows[0].cells
        fhdr[0].text, fhdr[1].text, fhdr[2].text = "Fact", "Source quote", "Verified"
        for f in facts:
            cells = table.add_row().cells
            cells[0].text = f.get("fact", "")
            cells[1].text = f.get("source_quote", "")
            cells[2].text = "Yes" if f.get("verified") else "⚠ UNVERIFIED"

    all_terms = draft.get("_all_extracted_terms") or []
    if all_terms:
        doc.add_heading("All terms extracted from the contract", level=1)
        doc.add_paragraph(
            "The disclosure states only the material terms, following 8-K convention. "
            "This is the full set the review engine extracted — use it to confirm "
            "nothing material was left out of the disclosure.")
        table = doc.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        thdr = table.rows[0].cells
        thdr[0].text, thdr[1].text = "Term", "Value"
        for t in all_terms:
            cells = table.add_row().cells
            cells[0].text = t.get("name", "")
            cells[1].text = t.get("value", "")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _draft_html(draft: dict) -> str:
    """The clean filing document — cover page, Item disclosure, Item 9.01, and
    signature. NO review/QC material (that lives in _review_html)."""
    esc = _esc
    r = REGISTRANT
    date = _report_date(draft)

    disclosure_html = "".join(f"<p>{esc(para)}</p>" for para in _disclosure_paragraphs(draft))
    sec_rows = "".join(
        f"<tr><td>{esc(cls)}</td><td style='text-align:center'>{esc(sym)}</td>"
        f"<td style='text-align:center'>{esc(exch)}</td></tr>"
        for cls, sym, exch in r["securities"]
    )
    egc = "&#9746;" if r["emerging_growth_company"] else "&#9744;"  # ☒ / ☐

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: 'Times New Roman', Georgia, serif; color: #000; padding: 50px 60px;
              font-size: 13px; line-height: 1.45; }}
      .draft-banner {{ text-align: center; font-weight: 700; font-size: 13px; margin-bottom: 24px; }}
      .center {{ text-align: center; }}
      .bold {{ font-weight: 700; }}
      .small {{ font-size: 11px; }}
      .cover p {{ margin: 4px 0; }}
      .cover table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
      .cover table td {{ text-align: center; padding: 4px; vertical-align: top; }}
      hr {{ border: none; border-top: 2px solid #000; margin: 6px 0 18px; }}
      .pagebreak {{ page-break-before: always; }}
      .filing p {{ text-indent: 0.4in; margin: 0 0 12px; }}
      .filing h2 {{ font-size: 13px; margin: 22px 0 4px; }}
      .sig-block {{ width: 60%; margin-left: auto; margin-top: 30px; }}
      .sig-block p {{ margin: 4px 0; text-indent: 0; }}
      table.exhibits, table.review {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 12px; }}
      table.exhibits td, table.exhibits th, table.review td, table.review th {{
        border: 1px solid #999; padding: 5px 8px; text-align: left; vertical-align: top; }}
      table.review th {{ background: #1a2238; color: #fff; }}
      tr.unverified td {{ color: #b4232a; font-weight: 600; }}
      .appendix-note {{ color: #555; font-size: 12px; }}
    </style></head><body>

      <div class="draft-banner">DRAFT &mdash; FOR INTERNAL REVIEW ONLY &mdash; NOT FILED WITH THE SEC</div>
      <hr>
      <div class="cover center">
        <p class="bold">UNITED STATES</p>
        <p class="bold">SECURITIES AND EXCHANGE COMMISSION</p>
        <p class="bold">Washington, D.C. 20549</p>
        <p class="bold" style="font-size:16px; margin-top:20px;">FORM 8-K</p>
        <p class="bold" style="margin-top:20px;">CURRENT REPORT</p>
        <p class="bold">Pursuant to Section 13 or 15(d) of the<br>Securities Exchange Act of 1934</p>
        <p style="margin-top:16px;">Date of Report (Date of earliest event reported): {esc(date)}</p>
        <p class="bold" style="margin-top:16px;">{esc(r['name'])}</p>
        <p class="small">(Exact name of registrant as specified in its charter)</p>
        <table>
          <tr>
            <td class="bold">{esc(r['state'])}</td>
            <td class="bold">{esc(r['file_number'])}</td>
            <td class="bold">{esc(r['irs_ein'])}</td>
          </tr>
          <tr>
            <td class="small">(State or other jurisdiction<br>of incorporation)</td>
            <td class="small">(Commission File Number)</td>
            <td class="small">(IRS Employer<br>Identification No.)</td>
          </tr>
        </table>
        <p class="bold">{"<br>".join(esc(l) for l in r['address'])}</p>
        <p class="small">(Address of principal executive offices, including zip code)</p>
        <p style="margin-top:12px;">Registrant's telephone number, including area code: {esc(r['phone'])}</p>
        <p class="bold" style="margin-top:12px;">Not Applicable</p>
        <p class="small">(Former name or former address, if changed since last report)</p>
      </div>

      <p style="margin-top:20px; text-indent:0.4in;">Check the appropriate box below if the Form 8-K filing is
      intended to simultaneously satisfy the filing obligation of the registrant under any of the following
      provisions:</p>
      <p>&#9744;&nbsp; Written communications pursuant to Rule 425 under the Securities Act (17 CFR 230.425)</p>
      <p>&#9744;&nbsp; Soliciting material pursuant to Rule 14a-12 under the Exchange Act (17 CFR 240.14a-12)</p>
      <p>&#9744;&nbsp; Pre-commencement communications pursuant to Rule 14d-2(b) under the Exchange Act (17 CFR 240.14d-2(b))</p>
      <p>&#9744;&nbsp; Pre-commencement communications pursuant to Rule 13e-4(c) under the Exchange Act (17 CFR 240.13e-4(c))</p>

      <p class="center" style="margin-top:16px;">Securities registered pursuant to Section 12(b) of the Act:</p>
      <table class="exhibits">
        <thead><tr><th>Title of each class</th><th>Trading Symbol(s)</th>
        <th>Name of each exchange on which registered</th></tr></thead>
        <tbody>{sec_rows}</tbody>
      </table>

      <p style="margin-top:16px;">Indicate by check mark whether the registrant is an emerging growth company as
      defined in Rule 405 of the Securities Act of 1933 (&sect;230.405 of this chapter) or Rule 12b-2 of the
      Securities Exchange Act of 1934 (&sect;240.12b-2 of this chapter).</p>
      <p>Emerging growth company {egc}</p>
      <p>If an emerging growth company, indicate by check mark if the registrant has elected not to use the
      extended transition period for complying with any new or revised financial accounting standards provided
      pursuant to Section 13(a) of the Exchange Act. &#9744;</p>

      <div class="pagebreak filing">
        <h2>Item {esc(draft.get('item',''))}. {esc(draft.get('item_title',''))}.</h2>
        {disclosure_html}
        <h2>Item 9.01. Financial Statements and Exhibits.</h2>
        <p style="text-indent:0;">(d) Exhibits</p>
        <table class="exhibits">
          <thead><tr><th>Exhibit</th><th>Description</th></tr></thead>
          <tbody>
            <tr><td>10.1</td><td>{esc(draft.get('_doc_type') or 'Agreement')}, dated {esc(date)}</td></tr>
            <tr><td>104</td><td>Cover Page Interactive Data File (embedded within the Inline XBRL documents)</td></tr>
          </tbody>
        </table>
      </div>

      <div class="pagebreak">
        <p class="center bold">SIGNATURE</p>
        <p style="text-indent:0.4in;">Pursuant to the requirements of the Securities Exchange Act of 1934, the
        registrant has duly caused this report to be signed on its behalf by the undersigned hereunto duly
        authorized.</p>
        <div class="sig-block">
          <p class="bold">{esc(r['name'])}</p>
          <p style="margin-top:20px;">By: /s/ {esc(r['signer_name'])} &nbsp; <i>[DRAFT — NOT YET SIGNED]</i></p>
          <p>Name: {esc(r['signer_name'])}</p>
          <p>Title: {esc(r['signer_title'])}</p>
        </div>
        <p style="margin-top:20px;">Dated: {esc(date)}</p>
      </div>
    </body></html>"""


def _review_html(draft: dict) -> str:
    """The review pack as a SEPARATE document — SEC checks, precedents, fact
    trace, full extraction. Never mixed into the filing."""
    esc = _esc
    facts_rows = "".join(
        f"<tr{' class=\"unverified\"' if not f.get('verified') else ''}>"
        f"<td>{esc(f.get('fact',''))}</td><td>{esc(f.get('source_quote',''))}</td>"
        f"<td>{'Yes' if f.get('verified') else '⚠ UNVERIFIED'}</td></tr>"
        for f in draft.get("facts_used") or []
    )
    precedents = "".join(f"<li>{esc(p)}</li>" for p in draft.get("_precedents_used") or [])
    all_terms_rows = "".join(
        f"<tr><td>{esc(t.get('name',''))}</td><td>{esc(t.get('value',''))}</td></tr>"
        for t in draft.get("_all_extracted_terms") or []
    )
    compliance_rows = "".join(
        f"<tr><td>{'✓' if c.get('satisfied') else '✗ MISSING'}</td>"
        f"<td>{esc(c.get('requirement',''))}</td></tr>"
        for c in draft.get("_compliance") or []
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: 'Times New Roman', Georgia, serif; color: #000; padding: 50px 60px;
              font-size: 13px; line-height: 1.45; }}
      h1 {{ font-size: 18px; }} h2 {{ font-size: 14px; margin-top: 22px; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 12px; }}
      td, th {{ border: 1px solid #999; padding: 5px 8px; text-align: left; vertical-align: top; }}
      th {{ background: #1a2238; color: #fff; }}
      tr.unverified td {{ color: #b4232a; font-weight: 600; }}
      .note {{ color: #555; font-size: 12px; }}
    </style></head><body>
      <h1>8-K Draft — Review Materials (Item {esc(draft.get('item',''))})</h1>
      <p class="note">Companion to the drafted 8-K for source contract
      {esc(draft.get('_source_contract','—'))}. NOT part of the filing — for internal
      review only. Verify every fact below against the source contract.</p>
      {"<h2>SEC requirement checks</h2><table><thead><tr><th>Status</th><th>Requirement</th></tr></thead><tbody>" + compliance_rows + "</tbody></table>" if compliance_rows else ""}
      {"<h2>Precedents used (style reference only)</h2><ul>" + precedents + "</ul>" if precedents else ""}
      {"<h2>Fact -&gt; source trace</h2><table><thead><tr><th>Fact</th><th>Source quote</th><th>Verified</th></tr></thead><tbody>" + facts_rows + "</tbody></table>" if facts_rows else ""}
      {"<h2>All terms extracted from the contract</h2><p class='note'>The disclosure states only the material terms, per 8-K convention. This is the full set the review engine extracted &mdash; use it to confirm nothing material was left out.</p><table><thead><tr><th>Term</th><th>Value</th></tr></thead><tbody>" + all_terms_rows + "</tbody></table>" if all_terms_rows else ""}
    </body></html>"""


def _render_pdf(html: str) -> bytes:
    """Render HTML to PDF via a local, offscreen Chromium (Playwright) — no
    network access, same engine used to convert reference filings."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        pdf_bytes = page.pdf(format="Letter", margin={"top": "0.6in", "bottom": "0.6in",
                                                        "left": "0.6in", "right": "0.6in"})
        browser.close()
    return pdf_bytes


def draft_to_pdf(draft: dict) -> bytes:
    """The clean filing PDF (no review material)."""
    return _render_pdf(_draft_html(draft))


def review_to_pdf(draft: dict) -> bytes:
    """The review-pack PDF (separate from the filing)."""
    return _render_pdf(_review_html(draft))
