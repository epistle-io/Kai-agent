import logging
import json
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("kai")
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

_recent_logs = []

def log(level: str, message: str):
    entry = {"time": datetime.now().isoformat(), "level": level.upper(), "message": message}
    _recent_logs.append(entry)
    if len(_recent_logs) > 500:
        _recent_logs.pop(0)
    getattr(logger, level.lower(), logger.info)(message)

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
