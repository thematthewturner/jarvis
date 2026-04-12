"""
Reliability layer for Jarvis:

- Action audit log: every write/tool call recorded with args, result, status, duration.
- Idempotency guard: dedupe identical writes within a short window (hash of tool+args).
- Job registry: per-job last run, last success, last error, duration, history.

All state lives in the existing SQLite DB next to the rest of Jarvis.  Nothing
here owns scheduling — it's a thin observability + safety layer the rest of
the stack can call into.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis.reliability")

# Tools that perform an external write or local state mutation.  Reads are not
# audited — they create log noise and no risk.  Keep this list in sync with
# tools.py::handle_tool_call.
WRITE_TOOLS: set[str] = {
    "save_note",
    "add_task",
    "update_task",
    "send_email",
    "archive_gmail_email",
    "star_gmail_email",
    "mark_gmail_email_read",
    "save_notion_note",
    "save_intentionality_signal",
    "create_icloud_event",
    "send_icloud_email",
    "flag_icloud_email",
    "archive_icloud_email",
    "move_icloud_email",
    "trash_icloud_email",
    "mark_icloud_email_read",
    "log_pool_chemistry",
    "log_pool_chemical",
    "log_pr",
    "log_workout",
}

# Per-tool dedupe window in seconds.  A write with the same args_hash inside
# this window is treated as an accidental repeat and short-circuited.  Tools
# not listed fall back to DEFAULT_DEDUPE_WINDOW.
DEFAULT_DEDUPE_WINDOW = 600  # 10 minutes
DEDUPE_WINDOWS: dict[str, int] = {
    # Fast finger-slip protection for transient user actions.
    "add_task": 300,
    "save_note": 180,
    "save_notion_note": 300,
    "save_intentionality_signal": 300,
    # Pool and fitness logs are idempotent within a session.
    "log_pool_chemistry": 900,
    "log_pool_chemical": 900,
    "log_pr": 900,
    "log_workout": 900,
    # Outbound communications deserve a longer window — duplicate sends are
    # the most embarrassing class of bug in an assistant like this.
    "send_email": 1800,
    "send_icloud_email": 1800,
    "create_icloud_event": 1800,
    # Mail-state mutations: short window, these get called in loops by triage.
    "flag_icloud_email": 120,
    "archive_icloud_email": 120,
    "star_gmail_email": 120,
    "archive_gmail_email": 120,
    "mark_gmail_email_read": 120,
    "mark_icloud_email_read": 120,
    "trash_icloud_email": 300,
    "move_icloud_email": 300,
}

# Tools that should NOT be deduped even though they're writes (e.g. update_task
# can legitimately update the same task twice in a row with different fields).
NO_DEDUPE: set[str] = {"update_task"}


async def init_reliability_db() -> None:
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill TEXT,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                duration_ms INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_log_created ON jarvis_action_log(created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_log_hash ON jarvis_action_log(args_hash, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_log_tool ON jarvis_action_log(tool, created_at)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                error TEXT,
                note TEXT
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_runs_job_started ON jarvis_job_runs(job_name, started_at DESC)"
        )
        await db.commit()


# ── Audit log helpers ────────────────────────────────────────────────────────

def _compute_args_hash(tool: str, args: dict) -> str:
    try:
        canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        canonical = str(args)
    digest = hashlib.sha1(f"{tool}::{canonical}".encode("utf-8")).hexdigest()
    return digest


def _truncate(value: str | None, max_len: int = 2000) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


async def _find_recent_action(tool: str, args_hash: str, window_seconds: int) -> dict | None:
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, status, result, created_at
            FROM jarvis_action_log
            WHERE tool = ? AND args_hash = ?
              AND status = 'ok'
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tool, args_hash, f"-{int(max(1, window_seconds))} seconds"),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "status": row[1],
        "result": row[2],
        "created_at": row[3],
    }


async def record_action(
    *,
    tool: str,
    args: dict,
    status: str,
    result: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    skill: str | None = None,
) -> int:
    args_hash = _compute_args_hash(tool, args)
    args_json = json.dumps(args or {}, ensure_ascii=True, default=str)
    now = datetime.utcnow().isoformat()
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO jarvis_action_log
                (skill, tool, args_json, args_hash, status, result, error, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill,
                tool,
                args_json,
                args_hash,
                status,
                _truncate(result),
                _truncate(error, 1000),
                duration_ms,
                now,
            ),
        )
        await db.commit()
        return cur.lastrowid or 0


async def execute_and_log(
    *,
    tool: str,
    args: dict,
    handler: Callable[[dict], Awaitable[str]],
    skill: str = "chat",
) -> str:
    """
    Run a tool handler with audit + idempotency.

    Read-only tools flow straight through with zero bookkeeping.  Write tools:

    1. Hash `(tool, args)`.
    2. If the same hash ran successfully inside the dedupe window, return that
       prior result wrapped in a "deduped" marker — no side effect.
    3. Otherwise invoke the handler, record success/failure, return result.
    """
    is_write = tool in WRITE_TOOLS
    if not is_write:
        return await handler(args)

    args_hash = _compute_args_hash(tool, args)
    window = DEDUPE_WINDOWS.get(tool, DEFAULT_DEDUPE_WINDOW)

    if tool not in NO_DEDUPE and window > 0:
        prior = await _find_recent_action(tool, args_hash, window)
        if prior is not None:
            logger.info("dedupe: %s within %ss -> reusing action #%s", tool, window, prior["id"])
            await record_action(
                tool=tool,
                args=args,
                status="dedup_skip",
                result=prior.get("result") or "",
                skill=skill,
                duration_ms=0,
            )
            prior_result = prior.get("result") or ""
            return f"[deduped] {prior_result}"

    started = time.perf_counter()
    try:
        result = await handler(args)
    except Exception as exc:  # noqa: BLE001 - we want broad catch, re-raise below
        duration_ms = int((time.perf_counter() - started) * 1000)
        await record_action(
            tool=tool,
            args=args,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=duration_ms,
            skill=skill,
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    await record_action(
        tool=tool,
        args=args,
        status="ok",
        result=str(result) if result is not None else "",
        duration_ms=duration_ms,
        skill=skill,
    )
    return result


async def get_recent_actions(limit: int = 40, tool: str | None = None) -> list[dict]:
    query = (
        "SELECT id, skill, tool, args_json, status, result, error, duration_ms, created_at "
        "FROM jarvis_action_log "
    )
    params: tuple = ()
    if tool:
        query += "WHERE tool = ? "
        params = (tool,)
    query += "ORDER BY id DESC LIMIT ?"
    params = params + (int(max(1, min(500, limit))),)
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    out: list[dict] = []
    for row in rows:
        try:
            args = json.loads(row[3] or "{}")
        except Exception:
            args = {"_raw": row[3]}
        out.append(
            {
                "id": row[0],
                "skill": row[1],
                "tool": row[2],
                "args": args,
                "status": row[4],
                "result": row[5],
                "error": row[6],
                "duration_ms": row[7],
                "created_at": row[8],
            }
        )
    return out


async def get_action_rollup(hours: int = 24) -> dict:
    hours = max(1, int(hours))
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT tool, status, COUNT(*)
            FROM jarvis_action_log
            WHERE created_at >= datetime('now', ?)
            GROUP BY tool, status
            ORDER BY tool
            """,
            (f"-{hours} hours",),
        )
        rows = await cur.fetchall()
    by_tool: dict[str, dict] = {}
    totals = {"ok": 0, "error": 0, "dedup_skip": 0}
    for tool, status, count in rows:
        bucket = by_tool.setdefault(tool, {"ok": 0, "error": 0, "dedup_skip": 0})
        if status in bucket:
            bucket[status] += int(count)
        else:
            bucket[status] = int(count)
        if status in totals:
            totals[status] += int(count)
    return {
        "window_hours": hours,
        "totals": totals,
        "by_tool": [
            {"tool": tool, **counts}
            for tool, counts in sorted(by_tool.items(), key=lambda kv: kv[0])
        ],
    }


