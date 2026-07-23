# Law_RAG — Local Legal Document Knowledge Base

A **fully local, private** document knowledge base for a law firm. No data leaves
the machine — all parsing, embedding, and search run on this Jetson AGX Thor.

**Phase 1 (this repo): find & retrieve.** Ingest PDF/Word documents with firm
metadata, then search them with hybrid retrieval (semantic + keyword) and get
**source citations**. No AI drafting — by design, matching the lawyers' comfort level.

## Architecture

```
Host venv (lightweight, no torch)             Docker (reuses the box's CUDA stack)
  parse PDF/Word (pymupdf, python-docx)         lawrag-db      Postgres + pgvector    :5434
  chunk + metadata                     ──HTTP──▶ lawrag-embed   vLLM Qwen3-Embedding   :8010
  hybrid search (vector + keyword, RRF)         lawrag-rerank  vLLM bge-reranker-v2-m3 :8011
  cross-encoder rerank -> citations             lawrag-llm     vLLM Qwen3.6-35B 32k   :8012
  due-diligence review (summarize + extract)
```

- **Storage:** one Postgres+pgvector DB holds vectors, keyword index (tsvector),
  and metadata — semantic search, keyword search, and filtering in a single system.
- **Retrieval (two stages):** (1) Reciprocal Rank Fusion of vector similarity +
  full-text search produces a candidate pool; (2) a cross-encoder reranker re-scores
  each (query, passage) pair for precise final ordering. Reranker auto-falls-back to
  RRF order if unavailable; toggle with `RERANK_ENABLED` or `query.py --no-rerank`.
