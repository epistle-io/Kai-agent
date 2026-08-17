"""api/server.py — KAI FastAPI server"""
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
 
from agent.kai import ask_kai
from utils.state import AgentState
from utils.logger import get_recent_logs, log
from utils.notifications import send_general_message
from utils.metrics import snapshot as metrics_snapshot, record_approval_attempt
from memory.system_store import (
    store_push_token,
    get_active_push_tokens,
    set_setting,
    get_setting,
)
 
state = AgentState()
conversation_history = []

API_KEY = os.getenv("KAI_API_KEY", "").strip()
origins_env = os.getenv("KAI_CORS_ORIGINS", "https://kai-mobile.app")
ALLOWED_ORIGINS = [o.strip() for o in origins_env.split(",") if o.strip()]
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
_IO_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def require_api_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    if not API_KEY:
        return True
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


def approvals_enabled() -> bool:
    return not bool(get_setting("approvals_disabled", False))


def _resolve_db_path() -> str:
    db_path = os.getenv("KAI_DB_PATH", "memory/kai_memory.db").strip() or "memory/kai_memory.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    return os.path.normpath(db_path)


def _resolve_backup_dir() -> str:
    backup_dir = os.getenv("KAI_DB_BACKUP_DIR", "backups").strip() or "backups"
    if not os.path.isabs(backup_dir):
        backup_dir = os.path.join(BASE_DIR, backup_dir)
    return os.path.normpath(backup_dir)


def _create_db_backup(prefix: str = "kai_memory") -> dict:
    import sqlite3

    db_path = _resolve_db_path()
    if not os.path.exists(db_path):
        raise HTTPException(404, f"Database file not found at: {db_path}")

    backup_dir = _resolve_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{prefix}_{stamp}.db"
    backup_path = os.path.join(backup_dir, backup_name)

    src = None
    dst = None
    try:
        src = sqlite3.connect(db_path, timeout=15)
        dst = sqlite3.connect(backup_path, timeout=15)
        with dst:
            src.backup(dst)
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}")
    finally:
        try:
            if src is not None:
                src.close()
            if dst is not None:
                dst.close()
        except Exception:
            pass

    return {
        "database_path": db_path,
        "backup_path": backup_path,
        "backup_name": backup_name,
        "backup_size_bytes": os.path.getsize(backup_path),
        "created_at": datetime.now().isoformat(),
    }


def _resolve_backup_file(name: str) -> str:
    backup_dir = _resolve_backup_dir()
    safe_name = os.path.basename(name or "").strip()
    if not safe_name:
        raise HTTPException(400, "Backup name is required")
    candidate = os.path.normpath(os.path.join(backup_dir, safe_name))
    if not candidate.startswith(backup_dir):
        raise HTTPException(400, "Invalid backup file path")
    if not os.path.exists(candidate):
        raise HTTPException(404, f"Backup file not found: {safe_name}")
    return candidate


def _latest_backup_file() -> str:
    backup_dir = _resolve_backup_dir()
    if not os.path.exists(backup_dir):
        raise HTTPException(404, "No backup directory found")
    files = [
        os.path.join(backup_dir, f)
        for f in os.listdir(backup_dir)
        if f.lower().endswith(".db")
    ]
    if not files:
        raise HTTPException(404, "No backup files found")
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def _run_with_timeout(fn, timeout_seconds: float, timeout_message: str):
    future = _IO_EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        raise HTTPException(504, timeout_message)
 
app = FastAPI(title="KAI Trading Assistant", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
 
class ChatMessage(BaseModel):
    message: str
 
class PushToken(BaseModel):
    token: str
 
class TradeApproval(BaseModel):
    notes: str = ""
 
# ─── Health ───────────────────────────────────────────────────────────────────
 
@app.get("/ping")
def ping():
    return {"status": "alive", "time": datetime.now().isoformat()}


@app.get("/health")
def health():
    token_count = len(get_active_push_tokens())
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "push_tokens": token_count,
        "agent_status": state.get_status(),
    }
 
@app.get("/")
def root():
    return {"name": "KAI", "version": "2.0", "status": state.get_status()}
 
# ─── Status & Signals ─────────────────────────────────────────────────────────
 
