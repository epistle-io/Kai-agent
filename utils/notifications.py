import os
import httpx
from utils.logger import log
from memory.system_store import get_active_push_tokens, disable_push_token

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def _get_tokens():
    tokens = get_active_push_tokens()
    # Backward compatibility for existing environments.
    env_token = os.getenv("EXPO_PUSH_TOKEN", "").strip()
    if env_token and env_token not in tokens:
        tokens.append(env_token)
    return tokens

def send_push(title: str, body: str, data: dict = None):
    tokens = _get_tokens()
    if not tokens:
        log("warning", "No EXPO_PUSH_TOKEN set — push notification skipped")
        return
    for token in tokens:
        try:
            res = httpx.post(
                EXPO_PUSH_URL,
                json={
                    "to": token,
                    "title": title,
                    "body": body,
                    "data": data or {},
                    "sound": "default",
                    "priority": "high",
                },
                timeout=10,
            )
            payload = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
            details = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(details, dict) and details.get("status") == "error":
                msg = str(details.get("message", ""))
                log("warning", f"Push token issue: {msg}")
                if "DeviceNotRegistered" in msg:
                    disable_push_token(token)
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
    
def send_signal_to_subscribers(message: str):
    """Send signal to all active subscriber chat IDs."""
    chat_ids = os.getenv("SUBSCRIBER_CHAT_IDS", "")
    if not chat_ids:
        log("warning", "No subscriber chat IDs configured")
        return
    
    ids = [cid.strip() for cid in chat_ids.split(",") if cid.strip()]
    
    for chat_id in ids:
        try:
            response = httpx.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=10
            )
            if response.status_code == 200:
                log("info", f"Signal sent to {chat_id}")
            else:
                log("error", f"Failed to send to {chat_id}: {response.text}")
        except Exception as e:
            log("error", f"Telegram send error for {chat_id}: {e}")


def get_user_chat_id(username_or_message: str) -> str:
    """Helper — paste a user's first message here to extract their chat ID."""
    pass
