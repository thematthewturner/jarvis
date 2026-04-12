"""
Kids Digest — daily email scan for anything relevant to the kids.

Scans all iCloud inboxes for emails related to:
- Jade's cheer (PACK team)
- Wando Track
- Mrs. Miller
- Educational opportunities / school communications

Produces a structured digest, saves to Notion and SQLite for app display.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from llm import complete_json
from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

KIDS_CONTEXT = """
Family context:
- Daughter 1: Jade — cheerleader on the PACK cheer team
- Daughter 2: (other daughter) — runs Wando Track & Field / cross country
- Notable teacher: Mrs. Miller
- Both attend school in the Charleston, SC area

Watch for:
- Cheer: PACK team schedule, competitions, tryouts, camp, uniforms, fees, parent meetings
- Track: Wando track schedule, meets, practice changes, coach communications
- Academics: Mrs. Miller communications, report cards, grades, tutoring, academic opportunities
- School: field trips, permission slips, school events, deadlines, early dismissals, closures
- Opportunities: scholarships, enrichment programs, sports camps, academic competitions
- Urgent: anything requiring a response or action within 48 hours
"""

SCAN_PROMPT = f"""You are Jarvis, helping a busy parent stay on top of their kids' activities.

{KIDS_CONTEXT}

Below are emails from the last 48 hours across both parents' iCloud inboxes.
Your job: identify ONLY emails that are relevant to the kids per the context above.
Ignore newsletters, promotions, bills, personal adult matters, and spam.

For each relevant email, extract key information. Be concise and actionable.

Return a JSON object with this exact structure:
{{
  "found": <number of relevant emails found>,
  "summary": "<1-2 sentence plain-English overview of what's notable today>",
  "items": [
    {{
      "child": "Jade" | "Track" | "Both" | "General",
      "category": "cheer" | "track" | "school" | "teacher" | "opportunity" | "urgent",
      "priority": "high" | "medium" | "low",
      "from": "<sender name or email>",
      "subject": "<email subject>",
      "date": "<date>",
      "account": "<which parent's email>",
      "digest": "<2-3 sentence plain-English summary of what this email says and why it matters>",
      "action": "<specific action needed, or null if none>"
    }}
  ]
}}

