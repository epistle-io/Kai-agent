"""memory/self_reflection.py — KAI weekly self-assessment"""
import sqlite3, os
from datetime import datetime, timedelta
from utils.logger import log
from utils.groq_client import chat

DB_PATH = os.environ.get(
    "KAI_DB_PATH",
    os.path.join(os.path.dirname(__file__), "kai_memory.db")
)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def run_weekly_reflection():
    log("info", "KAI: Running weekly self-reflection...")
    week_end = datetime.now()
    week_start = week_end - timedelta(days=7)
    conn = get_db()
    try:
        trades = conn.execute("SELECT * FROM trade_outcomes WHERE outcome!='OPEN' AND closed_at>=? AND closed_at<=?",
            (week_start.isoformat(), week_end.isoformat())).fetchall()
    except: conn.close(); return None
    if not trades:
        log("info", "KAI: No trades to reflect on."); conn.close(); return None

    total = len(trades)
    wins  = sum(1 for t in trades if t["outcome"]=="WIN")
    wr    = wins/total*100
    pips  = sum(t["pips_gained"] or 0 for t in trades)
    pnl   = sum(t["profit_loss"] or 0 for t in trades)
    pair_pnl = {}
    for t in trades:
        pair_pnl[t["symbol"]] = pair_pnl.get(t["symbol"],0) + (t["profit_loss"] or 0)
    best  = max(pair_pnl, key=pair_pnl.get) if pair_pnl else "N/A"
    worst = min(pair_pnl, key=pair_pnl.get) if pair_pnl else "N/A"

    summary = "\n".join(f"{t['direction']} {t['symbol']} — {t['outcome']} ({t['pips_gained'] or 0:.1f} pips) conf:{t['confidence']}/10 {t['day_of_week']}" for t in trades)
    prompt = f"""You are KAI reviewing your week {week_start.strftime('%b %d')} - {week_end.strftime('%b %d, %Y')}.
Stats: {total} trades, {wins} wins, {wr:.1f}% win rate, {pips:.1f} pips, ${pnl:.2f} P&L
Best: {best} | Worst: {worst}
Trades: {summary}
Write a honest 150-word self-reflection. What worked, what didn't, what to change next week. KAI's casual voice."""
    try:
        reflection = chat([{"role":"user","content":prompt}], temperature=0.7, max_tokens=180)
    except Exception as e:
        reflection = f"Reflection failed: {e}"

    conn.execute("INSERT INTO weekly_reflections (week_start,week_end,total_trades,wins,losses,win_rate,total_pips,best_pair,worst_pair,reflection_text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (week_start.isoformat(),week_end.isoformat(),total,wins,total-wins,round(wr,1),round(pips,1),best,worst,reflection,datetime.now().isoformat()))
    conn.commit(); conn.close()
    log("info", f"KAI reflection: {wr:.1f}% win rate this week")
    return {"win_rate":round(wr,1),"total_pips":round(pips,1),"reflection":reflection}

def get_latest_reflection():
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM weekly_reflections ORDER BY created_at DESC LIMIT 1").fetchone()
    except: conn.close(); return {"message":"No reflections yet."}
    conn.close()
    if not row: return {"message":"No reflections yet — needs a full week of trading."}
    return {"week":f"{row['week_start'][:10]} to {row['week_end'][:10]}",
            "stats":{"total_trades":row["total_trades"],"wins":row["wins"],"losses":row["losses"],
                     "win_rate":row["win_rate"],"total_pips":row["total_pips"]},
            "reflection":row["reflection_text"]}

def get_all_reflections():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM weekly_reflections ORDER BY created_at DESC").fetchall()
    except: conn.close(); return []
    conn.close()
    return [{"week":f"{r['week_start'][:10]} to {r['week_end'][:10]}","win_rate":r["win_rate"],
             "total_pips":r["total_pips"],"reflection":r["reflection_text"]} for r in rows]
