"""memory/outcome_learning.py — KAI learns from trade results"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get(
    "KAI_DB_PATH",
    os.path.join(os.path.dirname(__file__), "kai_memory.db")
)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _session_name_from_hour(hour: int) -> str:
    return "Asian" if hour < 8 else "London" if hour < 12 else "New York" if hour < 17 else "Off-hours"


def _session_block_from_hour(hour: int) -> str:
    start = (hour // 4) * 4
    end = start + 3
    return f"{start:02d}-{end:02d}"

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
        CREATE TABLE IF NOT EXISTS signal_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            decision TEXT,
            confidence INTEGER,
            trend TEXT,
            rsi_zone TEXT,
            atr_bucket TEXT,
            market_session TEXT,
            rule_score REAL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS weekly_consistency_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT,
            week_end TEXT,
            total_trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            win_rate REAL,
            expectancy_per_trade REAL,
            max_losing_streak INTEGER,
            confluence_pass_rate REAL,
            total_signals INTEGER,
            confluence_passes INTEGER,
            created_at TEXT
        );
    """)

    # Lightweight schema migration for existing DBs.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trade_outcomes)").fetchall()}
    if "ticket" not in existing:
        conn.execute("ALTER TABLE trade_outcomes ADD COLUMN ticket TEXT")
    if "confluence_reason" not in existing:
        conn.execute("ALTER TABLE trade_outcomes ADD COLUMN confluence_reason TEXT")
    if "session_block" not in existing:
        conn.execute("ALTER TABLE trade_outcomes ADD COLUMN session_block TEXT")

    conn.commit(); conn.close()


