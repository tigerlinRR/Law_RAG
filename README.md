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
  live filter. Admins can delete a document here.
- **Review a Contract** — drag-and-drop one or more PDF/Word files. One file →
  full report (summary, parties, key-clause table with verbatim quotes, risks).
  Several files → a comparison table plus per-file reports. Export the whole batch
  to **Excel** (clause matrix + risks sheet) or **Word** (memo).
- **Add to Library** — drag files in; type/parties/client/date auto-detected.
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
  metadata.py   auto-extract doc_type/title/parties/client/date at ingest
  export.py     batch DD export to Excel (matrix) + Word (memo)
  auth.py       users, per-client permissions, sessions, audit (ethical walls)
  api.py        FastAPI backend (login/stats/search/summarize/ingest/export) + serves web/
web/            local web UI (index.html, style.css, app.js) — no external assets
scripts/        init_db / user_admin / ingest / query / summarize / dd_batch / serve / make_samples
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
- **Phase 1.5 / next:** OCR for scanned PDFs; tie extraction citations to ingested
  chunk pages; TLS/SSO hardening; deployment auto-start.
- **Phase 3 (drafting, when trusted):** RAG-grounded drafting from precedents with a
  lawyer in the loop; optional LoRA for house style only — never for facts.

## Privacy notes

- No external network calls in the retrieval/ingest path.
- Roadmap: encryption at rest, per-user access control tied to client/matter,
  and an audit log of queries.
