"""
deriv/connector.py — MetaApi connector for Exness MT5
Real forex trading with SL/TP via MetaApi cloud
"""
import os, asyncio
import threading
import traceback
from datetime import datetime, timedelta
import inspect
from dotenv import load_dotenv
from utils.logger import log
load_dotenv()

METAAPI_TOKEN      = os.getenv("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "")

SYMBOL_MAP = {
    "frxEURUSD":"EURUSDm","EURUSD":"EURUSDm","EURUSDm":"EURUSDm",
    "frxGBPUSD":"GBPUSDm","GBPUSD":"GBPUSDm","GBPUSDm":"GBPUSDm",
    "cryBTCUSD":"BTCUSDm","BTCUSD":"BTCUSDm","BTCUSDm":"BTCUSDm",
}
TIMEFRAME_MAP = {
    "M1":"1m","M5":"5m","M15":"15m","M30":"30m","H1":"1h","H4":"4h","D1":"1d"
}

_api = None
_account = None
_connection = None


def _get_symbol_trade_params(symbol: str, ask: float, bid: float) -> tuple[float, int, float]:
    """Return pip size, rounding precision, and minimum stop distance in price units."""
    sym = (symbol or "").upper()
    ref_price = ask or bid or 0.0

    if "BTC" in sym:
        return (
            float(os.getenv("BTC_PIP_SIZE", "1.0")),
            int(os.getenv("BTC_PRICE_PRECISION", "2")),
            float(os.getenv("BTC_MIN_STOP_DISTANCE", "75.0")),
        )

    if "XAU" in sym or "GOLD" in sym:
        return (
            float(os.getenv("XAU_PIP_SIZE", "0.1")),
            int(os.getenv("XAU_PRICE_PRECISION", "2")),
            float(os.getenv("XAU_MIN_STOP_DISTANCE", "3.0")),
        )

    if "JPY" in sym:
        return (
            float(os.getenv("JPY_PIP_SIZE", "0.001")),
            int(os.getenv("JPY_PRICE_PRECISION", "3")),
            float(os.getenv("JPY_MIN_STOP_DISTANCE", "0.05")),
        )

    precision = 5 if ref_price < 10 else 4
    return (
        float(os.getenv("FX_PIP_SIZE", "0.0001")),
        int(os.getenv("FX_PRICE_PRECISION", str(precision))),
        float(os.getenv("FX_MIN_STOP_DISTANCE", "0.0005")),
    )

def get_mt5_symbol(symbol):
    return SYMBOL_MAP.get(symbol, symbol)

# Persistent event loop for the scheduler thread — avoids creating/abandoning
# loops on every call, which leaks async tasks from the MetaApi SDK.
_scheduler_loop = None


def _run_coro_in_new_thread(coro):
    result = {"value": None, "error": None}

    def _target():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(coro)
        except Exception as e:
            result["error"] = e
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()

    if result["error"] is not None:
        raise result["error"]
    return result["value"]

def run_async(coro):
    # If current thread already has a running loop (e.g. FastAPI request),
    # execute coroutine in a dedicated thread with its own loop.
    try:
        asyncio.get_running_loop()
        return _run_coro_in_new_thread(coro)
    except RuntimeError:
        pass

    global _scheduler_loop
    if _scheduler_loop is None or _scheduler_loop.is_closed():
        _scheduler_loop = asyncio.new_event_loop()

    if _scheduler_loop.is_running():
        return _run_coro_in_new_thread(coro)

    asyncio.set_event_loop(_scheduler_loop)
    return _scheduler_loop.run_until_complete(coro)

async def _ensure_connected():
    global _api, _account, _connection
    
    if _connection is not None:
        try:
            await _connection.get_account_information()
            return _connection
        except Exception:
            log("warning", "MetaApi: Connection lost, reconnecting...")
            _connection = None
    
    log("info", "MetaApi: Creating persistent connection...")
    from metaapi_cloud_sdk import MetaApi
    _api = MetaApi(METAAPI_TOKEN)
    
    try:
        log("info", f"MetaApi: Fetching account {METAAPI_ACCOUNT_ID}...")
        _account = await _api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        log("info", f"MetaApi: Account retrieved. State: {_account.state}")
    except Exception as e:
        log("error", f"MetaApi: Failed to fetch account. Error type: {type(e).__name__}, Message: {e}")
        log("error", f"Full traceback:\n{traceback.format_exc()}")
        raise
    
    # Auto-deploy if not deployed
    if _account.state not in ['DEPLOYED', 'DEPLOYING']:
        log("info", f"MetaApi: Deploying account (current state: {_account.state})...")
        await _account.deploy()
        await asyncio.sleep(5)
    
    _connection = _account.get_rpc_connection()
    await _connection.connect()
    await _connection.wait_synchronized(120)
    log("info", "MetaApi: Connected and synchronized.")
    return _connection

async def _get_account_async():
    conn = await _ensure_connected()
    try:
        info = await conn.get_account_information()
        return {
            "balance":      float(info.get("balance", 0)),
            "equity":       float(info.get("equity", 0)),
            "currency":     info.get("currency", "USD"),
            "profit":       float(info.get("profit", 0)),
            "free_margin":  float(info.get("freeMargin", 0)),
            "leverage":     int(info.get("leverage", 2000)),
            "login":        str(info.get("login", "")),
            "server":       "Exness-MT5Trial9",
            "account_type": "MT5 Standard Demo",
        }
    finally:
        log("info", "MetaApi: Account info fetched.")

async def _cycle_data_async(symbols, candle_count):
    import pandas as pd
    
    conn = await _ensure_connected()  # reuses existing connection
    
    # Account info
    info = await conn.get_account_information()
    account_info = {
        "balance":      float(info.get("balance", 0)),
        "equity":       float(info.get("equity", 0)),
        "currency":     info.get("currency", "USD"),
        "profit":       float(info.get("profit", 0)),
        "free_margin":  float(info.get("freeMargin", 0)),
        "leverage":     int(info.get("leverage", 2000)),
        "login":        str(info.get("login", "")),
        "server":       "Exness-MT5Trial9",
        "account_type": "MT5 Standard Demo",
    }

    # Open positions
    positions_raw = await conn.get_positions()
    positions = []
    for p in positions_raw:
        positions.append({
            "ticket":     p.get("id"),
            "symbol":     p.get("symbol", ""),
            "type":       "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL",
            "lot":        float(p.get("volume", 0)),
            "open_price": float(p.get("openPrice", 0)),
            "sl":         float(p.get("stopLoss") or 0),
            "tp":         float(p.get("takeProfit") or 0),
            "profit":     float(p.get("profit", 0)),
            "open_time":  str(p.get("time", "")),
        })

    # Candles — reuse same connection, no reconnect
    TF_CONFIG = [
        ("M5", "5m",  candle_count),
        ("H1", "1h",  50),
        ("H4", "4h",  30),
    ]

    def _to_df(candles):
        rows = [{"time": c.get("time", ""), "open": float(c.get("open", 0)),
                 "high": float(c.get("high", 0)), "low": float(c.get("low", 0)),
                 "close": float(c.get("close", 0))} for c in candles]
        import pandas as pd
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        return df

    multi_tf_candles = {}
    for symbol in symbols:
        sym    = get_mt5_symbol(symbol)
        tf_dfs = {}
        for tf_label, tf_api, count in TF_CONFIG:
            try:
                candles = await _account.get_historical_candles(sym, tf_api, None, count)
                if candles:
                    tf_dfs[tf_label] = _to_df(candles)
                    log("info", f"MetaApi: {symbol} {tf_label} — {len(candles)} candles")
                else:
                    tf_dfs[tf_label] = None
            except Exception as e:
                log("error", f"Candles error {symbol} {tf_label}: {e}")
                tf_dfs[tf_label] = None
        multi_tf_candles[symbol] = tf_dfs

    # DO NOT close connection here — keep it alive for next cycle
    return account_info, positions, multi_tf_candles

def get_cycle_data(pairs, candle_count=100):
    """Fetch account info, positions, and M5/H1/H4 candles in a single MetaApi session.
    Returns: (account_info_dict, positions_list, {symbol: {"M5": df, "H1": df, "H4": df}})
    """
    try:
        symbols = [p["symbol"] for p in pairs]
        return run_async(_cycle_data_async(symbols, candle_count))
    except Exception as e:
        log("error", f"Cycle data error: {e}")
        return (
            {"balance": 0, "equity": 0, "currency": "USD", "profit": 0,
             "free_margin": 0, "leverage": 2000, "login": "",
             "server": "Exness-MT5Trial9", "account_type": "MT5 Standard Demo"},
            [],
            {}
        )

async def _get_positions_async():
    conn = await _ensure_connected()
    try:
        positions = await conn.get_positions()
        result = []
        for p in positions:
            result.append({
                "ticket":     p.get("id"),
                "symbol":     p.get("symbol", ""),
                "type":       "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL",
                "lot":        float(p.get("volume", 0)),
                "open_price": float(p.get("openPrice", 0)),
                "sl":         float(p.get("stopLoss") or 0),
                "tp":         float(p.get("takeProfit") or 0),
                "profit":     float(p.get("profit", 0)),
                "open_time":  str(p.get("time", "")),
            })
        return result
    finally:
        log("info", "MetaApi: Positions fetched.")

async def _place_trade_async(symbol, direction, lot, sl_pips, tp_pips):
    conn = await _ensure_connected()  # reuses existing connection
    sym = get_mt5_symbol(symbol)

    price_info  = await conn.get_symbol_price(sym)
    ask         = float(price_info.get("ask", 0))
    bid         = float(price_info.get("bid", 0))

    pip_size, price_precision, min_stop_distance = _get_symbol_trade_params(sym, ask, bid)

    entry = ask if direction == "BUY" else bid
    requested_sl_distance = float(sl_pips) * pip_size
    requested_tp_distance = float(tp_pips) * pip_size
    sl_distance = max(requested_sl_distance, min_stop_distance)
    tp_distance = max(requested_tp_distance, min_stop_distance * 1.3)

    if direction == "BUY":
        sl_price = round(entry - sl_distance, price_precision)
        tp_price = round(entry + tp_distance, price_precision)
    else:
        sl_price = round(entry + sl_distance, price_precision)
        tp_price = round(entry - tp_distance, price_precision)

    log(
        "info",
        f"MetaApi order: {direction} {sym} lot={lot} entry={entry} "
        f"sl_pips={sl_pips} tp_pips={tp_pips} pip_size={pip_size} "
        f"sl_dist={sl_distance} tp_dist={tp_distance} min_stop={min_stop_distance} "
        f"SL={sl_price} TP={tp_price}",
    )

    if direction == "BUY":
        result = await conn.create_market_buy_order(sym, float(lot), sl_price, tp_price, {"comment": "KAI"})
    else:
        result = await conn.create_market_sell_order(sym, float(lot), sl_price, tp_price, {"comment": "KAI"})

    order_id = result.get("orderId") or result.get("positionId")
    log("info", f"Trade placed: {direction} {sym} ticket={order_id}")

    return {
        "success":     True,
        "ticket":      order_id,
        "symbol":      symbol,
        "direction":   direction,
        "lot":         lot,
        "entry_price": entry,
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_pips":     sl_pips,
        "tp_pips":     tp_pips,
        "time":        datetime.now().isoformat(),
    }

async def _close_position_async(ticket):
    conn = await _ensure_connected()
    try:
        result = await conn.close_position(str(ticket), {"comment": "KAI close"})
        return {"success": True, "result": str(result)}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        log("info", "MetaApi: Position closed.")


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return float(default)


def _parse_iso_time(value):
    if not value:
        return ""
    try:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)
    except Exception:
        return ""


