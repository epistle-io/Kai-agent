"""utils/metrics.py - Lightweight in-memory metrics for API observability."""
import threading
from collections import deque

_lock = threading.Lock()
_data = {
    "cycles_total": 0,
    "signals_generated": 0,
    "pending_signals": 0,
    "no_trade_signals": 0,
    "approvals_total": 0,
    "executions_success": 0,
    "executions_failed": 0,
}
_broker_latencies = deque(maxlen=200)


def record_cycle(suggestions: list):
    suggestions = suggestions or []
    with _lock:
        _data["cycles_total"] += 1
        _data["signals_generated"] += len(suggestions)
        _data["pending_signals"] += sum(1 for s in suggestions if s.get("status") == "PENDING_APPROVAL")
        _data["no_trade_signals"] += sum(1 for s in suggestions if s.get("status") == "NO_TRADE")


def record_approval_attempt():
    with _lock:
        _data["approvals_total"] += 1


def record_execution_result(success: bool, broker_latency_ms: float = None):
    with _lock:
        if success:
            _data["executions_success"] += 1
        else:
            _data["executions_failed"] += 1
        if broker_latency_ms is not None:
            _broker_latencies.append(float(broker_latency_ms))


def snapshot() -> dict:
    with _lock:
        success = _data["executions_success"]
        failed = _data["executions_failed"]
        total_exec = success + failed
        success_rate = round((success / total_exec) * 100, 2) if total_exec else 0.0
        avg_latency = round(sum(_broker_latencies) / len(_broker_latencies), 2) if _broker_latencies else None
        return {
            **_data,
            "execution_success_rate": success_rate,
            "avg_broker_latency_ms": avg_latency,
        }
