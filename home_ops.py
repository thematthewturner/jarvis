import asyncio
import os
import re
from datetime import date, datetime, timedelta

import aiosqlite

from memory import db_connect

VALID_CADENCE_UNITS = {
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
}
DEFAULT_CHECK_WINDOW_DAYS = 7
DEFAULT_HOME_TASK_SEEDS = [
    {
        "title": "Replace Air Filters (3)",
        "cadence_value": 60,
        "cadence_unit": "days",
        "days_until_due": 21,
        "notes": "Sizes: 14x25x1, 14x30x1, 12x12x1. Replace all three at the same time.",
    },
    {
        "title": "Fertilize Palm Trees",
        "cadence_value": 3,
        "cadence_unit": "months",
        "days_until_due": 14,
        "notes": "Use your preferred Amazon palm fertilizer listing. Add the exact product link to this note later if you want it embedded in the task.",
    },
    {
        "title": "Truck Oil Change + Tire Rotation",
        "cadence_value": 6,
        "cadence_unit": "months",
        "days_until_due": 21,
        "notes": "Bundle oil change and tire rotation in one service visit.",
    },
    {
        "title": "Replace Smoke / CO Detector Batteries",
        "cadence_value": 12,
        "cadence_unit": "months",
        "days_until_due": 45,
        "notes": "Replace all house detector batteries in one pass and test alarms after swap.",
    },
]
HOME_SOT_MIGRATIONS = [
    {
        "title": "Check Air Filters",
        "notes": "Monthly check. If dirty, replace with sizes 14x25x1, 14x30x1, 12x12x1.",
        "cadence_value": 30,
        "cadence_unit": "days",
        "next_due": "2026-03-18",
        "action": "updated",
        "details": "sot_v1: /home owns air-filter monthly checks",
    },
    {
        "title": "Replace Air Filters (3)",
        "notes": "Sizes: 14x25x1, 14x30x1, 12x12x1. Replace all three at the same time.",
        "cadence_value": 60,
        "cadence_unit": "days",
        "next_due": "2026-04-23",
        "action": "updated",
        "details": "sot_v1: /home owns 60-day air-filter replacements",
    },
    {
        "title": "Fertilize Palm Trees",
        "notes": "Use your preferred Amazon palm fertilizer listing. Add the exact product link to this note later if you want it embedded in the task.",
        "cadence_value": 3,
        "cadence_unit": "months",
        "next_due": "2026-05-28",
        "action": "updated",
        "details": "sot_v1: /home owns palm-tree fertilizing cadence",
    },
    {
        "title": "Replace HEPA Filter",
        "notes": "Replace whole-home HEPA filter. /home now owns the recurrence.",
        "cadence_value": 6,
        "cadence_unit": "months",
        "next_due": "2026-05-21",
        "action": "created",
        "details": "sot_v1: imported HEPA filter recurrence from Todoist",
    },
    {
        "title": "Change Pool Filters",
        "notes": "Change pool filters and inspect pressure after replacement. /home now owns the recurrence.",
        "cadence_value": 3,
        "cadence_unit": "months",
        "next_due": "2026-03-19",
        "action": "created",
        "details": "sot_v1: imported pool-filter recurrence from Todoist",
    },
]


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _format_date(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %Y")
    except ValueError:
        return value


def _normalize_unit(unit: str) -> str:
    normalized = VALID_CADENCE_UNITS.get((unit or "").strip().lower())
    if not normalized:
        raise ValueError("Cadence unit must be days, weeks, or months.")
    return normalized


def _add_months(source: date, months: int) -> date:
    import calendar

    month_index = source.month - 1 + months
    year = source.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(source.day, last_day))


def _advance_date(source: date, cadence_value: int, cadence_unit: str) -> date:
    if cadence_unit == "days":
        return source + timedelta(days=cadence_value)
    if cadence_unit == "weeks":
        return source + timedelta(weeks=cadence_value)
    if cadence_unit == "months":
        return _add_months(source, cadence_value)
    raise ValueError(f"Unsupported cadence unit: {cadence_unit}")


def _advance_schedule_past(
    scheduled_due: date,
    completed_date: date,
    cadence_value: int,
    cadence_unit: str,
) -> date:
    next_due = _advance_date(scheduled_due, cadence_value, cadence_unit)
    while next_due <= completed_date:
        next_due = _advance_date(next_due, cadence_value, cadence_unit)
    return next_due


