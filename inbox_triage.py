"""
Inbox Triage — smart morning inbox cleanup.

Scans all inboxes, classifies each email as:
  star    → flag it (action needed, worth keeping visible)
  archive → move to Archive (everything else)

Also supports Gmail historical triage via Gmail API (requires gmail.modify scope).

Runs at 7am daily (iCloud), or on-demand via /triage / /triage_gmail commands.
"""
import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from llm import complete_json, complete_json_sync
from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")
FINANCE_LABEL_NAME = "Jarvis/Finance"

TRIAGE_PROMPT = """You are Jarvis, helping a busy executive triage their email inbox.

Your job: classify each email below as ONE of two actions:

"star"    — Flag this email. It's from a real human (not automated/marketing),
            requires a response or action, or contains important personal information.
            Examples: direct messages from people, meeting requests, urgent matters,
            contracts, bills that need payment, time-sensitive opportunities.

"archive" — Move to archive. Automated, promotional, low-priority, or anything
            that does not clearly deserve a star.
            Examples: newsletters, marketing emails, order confirmations, shipping notices,
            social media notifications, automated alerts, promotional offers,
            system-generated reports nobody asked for.

IMPORTANT RULES:
- Be aggressive about archiving: if in doubt, archive it.
- Be conservative about starring: only star if there's a clear human sender and clear action.
- Never star newsletters, promotional emails, or automated system emails — even if they look important.
- Social media notifications (LinkedIn, Twitter, Facebook, Instagram) → always archive.
- Receipts, shipping confirmations → archive unless they show a problem.
- Bank/credit card transaction alerts → archive unless they clearly need review.
- "Unsubscribe" links in footer = it's a newsletter → archive.
- Marketing from brands → archive.

Return a JSON array. One object per email in the same order received:
[
  {"uid": "<uid from email>", "action": "star"|"archive", "reason": "<brief 1-sentence reason>"},
  ...
]

Return ONLY valid JSON. No markdown, no commentary."""

GMAIL_TRIAGE_PROMPT = """You are Jarvis, helping a busy executive triage Gmail.

For each email, return:
- action: "star" | "archive"
- finance: true | false
- reason: a brief one-sentence explanation

Action rules:
- "star" for clear human messages or things that need direct action.
- "archive" for newsletters, marketing, promo, social notifications, low-value automation,
  and anything that does not clearly deserve a star.
- Be aggressive about archiving: if unsure, archive.

Finance rules:
- Set finance=true if the email looks like a bill, statement, invoice, payment reminder, payment confirmation,
  tax document, insurance notice, utility notice, mortgage/loan notice, bank/credit-card account notice,
  or anything with an amount due, statement balance, due date, autopay status, or account document.
- Finance is independent of the action. A finance email can still be left in inbox or starred.
- Be slightly over-inclusive on finance=true when an email looks plausibly financial.

Return a JSON array. One object per email in the same order received:
[
  {"uid": "<uid from email>", "action": "star"|"archive", "finance": true|false, "reason": "<brief 1-sentence reason>"},
  ...
]

Return ONLY valid JSON. No markdown, no commentary."""


async def _init_db():
    import aiosqlite
    async with db_connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inbox_triage_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at      TEXT NOT NULL,
                account     TEXT NOT NULL,
                uid         TEXT NOT NULL,
                from_addr   TEXT,
                subject     TEXT,
                action      TEXT NOT NULL,
                reason      TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inbox_triage_runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at         TEXT NOT NULL,
                emails_scanned INTEGER DEFAULT 0,
                starred        INTEGER DEFAULT 0,
                archived       INTEGER DEFAULT 0,
                left_alone     INTEGER DEFAULT 0,
                summary        TEXT,
                created_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def _save_run(run_at: str, scanned: int, starred: int,
                    archived: int, left_alone: int, summary: str):
    import aiosqlite
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO inbox_triage_runs
               (run_at, emails_scanned, starred, archived, left_alone, summary)
               VALUES (?,?,?,?,?,?)""",
            (run_at, scanned, starred, archived, left_alone, summary)
        )
        await db.commit()


async def _save_actions(run_at: str, actions: list[dict]):
    import aiosqlite
    async with db_connect(DB_PATH) as db:
        for a in actions:
            await db.execute(
                """INSERT INTO inbox_triage_log
                   (run_at, account, uid, from_addr, subject, action, reason)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_at, a.get("account", ""), a.get("uid", ""),
                 a.get("from_addr", ""), a.get("subject", ""),
                 a.get("action", "leave"), a.get("reason", ""))
            )
        await db.commit()


