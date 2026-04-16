"""
Operational surfaces for Jarvis:
- skills registry and docs-friendly metadata
- user preferences
- digest freshness/cache policy
- inbox zero rollup
- digest quality scorecard
- source health dashboard
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from memory import DB_PATH, db_connect

ET = ZoneInfo("America/New_York")

DEFAULT_PREFERENCES = {
    "digest_enabled_sections": ["briefing", "kids", "research"],
    "digest_notification_verbosity": "highlights",
    "digest_weekend_mode": "lighter",
    "digest_cache_window_minutes": 240,
    "todoist_nudge_daily_limit": 3,
    "todoist_nudge_mode": "conservative",
    "morning_brief_enabled_sections": [
        "digest",
        "inbox_triage",
        "gmail_triage",
        "action_nudges",
        "home_maintenance",
        "pool_nudge",
    ],
}

SKILLS = [
    {
        "id": "daily_digest",
        "name": "Daily Digest",
        "category": "Executive",
        "summary": "Assemble one unified morning brief — digest (briefing+kids+research) plus iCloud triage, Gmail triage, action nudges, home ops, and pool nudge — then save to Notion and send one Telegram message.",
        "commands": ["/morning_brief", "/daily_digest", "/briefing", "/research", "/kids_digest"],
        "surfaces": ["telegram", "web", "notion"],
        "modules": ["morning_brief.py", "daily_digest.py", "briefing.py", "daily_research.py", "kids_digest.py"],
    },
    {
        "id": "inbox_triage",
        "name": "Inbox Triage",
        "category": "Email",
        "summary": "Scan iCloud and Gmail inboxes, aggressively archive noise, and star human/actionable email.",
        "commands": ["/triage", "/triage_gmail"],
        "surfaces": ["telegram", "web", "gmail", "icloud"],
        "modules": ["inbox_triage.py", "google_services.py", "icloud_mail.py"],
    },
    {
        "id": "action_nudges",
        "name": "Action Nudges",
        "category": "Execution",
        "summary": "Convert inbox signals and intentionality rhythms into concrete Todoist nudges, with dedupe and low-noise rules.",
        "commands": ["/action_nudges", "/nudges"],
        "surfaces": ["telegram", "todoist", "web"],
        "modules": ["nudges.py", "intentionality.py"],
    },
    {
        "id": "finance_console",
        "name": "Finance Console",
        "category": "Finance",
        "summary": "Keep Monarch transactions warm locally, surface spending signals, and summarize weekly trends.",
        "commands": ["/finance_sync", "/finance_digest", "/finance_weekly", "/finance_trend"],
        "surfaces": ["telegram", "web", "notion"],
        "modules": ["financial_db.py", "finance_weekly_digest.py", "monarch_services.py"],
    },
    {
        "id": "finance_inbox",
        "name": "Finance Inbox",
        "category": "Finance",
        "summary": "Process Drive-based finance documents, extract structured packets, and maintain an open-review queue.",
        "commands": ["/finance_inbox", "/finance_open", "/finance_done"],
        "surfaces": ["telegram", "web", "google-drive", "notion"],
        "modules": ["finance_inbox.py", "google_services.py"],
    },
    {
        "id": "fitness",
        "name": "Fitness",
        "category": "Health",
        "summary": "Sync WHOOP readiness data, track PRs and workouts, and surface recovery signals.",
        "commands": ["/fitness_status via chat", "/whoop/authorize"],
        "surfaces": ["telegram", "web", "whoop"],
        "modules": ["fitness_services.py"],
    },
    {
        "id": "pool_ops",
        "name": "Pool Ops",
        "category": "Home",
        "summary": "Track pool chemistry, OmniLogic equipment status, Leslie's data, and weekly maintenance nudges.",
        "commands": ["/pool", "/pool_nudge"],
        "surfaces": ["telegram", "web"],
        "modules": ["pool_services.py", "leslies_services.py", "nudges.py"],
    },
    {
        "id": "home_ops",
        "name": "Home Ops",
        "category": "Home",
        "summary": "Manage recurring home maintenance, due-soon checks, and Todoist queueing.",
        "commands": ["/home"],
        "surfaces": ["telegram", "web", "todoist"],
        "modules": ["home_ops.py"],
    },
    {
        "id": "intentionality",
        "name": "Intentionality",
        "category": "Personal",
        "summary": "Store long-lived themes, sources, and rhythms so Jarvis can keep them alive in digests and nudges.",
        "commands": ["/intentionality", "/i"],
        "surfaces": ["telegram", "todoist", "notion"],
        "modules": ["intentionality.py"],
    },
    {
        "id": "calendar_and_mail",
        "name": "Calendar and Mail",
        "category": "Assistant",
        "summary": "Read family calendars, Gmail, and iCloud mail, and act on them through tool-first workflows.",
        "commands": ["/family_calendar", "chat tools"],
        "surfaces": ["telegram", "web", "gmail", "icloud", "google-calendar"],
        "modules": ["google_services.py", "icloud_calendar.py", "icloud_mail.py", "tools.py"],
    },
    {
        "id": "context_memory",
        "name": "Context Memory",
        "category": "Core",
        "summary": "Persist recent conversation, notes, and profile context injected into Jarvis interactions.",
        "commands": ["chat", "/context via web"],
        "surfaces": ["telegram", "web"],
        "modules": ["memory.py", "brain.py", "tools.py"],
    },
]


async def init_ops_db():
    now = datetime.utcnow().isoformat()
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_preferences (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for key, value in DEFAULT_PREFERENCES.items():
            await db.execute(
                """
                INSERT OR IGNORE INTO jarvis_preferences (key, value_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, json.dumps(value, ensure_ascii=True), now),
            )
        await db.commit()


