"""Database layer: connection, schema, and pgvector registration.

Schema (Phase 1):
  documents  — one row per source file, with firm metadata used for filtering
               (client / matter / doc_type / author / doc_date). Matter+client
               are the basis for future access control / ethical-wall isolation.
  chunks     — one row per text chunk, holding the embedding (pgvector) plus a
               generated tsvector for keyword (BM25-style) search.
"""
from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

from .config import CONFIG


def connect() -> psycopg.Connection:
    conn = psycopg.connect(CONFIG.pg_dsn)
    # register_vector needs the extension present; init_schema creates it first.
    try:
        register_vector(conn)
    except psycopg.errors.UndefinedObject:
        conn.rollback()
    return conn


SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256       TEXT UNIQUE NOT NULL,          -- dedupe: same file never ingested twice
    path         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    doc_type     TEXT,                          -- e.g. S-8, NDA, SPA, memo
    client       TEXT,                          -- for matter/client isolation
    matter       TEXT,
    author       TEXT,                          -- handling lawyer / partner
    doc_date     DATE,                          -- date of the document itself
    n_pages      INT,
    n_chunks     INT,
    meta         JSONB DEFAULT '{{}}'::jsonb,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    page         INT,                           -- source page (PDF), NULL for docx
    content      TEXT NOT NULL,
    embedding    VECTOR({CONFIG.embed_dim}),
    tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

-- Keyword (full-text) index for the BM25-style leg of hybrid search.
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
-- Path to the stored original file, so the Library can serve it back.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS stored_path TEXT;

-- Metadata indexes for fast filtering / "find the S-8 for client X".
CREATE INDEX IF NOT EXISTS documents_client_idx ON documents (client);
CREATE INDEX IF NOT EXISTS documents_doc_type_idx ON documents (doc_type);
CREATE INDEX IF NOT EXISTS documents_author_idx ON documents (author);

-- ===== Access control (ethical walls) =====
-- users: who may log in; role 'admin' sees everything, 'lawyer' is scoped.
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'lawyer',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- user_clients: the clients a (non-admin) user is permitted to access.
CREATE TABLE IF NOT EXISTS user_clients (
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client        TEXT NOT NULL,
    PRIMARY KEY (user_id, client)
);
-- sessions: server-side session tokens (survive restarts, expire).
CREATE TABLE IF NOT EXISTS sessions (
    token         TEXT PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
-- audit_log: who did what, when.
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT,
    action        TEXT,
    detail        TEXT,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- client_aliases: maps a normalized client-name key -> one canonical client name,
-- so "Richtech" and "Richtech Robotics Inc." resolve to the same client.
CREATE TABLE IF NOT EXISTS client_aliases (
    alias_key     TEXT PRIMARY KEY,
    canonical     TEXT NOT NULL
);

-- generations: history of AI-generated documents (currently: 8-K drafts), so
-- past output is browsable instead of ephemeral CLI/API output. Scoped by
-- client like everything else (ethical wall).
CREATE TABLE IF NOT EXISTS generations (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT NOT NULL,              -- '8k_draft' (room for more kinds later)
    source_name   TEXT,
    client        TEXT,
    item          TEXT,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    result        JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS generations_client_idx ON generations (client);
"""

# Vector ANN index — created after data exists so HNSW builds well. Kept separate
# so init is cheap on an empty DB; ingest calls ensure_vector_index() at the end.
VECTOR_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
ON chunks USING hnsw (embedding vector_cosine_ops);
"""


def init_schema() -> None:
    """Create the extension, tables, and non-vector indexes (idempotent)."""
    with psycopg.connect(CONFIG.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def ensure_vector_index() -> None:
    with psycopg.connect(CONFIG.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(VECTOR_INDEX_SQL)
        conn.commit()