async def get_latest_triage() -> dict | None:
    """Return the most recent triage run + its actions from DB."""
    import aiosqlite
    await _init_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM inbox_triage_runs ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            run = await cur.fetchone()
        if not run:
            return None
        run_dict = dict(run)
        async with db.execute(
            "SELECT * FROM inbox_triage_log WHERE run_at = ? ORDER BY action, created_at",
            (run_dict["run_at"],)
        ) as cur:
            actions = [dict(r) for r in await cur.fetchall()]
    run_dict["actions"] = actions
    return run_dict


async def run_triage(hours: int = 48) -> str:
    """
    Main entry: fetch inbox emails, classify with Claude, execute actions.
    Returns a summary string suitable for Telegram.
    """
    await _init_db()

    now = datetime.now(ET)
    run_at = now.isoformat()

    logger.info("Inbox triage: fetching emails from all accounts...")

    from icloud_mail import fetch_unread_emails, archive_email, flag_email
    emails = await fetch_unread_emails(since_hours=hours)

    if not emails:
        summary = "Inbox is clean — no recent emails to triage."
        await _save_run(run_at, 0, 0, 0, 0, summary)
        return f"📬 Inbox Triage\n\n{summary}"

    # Cap at 60 emails per run
    emails = emails[:60]

    # Format minimal email headers for Claude (no body needed for triage)
    email_blocks = []
    for em in emails:
        email_blocks.append(
            f"UID: {em.get('uid', '?')}\n"
            f"From: {em.get('from', '?')}\n"
            f"Subject: {em.get('subject', '(no subject)')}\n"
            f"Date: {em.get('date', '?')}"
        )
    emails_text = "\n---\n".join(email_blocks)

    logger.info(f"Inbox triage: classifying {len(emails)} emails with Claude...")
    try:
        decisions = await complete_json(
            model="extract",
            max_tokens=2500,
            user_content=f"{TRIAGE_PROMPT}\n\n=== EMAILS ===\n\n{emails_text}",
            timeout_seconds=90,
        )
    except Exception as e:
        logger.error("Inbox triage: Claude returned invalid JSON: %s", e)
        return "Triage failed: could not parse Claude's response."

    # Build UID → email lookup
    uid_map = {em["uid"]: em for em in emails}

    starred_items  = []
    archived_items = []
    action_records = []

    for decision in decisions:
        uid    = decision.get("uid", "")
        raw_action = str(decision.get("action", "archive")).strip().lower()
        action = "star" if raw_action == "star" else "archive"
        reason = decision.get("reason", "")
        email  = uid_map.get(uid, {})

        action_records.append({
            "uid":      uid,
            "account":  email.get("account", ""),
            "from_addr": email.get("from", ""),
            "subject":  email.get("subject", "(no subject)"),
            "action":   action,
            "reason":   reason,
        })

        subject = email.get("subject", "(no subject)")

        if action == "star":
            try:
                await flag_email(uid, flagged=True)
                starred_items.append(subject)
            except Exception as e:
                logger.error(f"Triage: failed to star {uid}: {e}")
        else:
            try:
                await archive_email(uid)
                archived_items.append(subject)
            except Exception as e:
                logger.error(f"Triage: failed to archive {uid}: {e}")

    await _save_actions(run_at, action_records)

    summary = (
        f"Scanned {len(emails)} emails: "
        f"{len(starred_items)} starred, {len(archived_items)} archived."
    )
    await _save_run(run_at, len(emails), len(starred_items),
                    len(archived_items), 0, summary)

    logger.info(f"Inbox triage complete: {summary}")

    # Build Telegram message
    lines = [f"📬 Inbox Triage — {now.strftime('%a %b %d')}", f"\n{summary}"]

    if starred_items:
        lines.append(f"\n⭐ Starred ({len(starred_items)} action items):")
        for s in starred_items[:6]:
            lines.append(f"  • {s[:65]}")
        if len(starred_items) > 6:
            lines.append(f"  … and {len(starred_items) - 6} more")

    if archived_items:
        lines.append(f"\n🗂 Archived {len(archived_items)} emails")

    return "\n".join(lines)


# ── Gmail Historical Triage ───────────────────────────────────────────────────

