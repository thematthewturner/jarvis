"""
iCloud Mail — IMAP (read) + SMTP (send) integration for Jarvis.
Supports multiple iCloud accounts via ICLOUD_EMAIL / ICLOUD_EMAIL_2 / etc.
"""
import asyncio
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

logger = logging.getLogger("jarvis")

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587

MAX_RETRIES = 3


def _get_all_accounts() -> list[tuple[str, str]]:
    """Return list of (email, password) for all configured iCloud accounts."""
    import os
    accounts = []
    # Primary account
    email = os.getenv("ICLOUD_EMAIL", "")
    pw    = os.getenv("ICLOUD_APP_PASSWORD", "").replace("-", "")
    if email and pw:
        accounts.append((email, pw))
    # Additional accounts: ICLOUD_EMAIL_2, ICLOUD_EMAIL_3, …
    for i in range(2, 10):
        email = os.getenv(f"ICLOUD_EMAIL_{i}", "")
        pw    = os.getenv(f"ICLOUD_APP_PASSWORD_{i}", "").replace("-", "")
        if email and pw:
            accounts.append((email, pw))
        else:
            break
    if not accounts:
        raise RuntimeError("No iCloud accounts configured in .env")
    return accounts


def _get_credentials() -> tuple[str, str]:
    """Return primary account credentials (used for sending)."""
    return _get_all_accounts()[0]


def _email_to_dict(msg, account: str = "") -> dict:
    """Convert an imap_tools MailMessage to a clean dict."""
    return {
        "uid":         f"{account}:{msg.uid}" if account else msg.uid,
        "account":     account,
        "from":        msg.from_,
        "to":          list(msg.to),
        "subject":     msg.subject or "(no subject)",
        "date":        msg.date.isoformat() if msg.date else None,
        "body_text":   (msg.text or "").strip(),
        "body_html":   (msg.html or "").strip(),
        "attachments": [a.filename for a in msg.attachments],
        "seen":        "\\Seen" in (msg.flags or []),
    }


