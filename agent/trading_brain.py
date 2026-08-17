"""agent/trading_brain.py — Multi-timeframe market analysis with reasoning model"""
import json
import os
from utils.groq_client import chat
from utils.logger import log, save_report

SCALP_SYMBOL_HINTS = ("XAU", "GOLD", "BTC")

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

SCALP_ANALYSIS_PROMPT = """You are a professional XAU/BTC scalper focused on M5 execution with H1 context.

You will receive market data across 3 timeframes. Evaluate in this order:
1. H1 context: Is short-term momentum direction clear?
2. M5 execution: Is there momentum continuation or clean pullback continuation setup now?
3. H4 context: Use as bias only; do not hard-block valid scalp momentum entries.

Return ONLY this JSON — no markdown, no extra text:
{"signal":"BUY|SELL|WAIT","confidence":1-10,"analysis":"2 sentences describing the setup","reasoning":"specific technical reason for the signal","entry_price":0.0,"stop_loss_pips":0,"take_profit_pips":0,"risk_reward":"1:2","warnings":"key risk or NONE"}

Scalp rules for XAU/BTC:
- You MAY output BUY/SELL when H1 and M5 align with clear momentum, even if H4 disagrees.
- You MAY also output BUY/SELL on pullback continuation setups when H1 and H4 agree, and M5 is retracing into EMA9/EMA21 with non-extreme RSI.
- Prefer continuation setups over reversal guesses.
- If structure is choppy or unclear, output WAIT.
- Use tighter scalp logic with practical stops and minimum R:R of 1:1.3.
- Confidence 6+ is acceptable for scalp entries when H1+M5 alignment is strong."""


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


def _is_scalp_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return any(h in s for h in SCALP_SYMBOL_HINTS)


def _has_confluence(summaries: dict, symbol: str) -> tuple[bool, str]:
    """Hard check: H4 and H1 trends must agree before we call the AI.
    Returns (passes, reason_if_blocked)."""
    h4 = summaries.get("H4", {})
    h1 = summaries.get("H1", {})
    m5 = summaries.get("M5", {})

    bullish = {"BULLISH"}
    bearish = {"BEARISH"}

    def _is_pullback_continuation() -> tuple[bool, str]:
        if not h1 or not h4 or not m5:
            return False, ""
        h1_trend = h1.get("trend")
        h4_trend = h4.get("trend")
        m5_trend = m5.get("trend")
        if h1_trend not in bullish | bearish:
            return False, ""
        if h4_trend != h1_trend:
            return False, ""

        price = float(m5.get("current_price") or 0)
        ema9 = float(m5.get("ema9") or 0)
        ema21 = float(m5.get("ema21") or 0)
        ema50 = float(m5.get("ema50") or 0)
        rsi = float(m5.get("rsi") or 50)

        if not price or not ema9 or not ema21 or not ema50:
            return False, ""

        if h1_trend == "BULLISH":
            pullback_zone = price >= ema50 and price <= max(ema9, ema21) * 1.0025
            rsi_ok = 35 <= rsi <= 62
            if pullback_zone and rsi_ok and m5_trend in {"BEARISH", "NEUTRAL", None}:
                return True, f"H1/H4=BULLISH with M5 pullback continuation near EMA9/EMA21 and RSI {rsi:.1f}"
        else:
            pullback_zone = price <= ema50 and price >= min(ema9, ema21) * 0.9975
            rsi_ok = 38 <= rsi <= 65
            if pullback_zone and rsi_ok and m5_trend in {"BULLISH", "NEUTRAL", None}:
                return True, f"H1/H4=BEARISH with M5 pullback continuation near EMA9/EMA21 and RSI {rsi:.1f}"

        return False, ""

    if _is_scalp_symbol(symbol):
        if not h1 or not m5:
            return True, ""  # missing data — let AI decide
        h1_trend = h1.get("trend")
        m5_trend = m5.get("trend")
        if h1_trend in {"BULLISH", "BEARISH"} and m5_trend in {"BULLISH", "BEARISH"}:
            if h1_trend != m5_trend:
                pullback_ok, pullback_reason = _is_pullback_continuation()
                if pullback_ok:
                    return True, pullback_reason
                return False, f"H1={h1_trend} vs M5={m5_trend} — no scalp confluence"
        return True, ""

    if not h4 or not h1:
        return True, ""  # missing data — let AI decide
    if h4.get("trend") != h1.get("trend"):
        return False, f"H4={h4.get('trend')} vs H1={h1.get('trend')} — no confluence"
    return True, ""


