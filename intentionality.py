"""
Intentionality memory and nudges.

Stores long-lived personal growth themes, reading anchors, and recurring
relational practices so Jarvis can keep them in view inside digests and
occasionally turn them into Todoist prompts.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis.intentionality")
ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(ET)


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{raw}T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _clamp_priority(value: Any, default: int = 2) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = default
    return max(1, min(4, priority))


def _hydrate_entry(row: aiosqlite.Row) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = _safe_json_loads(item.pop("metadata_json", None))
    item["active"] = bool(item.get("active", 0))
    item["due_at"] = None
    if item.get("entry_type") == "rhythm":
        due_at = _compute_rhythm_due_at(item)
        item["due_at"] = due_at.isoformat() if due_at else None
    return item


async def init_intentionality_db():
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS intentionality_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS intentionality_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                unique_key TEXT NOT NULL UNIQUE,
                related_entry_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _insert_entry(
    entry_type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    await init_intentionality_db()
    now = _utc_iso()
    meta = metadata or {}
    async with db_connect(DB_PATH) as db:
        if replace_existing:
            await db.execute(
                """
                UPDATE intentionality_entries
                SET active = 0, updated_at = ?
                WHERE entry_type = ? AND active = 1
                """,
                (now, entry_type),
            )
        cursor = await db.execute(
            """
            INSERT INTO intentionality_entries
            (entry_type, content, metadata_json, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                entry_type,
                content.strip(),
                json.dumps(meta, ensure_ascii=True),
                now,
                now,
            ),
        )
        await db.commit()
        entry_id = cursor.lastrowid

    return await get_entry(entry_id)


async def get_entry(entry_id: int) -> dict[str, Any] | None:
    await init_intentionality_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM intentionality_entries WHERE id = ? LIMIT 1",
            (entry_id,),
        ) as cur:
            row = await cur.fetchone()
    return _hydrate_entry(row) if row else None


async def get_active_entries(entry_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    await init_intentionality_db()
    params: list[Any] = []
    query = """
        SELECT *
        FROM intentionality_entries
        WHERE active = 1
    """
    if entry_type:
        query += " AND entry_type = ?"
        params.append(entry_type)
    query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [_hydrate_entry(r) for r in rows]


async def set_primary_theme(theme: str) -> dict[str, Any]:
    return await _insert_entry("theme", theme, replace_existing=True)


async def add_context_note(note: str) -> dict[str, Any]:
    return await _insert_entry("context", note)


async def add_source_anchor(
    reference: str,
    source_kind: str = "other",
    note: str = "",
) -> dict[str, Any]:
    kind = (source_kind or "other").strip().lower()
    if kind not in {"scripture", "book", "other"}:
        kind = "other"
    metadata = {"source_kind": kind}
    if note.strip():
        metadata["note"] = note.strip()
    return await _insert_entry("source", reference, metadata=metadata)


async def add_rhythm(
    description: str,
    cadence_days: int,
    task_text: str | None = None,
    due_string: str = "today",
    priority: int = 2,
    last_completed_at: str | None = None,
) -> dict[str, Any]:
    cadence_days = max(1, int(cadence_days))
    metadata = {
        "cadence_days": cadence_days,
        "task_text": (task_text or description).strip(),
        "due_string": (due_string or "today").strip(),
        "priority": _clamp_priority(priority),
        "last_completed_at": last_completed_at or _now_et().replace(microsecond=0).isoformat(),
    }
    return await _insert_entry("rhythm", description, metadata=metadata)


def _compute_rhythm_due_at(entry: dict[str, Any]) -> datetime | None:
    metadata = entry.get("metadata") or {}
    try:
        cadence_days = max(1, int(metadata.get("cadence_days", 0)))
    except (TypeError, ValueError):
        return None

    anchor = (
        _parse_iso(metadata.get("last_completed_at"))
        or _parse_iso(entry.get("updated_at"))
        or _parse_iso(entry.get("created_at"))
    )
    if not anchor:
        return None
    return anchor + timedelta(days=cadence_days)


async def mark_rhythm_done(entry_id: int, completed_at: str | None = None) -> str:
    await init_intentionality_db()
    entry = await get_entry(entry_id)
    if not entry or entry.get("entry_type") != "rhythm" or not entry.get("active"):
        raise ValueError(f"No active rhythm found for id {entry_id}")

    timestamp = completed_at or _now_et().replace(microsecond=0).isoformat()
    metadata = entry.get("metadata") or {}
    metadata["last_completed_at"] = timestamp

    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE intentionality_entries
            SET metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=True), _utc_iso(), entry_id),
        )
        await db.commit()
    return timestamp


