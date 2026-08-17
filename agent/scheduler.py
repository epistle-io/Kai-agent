"""agent/scheduler.py — KAI trading engine with MetaApi/Exness"""
import os
import time
import uuid
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
load_dotenv()

from deriv.connector import (connect, disconnect, get_cycle_data, get_open_symbols,
                              calculate_lot_size, place_trade, get_closed_trade_snapshots)
from agent.trading_brain import analyze_market, build_tf_summaries
from agent.kai import generate_trade_alert, generate_no_trade_update
from utils.notifications import send_trade_alert, send_general_message
from utils.logger import log, save_report, set_cycle_id
from utils.metrics import record_cycle, record_execution_result
from utils.state import AgentState
from utils.notifications import send_signal_to_subscribers

SCALP_SYMBOL_HINTS = ("XAU", "GOLD", "BTC")


def _is_scalp_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return any(h in s for h in SCALP_SYMBOL_HINTS)


def _get_risk_caps() -> dict:
    return {
        "max_signals_per_day": int(os.getenv("MAX_SIGNALS_PER_DAY", "8")),
        "max_pending_signals": int(os.getenv("MAX_PENDING_SIGNALS", "3")),
        "daily_loss_limit_usd": float(os.getenv("DAILY_LOSS_LIMIT_USD", "5")),
        "max_open_positions": int(os.getenv("MAX_OPEN_POSITIONS", "2")),
        "scalp_min_confidence": int(os.getenv("SCALP_MIN_CONFIDENCE", "6")),
    }


def _risk_cap_block_reason(symbol: str, open_positions: list, pending_count: int, caps: dict) -> str:
    if len(open_positions) >= caps["max_open_positions"]:
        return f"Open-position cap reached ({len(open_positions)}/{caps['max_open_positions']})"
    if pending_count >= caps["max_pending_signals"]:
        return f"Pending-signal cap reached ({pending_count}/{caps['max_pending_signals']})"
    try:
        from memory.outcome_learning import get_daily_risk_status
        daily = get_daily_risk_status()
        if daily.get("opened_trades", 0) >= caps["max_signals_per_day"]:
            return f"Daily trade cap reached ({daily.get('opened_trades')}/{caps['max_signals_per_day']})"
        if abs(float(daily.get("realized_loss_usd", 0))) >= caps["daily_loss_limit_usd"] > 0:
            return f"Daily realized-loss cap reached (${abs(float(daily.get('realized_loss_usd', 0))):.2f}/${caps['daily_loss_limit_usd']:.2f})"
    except Exception as e:
        log("warning", f"Risk cap status check failed: {e}")
    return ""

