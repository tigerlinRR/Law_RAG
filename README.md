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

A first test of RAG-grounded drafting — deliberately **not** a fine-tuned model.
SEC disclosures are fact-critical, so this stays retrieval + extraction:

1. **Extract facts** from the source contract with the existing due-diligence
   engine (same clause checklist, verbatim quotes).
2. **Retrieve precedents** — prior 8-K filings that report the *same Item number*
   already in the library (`documents.meta.filing_items`, a JSONB array — a real
   8-K commonly reports several Items at once, matched by containment, not exact
   equality), used **only** for structure and tone, never as a source of facts.
3. **Draft** the Item disclosure with the LLM as a real 8-K would read: a
   **brief, selective** description of the *material* terms in one to three
   paragraphs, not a comprehensive summary — standard/boilerplate provisions are
   collapsed into a catch-all and the rest is deferred to the exhibit, calibrated
   to the precedents' own length and selectivity. It uses *only* the extracted
   contract facts; every disclosed fact is cited back to its verbatim quote in
   `facts_used`, missing facts are marked `[NOT STATED IN CONTRACT]` (never
   invented), and the standard "qualified in its entirety by reference to
   Exhibit 10.1" closing is guaranteed. Every citation (here and in the
   due-diligence engine's clause quotes) is checked programmatically against the
   source text (`summarize.verify_quote`) — a citation not found verbatim is
   flagged `⚠ UNVERIFIED` rather than silently trusted.
   - **Regulatory framework baked in (from Richtech counsel).** Each Item's
     mandatory SEC requirements are injected into the prompt via `ITEM_RULES`.
     For **Item 1.01** this encodes the must-disclose set — (a) date, (b)
     parties, (c) a material-relationship statement (auto-included in standard
     form, flagged for counsel since it can't be derived from the contract), (d)
     material terms — plus the **materiality standard** (the reasonable-
     shareholder / "total mix" test, erring toward *including* an arguably
     material term since omission is the greater risk) and a **neutral,
     no-puffery tone** requirement. Every draft carries a `_compliance` summary
     (a checklist of those requirements marked satisfied / missing) shown in the
     web view and in the exported appendix, so a reviewer sees the SEC-requirement
     QC at a glance. (The full framework is Item-1.01-specific today, matching
     counsel's guidance; other Items get the general checks until per-Item
     guidance is added.)
4. **Export** mirrors an actual Form 8-K, not a generic report: SEC cover page
   (registrant/EIN/address, checkboxes, securities table), the Item disclosure,
   an Item 9.01 exhibit index, and a signature block, all stamped
   `DRAFT — NOT FILED WITH THE SEC`. A clearly separate appendix (never mixed
   into the filing text) holds the precedents used, the fact→source-quote trace,
   and **the full set of terms extracted from the contract** — so a reviewer can
   confirm the selective disclosure didn't drop anything material.

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

*Deliberately excluded* are event-driven Items with no underlying contract to
draft from — bankruptcy (1.03), results of operations (2.02), delisting notices
(3.01), auditor changes (4.01/4.02), vote results (5.07), and Regulation FD /
other events (7.01/8.01). This tool drafts a disclosure *from a document*; those
Items don't have one.

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
  draft.py      experimental: draft an 8-K Item disclosure grounded in a contract's
                extracted facts, using same-Item precedents as style reference only
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
- **Phase 3 (drafting, experiment started):** 8-K Item 1.01 drafting grounded in
  contract facts + same-Item precedents (`lawrag/draft.py`), precedent library now
  30 of Richtech's real 8-Ks from SEC EDGAR — pending a real contract paired with
  the 8-K it triggered for the actual quality comparison. Still RAG, not fine-tuning
  — see rationale above and in project memory. LoRA remains reserved for house
  style only, never facts.

## Privacy notes

- No external network calls in the retrieval/ingest path.
- Roadmap: encryption at rest, per-user access control tied to client/matter,
  and an audit log of queries.