def record_trade_open(
    symbol,
    direction,
    confidence,
    timeframe,
    entry_price,
    lot,
    confluence_reason="",
    session_block="",
    ticket="",
):
    init_tables()
    now = datetime.now()
    hour = now.hour
    session = _session_name_from_hour(hour)
    block = session_block or _session_block_from_hour(hour)
    conn = get_db()
    cur = conn.execute("""INSERT INTO trade_outcomes
        (symbol,direction,confidence,timeframe,entry_price,lot,day_of_week,hour_of_day,market_session,opened_at,ticket,confluence_reason,session_block)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            symbol,
            direction,
            confidence,
            timeframe,
            entry_price,
            lot,
            now.strftime("%A"),
            hour,
            session,
            now.isoformat(),
            str(ticket or ""),
            confluence_reason or "",
            block,
        ))
    tid = cur.lastrowid; conn.commit(); conn.close()
    return tid


def link_trade_ticket(trade_id: int, ticket) -> None:
    init_tables()
    conn = get_db()
    conn.execute("UPDATE trade_outcomes SET ticket=? WHERE id=?", (str(ticket or ""), int(trade_id)))
    conn.commit()
    conn.close()


def get_open_tracked_tickets(limit: int = 200):
    init_tables()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, ticket, symbol, direction, entry_price, opened_at
        FROM trade_outcomes
        WHERE outcome='OPEN' AND COALESCE(ticket, '') != ''
        ORDER BY opened_at ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    conn.close()
    return [
        {
            "id": int(r["id"]),
            "ticket": str(r["ticket"]),
            "symbol": r["symbol"],
            "direction": r["direction"],
            "entry_price": float(r["entry_price"] or 0),
            "opened_at": r["opened_at"],
        }
        for r in rows
    ]


def _close_trade_row(conn, trade, close_price, profit_loss, notes=""):
    outcome = "WIN" if profit_loss > 0 else "LOSS" if profit_loss < 0 else "BREAKEVEN"
    entry_price = float(trade["entry_price"] or 0)
    close_value = float(close_price if close_price is not None else entry_price)
    diff = close_value - entry_price if trade["direction"] == "BUY" else entry_price - close_value
    pips = round(diff * (1 if "BTC" in (trade["symbol"] or "") else 10000), 1)
    conn.execute(
        "UPDATE trade_outcomes SET close_price=?,profit_loss=?,pips_gained=?,outcome=?,closed_at=?,notes=? WHERE id=?",
        (close_value, float(profit_loss), pips, outcome, datetime.now().isoformat(), notes, trade["id"]),
    )
    is_win = outcome == "WIN"
    for key in [f"{trade['symbol']}_{trade['direction']}", f"{trade['symbol']}_{trade['direction']}_{trade['day_of_week']}"]:
        conn.execute("""INSERT INTO pattern_memory (pattern_key,wins,losses,total_pips,last_updated)
            VALUES (?,?,?,?,?) ON CONFLICT(pattern_key) DO UPDATE SET
            wins=wins+?,losses=losses+?,total_pips=total_pips+?,last_updated=?""",
            (key, int(is_win), int(not is_win), pips, datetime.now().isoformat(),
             int(is_win), int(not is_win), pips, datetime.now().isoformat()))

def record_trade_close(trade_id, close_price, profit_loss, notes=""):
    init_tables()
    conn = get_db()
    trade = conn.execute("SELECT * FROM trade_outcomes WHERE id=?", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        return False
    _close_trade_row(conn, trade, close_price, profit_loss, notes)
    conn.commit(); conn.close()
    return True


def record_trade_close_by_ticket(ticket, close_price=None, profit_loss=None, notes=""):
    init_tables()
    conn = get_db()
    trade = conn.execute(
        "SELECT * FROM trade_outcomes WHERE ticket=? AND outcome='OPEN' ORDER BY id DESC LIMIT 1",
        (str(ticket),),
    ).fetchone()
    if not trade:
        conn.close()
        return False

    close_value = float(close_price) if close_price is not None else float(trade["entry_price"] or 0)
    pnl_value = float(profit_loss) if profit_loss is not None else 0.0
    _close_trade_row(conn, trade, close_value, pnl_value, notes)
    conn.commit()
    conn.close()
    return True


def reconcile_closed_trades(open_live_tickets: set, closed_snapshots: dict):
    """Reconcile tracked OPEN trades against broker state.

    A tracked trade is closed only when:
    1) ticket is no longer live, and
    2) broker snapshot has close price/profit for that ticket.
    """
    tracked = get_open_tracked_tickets(limit=500)
    if not tracked:
        return {"checked": 0, "closed": 0, "skipped": 0}

    closed = 0
    skipped = 0
    open_set = {str(t) for t in (open_live_tickets or set()) if str(t).strip()}

    for t in tracked:
        ticket = str(t.get("ticket") or "")
        if not ticket or ticket in open_set:
            continue

        snap = (closed_snapshots or {}).get(ticket)
        if not snap:
            skipped += 1
            continue

        ok = record_trade_close_by_ticket(
            ticket=ticket,
            close_price=snap.get("close_price"),
            profit_loss=snap.get("profit_loss"),
            notes=snap.get("notes", "Reconciled external close"),
        )
        if ok:
            closed += 1
        else:
            skipped += 1

    return {"checked": len(tracked), "closed": closed, "skipped": skipped}

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


def record_signal_decision(symbol, decision, confidence=0, trend="UNKNOWN", rsi_zone="UNKNOWN", atr_bucket="UNKNOWN", market_session="UNKNOWN", rule_score=0):
    init_tables()
    conn = get_db()
    conn.execute(
        """INSERT INTO signal_decisions
        (symbol, decision, confidence, trend, rsi_zone, atr_bucket, market_session, rule_score, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            symbol,
            decision,
            int(confidence or 0),
            trend,
            rsi_zone,
            atr_bucket,
            market_session,
            float(rule_score or 0),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_adaptive_confidence_threshold(symbol: str, market_session: str = "UNKNOWN") -> int:
    """Return dynamic confidence threshold (6-8) based on recent outcomes."""
    init_tables()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT outcome FROM trade_outcomes
        WHERE symbol=? AND outcome!='OPEN' AND market_session=?
        ORDER BY closed_at DESC
        LIMIT 20
        """,
        (symbol, market_session),
    ).fetchall()
    if len(rows) < 8:
        rows = conn.execute(
            """
            SELECT outcome FROM trade_outcomes
            WHERE symbol=? AND outcome!='OPEN'
            ORDER BY closed_at DESC
            LIMIT 20
            """,
            (symbol,),
        ).fetchall()
    conn.close()

    if len(rows) < 5:
        return 7

    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    wr = wins / len(rows)
    if wr >= 0.60:
        return 6
    if wr <= 0.40:
        return 8
    return 7


def get_no_trade_kpi(days: int = 30):
    init_tables()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT decision, COUNT(*) as n
        FROM signal_decisions
        WHERE created_at >= ?
        GROUP BY decision
        """,
        (since,),
    ).fetchall()
    conn.close()

    counts = {r["decision"]: r["n"] for r in rows}
    total = sum(counts.values())
    no_trade = counts.get("NO_TRADE", 0)
    actionable = counts.get("PENDING_APPROVAL", 0) + counts.get("APPROVED", 0) + counts.get("EXECUTED", 0)
    ratio = round((no_trade / total) * 100, 2) if total else 0.0
    return {
        "days": days,
        "total_signals": total,
        "no_trade_signals": no_trade,
        "actionable_signals": actionable,
        "no_trade_ratio": ratio,
    }


