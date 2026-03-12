"""
deriv/connector.py — MetaApi connector for Exness MT5
Real forex trading with SL/TP via MetaApi cloud
"""
import os, asyncio, functools
from datetime import datetime
from dotenv import load_dotenv
from utils.logger import log
load_dotenv()

METAAPI_TOKEN     = os.getenv("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "")

SYMBOL_MAP = {
    "frxEURUSD": "EURUSDm", "EURUSD": "EURUSDm", "EURUSDm": "EURUSDm",
    "frxGBPUSD": "GBPUSDm", "GBPUSD": "GBPUSDm", "GBPUSDm": "GBPUSDm",
    "cryBTCUSD": "BTCUSDm", "BTCUSD":  "BTCUSDm", "BTCUSDm": "BTCUSDm",
}
TIMEFRAME_MAP = {
    "M1":"1m","M5":"5m","M15":"15m","M30":"30m","H1":"1h","H4":"4h","D1":"1d"
}

def get_mt5_symbol(symbol):
    return SYMBOL_MAP.get(symbol, symbol)

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed(): raise RuntimeError
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

# ─── Core async functions ─────────────────────────────────────────────────────

async def _get_connection():
    from metaapi_cloud_sdk import MetaApi
    api        = MetaApi(METAAPI_TOKEN)
    account    = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    return api, account, connection

async def _get_account_async():
    api, account, conn = await _get_connection()
    try:
        info    = await conn.get_account_information()
        return {
            "balance":      float(info.get("balance", 0)),
            "equity":       float(info.get("equity", 0)),
            "currency":     info.get("currency", "USD"),
            "profit":       float(info.get("profit", 0)),
            "free_margin":  float(info.get("freeMargin", 0)),
            "leverage":     int(info.get("leverage", 2000)),
            "login":        info.get("login", ""),
            "server":       "Exness-MT5Trial9",
            "account_type": "MT5 Standard Demo",
        }
    finally:
        await conn.close()

async def _get_candles_async(symbol, timeframe, count):
    from metaapi_cloud_sdk import MetaApi
    api     = MetaApi(METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
    tf      = TIMEFRAME_MAP.get(timeframe, "5m")
    sym     = get_mt5_symbol(symbol)
    candles = await account.get_historical_candles(sym, tf, None, count)
    return candles

async def _get_positions_async():
    api, account, conn = await _get_connection()
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
        await conn.close()

async def _place_trade_async(symbol, direction, lot, sl_pips, tp_pips):
    api, account, conn = await _get_connection()
    try:
        sym        = get_mt5_symbol(symbol)
        action_type = "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL"

        # Get current price for SL/TP calculation
        price_info = await conn.get_symbol_price(sym)
        ask = float(price_info.get("ask", 0))
        bid = float(price_info.get("bid", 0))

        # Determine pip size
        is_jpy  = "JPY" in sym
        is_btc  = "BTC" in sym
        pip_size = 0.001 if is_jpy else (1.0 if is_btc else 0.0001)

        entry = ask if direction == "BUY" else bid
        sl_distance = float(sl_pips) * pip_size
        tp_distance = float(tp_pips) * pip_size

        if direction == "BUY":
            sl_price = round(entry - sl_distance, 5)
            tp_price = round(entry + tp_distance, 5)
        else:
            sl_price = round(entry + sl_distance, 5)
            tp_price = round(entry - tp_distance, 5)

        log("info", f"MetaApi order: {direction} {sym} lot={lot} entry~{entry} SL={sl_price} TP={tp_price}")

        result = await conn.create_market_order(
            sym,
            action_type,
            float(lot),
            sl_price,
            tp_price,
            {"comment": "KAI"}
        )

        order_id = result.get("orderId") or result.get("positionId")
        log("info", f"✅ Trade placed! {direction} {sym} ticket={order_id} SL={sl_price} TP={tp_price}")

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
    except Exception as e:
        log("error", f"MetaApi trade error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await conn.close()

async def _close_position_async(ticket):
    api, account, conn = await _get_connection()
    try:
        result = await conn.close_position(str(ticket), {"comment": "KAI close"})
        return {"success": True, "result": str(result)}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await conn.close()

# ─── Public synchronous API ───────────────────────────────────────────────────

def connect():
    try:
        if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
            log("error", "METAAPI_TOKEN or METAAPI_ACCOUNT_ID not set in environment")
            return False
        log("info", "MetaApi: Connecting to Exness MT5...")
        return True
    except Exception as e:
        log("error", f"MetaApi connect error: {e}")
        return False

def disconnect():
    log("info", "MetaApi: Disconnected.")

def get_account_info():
    try:
        return run_async(_get_account_async())
    except Exception as e:
        log("error", f"Account info error: {e}")
        return {"balance":0,"equity":0,"currency":"USD","profit":0,"free_margin":0,"leverage":2000}

def get_fresh_balance():
    try:
        info = run_async(_get_account_async())
        return float(info.get("balance", 0))
    except Exception as e:
        log("error", f"Balance fetch error: {e}")
        return None

def get_candles(symbol, timeframe, count=100):
    import pandas as pd
    try:
        candles = run_async(_get_candles_async(symbol, timeframe, count))
        if not candles: return None
        rows = []
        for c in candles:
            rows.append({
                "time":  c.get("time", ""),
                "open":  float(c.get("open", 0)),
                "high":  float(c.get("high", 0)),
                "low":   float(c.get("low", 0)),
                "close": float(c.get("close", 0)),
            })
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
        return run_async(_place_trade_async(symbol, direction, float(lot), float(sl_pips), float(tp_pips)))
    except Exception as e:
        log("error", f"Place trade error: {e}")
        return {"success": False, "error": str(e)}

def close_position(ticket):
    try:
        return run_async(_close_position_async(ticket))
    except Exception as e:
        return {"success": False, "error": str(e)}

def calculate_lot_size(symbol, risk_percent, sl_pips):
    try:
        balance    = float(get_account_info().get("balance", 10000))
        risk_amount = balance * (risk_percent / 100)
        pip_value  = 10.0 if "BTC" not in symbol else 1.0
        lot        = risk_amount / (float(sl_pips) * pip_value)
        return max(0.01, min(round(lot, 2), 10.0))
    except:
        return 0.01
