"""Local web API for Law_RAG — search + contract review. Fully local, no egress.

Serves the static frontend (web/) and exposes:
  GET  /api/stats      -> counts + distinct filter values (clients / types / authors)
  POST /api/search     -> hybrid retrieval with optional metadata filters
  POST /api/summarize  -> due-diligence review of an uploaded contract
"""
from __future__ import annotations

import io
import mimetypes
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, clients, db, export, generations
from .config import ROOT
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


@app.get("/api/generations/{gen_id}/export/word")
def export_generation_word(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.draft_to_word(g["result"])
    return StreamingResponse(io.BytesIO(data), media_type=_DOCX, headers={
        "Content-Disposition": f'attachment; filename="8k-draft-{gen_id}.docx"'})


@app.get("/api/generations/{gen_id}/export/pdf")
def export_generation_pdf(gen_id: int, user: dict = Depends(current_user)) -> StreamingResponse:
    g = _get_generation_or_404(gen_id, user)
    data = export.draft_to_pdf(g["result"])
    return StreamingResponse(io.BytesIO(data), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="8k-draft-{gen_id}.pdf"'})


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