def get_daily_risk_status():
    """Return today's opened trades and realized loss for hard risk caps."""
    init_tables()
    day_start = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    opened = conn.execute(
        """
        SELECT COUNT(*) as c
        FROM trade_outcomes
        WHERE opened_at >= ?
        """,
        (f"{day_start}T00:00:00",),
    ).fetchone()
    pnl = conn.execute(
        """
        SELECT COALESCE(SUM(profit_loss), 0) as pnl
        FROM trade_outcomes
        WHERE outcome != 'OPEN' AND closed_at >= ?
        """,
        (f"{day_start}T00:00:00",),
    ).fetchone()
    conn.close()

    realized_pnl = float((pnl or {}).get("pnl", 0) if hasattr(pnl, "get") else (pnl["pnl"] if pnl else 0))
    return {
        "opened_trades": int((opened or {}).get("c", 0) if hasattr(opened, "get") else (opened["c"] if opened else 0)),
        "realized_pnl_usd": round(realized_pnl, 2),
        "realized_loss_usd": round(abs(realized_pnl) if realized_pnl < 0 else 0, 2),
    }


def compute_weekly_consistency(days: int = 7, persist: bool = True):
    init_tables()
    now = datetime.now()
    week_start = now - timedelta(days=days)
    since = week_start.isoformat()
    conn = get_db()

    trades = conn.execute(
        """
        SELECT outcome, COALESCE(profit_loss, 0) as profit_loss, closed_at
        FROM trade_outcomes
        WHERE outcome != 'OPEN' AND closed_at >= ?
        ORDER BY closed_at ASC
        """,
        (since,),
    ).fetchall()

    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    net_pnl = sum(float(t["profit_loss"] or 0) for t in trades)
    win_rate = round((wins / total) * 100, 2) if total else 0.0
    expectancy = round(net_pnl / total, 4) if total else 0.0

    max_losing_streak = 0
    streak = 0
    for t in trades:
        if t["outcome"] == "LOSS":
            streak += 1
            if streak > max_losing_streak:
                max_losing_streak = streak
        else:
            streak = 0

    signal_rows = conn.execute(
        """
        SELECT decision, COUNT(*) as n
        FROM signal_decisions
        WHERE created_at >= ?
        GROUP BY decision
        """,
        (since,),
    ).fetchall()
    counts = {r["decision"]: int(r["n"]) for r in signal_rows}
    total_signals = sum(counts.values())
    confluence_passes = (
        counts.get("PENDING_APPROVAL", 0)
        + counts.get("APPROVED", 0)
        + counts.get("EXECUTED", 0)
    )
    confluence_pass_rate = round((confluence_passes / total_signals) * 100, 2) if total_signals else 0.0

    result = {
        "week_start": week_start.date().isoformat(),
        "week_end": now.date().isoformat(),
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "expectancy_per_trade": expectancy,
        "max_losing_streak": max_losing_streak,
        "confluence_pass_rate": confluence_pass_rate,
        "total_signals": total_signals,
        "confluence_passes": confluence_passes,
        "net_pnl": round(net_pnl, 2),
    }

    if persist:
        conn.execute(
            """
            INSERT INTO weekly_consistency_summaries
            (week_start, week_end, total_trades, wins, losses, win_rate, expectancy_per_trade,
             max_losing_streak, confluence_pass_rate, total_signals, confluence_passes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["week_start"],
                result["week_end"],
                result["total_trades"],
                result["wins"],
                result["losses"],
                result["win_rate"],
                result["expectancy_per_trade"],
                result["max_losing_streak"],
                result["confluence_pass_rate"],
                result["total_signals"],
                result["confluence_passes"],
                datetime.now().isoformat(),
            ),
        )
        conn.commit()

    conn.close()
    return result


def get_latest_weekly_consistency():
    init_tables()
    conn = get_db()
    row = conn.execute(
        """
        SELECT week_start, week_end, total_trades, wins, losses, win_rate,
               expectancy_per_trade, max_losing_streak, confluence_pass_rate,
               total_signals, confluence_passes, created_at
        FROM weekly_consistency_summaries
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "week_start": row["week_start"],
        "week_end": row["week_end"],
        "total_trades": int(row["total_trades"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "win_rate": float(row["win_rate"] or 0),
        "expectancy_per_trade": float(row["expectancy_per_trade"] or 0),
        "max_losing_streak": int(row["max_losing_streak"] or 0),
        "confluence_pass_rate": float(row["confluence_pass_rate"] or 0),
        "total_signals": int(row["total_signals"] or 0),
        "confluence_passes": int(row["confluence_passes"] or 0),
        "created_at": row["created_at"],
    }


def get_performance_dashboard():
    init_tables()
    conn = get_db()

    summary_row = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
            SUM(COALESCE(profit_loss, 0)) as pnl
        FROM trade_outcomes
        WHERE outcome != 'OPEN'
        """
    ).fetchone()

    total = int(summary_row["total"] or 0)
    wins = int(summary_row["wins"] or 0)
    pnl = float(summary_row["pnl"] or 0)
    losses = total - wins
    win_rate = round((wins / total) * 100, 2) if total else 0.0
    expectancy = round(pnl / total, 4) if total else 0.0

    daily_rows = conn.execute(
        """
        SELECT date(closed_at) as bucket,
               COUNT(*) as trades,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(profit_loss, 0)) as pnl
        FROM trade_outcomes
        WHERE outcome != 'OPEN' AND closed_at >= datetime('now', '-14 days')
        GROUP BY date(closed_at)
        ORDER BY bucket ASC
        """
    ).fetchall()

    weekly_rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', closed_at) as bucket,
               COUNT(*) as trades,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(profit_loss, 0)) as pnl
        FROM trade_outcomes
        WHERE outcome != 'OPEN' AND closed_at >= datetime('now', '-84 days')
        GROUP BY strftime('%Y-W%W', closed_at)
        ORDER BY bucket ASC
        """
    ).fetchall()

    monthly_rows = conn.execute(
        """
        SELECT strftime('%Y-%m', closed_at) as bucket,
               COUNT(*) as trades,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(profit_loss, 0)) as pnl
        FROM trade_outcomes
        WHERE outcome != 'OPEN' AND closed_at >= datetime('now', '-365 days')
        GROUP BY strftime('%Y-%m', closed_at)
        ORDER BY bucket ASC
        """
    ).fetchall()

    recent_rows = conn.execute(
        """
        SELECT id, ticket, symbol, direction, confidence, confluence_reason, session_block,
               outcome, profit_loss, pips_gained, lot, opened_at, closed_at
        FROM trade_outcomes
        WHERE outcome != 'OPEN'
        ORDER BY closed_at DESC
        LIMIT 20
        """
    ).fetchall()

    conn.close()

    def _format_buckets(rows):
        out = []
        for r in rows:
            trades = int(r["trades"] or 0)
            wins_in_bucket = int(r["wins"] or 0)
            out.append(
                {
                    "label": r["bucket"],
                    "trades": trades,
                    "wins": wins_in_bucket,
                    "win_rate": round((wins_in_bucket / trades) * 100, 2) if trades else 0.0,
                    "pnl": round(float(r["pnl"] or 0), 2),
                }
            )
        return out

    return {
        "summary": {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "net_pnl": round(pnl, 2),
            "expectancy_per_trade": expectancy,
        },
        "daily": _format_buckets(daily_rows),
        "weekly": _format_buckets(weekly_rows),
        "monthly": _format_buckets(monthly_rows),
        "recent_trades": [
            {
                "id": r["id"],
                "ticket": r["ticket"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "confidence": int(r["confidence"] or 0),
                "confluence_reason": r["confluence_reason"] or "",
                "session_block": r["session_block"] or "",
                "outcome": r["outcome"],
                "profit_loss": round(float(r["profit_loss"] or 0), 2),
                "pips_gained": round(float(r["pips_gained"] or 0), 1),
                "lot": float(r["lot"] or 0),
                "opened_at": r["opened_at"],
                "closed_at": r["closed_at"],
            }
            for r in recent_rows
        ],
        "latest_weekly_checkpoint": get_latest_weekly_consistency(),
    }