def _scalp_pullback_setup(symbol: str, summaries: dict, rule_score: float) -> dict:
    """Deterministic scalp booster for clean pullback continuation setups.

    Used only when AI returns WAIT, so we can still capture obvious continuation pullbacks.
    """
    if not _is_scalp_symbol(symbol):
        return {"ok": False}

    if not bool(int(os.getenv("SCALP_PULLBACK_ENABLE", "1"))):
        return {"ok": False}

    min_rule_score = float(os.getenv("SCALP_PULLBACK_MIN_RULE_SCORE", "45"))
    if float(rule_score or 0) < min_rule_score:
        return {"ok": False}

    h4 = summaries.get("H4") or {}
    h1 = summaries.get("H1") or {}
    m5 = summaries.get("M5") or {}
    if not h4 or not h1 or not m5:
        return {"ok": False}

    h4_trend = h4.get("trend")
    h1_trend = h1.get("trend")
    m5_trend = m5.get("trend")
    if h4_trend != h1_trend or h1_trend not in {"BULLISH", "BEARISH"}:
        return {"ok": False}

    price = float(m5.get("current_price") or 0)
    ema9 = float(m5.get("ema9") or 0)
    ema21 = float(m5.get("ema21") or 0)
    ema50 = float(m5.get("ema50") or 0)
    rsi = float(m5.get("rsi") or 50)
    if not price or not ema9 or not ema21 or not ema50:
        return {"ok": False}

    near_retrace = min(ema9, ema21) <= price <= max(ema9, ema21)
    if h1_trend == "BULLISH":
        pullback_ok = (
            near_retrace
            and price >= ema50
            and m5_trend in {"BEARISH", "NEUTRAL", None}
            and 34 <= rsi <= 62
        )
        signal = "BUY"
    else:
        pullback_ok = (
            near_retrace
            and price <= ema50
            and m5_trend in {"BULLISH", "NEUTRAL", None}
            and 38 <= rsi <= 66
        )
        signal = "SELL"

    if not pullback_ok:
        return {"ok": False}

    if "BTC" in (symbol or "").upper():
        sl_pips, tp_pips = 140, 210
    else:
        sl_pips, tp_pips = 25, 38

    conf = max(5, min(7, int(round(float(rule_score) / 12))))
    return {
        "ok": True,
        "signal": signal,
        "confidence": conf,
        "stop_loss_pips": sl_pips,
        "take_profit_pips": tp_pips,
        "risk_reward": "1:1.5",
        "reasoning": f"Pullback continuation: H1/H4={h1_trend}, M5 retrace into EMA9/21, RSI {rsi:.1f}",
        "analysis": "Clean pullback continuation setup aligned with higher timeframe momentum.",
        "warnings": "Scalp booster signal — manage execution tightly",
    }


def _normal_confluence_ok(summaries: dict) -> tuple[bool, str]:
    """Strict normal setup gate: H4, H1, and M5 trend all align."""
    h4 = summaries.get("H4") or {}
    h1 = summaries.get("H1") or {}
    m5 = summaries.get("M5") or {}
    if not h4 or not h1 or not m5:
        return False, "Missing H4/H1/M5 data"

    t4 = h4.get("trend")
    t1 = h1.get("trend")
    t5 = m5.get("trend")
    if t4 in {"BULLISH", "BEARISH"} and t4 == t1 == t5:
        return True, f"H4/H1/M5 aligned ({t4})"
    return False, f"Normal confluence not aligned (H4={t4}, H1={t1}, M5={t5})"


