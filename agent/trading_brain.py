"""agent/trading_brain.py — Market analysis (token efficient)"""
import json
from utils.groq_client import chat
from utils.logger import log, save_report

ANALYSIS_PROMPT = """You are a forex/crypto technical analyst. Analyze market data and return ONLY valid JSON:
{"analysis":"2 sentence summary","signal":"BUY|SELL|WAIT","confidence":1-10,"reasoning":"brief why","entry_price":0.0,"stop_loss_pips":0,"take_profit_pips":0,"risk_reward":"1:2","warnings":"any"}
Only signal BUY/SELL if confidence>=6. Return JSON only, no extra text."""

def analyze_market(symbol, timeframe, market_summary, account_info):
    pattern_insights = ""
    knowledge_context = ""
    try:
        from memory.outcome_learning import get_pattern_insights
        pattern_insights = get_pattern_insights(symbol, "BUY/SELL", timeframe)
    except: pass
    try:
        from memory.knowledge_feed import search_knowledge
        knowledge_context = search_knowledge(f"{symbol} {timeframe}", top_k=1)
    except: pass

    # Keep market data concise — only send what matters
    key_data = {
        "symbol": symbol, "timeframe": timeframe,
        "price": market_summary.get("current_price"),
        "ema9": market_summary.get("ema9"),
        "ema21": market_summary.get("ema21"),
        "ema50": market_summary.get("ema50"),
        "rsi": market_summary.get("rsi"),
        "macd": market_summary.get("macd"),
        "macd_signal": market_summary.get("macd_signal"),
        "atr": market_summary.get("atr"),
        "trend": market_summary.get("trend"),
        "rsi_signal": market_summary.get("rsi_signal"),
    }

    user_message = f"Analyze: {json.dumps(key_data)}\nBalance: ${account_info.get('balance')}"
    if pattern_insights and "No historical" not in pattern_insights:
        user_message += f"\nPatterns: {pattern_insights}"
    if knowledge_context:
        user_message += f"\n{knowledge_context[:200]}"

    try:
        raw = chat(
            messages=[{"role":"system","content":ANALYSIS_PROMPT},
                      {"role":"user","content":user_message}],
            temperature=0.3, max_tokens=300,
        )
        suggestion = _parse_json(raw)
        suggestion["symbol"] = symbol
        suggestion["timeframe"] = timeframe
        save_report("trade_suggestion", suggestion)
        return suggestion
    except Exception as e:
        log("error", f"Analysis error: {e}")
        return {"signal":"WAIT","confidence":0,"error":str(e)}

def _parse_json(raw):
    import json as j
    try: return j.loads(raw)
    except: pass
    try:
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s != -1 and e > s: return j.loads(raw[s:e])
    except: pass
    return {"signal":"WAIT","confidence":0,"analysis":raw,"warnings":"Parse error"}
