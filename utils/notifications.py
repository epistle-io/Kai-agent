import os
import httpx
from utils.logger import log

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

def _get_token():
    return os.getenv("EXPO_PUSH_TOKEN", "")

def send_push(title: str, body: str, data: dict = None):
    token = _get_token()
    if not token:
        log("warning", "No EXPO_PUSH_TOKEN set — push notification skipped")
        return
    try:
        httpx.post(EXPO_PUSH_URL, json={
            "to": token, "title": title, "body": body,
            "data": data or {}, "sound": "default",
            "priority": "high",
        }, timeout=10)
    except Exception as e:
        log("error", f"Push notification failed: {e}")

def send_trade_alert(symbol: str, signal: str, message: str, suggestion: dict):
    emoji = "🟢" if signal == "BUY" else "🔴"
    send_push(
        title=f"{emoji} KAI Signal — {symbol}",
        body=message[:100],
        data={"type": "trade_signal", "symbol": symbol, "signal": signal},
    )

def send_general_message(message: str):
    send_push(title="KAI", body=message)
