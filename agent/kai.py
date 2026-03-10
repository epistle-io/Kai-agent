"""agent/kai.py — KAI's personality and language layer"""
from utils.groq_client import chat
from utils.logger import log

KAI_SYSTEM = """You are KAI — a sharp, confident personal AI assistant and trading analyst.
You have a casual but intelligent personality. You're direct, never vague.
You help with: trading signals, market analysis, account management, and general personal tasks.
When discussing trades, be specific about entry, stop loss, and take profit.
When chatting casually, be warm and helpful. Keep responses concise but complete.
You never say "I cannot" — you always find a way to help."""

def ask_kai(message: str, history: list, context: dict = None) -> str:
    ctx = ""
    if context:
        acct = context.get("account", {})
        if acct.get("balance"):
            ctx += f"\nAccount: ${acct.get('balance')} balance, {acct.get('currency','USD')}"
        signals = context.get("latest_signals", [])
        if signals:
            ctx += f"\nLatest signals: {signals}"
    system = KAI_SYSTEM + (f"\n\nCurrent context:{ctx}" if ctx else "")
    messages = [{"role":"system","content":system}] + history[-20:] + [{"role":"user","content":message}]
    try:
        return chat(messages, temperature=0.7, max_tokens=600)
    except Exception as e:
        log("error", f"KAI chat error: {e}")
        return "Having trouble connecting to my AI right now. Check that the Groq API key is set correctly."

def generate_trade_alert(symbol: str, suggestion: dict) -> str:
    signal    = suggestion.get("signal","")
    conf      = suggestion.get("confidence", 0)
    analysis  = suggestion.get("analysis","")
    reasoning = suggestion.get("reasoning","")
    entry     = suggestion.get("entry_price", 0)
    sl_pips   = suggestion.get("stop_loss_pips", 0)
    tp_pips   = suggestion.get("take_profit_pips", 0)
    rr        = suggestion.get("risk_reward","")
    prompt = f"""Write a short, punchy trade alert message as KAI for this setup:
Symbol: {symbol} | Signal: {signal} | Confidence: {conf}/10
Analysis: {analysis} | Reasoning: {reasoning}
Entry: {entry} | SL: {sl_pips} pips | TP: {tp_pips} pips | R:R {rr}
Be direct, confident, max 3 sentences. No emojis except one at the start."""
    try:
        return chat([{"role":"user","content":prompt}], temperature=0.6, max_tokens=150)
    except:
        return f"{signal} signal on {symbol} — confidence {conf}/10. Entry at {entry}."

def generate_no_trade_update(symbol: str, suggestion: dict) -> str:
    signal = suggestion.get("signal","WAIT")
    conf   = suggestion.get("confidence",0)
    analysis = suggestion.get("analysis","")
    prompt = f"""KAI checked {symbol} and found no trade setup (signal: {signal}, confidence {conf}/10).
Analysis: {analysis}
Write one short sentence saying you checked and there's nothing to act on right now. Be casual."""
    try:
        return chat([{"role":"user","content":prompt}], temperature=0.5, max_tokens=80)
    except:
        return f"Checked {symbol} — no setup right now ({conf}/10 confidence)."
