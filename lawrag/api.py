"""Local web API for Law_RAG — search + contract review. Fully local, no egress.

Serves the static frontend (web/) and exposes:
  GET  /api/stats      -> counts + distinct filter values (clients / types / authors)
  POST /api/search     -> hybrid retrieval with optional metadata filters
  POST /api/summarize  -> due-diligence review of an uploaded contract
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import ROOT
from .ingest import DocMeta, ingest_file
from .parsers import NeedsOCR
from .retrieve import Filters, search
from .summarize import review_contract

WEB = ROOT / "web"
app = FastAPI(title="Law_RAG", docs_url="/api/docs")


class SearchReq(BaseModel):
    query: str
    client: str | None = None
    matter: str | None = None
    doc_type: str | None = None
    author: str | None = None
    top_k: int | None = None
    rerank: bool = True


@app.get("/api/stats")
def stats() -> dict:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents")
        docs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM chunks")
        chunks = cur.fetchone()[0]

        def distinct(col: str) -> list[str]:
            cur.execute(
                f"SELECT DISTINCT {col} FROM documents "
                f"WHERE {col} IS NOT NULL AND {col} <> '' ORDER BY 1"
            )
            return [r[0] for r in cur.fetchall()]

        return {
            "documents": docs, "chunks": chunks,
            "clients": distinct("client"),
            "doc_types": distinct("doc_type"),
            "authors": distinct("author"),
        }


@app.post("/api/search")
def api_search(req: SearchReq) -> dict:
    filters = Filters(client=req.client or None, matter=req.matter or None,
                      doc_type=req.doc_type or None, author=req.author or None)
    hits = search(req.query, filters=filters, top_k=req.top_k, use_rerank=req.rerank)
    out = []
    for h in hits:
        d = asdict(h)
        d["citation"] = h.citation()
        out.append(d)
    return {"hits": out, "reranked": bool(hits) and hits[0].reranked}


@app.post("/api/summarize", response_model=None)
async def api_summarize(file: UploadFile = File(...)):
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
async def api_ingest(files: list[UploadFile] = File(...)):
    """Ingest uploaded files into the knowledge base with auto-extracted metadata."""
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
