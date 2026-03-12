"""api/server.py — KAI FastAPI server"""
import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
 
from agent.kai import ask_kai
from utils.state import AgentState
from utils.logger import get_recent_logs, log
from utils.notifications import send_general_message
 
state = AgentState()
conversation_history = []
 
app = FastAPI(title="KAI Trading Assistant", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
 
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
    }
 
@app.get("/suggestions")
def get_suggestions():
    return {"suggestions": state.get_latest_suggestions(), "pending_count": len(state.get_pending_suggestions())}
 
@app.post("/scan")
def trigger_scan(background_tasks: BackgroundTasks):
    def do_scan():
        from agent.scheduler import run_analysis_cycle
        run_analysis_cycle()
    background_tasks.add_task(do_scan)
    return {"message": "Scan started"}
 
# ─── Trade Approval ───────────────────────────────────────────────────────────
 
@app.post("/approve/{symbol}")
def approve_trade(symbol: str, approval: TradeApproval, background_tasks: BackgroundTasks):
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
def reject_trade(symbol: str):
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
    try:
        from deriv.connector import get_account_info
        acct = get_account_info()
        state.set_account(acct)
        return acct
    except Exception as e:
        # Fall back to cached state
        cached = state.get_account()
        if cached: return cached
        return {"error": str(e), "balance": 0}
 
# ─── Positions — always fetches live from Deriv ───────────────────────────────
 
@app.get("/positions")
def get_positions():
    try:
        from deriv.connector import get_open_positions
        positions = get_open_positions()
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
    result = close_position(ticket)
    if result.get("success"):
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
    os.environ["EXPO_PUSH_TOKEN"] = token
    log("info", f"Push token registered: {token[:30]}...")
    return {"message": "Push token registered."}
 
# ─── Learning Endpoints ───────────────────────────────────────────────────────
 
@app.get("/kai/performance")
def get_performance():
    from memory.outcome_learning import get_win_rate_summary
    return get_win_rate_summary()
 
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
    token = os.environ.get("EXPO_PUSH_TOKEN", "")
    if not token:
        return {
            "success": False,
            "error": "No EXPO_PUSH_TOKEN set. Install the APK, open the app once, then try again.",
            "tip": "After opening the app, check Railway logs for 'Push token registered'"
        }
    try:
        import httpx
        response = httpx.post(
            "https://exp.host/--/api/v2/push/send",
            json={
                "to": token,
                "title": "🤖 KAI is watching",
                "body": "Notifications are working! I'll alert you when I find a trade setup.",
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
            return {"success": True, "message": "Notification sent! Check your phone.", "ticket": ticket}
        else:
            return {"success": False, "error": ticket.get("message", "Unknown error"), "details": result}
    except Exception as e:
        return {"success": False, "error": str(e)}