def _compact_notes(notes: str, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", (notes or "").strip())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _todoist_content(title: str, notes: str) -> str:
    content = title.strip()
    compact = _compact_notes(notes, limit=170)
    if compact:
        content = f"{content} - {compact}"
    if len(content) <= 300:
        return content
    return content[:297].rstrip() + "..."


def _todoist_add_task_sync(content: str, due_string: str) -> tuple[str, str]:
    from todoist_api_python.api import TodoistAPI

    token = os.getenv("TODOIST_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TODOIST_API_TOKEN is not configured.")

    api = TodoistAPI(token)
    task = api.add_task(content=content, due_string=due_string)
    resolved_due = task.due.string if getattr(task, "due", None) else due_string
    return str(task.id), resolved_due


def _todoist_close_task_sync(task_id: str):
    from todoist_api_python.api import TodoistAPI

    token = os.getenv("TODOIST_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TODOIST_API_TOKEN is not configured.")

    api = TodoistAPI(token)
    api.close_task(task_id)


async def init_home_ops_db():
    async with db_connect() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS home_tasks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                title             TEXT NOT NULL UNIQUE,
                notes             TEXT NOT NULL DEFAULT '',
                cadence_value     INTEGER NOT NULL,
                cadence_unit      TEXT NOT NULL,
                next_due          TEXT NOT NULL,
                last_completed    TEXT,
                todoist_task_id   TEXT,
                todoist_created_at TEXT,
                managed_in_home   INTEGER NOT NULL DEFAULT 1,
                is_active         INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )
            """
        )
        async with db.execute("PRAGMA table_info(home_tasks)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "managed_in_home" not in columns:
            await db.execute(
                "ALTER TABLE home_tasks ADD COLUMN managed_in_home INTEGER NOT NULL DEFAULT 1"
            )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS home_task_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                home_task_id  INTEGER NOT NULL,
                action        TEXT NOT NULL,
                details       TEXT NOT NULL DEFAULT '',
                happened_at   TEXT NOT NULL,
                FOREIGN KEY (home_task_id) REFERENCES home_tasks(id) ON DELETE CASCADE
            )
            """
        )
        await db.commit()
        await _seed_default_home_tasks(db)
        await _apply_home_sot_migrations(db)
        await db.commit()


async def _log_history(
    db: aiosqlite.Connection,
    home_task_id: int,
    action: str,
    details: str = "",
):
    await db.execute(
        """
        INSERT INTO home_task_history (home_task_id, action, details, happened_at)
        VALUES (?, ?, ?, ?)
        """,
        (home_task_id, action, details, _utcnow()),
    )