@app.get("/status")
def get_status():
    suggestions = state.get_latest_suggestions()
    pending     = state.get_pending_suggestions()
    return {
        "agent_status":      state.get_status(),
        "pairs_watched":     [os.getenv("PAIR_1","frxEURUSD"), os.getenv("PAIR_2","frxGBPUSD"), os.getenv("PAIR_3","cryBTCUSD")],
        "pending_approvals": len(pending),
        "last_checked":      suggestions[0].get("checked_at") if suggestions else None,
        "approvals_enabled": approvals_enabled(),
        "emergency_mode": not approvals_enabled(),
    }
 
@app.get("/suggestions")
def get_suggestions():
    return {"suggestions": state.get_latest_suggestions(), "pending_count": len(state.get_pending_suggestions())}
 
@app.post("/scan")
def trigger_scan(background_tasks: BackgroundTasks, _auth=Depends(require_api_key)):
    def do_scan():
        from agent.scheduler import run_analysis_cycle
        run_analysis_cycle()
    background_tasks.add_task(do_scan)
    return {"message": "Scan started"}
 
# ─── Trade Approval ───────────────────────────────────────────────────────────
 
@app.post("/approve/{symbol}")
def approve_trade(symbol: str, approval: TradeApproval, background_tasks: BackgroundTasks, _auth=Depends(require_api_key)):
    if not approvals_enabled():
        raise HTTPException(423, "Approvals are disabled (emergency mode enabled)")

    record_approval_attempt()
    suggestions = state.get_latest_suggestions()
 
    # Flexible symbol matching
    suggestion = next((s for s in suggestions if s.get("symbol") == symbol), None)
    if not suggestion:
        suggestion = next((s for s in suggestions
                           if symbol in s.get("symbol","") or s.get("symbol","") in symbol), None)
 
    if not suggestion:
        available = [s.get("symbol") for s in suggestions]
        raise HTTPException(404, f"No suggestion for '{symbol}'. Available: {available}")
    if suggestion.get("status") != "PENDING_APPROVAL":
        raise HTTPException(400, f"Status is '{suggestion.get('status')}' — not pending")
 
    def execute():
        from agent.scheduler import execute_approved_trade
        log("info", f"Executing: {suggestion.get('signal')} {suggestion.get('symbol')}")
        result = execute_approved_trade(suggestion)
        log("info", f"Result: {result}")
        status = "EXECUTED" if result.get("success") else "FAILED"
        state.update_suggestion_status(symbol, status, {"execution_result": result})
        if result.get("success"):
            state.add_to_history({**suggestion, **result, "status": "EXECUTED"})
            # Refresh balance in state after trade
            try:
                from deriv.connector import get_fresh_balance
                bal = get_fresh_balance()
                if bal is not None:
                    acct = state.get_account()
                    acct["balance"] = bal
                    acct["equity"]  = bal
                    state.set_account(acct)
            except: pass
        else:
            send_general_message(f"Trade failed: {result.get('error','Unknown error')}")
 
    state.update_suggestion_status(symbol, "APPROVED")
    background_tasks.add_task(execute)
    return {"message": f"Approved! Placing {suggestion.get('signal')} on {suggestion.get('symbol')}...", "symbol": symbol}
 
@app.post("/reject/{symbol}")
def reject_trade(symbol: str, _auth=Depends(require_api_key)):
    state.update_suggestion_status(symbol, "REJECTED", {"rejected_at": datetime.now().isoformat()})
    return {"message": f"Skipping {symbol}. KAI will keep watching."}
 
# ─── Chat ─────────────────────────────────────────────────────────────────────
 