def _gmail_fetch_inbox_sync(query: str, max_emails: int) -> list[dict]:
    """Fetch Gmail inbox messages (metadata only) up to max_emails."""
    from google_services import _build, _execute_with_retry
    service = _build("gmail", "v1")

    emails = []
    page_token = None

    while len(emails) < max_emails:
        batch_size = min(100, max_emails - len(emails))
        kwargs = {"userId": "me", "q": query, "maxResults": batch_size}
        if page_token:
            kwargs["pageToken"] = page_token

        result = _execute_with_retry(
            service.users().messages().list(**kwargs),
            "gmail.triage.list",
        )
        messages = result.get("messages", [])
        if not messages:
            break

        # Fetch headers for each message in this page
        for msg in messages:
            try:
                detail = _execute_with_retry(
                    service.users().messages().get(
                        userId="me",
                        id=msg["id"],
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    ),
                    "gmail.triage.get",
                )
                headers = {h["name"]: h["value"]
                           for h in detail["payload"].get("headers", [])}
                emails.append({
                    "id":      msg["id"],
                    "from":    headers.get("From", "?"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date":    headers.get("Date", ""),
                    "snippet": detail.get("snippet", "")[:220],
                })
            except Exception as e:
                logger.warning(f"Gmail fetch detail failed for {msg['id']}: {e}")

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return emails


def _ensure_gmail_label_sync(service, label_name: str) -> str:
    from google_services import _execute_with_retry

    labels = _execute_with_retry(
        service.users().labels().list(userId="me"),
        "gmail.labels.list",
    ).get("labels", [])
    for label in labels:
        if label.get("name") == label_name:
            return label["id"]

    created = _execute_with_retry(
        service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ),
        "gmail.labels.create",
    )
    return created["id"]


def _gmail_apply_actions_batch_sync(
    star_ids: list[str],
    archive_ids: list[str],
    finance_ids: list[str],
) -> tuple[int, int, int]:
    """Apply star/archive/finance labels in batch, reusing one Gmail service."""
    from google_services import _build, _execute_with_retry
    service = _build("gmail", "v1")

    def _chunks(items: list[str], size: int = 1000):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    applied_star = 0
    applied_archive = 0
    applied_finance = 0
    finance_label_id = _ensure_gmail_label_sync(service, FINANCE_LABEL_NAME) if finance_ids else ""

    for chunk in _chunks(star_ids):
        _execute_with_retry(
            service.users().messages().batchModify(
                userId="me",
                body={"ids": chunk, "addLabelIds": ["STARRED"]},
            ),
            "gmail.triage.batch_star",
        )
        applied_star += len(chunk)

    for chunk in _chunks(archive_ids):
        _execute_with_retry(
            service.users().messages().batchModify(
                userId="me",
                body={"ids": chunk, "removeLabelIds": ["INBOX"]},
            ),
            "gmail.triage.batch_archive",
        )
        applied_archive += len(chunk)

    if finance_label_id:
        for chunk in _chunks(finance_ids):
            _execute_with_retry(
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": chunk, "addLabelIds": [finance_label_id]},
                ),
                "gmail.triage.batch_finance",
            )
            applied_finance += len(chunk)

    return applied_star, applied_archive, applied_finance


def _classify_batch_sync(email_batch: list[dict]) -> list[dict]:
    """Send one batch to Claude and return decisions."""
    email_blocks = []
    for em in email_batch:
        email_blocks.append(
            f"UID: {em['id']}\n"
            f"From: {em['from']}\n"
            f"Subject: {em['subject']}\n"
            f"Date: {em['date']}\n"
            f"Snippet: {em.get('snippet', '')}"
        )
    emails_text = "\n---\n".join(email_blocks)

    return complete_json_sync(
        model="extract",
        max_tokens=2800,
        user_content=f"{GMAIL_TRIAGE_PROMPT}\n\n=== EMAILS ===\n\n{emails_text}",
    )