# ── Job registry helpers ─────────────────────────────────────────────────────

async def start_job_run(job_name: str, note: str | None = None) -> int:
    now = datetime.utcnow().isoformat()
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO jarvis_job_runs
                (job_name, status, started_at, note)
            VALUES (?, 'running', ?, ?)
            """,
            (job_name, now, note),
        )
        await db.commit()
        return cur.lastrowid or 0


async def finish_job_run(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
    note: str | None = None,
) -> None:
    if not run_id:
        return
    now = datetime.utcnow().isoformat()
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT started_at FROM jarvis_job_runs WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        duration_ms: int | None = None
        if row and row[0]:
            try:
                started = datetime.fromisoformat(row[0])
                duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
            except Exception:
                duration_ms = None
        await db.execute(
            """
            UPDATE jarvis_job_runs
            SET status = ?, finished_at = ?, duration_ms = ?, error = ?,
                note = COALESCE(?, note)
            WHERE id = ?
            """,
            (status, now, duration_ms, _truncate(error, 1000), note, run_id),
        )
        await db.commit()


async def record_job_event(
    job_name: str,
    *,
    status: str,
    error: str | None = None,
    note: str | None = None,
) -> int:
    """Insert a completed job row in one shot (for 'skipped' events, etc.)."""
    now = datetime.utcnow().isoformat()
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO jarvis_job_runs
                (job_name, status, started_at, finished_at, duration_ms, error, note)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (job_name, status, now, now, _truncate(error, 1000), note),
        )
        await db.commit()
        return cur.lastrowid or 0


async def get_job_status(known_jobs: list[str] | None = None) -> list[dict]:
    """Return one row per job with last run, last success, and recent error."""
    async with db_connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT job_name FROM jarvis_job_runs")
        rows = await cur.fetchall()
    seen = {r[0] for r in rows if r[0]}
    if known_jobs:
        seen.update(known_jobs)

    out: list[dict] = []
    for job_name in sorted(seen):
        async with db_connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT id, status, started_at, finished_at, duration_ms, error, note
                FROM jarvis_job_runs
                WHERE job_name = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_name,),
            )
            last = await cur.fetchone()
            cur = await db.execute(
                """
                SELECT started_at
                FROM jarvis_job_runs
                WHERE job_name = ? AND status = 'ok'
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_name,),
            )
            last_ok = await cur.fetchone()
            cur = await db.execute(
                """
                SELECT started_at, error
                FROM jarvis_job_runs
                WHERE job_name = ? AND status = 'error'
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_name,),
            )
            last_err = await cur.fetchone()

        out.append(
            {
                "job_name": job_name,
                "last_status": last[1] if last else None,
                "last_started_at": last[2] if last else None,
                "last_finished_at": last[3] if last else None,
                "last_duration_ms": last[4] if last else None,
                "last_error_snippet": last[5] if last else None,
                "last_note": last[6] if last else None,
                "last_success_at": last_ok[0] if last_ok else None,
                "last_error_at": last_err[0] if last_err else None,
                "last_error_message": last_err[1] if last_err else None,
            }
        )
    return out


async def get_recent_job_runs(limit: int = 30) -> list[dict]:
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, job_name, status, started_at, finished_at, duration_ms, error, note
            FROM jarvis_job_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(max(1, min(200, limit))),),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "job_name": r[1],
            "status": r[2],
            "started_at": r[3],
            "finished_at": r[4],
            "duration_ms": r[5],
            "error": r[6],
            "note": r[7],
        }
        for r in rows
    ]
