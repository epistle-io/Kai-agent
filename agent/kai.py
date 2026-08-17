"""agent/kai.py — KAI personality (token-efficient)"""
from utils.groq_client import chat_fast
from utils.logger import log

KAI_SYSTEM = """You are KAI — a sharp, confident personal AI trading assistant.
Be direct and concise. For trade signals: give specific entry, SL, TP.
For chat: be helpful and brief. Never say "I cannot" — always find a way."""


def _prepare_history(history: list, max_chars: int = 5000, max_items: int = 8) -> list:
    """Keep recent valid turns only to avoid payload/validation issues."""
    cleaned = []
    remaining = max_chars

    for item in reversed((history or [])[-20:]):
        role = (item or {}).get("role")
        content = (item or {}).get("content")
        if role not in {"user", "assistant"}:
            continue
        if content is None:
            continue
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            continue

        if len(content) > remaining:
            if remaining < 120:
                break
            content = content[:remaining]
        cleaned.append({"role": role, "content": content})
        remaining -= len(content)

        if len(cleaned) >= max_items or remaining <= 0:
            break

    cleaned.reverse()
    return cleaned


def _local_fallback(message: str, context: dict = None) -> str:
    msg = (message or "").lower()
    account = (context or {}).get("account") or {}
    signals = (context or {}).get("latest_signals") or []

    if "account" in msg or "balance" in msg or "equity" in msg:
        bal = account.get("balance")
        eq = account.get("equity")
        cur = account.get("currency", "USD")
        if bal is not None:
            return f"Quick account snapshot: balance {cur} {bal}, equity {eq if eq is not None else bal}."
        return "I can’t read the account snapshot right now, but the server is online. Pull to refresh dashboard in a few seconds."

    if "signal" in msg or "market" in msg or "trade" in msg:
        if signals:
            top = signals[:3]
            summary = ", ".join([
                f"{s.get('symbol','?')} {s.get('signal','WAIT')} ({s.get('confidence','?')}/10)"
                for s in top
            ])
            return f"Latest signal snapshot: {summary}."
        return "No fresh signal snapshot yet. Trigger a scan and I will summarize immediately."

    return "I’m online, but my AI provider is temporarily busy. You can still use dashboard, signals, and positions while I reconnect."

def ask_kai(message: str, history: list, context: dict = None) -> str:
    ctx = ""
    if context:
        acct = context.get("account", {})
        if acct.get("balance"):
            ctx += f"\nBalance: ${acct.get('balance')} {acct.get('currency','USD')}"
        signals = context.get("latest_signals", [])
        if signals:
            ctx += f"\nSignals: {signals}"
    system = KAI_SYSTEM + (f"\nContext:{ctx}" if ctx else "")
    history_turns = _prepare_history(history)
    messages = [{"role":"system","content":system}] + history_turns + [{"role":"user","content":message}]
    try:
        return chat_fast(messages, temperature=0.7, max_tokens=220)
    except Exception as e:
        log("error", f"KAI chat error: {e}")
        return _local_fallback(message, context)

def generate_trade_alert(symbol: str, suggestion: dict) -> str:
    signal = suggestion.get("signal","")
    conf   = suggestion.get("confidence", 0)
    entry  = suggestion.get("entry_price", 0)
    sl     = suggestion.get("stop_loss_pips", 0)
    tp     = suggestion.get("take_profit_pips", 0)
    rr     = suggestion.get("risk_reward","")
    prompt = f"Write a 2-sentence trade alert as KAI: {signal} {symbol}, confidence {conf}/10, entry {entry}, SL {sl} pips, TP {tp} pips, R:R {rr}. Be direct."
    try:
        return chat_fast([{"role":"user","content":prompt}], temperature=0.6, max_tokens=80)
    except:
        return f"{signal} on {symbol} — {conf}/10 confidence. Entry {entry}, SL {sl} pips, TP {tp} pips."

def generate_no_trade_update(symbol: str, suggestion: dict) -> str:
    signal = suggestion.get("signal","WAIT")
    conf   = suggestion.get("confidence", 0)
    reason = suggestion.get("reasoning", "")
    msg    = f"Checked {symbol} — no setup ({signal}, {conf}/10)"
    if reason:
        msg += f". {reason}"
    return msg + "."