async def run_gmail_triage(
    max_emails: int = 300,
    query: str = "in:inbox",
    progress_callback=None,
) -> str:
    """
    Historical Gmail inbox triage. Fetches up to max_emails messages,
    classifies with Claude in batches of 50, applies star/archive actions.

    progress_callback: optional async fn(text: str) called after each batch.
    Requires gmail.modify scope — re-run scripts/authorize_google.py if missing.
    """
    await _init_db()
    now = datetime.now(ET)
    run_at = now.isoformat()

    logger.info(f"Gmail triage: fetching up to {max_emails} inbox messages...")

    # Fetch all inbox messages (sync, blocking)
    emails = await asyncio.to_thread(_gmail_fetch_inbox_sync, query, max_emails)

    if not emails:
        return "📬 Gmail Triage\n\nNo messages found in inbox."

    total = len(emails)
    logger.info(f"Gmail triage: fetched {total} messages, classifying in batches...")

    if progress_callback:
        await progress_callback(f"📬 Gmail triage started — {total} messages to classify…")

    # Process in batches of 50
    BATCH = 50
    starred_items  = []
    archived_items = []
    finance_items  = []
    action_records = []

    for batch_start in range(0, total, BATCH):
        batch = emails[batch_start: batch_start + BATCH]
        batch_num = batch_start // BATCH + 1
        total_batches = (total + BATCH - 1) // BATCH

        logger.info(f"Gmail triage: batch {batch_num}/{total_batches} ({len(batch)} emails)")

        try:
            decisions = await asyncio.to_thread(_classify_batch_sync, batch)
        except Exception as e:
            logger.error(f"Gmail triage batch {batch_num} failed: {e}")
            if progress_callback:
                await progress_callback(f"⚠️ Batch {batch_num}/{total_batches} failed, skipping: {e}")
            continue

        id_map = {em["id"]: em for em in batch}

        b_starred = b_archived = b_finance = 0
        to_star: list[tuple[str, str]] = []
        to_archive: list[tuple[str, str]] = []
        to_finance: list[tuple[str, str]] = []
        for decision in decisions:
            msg_id = decision.get("uid", "")
            raw_action = str(decision.get("action", "archive")).strip().lower()
            action = "star" if raw_action == "star" else "archive"
            finance = bool(decision.get("finance", False))
            reason = decision.get("reason", "")
            email  = id_map.get(msg_id, {})
            if not email:
                continue
            subject = email.get("subject", "(no subject)")

            action_records.append({
                "uid":       msg_id,
                "account":   "gmail",
                "from_addr": email.get("from", ""),
                "subject":   subject,
                "action":    action,
                "reason":    f"{reason} [finance]" if finance else reason,
            })

            if action == "star":
                to_star.append((msg_id, subject))
            else:
                to_archive.append((msg_id, subject))

            if finance:
                to_finance.append((msg_id, subject))

        def _dedupe(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
            seen: set[str] = set()
            unique = []
            for mid, sub in items:
                if mid in seen:
                    continue
                seen.add(mid)
                unique.append((mid, sub))
            return unique

        to_star = _dedupe(to_star)
        to_archive = _dedupe(to_archive)
        to_finance = _dedupe(to_finance)

        if to_star or to_archive or to_finance:
            try:
                _, _, applied_finance = await asyncio.to_thread(
                    _gmail_apply_actions_batch_sync,
                    [mid for mid, _ in to_star],
                    [mid for mid, _ in to_archive],
                    [mid for mid, _ in to_finance],
                )
                starred_items.extend(sub for _, sub in to_star)
                archived_items.extend(sub for _, sub in to_archive)
                finance_items.extend(sub for _, sub in to_finance)
                b_starred += len(to_star)
                b_archived += len(to_archive)
                b_finance += applied_finance
            except Exception as e:
                logger.error(f"Gmail triage: failed to apply batch actions: {e}")
                if progress_callback:
                    await progress_callback(
                        f"⚠️ Batch {batch_num}/{total_batches} actions failed: {e}"
                    )

        if progress_callback:
            done = min(batch_start + BATCH, total)
            await progress_callback(
                f"✅ Batch {batch_num}/{total_batches} done ({done}/{total}) "
                f"— ⭐{b_starred} 🗂{b_archived} 💵{b_finance}"
            )

    # Save to DB
    await _save_actions(run_at, action_records)
    summary = (
        f"Gmail: scanned {total} emails — "
        f"{len(starred_items)} starred, {len(archived_items)} archived, "
        f"{len(finance_items)} finance-labeled."
    )
    await _save_run(run_at, total, len(starred_items),
                    len(archived_items), 0, summary)

    logger.info(f"Gmail triage complete: {summary}")

    lines = [f"📬 Gmail Triage Complete — {now.strftime('%a %b %d')}", f"\n{summary}"]

    if starred_items:
        lines.append(f"\n⭐ Starred ({len(starred_items)} action items):")
        for s in starred_items[:8]:
            lines.append(f"  • {s[:65]}")
        if len(starred_items) > 8:
            lines.append(f"  … and {len(starred_items) - 8} more")

    if archived_items:
        lines.append(f"\n🗂 Archived {len(archived_items)} emails")

    if finance_items:
        lines.append(f"\n💵 Finance-labeled ({len(finance_items)}):")
        for s in finance_items[:8]:
            lines.append(f"  • {s[:65]}")
        if len(finance_items) > 8:
            lines.append(f"  … and {len(finance_items) - 8} more")

    return "\n".join(lines)