@app.post("/chat")
def chat_with_kai(msg: ChatMessage):
    global conversation_history
    context = {
        "account":        state.get_account(),
        "open_positions": state.get_positions(),
        "latest_signals": [{"symbol":s.get("symbol"),"signal":s.get("signal"),"confidence":s.get("confidence")}
                           for s in state.get_latest_suggestions()],
        "current_time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    response = ask_kai(msg.message, conversation_history, context)
    conversation_history.append({"role":"user","content":msg.message})
    conversation_history.append({"role":"assistant","content":response})
    if len(conversation_history) > 40:
        conversation_history = conversation_history[-40:]
    return {"kai": response, "timestamp": datetime.now().isoformat()}
 
# ─── Account — always fetches live from Deriv ─────────────────────────────────
 
@app.get("/account")
def get_account():
    # Return cached state immediately; live fetch only if cache is empty
    cached = state.get_account()
    if cached:
        return cached
    try:
        from deriv.connector import get_account_info
        acct = _run_with_timeout(
            get_account_info,
            timeout_seconds=8,
            timeout_message="Account fetch timed out",
        )
        state.set_account(acct)
        return acct
    except Exception as e:
        return {"error": str(e), "balance": 0}
 
# ─── Positions — always fetches live from Deriv ───────────────────────────────
 
@app.get("/positions")
def get_positions():
    try:
        from deriv.connector import get_open_positions
        positions = _run_with_timeout(
            get_open_positions,
            timeout_seconds=10,
            timeout_message="Positions fetch timed out",
        )
        if positions is None:
            positions = []
        state.set_positions(positions)
 
        # Enrich with live P&L
        total_profit = sum(float(p.get("profit") or 0) for p in positions)
        return {
            "positions":    positions,
            "count":        len(positions),
            "total_profit": round(total_profit, 2),
        }
    except Exception as e:
        cached = state.get_positions()
        return {"positions": cached, "count": len(cached), "error": str(e)}
 
@app.post("/close/{ticket}")
def close_pos(ticket: int):
    from deriv.connector import close_position
    prior_positions = state.get_positions() or []
    pos = next((p for p in prior_positions if str(p.get("ticket")) == str(ticket)), None)
    result = close_position(ticket) or {"success": False, "error": "No response from broker"}
    if result.get("success"):
        try:
            from memory.outcome_learning import record_trade_close_by_ticket
            close_price = None
            profit_loss = None
            notes = "Closed from mobile/API"
            if pos:
                close_price = pos.get("current_price") or pos.get("open_price")
                profit_loss = pos.get("profit")
            record_trade_close_by_ticket(ticket, close_price=close_price, profit_loss=profit_loss, notes=notes)
        except Exception as e:
            log("warning", f"Close outcome tracking skipped: {e}")

        # Refresh balance after close
        try:
            from deriv.connector import get_fresh_balance
            bal = get_fresh_balance()
            if bal:
                acct = state.get_account()
                acct["balance"] = bal
                state.set_account(acct)
        except: pass
    return result
 
# ─── History & Logs ───────────────────────────────────────────────────────────
 
@app.get("/history")
def get_history():
    return {"trades": state.get_trade_history()}
 
@app.get("/logs")
def get_logs(lines: int = 50):
    return {"logs": get_recent_logs(lines)}
 
# ─── Push Notifications ───────────────────────────────────────────────────────
 
@app.post("/notify/token")
def register_push_token(payload: PushToken):
    token = payload.token
    saved = store_push_token(token)
    if not saved:
        raise HTTPException(400, "token is required")
    log("info", f"Push token registered: {token[:30]}...")
    return {"message": "Push token registered.", "registered_tokens": len(get_active_push_tokens())}


@app.get("/controls")
def get_controls():
    return {
        "approvals_enabled": approvals_enabled(),
        "emergency_mode": not approvals_enabled(),
    }


@app.post("/controls/approvals")
def set_approvals_control(payload: dict, _auth=Depends(require_api_key)):
    enabled = bool(payload.get("enabled", True))
    set_setting("approvals_disabled", not enabled)
    return {
        "message": "Approvals updated",
        "approvals_enabled": approvals_enabled(),
        "emergency_mode": not approvals_enabled(),
    }


@app.post("/admin/backup-db")
def backup_db(_auth=Depends(require_api_key)):
    """Create a timestamped backup of the SQLite database."""
    result = _create_db_backup(prefix="kai_memory")

    return {
        "success": True,
        "message": "Database backup created",
        **result,
    }


@app.get("/admin/backup-db/download")
def download_db_backup(name: str = "", create: bool = True, _auth=Depends(require_api_key)):
    """Download a backup file to client. If name is empty and create=true, creates a fresh backup first."""
    if name.strip():
        backup_path = _resolve_backup_file(name.strip())
    else:
        if create:
            created = _create_db_backup(prefix="kai_memory")
            backup_path = created["backup_path"]
        else:
            backup_path = _latest_backup_file()

    return FileResponse(
        path=backup_path,
        filename=os.path.basename(backup_path),
        media_type="application/x-sqlite3",
    )


@app.post("/admin/backup-db/import")
async def import_db_backup(request: Request, _auth=Depends(require_api_key)):
    """Import (restore) SQLite DB from raw request body bytes.
    Send content-type application/octet-stream with a .db file body.
    """
    import sqlite3

    payload = await request.body()
    if not payload:
        raise HTTPException(400, "Empty request body. Send SQLite file bytes.")
    if not payload.startswith(b"SQLite format 3"):
        raise HTTPException(400, "Invalid SQLite file header")

    db_path = _resolve_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    temp_path = f"{db_path}.import.tmp"
    with open(temp_path, "wb") as f:
        f.write(payload)

    conn = None
    try:
        conn = sqlite3.connect(temp_path, timeout=10)
        check = conn.execute("PRAGMA integrity_check;").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise HTTPException(400, "SQLite integrity_check failed")
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    pre_restore = None
    if os.path.exists(db_path):
        pre_restore = _create_db_backup(prefix="pre_restore")

    try:
        os.replace(temp_path, db_path)
    except Exception as e:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise HTTPException(500, f"Import failed. Stop active jobs and retry: {e}")

    return {
        "success": True,
        "message": "Database imported successfully",
        "database_path": db_path,
        "imported_size_bytes": len(payload),
        "pre_restore_backup": pre_restore,
        "imported_at": datetime.now().isoformat(),
    }


@app.get("/metrics")
def get_metrics():
    try:
        from memory.outcome_learning import get_no_trade_kpi
        no_trade = get_no_trade_kpi(days=30)
    except Exception:
        no_trade = {"message": "unavailable"}
    return {
        "time": datetime.now().isoformat(),
        "runtime": metrics_snapshot(),
        "no_trade_kpi": no_trade,
    }
 
# ─── Learning Endpoints ───────────────────────────────────────────────────────
 
@app.get("/kai/performance")
def get_performance():
    from memory.outcome_learning import get_win_rate_summary
    return get_win_rate_summary()


@app.get("/kai/performance/insights")
def get_performance_insights():
    from memory.outcome_learning import get_performance_dashboard
    return get_performance_dashboard()


@app.get("/kai/performance/weekly-checkpoint")
def get_weekly_checkpoint(refresh: bool = False):
    from memory.outcome_learning import compute_weekly_consistency, get_latest_weekly_consistency
    if refresh:
        return compute_weekly_consistency(days=7, persist=True)
    latest = get_latest_weekly_consistency()
    return latest or compute_weekly_consistency(days=7, persist=False)
 
@app.post("/kai/knowledge")
def add_knowledge(payload: dict):
    from memory.knowledge_feed import add_knowledge
    text   = payload.get("text","")
    source = payload.get("source","manual")
    tags   = payload.get("tags","")
    if not text: raise HTTPException(400, "text is required")
    n = add_knowledge(text, source=source, tags=tags)
    return {"message": f"Added {n} chunks from '{source}'"}
 
@app.get("/kai/knowledge")
def knowledge_stats():
    from memory.knowledge_feed import get_knowledge_stats
    return get_knowledge_stats()
 
@app.get("/kai/reflection")
def get_reflection():
    from memory.self_reflection import get_latest_reflection
    return get_latest_reflection()
 
@app.post("/kai/reflection/run")
def trigger_reflection():
    from memory.self_reflection import run_weekly_reflection
    result = run_weekly_reflection()
    return result or {"message": "No trades to reflect on yet."}
 
# ─── Test Notification Endpoint ──────────────────────────────────────────────
 
@app.post("/test/notification")
def test_notification():
    """Send a test push notification to verify setup is working."""
    tokens = get_active_push_tokens()
    if not tokens:
        return {
            "success": False,
            "error": "No push token is registered. Install the APK, open the app once, then try again.",
            "tip": "After opening the app, check Railway logs for 'Push token registered'"
        }
    try:
        import httpx
        sent = 0
        last_ticket = {}
        for token in tokens:
            response = httpx.post(
                "https://exp.host/--/api/v2/push/send",
                json={
                    "to": token,
                    "title": "KAI is watching",
                    "body": "Notifications are working! I will alert you when I find a trade setup.",
                    "data": {"type": "test"},
                    "sound": "default",
                    "priority": "high",
                },
                timeout=10,
            )
            result = response.json()
            ticket = result.get("data", {})
            status = ticket.get("status", "unknown")
            if status == "ok":
                sent += 1
            last_ticket = ticket
        if sent > 0:
            return {
                "success": True,
                "message": f"Notification sent to {sent} device(s).",
                "ticket": last_ticket,
            }
        return {
            "success": False,
            "error": "Push provider did not accept any notification requests.",
            "ticket": last_ticket,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    
    if text == "/start":
        # Send them their chat ID
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"Welcome to KAI Trader! ✅\n\nYour Chat ID is: <code>{chat_id}</code>\n\nSend this to @kai_trader_agent to complete your onboarding.",
                "parse_mode": "HTML"
            }
        )
    return {"ok": True}