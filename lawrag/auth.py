"""Access control: users, per-client permissions (ethical walls), sessions, audit.

Enforcement model:
  * role 'admin'  -> allowed_clients is None  -> sees everything
  * role 'lawyer' -> allowed_clients is the explicit list from user_clients; the
    retrieval/stats layer MUST restrict to `client = ANY(allowed_clients)`. An empty
    list means the lawyer can see nothing until granted access.

Passwords use PBKDF2-HMAC-SHA256 (stdlib, no extra deps). Sessions are random
tokens stored server-side with an expiry. This is application-layer isolation;
transport security (TLS/SSO) is a separate deployment concern.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from . import db

SESSION_HOURS = 12
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return salt, h.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, expected_hash)


# ---------- users ----------
def create_user(username: str, password: str, role: str = "lawyer",
                clients: list[str] | None = None) -> None:
    salt, ph = hash_password(password)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, salt, password_hash, role) VALUES (%s,%s,%s,%s) "
            "RETURNING id", (username, salt, ph, role))
        uid = cur.fetchone()[0]
        for c in clients or []:
            cur.execute("INSERT INTO user_clients (user_id, client) VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", (uid, c))
        conn.commit()


def set_password(username: str, password: str) -> None:
    salt, ph = hash_password(password)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET salt=%s, password_hash=%s WHERE username=%s",
                    (salt, ph, username))
        conn.commit()


def grant(username: str, client: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"no such user: {username}")
        cur.execute("INSERT INTO user_clients (user_id, client) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING", (row[0], client))
        conn.commit()


def revoke(username: str, client: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_clients uc USING users u "
                    "WHERE uc.user_id=u.id AND u.username=%s AND uc.client=%s",
                    (username, client))
        conn.commit()


def list_users() -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.username, u.role,
                   COALESCE(array_agg(uc.client) FILTER (WHERE uc.client IS NOT NULL), '{}')
            FROM users u LEFT JOIN user_clients uc ON uc.user_id=u.id
            GROUP BY u.username, u.role ORDER BY u.username
        """)
        return [{"username": r[0], "role": r[1], "clients": list(r[2])}
                for r in cur.fetchall()]


def _allowed_clients(cur, user_id: int, role: str) -> list[str] | None:
    if role == "admin":
        return None  # unrestricted
    cur.execute("SELECT client FROM user_clients WHERE user_id=%s ORDER BY client", (user_id,))
    return [r[0] for r in cur.fetchall()]


# ---------- authentication / sessions ----------
def authenticate(username: str, password: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, salt, password_hash, role FROM users WHERE username=%s",
                    (username,))
        row = cur.fetchone()
        if not row or not verify_password(password, row[1], row[2]):
            return None
        return {"id": row[0], "username": username, "role": row[3],
                "allowed_clients": _allowed_clients(cur, row[0], row[3])}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token, user_id, expires_at) "
            "VALUES (%s,%s, now() + make_interval(hours => %s))",
            (token, user_id, SESSION_HOURS))
        conn.commit()
    return token


def resolve_session(token: str | None) -> dict | None:
    if not token:
        return None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.id, u.username, u.role
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token=%s AND s.expires_at > now()
        """, (token,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "role": row[2],
                "allowed_clients": _allowed_clients(cur, row[0], row[2])}


def delete_session(token: str | None) -> None:
    if not token:
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
        conn.commit()


def log(username: str | None, action: str, detail: str = "") -> None:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO audit_log (username, action, detail) VALUES (%s,%s,%s)",
                        (username, action, detail[:2000]))
            conn.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the request
        pass
