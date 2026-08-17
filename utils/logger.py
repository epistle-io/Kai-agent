import logging
import json
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar

logger = logging.getLogger("kai")
logger.setLevel(logging.INFO)
logger.propagate = False

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "kai.log")

_cycle_id = ContextVar("cycle_id", default="-")

if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

_recent_logs = []


def set_cycle_id(cycle_id: str):
    _cycle_id.set(cycle_id or "-")


def get_cycle_id() -> str:
    return _cycle_id.get()

def log(level: str, message: str):
    cid = _cycle_id.get()
    prefixed = f"[cid:{cid}] {message}" if cid and cid != "-" else message
    entry = {
        "time": datetime.now().isoformat(),
        "level": level.upper(),
        "cycle_id": cid,
        "message": prefixed,
    }
    _recent_logs.append(entry)
    if len(_recent_logs) > 500:
        _recent_logs.pop(0)
    getattr(logger, level.lower(), logger.info)(prefixed)

def get_recent_logs(n: int = 50):
    return _recent_logs[-n:]

def save_report(name: str, data: dict):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOGS_DIR, f"{name}_{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log("error", f"Save report failed: {e}")

def get_recent_reports(n: int = 10):
    try:
        files = sorted(os.listdir(LOGS_DIR), reverse=True)[:n]
        reports = []
        for f in files:
            with open(os.path.join(LOGS_DIR, f)) as fp:
                reports.append(json.load(fp))
        return reports
    except:
        return []
