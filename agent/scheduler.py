"""agent/scheduler.py — KAI trading engine with MetaApi/Exness"""
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
load_dotenv()

from deriv.connector import (connect, disconnect, get_cycle_data, get_open_symbols,
                              calculate_lot_size, place_trade)
from agent.trading_brain import analyze_market, build_tf_summaries
from agent.kai import generate_trade_alert, generate_no_trade_update
from utils.notifications import send_trade_alert, send_general_message
from utils.logger import log, save_report
from utils.state import AgentState

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
    log("info", f"=== KAI cycle started | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    state.set_status("analyzing")

    if not connect():
        log("error", "Connection failed. Skipping cycle.")
        state.set_status("error")
        return

    try:
        # Single MetaApi session for the full cycle — avoids creating 6 separate SDK instances
        account_info, open_positions, multi_tf_candles = get_cycle_data(WATCH_PAIRS, CANDLES)
        open_symbols = get_open_symbols(open_positions)
        state.set_account(account_info)
        state.set_positions(open_positions)

        log("info", f"Balance: ${account_info.get('balance')} | Open: {len(open_positions)}")

        suggestions = []

        for pair in WATCH_PAIRS:
            symbol    = pair["symbol"]
            timeframe = pair["timeframe"]

            if symbol in open_symbols:
                log("info", f"Skipping {symbol} — trade already open")
                continue

            log("info", f"KAI: Analyzing {symbol} (M5+H1+H4)...")
            tf_dfs = multi_tf_candles.get(symbol, {})
            if not tf_dfs or tf_dfs.get("M5") is None:
                log("warning", f"No candle data for {symbol}")
                continue

            tf_summaries = build_tf_summaries(tf_dfs, symbol)
            suggestion   = analyze_market(symbol, tf_summaries, account_info)
            suggestion["symbol"]     = symbol
            suggestion["checked_at"] = datetime.now().isoformat()

            signal     = suggestion.get("signal", "WAIT")
            confidence = int(suggestion.get("confidence") or 0)

            if signal in ["BUY", "SELL"] and confidence >= 7:
                sl_pips = float(suggestion.get("stop_loss_pips") or 20)
                tp_pips = float(suggestion.get("take_profit_pips") or 40)
                suggestion["stop_loss_pips"]   = sl_pips
                suggestion["take_profit_pips"] = tp_pips
                lot = calculate_lot_size(symbol, MAX_RISK, sl_pips,
                                         balance=account_info.get("balance"))
                suggestion["calculated_lot"] = lot
                suggestion["status"]         = "PENDING_APPROVAL"

                kai_message = generate_trade_alert(symbol, suggestion)
                suggestion["kai_message"] = kai_message
                send_trade_alert(symbol, signal, kai_message, suggestion)

                log("info", f"KAI: {symbol} — {signal} ({confidence}/10) — awaiting approval")
                suggestions.append(suggestion)
            else:
                suggestion["status"]      = "NO_TRADE"
                suggestion["kai_message"] = generate_no_trade_update(symbol, suggestion)
                log("info", f"KAI: {symbol} — no setup ({signal}, {confidence}/10)")
                suggestions.append(suggestion)

        state.set_latest_suggestions(suggestions)

    except Exception as e:
        log("error", f"Cycle error: {e}")
    finally:
        disconnect()
        state.set_status("idle")
        import gc; gc.collect()
        log("info", "=== KAI cycle complete ===")


def execute_approved_trade(suggestion: dict) -> dict:
    if not connect():
        return {"success": False, "error": "Connection failed"}
    try:
        signal  = suggestion.get("signal")
        lot     = float(suggestion.get("calculated_lot") or 0.01)
        sl_pips = float(suggestion.get("stop_loss_pips") or 20)
        tp_pips = float(suggestion.get("take_profit_pips") or 40)
        symbol  = suggestion.get("symbol", "EURUSDm")

        result = place_trade(symbol, signal, lot, sl_pips, tp_pips)

        if result.get("success"):
            try:
                from memory.outcome_learning import record_trade_open
                trade_id = record_trade_open(
                    symbol, signal, suggestion.get("confidence", 0),
                    suggestion.get("timeframe", "M5"),
                    float(suggestion.get("entry_price") or 0), lot
                )
                result["trade_id"] = trade_id
            except: pass
            send_general_message(f"Trade placed! {signal} on {symbol} ✅")
            log("info", f"Trade executed — {signal} {symbol} ticket:{result.get('ticket')}")
        else:
            log("error", f"Trade failed: {result.get('error')}")

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

    scheduler.start()
    log("info", "KAI scheduler running.")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
