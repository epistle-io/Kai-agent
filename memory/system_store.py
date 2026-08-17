"""memory/system_store.py - Persistent app settings and push token storage."""
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get(
    "KAI_DB_PATH",
    os.path.join(os.path.dirname(__file__), "kai_memory.db"),
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS push_tokens (
            token TEXT PRIMARY KEY,
            active INTEGER DEFAULT 1,
            added_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
    """
    )
    conn.commit()
    conn.close()


def store_push_token(token: str):
    init_tables()
    token = (token or "").strip()
    if not token:
        return False
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        """
        INSERT INTO push_tokens (token, active, added_at, updated_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(token) DO UPDATE SET
            active=1,
            updated_at=excluded.updated_at
        """,
        (token, now, now),
    )
    conn.commit()
    conn.close()
    return True


def get_active_push_tokens():
    init_tables()
    conn = get_db()
    rows = conn.execute(
        "SELECT token FROM push_tokens WHERE active=1 ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [r["token"] for r in rows]


def disable_push_token(token: str):
    init_tables()
    conn = get_db()
    conn.execute(
        "UPDATE push_tokens SET active=0, updated_at=? WHERE token=?",
        (datetime.now().isoformat(), token),
    )
    conn.commit()
    conn.close()


def set_setting(key: str, value):
    init_tables()
    now = datetime.now().isoformat()
    encoded = json.dumps(value)
    conn = get_db()
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, encoded, now),
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    init_tables()
    conn = get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default