- **Isolation:** `client` / `matter` metadata filters are the basis for ethical-wall
  access control (a user's permitted scope becomes a mandatory filter).

## Setup

Containers (already created; to (re)start):
```bash
sudo docker start lawrag-db lawrag-embed
```

Python env:
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export PYTHONPATH=$PWD
./.venv/bin/python -m playwright install chromium   # one-time: needed for PDF export of 8-K drafts
```

## Usage

```bash
# Initialize schema (idempotent)
./.venv/bin/python scripts/init_db.py

# Create the first admin user, then scoped lawyers (see Access control below)
./.venv/bin/python scripts/user_admin.py add admin --password '<pick-one>' --role admin
./.venv/bin/python scripts/user_admin.py add jchen --password '<pw>' --clients Richtech "Acme Corp"

# Restart all services after a reboot
sudo docker start lawrag-db lawrag-embed lawrag-rerank lawrag-llm

# Ingest a file or a whole folder, with metadata applied to the batch
./.venv/bin/python scripts/ingest.py /path/to/docs \
    --client Richtech --doc-type S-8 --author "Jane Partner" --doc-date 2021-06-15

# Search (hybrid), optionally filtered
./.venv/bin/python scripts/query.py "employee stock incentive plan" --client Richtech
./.venv/bin/python scripts/query.py "registration statement" --doc-type S-8 -k 5

# Due-diligence review of a contract: summary + key clauses (with quotes) + risks
./.venv/bin/python scripts/summarize.py /path/to/contract.pdf
./.venv/bin/python scripts/summarize.py /path/to/contract.docx --json   # machine-readable

# Batch DD over a whole folder -> Excel comparison matrix + Word memo
./.venv/bin/python scripts/dd_batch.py /path/to/data_room --excel dd.xlsx --word dd.docx

# Experimental: draft an 8-K Item disclosure from a contract (see "8-K drafting" below)
./.venv/bin/python scripts/draft_8k.py /path/to/contract.docx --item 1.01

# Generate synthetic sample docs for testing
./.venv/bin/python scripts/make_samples.py

# Web app — open http://localhost:8080 in a browser on this machine
./.venv/bin/python scripts/serve.py
# To reach it from other devices on your private tailnet (deliberate opt-in):
#   LAWRAG_HOST=0.0.0.0 ./.venv/bin/python scripts/serve.py
```

## Web interface

A local web UI (`web/`, served by `lawrag/api.py`), gated by login. Views:
- **Find Documents** — search box + client/type/attorney filters + AI-rerank toggle;
  results show the source file, type badge, metadata, relevance, and a snippet.
- **Library** — browse every document the user may see (scoped by client), with a
  live filter. **Click a filename to open the original PDF/Word** (downloads are
  access-scoped too). Admins can delete a document here.
- **Review a Contract** — drag-and-drop one or more PDF/Word files. One file →
  full report (summary, parties, key-clause table with verbatim quotes, risks).
  Several files → a comparison table plus per-file reports. Export the whole batch
  to **Excel** (clause matrix + risks sheet) or **Word** (memo).
- **Add to Library** — drag files in; type/parties/client/date auto-detected.
- **Generate 8-K** — pick an Item type and a client, drop in the triggering
  contract, and get a drafted Item disclosure (~1 min); it renders inline with
  its fact→source trace and is saved to History. Precedents and the saved record
  are scoped to the caller's clients.
- **History** — every AI-generated document (experimental 8-K drafts), scoped by
  client. Click one to see the full draft with its fact → source-quote trace and
  `⚠ UNVERIFIED` flags, and download it as **Word** or **PDF** — not just
  on-screen text. Timestamps are shown in the machine's local time.
- **Users** (admin only) — create users, set role (lawyer/admin), grant/revoke
  client access with checkboxes, reset passwords, delete users.

All views respect the caller's client scope. Binds to `127.0.0.1` by default (this
machine only) — the safe default for confidential documents. Nothing is sent off-device.

## Access control (ethical walls)

Every API call requires a login. Users have a role:
- **admin** — sees all clients; manage users in the web **Users** tab or via
  `scripts/user_admin.py`.
- **lawyer** — sees only the clients explicitly granted to them.

The client allowlist is enforced **server-side** on every search, on the stats/
filter lists, and on ingest — a lawyer can never retrieve another client's
documents, even when they are the most relevant match (conflict-of-interest wall).
All logins and searches are written to an `audit_log` table.

```bash
python scripts/user_admin.py add    jchen --password PW --clients Richtech "Acme Corp"
python scripts/user_admin.py grant  jchen "New Client"
python scripts/user_admin.py revoke jchen "Acme Corp"
python scripts/user_admin.py list
```

### Client-name normalization

So one real client isn't split across name variants, client names are canonicalized
at ingest. Trivial variants merge automatically (case/punctuation/legal suffixes:
"ACME CORP." == "Acme Corporation"). For variants a machine can't infer, an admin
declares the mapping — which also rewrites existing documents and grants:

```bash
python scripts/user_admin.py clients                                # list clients + doc counts
python scripts/user_admin.py merge "Richtech" "Richtech Robotics Inc."   # canonicalize
```

**Scope of this layer:** it is application-layer isolation. Production deployment
still needs transport security (TLS/HTTPS — today it is plain HTTP over localhost/
tailnet) and optionally SSO.

## 8-K drafting (experiment)

RAG-grounded drafting. **Facts always come from the source document(s) via extraction —
never from model memory.** The architecture is **SETTLED (v1-spine): the model
UNDERSTANDS/extracts, CODE generates, a deterministic guardrail backstops, and humans
fill the gaps.** Split of concerns: extraction = facts, code/prompt + rubric = structure
& tone, guardrail = the compliance red line.

- **Served model = the PLAIN BASE `Qwen3.6-35B` (Docker `lawrag-llm`, :8012).** An earlier
  fine-tuned *style* adapter (v2) and a delexicalized variant (v4/v5) were built, validated,
  and then **RETIRED** — fine-tuning fabricates figures (the exact compliance red line), and
  a base-vs-adapter A/B on a real deal found the base model *more* accurate on the
  accuracy-first spine (more facts extracted + verified, no `<think>` leak under guided
  JSON). See "Retired: fine-tuned adapter / delex" below for the full history. Per-customer
  house *style* is planned as facts-stripped few-shot from that customer's own filings, **not**
  fine-tuning.

1. **Extract facts** from the source document with the due-diligence engine (Item-specific
   clause checklist, verbatim quotes), followed by a **verify-gated repair pass**
   (`summarize._repair_extraction`): any clause whose quote failed verification, or that came
   back "Not found", gets a targeted second pass — a repair is accepted **only if the new
   quote verifies against the source**, so it improves fidelity and completeness while never
   inventing. Long documents are map-reduced with window overlap.
2. **Precedents are OFF by default** (`draft_8k` `n_precedents=0`). On the base model, in-prompt
   precedents caused fact leakage (the model copied their share counts / file numbers into the
   draft), so pure generation runs precedent-free — which also means it needs no DB/embed/rerank.
   Same-Item precedent retrieval (`documents.meta.filing_items`, a JSONB containment match) is
   still available with `n_precedents>0` for comparison.
3. **Draft — figures are hard-locked to the source. Three `draft_8k(mode=...)` modes:**
   - **`hybrid` (default):** the model drafts the disclosure in its 8-K style, then
     `draft._lock_figures` replaces **every figure not grounded in the source** (or a
     valid derivation) with a visible placeholder `[NOT IN SOURCE — CONFIRM]` — **no
     imagined NUMBER survives as a plausible value**; `_blanked_figures` lists them for
     the reviewer. Keeps fluent prose. *Caveat:* locking secures **numbers only** — a
     non-numeric fabrication (e.g. "registered direct offering", an S-3 file number) can
     still appear in the model's prose and needs human review.
   - **`assemble`:** the disclosure is built deterministically from the verified extracted
     clauses (+ derived share count) — **the model writes no prose at all, so nothing
     (number OR narrative) can be imagined.** Zero-invention; prose is templated and omits
     fields the extraction missed (errs toward omission). The max-safety option.
   - **`llm`:** raw LLM drafting, no locking (legacy / comparison).

   Every disclosed fact is cited back to its verbatim quote in `facts_used`; the standard
   "qualified in its entirety by reference to Exhibit 10.1" closing is guaranteed exactly
   once (de-dup tolerates the model dropping "its"). **Defined terms are kept consistent:**
   the registrant always reads as **"the Company"** — introduced once by its full legal name
   followed by `(the "Company")` and used throughout, never by its contract role (the model
   otherwise sometimes writes "the Purchaser"/"the Borrower"). A deterministic pass
   (`_normalize_registrant_term`) **anchored on the registrant's actual name** renames a role
   term the model bound to that name and inserts the `(the "Company")` definition if it was
   omitted — so the *counterparty's* own role term is never touched. (When several agreements
   merge into one Item, each gets a distinct term — "Purchase Agreement" vs "Registration Rights
   Agreement" — so the merged prose never defines "the Agreement" twice; and each later agreement's
   body is rewritten to connect to the first ("In connection with the Purchase Agreement, … the
   Company also entered into …") instead of re-introducing the registrant and re-defining shared
   terms, so every defined term appears exactly once.) Every citation (here and in the
   due-diligence engine's clause quotes) is checked programmatically against the source text
   (`summarize.verify_quote`) — a citation not found verbatim is flagged `⚠ UNVERIFIED` rather
   than silently trusted. The check is **ellipsis-aware**: a quote that elides non-contiguous
   language ("A … B") verifies iff every segment appears verbatim in the source in order, so a
   genuine elided quote isn't falsely flagged while a paraphrase/fabrication still fails.
   Required boilerplate assertions (the (c) material-relationship statement, the exhibit
   qualifier) are kept out of the fact→source trace — they aren't source-grounded facts (they're
   covered by the SEC-requirement checks), so they never show a misleading `⚠ UNVERIFIED`.
   - **Narrative-claim audit (review-only).** The numeric guardrail (step 4) locks fabricated
     *figures*, but a model can still invent a *non-numeric* claim or a spelled-out number
     ("terminate on **ten** business days' notice"). `draft._narrative_flags` runs an LLM audit
     of each substantive sentence against the grounded facts + source, then **independently
     re-checks each flagged claim in isolation and drops any it finds supported** (a batch "find
     the unsupported" pass over-flags; the isolated re-check restores precision). It never blocks
     and never edits the draft — purely additive; boilerplate/(c)/qualifier/FLS are skipped.
   - **Regulatory framework baked in.** Each Item's mandatory SEC requirements are
     injected into the prompt via `ITEM_RULES`. For **Item 1.01** this encodes the
     must-disclose set — (a) date, (b) parties, (c) a material-relationship statement
     (auto-included in standard form, flagged for counsel since it can't be derived from
     the contract), (d) material terms — plus the **materiality standard** (the reasonable-
     shareholder / "total mix" test, erring toward *including* an arguably material term
     since omission is the greater risk) and a **neutral, no-puffery tone** requirement.
     Every draft carries a `_compliance` summary (a checklist of those requirements marked
     satisfied / missing) shown in the web view and in the exported appendix, so a reviewer
     sees the SEC-requirement QC at a glance. (The full framework is Item-1.01-specific
     today; other Items get the general checks until per-Item guidance is added.)
4. **Reconcile every figure (fact guardrail).** After drafting, `lawrag.guardrail`
   normalizes then reconciles each material datum (currency, share/unit counts, %,
   dates, parties) in the draft against the source contract — pure local text, no DB.
   A figure in the draft with no match in the source is **RED and blocks** the draft
   from being treated as "ready"; the verdict + flagged figures show as a one-line
   banner on screen and in full in the review pack. This catches format-correct-but-
   wrong numbers that the presence-only `_compliance` checks miss.
   - **Derived figures** (a figure that equals a *specific, labeled* arithmetic
     derivation from verbatim source figures — e.g. share count = aggregate ÷ per-share
     price, which the drafter computes deterministically from the extracted clauses) are
     recognized as **grounded → "derived": review-required but NON-blocking**, with the
     arithmetic shown for one-glance confirmation. The check is *anchored* to that one
     labeled computation — it does NOT blind-search all number pairs (which would
     coincidentally "ground" a wrong figure), so a wrong/invented count stays RED.
   - **Omissions** are AMBER, review-only, never blocking (scoping to the rubric's
     MUST-disclose fields is a pending enhancement).
   Verdict: `blocked` (any fabrication) / `needs_review` (derived or omitted, non-
   blocking) / `clean`.
   - **Fix & re-check in-app.** Each substantive Item's disclosure is editable in the web
     view; a **"Re-check facts"** button re-runs the guardrail on the edited text
     (`POST /api/generations/{id}/reverify`, against the stored source) and updates the
     banner live — so a reviewer corrects a flagged figure and clears the block without
     leaving the app.
5. **Export — two separate files, never combined:**
   - The **8-K filing** (`draft_to_word` / `draft_to_pdf`): a clean document that
     mirrors an actual Form 8-K — SEC cover page (registrant/EIN/address,
     checkboxes, securities table), the Item disclosure, an Item 9.01 exhibit
     index, and a signature block — nothing else, so it is ready for counsel to
     finalize and file.
   - The **review pack** (`review_to_word` / `review_to_pdf`): a *separate*
     document for legal review only — the SEC-requirement checks, precedents
     used, the fact→source-quote trace, and the full set of extracted contract
     terms (to confirm the selective disclosure didn't drop anything material).
   Web (History tab) and CLI (`--docx/--pdf` for the filing, `--review-docx/
   --review-pdf` for the pack) expose both; the API serves the filing at
   `/export/word|pdf` and the pack at `/export/review-word|review-pdf`.

**Auto-detect the triggered Item(s).** On upload, `draft.detect_items` (an LLM classifier
over the document head, `POST /api/detect-items`) suggests which 8-K Item(s) the document
triggers, with a one-line reason, and the Generate tab **pre-checks the suggestion for the
user to confirm or adjust** (suggestion-only, never auto-committed). It classifies by
*document role*: a press release → 8.01 (or 7.01), **never** the substantive Item it merely
discusses; a signed agreement → **1.01 (primary)** plus any secondary Item (a note also → 2.03,
a private stock sale also → 3.02), never a secondary Item *alone*; and it is conservative about
2.01 — it will not suggest "completion" for a purchase agreement that is only signed and will
close later (a known failure mode of some cloud rivals). A document a reviewer nonetheless routes
to a cross-reference Item (e.g. 3.02) is redirected into that Item's substantive companion (1.01)
so it is still drafted and indexed, never dropped.

**Multiple documents per Item, multiple Items in one filing.** Real 8-Ks bundle several Items
and several documents — often **more than one document per Item** (Item 1.01 from a Securities
Purchase Agreement *and* a Registration Rights Agreement; Item 8.01 from two press releases).
The Generate tab accepts **one or more files** — each becomes a card whose detected Item(s) are
pre-checked — and the user routes each document to the Item(s) it covers (`routing` is
`item → one or more documents`). `draft.draft_filing`:
- **Contract Item from several agreements:** drafts each agreement (each guardrail-checked
  against its own source), then merges — the substantive bodies are spliced under **one** (c)
  material-relationship statement and **one** combined qualifier citing all their exhibits
  ("descriptions of the *Purchase Agreement* and *Registration Rights Agreement* … Exhibits 10.1
  and 10.2").
- **News Item from several press releases:** one paragraph each, furnished as **99.1, 99.2, …**.
- **Cross-reference Items** (3.02 → 1.01, 2.01/2.03 → 1.01): the "incorporated by reference"
  boilerplate (no LLM, no fabrication).

Documents are numbered by role — agreements/instruments → 10.1, 10.2, …; press releases →
99.1, 99.2, … — and the **Item 9.01 exhibit index is built from the actual documents supplied**
(each named specifically, e.g. "Registration Rights Agreement") in SEC exhibit-number order + the
104 cover-page XBRL. A supplied document's number is taken from the UI (or inferred from its
filename, e.g. `…EX-4.1…`). **Index-only exhibits** — ones a filing *lists* in Item 9.01 but does
not draft narrative from (securities instruments **EX-4.x** warrants, a legal opinion **EX-5.1**, a
consent **EX-23.1**) — are placed in the index by their number with a specific description read
from the document (a "FORM OF …" heading → "Form of Common Warrant") or a type default, without
being treated as a drafting source. So the tool accepts an arbitrary exhibit set
(1.x/4.x/5.x/10.x/23.x/99.x): it drafts from the contracts and press releases and lists the rest. Every Item runs the numeric guardrail + narrative audit against *its* source;
all safety signals merge to the filing level. `.txt` input is supported (press releases are often
plain text). A contract Item is also given the filing's **press-release text as related-filing
context** and grounded against the contract *plus* that press release, so facts a "Form of"
agreement omits but the press release states (share count, offering size, gross proceeds) can enter
Item 1.01 as grounded facts — anything not in any filing document is still locked/flagged.

```bash
# Tag historical 8-Ks with their Item number(s) at ingest (auto-detected, or manual
# — a filing can report several, e.g. Item 1.01 + Item 9.01 together):
./.venv/bin/python scripts/ingest.py /path/to/old_8ks --doc-type 8-K --filing-item 1.01 9.01

# Draft a new Item 1.01 disclosure from a contract that triggers one
./.venv/bin/python scripts/draft_8k.py /path/to/contract.docx --item 1.01
./.venv/bin/python scripts/draft_8k.py /path/to/contract.docx --item 1.01 --json

# Every draft is saved to the History tab by default (--client to tag it, --no-save to
# skip); also write it out as a file:
./.venv/bin/python scripts/draft_8k.py /path/to/contract.docx --item 1.01 \
    --client "Richtech Robotics Inc." --docx draft.docx --pdf draft.pdf
```

**Matches real-filing conventions for two edge cases, found by testing against
Richtech's own real filings:**
- **Redacted source contracts.** Some exhibits are filed with portions redacted
  under Item 601(b)(10)(iv) of Regulation S-K, which left literal block-placeholder
  glyphs in the extracted text; the drafting step was echoing them straight into
  the disclosure (e.g. "████████ Inc."). `summarize.review_contract` now collapses
  those glyphs to a `[REDACTED]` marker before the fact ever reaches the drafting
  prompt, and the prompt instructs the model to describe that party only by a
  short generic role-based reference — exactly how Richtech's own filings handle
  it (e.g. "one of the largest retailers in the world (the "Client")") — never by
  printing placeholder characters. A `_compliance` check flags any residual
  redaction marker as a safety net.
- **Forward-Looking Statements legend.** Only 3 of Richtech's 17 real Item 1.01
  filings carry the PSLRA safe-harbor "Forward-Looking Statements" paragraph — always
  because a human added a forward-looking view of the deal, never by default. The
  legend is attached to a draft **only when a reviewer supplies a business-context
  note** (see the box below); it appears as its own labeled section between the Item
  disclosure and Item 9.01 — matching where real filings place it.
  - The drafting step itself **never** adds the legend, even when the disclosure
    recites forward-looking-*sounding* deal mechanics ("the closing is expected to
    occur…", a press release's "the Company intends to use the proceeds…"). Those are
    grounded present/near-term facts, not the Company's own projections, and real
    filings routinely carry them without a body legend (it lives in the press-release
    exhibit). The gate is the presence of a reviewer note, not disclosure phrasing —
    so a draft is never given a safe-harbor legend the reviewer didn't ask for.
  - Checked against the real contracts behind those 3 filings: the forward-looking
    phrasing (e.g. "a strategic ... facility for warehousing, assembly and light
    manufacturing") never appears in the contract itself — it's business/strategic
    context a human adds from outside knowledge of the Company's plans, which by
    definition no document-grounded extraction can produce. That is exactly why the
    legend is gated on reviewer input: the **web UI has a "Business / strategic
    context" box** on every draft (Generate 8-K and History) where legal or
    management can describe that context in a sentence or two; `draft.
    add_business_context` merges it into the disclosure's opening paragraph at the
    natural position (typically right after the asset/subject-matter description,
    before the financial/closing terms — the same spot real filings put it),
    rather than bolting a sentence onto the end. Every existing fact, figure, and
    defined term in that paragraph is checked to confirm it survived the rewrite
    unchanged (`_preserves_facts`); if the check fails, it falls back to a plain
    append instead of risking a silently altered figure. The added text is
    attached to the draft clearly marked as reviewer-supplied (not a contract
    citation, shown separately in the fact→source trace), and the Forward-Looking
    Statements legend is added automatically. `POST /api/generations/{id}/
    business-context` persists the update to that same History record.

Downloaded filenames are named after the contract's actual event date, not the
internal generation id, e.g. `2025-08-21 - 8-K Draft.docx` / `2025-08-21 - 8-K
Draft - Review.pdf`.

**On-screen view — the filing content, Item by Item.** Every draft (Generate
8-K and History) shows a **Filing content** panel that lays out the substantive
filing as clean, readable text, one section per Item — Item 1.01 (the disclosure
paragraphs, plus the Forward-Looking Statements legend when present) and Item
9.01 (the exhibit index: 10.1 = the source agreement dated the event date, 104 =
the cover-page XBRL) — mirroring the Word/PDF filing body without the boilerplate
cover page. The SEC-requirement checks, precedents used, and fact→source-quote
trace are **not** shown here — they live in the downloadable **Review pack**
(Word/PDF), keeping the on-screen view focused on what will actually be filed.
The exact formatted document is always one click away via the download buttons.

Every generated draft is re-accessible from the **History** tab (persisted in
the `generations` table, client-scoped), and each row has a **Delete** button to
prune drafts you no longer need (`DELETE /api/generations/{id}`, scoped to what
the caller can already see, so a user can only delete their own).

Started with **Item 1.01 (Entry into a Material Definitive Agreement)**: the most
common trigger, most template-able disclosure, and its inputs (parties, term,
payment, termination) map directly onto fields the due-diligence engine already
extracts. Precedent library is **30 of Richtech's own real 8-K/8-K-A filings**
pulled from SEC EDGAR (Item numbers from EDGAR's own filing metadata — 17 of the
30 report Item 1.01).

**Item-specific extraction:** most real 8-Ks disclose several Items at once, and
each Item type needs different facts — a financial instrument needs principal/
interest/maturity, not services-contract terms like IP or exclusivity.
`draft.ITEM_CHECKLISTS` lets an Item override the default general-commercial
checklist passed to the due-diligence engine (`summarize.review_contract`).

**Items covered.** Every 8-K Item that is driven by a source transactional
*document* (the thing this tool drafts *from*) now has a tailored checklist:

| Item | Title | Extraction focus |
|------|-------|------------------|
| 1.01 | Entry into a Material Definitive Agreement | parties, term, key commercial terms (default checklist) |
| 1.02 | Termination of a Material Definitive Agreement | agreement terminated, date, reason, fees, surviving obligations |
| 2.01 | Completion of Acquisition or Disposition of Assets | parties, assets, closing date, consideration |
| 2.03 | Creation of a Direct Financial Obligation | principal, discount, interest, maturity, conversion, redemption |
| 3.02 | Unregistered Sales of Equity Securities | securities, price, exemption relied upon, use of proceeds |
| 5.02 | Departure/Election of Directors or Officers | name, position, event, effective date, compensatory terms |

**News Items** — Regulation FD (7.01) and Other Events (8.01) — are drafted from a **press
release** supplied as a second document (see multi-document filing above), which is then
furnished as Exhibit 99.1. *Deliberately excluded* are event-driven Items with no underlying
*document* to draft from — bankruptcy (1.03), results of operations (2.02), delisting notices
(3.01), auditor changes (4.01/4.02), vote results (5.07). This tool drafts a disclosure *from
a document*; those Items don't have one.

**Materiality rubric (Item 1.01) — a company-neutral, data-derived guide to *what is
treated as material*, not just how it's phrased.** Matching tone isn't enough: real filings
make deliberate *inclusion/omission* choices (governing law is essentially never called out;
price almost always is) that a generic checklist doesn't capture. An earlier rubric was
derived from **17 of Richtech's own** Item 1.01 filings — a single-issuer bias unfit for a
multi-company product. It was **replaced with a market-norm rubric measured across the ~90-issuer
public-EDGAR corpus** (`training/build_general_rubric.py`, a deterministic, deal-type-aware
keyword scan — no LLM), so the bands reflect general practice rather than one filer's habits.

Across **245 real Item 1.01 disclosures (~90 issuers)**, terms fall into three bands:

| Band | Terms (with disclosure rate) |
|------|------------------------|
| **Always** disclose when present | Price / principal (89%) |
| **Usually** disclose, deal-type dependent | Term (60%), asset description (55%), reps (53%, ~87% for equity deals), closing timing (46%, ~68% for equity), conversion (37%), interest rate (~58% for debt); earnest money "include when present" |
| **Rarely / never** as an individual term | Governing law (0/245), dispute resolution (0.8%), confidentiality (6%), assignment (10%) — folded into a boilerplate "customary provisions" catch-all instead |

This is encoded in `draft.ITEM_CHECKLISTS["1.01"]` (a 23-field checklist covering every deal
type Item 1.01 spans — financings, real estate, notes, services, M&A) and
`draft.ITEM_RULES["1.01"]` (the measured ALWAYS/USUALLY/RARELY bands + the general SEC (a)-(d)
requirements, with an instruction to prefer *including* a term when materiality is arguable,
since omission is the greater legal risk). One gap is fundamental to any document-grounded
approach: business-context narrative that isn't in the source at all (e.g. *why* a property
matters strategically) — see the Business/strategic-context box above.

**Registrant profile is a per-deployment input, not hardcoded.** The 8-K cover/signature
identifiers (name, state, Commission File No., EIN, address, phone, securities table, EGC flag,
signer) come from a `registrant.json` (`export.load_registrant`, path via `REGISTRANT_FILE`),
editable in-browser via the admin **Company** tab (`GET/PUT /api/registrant`) and read at render
time — a new customer or a changed address is a config edit, no code change.

**Held-out validation.** A real-estate Purchase & Sale Agreement (its own real 8-K excluded)
re-drafts guardrail-CLEAN with every figure grounded — building size (79,325 sq ft), earnest
money ($600,000), price ($21,180,000), closing timing — while boilerplate terms (governing law,
assignment, dispute resolution) fold into the catch-all rather than being enumerated. All SEC
compliance checks pass: (a) date, (b) parties, (c) material-relationship statement, (d) material
terms, and the exhibit-incorporation qualifier (rendered exactly once).

**Three real held-out tests done** (a real contract with its own real resulting
8-K excluded from its precedent pool, then compared against what was actually
filed): Item 1.01 (Master Services Agreement), Item 2.03 (convertible promissory
note), and Item 3.02 (securities purchase agreement). All confirm disclosed
facts are extracted accurately (dates/amounts/rates/exemptions all matched) and
the model declines to invent anything redacted or absent. An earlier version was
too verbose — it enumerated every extracted term, where a real 8-K states only
the material ones and defers the rest to the exhibit; the drafting step was
reworked to be selective and calibrated to the precedents, and now produces
one-to-three-paragraph disclosures close to the real filings' shape. Items 1.02,
2.01 and 5.02 are wired with checklists but not yet held-out-tested (no clean
source document was on hand); they draft the same way once a real document is
supplied. **Next: a lawyer reviews the drafts** — this stays an experiment
requiring sign-off, and RAG-grounded, never fine-tuned.

### Retired: fine-tuned adapter / delexicalized variant (`training/`, history only)

A **LoRA style adapter** (v2) and a later **delexicalized** variant (v4/v5) were built and
validated on an RTX 6000, then **retired** — kept in `training/` for history, not on the
serving path. The lesson defines the current architecture:
- The v2 adapter won on *style* (A/B on held-out companies: ROUGE-L 0.246→0.464, output
  tightened 2430→1098 chars) but **fabricated figures** on number-dense disclosures — the exact
  compliance red line. A data-clean **v3 did not beat v2**, confirming fabrication is *structural*
  to using generative weights for facts, not a data-volume problem.
- The **delexicalized** idea (train on typed placeholders, backfill real values deterministically)
  worked for transactional Items (2.03/3.02 ~78% groundable) but not the narrative **1.01/5.02
  core** (~6% / 0%), where disclosures paraphrase and pull facts from outside the paired exhibit.
  Widening the training window was measured to buy only ~+5 pts — so long-context (ZeRO-3)
  training was ruled out.
- **Conclusion:** facts must live in extraction + guardrail, never in weights. Style is obtainable
  *without* fine-tuning — deterministic EDGAR-faithful export (structure) + prompt/rubric (tone) +
  planned facts-stripped few-shot from the customer's own filings. Production runs the **plain base
  model**; the design is still "shared base per filing type" (8-K done; S-8 / 10-K later). The
  full rationale is in `8K_DRAFTING_FINDINGS_REPORT.md`; the retired training package and RTX
  recipe are in `training/` (`DEPLOY_THOR.md`, `DELEX_V5_FINDINGS.md`). Every draft still requires
  lawyer sign-off.

## Layout

```
lawrag/
  config.py     env-based config (.env)
  db.py         schema (documents, chunks), pgvector, indexes
  parsers.py    PDF (pymupdf) + Word (python-docx); flags scanned PDFs (NeedsOCR)
  chunk.py      paragraph-aware chunking with overlap
  embed.py      embedding client -> local vLLM endpoint
  ingest.py     file -> parse -> chunk -> embed -> store (dedupe by sha256)
  retrieve.py   hybrid search (vector + keyword, RRF) + rerank + metadata filters
  rerank.py     cross-encoder reranker client
  llm.py        LLM client (chat + guided-JSON structured output)
  summarize.py  due-diligence engine: clause extraction + risk flags + summary
  metadata.py   auto-extract doc_type/title/parties/client/date(/filing_item) at ingest
  export.py     batch DD export to Excel (matrix) + Word (memo); 8-K draft export to Word/PDF
  auth.py       users, per-client permissions, sessions, audit (ethical walls)
  draft.py      experimental: draft 8-K Item disclosure(s) grounded in the source
                document(s) — extraction + code generation, figure-lock, narrative audit,
                auto-Item detection, multi-document routing (draft_filing)
  guardrail.py  deterministic fact-reconciliation (RED blocks fabrication) — no DB/retrieval
  generations.py  history of AI-generated documents (currently 8-K drafts), client-scoped
  api.py        FastAPI backend (login/stats/search/summarize/ingest/export) + serves web/
web/            local web UI (index.html, style.css, app.js) — no external assets
scripts/        init_db / user_admin / ingest / query / summarize / dd_batch / draft_8k /
                serve / make_samples
data/sample/    synthetic test documents
```

## Roadmap

- **Phase 1 (done):** ingestion + hybrid retrieval + **cross-encoder reranker** +
  citations + metadata filters.
- **Phase 2 (done):** due-diligence review — contract summary + clause extraction to a
  fixed checklist with verbatim quotes + risk flags, served by the local `Qwen3.6-35B`
  LLM (32k context) with guided-JSON output and map-reduce for long contracts.
- **Auto-metadata + web upload (done):** on ingest, the LLM auto-detects doc_type /
  title / parties / client / date (guided JSON); the "Add to Library" web tab lets a
  lawyer drag files in — no manual tagging. Reranker upgraded to `bge-reranker-v2-m3`
  (the 0.6B Qwen reranker degraded ranking on a larger corpus).
- **Batch DD + export (done):** review a whole folder of contracts; export an Excel
  comparison matrix (one row per contract, clause columns, + a risks sheet) or a Word
  memo — via the web "Review" tab or `scripts/dd_batch.py`.
- **Access control (done):** login + role-based, per-client ethical walls enforced
  server-side on all retrieval/stats/ingest, with an audit log (`lawrag/auth.py`,
  `scripts/user_admin.py`).
- **Client-name normalization (done):** canonical client names at ingest + admin
  `merge` to consolidate variants (`lawrag/clients.py`).
- **In-web admin + Library (done):** a Library tab to browse permitted documents,
  and an admin-only Users tab for full user management — no CLI needed.
- **Original-file storage (done):** ingested originals are kept in `storage/`
  (keyed by hash, gitignored) and served back via an access-scoped download link.
- **Phase 1.5 / next:** OCR for scanned PDFs; tie extraction citations to ingested
  chunk pages; TLS/SSO hardening; deployment auto-start.
- **Phase 3 (drafting, experiment — architecture SETTLED):** 8-K drafting grounded in the
  source document(s) on the **plain base model** (`lawrag/draft.py`): Item-specific extraction
  + repair, code generation with figure-lock (`mode="hybrid"` default), numeric guardrail +
  narrative audit, company-neutral data-derived materiality rubric, auto-Item detection,
  multi-document filing (contract + press release → merged exhibits), editable registrant
  profile. Fine-tuning (adapter/delex) was tried and **retired** — facts stay in extraction +
  guardrail, never in weights. **Next (no training):** per-customer tone via facts-stripped
  few-shot from the customer's own filings; deeper extraction; offering-specific exhibits
  (5.1/23.1) as reviewer supplements. Every draft requires lawyer sign-off.

## Privacy notes

- No external network calls in the retrieval/ingest path.
- Roadmap: encryption at rest, per-user access control tied to client/matter,
  and an audit log of queries.
