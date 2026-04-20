"""agent/trading_brain.py — Multi-timeframe market analysis with reasoning model"""
import json
from utils.groq_client import chat
from utils.logger import log, save_report

ANALYSIS_PROMPT = """You are a professional forex/crypto trader with 10+ years experience. Real money is on the line.

You will receive market data across 3 timeframes. Reason through them in order before deciding:
1. H4 (Trend): What is the major trend? Is price respecting key EMAs? Above or below EMA50?
2. H1 (Confirmation): Does momentum confirm the H4 direction? MACD and RSI aligned?
3. M5 (Entry): Is there a clean entry signal? Is price near support/resistance? Good R:R?

After your reasoning return ONLY this JSON — no extra text, no markdown:
{"signal":"BUY|SELL|WAIT","confidence":1-10,"analysis":"2 sentences describing the setup","reasoning":"specific technical reason for the signal","entry_price":0.0,"stop_loss_pips":0,"take_profit_pips":0,"risk_reward":"1:2","warnings":"key risk or NONE"}

Strict rules:
- Signal BUY/SELL ONLY when: H4 trend aligns + H1 confirms + M5 gives entry + confidence >= 7
- If ANY timeframe contradicts the others, signal = WAIT
- If near key resistance (BUY) or support (SELL), reduce confidence or WAIT
- SL should be beyond nearest swing level, not arbitrary
- Minimum R:R of 1:1.5 — otherwise WAIT"""


def build_tf_summaries(tf_dfs: dict, symbol: str) -> dict:
    """Build market summaries for each timeframe, including swing S/R levels."""
    from mt5.indicators import get_market_summary, get_swing_levels
    summaries = {}
    for tf_label, df in tf_dfs.items():
        if df is not None and not df.empty:
            summary = get_market_summary(df, symbol)
            summary["swing"] = get_swing_levels(df)
            summaries[tf_label] = summary
    return summaries


def _has_confluence(summaries: dict) -> tuple[bool, str]:
    """Hard check: H4 and H1 trends must agree before we call the AI.
    Returns (passes, reason_if_blocked)."""
    h4 = summaries.get("H4", {})
    h1 = summaries.get("H1", {})
    if not h4 or not h1:
        return True, ""  # missing data — let AI decide
    if h4.get("trend") != h1.get("trend"):
        return False, f"H4={h4.get('trend')} vs H1={h1.get('trend')} — no confluence"
    return True, ""


def analyze_market(symbol: str, tf_summaries: dict, account_info: dict) -> dict:
    pattern_insights  = ""
    knowledge_context = ""
    try:
        from memory.outcome_learning import get_pattern_insights
        pattern_insights = get_pattern_insights(symbol, "BUY/SELL", "M5")
    except: pass
    try:
        from memory.knowledge_feed import search_knowledge
        knowledge_context = search_knowledge(f"{symbol} multi-timeframe", top_k=1)
    except: pass

    # Hard confluence guard — skip AI call if H4 and H1 disagree
    passes, reason = _has_confluence(tf_summaries)
    if not passes:
        log("info", f"KAI: {symbol} — confluence blocked ({reason})")
        return {"signal": "WAIT", "confidence": 3,
                "analysis": f"Mixed signals: {reason}",
                "reasoning": reason, "warnings": "Timeframe conflict"}

    # Build compact multi-TF data block for the prompt
    def _tf_block(summary: dict, label: str) -> dict:
        if not summary:
            return None
        return {
            "tf":        label,
            "price":     summary.get("current_price"),
            "ema9":      summary.get("ema9"),
            "ema21":     summary.get("ema21"),
            "ema50":     summary.get("ema50"),
            "rsi":       summary.get("rsi"),
            "rsi_sig":   summary.get("rsi_signal"),
            "macd":      summary.get("macd"),
            "macd_sig":  summary.get("macd_signal"),
            "macd_hist": summary.get("macd_hist"),
            "atr":       summary.get("atr"),
            "trend":     summary.get("trend"),
            "bb_upper":  summary.get("bb_upper"),
            "bb_lower":  summary.get("bb_lower"),
            "resistance": summary.get("swing", {}).get("resistance"),
            "support":    summary.get("swing", {}).get("support"),
        }

    market_data = {
        "symbol":  symbol,
        "balance": account_info.get("balance"),
        "H4":      _tf_block(tf_summaries.get("H4"), "H4"),
        "H1":      _tf_block(tf_summaries.get("H1"), "H1"),
        "M5":      _tf_block(tf_summaries.get("M5"), "M5"),
    }

    user_message = f"Analyze this multi-timeframe data:\n{json.dumps(market_data, indent=None)}"
    if pattern_insights and "No historical" not in pattern_insights:
        user_message += f"\nKAI's past patterns: {pattern_insights}"
    if knowledge_context:
        user_message += f"\nTrading rules: {knowledge_context[:300]}"

    try:
        raw = chat(
            messages=[{"role": "system", "content": ANALYSIS_PROMPT},
                      {"role": "user",   "content": user_message}],
            temperature=0.2,
            max_tokens=600,
        )
        suggestion = _parse_json(raw)
        suggestion["symbol"]    = symbol
        suggestion["timeframe"] = "M5+H1+H4"
        save_report("trade_suggestion", suggestion)
        return suggestion
    except Exception as e:
        log("error", f"Analysis error: {e}")
        return {"signal": "WAIT", "confidence": 0, "error": str(e)}


def _parse_json(raw: str) -> dict:
    import json as j
    try:
        return j.loads(raw)
    except: pass
    try:
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s != -1 and e > s:
            return j.loads(raw[s:e])
    except: pass
    return {"signal": "WAIT", "confidence": 0, "analysis": raw, "warnings": "Parse error"}
