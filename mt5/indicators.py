"""mt5/indicators.py — Technical indicators using pandas (no MT5 needed)"""
import pandas as pd
import numpy as np

def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < 20: return df
    df = df.copy()
    # EMAs
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    # Bollinger Bands
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"] = sma20 + (2 * std20)
    df["bb_lower"] = sma20 - (2 * std20)
    df["bb_mid"]   = sma20
    # ATR
    df["tr"] = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = df["tr"].rolling(14).mean()
    # Volume proxy (price range as volume substitute)
    df["vol_proxy"] = df["high"] - df["low"]
    return df

def get_market_summary(df: pd.DataFrame, symbol: str) -> dict:
    if df is None or len(df) < 2: return {}
    df = add_all_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    return {
        "symbol": symbol,
        "current_price": round(float(last["close"]), 5),
        "open": round(float(last["open"]), 5),
        "high": round(float(last["high"]), 5),
        "low":  round(float(last["low"]), 5),
        "ema9":  round(float(last["ema9"]),  5),
        "ema21": round(float(last["ema21"]), 5),
        "ema50": round(float(last["ema50"]), 5),
        "rsi": round(float(last["rsi"]), 2),
        "macd": round(float(last["macd"]), 6),
        "macd_signal": round(float(last["macd_signal"]), 6),
        "macd_hist": round(float(last["macd_hist"]), 6),
        "bb_upper": round(float(last["bb_upper"]), 5),
        "bb_lower": round(float(last["bb_lower"]), 5),
        "atr": round(float(last["atr"]), 5),
        "trend": "BULLISH" if last["ema9"] > last["ema21"] else "BEARISH",
        "rsi_signal": "OVERBOUGHT" if last["rsi"] > 70 else "OVERSOLD" if last["rsi"] < 30 else "NEUTRAL",
        "candles_analyzed": len(df),
        "price_change": round(float(last["close"] - prev["close"]), 5),
    }


def get_swing_levels(df: pd.DataFrame, lookback: int = 30) -> dict:
    """Detect nearest swing high (resistance) and swing low (support) from recent candles."""
    if df is None or len(df) < lookback + 4:
        return {"resistance": None, "support": None}

    highs  = df["high"].values
    lows   = df["low"].values
    n      = len(df)
    start  = max(0, n - lookback)

    swing_highs, swing_lows = [], []
    for i in range(start + 2, n - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and
                highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_highs.append(round(float(highs[i]), 5))
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and
                lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append(round(float(lows[i]), 5))

    price = float(df["close"].iloc[-1])
    above = [h for h in swing_highs if h > price]
    below = [l for l in swing_lows  if l < price]
    return {
        "resistance": min(above) if above else None,
        "support":    max(below) if below else None,
    }