async def _get_closed_trade_snapshots_async(tickets, lookback_days=14):
    """Best-effort broker history lookup for closed tickets.

    Returns {ticket: {close_price, profit_loss, closed_at, notes}}.
    """
    conn = await _ensure_connected()
    requested = {str(t) for t in (tickets or []) if str(t).strip()}
    if not requested:
        return {}

    end = datetime.utcnow()
    start = end - timedelta(days=max(1, int(lookback_days or 14)))

    deal_rows = []

    # MetaApi SDK versions can expose different history methods.
    candidate_calls = [
        ("get_deals_by_time_range", (start, end)),
        ("get_history_deals_by_time_range", (start, end)),
        ("get_deals_by_time_range", (start.isoformat(), end.isoformat())),
        ("get_history_deals_by_time_range", (start.isoformat(), end.isoformat())),
    ]

    for method_name, args in candidate_calls:
        method = getattr(conn, method_name, None)
        if not callable(method):
            continue
        try:
            maybe_rows = method(*args)
            rows = await maybe_rows if inspect.isawaitable(maybe_rows) else maybe_rows
            if rows:
                deal_rows = rows
                break
        except Exception:
            continue

    if not deal_rows:
        return {}

    if isinstance(deal_rows, dict):
        for key in ("deals", "items", "history"):
            if isinstance(deal_rows.get(key), list):
                deal_rows = deal_rows.get(key)
                break
        else:
            deal_rows = []
    elif not isinstance(deal_rows, list):
        try:
            deal_rows = list(deal_rows)
        except Exception:
            deal_rows = []

    snapshots = {}
    for d in deal_rows:
        ticket = str(
            d.get("positionId")
            or d.get("position_id")
            or d.get("position")
            or ""
        )
        if not ticket or ticket not in requested:
            continue

        entry = str(d.get("entryType") or d.get("entry") or "").upper()
        if entry and "OUT" not in entry and "CLOSE" not in entry:
            continue

        closed_at = _parse_iso_time(d.get("time") or d.get("doneTime") or d.get("updateTime"))
        current = snapshots.get(ticket)
        if current and closed_at and current.get("closed_at") and closed_at <= current.get("closed_at"):
            continue

        snapshots[ticket] = {
            "close_price": _safe_float(d.get("price") or d.get("closePrice") or 0),
            "profit_loss": _safe_float(d.get("profit") or d.get("realizedProfit") or 0),
            "closed_at": closed_at,
            "notes": "Reconciled external close (SL/TP or manual MT5)",
        }

    return snapshots

