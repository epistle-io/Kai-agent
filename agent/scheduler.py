"""agent/scheduler.py — KAI's trading engine"""
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
load_dotenv()

from deriv.connector import (connect, disconnect, get_candles, get_account_info,
                              get_open_positions, get_open_symbols, calculate_lot_size, place_trade)
from mt5.indicators import get_market_summary
from agent.trading_brain import analyze_market
from agent.kai import generate_trade_alert, generate_no_trade_update
from utils.notifications import send_trade_alert, send_general_message
from utils.logger import log, save_report
from utils.state import AgentState

WATCH_PAIRS = [
    {"symbol": os.getenv("PAIR_1", "frxEURUSD"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
    {"symbol": os.getenv("PAIR_2", "frxGBPUSD"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
    {"symbol": os.getenv("PAIR_3", "cryBTCUSD"), "timeframe": os.getenv("DEFAULT_TIMEFRAME", "M5")},
]
CANDLES   = int(os.getenv("CANDLES_TO_ANALYZE", 100))
INTERVAL  = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))
MAX_RISK  = float(os.getenv("MAX_RISK_PERCENT", 1.0))
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "true").lower() == "true"

state     = AgentState()
scheduler = BackgroundScheduler()


def run_analysis_cycle():
    log("info", f"=== KAI cycle started | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    state.set_status("analyzing")

    if not connect():
        log("error", "KAI: Deriv connection failed. Skipping cycle.")
        state.set_status("error")
        return

    try:
        account_info  = get_account_info()
        open_positions = get_open_positions()
        open_symbols   = get_open_symbols(open_positions)
        state.set_account(account_info)
        state.set_positions(open_positions)

        log("info", f"KAI: Account balance: {account_info.get('balance')} | Open positions: {len(open_positions)}")

        suggestions = []

        for pair in WATCH_PAIRS:
            symbol    = pair["symbol"]
            timeframe = pair["timeframe"]

            if symbol in open_symbols:
                log("info", f"KAI: Skipping {symbol} — trade already open")
                continue

            log("info", f"KAI: Analyzing {symbol} {timeframe}...")
            df = get_candles(symbol, timeframe, CANDLES)
            if df is None or df.empty:
                log("warning", f"KAI: No candle data for {symbol}")
                continue

            market_summary = get_market_summary(df, symbol)
            suggestion = analyze_market(symbol, timeframe, market_summary, account_info)
            suggestion["symbol"] = symbol
            suggestion["checked_at"] = datetime.now().isoformat()

            signal     = suggestion.get("signal", "WAIT")
            confidence = suggestion.get("confidence", 0)

            if signal in ["BUY","SELL"] and int(confidence or 0) >= 6:
                sl_pips = float(suggestion.get("stop_loss_pips") or 20)
                tp_pips = float(suggestion.get("take_profit_pips") or 40)
                suggestion["stop_loss_pips"]   = sl_pips
                suggestion["take_profit_pips"] = tp_pips
                lot = calculate_lot_size(symbol, MAX_RISK, sl_pips)
                suggestion["calculated_lot"] = lot
                suggestion["status"] = "PENDING_APPROVAL"

                kai_message = generate_trade_alert(symbol, suggestion)
                suggestion["kai_message"] = kai_message
                send_trade_alert(symbol, signal, kai_message, suggestion)

                log("info", f"KAI: {symbol} — {signal} signal (confidence {confidence}/10) — notification sent")
                suggestions.append(suggestion)

            else:
                suggestion["status"] = "NO_TRADE"
                suggestion["kai_message"] = generate_no_trade_update(symbol, suggestion)
                log("info", f"KAI: {symbol} — no setup ({signal}, {confidence}/10)")
                suggestions.append(suggestion)

        state.set_latest_suggestions(suggestions)
        save_report("cycle_complete", {"pairs": len(suggestions), "signals": [
            s["symbol"] for s in suggestions if s.get("status") == "PENDING_APPROVAL"]})

    except Exception as e:
        log("error", f"KAI cycle error: {e}")
    finally:
        disconnect()
        state.set_status("idle")
        log("info", "=== KAI cycle complete ===")


def execute_approved_trade(suggestion: dict) -> dict:
    if not connect():
        return {"success": False, "error": "Deriv connection failed"}
    try:
        signal   = suggestion.get("signal")
        lot      = float(suggestion.get("calculated_lot") or 1.0)
        sl_pips  = float(suggestion.get("stop_loss_pips") or 20)
        tp_pips  = float(suggestion.get("take_profit_pips") or 40)
        symbol   = suggestion.get("symbol", "frxEURUSD")

        result = place_trade(symbol, signal, lot, sl_pips, tp_pips)

        if result.get("success"):
            try:
                from memory.outcome_learning import record_trade_open
                trade_id = record_trade_open(
                    symbol, signal, suggestion.get("confidence", 0),
                    suggestion.get("timeframe","M5"),
                    float(suggestion.get("entry_price") or 0), lot
                )
                result["trade_id"] = trade_id
            except: pass

            send_general_message(f"Trade placed! {signal} on {symbol}. I'll keep an eye on it 👀")
            log("info", f"KAI: Trade executed — {signal} {symbol} ticket:{result.get('ticket')}")

        save_report("trade_executed", result)
        return result

    except Exception as e:
        log("error", f"Trade execution error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        disconnect()


def start_scheduler():
    # Load knowledge on startup
    try:
        from memory.knowledge_feed import load_knowledge_folder
        loaded = load_knowledge_folder()
        if loaded: log("info", f"KAI: Knowledge loaded — {', '.join(loaded)}")
        else: log("info", "KAI: No knowledge files found. Add .txt files to /knowledge folder.")
    except Exception as e:
        log("warning", f"Knowledge load skipped: {e}")

    log("info", f"KAI: Watching {[p['symbol'] for p in WATCH_PAIRS]} every {INTERVAL} mins")
    run_analysis_cycle()

    scheduler.add_job(run_analysis_cycle, "interval", minutes=INTERVAL,
                      id="kai_trading", replace_existing=True)

    # Weekly reflection every Sunday midnight
    try:
        from memory.self_reflection import run_weekly_reflection
        scheduler.add_job(run_weekly_reflection, "cron",
                          day_of_week="sun", hour=0, minute=0,
                          id="kai_reflection", replace_existing=True)
    except: pass

    scheduler.start()
    log("info", "KAI: Scheduler running.")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        log("info", "KAI: Scheduler stopped.")
