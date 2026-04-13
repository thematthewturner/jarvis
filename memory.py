import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = str((Path(__file__).resolve().parent / "data" / "jarvis.db"))


@asynccontextmanager
async def db_connect(path: str | None = None):
    db = await aiosqlite.connect(path or DB_PATH)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


async def init_db():
    async with db_connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        await db.commit()

    from financial_db import init_financial_db
    await init_financial_db()

    from pool_services import init_pool_db
    await init_pool_db()

    from fitness_services import init_fitness_db
    await init_fitness_db()

    from research_services import init_research_db
    await init_research_db()

    from home_ops import init_home_ops_db
    await init_home_ops_db()

    from intentionality import init_intentionality_db
    await init_intentionality_db()

    from investor_bot import init_investor_db
    await init_investor_db()

    from jarvis_ops import init_ops_db
    await init_ops_db()

    from jarvis_reliability import init_reliability_db
    await init_reliability_db()


async def save_message(role: str, content: str):
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_recent_messages(limit: int = 20) -> list[dict]:
    async with db_connect() as db:
        cursor = await db.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