async def save_signal_from_chat(
    signal_type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    entry_type = (signal_type or "").strip().lower()
    meta = metadata or {}
    text = (content or "").strip()
    if not text:
        raise ValueError("Content is required")

    if entry_type == "theme":
        entry = await set_primary_theme(text)
        return f"Intentionality theme saved (id {entry['id']}): {entry['content']}"
    if entry_type == "context":
        entry = await add_context_note(text)
        return f"Intentionality context saved (id {entry['id']})."
    if entry_type == "source":
        entry = await add_source_anchor(
            reference=text,
            source_kind=str(meta.get("source_kind", "other")),
            note=str(meta.get("note", "")),
        )
        kind = entry["metadata"].get("source_kind", "other")
        return f"Intentionality {kind} anchor saved (id {entry['id']})."
    if entry_type == "rhythm":
        cadence_days = meta.get("cadence_days")
        if cadence_days is None:
            raise ValueError("Rhythm signals require metadata.cadence_days")
        entry = await add_rhythm(
            description=text,
            cadence_days=int(cadence_days),
            task_text=str(meta.get("task_text") or text),
            due_string=str(meta.get("due_string") or "today"),
            priority=_clamp_priority(meta.get("priority"), default=2),
            last_completed_at=str(meta.get("last_completed_at") or "") or None,
        )
        return f"Intentionality rhythm saved (id {entry['id']}) every {entry['metadata']['cadence_days']} days."

    raise ValueError(f"Unsupported intentionality signal type: {signal_type}")


def _format_source_label(source: dict[str, Any]) -> str:
    kind = source.get("metadata", {}).get("source_kind", "other")
    if kind == "scripture":
        note = source.get("metadata", {}).get("note")
        if note:
            return f"NIV anchor: {source['content']} ({note})"
        return f"NIV anchor: {source['content']}"
    if kind == "book":
        note = source.get("metadata", {}).get("note")
        if note:
            return f"Book anchor: {source['content']} ({note})"
        return f"Book anchor: {source['content']}"
    note = source.get("metadata", {}).get("note")
    if note:
        return f"Anchor: {source['content']} ({note})"
    return f"Anchor: {source['content']}"


def _humanize_days(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return f"in {days} days"
    if 27 <= days <= 31:
        return "in about a month"
    return f"in {days} days"


def _summarize_rhythm_due(entry: dict[str, Any], now: datetime) -> tuple[str, int] | None:
    due_at = _compute_rhythm_due_at(entry)
    if not due_at:
        return None
    delta_days = (due_at.date() - now.date()).days
    description = entry.get("content", "").strip()
    if delta_days < 0:
        return (f"Rhythm overdue: {description} ({abs(delta_days)} day(s) late).", delta_days)
    if delta_days == 0:
        return (f"Rhythm due today: {description}.", delta_days)
    if delta_days <= 7:
        return (f"Rhythm coming up: {description} {_humanize_days(delta_days)}.", delta_days)
    return None


async def get_intentionality_snapshot() -> dict[str, Any]:
    await init_intentionality_db()
    themes = await get_active_entries("theme", limit=1)
    context_entries = await get_active_entries("context", limit=5)
    sources = await get_active_entries("source", limit=5)
    rhythms = await get_active_entries("rhythm", limit=20)

    now = _now_et()
    due_rhythms: list[dict[str, Any]] = []
    for rhythm in rhythms:
        due_at = _compute_rhythm_due_at(rhythm)
        if not due_at:
            continue
        rhythm["due_at"] = due_at.isoformat()
        rhythm["days_until_due"] = (due_at.date() - now.date()).days
        due_rhythms.append(rhythm)

    due_rhythms.sort(key=lambda item: item.get("days_until_due", 9999))

    return {
        "theme": themes[0] if themes else None,
        "context": context_entries,
        "sources": sources,
        "rhythms": due_rhythms,
    }


async def format_intentionality_status() -> str:
    snapshot = await get_intentionality_snapshot()
    lines: list[str] = ["Intentionality"]

    theme = snapshot.get("theme")
    if theme:
        lines.append(f"Theme: {theme['content']}")
    else:
        lines.append("Theme: not set")

    sources = snapshot.get("sources") or []
    if sources:
        lines.append("")
        lines.append("Anchors:")
        for source in sources[:4]:
            lines.append(f"- #{source['id']} {_format_source_label(source)}")

    context_entries = snapshot.get("context") or []
    if context_entries:
        lines.append("")
        lines.append("Context:")
        for entry in context_entries[:4]:
            lines.append(f"- #{entry['id']} {entry['content']}")

    rhythms = snapshot.get("rhythms") or []
    if rhythms:
        lines.append("")
        lines.append("Rhythms:")
        now = _now_et()
        for rhythm in rhythms[:6]:
            summary = _summarize_rhythm_due(rhythm, now)
            if summary:
                text, _ = summary
            else:
                due_at = _parse_iso(rhythm.get("due_at"))
                if due_at:
                    text = f"{rhythm['content']} ({_humanize_days((due_at.date() - now.date()).days)})"
                else:
                    text = rhythm["content"]
            lines.append(f"- #{rhythm['id']} {text}")

    if len(lines) == 2 and not sources and not context_entries and not rhythms:
        lines.append("")
        lines.append("Use /intentionality set, /intentionality source, or /intentionality rhythm to seed it.")

    return "\n".join(lines)


async def generate_digest_nudge() -> str:
    snapshot = await get_intentionality_snapshot()
    parts: list[str] = []

    theme = snapshot.get("theme")
    if theme:
        parts.append(f"Hold this theme today: {theme['content']}.")

    sources = snapshot.get("sources") or []
    if sources:
        parts.append(f"{_format_source_label(sources[0])}.")

    rhythms = snapshot.get("rhythms") or []
    now = _now_et()
    for rhythm in rhythms:
        summary = _summarize_rhythm_due(rhythm, now)
        if summary:
            parts.append(summary[0])
            break

    context_entries = snapshot.get("context") or []
    if context_entries and len(parts) < 3:
        parts.append(f"Remember: {context_entries[0]['content']}.")

    return " ".join(parts[:3]).strip()


async def _event_exists(unique_key: str) -> bool:
    await init_intentionality_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 FROM intentionality_events WHERE unique_key = ? LIMIT 1",
            (unique_key,),
        ) as cur:
            row = await cur.fetchone()
    return bool(row)


async def _save_event(
    event_type: str,
    unique_key: str,
    related_entry_id: int | None,
    payload: dict[str, Any],
):
    await init_intentionality_db()
    now = _utc_iso()
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO intentionality_events
            (event_type, unique_key, related_entry_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                unique_key,
                related_entry_id,
                json.dumps(payload, ensure_ascii=True),
                now,
            ),
        )
        await db.commit()


