"""
Unified daily digest pipeline.

Builds one combined morning digest from briefing, kids/school email, and
research updates, saves a single Notion page, stores the result locally, and
returns a concise notification payload with highlights.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from jarvis_ops import (
    compute_digest_quality,
    compute_digest_source_signature,
    get_effective_digest_sections,
    get_preferences,
)
from memory import DB_PATH, db_connect
from notion_services import save_digest_page

logger = logging.getLogger("jarvis.daily_digest")
ET = ZoneInfo("America/New_York")


async def _init_db():
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_digest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                highlights_json TEXT NOT NULL DEFAULT '[]',
                sections_json TEXT NOT NULL DEFAULT '[]',
                notion_url TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_digest_runs_created_at ON daily_digest_runs(created_at DESC)"
        )
        async with db.execute("PRAGMA table_info(daily_digest_runs)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "source_signature" not in columns:
            await db.execute("ALTER TABLE daily_digest_runs ADD COLUMN source_signature TEXT")
        if "source_state_json" not in columns:
            await db.execute("ALTER TABLE daily_digest_runs ADD COLUMN source_state_json TEXT")
        if "quality_json" not in columns:
            await db.execute("ALTER TABLE daily_digest_runs ADD COLUMN quality_json TEXT")
        await db.commit()


def _rich_text(content: str) -> list[dict]:
    text = (content or "").strip()
    if not text:
        return []
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def _paragraph_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    for paragraph in [p.strip() for p in (text or "").split("\n\n") if p.strip()]:
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text(paragraph)},
            }
        )
    return blocks


def _first_signal_line(text: str, fallback: str = "") -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("-", "•", "*")):
            line = line[1:].strip()
        if len(line) > 180:
            line = line[:177].rstrip() + "..."
        return line
    return fallback


def _format_kids_section(summary: str, items: list[dict]) -> str:
    if not items:
        return summary or "Nothing notable for the kids today."

    lines = [summary.strip() or "Notable kids and school items:", ""]
    for item in items[:5]:
        child = item.get("child", "General")
        subject = item.get("subject", "(no subject)")
        digest = (item.get("digest") or "").strip()
        action = (item.get("action") or "").strip()
        lines.append(f"[{child}] {subject}")
        if digest:
            lines.append(digest)
        if action:
            lines.append(f"Action: {action}")
        lines.append("")
    return "\n".join(line for line in lines if line is not None).strip()


def _build_notion_blocks(summary: str, highlights: list[str], sections: list[dict]) -> list[dict]:
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": _rich_text(summary),
                "icon": {"type": "emoji", "emoji": "📌"},
                "color": "gray_background",
            },
        }
    ]

    if highlights:
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text("Highlights")},
            }
        )
        for item in highlights:
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rich_text(item)},
                }
            )

    for section in sections:
        title = (section.get("title") or "").strip()
        text = (section.get("text") or "").strip()
        if not title or not text:
            continue
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(title)},
            }
        )
        blocks.extend(_paragraph_blocks(text))

    return blocks[:100]


async def _save_run(
    digest_date: str,
    title: str,
    summary: str,
    highlights: list[str],
    sections: list[dict],
    notion_url: str,
    source_signature: str,
    source_state: dict,
    quality: dict,
):
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO daily_digest_runs
            (digest_date, title, summary, highlights_json, sections_json, notion_url, source_signature, source_state_json, quality_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(digest_date) DO UPDATE SET
                title=excluded.title,
                summary=excluded.summary,
                highlights_json=excluded.highlights_json,
                sections_json=excluded.sections_json,
                notion_url=excluded.notion_url,
                source_signature=excluded.source_signature,
                source_state_json=excluded.source_state_json,
                quality_json=excluded.quality_json,
                created_at=datetime('now')
            """,
            (
                digest_date,
                title,
                summary,
                json.dumps(highlights, ensure_ascii=True),
                json.dumps(sections, ensure_ascii=True),
                notion_url or "",
                source_signature,
                json.dumps(source_state, ensure_ascii=True),
                json.dumps(quality, ensure_ascii=True),
            ),
        )
        await db.commit()


async def get_latest_daily_digest() -> dict | None:
    import aiosqlite

    await _init_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_digest_runs ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["highlights"] = json.loads(payload.get("highlights_json") or "[]")
    payload["sections"] = json.loads(payload.get("sections_json") or "[]")
    payload["source_state"] = json.loads(payload.get("source_state_json") or "{}")
    payload["quality"] = json.loads(payload.get("quality_json") or "{}")
    return payload