def _fetch_sync_account(criteria, email: str, password: str,
                        folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """Fetch emails for a single account."""
    from imap_tools import MailBox
    results = []
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with MailBox(IMAP_HOST, IMAP_PORT).login(email, password, initial_folder=folder) as mb:
                for msg in mb.fetch(criteria, mark_seen=False, bulk=True):
                    results.append(_email_to_dict(msg, account=email))
                    if len(results) >= limit:
                        break
            return sorted(results, key=lambda m: m["date"] or "", reverse=True)
        except Exception as e:
            last_err = e
            if "auth" in str(e).lower() or "login" in str(e).lower():
                logger.error(f"iCloud IMAP auth failed for {email} — regenerate app-specific password")
                break
            import time
            wait = 2 ** attempt
            logger.warning(f"IMAP attempt {attempt + 1} for {email} failed ({e}), retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(
        f"iCloud IMAP fetch failed for {email} after {MAX_RETRIES} attempts: {last_err}"
    )


def _fetch_sync(criteria, folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """Fetch from all accounts, merged and sorted."""
    accounts = _get_all_accounts()
    results = []
    seen_uids: set[str] = set()
    errors: list[str] = []
    for email, pw in accounts:
        try:
            account_msgs = _fetch_sync_account(criteria, email, pw, folder=folder, limit=limit)
        except Exception as e:
            errors.append(str(e))
            continue
        for msg in account_msgs:
            if msg["uid"] not in seen_uids:
                seen_uids.add(msg["uid"])
                results.append(msg)
    if errors:
        raise RuntimeError("; ".join(errors))
    return sorted(results, key=lambda m: m["date"] or "", reverse=True)


async def fetch_unread_emails(since_hours: int = 24) -> list[dict]:
    """Fetch unread emails from all iCloud inboxes in the last N hours."""
    return await fetch_recent_emails(since_hours=since_hours, unread_only=True)


async def fetch_recent_emails(since_hours: int = 24, unread_only: bool = False) -> list[dict]:
    """Fetch recent emails from all iCloud inboxes, optionally unread-only."""
    from imap_tools import A
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).date()
    criteria = A(seen=False, date_gte=since) if unread_only else A(date_gte=since)
    return await asyncio.to_thread(_fetch_sync, criteria)


async def fetch_emails_by_sender(sender_patterns: list[str], since_days: int = 7) -> list[dict]:
    """Fetch emails from specific senders across all accounts."""
    from imap_tools import A
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    results = []
    seen_uids: set[str] = set()
    for pattern in sender_patterns:
        criteria = A(from_=pattern, date_gte=since)
        for m in await asyncio.to_thread(_fetch_sync, criteria, "INBOX", 10):
            if m["uid"] not in seen_uids:
                seen_uids.add(m["uid"])
                results.append(m)
    return sorted(results, key=lambda m: m["date"] or "", reverse=True)


async def fetch_emails_by_subject(subject_keywords: list[str], since_days: int = 7) -> list[dict]:
    """Fetch emails by subject keywords across all accounts."""
    from imap_tools import A
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    results = []
    seen_uids: set[str] = set()
    for kw in subject_keywords:
        criteria = A(subject=kw, date_gte=since)
        for m in await asyncio.to_thread(_fetch_sync, criteria, "INBOX", 10):
            if m["uid"] not in seen_uids:
                seen_uids.add(m["uid"])
                results.append(m)
    return sorted(results, key=lambda m: m["date"] or "", reverse=True)


async def search_emails(query: str, since_days: int = 30, folder: str = "INBOX") -> list[dict]:
    """Search across all accounts: matches query in sender, subject, and body."""
    from imap_tools import A
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    results = []
    seen_uids: set[str] = set()
    for criteria in [
        A(from_=query,    date_gte=since),
        A(subject=query,  date_gte=since),
        A(text=query,     date_gte=since),
    ]:
        for m in await asyncio.to_thread(_fetch_sync, criteria, folder=folder, limit=10):
            if m["uid"] not in seen_uids:
                seen_uids.add(m["uid"])
                results.append(m)
    return sorted(results, key=lambda m: m["date"] or "", reverse=True)[:20]


# ── Email actions (archive / move / trash / mark-read) ────────────────────────

# Known iCloud folder aliases → canonical names
FOLDER_ALIASES: dict[str, str] = {
    "archive":          "Archive",
    "trash":            "Deleted Messages",
    "deleted":          "Deleted Messages",
    "junk":             "Junk",
    "spam":             "Junk",
    "action":           "Action",
    "hold":             "Action/Hold",
    "follow up":        "Follow Up",
    "followup":         "Follow Up",
    "inbox":            "INBOX",
    "sent":             "Sent Messages",
}


def _parse_composite_uid(uid_str: str) -> tuple[str, str]:
    """Split 'account@email.com:12345' → ('account@email.com', '12345')."""
    if ":" not in uid_str:
        raise ValueError(f"UID '{uid_str}' has no account prefix — expected 'account:uid'")
    account, raw_uid = uid_str.rsplit(":", 1)
    return account.strip(), raw_uid.strip()


def _action_sync(account: str, password: str, uid: str,
                 action: str, folder: str | None = None) -> str:
    """Perform a single IMAP action on one email. Returns status string."""
    from imap_tools import MailBox, MailMessageFlags
    try:
        with MailBox(IMAP_HOST, IMAP_PORT).login(account, password, initial_folder="INBOX") as mb:
            uids = [uid]
            if action == "move":
                mb.move(uids, folder)
                return f"Moved to {folder}"
            elif action == "copy":
                mb.copy(uids, folder)
                return f"Copied to {folder}"
            elif action == "delete":
                mb.delete(uids)
                return "Deleted"
            elif action == "mark_seen":
                mb.flag(uids, [MailMessageFlags.SEEN], True)
                return "Marked as read"
            elif action == "mark_unseen":
                mb.flag(uids, [MailMessageFlags.SEEN], False)
                return "Marked as unread"
            elif action == "flag":
                mb.flag(uids, [MailMessageFlags.FLAGGED], True)
                return "Starred"
            elif action == "unflag":
                mb.flag(uids, [MailMessageFlags.FLAGGED], False)
                return "Unstarred"
            else:
                return f"Unknown action: {action}"
    except Exception as e:
        logger.error("iCloud email action %s failed for %s:%s — %s", action, account, uid, e)
        raise RuntimeError(f"iCloud action '{action}' failed: {e}") from e


async def _email_action(uid_str: str, action: str, folder: str | None = None) -> str:
    """Route an action to the correct account using the composite UID."""
    accounts = {email: pw for email, pw in _get_all_accounts()}
    account, raw_uid = _parse_composite_uid(uid_str)
    if account not in accounts:
        raise ValueError(f"Unknown account '{account}'")
    pw = accounts[account]
    return await asyncio.to_thread(_action_sync, account, pw, raw_uid, action, folder)


async def archive_email(uid_str: str) -> str:
    """Move an email to the Archive folder."""
    return await _email_action(uid_str, "move", "Archive")


async def flag_email(uid_str: str, flagged: bool = True) -> str:
    """Star (flag) or un-star an email using the IMAP \\Flagged flag."""
    return await _email_action(uid_str, "flag" if flagged else "unflag")


async def trash_email(uid_str: str) -> str:
    """Move an email to Deleted Messages (Trash)."""
    return await _email_action(uid_str, "move", "Deleted Messages")


async def move_email(uid_str: str, folder: str) -> str:
    """Move an email to any folder. Resolves common aliases."""
    canonical = FOLDER_ALIASES.get(folder.lower().strip(), folder)
    return await _email_action(uid_str, "move", canonical)


async def mark_email_read(uid_str: str, seen: bool = True) -> str:
    """Mark an email as read or unread."""
    return await _email_action(uid_str, "mark_seen" if seen else "mark_unseen")


def _send_sync(to: str, subject: str, body: str, html: bool = False) -> bool:
    """Send from the primary iCloud account."""
    email, password = _get_credentials()
    msg = MIMEMultipart("alternative")
    msg["From"]    = email
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(email, password)
            server.sendmail(email, [to], msg.as_string())
        logger.info(f"iCloud email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"iCloud SMTP send failed: {e}")
        return False


async def send_email(to: str, subject: str, body: str, html: bool = False) -> bool:
    """Send an email from the primary iCloud account."""
    return await asyncio.to_thread(_send_sync, to, subject, body, html)


async def get_mailbox_folders() -> list[str]:
    """List folders for the primary account."""
    from imap_tools import MailBox
    email, password = _get_credentials()
    def _sync():
        with MailBox(IMAP_HOST, IMAP_PORT).login(email, password) as mb:
            return [f.name for f in mb.folder.list()]
    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.error(f"iCloud folder list failed: {e}")
        return []


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, d):
        self._parts.append(d)

    def get_text(self) -> str:
        return " ".join(self._parts)


def prepare_email_for_claude(email_dict: dict, max_body_length: int = 3000) -> str:
    """Format an email dict into a clean string for Claude analysis."""
    body = email_dict.get("body_text") or ""
    if not body and email_dict.get("body_html"):
        stripper = _HTMLStripper()
        stripper.feed(email_dict["body_html"])
        body = stripper.get_text()

    body = re.sub(r"\s{3,}", "\n\n", body).strip()
    if len(body) > max_body_length:
        body = body[:max_body_length] + "\n... [truncated]"

    lines = [
        f"UID: {email_dict.get('uid', '?')}",
        f"From: {email_dict.get('from', '?')}",
        f"To account: {email_dict.get('account', '')}",
        f"Subject: {email_dict.get('subject', '?')}",
        f"Date: {email_dict.get('date', '?')}",
        "",
        body,
    ]
    if email_dict.get("attachments"):
        lines.append(f"\nAttachments: {', '.join(email_dict['attachments'])}")

    return "\n".join(lines)