def _prefilter_and_score(symbol: str, summaries: dict) -> tuple[bool, dict]:
    """Apply deterministic market quality filters and output a rule score."""
    if _is_scalp_symbol(symbol):
        min_atr_pct = float(os.getenv("SCALP_MIN_ATR_PCT", "0.015"))
        max_atr_pct = float(os.getenv("SCALP_MAX_ATR_PCT", "2.50"))
        min_trend_strength_pct = float(os.getenv("SCALP_MIN_TREND_STRENGTH_PCT", "0.010"))
    else:
        min_atr_pct = float(os.getenv("MIN_ATR_PCT", "0.03"))
        max_atr_pct = float(os.getenv("MAX_ATR_PCT", "1.50"))
        min_trend_strength_pct = float(os.getenv("MIN_TREND_STRENGTH_PCT", "0.02"))

    h4 = summaries.get("H4") or {}
    h1 = summaries.get("H1") or {}
    m5 = summaries.get("M5") or {}

    price = float(m5.get("current_price") or 0)
    atr = float(m5.get("atr") or 0)
    atr_pct = (atr / price) * 100 if price else 0
    trend_strength = 0.0
    if price and m5.get("ema9") and m5.get("ema21"):
        trend_strength = abs(float(m5.get("ema9")) - float(m5.get("ema21"))) / price * 100

    if price and (atr_pct < min_atr_pct or atr_pct > max_atr_pct):
        return False, {
            "reason": f"ATR regime out of bounds ({atr_pct:.3f}% not in {min_atr_pct}-{max_atr_pct}%)",
            "rule_score": 25,
            "rule_confidence": 3,
            "atr_bucket": "LOW" if atr_pct < min_atr_pct else "HIGH",
        }

    if trend_strength < min_trend_strength_pct:
        return False, {
            "reason": f"Trend too weak ({trend_strength:.3f}% < {min_trend_strength_pct}%)",
            "rule_score": 30,
            "rule_confidence": 3,
            "atr_bucket": "NORMAL",
        }

    score = 0
    if h4.get("trend") and h4.get("trend") == h1.get("trend"):
        score += 35
    if h1.get("macd") is not None and h1.get("macd_signal") is not None:
        if h1.get("trend") == "BULLISH" and float(h1.get("macd")) >= float(h1.get("macd_signal")):
            score += 20
        elif h1.get("trend") == "BEARISH" and float(h1.get("macd")) <= float(h1.get("macd_signal")):
            score += 20
    rsi = float(m5.get("rsi") or 50)
    trend = m5.get("trend") or "UNKNOWN"
    if trend == "BULLISH" and 35 <= rsi <= 68:
        score += 20
    elif trend == "BEARISH" and 32 <= rsi <= 65:
        score += 20

    resistance = m5.get("swing", {}).get("resistance")
    support = m5.get("swing", {}).get("support")
    if price and resistance and support:
        dist_up = abs(float(resistance) - price) / price * 100
        dist_dn = abs(price - float(support)) / price * 100
        if dist_up > 0.05 and dist_dn > 0.05:
            score += 25

    rule_conf = min(9, max(3, round(score / 12)))
    if score >= 80:
        atr_bucket = "STRONG"
    elif score >= 60:
        atr_bucket = "NORMAL"
    else:
        atr_bucket = "WEAK"
    return True, {
        "reason": "Rule checks passed",
        "rule_score": score,
        "rule_confidence": rule_conf,
        "atr_bucket": atr_bucket,
    }


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
    passes, reason = _has_confluence(tf_summaries, symbol)
    if not passes:
        log("info", f"KAI: {symbol} — confluence blocked ({reason})")
        return {"signal": "WAIT", "confidence": 3,
                "analysis": f"Mixed signals: {reason}",
                "reasoning": reason, "warnings": "Timeframe conflict"}

    pref_ok, pref = _prefilter_and_score(symbol, tf_summaries)
    if not pref_ok:
        return {
            "signal": "WAIT",
            "confidence": int(pref.get("rule_confidence", 3)),
            "analysis": f"Skipped by deterministic filters: {pref.get('reason')}",
            "reasoning": pref.get("reason"),
            "warnings": "Prefilter blocked",
            "rule_score": pref.get("rule_score", 0),
            "atr_bucket": pref.get("atr_bucket", "UNKNOWN"),
        }

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
    user_message += f"\nRule score pre-check: {pref.get('rule_score')} / 100."
    if reason:
        user_message += f"\nConfluence context: {reason}"
    if pattern_insights and "No historical" not in pattern_insights:
        user_message += f"\nKAI's past patterns: {pattern_insights}"
    if knowledge_context:
        user_message += f"\nTrading rules: {knowledge_context[:300]}"

    try:
        chosen_prompt = SCALP_ANALYSIS_PROMPT if _is_scalp_symbol(symbol) else ANALYSIS_PROMPT
        raw = chat(
            messages=[{"role": "system", "content": chosen_prompt},
                      {"role": "user",   "content": user_message}],
            temperature=0.2,
            max_tokens=280,
        )
        suggestion = _parse_json(raw)
        if "reasoning" not in suggestion or not str(suggestion.get("reasoning") or "").strip():
            suggestion["reasoning"] = reason or pref.get("reason") or "No qualifying setup at this moment"
        if "analysis" not in suggestion or not str(suggestion.get("analysis") or "").strip():
            suggestion["analysis"] = "Market conditions did not meet entry requirements"
        ai_conf = int(suggestion.get("confidence") or 0)
        rule_conf = int(pref.get("rule_confidence", 0))
        blended_conf = max(1, min(10, round((ai_conf * 0.6) + (rule_conf * 0.4))))
        suggestion["confidence"] = blended_conf
        suggestion["rule_score"] = pref.get("rule_score", 0)
        suggestion["atr_bucket"] = pref.get("atr_bucket", "UNKNOWN")
        suggestion["setup_mode"] = "scalp" if _is_scalp_symbol(symbol) else "normal"
        if reason:
            suggestion["confluence_reason"] = reason

        # Controlled loosen: only for scalp symbols, only when AI returns WAIT,
        # and only when deterministic pullback continuation conditions are strong.
        if _is_scalp_symbol(symbol) and suggestion.get("signal") == "WAIT":
            booster = _scalp_pullback_setup(symbol, tf_summaries, pref.get("rule_score", 0))
            if booster.get("ok"):
                suggestion.update(booster)
                suggestion["setup_mode"] = "scalp-pullback"

        # Hybrid path: on scalp symbols, still allow strict normal setup checks.
        # If scalp flow says WAIT but full normal confluence exists, ask the normal prompt.
        if _is_scalp_symbol(symbol) and suggestion.get("signal") == "WAIT":
            normal_ok, normal_reason = _normal_confluence_ok(tf_summaries)
            if normal_ok:
                try:
                    raw_normal = chat(
                        messages=[
                            {"role": "system", "content": ANALYSIS_PROMPT},
                            {"role": "user", "content": user_message + f"\nNormal setup gate: {normal_reason}"},
                        ],
                        temperature=0.2,
                        max_tokens=280,
                    )
                    normal_suggestion = _parse_json(raw_normal)
                    normal_signal = normal_suggestion.get("signal")
                    normal_conf = int(normal_suggestion.get("confidence") or 0)
                    normal_blended_conf = max(1, min(10, round((normal_conf * 0.6) + (int(pref.get("rule_confidence", 0)) * 0.4))))

                    if normal_signal in {"BUY", "SELL"} and normal_blended_conf >= 6:
                        suggestion = {
                            **suggestion,
                            **normal_suggestion,
                            "confidence": normal_blended_conf,
                            "rule_score": pref.get("rule_score", 0),
                            "atr_bucket": pref.get("atr_bucket", "UNKNOWN"),
                            "setup_mode": "normal-on-scalp-symbol",
                            "confluence_reason": normal_reason,
                        }
                except Exception as e:
                    log("warning", f"Normal-on-scalp evaluation skipped: {e}")

        min_rule_score = float(os.getenv("SCALP_MIN_RULE_SCORE", "50")) if _is_scalp_symbol(symbol) else 55
        if suggestion.get("signal") in ["BUY", "SELL"] and pref.get("rule_score", 0) < min_rule_score:
            suggestion["signal"] = "WAIT"
            suggestion["warnings"] = "Converted to WAIT by low rule score"
            suggestion["reasoning"] = f"Rule score too low ({pref.get('rule_score', 0)}/100)"

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
