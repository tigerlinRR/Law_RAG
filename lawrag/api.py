"""Local web API for Law_RAG — search + contract review. Fully local, no egress.

Serves the static frontend (web/) and exposes:
  GET  /api/stats      -> counts + distinct filter values (clients / types / authors)
  POST /api/search     -> hybrid retrieval with optional metadata filters
  POST /api/summarize  -> due-diligence review of an uploaded contract
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from decimal import Decimal

from . import auth, clients, db, export, generations, guardrail
from .config import ROOT
from .draft import (ITEM_TITLES, _FIGURE_PLACEHOLDER, _FORWARD_LOOKING_STATEMENTS,
                    _compliance_flags, _narrative_flags, _needs_forward_looking_statements,
                    add_business_context, detect_items, draft_8k, draft_filing)
from .ingest import DocMeta, ingest_file
from .parsers import NeedsOCR
from .retrieve import Filters, search
from .summarize import review_contract

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_COOKIE = "lawrag_session"


def current_user(lawrag_session: str | None = Cookie(default=None)) -> dict:
    """Auth dependency: resolves the session cookie to a user or 401s.

    Returns a dict with allowed_clients (None for admin = unrestricted)."""
    user = auth.resolve_session(lawrag_session)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


class LoginReq(BaseModel):
    username: str
    password: str

WEB = ROOT / "web"
app = FastAPI(title="Law_RAG", docs_url="/api/docs")


@app.post("/api/login")
def login(req: LoginReq, response: Response) -> dict:
    user = auth.authenticate(req.username, req.password)
    if not user:
        auth.log(req.username, "login_failed")
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = auth.create_session(user["id"])
    response.set_cookie(_COOKIE, token, httponly=True, samesite="lax",
                        max_age=auth.SESSION_HOURS * 3600, path="/")
    auth.log(user["username"], "login")
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/logout")
def logout(response: Response, lawrag_session: str | None = Cookie(default=None)) -> dict:
    auth.delete_session(lawrag_session)
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"], "role": user["role"],
            "clients": user["allowed_clients"]}


class SearchReq(BaseModel):
    query: str
    client: str | None = None
    matter: str | None = None
    doc_type: str | None = None
    author: str | None = None
    top_k: int | None = None
    rerank: bool = True


@app.get("/api/stats")
def stats(user: dict = Depends(current_user)) -> dict:
    allowed = user["allowed_clients"]  # None = admin/unrestricted
    scope = "" if allowed is None else " AND client = ANY(%(a)s)"
    params = {} if allowed is None else {"a": allowed}
    with db.connect() as conn, conn.cursor() as cur:
        if allowed is None:
            cur.execute("SELECT count(*) FROM documents")
        else:
            cur.execute("SELECT count(*) FROM documents WHERE client = ANY(%(a)s)", params)
        docs = cur.fetchone()[0]
        if allowed is None:
            cur.execute("SELECT count(*) FROM chunks")
        else:
            cur.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.id=c.document_id "
                        "WHERE d.client = ANY(%(a)s)", params)
        chunks = cur.fetchone()[0]

        def distinct(col: str) -> list[str]:
            cur.execute(
                f"SELECT DISTINCT {col} FROM documents "
                f"WHERE {col} IS NOT NULL AND {col} <> ''{scope} ORDER BY 1", params)
            return [r[0] for r in cur.fetchall()]

        return {
            "documents": docs, "chunks": chunks,
            "clients": distinct("client"),
            "doc_types": distinct("doc_type"),
            "authors": distinct("author"),
        }


@app.post("/api/search")
def api_search(req: SearchReq, user: dict = Depends(current_user)) -> dict:
    filters = Filters(client=req.client or None, matter=req.matter or None,
                      doc_type=req.doc_type or None, author=req.author or None)
    hits = search(req.query, filters=filters, top_k=req.top_k, use_rerank=req.rerank,
                  allowed_clients=user["allowed_clients"])
    auth.log(user["username"], "search", req.query)
    out = []
    for h in hits:
        d = asdict(h)
        d["citation"] = h.citation()
        out.append(d)
    return {"hits": out, "reranked": bool(hits) and hits[0].reranked}


@app.post("/api/summarize", response_model=None)
async def api_summarize(file: UploadFile = File(...), user: dict = Depends(current_user)):
    auth.log(user["username"], "summarize", file.filename or "")
    tmpdir = Path(tempfile.mkdtemp(prefix="lawrag_"))
    dest = tmpdir / (file.filename or "upload")
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return review_contract(dest)
    except NeedsOCR as e:
        return JSONResponse({"error": "scanned",
                             "detail": "This looks like a scanned PDF; OCR is not "
                                       "enabled yet."}, status_code=422)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "failed", "detail": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/ingest", response_model=None)
async def api_ingest(files: list[UploadFile] = File(...), user: dict = Depends(current_user)):
    """Ingest uploaded files into the knowledge base with auto-extracted metadata."""
    auth.log(user["username"], "ingest", ", ".join(f.filename or "" for f in files))
    results = []
    tmpdir = Path(tempfile.mkdtemp(prefix="lawrag_ing_"))
    try:
        for uf in files:
            dest = tmpdir / (uf.filename or "upload")
            with open(dest, "wb") as f:
                shutil.copyfileobj(uf.file, f)
            r = ingest_file(dest, DocMeta(), auto=True)
            results.append({
                "filename": r.path.name, "status": r.status, "n_chunks": r.n_chunks,
                "doc_type": r.doc_type, "client": r.client, "doc_date": r.doc_date,
                "parties": r.parties, "detail": r.detail,
            })
        db.ensure_vector_index()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"results": results}


# ---------- 8-K drafting ----------
@app.get("/api/draft-items")
def api_draft_items(user: dict = Depends(current_user)) -> dict:
    """8-K Item types this tool can draft (each has a tailored extraction checklist)."""
    return {"items": [{"item": k, "title": v} for k, v in ITEM_TITLES.items()]}


@app.post("/api/detect-items", response_model=None)
async def api_detect_items(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Suggest the 8-K Item(s) an uploaded document triggers, so the UI can pre-check them.
    Suggestion only — the user confirms/adjusts before drafting."""
    tmpdir = Path(tempfile.mkdtemp(prefix="lawrag_detect_"))
    dest = tmpdir / (file.filename or "upload")
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return {"suggested": detect_items(dest)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "failed", "detail": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/generate/8k", response_model=None)
async def api_generate_8k(files: list[UploadFile] = File(...),
                          item: str = Form("1.01"), items: str = Form(""),
                          client: str = Form(""), assignments: str = Form(""),
                          user: dict = Depends(current_user)):
    """Draft a (possibly multi-Item, multi-document) 8-K and save to History.

    `assignments` (JSON): [{"filename": "...", "items": ["8.01"]}, ...] — the user's confirmed
    mapping of each uploaded document to the Item(s) it covers (a contract -> 1.01, a press
    release -> 8.01). If absent, falls back to the legacy single-doc behavior (`items`/`item`
    on the one file). Records are scoped to the caller's permitted clients."""
    allowed = user["allowed_clients"]  # None = admin/unrestricted
    canonical = clients.resolve(client) if client.strip() else None
    if allowed is not None and (canonical is None or canonical not in allowed):
        raise HTTPException(status_code=403, detail="client not in your permitted scope")
    if not files:
        raise HTTPException(status_code=400, detail="upload at least one document")
    try:
        assign = json.loads(assignments) if assignments.strip() else []
    except json.JSONDecodeError:
        assign = []

    tmpdir = Path(tempfile.mkdtemp(prefix="lawrag_gen_"))
    try:
        saved: list[tuple[str, Path]] = []
        for uf in files:
            dest = tmpdir / (uf.filename or f"upload_{len(saved)}")
            with open(dest, "wb") as f:
                shutil.copyfileobj(uf.file, f)
            saved.append((uf.filename or dest.name, dest))
        by_name = {name: path for name, path in saved}

        routing: dict[str, Path] = {}
        sel: list[str] = []
        for a in assign:
            p = by_name.get(a.get("filename"))
            if not p:
                continue
            for it in a.get("items", []):
                if it in ITEM_TITLES:
                    routing[it] = p
                    if it not in sel:
                        sel.append(it)
        if not sel:  # legacy / single-doc: global items on the first file, auto-route
            sel = [i.strip() for i in items.split(",") if i.strip()] or [item]
            routing = {}
        bad = [i for i in sel if i not in ITEM_TITLES]
        if bad:
            raise HTTPException(status_code=400, detail=f"unsupported item(s): {', '.join(bad)}")

        auth.log(user["username"], "generate_8k",
                 f"items {','.join(sel)}: {', '.join(by_name)}")
        r = draft_filing([p for _, p in saved], sel, allowed_clients=allowed,
                         routing={it: str(p) for it, p in routing.items()} or None)
        gen_id = generations.save("8k_draft", r, source_name=saved[0][0],
                                  client=canonical, item=r.get("item", sel[0]),
                                  created_by=user["username"])
        return {"id": gen_id, "result": r}
    except HTTPException:
        raise
    except NeedsOCR:
        return JSONResponse({"error": "scanned",
                             "detail": "This looks like a scanned PDF; OCR is not "
                                       "enabled yet."}, status_code=422)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "failed", "detail": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class BusinessContextReq(BaseModel):
    note: str


@app.post("/api/generations/{gen_id}/business-context")
def api_add_business_context(gen_id: int, req: BusinessContextReq,
                              user: dict = Depends(current_user)) -> dict:
    """Let a human reviewer (legal/management) add a sentence of business/strategic
    context the contract itself doesn't state -- e.g. why a property matters to the
    Company's plans. This is the one category of content a document-grounded draft
    can never produce on its own; it must come from a person, and is stored clearly
    attributed to them (not as a contract citation) -- see draft.add_business_context."""
    g = _get_generation_or_404(gen_id, user)
    auth.log(user["username"], "add_business_context", f"generation {gen_id}")
    updated = add_business_context(g["result"], req.note)
    generations.update_result(gen_id, updated)
    return {"id": gen_id, "result": updated}


def _sections_of(result: dict) -> list[dict]:
    """The draft's disclosure sections — the multi-Item list, or a single primary Item."""
    return result.get("_items") or [{
        "item": result.get("item"), "item_title": result.get("item_title"),
        "disclosure": result.get("disclosure", ""), "cross_ref": False}]


def _recompute_verification(result: dict) -> None:
    """Re-run the fact guardrail + presence/FLS checks against the stored source, in place.
    Shared by /reverify (after text edits) and /supplements (after gap fills). Human-supplied
    supplement figures live in `_derived_values`, so they reconcile as grounded (non-blocking)
    rather than being re-flagged as fabricated."""
    sections = _sections_of(result)
    src = result.get("_source_text", "")
    derived = [(Decimal(v), d) for v, d in result.get("_derived_values", [])]
    body = "\n\n".join(s.get("disclosure", "") for s in sections if not s.get("cross_ref"))
    target = body or result.get("disclosure", "")
    result["_guardrail"] = guardrail.reconcile(target, src, derived=derived)
    # Re-run the #6 narrative-claim audit (review-only) against the GROUNDED FACTS the draft
    # was built from, plus what the reviewer has since confirmed (supplements + business
    # context) — so their added facts are treated as supported, not re-flagged.
    evidence = result.get("_grounded_facts", "") or src
    for s in result.get("_supplements", []):
        evidence += f"\nReviewer-confirmed value: {s.get('value', '')}"
    for f in result.get("facts_used", []):
        if f.get("source") == "business_context":
            evidence += f"\nReviewer business context: {f.get('fact', '')}"
    result["_narrative_flags"] = _narrative_flags(target, evidence)
    # Unfilled placeholders still count as "to fill" (a placeholder isn't a figure the
    # guardrail flags, but the reviewer must still resolve it).
    result["_blanked_figures"] = [_FIGURE_PLACEHOLDER] * target.count(_FIGURE_PLACEHOLDER)
    result["_compliance"] = _compliance_flags(result.get("item", ""),
                                              result.get("disclosure", ""))
    if _needs_forward_looking_statements(result.get("disclosure", "")):
        result["_forward_looking_statements"] = _FORWARD_LOOKING_STATEMENTS
    else:
        result.pop("_forward_looking_statements", None)


def _replace_nth(text: str, needle: str, repl: str, n: int) -> tuple[str, bool]:
    """Replace the n-th (0-based) occurrence of `needle` with `repl`."""
    parts = text.split(needle)
    if n < 0 or n >= len(parts) - 1:
        return text, False
    return needle.join(parts[:n + 1]) + repl + needle.join(parts[n + 1:]), True


def _supp_decimal(s: str):
    """Parse a reviewer-supplied value to a Decimal (strip $/commas/text), else None."""
    t = re.sub(r"[^0-9.]", "", s or "")
    try:
        return Decimal(t) if t not in ("", ".") else None
    except Exception:
        return None


class ReverifyReq(BaseModel):
    items: list[dict]  # [{"item": "1.01", "disclosure": "<edited text>"}]


@app.post("/api/generations/{gen_id}/reverify")
def api_reverify(gen_id: int, req: ReverifyReq, user: dict = Depends(current_user)) -> dict:
    """Re-run the fact guardrail after a human edits the draft's figures/text, so a
    corrected draft can clear the banner in-app. Reconciles the edited disclosure(s)
    against the stored source text (+ anchored derivations) and re-saves."""
    g = _get_generation_or_404(gen_id, user)
    result = g["result"]
    edits = {e.get("item"): e.get("disclosure", "") for e in req.items}
    sections = _sections_of(result)
    for s in sections:
        if s.get("item") in edits:
            s["disclosure"] = edits[s["item"]]
    result["_items"] = sections
    if result.get("item") in edits:
        result["disclosure"] = edits[result["item"]]
    _recompute_verification(result)
    generations.update_result(gen_id, result)
    auth.log(user["username"], "reverify", f"generation {gen_id}")
    return {"id": gen_id, "result": result}


class SupplementFill(BaseModel):
    index: int              # 0-based [NOT IN SOURCE — CONFIRM] occurrence in the item's text
    value: str
    item: str | None = None


class SupplementsReq(BaseModel):
    fills: list[SupplementFill]


@app.post("/api/generations/{gen_id}/supplements")
def api_supplements(gen_id: int, req: SupplementsReq,
                    user: dict = Depends(current_user)) -> dict:
    """Fill the flagged gaps -- the `[NOT IN SOURCE — CONFIRM]` placeholders the guardrail
    blanked -- with reviewer-supplied values. A supplied value is a fact the reviewer
    vouches for that the contract does not state; it is recorded as a grounded supplement
    (added to `_derived_values`, so the guardrail treats it as grounded/non-blocking, like a
    derivation) and substituted into the disclosure. Then re-reconciles so the banner clears."""
    g = _get_generation_or_404(gen_id, user)
    result = g["result"]
    sections = _sections_of(result)
    fills_by_item: dict[str, list[SupplementFill]] = {}
    for f in req.fills:
        fills_by_item.setdefault(f.item or result.get("item"), []).append(f)
    supplements = result.get("_supplements", [])
    derived = result.get("_derived_values", [])
    for s in sections:
        disc = s.get("disclosure", "")
        # apply highest index first so earlier occurrences' positions stay valid
        for f in sorted(fills_by_item.get(s.get("item"), []), key=lambda x: -x.index):
            val = (f.value or "").strip()
            if not val:
                continue
            disc, ok = _replace_nth(disc, _FIGURE_PLACEHOLDER, val, f.index)
            if ok:
                supplements.append({"value": val, "item": s.get("item")})
                num = _supp_decimal(val)
                if num is not None:
                    derived.append([str(num), f"supplied by reviewer: {val}"])
        s["disclosure"] = disc
    result["_items"] = sections
    for s in sections:
        if s.get("item") == result.get("item"):
            result["disclosure"] = s["disclosure"]
    result["_supplements"] = supplements
    result["_derived_values"] = derived
    _recompute_verification(result)
    generations.update_result(gen_id, result)
    auth.log(user["username"], "supplements", f"generation {gen_id}")
    return {"id": gen_id, "result": result}


class ExportReq(BaseModel):
    reviews: list[dict]


@app.post("/api/export/excel")
def export_excel(req: ExportReq, user: dict = Depends(current_user)) -> StreamingResponse:
    data = export.to_excel(req.reviews)
    return StreamingResponse(io.BytesIO(data), media_type=_XLSX, headers={
        "Content-Disposition": 'attachment; filename="due_diligence.xlsx"'})


@app.post("/api/export/word")
def export_word(req: ExportReq, user: dict = Depends(current_user)) -> StreamingResponse:
    data = export.to_word(req.reviews)
    return StreamingResponse(io.BytesIO(data), media_type=_DOCX, headers={
        "Content-Disposition": 'attachment; filename="due_diligence.docx"'})


# ---------- library ----------
@app.get("/api/documents")
def documents(user: dict = Depends(current_user)) -> dict:
    allowed = user["allowed_clients"]
    where, params = "", []
    if allowed is not None:
        if not allowed:
            return {"documents": []}
        where, params = "WHERE client = ANY(%s)", [allowed]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, filename, doc_type, client, author,
                   to_char(doc_date,'YYYY-MM-DD'), n_pages, n_chunks, meta,
                   to_char(ingested_at,'YYYY-MM-DD'), stored_path, path
            FROM documents {where} ORDER BY ingested_at DESC, filename
        """, params)
        docs = []
        for r in cur.fetchall():
            meta = r[8] if isinstance(r[8], dict) else {}
            has_file = bool((r[10] and os.path.exists(r[10])) or (r[11] and os.path.exists(r[11])))
            docs.append({"id": r[0], "filename": r[1], "doc_type": r[2], "client": r[3],
                         "author": r[4], "doc_date": r[5], "n_pages": r[6], "n_chunks": r[7],
                         "parties": meta.get("parties", []), "ingested_at": r[9],
                         "has_file": has_file})
    return {"documents": docs}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int, user: dict = Depends(current_user)):
    """Serve the original file, enforcing the caller's client scope."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT filename, client, stored_path, path FROM documents WHERE id=%s",
                    (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    filename, client, stored_path, path = row
    allowed = user["allowed_clients"]
    if allowed is not None and client not in allowed:
        raise HTTPException(status_code=403, detail="not permitted")
    fpath = next((p for p in (stored_path, path) if p and os.path.exists(p)), None)
    if not fpath:
        raise HTTPException(status_code=404, detail="original file is not stored")
    media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(fpath, media_type=media, filename=filename,
                        content_disposition_type="inline")


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(require_admin)) -> dict:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
        conn.commit()
    auth.log(user["username"], "delete_document", str(doc_id))
    return {"ok": True}


# ---------- generation history ----------
@app.get("/api/generations")
def api_generations(user: dict = Depends(current_user)) -> dict:
    return {"generations": generations.list_for(user["allowed_clients"])}


@app.get("/api/generations/{gen_id}")
def api_generation_detail(gen_id: int, user: dict = Depends(current_user)) -> dict:
    g = generations.get(gen_id, user["allowed_clients"])
    if not g:
        raise HTTPException(status_code=404, detail="not found")
    auth.log(user["username"], "view_generation", str(gen_id))
    return g


def _get_generation_or_404(gen_id: int, user: dict) -> dict:
    g = generations.get(gen_id, user["allowed_clients"])
    if not g:
        raise HTTPException(status_code=404, detail="not found")
    return g


# The 8-K filing itself (clean, ready to finalize) and the review pack (separate,
# for counsel) are DISTINCT downloads — never combined in one file. Named after the
# actual contract/event date (not the internal generation id) to match how the
# firm's own filings/contracts are named, e.g. "2025-08-21 - 8-K Draft.docx".
@app.get("/api/generations/{gen_id}/export/word")
def export_generation_word(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.draft_to_word(g["result"])
    name = f"{export.filing_date_iso(g['result'])} - 8-K Draft.docx"
    return StreamingResponse(io.BytesIO(data), media_type=_DOCX, headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/generations/{gen_id}/export/pdf")
def export_generation_pdf(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.draft_to_pdf(g["result"])
    name = f"{export.filing_date_iso(g['result'])} - 8-K Draft.pdf"
    return StreamingResponse(io.BytesIO(data), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/generations/{gen_id}/export/review-word")
def export_review_word(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.review_to_word(g["result"])
    name = f"{export.filing_date_iso(g['result'])} - 8-K Draft - Review.docx"
    return StreamingResponse(io.BytesIO(data), media_type=_DOCX, headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/generations/{gen_id}/export/review-pdf")
def export_review_pdf(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.review_to_pdf(g["result"])
    name = f"{export.filing_date_iso(g['result'])} - 8-K Draft - Review.pdf"
    return StreamingResponse(io.BytesIO(data), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/generations/{gen_id}")
def delete_generation(gen_id: int, user: dict = Depends(current_user)) -> dict:
    """Remove a generated draft from History. Client-scoped like every other
    generation endpoint, so a user can only delete what they can already see."""
    if not generations.delete(gen_id, user["allowed_clients"]):
        raise HTTPException(status_code=404, detail="not found")
    auth.log(user["username"], "delete_generation", str(gen_id))
    return {"ok": True}


# ---------- user management (admin only) ----------
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "lawyer"
    clients: list[str] = []


class UpdateUser(BaseModel):
    role: str | None = None
    password: str | None = None
    clients: list[str] | None = None


@app.get("/api/users")
def api_users(user: dict = Depends(require_admin)) -> dict:
    return {"users": auth.list_users()}


@app.get("/api/clients")
def api_clients(user: dict = Depends(require_admin)) -> dict:
    return {"clients": [c for c, _ in clients.client_counts()]}


@app.post("/api/users")
def api_create_user(req: NewUser, user: dict = Depends(require_admin)) -> dict:
    if auth.user_exists(req.username):
        raise HTTPException(status_code=409, detail="user already exists")
    auth.create_user(req.username, req.password, req.role,
                     [clients.resolve(c) for c in req.clients])
    auth.log(user["username"], "create_user", req.username)
    return {"ok": True}


@app.put("/api/users/{username}")
def api_update_user(username: str, req: UpdateUser,
                    user: dict = Depends(require_admin)) -> dict:
    if not auth.user_exists(username):
        raise HTTPException(status_code=404, detail="no such user")
    if req.role is not None:
        auth.set_role(username, req.role)
    if req.password:
        auth.set_password(username, req.password)
    if req.clients is not None:
        auth.set_clients(username, [clients.resolve(c) for c in req.clients])
    auth.log(user["username"], "update_user", username)
    return {"ok": True}


@app.delete("/api/users/{username}")
def api_delete_user(username: str, user: dict = Depends(require_admin)) -> dict:
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    auth.delete_user(username)
    auth.log(user["username"], "delete_user", username)
    return {"ok": True}


# ---------- registrant profile (admin only) ----------
class RegistrantProfile(BaseModel):
    name: str | None = None
    state: str | None = None
    file_number: str | None = None
    irs_ein: str | None = None
    address: list[str] | None = None
    phone: str | None = None
    securities: list[list[str]] | None = None
    emerging_growth_company: bool | None = None
    signer_name: str | None = None
    signer_title: str | None = None


@app.get("/api/registrant")
def api_get_registrant(user: dict = Depends(require_admin)) -> dict:
    """The issuer profile used on the 8-K cover/signature — editable in-browser so a new
    company / a changed address / new officers needs no file edit or restart."""
    return {"registrant": export.load_registrant()}


@app.put("/api/registrant")
def api_put_registrant(req: RegistrantProfile, user: dict = Depends(require_admin)) -> dict:
    updated = export.save_registrant(req.model_dump(exclude_none=True))
    auth.log(user["username"], "update_registrant", updated.get("name", ""))
    return {"registrant": updated}


@app.get("/")
def index() -> Response:
    # Inject a cache-busting version (based on each asset's mtime) onto the CSS/JS
    # URLs so a browser always fetches the current file after a deploy, instead of
    # serving a stale cached copy — which otherwise makes layout/JS edits silently
    # not appear until a manual hard-refresh.
    html = (WEB / "index.html").read_text()
    for asset in ("style.css", "app.js"):
        try:
            v = int((WEB / asset).stat().st_mtime)
        except OSError:
            continue
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    return Response(content=html, media_type="text/html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