# ─── Public synchronous API ───────────────────────────────────────────────────

def connect():
    """Warm up the persistent connection at startup."""
    try:
        if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
            log("error", "METAAPI_TOKEN or METAAPI_ACCOUNT_ID not set")
            return False
        run_async(_ensure_connected())
        log("info", "MetaApi: Persistent connection established.")
        return True
    except Exception as e:
        log("error", f"MetaApi connect error: {e}")
        log("error", f"Exception type: {type(e).__name__}")
        log("error", f"Full traceback:\n{traceback.format_exc()}")
        return False

def disconnect():
    """Only call this on full shutdown — not between cycles."""
    global _connection, _api
    log("info", "MetaApi: Keeping connection alive between cycles.")
    # Don't actually disconnect — connection stays open


def get_account_info():
    async def _get():
        conn = await _ensure_connected()
        info = await conn.get_account_information()
        return {
            "balance":      float(info.get("balance", 0)),
            "equity":       float(info.get("equity", 0)),
            "currency":     info.get("currency", "USD"),
            "profit":       float(info.get("profit", 0)),
            "free_margin":  float(info.get("freeMargin", 0)),
            "leverage":     int(info.get("leverage", 2000)),
            "login":        str(info.get("login", "")),
            "server":       "Exness-MT5Trial9",
            "account_type": "MT5 Standard Demo",
        }
    try:
        return run_async(_get())
    except Exception as e:
        log("error", f"Account info error: {e}")
        return {"balance":0,"equity":0,"currency":"USD","profit":0,
                "free_margin":0,"leverage":2000}

