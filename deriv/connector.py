"""deriv/connector.py — Deriv WebSocket API connector"""
import asyncio, json, os, websockets
from datetime import datetime
from dotenv import load_dotenv
from utils.logger import log
load_dotenv()

DERIV_TOKEN = os.getenv("DERIV_API_TOKEN", "")
DERIV_DEMO = os.getenv("DERIV_DEMO", "true").lower() == "true"
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=36544"

SYMBOL_MAP = {
    "EURUSD": "frxEURUSD", "EURUSDm": "frxEURUSD", "frxEURUSD": "frxEURUSD",
    "GBPUSD": "frxGBPUSD", "GBPUSDm": "frxGBPUSD", "frxGBPUSD": "frxGBPUSD",
    "BTCUSD": "cryBTCUSD", "BTCUSDm": "cryBTCUSD", "cryBTCUSD": "cryBTCUSD",
}
TIMEFRAME_MAP = {"M1":60,"M5":300,"M15":900,"M30":1800,"H1":3600,"H4":14400,"D1":86400}

def get_deriv_symbol(symbol): return SYMBOL_MAP.get(symbol, symbol)

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed(): raise RuntimeError
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

async def _auth(ws):
    await ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    r = json.loads(await ws.recv())
    if r.get("error"): raise Exception(r["error"]["message"])
    return r

async def _get_candles_async(symbol, timeframe, count):
    sym = get_deriv_symbol(symbol)
    gran = TIMEFRAME_MAP.get(timeframe, 300)
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        await _auth(ws)
        await ws.send(json.dumps({"ticks_history": sym, "adjust_start_time":1,
            "count": count, "end": "latest", "granularity": gran, "style": "candles"}))
        r = json.loads(await ws.recv())
        if r.get("error"): raise Exception(r["error"]["message"])
        return r.get("candles", [])

async def _get_account_async():
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        r = await _auth(ws)
        data = r.get("authorize", {})
        await ws.send(json.dumps({"balance": 1, "account": "current"}))
        br = json.loads(await ws.recv())
        bal = br.get("balance", {})
        return {
            "balance": bal.get("balance", 0),
            "equity": bal.get("balance", 0),
            "currency": bal.get("currency", "USD"),
            "profit": 0,
            "free_margin": bal.get("balance", 0),
            "leverage": 100,
            "login": data.get("loginid", ""),
            "server": "Deriv-Demo" if DERIV_DEMO else "Deriv-Live"
        }

async def _get_positions_async():
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        await _auth(ws)
        await ws.send(json.dumps({"portfolio": 1}))
        r = json.loads(await ws.recv())
        contracts = r.get("portfolio", {}).get("contracts", [])
        return [{"ticket": c.get("contract_id"), "symbol": c.get("underlying"),
                 "type": "BUY" if c.get("contract_type") in ["CALL","MULTUP"] else "SELL",
                 "profit": c.get("profit_loss", 0)} for c in contracts]

async def _place_trade_async(symbol, direction, amount, sl_pips, tp_pips):
    """
    Place a Multipliers contract on Deriv.
    Multipliers are open-ended — no duration/expiry needed.
    Uses stop_loss and take_profit as dollar amounts, not pips.
    """
    sym = get_deriv_symbol(symbol)
    ctype = "MULTUP" if direction == "BUY" else "MULTDOWN"

    # Convert pips to approximate dollar amounts for limit orders
    # For forex: 1 pip ≈ $0.10 per $10 stake at 100x multiplier
    # Keep it simple: use fixed SL/TP amounts based on stake
    sl_amount = round(amount * 0.5, 2)   # Max loss = 50% of stake
    tp_amount = round(amount * 1.0, 2)   # Take profit = 100% of stake (1:2 R:R)

    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        await _auth(ws)

        # Get proposal first
        proposal_req = {
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": ctype,
            "currency": "USD",
            "symbol": sym,
            "multiplier": 100,
            "limit_order": {
                "stop_loss": sl_amount,
                "take_profit": tp_amount,
            }
        }

        await ws.send(json.dumps(proposal_req))
        pr = json.loads(await ws.recv())

        if pr.get("error"):
            error_msg = pr["error"]["message"]
            log("error", f"Deriv proposal error: {error_msg}")
            return {"success": False, "error": error_msg}

        pid = pr.get("proposal", {}).get("id")
        if not pid:
            return {"success": False, "error": "No proposal ID received"}

        # Buy the contract
        await ws.send(json.dumps({"buy": pid, "price": amount}))
        br = json.loads(await ws.recv())

        if br.get("error"):
            error_msg = br["error"]["message"]
            log("error", f"Deriv buy error: {error_msg}")
            return {"success": False, "error": error_msg}

        c = br.get("buy", {})
        log("info", f"Trade placed: {ctype} {sym} stake=${amount} contract={c.get('contract_id')}")
        return {
            "success": True,
            "ticket": c.get("contract_id"),
            "symbol": symbol,
            "direction": direction,
            "lot": amount,
            "open_price": c.get("buy_price"),
            "sl_amount": sl_amount,
            "tp_amount": tp_amount,
            "time": datetime.now().isoformat(),
        }

async def _close_async(contract_id):
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        await _auth(ws)
        await ws.send(json.dumps({"sell": contract_id, "price": 0}))
        r = json.loads(await ws.recv())
        if r.get("error"): return {"success": False, "error": r["error"]["message"]}
        return {"success": True}

# ─── Public synchronous API ───────────────────────────────────────────────────

def connect():
    try:
        run_async(asyncio.sleep(0))
        log("info", "Deriv connected successfully")
        return True
    except Exception as e:
        log("error", f"Deriv connection failed: {e}")
        return False

def disconnect(): log("info", "Deriv disconnected.")

def get_account_info():
    try: return run_async(_get_account_async())
    except Exception as e:
        log("error", f"Account error: {e}")
        return {"balance":0,"equity":0,"currency":"USD","profit":0,"free_margin":0,"leverage":100}

def get_candles(symbol, timeframe, count=100):
    import pandas as pd
    try:
        candles = run_async(_get_candles_async(symbol, timeframe, count))
        if not candles: return None
        df = pd.DataFrame(candles)
        df.rename(columns={"epoch":"time"}, inplace=True)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df[["time","open","high","low","close"]].astype(
            {"open":float,"high":float,"low":float,"close":float})
        log("info", f"Deriv: Got {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        log("error", f"Candles error: {e}")
        return None

def get_open_positions():
    try: return run_async(_get_positions_async())
    except Exception as e:
        log("error", f"Positions error: {e}")
        return []

def get_open_symbols(positions): return [p.get("symbol") for p in positions]

def place_trade(symbol, direction, lot, sl_pips, tp_pips):
    try:
        stake = max(1.0, min(float(lot) * 10, 50.0))
        return run_async(_place_trade_async(symbol, direction, stake, float(sl_pips), float(tp_pips)))
    except Exception as e:
        log("error", f"Place trade error: {e}")
        return {"success": False, "error": str(e)}

def close_position(ticket):
    try: return run_async(_close_async(ticket))
    except Exception as e: return {"success": False, "error": str(e)}

def calculate_lot_size(symbol, risk_percent, sl_pips):
    try:
        balance = float(get_account_info().get("balance", 100))
        return max(1.0, min(round(balance * (risk_percent / 100), 2), 50.0))
    except: return 1.0