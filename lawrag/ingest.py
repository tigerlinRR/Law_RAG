"""Ingestion pipeline: file -> parse -> chunk -> embed -> store.

Idempotent: a file's SHA-256 is checked first, so re-running never duplicates.
Scanned PDFs (no extractable text) are skipped and reported (OCR is Phase 2).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import clients, db, embed
from .chunk import chunk_blocks
from .config import CONFIG
from .metadata import extract_metadata
from .parsers import NeedsOCR, SUPPORTED, parse


@dataclass
class DocMeta:
    doc_type: str | None = None
    client: str | None = None
    matter: str | None = None
    author: str | None = None
    doc_date: str | None = None  # ISO 'YYYY-MM-DD'
    extra: dict = field(default_factory=dict)


@dataclass
class IngestResult:
    path: Path
    status: str            # "ingested" | "skipped_duplicate" | "needs_ocr" | "error"
    n_chunks: int = 0
    detail: str = ""
    # metadata stored (auto-extracted unless the caller set it explicitly)
    doc_type: str | None = None
    client: str | None = None
    doc_date: str | None = None
    parties: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _store_original(src: Path, digest: str) -> str:
    """Copy the original file into managed storage (keyed by hash) and return its path."""
    dest_dir = Path(CONFIG.storage_dir) / digest[:2]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{digest}{src.suffix.lower()}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return str(dest)


def ingest_file(path: Path, meta: DocMeta | None = None, auto: bool = False) -> IngestResult:
    meta = meta or DocMeta()
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED:
        return IngestResult(path, "error", detail=f"unsupported type {path.suffix}")

    digest = _sha256(path)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE sha256=%s", (digest,))
            if cur.fetchone():
                return IngestResult(path, "skipped_duplicate")

    # Parse (may flag scanned PDF).
    try:
        blocks = parse(path)
    except NeedsOCR as e:
        return IngestResult(path, "needs_ocr", detail=str(e))
    except Exception as e:  # noqa: BLE001 — report and continue with the batch
        return IngestResult(path, "error", detail=repr(e))

    # Auto-extract metadata for any field the caller left unset.
    if auto:
        head = "\n\n".join(b.text for b in blocks)
        try:
            md = extract_metadata(head, path.name)
            meta.doc_type = meta.doc_type or (md.get("doc_type") or None)
            meta.client = meta.client or (md.get("client") or None)
            meta.doc_date = meta.doc_date or (md.get("doc_date") or None)
            if md.get("parties"):
                meta.extra.setdefault("parties", md["parties"])
            if md.get("title"):
                meta.extra.setdefault("title", md["title"])
            meta.extra["auto"] = True
        except Exception as e:  # noqa: BLE001 — metadata is best-effort, don't fail ingest
            meta.extra["auto_error"] = str(e)

    # Canonicalize the client name so variants map to one client.
    if meta.client:
        meta.client = clients.resolve(meta.client)

    chunks = chunk_blocks(blocks)
    if not chunks:
        return IngestResult(path, "error", detail="no text after chunking")

    vectors = embed.embed_documents([c.content for c in chunks])
    n_pages = max((b.page or 0 for b in blocks), default=0) or None
    stored_path = _store_original(path, digest)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (sha256, path, filename, doc_type, client, matter, author,
                     doc_date, n_pages, n_chunks, meta, stored_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    digest, str(path.resolve()), path.name, meta.doc_type,
                    meta.client, meta.matter, meta.author, meta.doc_date,
                    n_pages, len(chunks), json.dumps(meta.extra), stored_path,
                ),
            )
            doc_id = cur.fetchone()[0]
            cur.executemany(
                """
                INSERT INTO chunks (document_id, chunk_index, page, content, embedding)
                VALUES (%s,%s,%s,%s,%s)
                """,
                [(doc_id, c.chunk_index, c.page, c.content, v)
                 for c, v in zip(chunks, vectors)],
            )
        conn.commit()

    return IngestResult(path, "ingested", n_chunks=len(chunks),
                        doc_type=meta.doc_type, client=meta.client,
                        doc_date=meta.doc_date, parties=meta.extra.get("parties", []))


def iter_files(root: Path) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED)