async def generate_daily_digest(preferences: dict | None = None) -> dict:
    from briefing import generate_briefing
    from daily_research import generate_research_digest
    from kids_digest import generate_kids_digest

    now = datetime.now(ET)
    prefs = preferences or await get_preferences()
    enabled_sections = get_effective_digest_sections(prefs, now)
    digest_date = now.date().isoformat()
    title = f"Daily Digest — {now.strftime('%b %d, %Y')}"

    tasks = {}
    if "briefing" in enabled_sections:
        tasks["briefing"] = generate_briefing()
    if "research" in enabled_sections:
        tasks["research"] = generate_research_digest()
    if "kids" in enabled_sections:
        tasks["kids"] = generate_kids_digest()
    task_names = list(tasks.keys())
    task_results = await asyncio.gather(*tasks.values(), return_exceptions=True) if tasks else []
    results = dict(zip(task_names, task_results))

    briefing_text = ""
    research_text = ""
    kids_summary = ""
    kids_items: list[dict] = []
    research_topic_data: list[dict] = []
    research_stats = {"new_items": 0, "total_items": 0}
    errors: list[str] = []

    briefing_result = results.get("briefing")
    if briefing_result is not None:
        if isinstance(briefing_result, Exception):
            logger.error("Daily digest briefing failed: %s", briefing_result)
            briefing_text = f"(briefing unavailable: {briefing_result})"
            errors.append("Briefing unavailable")
        else:
            briefing_text, _briefing_title = briefing_result

    research_result = results.get("research")
    if research_result is not None:
        if isinstance(research_result, Exception):
            logger.error("Daily digest research failed: %s", research_result)
            research_text = f"(research unavailable: {research_result})"
            errors.append("Research unavailable")
        else:
            research_text, _research_title, research_topic_data, research_stats = research_result

    kids_result = results.get("kids")
    if kids_result is not None:
        if isinstance(kids_result, Exception):
            logger.error("Daily digest kids digest failed: %s", kids_result)
            kids_summary = f"(kids/school digest unavailable: {kids_result})"
            errors.append("Kids digest unavailable")
        else:
            kids_summary = kids_result.get("summary", "")
            kids_items = kids_result.get("items", [])

    kids_text = _format_kids_section(kids_summary, kids_items)
    summary = _first_signal_line(
        briefing_text,
        fallback="Morning digest assembled.",
    )
    if errors:
        summary = f"{summary} Partial data: {', '.join(errors)}."

    highlights = []
    if "briefing" in enabled_sections:
        highlights.append(_first_signal_line(briefing_text, "Briefing ready."))
    if "kids" in enabled_sections:
        highlights.append(_first_signal_line(kids_summary, "Nothing notable for the kids today."))
    if "research" in enabled_sections:
        highlights.append(
            _first_signal_line(
                research_text,
                "No materially new research intel since the last digest.",
            )
        )
    highlights = [item for item in highlights if item]

    sections = []
    if "briefing" in enabled_sections:
        sections.append({"title": "Executive Briefing", "text": briefing_text})
    if "kids" in enabled_sections:
        sections.append({"title": "Kids & School", "text": kids_text})
    if "research" in enabled_sections:
        sections.append({"title": "Research", "text": research_text})

    return {
        "digest_date": digest_date,
        "title": title,
        "summary": summary,
        "highlights": highlights,
        "sections": sections,
        "enabled_sections": enabled_sections,
        "research_topic_data": research_topic_data,
        "research_stats": research_stats,
        "kids_items": kids_items,
    }


def _build_notification_text(title: str, notion_url: str, highlights: list[str], verbosity: str = "highlights", summary: str = "") -> str:
    lines = [f"📌 {title}"]
    if notion_url:
        lines.append(f"Open in Notion: {notion_url}")
    if verbosity == "compact":
        if summary:
            lines.append("")
            lines.append(summary)
        return "\n".join(lines)
    if highlights:
        lines.append("")
        lines.append("Highlights:")
        limit = 2 if verbosity == "highlights" else 5
        for item in highlights[:limit]:
            lines.append(f"• {item}")
    elif summary:
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


def _is_cached_digest_fresh(latest: dict, cache_window_minutes: int) -> bool:
    created_at = str(latest.get("created_at") or "").strip()
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return False
    age_seconds = (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds()
    return age_seconds <= max(60, int(cache_window_minutes)) * 60


async def run_daily_digest(force_refresh: bool = False) -> dict:
    from research_services import mark_research_items_reported

    await _init_db()
    prefs = await get_preferences()
    source_signature, source_state = await compute_digest_source_signature(prefs)
    latest = await get_latest_daily_digest()
    if (
        not force_refresh
        and latest
        and latest.get("digest_date") == datetime.now(ET).date().isoformat()
        and latest.get("source_signature") == source_signature
        and _is_cached_digest_fresh(latest, int(prefs.get("digest_cache_window_minutes", 240)))
    ):
        latest["cached"] = True
        latest["notification_text"] = _build_notification_text(
            latest.get("title", "Daily Digest"),
            latest.get("notion_url", ""),
            latest.get("highlights", []),
            verbosity=str(prefs.get("digest_notification_verbosity", "highlights")),
            summary=str(latest.get("summary", "")),
        )
        return latest

    payload = await generate_daily_digest(preferences=prefs)
    quality = compute_digest_quality(payload["sections"], payload["highlights"])
    try:
        notion_url = await save_digest_page(
            {
                "title": payload["title"],
                "digest_type": "Daily Digest",
                "digest_date": payload["digest_date"],
                "children": _build_notion_blocks(
                    payload["summary"],
                    payload["highlights"],
                    payload["sections"],
                ),
            }
        )
    except Exception as e:
        logger.error("Failed saving daily digest to Notion: %s", e)
        notion_url = ""
    clean_notion_url = notion_url if notion_url.startswith("http") else ""

    if payload["research_stats"].get("new_items", 0) > 0:
        try:
            await mark_research_items_reported(payload["research_topic_data"])
        except Exception as e:
            logger.error("Failed marking research items reported for daily digest: %s", e)

    await _save_run(
        digest_date=payload["digest_date"],
        title=payload["title"],
        summary=payload["summary"],
        highlights=payload["highlights"],
        sections=payload["sections"],
        notion_url=clean_notion_url,
        source_signature=source_signature,
        source_state=source_state,
        quality=quality,
    )

    payload["notion_url"] = clean_notion_url
    payload["cached"] = False
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    payload["source_signature"] = source_signature
    payload["source_state"] = source_state
    payload["quality"] = quality
    payload["notification_text"] = _build_notification_text(
        payload["title"],
        clean_notion_url,
        payload["highlights"],
        verbosity=str(prefs.get("digest_notification_verbosity", "highlights")),
        summary=payload["summary"],
    )
    return payload