def _extract_task_id(text: str) -> str | None:
    match = re.search(r"\[task_id:([^\]]+)\]", text or "")
    return match.group(1).strip() if match else None


def _create_task_sync(content: str, due_string: str, priority: int) -> tuple[str, str | None]:
    from todoist_api_python.api import TodoistAPI
    token = os.getenv("TODOIST_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TODOIST_API_TOKEN is not configured.")
    api = TodoistAPI(token)
    task = api.add_task(content=content, due_string=due_string or "")
    if priority in (2, 3, 4):
        try:
            api.update_task(task.id, priority=priority)
        except Exception as exc:
            logger.warning("Failed to set Todoist priority for %s: %s", task.id, exc)
    due_part = f" (due: {task.due.string})" if task.due else ""
    return (f"Task added: {task.content}{due_part} [task_id:{task.id}]", str(task.id))


async def _create_task(content: str, due_string: str, priority: int) -> tuple[str, str | None]:
    import asyncio

    return await asyncio.to_thread(_create_task_sync, content, due_string, priority)


async def run_intentionality_todo_nudges(notify_empty: bool = False) -> str:
    snapshot = await get_intentionality_snapshot()
    rhythms = snapshot.get("rhythms") or []
    if not rhythms:
        return "No intentionality rhythms configured." if notify_empty else ""

    now = _now_et()
    created: list[str] = []

    for rhythm in rhythms:
        due_at = _compute_rhythm_due_at(rhythm)
        if not due_at or due_at.date() > now.date():
            continue

        due_key = due_at.date().isoformat()
        unique_key = f"intentionality_rhythm:{rhythm['id']}:{due_key}"
        if await _event_exists(unique_key):
            continue

        metadata = rhythm.get("metadata") or {}
        task_text = str(metadata.get("task_text") or rhythm.get("content") or "").strip()
        if not task_text:
            continue
        due_string = str(metadata.get("due_string") or "today").strip()
        priority = _clamp_priority(metadata.get("priority"), default=2)

        try:
            _, task_id = await _create_task(task_text[:140], due_string=due_string, priority=priority)
        except Exception as exc:
            logger.error("Intentionality task creation failed for rhythm %s: %s", rhythm["id"], exc)
            continue

        days_late = max(0, (now.date() - due_at.date()).days)
        await _save_event(
            event_type="todo_task",
            unique_key=unique_key,
            related_entry_id=int(rhythm["id"]),
            payload={
                "task_id": task_id,
                "task_text": task_text,
                "due_string": due_string,
                "days_late": days_late,
            },
        )
        if days_late:
            created.append(f"{task_text} (due {days_late} day(s) ago)")
        else:
            created.append(f"{task_text} (due now)")

    if not created:
        return "No intentionality tasks were due today." if notify_empty else ""

    lines = [f"Intentionality nudges created {len(created)} Todoist task(s):", ""]
    for item in created[:6]:
        lines.append(f"- {item}")
    if len(created) > 6:
        lines.append(f"- ... and {len(created) - 6} more")
    return "\n".join(lines)