async def _seed_default_home_tasks(db: aiosqlite.Connection):
    async with db.execute("SELECT COUNT(*) FROM home_tasks") as cur:
        row = await cur.fetchone()
    existing_count = int(row[0]) if row else 0
    if existing_count > 0:
        return

    now = _utcnow()
    today = date.today()

    for seed in DEFAULT_HOME_TASK_SEEDS:
        due_date = today + timedelta(days=int(seed["days_until_due"]))
        cursor = await db.execute(
            """
            INSERT INTO home_tasks (
                title, notes, cadence_value, cadence_unit, next_due,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seed["title"],
                seed["notes"],
                int(seed["cadence_value"]),
                _normalize_unit(seed["cadence_unit"]),
                due_date.isoformat(),
                now,
                now,
            ),
        )
        await _log_history(
            db,
            cursor.lastrowid,
            "seeded",
            f"Seeded default task; first due {_format_date(due_date.isoformat())}",
        )


async def _apply_home_sot_migrations(db: aiosqlite.Connection):
    for item in HOME_SOT_MIGRATIONS:
        async with db.execute(
            """
            SELECT 1
            FROM home_task_history
            WHERE details = ?
            LIMIT 1
            """,
            (item["details"],),
        ) as cur:
            if await cur.fetchone():
                continue

        async with db.execute(
            "SELECT id FROM home_tasks WHERE title = ? LIMIT 1",
            (item["title"],),
        ) as cur:
            row = await cur.fetchone()

        if row:
            task_id = row[0]
            await db.execute(
                """
                UPDATE home_tasks
                SET notes = ?, cadence_value = ?, cadence_unit = ?, next_due = ?,
                    managed_in_home = 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    item["notes"],
                    int(item["cadence_value"]),
                    _normalize_unit(item["cadence_unit"]),
                    item["next_due"],
                    _utcnow(),
                    task_id,
                ),
            )
        else:
            cursor = await db.execute(
                """
                INSERT INTO home_tasks (
                    title, notes, cadence_value, cadence_unit, next_due,
                    managed_in_home, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    item["title"],
                    item["notes"],
                    int(item["cadence_value"]),
                    _normalize_unit(item["cadence_unit"]),
                    item["next_due"],
                    _utcnow(),
                    _utcnow(),
                ),
            )
            task_id = cursor.lastrowid

        await _log_history(
            db,
            task_id,
            item["action"],
            item["details"],
        )


def parse_home_add_input(raw: str) -> dict:
    if not raw.strip():
        raise ValueError(
            "Use: /home add Title | every 90 days | starts 2026-03-15 | Notes"
        )

    parts = [part.strip() for part in raw.split("|") if part.strip()]
    if len(parts) < 2:
        raise ValueError(
            "Use: /home add Title | every 90 days | starts 2026-03-15 | Notes"
        )

    title = parts[0]
    cadence_match = re.fullmatch(
        r"every\s+(\d+)\s+(day|days|week|weeks|month|months)",
        parts[1],
        re.IGNORECASE,
    )
    if not cadence_match:
        raise ValueError("Cadence must look like: every 90 days or every 6 months.")

    cadence_value = int(cadence_match.group(1))
    if cadence_value <= 0:
        raise ValueError("Cadence value must be greater than 0.")

    cadence_unit = _normalize_unit(cadence_match.group(2))
    start_date = None
    notes_parts = []

    for part in parts[2:]:
        start_match = re.fullmatch(
            r"(?:starts|due)\s+(\d{4}-\d{2}-\d{2})",
            part,
            re.IGNORECASE,
        )
        if start_match and start_date is None:
            start_date = start_match.group(1)
            continue
        notes_parts.append(part)

    return {
        "title": title,
        "cadence_value": cadence_value,
        "cadence_unit": cadence_unit,
        "notes": " | ".join(notes_parts),
        "start_date": start_date,
    }


async def add_home_task(
    title: str,
    cadence_value: int,
    cadence_unit: str,
    notes: str = "",
    start_date: str | None = None,
) -> str:
    await init_home_ops_db()

    title = (title or "").strip()
    if not title:
        raise ValueError("Title is required.")
    if cadence_value <= 0:
        raise ValueError("Cadence value must be greater than 0.")

    cadence_unit = _normalize_unit(cadence_unit)
    due_date = (
        datetime.fromisoformat(start_date).date()
        if start_date
        else date.today()
    )
    now = _utcnow()

    async with db_connect() as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO home_tasks (
                    title, notes, cadence_value, cadence_unit, next_due,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    (notes or "").strip(),
                    cadence_value,
                    cadence_unit,
                    due_date.isoformat(),
                    now,
                    now,
                ),
            )
        except aiosqlite.IntegrityError:
            return f"Home task already exists: {title}"

        task_id = cursor.lastrowid
        await _log_history(
            db,
            task_id,
            "created",
            f"Due {_format_date(due_date.isoformat())}; every {cadence_value} {cadence_unit}",
        )
        await db.commit()

    note_line = f"\nNotes: {_compact_notes(notes)}" if (notes or "").strip() else ""
    return (
        f"Home task added: [{task_id}] {title}\n"
        f"Next due: {_format_date(due_date.isoformat())}\n"
        f"Cadence: every {cadence_value} {cadence_unit}"
        f"{note_line}"
    )


async def _fetch_tasks(active_only: bool = True) -> list[dict]:
    await init_home_ops_db()

    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT *
            FROM home_tasks
            WHERE (? = 0 OR is_active = 1)
            ORDER BY next_due ASC, title ASC
        """
        async with db.execute(sql, (1 if active_only else 0,)) as cur:
            rows = [dict(row) for row in await cur.fetchall()]
    return rows


async def get_home_status(days_ahead: int = 30) -> str:
    tasks = await _fetch_tasks(active_only=True)
    if not tasks:
        return (
            "No home maintenance tasks yet.\n\n"
            "Add one with:\n"
            "/home add Replace Air Filters (3) | every 90 days | "
            "starts 2026-03-15 | Sizes: 16x20x1, 20x20x1, 20x25x1"
        )

    today = date.today()
    window_end = today + timedelta(days=max(days_ahead, 0))
    due_now = []
    due_later = []
    recent = []

    for task in tasks:
        next_due = datetime.fromisoformat(task["next_due"]).date()
        if next_due <= window_end:
            due_now.append(task)
        else:
            due_later.append(task)
        if task.get("last_completed"):
            recent.append(task)

    lines = [
        "Home maintenance",
        f"Active tasks: {len(tasks)}",
        "",
        f"Due now / next {days_ahead} days:",
    ]

    if due_now:
        for task in due_now[:8]:
            cadence = f"every {task['cadence_value']} {task['cadence_unit']}"
            due_label = _format_date(task["next_due"])
            todoist_tag = " [Todoist queued]" if task.get("todoist_task_id") else ""
            lines.append(
                f"- [{task['id']}] {task['title']} - due {due_label} - {cadence}{todoist_tag}"
            )
            if task.get("notes"):
                lines.append(f"  Notes: {_compact_notes(task['notes'])}")
    else:
        lines.append("- Nothing due in that window.")

    if due_later:
        lines.extend(["", "Later on:"])
        for task in due_later[:3]:
            lines.append(
                f"- [{task['id']}] {task['title']} - due {_format_date(task['next_due'])}"
            )

    if recent:
        lines.extend(["", "Recently completed:"])
        for task in sorted(
            recent,
            key=lambda row: row.get("last_completed") or "",
            reverse=True,
        )[:3]:
            lines.append(
                f"- [{task['id']}] {task['title']} - last done {_format_date(task['last_completed'])}"
            )

    lines.extend(
        [
            "",
            "Commands:",
            "/home add Title | every 90 days | starts 2026-03-15 | Notes",
            "/home info <id>",
            "/home done <id>",
            "/home check",
        ]
    )
    return "\n".join(lines)


async def get_home_dashboard(days_ahead: int = 30, limit: int = 5) -> dict:
    tasks = await _fetch_tasks(active_only=True)
    today = date.today()
    window_end = today + timedelta(days=max(days_ahead, 0))
    due_now = []

    for task in tasks:
        next_due = datetime.fromisoformat(task["next_due"]).date()
        if next_due <= window_end:
            due_now.append(task)

    due_now.sort(key=lambda row: (row.get("next_due") or "", row.get("title") or ""))
    next_due_source = due_now[0] if due_now else (tasks[0] if tasks else None)
    next_due_label = _format_date(next_due_source["next_due"]) if next_due_source else None

    return {
        "count": len(due_now),
        "total_active": len(tasks),
        "next_due": next_due_label,
        "items": [
            {
                "id": task["id"],
                "title": task["title"],
                "due": _format_date(task["next_due"]),
                "days_until": (
                    datetime.fromisoformat(task["next_due"]).date() - today
                ).days,
                "notes": _compact_notes(task.get("notes", ""), limit=110),
                "todoist_queued": bool(task.get("todoist_task_id")),
            }
            for task in due_now[:limit]
        ],
    }


async def get_home_task_details(task_id: int) -> str:
    await init_home_ops_db()

    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM home_tasks WHERE id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return f"Home task not found: {task_id}"

        async with db.execute(
            """
            SELECT action, details, happened_at
            FROM home_task_history
            WHERE home_task_id = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (task_id,),
        ) as cur:
            history = await cur.fetchall()

    task = dict(row)
    lines = [
        f"[{task['id']}] {task['title']}",
        f"Source of truth: {'/home' if task.get('managed_in_home', 1) else 'external'}",
        f"Next due: {_format_date(task['next_due'])}",
        f"Cadence: every {task['cadence_value']} {task['cadence_unit']}",
        f"Last completed: {_format_date(task.get('last_completed'))}",
        f"Todoist linked: {'yes' if task.get('todoist_task_id') else 'no'}",
        "",
        "Notes:",
        task.get("notes") or "(none)",
    ]

    if history:
        lines.extend(["", "Recent history:"])
        for action, details, happened_at in history:
            stamp = _format_date(happened_at)
            suffix = f" - {details}" if details else ""
            lines.append(f"- {stamp}: {action}{suffix}")

    return "\n".join(lines)


async def run_home_maintenance_check(
    days_ahead: int = DEFAULT_CHECK_WINDOW_DAYS,
    notify_empty: bool = True,
) -> str:
    tasks = await _fetch_tasks(active_only=True)
    if not tasks:
        return "No home maintenance tasks configured." if notify_empty else ""

    today = date.today()
    window_end = today + timedelta(days=max(days_ahead, 0))
    created = []
    still_due = []
    errors = []

    async with db_connect() as db:
        for task in tasks:
            next_due = datetime.fromisoformat(task["next_due"]).date()
            if next_due > window_end:
                continue

            if task.get("todoist_task_id"):
                if next_due <= today:
                    due_label = "today" if next_due == today else _format_date(task["next_due"])
                    still_due.append(
                        f"- [{task['id']}] {task['title']} still open (due {due_label})"
                    )
                continue

            content = _todoist_content(task["title"], task.get("notes", ""))
            try:
                todoist_task_id, resolved_due = await asyncio.to_thread(
                    _todoist_add_task_sync,
                    content,
                    task["next_due"],
                )
            except Exception as exc:
                errors.append(f"{task['title']}: {exc}")
                continue

            await db.execute(
                """
                UPDATE home_tasks
                SET todoist_task_id = ?, todoist_created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (todoist_task_id, _utcnow(), _utcnow(), task["id"]),
            )
            await _log_history(
                db,
                task["id"],
                "todoist_created",
                f"Task {todoist_task_id} due {resolved_due}",
            )
            created.append(
                f"- [{task['id']}] {task['title']} -> Todoist ({resolved_due})"
            )

        await db.commit()

    if created:
        lines = [
            "Home maintenance sync complete.",
            f"Created {len(created)} Todoist task(s):",
            *created,
        ]
        if still_due:
            lines.extend(
                [
                    "",
                    "Still due:",
                    *still_due[:6],
                ]
            )
        if errors:
            lines.extend(["", "Errors:", *[f"- {err}" for err in errors[:4]]])
        return "\n".join(lines)

    if still_due:
        lines = [
            "Home maintenance still due:",
            *still_due[:6],
        ]
        if errors:
            lines.extend(["", "Errors:", *[f"- {err}" for err in errors[:4]]])
        return "\n".join(lines)

    if errors:
        return "Home maintenance sync failed:\n" + "\n".join(f"- {err}" for err in errors[:4])

    return "No home maintenance tasks needed Todoist sync." if notify_empty else ""


async def complete_home_task(task_id: int, completed_on: str | None = None) -> str:
    await init_home_ops_db()

    try:
        completed_date = (
            datetime.fromisoformat(completed_on).date()
            if completed_on
            else date.today()
        )
    except ValueError:
        raise ValueError("Completion date must be YYYY-MM-DD.")

    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM home_tasks WHERE id = ? AND is_active = 1",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return f"Home task not found: {task_id}"

        task = dict(row)
        scheduled_due = datetime.fromisoformat(task["next_due"]).date()
        next_due = _advance_schedule_past(
            scheduled_due,
            completed_date,
            int(task["cadence_value"]),
            task["cadence_unit"],
        )

        todoist_close_error = None
        if task.get("todoist_task_id"):
            try:
                await asyncio.to_thread(_todoist_close_task_sync, task["todoist_task_id"])
            except Exception as exc:
                todoist_close_error = str(exc)

        await db.execute(
            """
            UPDATE home_tasks
            SET last_completed = ?, next_due = ?, todoist_task_id = NULL,
                todoist_created_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                completed_date.isoformat(),
                next_due.isoformat(),
                _utcnow(),
                task_id,
            ),
        )
        await _log_history(
            db,
            task_id,
            "completed",
            f"Completed {_format_date(completed_date.isoformat())}; next due {_format_date(next_due.isoformat())}",
        )
        await db.commit()

    lines = [
        f"Home task completed: [{task_id}] {task['title']}",
        f"Next due: {_format_date(next_due.isoformat())}",
    ]
    if todoist_close_error:
        lines.append(f"Todoist close warning: {todoist_close_error}")
    return "\n".join(lines)