If no relevant emails are found, return: {{"found": 0, "summary": "Nothing notable for the kids today.", "items": []}}
Return ONLY valid JSON. No markdown, no commentary.
"""

KIDS_SIGNAL_KEYWORDS = (
    "jade", "pack", "cheer", "wando", "track", "cross country", "coach",
    "teacher", "mrs. miller", "school", "classroom", "field trip", "permission slip",
    "report card", "grade", "tutoring", "uniform", "tryout", "practice", "meet",
    "camp", "parent meeting", "scholarship", "enrichment", "student", "attendance",
)

KIDS_ACTION_KEYWORDS = (
    "reply", "respond", "sign", "submit", "upload", "bring", "send", "rsvp",
    "due", "deadline", "tomorrow", "today", "required", "urgent", "reminder",
)


async def _init_db():
    import aiosqlite
    async with db_connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kids_digest (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                summary     TEXT NOT NULL,
                items_json  TEXT NOT NULL DEFAULT '[]',
                items_found INTEGER DEFAULT 0,
                notion_url  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def _save_to_db(date: str, summary: str, items: list, notion_url: str | None = None):
    import aiosqlite
    async with db_connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO kids_digest (date, summary, items_json, items_found, notion_url) VALUES (?,?,?,?,?)",
            (date, summary, json.dumps(items), len(items), notion_url)
        )
        await db.commit()


async def get_latest_digest() -> dict | None:
    """Return the most recent kids digest from DB."""
    import aiosqlite
    await _init_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM kids_digest ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    r = dict(row)
    r["items"] = json.loads(r.get("items_json", "[]"))
    return r


def _looks_like_kids_signal(email_item: dict) -> bool:
    fields = [
        email_item.get("from", ""),
        email_item.get("subject", ""),
        email_item.get("body_text", ""),
        " ".join(email_item.get("attachments") or []),
    ]
    text = " ".join(fields).lower()
    sender = (email_item.get("from", "") or "").lower()
    strong_hits = sum(1 for token in KIDS_SIGNAL_KEYWORDS if token in text or token in sender)
    action_hits = sum(1 for token in KIDS_ACTION_KEYWORDS if token in text)
    school_sender = any(token in sender for token in (".edu", "school", "teacher", "coach", "charlestoncountyschools"))
    return strong_hits >= 1 or (school_sender and action_hits >= 1)


def _select_candidate_emails(emails: list[dict], limit: int = 20) -> list[dict]:
    likely = [email for email in emails if _looks_like_kids_signal(email)]
    if likely:
        return likely[:limit]
    return emails[: min(12, limit)]


async def _save_to_notion(date_label: str, summary: str, items: list) -> str | None:
    """Save digest as a Notion page. Returns page URL or None."""
    try:
        from notion_services import save_digest_page

        if not os.getenv("NOTION_TOKEN", "").strip():
            return None

        # Build rich content blocks
        blocks = [
            {
                "object": "block", "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": summary}}],
                    "icon": {"type": "emoji", "emoji": "👧"},
                    "color": "blue_background"
                }
            }
        ]

        if items:
            priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            cat_emoji = {
                "cheer": "📣", "track": "🏃", "school": "🏫",
                "teacher": "📚", "opportunity": "⭐", "urgent": "🚨"
            }
            for item in items:
                pe = priority_emoji.get(item.get("priority", "low"), "⚪")
                ce = cat_emoji.get(item.get("category", "school"), "📧")
                header = f"{pe} {ce} [{item.get('child','')}] {item.get('subject','')}"
                body = item.get("digest", "")
                if item.get("action"):
                    body += f"\n\n→ Action: {item['action']}"

                blocks.append({
                    "object": "block", "type": "heading_3",
                    "heading_3": {"rich_text": [{"type": "text", "text": {"content": header}}]}
                })
                blocks.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": body[:2000]}}]
                    }
                })
                blocks.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {
                            "content": f"From: {item.get('from','')} · {item.get('date','')} · {item.get('account','')}",
                        }, "annotations": {"color": "gray"}}]
                    }
                })
        else:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": "No relevant items today."}}]}
            })

        title = f"Kids Digest — {date_label}"
        digest_date = datetime.now(ET).date().isoformat()
        result = await save_digest_page({
            "title": title,
            "digest_type": "Kids Digest",
            "digest_date": digest_date,
            "children": blocks[:100],
        })
        return result if result.startswith("http") else None
    except Exception as e:
        logger.error(f"Kids digest Notion save failed: {e}")
        return None


async def generate_kids_digest() -> dict:
    """
    Scan emails and build the kids digest payload without saving side effects.
    """
    now = datetime.now(ET)

    logger.info("Kids digest: fetching emails from all accounts...")

    # Fetch last 72h of emails across all parent accounts, even if already opened.
    from icloud_mail import fetch_recent_emails, prepare_email_for_claude
    emails = await fetch_recent_emails(since_hours=72, unread_only=False)

    if not emails:
        logger.info("Kids digest: no emails in last 72h")
        return {
            "date": now.strftime("%Y-%m-%d"),
            "summary": "No emails in the last 72 hours to scan.",
            "items": [],
            "items_found": 0,
        }

    candidate_emails = _select_candidate_emails(emails, limit=20)

    # Format only likely-relevant emails to keep token cost bounded.
    email_blocks = []
    for i, em in enumerate(candidate_emails):
        email_blocks.append(f"--- Email {i+1} ---\n{prepare_email_for_claude(em, max_body_length=500)}")
    email_text = "\n\n".join(email_blocks)

    logger.info(
        "Kids digest: analyzing %s likely-relevant emails (from %s fetched)",
        len(candidate_emails),
        len(emails),
    )

    try:
        result = await complete_json(
            model="extract",
            max_tokens=1400,
            user_content=f"{SCAN_PROMPT}\n\n=== EMAILS ===\n\n{email_text}",
            timeout_seconds=90,
        )
    except Exception as e:
        logger.error("Kids digest: Claude returned invalid JSON: %s", e)
        result = {"found": 0, "summary": "Digest generation failed (parse error).", "items": []}

    summary = result.get("summary", "")
    items   = result.get("items", [])

    logger.info(f"Kids digest: found {len(items)} relevant items")
    return {
        "date": now.strftime("%Y-%m-%d"),
        "summary": summary,
        "items": items,
        "items_found": len(items),
    }


async def run_kids_digest() -> str:
    """
    Main entry point: scan emails, analyze with Claude, save to Notion + DB.
    Returns a summary string suitable for Telegram.
    """
    await _init_db()

    now = datetime.now(ET)
    date_label = now.strftime("%A, %B %d, %Y")
    payload = await generate_kids_digest()
    summary = payload.get("summary", "")
    items = payload.get("items", [])

    # Save to Notion
    notion_url = await _save_to_notion(date_label, summary, items)

    # Save to DB
    await _save_to_db(payload.get("date", now.strftime("%Y-%m-%d")), summary, items, notion_url)

    # Format Telegram message
    if not items:
        return f"👧 Kids Digest — {date_label}\n\n{summary}"

    lines = [f"👧 Kids Digest — {date_label}", f"\n{summary}\n"]
    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for item in items:
        pe = priority_emoji.get(item.get("priority", "low"), "⚪")
        lines.append(f"{pe} [{item.get('child','')}] {item.get('subject','')}")
        lines.append(f"   {item.get('digest','')}")
        if item.get("action"):
            lines.append(f"   → {item['action']}")
        lines.append("")

    if notion_url:
        lines.append(f"📝 Full digest: {notion_url}")

    return "\n".join(lines)