async def _table_exists(table_name: str) -> bool:
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        )
        return bool(await cur.fetchone())


async def _safe_scalar(query: str, params: tuple = ()) -> str:
    async with db_connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
    if not row:
        return ""
    return "" if row[0] is None else str(row[0])


def _parse_json_value(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _serialize_value(value) -> str:
    return json.dumps(value, ensure_ascii=True)


def _coerce_preference(key: str, value):
    if key in {"digest_cache_window_minutes", "todoist_nudge_daily_limit"}:
        number = int(value)
        if key == "digest_cache_window_minutes":
            return max(15, min(1440, number))
        return max(0, min(20, number))
    if key == "digest_enabled_sections":
        allowed = {"briefing", "kids", "research"}
        cleaned = [item for item in value if item in allowed]
        return cleaned or list(DEFAULT_PREFERENCES[key])
    if key == "morning_brief_enabled_sections":
        allowed = {
            "digest",
            "inbox_triage",
            "gmail_triage",
            "action_nudges",
            "home_maintenance",
            "pool_nudge",
        }
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        cleaned = [item for item in (value or []) if item in allowed]
        return cleaned or list(DEFAULT_PREFERENCES[key])
    if key == "digest_notification_verbosity":
        return value if value in {"compact", "highlights", "full"} else DEFAULT_PREFERENCES[key]
    if key == "digest_weekend_mode":
        return value if value in {"lighter", "normal"} else DEFAULT_PREFERENCES[key]
    if key == "todoist_nudge_mode":
        return value if value in {"conservative", "normal", "aggressive"} else DEFAULT_PREFERENCES[key]
    return value


async def get_preferences() -> dict:
    await init_ops_db()
    async with db_connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, value_json FROM jarvis_preferences")
        rows = await cur.fetchall()
    prefs = dict(DEFAULT_PREFERENCES)
    for key, raw in rows:
        prefs[key] = _parse_json_value(raw)
    return prefs


async def update_preferences(updates: dict) -> dict:
    await init_ops_db()
    now = datetime.utcnow().isoformat()
    clean_updates = {}
    for key, value in updates.items():
        if key not in DEFAULT_PREFERENCES:
            continue
        clean_updates[key] = _coerce_preference(key, value)

    if clean_updates:
        async with db_connect(DB_PATH) as db:
            for key, value in clean_updates.items():
                await db.execute(
                    """
                    INSERT INTO jarvis_preferences (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (key, _serialize_value(value), now),
                )
            await db.commit()
    return await get_preferences()


def get_effective_digest_sections(prefs: dict, now: datetime | None = None) -> list[str]:
    current = now or datetime.now(ET)
    sections = list(prefs.get("digest_enabled_sections") or DEFAULT_PREFERENCES["digest_enabled_sections"])
    if current.weekday() >= 5 and prefs.get("digest_weekend_mode") == "lighter":
        sections = [section for section in sections if section != "research"] or ["briefing", "kids"]
    return sections


def get_effective_morning_sections(prefs: dict, now: datetime | None = None) -> list[str]:
    """Which morning-brief sections should run right now, honoring weekend mode."""
    current = now or datetime.now(ET)
    sections = list(
        prefs.get("morning_brief_enabled_sections")
        or DEFAULT_PREFERENCES["morning_brief_enabled_sections"]
    )
    if current.weekday() >= 5 and prefs.get("digest_weekend_mode") == "lighter":
        # On weekends, drop the noisy workday-only sections.
        sections = [
            s for s in sections
            if s not in {"action_nudges", "gmail_triage"}
        ] or ["digest"]
    return sections


def compute_digest_quality(sections: list[dict], highlights: list[str]) -> dict:
    section_stats = []
    empty_sections = 0
    total_chars = 0
    for section in sections:
        title = str(section.get("title") or "Section")
        text = str(section.get("text") or "").strip()
        chars = len(text)
        empty = chars == 0 or text.startswith("(")
        if empty:
            empty_sections += 1
        total_chars += chars
        section_stats.append({"title": title, "chars": chars, "empty": empty})

    score = 100
    score -= empty_sections * 18
    score -= max(0, 2 - len(highlights)) * 8
    if total_chars < 400:
        score -= 10
    if total_chars > 7000:
        score -= 8
    score = max(0, min(100, score))

    return {
        "score": score,
        "section_count": len(section_stats),
        "empty_sections": empty_sections,
        "highlight_count": len(highlights),
        "total_chars": total_chars,
        "sections": section_stats,
    }


async def compute_digest_source_signature(prefs: dict, now: datetime | None = None) -> tuple[str, dict]:
    current = now or datetime.now(ET)
    state = {
        "date": current.date().isoformat(),
        "hour_bucket": current.strftime("%Y-%m-%dT%H"),
        "sections": get_effective_digest_sections(prefs, current),
        "verbosity": prefs.get("digest_notification_verbosity"),
        "weekend_mode": prefs.get("digest_weekend_mode"),
        "messages_latest": await _safe_scalar("SELECT MAX(timestamp) FROM messages"),
        "triage_latest": await _safe_scalar("SELECT MAX(created_at) FROM inbox_triage_runs") if await _table_exists("inbox_triage_runs") else "",
        "kids_latest": await _safe_scalar("SELECT MAX(created_at) FROM kids_digest") if await _table_exists("kids_digest") else "",
        "research_pref_latest": await _safe_scalar("SELECT MAX(updated_at) FROM research_preferences") if await _table_exists("research_preferences") else "",
        "research_seen_latest": await _safe_scalar("SELECT MAX(last_reported_at) FROM research_seen_items") if await _table_exists("research_seen_items") else "",
        "intentionality_latest": await _safe_scalar("SELECT MAX(updated_at) FROM intentionality_entries") if await _table_exists("intentionality_entries") else "",
        "finance_sync_latest": await _safe_scalar(
            "SELECT MAX(updated_at) FROM financial_sync_state"
        ) if await _table_exists("financial_sync_state") else "",
    }
    encoded = json.dumps(state, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest(), state


async def get_inbox_zero_center(limit: int = 8) -> dict:
    buckets = {
        "triage_stars": [],
        "kids_actions": [],
        "finance_review": [],
        "todoist_nudges": [],
    }

    if await _table_exists("inbox_triage_log"):
        async with db_connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT subject, reason, created_at
                FROM inbox_triage_log
                WHERE action = 'star'
                  AND created_at >= datetime('now', '-3 day')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        for subject, reason, created_at in rows:
            buckets["triage_stars"].append(
                {
                    "title": subject or "(no subject)",
                    "detail": reason or "",
                    "created_at": created_at,
                    "source": "triage",
                    "priority": "medium",
                }
            )

    if await _table_exists("kids_digest"):
        async with db_connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT items_json, created_at
                FROM kids_digest
                ORDER BY created_at DESC
                LIMIT 3
                """
            )
            rows = await cur.fetchall()
        for items_json, created_at in rows:
            items = json.loads(items_json or "[]")
            for item in items:
                action = str(item.get("action") or "").strip()
                if not action:
                    continue
                buckets["kids_actions"].append(
                    {
                        "title": item.get("subject") or "(no subject)",
                        "detail": action,
                        "created_at": created_at,
                        "source": "kids",
                        "priority": item.get("priority", "medium"),
                    }
                )
        buckets["kids_actions"] = buckets["kids_actions"][:limit]

    if await _table_exists("finance_documents"):
        async with db_connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT summary, due_date, amount_due, processed_at
                FROM finance_documents
                WHERE review_status = 'open'
                ORDER BY
                    CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
                    due_date ASC,
                    processed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        for summary, due_date, amount_due, processed_at in rows:
            detail = []
            if due_date:
                detail.append(f"due {due_date}")
            if amount_due is not None:
                detail.append(f"${float(amount_due):,.2f}")
            buckets["finance_review"].append(
                {
                    "title": summary or "Finance item",
                    "detail": " · ".join(detail),
                    "created_at": processed_at,
                    "source": "finance",
                    "priority": "high" if due_date else "medium",
                }
            )

    if await _table_exists("nudge_events"):
        async with db_connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT payload_json, created_at
                FROM nudge_events
                WHERE status = 'created'
                  AND created_at >= datetime('now', '-3 day')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        for payload_json, created_at in rows:
            payload = _parse_json_value(payload_json) or {}
            decision = (payload.get("decision") or {}) if isinstance(payload, dict) else {}
            task = decision.get("task") or payload.get("task") or "Todoist nudge"
            reason = decision.get("reason") or ""
            buckets["todoist_nudges"].append(
                {
                    "title": str(task),
                    "detail": str(reason),
                    "created_at": created_at,
                    "source": "todoist",
                    "priority": "medium",
                }
            )

    total = sum(len(items) for items in buckets.values())
    return {
        "total_items": total,
        "buckets": buckets,
        "summary": {
            "triage_stars": len(buckets["triage_stars"]),
            "kids_actions": len(buckets["kids_actions"]),
            "finance_review": len(buckets["finance_review"]),
            "todoist_nudges": len(buckets["todoist_nudges"]),
        },
    }


async def get_digest_quality_scorecard(days: int = 14) -> dict:
    if not await _table_exists("daily_digest_runs"):
        return {"run_count": 0, "overall_score": None, "sections": [], "suggestions": []}

    async with db_connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT digest_date, title, highlights_json, sections_json, quality_json
            FROM daily_digest_runs
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            """,
            (f"-{max(1, int(days))} day",),
        )
        rows = await cur.fetchall()

    if not rows:
        return {"run_count": 0, "overall_score": None, "sections": [], "suggestions": []}

    total_score = 0
    section_rollup: dict[str, dict] = {}
    for _digest_date, _title, highlights_json, sections_json, quality_json in rows:
        quality = _parse_json_value(quality_json) if quality_json else None
        sections = _parse_json_value(sections_json) or []
        highlights = _parse_json_value(highlights_json) or []
        if not quality:
            quality = compute_digest_quality(sections, highlights)
        total_score += int(quality.get("score") or 0)
        for section in quality.get("sections") or []:
            title = section.get("title") or "Section"
            bucket = section_rollup.setdefault(title, {"runs": 0, "empty": 0, "chars": 0})
            bucket["runs"] += 1
            bucket["chars"] += int(section.get("chars") or 0)
            if section.get("empty"):
                bucket["empty"] += 1

    section_stats = []
    suggestions = []
    for title, bucket in section_rollup.items():
        avg_chars = round(bucket["chars"] / max(1, bucket["runs"]))
        empty_rate = round(bucket["empty"] / max(1, bucket["runs"]) * 100)
        section_stats.append(
            {
                "title": title,
                "avg_chars": avg_chars,
                "empty_rate": empty_rate,
                "runs": bucket["runs"],
            }
        )
        if empty_rate >= 60:
            suggestions.append(f"{title} is empty often ({empty_rate}%); consider reducing frequency or raising its signal threshold.")
        elif avg_chars > 2600:
            suggestions.append(f"{title} runs long on average ({avg_chars} chars); consider tighter limits or shorter summaries.")

    return {
        "run_count": len(rows),
        "overall_score": round(total_score / len(rows)),
        "sections": sorted(section_stats, key=lambda item: item["title"]),
        "suggestions": suggestions[:5],
    }


def _status_payload(name: str, status: str, summary: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "summary": summary, "detail": detail}


async def get_source_health(live: bool = False) -> dict:
    sources = []

    anthropic_ok = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    sources.append(
        _status_payload(
            "Anthropic",
            "ok" if anthropic_ok else "error",
            "API key configured" if anthropic_ok else "ANTHROPIC_API_KEY missing",
            f"chat={os.getenv('CLAUDE_MODEL_CHAT', '')} report={os.getenv('CLAUDE_MODEL_REPORT', '')}",
        )
    )

    notion_token = bool(os.getenv("NOTION_TOKEN", "").strip())
    notion_db = bool(os.getenv("NOTION_DIGESTS_DB_ID", "").strip() or os.getenv("NOTION_NOTES_DB_ID", "").strip())
    sources.append(
        _status_payload(
            "Notion",
            "ok" if notion_token and notion_db else "warn",
            "Notion ready" if notion_token and notion_db else "Token or database config incomplete",
        )
    )

    google_token_path = Path(os.getenv("GOOGLE_TOKEN_FILE", "google_token.json"))
    google_token_ok = google_token_path.exists()
    google_summary = f"token {'present' if google_token_ok else 'missing'}"
    try:
        from google_services import _read_token_scopes

        scopes = _read_token_scopes()
        if "https://www.googleapis.com/auth/gmail.modify" in scopes:
            google_summary += " · gmail scopes ok"
        else:
            google_summary += " · gmail.modify missing"
    except Exception:
        pass
    sources.append(_status_payload("Gmail", "ok" if google_token_ok else "warn", google_summary))

    icloud_email = bool(os.getenv("ICLOUD_EMAIL", "").strip())
    icloud_password = bool(os.getenv("ICLOUD_APP_PASSWORD", "").strip())
    icloud_status = "ok" if icloud_email and icloud_password else "warn"
    icloud_summary = "Credentials configured" if icloud_status == "ok" else "iCloud credentials missing"
    if live and icloud_status == "ok":
        try:
            from icloud_service import icloud

            health = await icloud.health_check()
            if health.get("imap") != "ok" or health.get("caldav") != "ok":
                icloud_status = "warn"
                icloud_summary = f"imap={health.get('imap')} caldav={health.get('caldav')}"
        except Exception as e:
            icloud_status = "warn"
            icloud_summary = f"Live check failed: {e}"
    sources.append(_status_payload("iCloud", icloud_status, icloud_summary))

    monarch_email = bool(os.getenv("MONARCH_EMAIL", "").strip())
    monarch_password = bool(os.getenv("MONARCH_PASSWORD", "").strip())
    monarch_sync = await _safe_scalar(
        "SELECT value FROM financial_sync_state WHERE key='monarch_last_sync_date'"
    ) if await _table_exists("financial_sync_state") else ""
    monarch_summary = f"last sync {monarch_sync}" if monarch_sync else "No local sync yet"
    sources.append(
        _status_payload(
            "Monarch",
            "ok" if monarch_email and monarch_password else "warn",
            monarch_summary if monarch_email and monarch_password else "MONARCH_EMAIL or MONARCH_PASSWORD missing",
        )
    )

    whoop_creds = bool(os.getenv("WHOOP_CLIENT_ID", "").strip() and os.getenv("WHOOP_CLIENT_SECRET", "").strip())
    whoop_token_file = Path(__file__).resolve().parent / "data" / "whoop_tokens.json"
    whoop_latest = await _safe_scalar("SELECT MAX(date) FROM fitness_whoop_daily") if await _table_exists("fitness_whoop_daily") else ""
    whoop_summary = f"latest local sample {whoop_latest}" if whoop_latest else "No WHOOP sample cached"
    sources.append(
        _status_payload(
            "WHOOP",
            "ok" if whoop_creds and whoop_token_file.exists() else "warn",
            whoop_summary if whoop_creds else "WHOOP credentials missing",
        )
    )

    overall = "ok" if all(source["status"] == "ok" for source in sources) else "warn"
    return {"overall": overall, "sources": sources}


def get_skills_registry() -> list[dict]:
    return list(SKILLS)


def get_skills_overview_text() -> str:
    lines = ["Jarvis Skills", ""]
    for skill in SKILLS:
        lines.append(f"- {skill['name']} [{skill['category']}]")
        lines.append(f"  {skill['summary']}")
        if skill.get("commands"):
            lines.append(f"  Commands: {', '.join(skill['commands'])}")
    return "\n".join(lines)


KNOWN_SCHEDULED_JOBS = [
    "financial_sync",
    "morning_brief",
    "weekly_finance_digest",
]


async def get_ops_overview(live_health: bool = False) -> dict:
    from jarvis_reliability import (
        get_action_rollup,
        get_job_status,
        get_recent_actions,
        get_recent_job_runs,
    )

    prefs = await get_preferences()
    try:
        jobs = await get_job_status(KNOWN_SCHEDULED_JOBS)
    except Exception:
        jobs = []
    try:
        recent_runs = await get_recent_job_runs(limit=20)
    except Exception:
        recent_runs = []
    try:
        audit_rollup = await get_action_rollup(hours=24)
    except Exception:
        audit_rollup = {"window_hours": 24, "totals": {}, "by_tool": []}
    try:
        recent_actions = await get_recent_actions(limit=25)
    except Exception:
        recent_actions = []

    return {
        "skills": get_skills_registry(),
        "preferences": prefs,
        "health": await get_source_health(live=live_health),
        "inbox_zero": await get_inbox_zero_center(),
        "digest_quality": await get_digest_quality_scorecard(),
        "jobs": jobs,
        "recent_job_runs": recent_runs,
        "audit_rollup": audit_rollup,
        "recent_actions": recent_actions,
    }