# Exness MT5 symbol names
WATCH_PAIRS = [
    {"symbol": os.getenv("PAIR_1", "EURUSDm"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
    {"symbol": os.getenv("PAIR_2", "GBPUSDm"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
    {"symbol": os.getenv("PAIR_3", "BTCUSDm"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
]
CANDLES  = int(os.getenv("CANDLES_TO_ANALYZE", 100))
INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))
MAX_RISK = float(os.getenv("MAX_RISK_PERCENT", 1.0))

state     = AgentState()
scheduler = BackgroundScheduler()


def run_analysis_cycle():
    cycle_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    set_cycle_id(cycle_id)
    log("info", f"=== KAI cycle started | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    state.set_status("analyzing")

    if not connect():
        log("error", "Connection failed. Skipping cycle.")
        state.set_status("error")
        return

    try:
        caps = _get_risk_caps()
        # Single MetaApi session for the full cycle — avoids creating 6 separate SDK instances
        cycle_data = get_cycle_data(WATCH_PAIRS, CANDLES)
        if not cycle_data:
            raise RuntimeError("No cycle data returned from broker")
        account_info, open_positions, multi_tf_candles = cycle_data
        account_info = account_info or {}
        open_positions = open_positions or []
        multi_tf_candles = multi_tf_candles or {}
        open_symbols = get_open_symbols(open_positions)
        state.set_account(account_info)
        state.set_positions(open_positions)

        try:
            from memory.outcome_learning import get_open_tracked_tickets, reconcile_closed_trades

            tracked = get_open_tracked_tickets(limit=500)
            if tracked:
                live_tickets = {str(p.get("ticket")) for p in open_positions if p.get("ticket") is not None}
                candidate_tickets = [t["ticket"] for t in tracked if t.get("ticket") and t["ticket"] not in live_tickets]
                if candidate_tickets:
                    snapshots = get_closed_trade_snapshots(candidate_tickets, lookback_days=14) or {}
                    recon = reconcile_closed_trades(live_tickets, snapshots)
                    if recon.get("closed", 0) > 0:
                        log("info", f"Reconciled {recon['closed']} externally closed trade(s)")
        except Exception as e:
            log("warning", f"Trade reconciliation skipped: {e}")

        log("info", f"Balance: ${account_info.get('balance')} | Open: {len(open_positions)}")

        suggestions = []
        pending_cap_count = 0

        for pair in WATCH_PAIRS:
            symbol    = pair["symbol"]
            timeframe = pair["timeframe"]

            if symbol in open_symbols:
                log("info", f"Skipping {symbol} — trade already open")
                continue

            log("info", f"KAI: Analyzing {symbol} (M5+H1+H4)...")
            tf_dfs = multi_tf_candles.get(symbol, {})
            m5_df  = tf_dfs.get("M5") if tf_dfs else None

            # If we have no M5 data at all, still show a card — don't silently skip
            if m5_df is None or (hasattr(m5_df, "empty") and m5_df.empty):
                log("warning", f"No candle data for {symbol} — showing data unavailable card")
                suggestions.append({
                    "symbol":     symbol,
                    "timeframe":  "M5+H1+H4",
                    "signal":     "WAIT",
                    "confidence": 0,
                    "analysis":   "Market data unavailable for this cycle.",
                    "reasoning":  "Could not fetch candle data from broker. Will retry next cycle.",
                    "kai_message": f"⚠️ {symbol} — data unavailable this cycle. Retrying in {INTERVAL} mins.",
                    "status":     "NO_TRADE",
                    "checked_at": datetime.now().isoformat(),
                })
                continue

            tf_summaries = build_tf_summaries(tf_dfs, symbol)
            suggestion   = analyze_market(symbol, tf_summaries, account_info)
            suggestion["symbol"]     = symbol
            suggestion["checked_at"] = datetime.now().isoformat()

            signal     = suggestion.get("signal", "WAIT")
            confidence = int(suggestion.get("confidence") or 0)

            hour = datetime.now().hour
            market_session = "Asian" if hour < 8 else "London" if hour < 12 else "New York" if hour < 17 else "Off-hours"
            try:
                from memory.outcome_learning import get_adaptive_confidence_threshold
                threshold = get_adaptive_confidence_threshold(symbol, market_session)
            except Exception:
                threshold = 7
            if _is_scalp_symbol(symbol):
                threshold = min(threshold, caps["scalp_min_confidence"])
            suggestion["confidence_threshold"] = threshold

            if signal in ["BUY", "SELL"] and confidence >= threshold:
                block_reason = _risk_cap_block_reason(symbol, open_positions, pending_cap_count, caps)
                if block_reason:
                    suggestion["signal"] = "WAIT"
                    suggestion["status"] = "NO_TRADE"
                    suggestion["warnings"] = "Blocked by risk cap"
                    suggestion["reasoning"] = block_reason
                    suggestion["kai_message"] = generate_no_trade_update(symbol, suggestion)
                    log("info", f"KAI: {symbol} — blocked by risk cap ({block_reason})")
                    suggestions.append(suggestion)
                    continue

                sl_pips = float(suggestion.get("stop_loss_pips") or 20)
                tp_pips = float(suggestion.get("take_profit_pips") or 40)
                suggestion["stop_loss_pips"]   = sl_pips
                suggestion["take_profit_pips"] = tp_pips
                suggestion["confluence_reason"] = suggestion.get("reasoning", "")
                lot = calculate_lot_size(symbol, MAX_RISK, sl_pips,
                                         balance=account_info.get("balance"))
                suggestion["calculated_lot"] = lot
                suggestion["status"]         = "PENDING_APPROVAL"

                kai_message = generate_trade_alert(symbol, suggestion)
                suggestion["kai_message"] = kai_message
                send_trade_alert(symbol, signal, kai_message, suggestion)
                
                signal_message = (
                    f"🤖 <b>KAI Signal</b>\n\n"
                    f"<b>Pair:</b> {symbol}\n"
                    f"<b>Signal:</b> {signal}\n"
                    f"<b>Confidence:</b> {confidence}/10\n"
                    f"<b>Entry:</b> {suggestion.get('entry_price', 'Market')}\n"
                    f"<b>SL:</b> {suggestion.get('stop_loss_pips')} pips\n"
                    f"<b>TP:</b> {suggestion.get('take_profit_pips')} pips\n"
                    f"<b>R:R:</b> 1:2\n\n"
                    f"⚠️ <i>This is a signal only. Place the trade yourself and manage your own risk.</i>"
                )
                send_signal_to_subscribers(signal_message)

                log("info", f"KAI: {symbol} — {signal} ({confidence}/10) — awaiting approval")
                suggestions.append(suggestion)
                pending_cap_count += 1
            else:
                suggestion["status"]      = "NO_TRADE"
                suggestion["kai_message"] = generate_no_trade_update(symbol, suggestion)
                log("info", f"KAI: {symbol} — no setup ({signal}, {confidence}/10) — {suggestion.get('reasoning','')[:80]}")
                suggestions.append(suggestion)

            try:
                from memory.outcome_learning import record_signal_decision
                h1 = tf_summaries.get("H1", {})
                m5 = tf_summaries.get("M5", {})
                record_signal_decision(
                    symbol=symbol,
                    decision=suggestion.get("status", "NO_TRADE"),
                    confidence=suggestion.get("confidence", 0),
                    trend=h1.get("trend", "UNKNOWN"),
                    rsi_zone=m5.get("rsi_signal", "UNKNOWN"),
                    atr_bucket=suggestion.get("atr_bucket", "UNKNOWN"),
                    market_session=market_session,
                    rule_score=suggestion.get("rule_score", 0),
                )
            except Exception as e:
                log("warning", f"Signal decision tracking skipped: {e}")

        state.set_latest_suggestions(suggestions)
        record_cycle(suggestions)

    except Exception as e:
        log("error", f"Cycle error: {e}")
    finally:
        disconnect()
        state.set_status("idle")
        import gc; gc.collect()
        log("info", "=== KAI cycle complete ===")
        set_cycle_id("-")


def execute_approved_trade(suggestion: dict) -> dict:
    if not connect():
        return {"success": False, "error": "Connection failed"}
    try:
        started = time.perf_counter()
        signal  = suggestion.get("signal")
        lot     = float(suggestion.get("calculated_lot") or 0.01)
        sl_pips = float(suggestion.get("stop_loss_pips") or 20)
        tp_pips = float(suggestion.get("take_profit_pips") or 40)
        symbol  = suggestion.get("symbol", "EURUSDm")

        result = place_trade(symbol, signal, lot, sl_pips, tp_pips) or {
            "success": False,
            "error": "No response from broker",
        }

        if result.get("success"):
            try:
                from memory.outcome_learning import record_trade_open, link_trade_ticket
                trade_id = record_trade_open(
                    symbol, signal, suggestion.get("confidence", 0),
                    suggestion.get("timeframe", "M5"),
                    float(suggestion.get("entry_price") or 0),
                    lot,
                    confluence_reason=suggestion.get("confluence_reason") or suggestion.get("reasoning", ""),
                    session_block=suggestion.get("session_block", ""),
                    ticket=result.get("ticket", ""),
                )
                if trade_id and result.get("ticket"):
                    link_trade_ticket(trade_id, result.get("ticket"))
                result["trade_id"] = trade_id
            except: pass
            send_general_message(f"Trade placed! {signal} on {symbol} ✅")
            log("info", f"Trade executed — {signal} {symbol} ticket:{result.get('ticket')}")
        else:
            log("error", f"Trade failed: {result.get('error')}")

        elapsed_ms = (time.perf_counter() - started) * 1000
        record_execution_result(bool(result.get("success")), elapsed_ms)
        result["broker_latency_ms"] = round(elapsed_ms, 2)

        save_report("trade_executed", result)
        return result

    except Exception as e:
        log("error", f"Trade execution error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        disconnect()


def start_scheduler():
    try:
        from memory.knowledge_feed import load_knowledge_folder
        loaded = load_knowledge_folder()
        if loaded: log("info", f"Knowledge loaded: {', '.join(loaded)}")
    except Exception as e:
        log("warning", f"Knowledge load skipped: {e}")

    log("info", f"Watching: {[p['symbol'] for p in WATCH_PAIRS]} every {INTERVAL} mins")
    run_analysis_cycle()

    scheduler.add_job(run_analysis_cycle, "interval", minutes=INTERVAL,
                      id="kai_trading", replace_existing=True)

    try:
        from memory.self_reflection import run_weekly_reflection
        scheduler.add_job(run_weekly_reflection, "cron",
                          day_of_week="sun", hour=0, minute=0,
                          id="kai_reflection", replace_existing=True)
    except: pass

    try:
        from memory.outcome_learning import compute_weekly_consistency
        scheduler.add_job(
            lambda: compute_weekly_consistency(days=7, persist=True),
            "cron",
            day_of_week="sun",
            hour=0,
            minute=5,
            id="kai_weekly_checkpoint",
            replace_existing=True,
        )
    except Exception as e:
        log("warning", f"Weekly checkpoint scheduler skipped: {e}")

    scheduler.start()
    log("info", "KAI scheduler running.")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
