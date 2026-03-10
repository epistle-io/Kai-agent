"""agent/trading_brain.py — Market analysis with knowledge feed"""
import json
from utils.groq_client import chat
from utils.logger import log, save_report

ANALYSIS_PROMPT = """You are a professional forex and crypto technical analyst.
Analyze the market data and return ONLY valid JSON:
{
  "analysis": "3-4 sentence market analysis",
  "signal": "BUY" or "SELL" or "WAIT",
  "confidence": 1-10,
  "reasoning": "why this signal",
  "entry_price": number,
  "stop_loss_pips": number,
  "take_profit_pips": number,
  "risk_reward": "e.g. 1:2",
  "warnings": "any concerns"
}
Only BUY/SELL when confidence >= 6. Return ONLY JSON."""

def analyze_market(symbol, timeframe, market_summary, account_info):
    # Try to get pattern insights and knowledge
    pattern_insights = ""
    knowledge_context = ""
    try:
        from memory.outcome_learning import get_pattern_insights
        pattern_insights = get_pattern_insights(symbol, "BUY/SELL", timeframe)
    except: pass
    try:
        from memory.knowledge_feed import search_knowledge
        knowledge_context = search_knowledge(f"{symbol} {timeframe} trading strategy", top_k=2)
    except: pass

    user_message = f"""
Analyze this market:
SYMBOL: {symbol} | TIMEFRAME: {timeframe}
MARKET DATA: {json.dumps(market_summary, indent=2)}
ACCOUNT: Balance {account_info.get('balance')} {account_info.get('currency')}
"""
    if pattern_insights:
        user_message += f"\nHISTORICAL PATTERNS:\n{pattern_insights}"
    if knowledge_context:
        user_message += f"\n{knowledge_context}"
    user_message += "\nReturn JSON only."

    try:
        raw = chat(
            messages=[{"role":"system","content":ANALYSIS_PROMPT},
                      {"role":"user","content":user_message}],
            temperature=0.3, max_tokens=1024,
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
