"""WhatsApp chats and messages in SQLite. Written by webhook.py, read by the app and the agent tools."""

import os
import sqlite3
import time
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("WA_DB_PATH", "whatsapp.db")
WINDOW_SECONDS = 24 * 60 * 60   # Meta's customer service window
MAX_TEXT_CHARS = 4000
_STATUS_RANK = {"": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    wa_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    last_inbound_at REAL,
    unread INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    wa_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    kind TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (wa_id, ts);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # webhook writes while Streamlit reads
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def clean_wa_id(value) -> str:
    """WhatsApp ids are the phone number in digits only, e.g. 919876543210."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if 8 <= len(digits) <= 15 else ""


def _to_ts(value) -> float:
    try:
        ts = float(value)
        return ts if ts > 0 else time.time()
    except (TypeError, ValueError):
        return time.time()


def window_open(chat: dict | None) -> bool:
    last = (chat or {}).get("last_inbound_at")
    return bool(last) and time.time() - last < WINDOW_SECONDS


def _chat_dict(row) -> dict:
    chat = dict(row)
    chat["window_open"] = window_open(chat)
    return chat


# ---------- writing ----------
def save_inbound(msg_id: str, wa_id: str, name: str, text: str, ts=None, kind: str = "text") -> bool:
    """Save a received message. Returns False if it was a duplicate or invalid."""
    wa_id = clean_wa_id(wa_id)
    if not wa_id or not msg_id:
        return False
    ts = _to_ts(ts)
    with _connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO messages (id, wa_id, direction, kind, text, ts) VALUES (?, ?, 'in', ?, ?, ?)",
            (msg_id, wa_id, kind, (text or "")[:MAX_TEXT_CHARS], ts),
        )
        if cur.rowcount == 0:
            return False  # Meta delivered the same message twice
        c.execute(
            """INSERT INTO chats (wa_id, name, last_inbound_at, unread) VALUES (?, ?, ?, 1)
               ON CONFLICT(wa_id) DO UPDATE SET
                 name = CASE WHEN excluded.name != '' THEN excluded.name ELSE chats.name END,
                 last_inbound_at = MAX(COALESCE(chats.last_inbound_at, 0), excluded.last_inbound_at),
                 unread = chats.unread + 1""",
            (wa_id, (name or "")[:80], ts),
        )
    return True


def save_outbound(msg_id: str, wa_id: str, text: str) -> None:
    with _connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO messages (id, wa_id, direction, text, ts, status) VALUES (?, ?, 'out', ?, ?, 'sent')",
            (msg_id, clean_wa_id(wa_id), text[:MAX_TEXT_CHARS], time.time()),
        )


def update_status(msg_id: str, status: str, error: str = "") -> None:
    """Delivery receipts can arrive out of order, so never downgrade (read -> delivered)."""
    if not msg_id or status not in _STATUS_RANK:
        return
    with _connect() as c:
        row = c.execute("SELECT status FROM messages WHERE id = ? AND direction = 'out'", (msg_id,)).fetchone()
        if row and _STATUS_RANK[status] > _STATUS_RANK.get(row["status"], 0):
            c.execute("UPDATE messages SET status = ?, error = ? WHERE id = ?", (status, error[:200], msg_id))


def mark_read(wa_id: str) -> None:
    with _connect() as c:
        c.execute("UPDATE chats SET unread = 0 WHERE wa_id = ?", (clean_wa_id(wa_id),))


# ---------- reading ----------
def list_chats(limit: int = 10) -> list[dict]:
    limit = max(1, min(int(limit or 10), 50))
    with _connect() as c:
        rows = c.execute(
            """SELECT c.wa_id, c.name, c.last_inbound_at, c.unread,
                      (SELECT MAX(ts) FROM messages m WHERE m.wa_id = c.wa_id) AS last_ts
               FROM chats c ORDER BY last_ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_chat_dict(r) for r in rows]


def get_chat(wa_id: str) -> dict | None:
    with _connect() as c:
        row = c.execute("SELECT * FROM chats WHERE wa_id = ?", (clean_wa_id(wa_id),)).fetchone()
    return _chat_dict(row) if row else None


def get_messages(wa_id: str, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 50))
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE wa_id = ? ORDER BY ts DESC LIMIT ?",
            (clean_wa_id(wa_id), limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]  # oldest first


def sent_recently(wa_id: str, text: str, within_seconds: int) -> bool:
    with _connect() as c:
        row = c.execute(
            """SELECT 1 FROM messages WHERE wa_id = ? AND direction = 'out' AND text = ?
               AND ts > ? AND status != 'failed' LIMIT 1""",
            (clean_wa_id(wa_id), text, time.time() - within_seconds),
        ).fetchone()
    return row is not None