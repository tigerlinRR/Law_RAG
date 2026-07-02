"""History of AI-generated documents (currently: 8-K drafts) so past output is
browsable instead of scattered across ephemeral CLI/API output. Scoped by
client like every other retrieval path (ethical wall)."""
from __future__ import annotations

import json

from . import db


def save(kind: str, result: dict, *, source_name: str | None = None,
         client: str | None = None, item: str | None = None,
         created_by: str | None = None) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO generations (kind, source_name, client, item, created_by, result)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (kind, source_name, client, item, created_by, json.dumps(result)),
        )
        gen_id = cur.fetchone()[0]
        conn.commit()
    return gen_id


def list_for(allowed_clients: list[str] | None) -> list[dict]:
    """`allowed_clients`: None = unrestricted (admin); a list = hard limit (ethical wall)."""
    where, params = "", []
    if allowed_clients is not None:
        if not allowed_clients:
            return []
        where, params = "WHERE client = ANY(%s)", [allowed_clients]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, kind, source_name, client, item, created_by,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI')
            FROM generations {where}
            ORDER BY created_at DESC
        """, params)
        return [
            {"id": r[0], "kind": r[1], "source_name": r[2], "client": r[3],
             "item": r[4], "created_by": r[5], "created_at": r[6]}
            for r in cur.fetchall()
        ]


def get(gen_id: int, allowed_clients: list[str] | None) -> dict | None:
    """Returns None if not found OR outside the caller's allowed clients."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, kind, source_name, client, item, created_by,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI'), result
            FROM generations WHERE id=%s
        """, (gen_id,))
        row = cur.fetchone()
    if not row:
        return None
    if allowed_clients is not None and row[3] not in allowed_clients:
        return None
    return {"id": row[0], "kind": row[1], "source_name": row[2], "client": row[3],
            "item": row[4], "created_by": row[5], "created_at": row[6], "result": row[7]}
