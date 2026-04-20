"""memory/outcome_learning.py — KAI learns from trade results"""
import json, sqlite3, os
from datetime import datetime, timedelta
from utils.logger import log

DB_PATH = os.environ.get(
    "KAI_DB_PATH",
    os.path.join(os.path.dirname(__file__), "kai_memory.db")
)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, confidence INTEGER,
            timeframe TEXT, entry_price REAL, close_price REAL,
            lot REAL, profit_loss REAL, pips_gained REAL,
            outcome TEXT DEFAULT 'OPEN',
            day_of_week TEXT, hour_of_day INTEGER, market_session TEXT,
            opened_at TEXT, closed_at TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS pattern_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_key TEXT UNIQUE NOT NULL,
            wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            total_pips REAL DEFAULT 0, last_updated TEXT
        );
        CREATE TABLE IF NOT EXISTS weekly_reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT, week_end TEXT, total_trades INTEGER,
            wins INTEGER, losses INTEGER, win_rate REAL,
            total_pips REAL, best_pair TEXT, worst_pair TEXT,
            reflection_text TEXT, created_at TEXT
        );
    """)
    conn.commit(); conn.close()

def record_trade_open(symbol, direction, confidence, timeframe, entry_price, lot):
    init_tables()
    now = datetime.now()
    hour = now.hour
    session = "Asian" if hour < 8 else "London" if hour < 12 else "New York" if hour < 17 else "Off-hours"
    conn = get_db()
    cur = conn.execute("""INSERT INTO trade_outcomes
        (symbol,direction,confidence,timeframe,entry_price,lot,day_of_week,hour_of_day,market_session,opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (symbol,direction,confidence,timeframe,entry_price,lot,now.strftime("%A"),hour,session,now.isoformat()))
    tid = cur.lastrowid; conn.commit(); conn.close()
    return tid

def record_trade_close(trade_id, close_price, profit_loss, notes=""):
    init_tables()
    conn = get_db()
    trade = conn.execute("SELECT * FROM trade_outcomes WHERE id=?", (trade_id,)).fetchone()
    if not trade: conn.close(); return
    outcome = "WIN" if profit_loss > 0 else "LOSS" if profit_loss < 0 else "BREAKEVEN"
    diff = close_price - trade["entry_price"] if trade["direction"]=="BUY" else trade["entry_price"] - close_price
    pips = round(diff * (1 if "BTC" in trade["symbol"] else 10000), 1)
    conn.execute("UPDATE trade_outcomes SET close_price=?,profit_loss=?,pips_gained=?,outcome=?,closed_at=?,notes=? WHERE id=?",
        (close_price,profit_loss,pips,outcome,datetime.now().isoformat(),notes,trade_id))
    is_win = outcome=="WIN"
    for key in [f"{trade['symbol']}_{trade['direction']}", f"{trade['symbol']}_{trade['direction']}_{trade['day_of_week']}"]:
        conn.execute("""INSERT INTO pattern_memory (pattern_key,wins,losses,total_pips,last_updated)
            VALUES (?,?,?,?,?) ON CONFLICT(pattern_key) DO UPDATE SET
            wins=wins+?,losses=losses+?,total_pips=total_pips+?,last_updated=?""",
            (key,int(is_win),int(not is_win),pips,datetime.now().isoformat(),
             int(is_win),int(not is_win),pips,datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_pattern_insights(symbol, direction, timeframe):
    init_tables()
    conn = get_db()
    insights = []
    for key in [f"{symbol}_BUY", f"{symbol}_SELL"]:
        row = conn.execute("SELECT * FROM pattern_memory WHERE pattern_key=?", (key,)).fetchone()
        if row:
            total = row["wins"] + row["losses"]
            if total >= 3:
                wr = row["wins"]/total*100
                insights.append(f"{key.split('_')[1]} on {symbol}: {wr:.0f}% win rate over {total} trades")
    conn.close()
    return "KAI patterns: " + " | ".join(insights) if insights else "No historical data yet."

def get_win_rate_summary():
    init_tables()
    conn = get_db()
    rows = conn.execute("""SELECT symbol,direction,COUNT(*) as total,
        SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
        AVG(pips_gained) as avg_pips, SUM(profit_loss) as pnl
        FROM trade_outcomes WHERE outcome!='OPEN' GROUP BY symbol,direction""").fetchall()
    conn.close()
    return {"performance": [{"symbol":r["symbol"],"direction":r["direction"],"total":r["total"],
        "wins":r["wins"],"win_rate":round(r["wins"]/r["total"]*100,1) if r["total"] else 0,
        "avg_pips":round(r["avg_pips"] or 0,1),"pnl":round(r["pnl"] or 0,2)} for r in rows]}
