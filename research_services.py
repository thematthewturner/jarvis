"""
RSS-based daily research fetcher.

Pulls latest headlines from Google News RSS for configured topics.
No API key needed — free, token-efficient (headlines only, not full articles).
"""
import asyncio
import hashlib
import logging
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import feedparser

from memory import db_connect

logger = logging.getLogger("jarvis")

DEFAULT_RESEARCH_TOPICS = [
    {
        "name": "Healthcare & Medicare",
        "emoji": "🏥",
        "queries": [
            "medicare advantage trends 2026",
            "CMS healthcare policy medicare",
            "health insurance industry news",
        ],
    },
    {
        "name": "AI Breakthroughs",
        "emoji": "🤖",
        "queries": [
            "artificial intelligence breakthrough research 2026",
            "LLM AI model release OpenAI Anthropic Google",
        ],
    },
]
RESEARCH_TOPICS = DEFAULT_RESEARCH_TOPICS  # Backward-compatible alias.


async def init_research_db():
    async with db_connect() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS research_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS research_seen_items (
                signature TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT,
                source TEXT,
                topic TEXT,
                first_seen_at TEXT NOT NULL,
                last_reported_at TEXT NOT NULL,
                report_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_seen_last ON research_seen_items(last_reported_at DESC)"
        )
        await db.commit()


async def get_focus_topic_prompt() -> str:
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT value FROM research_preferences WHERE key='focus_topic_prompt' LIMIT 1"
        )
        row = await cur.fetchone()
    return (row[0] if row else "").strip()


async def set_focus_topic_prompt(prompt: str) -> str:
    clean = " ".join((prompt or "").strip().split())
    if not clean:
        raise ValueError("Focus topic prompt cannot be empty.")
    now = datetime.utcnow().isoformat()
    async with db_connect() as db:
        await db.execute(
            """
            INSERT INTO research_preferences (key, value, updated_at)
            VALUES ('focus_topic_prompt', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (clean, now),
        )
        await db.commit()
    return clean


async def clear_focus_topic_prompt() -> None:
    async with db_connect() as db:
        await db.execute("DELETE FROM research_preferences WHERE key='focus_topic_prompt'")
        await db.commit()


def _build_focus_topic(prompt: str) -> dict:
    clean = " ".join((prompt or "").strip().split())
    label = clean if len(clean) <= 42 else f"{clean[:39]}..."
    queries = [clean, f"{clean} latest", f"{clean} news", f"{clean} analysis"]
    deduped = []
    seen = set()
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(q)
    return {"name": f"Focus: {label}", "emoji": "🎯", "queries": deduped}


async def get_research_topics(override_focus_prompt: str | None = None) -> list[dict]:
    if override_focus_prompt:
        return [_build_focus_topic(override_focus_prompt)]

    focus_prompt = await get_focus_topic_prompt()
    if focus_prompt:
        return [_build_focus_topic(focus_prompt), *DEFAULT_RESEARCH_TOPICS]
    return list(DEFAULT_RESEARCH_TOPICS)


def _fetch_rss_sync(query: str, max_items: int = 8) -> list[dict]:
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en"
    )
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:max_items]:
        items.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "source": (entry.get("source") or {}).get("title", ""),
            "published": entry.get("published", ""),
        })
    return items


async def fetch_topic_news(topic: dict, max_per_query: int = 6) -> list[dict]:
    """Fetch and deduplicate news for a topic across all its queries."""
    results_per_query = await asyncio.gather(
        *[asyncio.to_thread(_fetch_rss_sync, q, max_per_query) for q in topic["queries"]],
        return_exceptions=True,
    )

    seen = set()
    items = []
    for result in results_per_query:
        if isinstance(result, Exception):
            logger.warning(f"RSS fetch error: {result}")
            continue
        for item in result:
            key = item["title"][:60].lower()
            if key not in seen and item["title"]:
                seen.add(key)
                items.append(item)

    return items[:10]  # cap at 10 per topic


def _normalize_title(title: str) -> str:
    return " ".join((title or "").lower().split())


def _canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    path = parsed.path or ""

    # Google News RSS links often wrap real links in query params.
    q = parse_qs(parsed.query)
    wrapped = (q.get("url") or q.get("u") or [""])[0]
    if wrapped:
        return _canonical_url(unquote(wrapped))

    return f"{netloc}{path}".rstrip("/")


def research_item_signature(item: dict) -> str:
    canonical = _canonical_url(item.get("url", ""))
    if canonical:
        raw = canonical
    else:
        raw = f"{_normalize_title(item.get('title', ''))}|{(item.get('source') or '').lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _get_existing_signatures(signatures: list[str]) -> set[str]:
    if not signatures:
        return set()
    existing: set[str] = set()
    async with db_connect() as db:
        chunk_size = 500
        for i in range(0, len(signatures), chunk_size):
            chunk = signatures[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cur = await db.execute(
                f"SELECT signature FROM research_seen_items WHERE signature IN ({placeholders})",
                chunk,
            )
            rows = await cur.fetchall()
            existing.update(r[0] for r in rows)
    return existing


async def filter_new_research_items(topic_data: list[dict]) -> tuple[list[dict], dict]:
    all_sigs = []
    for td in topic_data:
        for item in td["items"]:
            all_sigs.append(research_item_signature(item))

    existing = await _get_existing_signatures(all_sigs)
    run_seen = set()
    filtered = []
    total_items = 0
    new_items = 0

    for td in topic_data:
        topic = td["topic"]
        keep = []
        for item in td["items"]:
            total_items += 1
            sig = research_item_signature(item)
            if sig in existing or sig in run_seen:
                continue
            run_seen.add(sig)
            copy_item = dict(item)
            copy_item["_signature"] = sig
            keep.append(copy_item)
            new_items += 1
        filtered.append({"topic": topic, "items": keep})

    stats = {"total_items": total_items, "new_items": new_items}
    return filtered, stats


async def mark_research_items_reported(topic_data: list[dict]) -> int:
    now = datetime.utcnow().isoformat()
    rows = []
    for td in topic_data:
        topic_name = td["topic"]["name"]
        for item in td["items"]:
            sig = item.get("_signature") or research_item_signature(item)
            rows.append(
                (
                    sig,
                    item.get("title", ""),
                    item.get("url", ""),
                    item.get("source", ""),
                    topic_name,
                    now,
                    now,
                )
            )

    if not rows:
        return 0

    async with db_connect() as db:
        await db.executemany(
            """
            INSERT INTO research_seen_items
            (signature, title, url, source, topic, first_seen_at, last_reported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signature) DO UPDATE SET
                last_reported_at=excluded.last_reported_at,
                topic=excluded.topic,
                report_count=research_seen_items.report_count + 1
            """,
            rows,
        )
        await db.commit()
    return len(rows)


async def fetch_all_research(topics: list[dict] | None = None) -> list[dict]:
    """Fetch news for all configured topics concurrently."""
    active_topics = topics or await get_research_topics()
    results = await asyncio.gather(
        *[fetch_topic_news(topic) for topic in active_topics],
        return_exceptions=True,
    )
    topic_data = []
    for topic, items in zip(active_topics, results):
        topic_data.append({
            "topic": topic,
            "items": items if not isinstance(items, Exception) else [],
        })
    return topic_data
