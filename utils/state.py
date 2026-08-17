import threading
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get(
    "KAI_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", "kai_memory.db"),
)


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_state_table():
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _save_state(key: str, value):
    _init_state_table()
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, json.dumps(value), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def _load_state(key: str, default):
    _init_state_table()
    conn = _get_db()
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default

class AgentState:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._data_lock = threading.RLock()
                    cls._instance._data = {
                        "status": _load_state("status", "idle"),
                        "suggestions": _load_state("suggestions", []),
                        "account": {},
                        "positions": [],
                        "history": _load_state("history", []),
                    }
        return cls._instance

    def get_status(self):
        with self._data_lock:
            return self._data["status"]

    def set_status(self, s):
        with self._data_lock:
            self._data["status"] = s
            _save_state("status", s)

    def get_latest_suggestions(self):
        with self._data_lock:
            return list(self._data["suggestions"])

    def set_latest_suggestions(self, s):
        with self._data_lock:
            self._data["suggestions"] = list(s or [])
            _save_state("suggestions", self._data["suggestions"])

    def get_pending_suggestions(self):
        with self._data_lock:
            return [s for s in self._data["suggestions"] if s.get("status") == "PENDING_APPROVAL"]

    def get_account(self):
        with self._data_lock:
            return dict(self._data["account"])

    def set_account(self, a):
        with self._data_lock:
            self._data["account"] = dict(a or {})

    def get_positions(self):
        with self._data_lock:
            return list(self._data["positions"])

    def set_positions(self, p):
        with self._data_lock:
            self._data["positions"] = list(p or [])

    def get_trade_history(self):
        with self._data_lock:
            return list(self._data["history"])

    def add_to_history(self, t):
        with self._data_lock:
            self._data["history"].insert(0, t)
            self._data["history"] = self._data["history"][:300]
            _save_state("history", self._data["history"])

    def update_suggestion_status(self, symbol, status, extra=None):
        with self._data_lock:
            for s in self._data["suggestions"]:
                if s.get("symbol") == symbol:
                    s["status"] = status
                    if extra:
                        s.update(extra)
                    break
            _save_state("suggestions", self._data["suggestions"])
