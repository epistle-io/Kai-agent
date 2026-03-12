"""agent/kai.py — KAI personality (token-efficient)"""
from utils.groq_client import chat
from utils.logger import log

KAI_SYSTEM = """You are KAI — a sharp, confident personal AI trading assistant.
Be direct and concise. For trade signals: give specific entry, SL, TP.
For chat: be helpful and brief. Never say "I cannot" — always find a way."""

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
    messages = [{"role":"system","content":system}] + history[-10:] + [{"role":"user","content":message}]
    try:
        return chat(messages, temperature=0.7, max_tokens=400)
    except Exception as e:
        log("error", f"KAI chat error: {e}")
        return "Having trouble connecting right now. Try again in a moment."

def generate_trade_alert(symbol: str, suggestion: dict) -> str:
    signal = suggestion.get("signal","")
    conf   = suggestion.get("confidence", 0)
    entry  = suggestion.get("entry_price", 0)
    sl     = suggestion.get("stop_loss_pips", 0)
    tp     = suggestion.get("take_profit_pips", 0)
    rr     = suggestion.get("risk_reward","")
    # Keep it short — save tokens
    prompt = f"Write a 2-sentence trade alert as KAI: {signal} {symbol}, confidence {conf}/10, entry {entry}, SL {sl} pips, TP {tp} pips, R:R {rr}. Be direct."
    try:
        return chat([{"role":"user","content":prompt}], temperature=0.6, max_tokens=100)
    except:
        return f"{signal} on {symbol} — {conf}/10 confidence. Entry {entry}, SL {sl} pips, TP {tp} pips."

def generate_no_trade_update(symbol: str, suggestion: dict) -> str:
    # Don't call AI for no-trade updates — just return a static message
    # This saves ~200 tokens per symbol per cycle
    signal = suggestion.get("signal","WAIT")
    conf   = suggestion.get("confidence", 0)
    return f"Checked {symbol} — no setup right now ({signal}, {conf}/10)."
