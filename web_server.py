"""
Jarvis Web UI — local FastAPI server.

Run:  python3 web_server.py
Open: http://localhost:7777
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from brain import think
from memory import init_db, save_message, get_recent_messages
from startup_checks import secure_token_files, validate_required_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jarvis.web")

app = FastAPI()

# Serve static files from web/ directory
WEB_DIR = Path(__file__).parent / "web"
WEB_DIR.mkdir(exist_ok=True)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    validate_required_env(["ANTHROPIC_API_KEY"], scope="web_server")
    secure_token_files(Path(__file__).parent)
    await init_db()
    logger.info("Jarvis Web UI ready at http://localhost:7777")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/history")
async def history():
    messages = await get_recent_messages(limit=30)
    return {"messages": messages}


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    user_message = body.get("message", "").strip()
    if not user_message:
        return StreamingResponse(
            _error_stream("Empty message"),
            media_type="text/event-stream"
        )

    history = await get_recent_messages(limit=20)
    await save_message("user", user_message)

    return StreamingResponse(
        _chat_stream(user_message, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/fitness")
async def fitness_page():
    return FileResponse(WEB_DIR / "fitness.html")


@app.get("/api/whoop/history")
async def whoop_history(days: int = 30, sync: bool = False):
    """Return WHOOP history. Pass ?sync=true to refresh from API first."""
    from fitness_services import sync_history, get_history_from_db
    if sync:
        rows = await sync_history(days)
    else:
        rows = await get_history_from_db(days)
        if not rows:
            rows = await sync_history(days)
    return {"records": rows}


@app.post("/skill/{skill_name}")
async def skill(skill_name: str):
    return StreamingResponse(
        _skill_stream(skill_name),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── SSE stream generators ─────────────────────────────────────────────────────

async def _chat_stream(user_message: str, history: list[dict]):
    try:
        response = await think(user_message, history)
        await save_message("assistant", response)
        async for chunk in _word_stream(response):
            yield chunk
    except Exception as e:
        logger.error(f"Chat error: {e}")
        yield _sse({"error": str(e)})


async def _skill_stream(skill_name: str):
    skill_map = {
        "briefing": _run_briefing,
        "research": _run_research,
        "budget_meeting": _run_budget_meeting,
    }
    runner = skill_map.get(skill_name)
    if not runner:
        yield _sse({"error": f"Unknown skill: {skill_name}"})
        return

    try:
        text = await runner()
        async for chunk in _word_stream(text):
            yield chunk
    except Exception as e:
        logger.error(f"Skill '{skill_name}' error: {e}")
        yield _sse({"error": str(e)})


async def _word_stream(text: str):
    """Stream text word-by-word for a smooth typewriter effect."""
    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = ("" if i == 0 else " ") + word
        yield _sse({"text": chunk})
        await asyncio.sleep(0.012)
    yield _sse({"done": True})


async def _error_stream(msg: str):
    yield _sse({"error": msg})


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Skill runners ─────────────────────────────────────────────────────────────

async def _run_briefing() -> str:
    from briefing import run_briefing
    return await run_briefing()


async def _run_research() -> str:
    from daily_research import run_daily_research
    return await run_daily_research()


async def _run_budget_meeting() -> str:
    from budget_meeting import run_budget_meeting
    return await run_budget_meeting()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=7777, log_level="warning")
