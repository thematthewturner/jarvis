"""
Morning Brief — the unified morning runner.

Executes the daily digest plus inbox triage, gmail triage, action nudges,
pool nudge, and home maintenance in parallel, then stitches the results into
a single Telegram message.

- Sections honor the `morning_brief_enabled_sections` preference.
- Sections that return empty strings are omitted from the output.
- Exceptions in individual sections are captured per-section, not fatal —
  a failing Gmail triage doesn't kill the whole brief.
- The caller is responsible for sending `parts` to Telegram (or for splitting
  the single-shot `text` another way). Telegram caps messages at ~4096 chars;
  we split at 4000 on paragraph boundaries so each chunk lands cleanly.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from jarvis_ops import get_effective_morning_sections, get_preferences

logger = logging.getLogger("jarvis.morning_brief")
ET = ZoneInfo("America/New_York")

SECTION_LABELS = {
    "digest": "🗞️ Daily Digest",
    "inbox_triage": "📬 iCloud Triage",
    "gmail_triage": "✉️ Gmail Triage",
    "action_nudges": "🎯 Action Nudges",
    "home_maintenance": "🏠 Home Ops",
    "pool_nudge": "🏊 Pool Nudge",
}

ALL_SECTIONS = list(SECTION_LABELS.keys())


async def _run_digest_section() -> str:
    from daily_digest import run_daily_digest
    payload = await run_daily_digest()
    return (payload.get("notification_text") or "").strip()


async def _run_inbox_triage_section() -> str:
    from inbox_triage import run_triage
    return await run_triage()


async def _run_gmail_triage_section() -> str:
    from inbox_triage import run_gmail_triage
    return await run_gmail_triage(
        max_emails=50,
        query="in:inbox newer_than:2d",
    )


async def _run_action_nudges_section() -> str:
    from nudges import run_daily_email_action_nudges
    return await run_daily_email_action_nudges(notify_empty=False)


async def _run_pool_nudge_section() -> str:
    from nudges import run_weekly_pool_nudge
    return await run_weekly_pool_nudge(force=False, notify_empty=False)


async def _run_home_maintenance_section() -> str:
    from home_ops import run_home_maintenance_check
    return await run_home_maintenance_check(days_ahead=7, notify_empty=False)


SECTION_RUNNERS = {
    "digest": _run_digest_section,
    "inbox_triage": _run_inbox_triage_section,
    "gmail_triage": _run_gmail_triage_section,
    "action_nudges": _run_action_nudges_section,
    "home_maintenance": _run_home_maintenance_section,
    "pool_nudge": _run_pool_nudge_section,
}


async def run_morning_brief(
    *,
    force_sections: list[str] | None = None,
) -> dict:
    """
    Run all enabled morning sections in parallel and stitch the results
    into a single message.

    Returns dict with:
        - text: the full stitched message
        - parts: list[str] of Telegram-sized chunks (len <= 4000 chars)
        - header: the header line used at the top of the brief
        - sections: {name: {"status": "ok"|"empty"|"error", "text": str, "error": str|None}}
        - generated_at: ISO timestamp (ET)
    """
    prefs = await get_preferences()
    if force_sections is not None:
        sections = [s for s in force_sections if s in SECTION_RUNNERS]
    else:
        sections = [s for s in get_effective_morning_sections(prefs) if s in SECTION_RUNNERS]

    now = datetime.now(ET)
    header = f"🌅 Morning Brief — {now.strftime('%A, %b %d')}"

    if not sections:
        body = f"{header}\n\nNo sections enabled."
        return {
            "text": body,
            "parts": [body],
            "header": header,
            "sections": {},
            "generated_at": now.isoformat(),
        }

    logger.info("Morning brief running sections: %s", sections)
    runners = [SECTION_RUNNERS[name]() for name in sections]
    results = await asyncio.gather(*runners, return_exceptions=True)

    section_details: dict = {}
    body_parts: list[str] = []

    for name, result in zip(sections, results):
        label = SECTION_LABELS.get(name, name)
        if isinstance(result, Exception):
            err = f"{type(result).__name__}: {result}"
            logger.warning("Morning brief section %s errored: %s", name, err)
            section_details[name] = {"status": "error", "text": "", "error": err}
            body_parts.append(f"{label}\n⚠️ {err}")
            continue
        text = (result or "").strip()
        if not text:
            section_details[name] = {"status": "empty", "text": "", "error": None}
            continue
        section_details[name] = {"status": "ok", "text": text, "error": None}
        body_parts.append(f"{label}\n{text}")

    if not body_parts:
        stitched = f"{header}\n\nAll sections ran empty."
    else:
        stitched = header + "\n\n" + "\n\n—————————\n\n".join(body_parts)

    parts = _split_for_telegram(stitched, limit=4000)

    return {
        "text": stitched,
        "parts": parts,
        "header": header,
        "sections": section_details,
        "generated_at": now.isoformat(),
    }


def _split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """Split a long message on paragraph boundaries so Telegram accepts each chunk."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = para if not buf else f"{buf}\n\n{para}"
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
        if len(para) <= limit:
            buf = para
        else:
            # Paragraph by itself is too long — hard-split it.
            for i in range(0, len(para), limit):
                parts.append(para[i:i + limit])
            buf = ""
    if buf:
        parts.append(buf)
    return parts
