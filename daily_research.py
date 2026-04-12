"""
Daily research digest: fetch RSS headlines → synthesize with Claude → save to Notion → Telegram.

Supports a user-defined focus topic and suppresses already-reported headlines.
Designed to run at 5am ET daily, or on-demand via /research command.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from llm import complete_text

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

RESEARCH_PROMPT = """You are Jarvis preparing Matt's daily research digest.

Matt works in healthcare tech and AI. Be direct and specific.

For each topic section provided, give:
- 2-4 bullet points on the most significant NEW developments (only from provided items)
- 1 sentence on what matters most / what to watch

Cite specific companies, products, policies, or numbers where available.
No fluff. Plain text only — no markdown headers with #, no bold."""


def _build_research_context(topic_data: list[dict], date_label: str) -> str:
    lines = [f"Daily research digest for {date_label}. Include only genuinely new intel.\n"]
    for td in topic_data:
        topic = td["topic"]
        items = td["items"]
        lines.append(f"{topic['emoji']} {topic['name'].upper()}:")
        if items:
            for item in items:
                source = f" [{item['source']}]" if item.get("source") else ""
                lines.append(f"  - {item.get('title', '')}{source}")
        else:
            lines.append("  (no new items)")
        lines.append("")
    return "\n".join(lines)


def _build_no_new_digest(topic_data: list[dict], stats: dict) -> str:
    lines = [
        "No materially new research intel since the last digest.",
        f"Scanned {stats.get('total_items', 0)} candidate headlines across {len(topic_data)} topic streams.",
        "",
        "If you want a different lens, set a focus prompt with:",
        "/research_topic <topic you want tracked daily>",
    ]
    return "\n".join(lines)


async def generate_research_digest(
    override_focus_prompt: str | None = None,
) -> tuple[str, str, list[dict], dict]:
    """
    Fetch headlines for all topics, synthesize with Claude.
    Returns (digest_text, notion_title, new_topic_data, stats).
    """
    from research_services import (
        fetch_all_research,
        filter_new_research_items,
        get_research_topics,
    )

    now = datetime.now(ET)
    date_label = now.strftime("%A, %B %d, %Y")
    active_topics = await get_research_topics(override_focus_prompt=override_focus_prompt)
    topic_data = await fetch_all_research(topics=active_topics)
    new_topic_data, stats = await filter_new_research_items(topic_data)
    notion_title = f"Research Digest — {now.strftime('%b %d, %Y')}"

    if stats["new_items"] == 0:
        return _build_no_new_digest(new_topic_data, stats), notion_title, new_topic_data, stats

    context = _build_research_context(new_topic_data, date_label)

    digest_text = await complete_text(
        model="report",
        max_tokens=800,
        system=RESEARCH_PROMPT,
        user_content=context,
    )
    return digest_text.strip(), notion_title, new_topic_data, stats


async def save_research_to_notion(title: str, digest_text: str, topic_data: list[dict]):
    """Save digest + all headlines with clickable links to a Notion page."""
    from notion_services import save_digest_page

    def _save_sync():
        children = []

        # Claude's synthesis at the top
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": digest_text[:2000]}}]
            },
        })
        children.append({"object": "block", "type": "divider", "divider": {}})

        # Headlines with clickable links per topic
        for td in topic_data:
            topic = td["topic"]
            items = td["items"]

            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {
                        "content": f"{topic['emoji']} {topic['name']}"
                    }}]
                },
            })

            if items:
                for item in items:
                    rich_text = [{
                        "type": "text",
                        "text": {
                            "content": item["title"],
                            "link": {"url": item["url"]} if item["url"] else None,
                        },
                    }]
                    if item["source"]:
                        rich_text.append({
                            "type": "text",
                            "text": {"content": f"  — {item['source']}"},
                        })
                    children.append({
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": rich_text},
                    })
            else:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": "(no results)"}}]
                    },
                })

        return children[:100]

    try:
        children = await asyncio.to_thread(_save_sync)
        digest_date = datetime.now(ET).date().isoformat()
        await save_digest_page({
            "title": title,
            "digest_type": "Research Digest",
            "digest_date": digest_date,
            "children": children,
        })
        logger.info(f"Research saved to Notion: {title}")
    except Exception as e:
        logger.error(f"Failed to save research to Notion: {e}")


async def run_daily_research(
    override_focus_prompt: str | None = None,
    notify_when_no_new: bool = True,
) -> str:
    """
    Full pipeline: fetch RSS → dedupe to new-only → synthesize → save to Notion.
    Returns digest text for Telegram delivery.
    """
    from research_services import mark_research_items_reported

    digest_text, notion_title, topic_data, stats = await generate_research_digest(
        override_focus_prompt=override_focus_prompt
    )

    if stats["new_items"] > 0:
        await mark_research_items_reported(topic_data)
        await save_research_to_notion(notion_title, digest_text, topic_data)
    elif not notify_when_no_new:
        return ""

    return digest_text