def get_fresh_balance():
    try:
        info = get_account_info()
        return float(info.get("balance", 0))
    except Exception as e:
        log("error", f"Balance fetch error: {e}")
        return None

def get_candles(symbol, timeframe, count=100):
    import pandas as pd
    async def _get():
        conn = await _ensure_connected()
        sym = get_mt5_symbol(symbol)
        tf = TIMEFRAME_MAP.get(timeframe, "5m")
        candles = await conn.get_historical_candles(sym, tf, None, count)
        return candles
    try:
        candles = run_async(_get())
        if not candles: return None
        rows = [{"time": c.get("time",""), "open": float(c.get("open",0)),
                 "high": float(c.get("high",0)), "low": float(c.get("low",0)),
                 "close": float(c.get("close",0))} for c in candles]
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        log("info", f"MetaApi: Got {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        log("error", f"Candles error: {e}")
        return None

def get_open_positions():
    try:
        return run_async(_get_positions_async())
    except Exception as e:
        log("error", f"Positions error: {e}")
        return []

def get_open_symbols(positions):
    return [p.get("symbol") for p in positions]

def place_trade(symbol, direction, lot, sl_pips, tp_pips):
    try:
        return run_async(_place_trade_async(
            symbol, direction, float(lot), float(sl_pips), float(tp_pips)
        ))
    except Exception as e:
        log("error", f"Place trade error: {e}")
        return {"success": False, "error": str(e)}

def close_position(ticket):
    try:
        return run_async(_close_position_async(ticket))
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_closed_trade_snapshots(tickets, lookback_days=14):
    try:
        return run_async(_get_closed_trade_snapshots_async(tickets, lookback_days=lookback_days))
    except Exception as e:
        log("warning", f"Closed trade snapshot lookup failed: {e}")
        return {}

def _safe_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        log("warning", f"Invalid {name}='{raw}', using default {default}")
        return default


def _normalize_lot(lot: float, min_lot: float, max_lot: float, step: float) -> float:
    if step <= 0:
        step = 0.01
    if min_lot <= 0:
        min_lot = 0.01
    if max_lot < min_lot:
        max_lot = min_lot

    snapped = round(round(lot / step) * step, 2)
    return max(min_lot, min(snapped, max_lot))


def calculate_lot_size(symbol, risk_percent, sl_pips, balance=None):
    try:
        lot_mode = os.getenv("LOT_SIZE_MODE", "risk").strip().lower()
        fixed_lot = _safe_env_float("FIXED_LOT_SIZE", 0.0)
        min_lot = _safe_env_float("MIN_LOT_SIZE", 0.01)
        max_lot = _safe_env_float("MAX_LOT_SIZE", 10.0)
        lot_step = _safe_env_float("LOT_SIZE_STEP", 0.01)

        # Optional fixed lot mode for explicit manual size control from env/secrets.
        if lot_mode == "fixed" and fixed_lot > 0:
            return _normalize_lot(fixed_lot, min_lot, max_lot, lot_step)

        if balance is None:
            balance = float(get_account_info().get("balance", 10000))
        else:
            balance = float(balance)
        risk_amount = balance * (risk_percent / 100)
        pip_value   = 10.0 if "BTC" not in symbol else 1.0
        lot         = risk_amount / (float(sl_pips) * pip_value)
        return _normalize_lot(lot, min_lot, max_lot, lot_step)
    except:
        return 0.01
