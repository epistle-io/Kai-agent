"""
main.py — KAI Railway Entry Point
Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import threading
from contextlib import asynccontextmanager
from utils.logger import log

@asynccontextmanager
async def lifespan(app):
    log("info", "=" * 50)
    log("info", "  KAI v2 — Personal AI Trading Assistant")
    log("info", "  Broker: Deriv | AI: Gemini 2.5 Flash (OpenRouter)")
    log("info", "=" * 50)

    def run_scheduler():
        try:
            from agent.scheduler import start_scheduler
            start_scheduler()
        except Exception as e:
            log("error", f"Scheduler error: {e}")

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    yield
    log("info", "KAI shutting down...")

from api.server import app
app.router.lifespan_context = lifespan
