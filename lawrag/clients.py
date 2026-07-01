"""Client-name normalization so one real client isn't split across name variants.

Two layers:
  1. Deterministic key: lowercased, punctuation dropped, legal-entity suffixes
     removed. This auto-merges trivial variants ("Acme Corp." == "acme corporation").
  2. Explicit aliases (client_aliases table): for variants a machine can't infer
     ("Richtech" vs "Richtech Robotics Inc."), an admin declares the mapping via
     `merge()`. Aliases are keyed by the normalized key.

resolve() runs at ingest time so documents are stored under the canonical name;
grants should be resolved the same way so access lines up.
"""
from __future__ import annotations

import re

from . import db

# Legal-entity suffix tokens stripped when building the match key.
_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "company",
    "corp", "corporation", "plc", "gmbh", "ag", "sa", "nv", "bv", "pllc", "pc",
}


def normalize_key(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def resolve(raw: str | None) -> str | None:
    """Return the canonical client name for a raw name (or the trimmed raw name)."""
    if not raw:
        return raw
    key = normalize_key(raw)
    if not key:
        return raw.strip()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT canonical FROM client_aliases WHERE alias_key=%s", (key,))
        row = cur.fetchone()
    return row[0] if row else raw.strip()


def add_alias(alias_raw: str, canonical: str) -> None:
    """Map alias_raw's key -> canonical (and make canonical resolve to itself)."""
    with db.connect() as conn, conn.cursor() as cur:
        for raw in (alias_raw, canonical):
            key = normalize_key(raw)
            if key:
                cur.execute(
                    "INSERT INTO client_aliases (alias_key, canonical) VALUES (%s,%s) "
                    "ON CONFLICT (alias_key) DO UPDATE SET canonical=EXCLUDED.canonical",
                    (key, canonical))
        conn.commit()


def merge(from_name: str, into_name: str) -> dict:
    """Declare from_name an alias of into_name and rewrite existing docs + grants."""
    add_alias(from_name, into_name)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE documents SET client=%s WHERE client=%s", (into_name, from_name))
        docs = cur.rowcount
        cur.execute("UPDATE user_clients SET client=%s WHERE client=%s",
                    (into_name, from_name))
        grants = cur.rowcount
        conn.commit()
    return {"documents_updated": docs, "grants_updated": grants}


def client_counts() -> list[tuple[str, int]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT client, count(*) FROM documents "
                    "WHERE client IS NOT NULL GROUP BY client ORDER BY client")
        return [(r[0], r[1]) for r in cur.fetchall()]