def _normalize_pipe_parts(raw: str) -> list[str]:
    return [part.strip() for part in raw.split("|") if part.strip()]


def parse_cadence_text(cadence_text: str) -> int:
    value = (cadence_text or "").strip().lower()
    if value == "daily":
        return 1
    if value == "weekly":
        return 7
    if value == "biweekly":
        return 14
    if value == "monthly":
        return 30
    if value == "quarterly":
        return 90

    match = re.search(r"(\d+)", value)
    if not match:
        raise ValueError("Could not parse cadence. Use 'daily', 'weekly', 'monthly', or 'every 30 days'.")
    return max(1, int(match.group(1)))


def parse_source_input(raw: str) -> dict[str, str]:
    parts = _normalize_pipe_parts(raw)
    if not parts:
        raise ValueError("Usage: /intentionality source scripture|book|other | reference | optional note")
    if len(parts) == 1:
        return {"source_kind": "other", "reference": parts[0], "note": ""}
    if len(parts) == 2:
        return {"source_kind": parts[0].lower(), "reference": parts[1], "note": ""}
    return {"source_kind": parts[0].lower(), "reference": parts[1], "note": parts[2]}


def parse_study_input(args: list[str]) -> dict[str, Any]:
    if len(args) < 2:
        raise ValueError("Usage: /intentionality study <daily|weekly|monthly|every 30 days> <scripture reference>")

    cadence_days = parse_cadence_text(args[0])
    reference = " ".join(args[1:]).strip()
    if not reference:
        raise ValueError("Usage: /intentionality study <daily|weekly|monthly|every 30 days> <scripture reference>")

    return {
        "reference": reference,
        "cadence_days": cadence_days,
        "task_text": f"Study {reference}",
        "note": f"{args[0].strip().lower()} study",
    }


def parse_rhythm_input(raw: str) -> dict[str, Any]:
    parts = _normalize_pipe_parts(raw)
    if len(parts) < 2:
        raise ValueError(
            "Usage: /intentionality rhythm <description> | every 30 days\n"
            "Or: /intentionality rhythm <description> | weekly|monthly|quarterly"
        )

    cadence_days = parse_cadence_text(parts[1])

    payload: dict[str, Any] = {
        "description": parts[0],
        "cadence_days": cadence_days,
        "task_text": parts[2] if len(parts) >= 3 else parts[0],
    }

    if len(parts) >= 4 and parts[3]:
        payload["due_string"] = parts[3]
    if len(parts) >= 5 and parts[4]:
        payload["priority"] = _clamp_priority(parts[4])
    return payload
