#!/usr/bin/env python3
"""Jarvis daily digest pipeline — enhanced with weather + Todoist.

Collects read-only source updates, dedupes against persistent state, asks the
configured LLM for a concise digest, writes the full digest to Notion, and emits
a short Telegram-ready message containing the Notion link.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NOTION_VERSION = "2025-09-03"
ANTHROPIC_VERSION = "2023-06-01"

STATE_PATH = Path(os.getenv("JARVIS_DAILY_DIGEST_STATE", "/opt/data/jarvis/daily_digest_state.json"))
FEEDBACK_PATH = Path(os.getenv("JARVIS_DAILY_DIGEST_FEEDBACK", "/opt/data/jarvis/daily_digest_feedback.md"))
BLOGWATCHER_BIN = Path(os.getenv("BLOGWATCHER_BIN", "/opt/data/bin/blogwatcher-cli"))
BLOGWATCHER_DB = Path(os.getenv("BLOGWATCHER_DB", "/opt/data/blogwatcher/blogwatcher-cli.db"))
BASE_DIR = Path(__file__).resolve().parent
WHOOP_PYTHON_BIN = os.getenv("WHOOP_PYTHON_BIN") or sys.executable


def configured_path(env_name: str, defaults: list[Path]) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured)
    for path in defaults:
        if path.exists():
            return path
    return defaults[0]


def configured_cache_path(env_name: str, defaults: list[Path]) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured)
    for path in defaults:
        if path.exists() or path.parent.exists():
            return path
    return defaults[0]


WHOOP_MONITOR_BIN = configured_path(
    "WHOOP_MONITOR_BIN",
    [
        Path("/opt/data/jarvis/whoop_monitor.py"),
        BASE_DIR / "scripts" / "whoop_monitor.py",
        BASE_DIR / "whoop_monitor.py",
    ],
)
WHOOP_TOKEN_FILE = configured_path(
    "WHOOP_TOKEN_FILE",
    [
        Path("/opt/data/secrets/whoop_token.json"),
        Path("/Users/toniturner/Library/Mobile Documents/com~apple~CloudDocs/projects/jarvis/hermes-jarvis-parity-pack/secrets.local/whoop_tokens.json"),
        Path("/Users/toniturner/Library/Mobile Documents/com~apple~CloudDocs/projects/jarvis/data/whoop_tokens.json"),
        Path("/private/tmp/whoop_token.json"),
    ],
)
WHOOP_CACHE_FILE = configured_cache_path(
    "WHOOP_CACHE_FILE",
    [
        Path("/opt/data/jarvis/whoop_cache.json"),
        Path("/private/tmp/whoop_cache.json"),
        BASE_DIR / "whoop_cache.json",
    ],
)
INBOX_CLEANER_BIN = configured_path(
    "INBOX_CLEANER_BIN",
    [
        Path("/opt/data/jarvis/inbox_cleaner.py"),
        BASE_DIR / "scripts" / "inbox_cleaner.py",
        BASE_DIR / "inbox_cleaner.py",
    ],
)

HIGH_SIGNAL_TERMS = re.compile(
    r"\b("
    r"appointment|calendar|deadline|due|urgent|action required|confirm|confirmation|"
    r"reservation|itinerary|flight|hotel|marriott|travel|school|teacher|family|"
    r"bill|invoice|receipt|refund|payment|bank|tax|insurance|doctor|dentist|"
    r"health|hrv|sleep|zone 2|ai|openai|anthropic|healthcare|charleston|mt pleasant|"
    r"assignment|classroom|conference|grade|grades|homework|field trip|permission|"
    r"tryout|practice|coach|principal|counselor|charleston\.k12|charleston county schools"
    r")\b",
    re.IGNORECASE,
)
LOW_VALUE_TERMS = re.compile(
    r"\b("
    r"sale|promo|promotion|coupon|deal|newsletter|sponsored|advertisement|"
    r"unsubscribe|limited time|clearance|shop now|cart|points offer"
    r")\b",
    re.IGNORECASE,
)
NOISY_NEWS_TERMS = re.compile(
    r"\b("
    r"storage shed|siding|celebrity|sweepstakes|click here|sponsored|"
    r"restaurant special|menu now available"
    r")\b",
    re.IGNORECASE,
)
FAMILY_MAIL_ACCOUNTS = {
    "thetoniturner@icloud.com": "Toni Turner",
}
FAMILY_SCHOOL_TERMS = re.compile(
    r"\b("
    r"school|teacher|classroom|assignment|homework|grade|grades|conference|field trip|"
    r"permission|tryout|practice|coach|principal|counselor|pack post|charleston\.k12|"
    r"charleston county schools|byga|u12|u13|u14|u15|u16|u17|u18|sat|algebra"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class SourceResult:
    name: str
    ok: bool
    items: list[dict]
    error: str | None = None


def now_et() -> datetime:
    return datetime.now(ET)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": {}, "digests": [], "preferences": []}
    with STATE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_et().isoformat()
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    tmp.replace(STATE_PATH)


def remember_seen(state: dict, items: list[dict], max_seen: int = 5000) -> None:
    seen = state.setdefault("seen", {})
    stamp = now_et().isoformat()
    for item in items:
        key = item.get("key")
        if key:
            seen[key] = {"first_reported_at": stamp, "title": item.get("title") or item.get("subject")}
    if len(seen) > max_seen:
        keep = sorted(seen.items(), key=lambda pair: pair[1].get("first_reported_at", ""))[-max_seen:]
        state["seen"] = dict(keep)


def is_seen(state: dict, key: str) -> bool:
    return key in state.get("seen", {})


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            parts.append(part.decode(enc or "utf-8", "replace"))
        else:
            parts.append(part)
    return "".join(parts)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def first_value(mapping: dict, *names: str):
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def display_number(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}"
    return str(value)


def metric_text(label: str, value, suffix: str = "") -> str | None:
    if value in (None, ""):
        return None
    return f"{label} {display_number(value)}{suffix}"


def item_text(item: dict) -> str:
    return " ".join(
        normalize_text(str(item.get(field) or ""))
        for field in ("title", "subject", "snippet", "description", "from", "blog")
    )


def item_title(item: dict) -> str:
    return normalize_text(str(item.get("title") or item.get("subject") or "(untitled)"))


def packet_items(packet: dict, label: str) -> list[dict]:
    for source in packet.get("sources", []):
        if source.get("label") == label:
            return source.get("items", [])
    return []


def clean_source_error(label: str, error: str | None) -> str:
    """Turn collector failures into human-safe notes; never expose stack traces."""
    if not error:
        return ""
    text = normalize_text(str(error))
    lower = text.lower()
    if label.upper() == "WHOOP" or "whoop" in lower:
        return "WHOOP data was unavailable for this run, so the brief skipped recovery analysis."
    if "traceback" in lower:
        return f"{label} had a source error; details were suppressed."
    for noisy in ("file \"/", "systemexit", "urllib.error", "subprocess."):
        if noisy in lower:
            return f"{label} had a source error; details were suppressed."
    return f"{label}: {text[:180]}"


def compact_source_warnings(errors: list[str]) -> list[str]:
    warnings = []
    for error in errors:
        label, _, detail = error.partition(":")
        warning = clean_source_error(label.strip() or "Source", detail.strip() or error)
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


def markdown_item_link(item: dict) -> str:
    title = item_title(item)
    url = item.get("url")
    if url:
        return f"[{title}]({url})"
    sender = normalize_text(str(item.get("from") or ""))
    if sender:
        return f"{title} ({sender})"
    return title


def strip_duplicate_title_heading(markdown: str, title: str) -> str:
    """Notion already renders the page title; avoid a repeated H1 block."""
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("# "):
        return markdown
    heading = normalize_text(lines[0][2:])
    page_title = normalize_text(title)
    if heading == page_title or heading.lower().startswith("daily digest"):
        return "\n".join(lines[1:]).lstrip()
    return markdown


def canonical_digest_title() -> str:
    return f"Daily Digest - {now_et().strftime('%B %d, %Y')}"


def correct_weekday_labels(text: str, days: int = 14) -> str:
    """Fix model weekday arithmetic for visible date labels in the near-term brief."""
    fixed = text
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    today = now_et().date()
    for offset in range(days + 1):
        date_value = today + timedelta(days=offset)
        correct = date_value.strftime("%A")
        month = date_value.strftime("%B")
        day = str(date_value.day)
        for wrong in weekdays:
            if wrong == correct:
                continue
            fixed = re.sub(rf"\b{wrong}, {month} {day}\b", f"{correct}, {month} {day}", fixed)
            fixed = re.sub(rf"\b{wrong} {month} {day}\b", f"{correct} {month} {day}", fixed)
    return fixed


def normalize_digest_output(digest: dict) -> dict:
    digest["title"] = canonical_digest_title()
    for field in ("markdown", "telegram"):
        if digest.get(field):
            digest[field] = correct_weekday_labels(str(digest[field]))
    return digest


def ensure_source_links(digest: dict, packet: dict) -> dict:
    """Guarantee the Notion page has a compact link trail for source items."""
    markdown = str(digest.get("markdown") or "")
    existing = set(re.findall(r"(?:https?://)[^\s)>\]]+", markdown))
    groups = [
        ("Email Links", {"Gmail"}),
        ("Calendar & Task Links", {"Calendar", "Tasks"}),
        ("News & Research Links", {"Blogs / News"}),
    ]
    additions: list[str] = []
    seen_urls: set[str] = set(existing)
    for heading, labels in groups:
        lines = []
        for source in packet.get("sources", []):
            if source.get("label") not in labels:
                continue
            for item in source.get("items", []):
                url = str(item.get("url") or "")
                if not url.startswith("http") or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = item_title(item)
                source_label = source.get("label", "Source")
                lines.append(f"- [{title}]({url}) - {source_label}")
                if len(lines) >= 8:
                    break
            if len(lines) >= 8:
                break
        if lines:
            additions.extend(["", f"### {heading}", *lines])
    if additions:
        if "## Source Links" not in markdown:
            markdown = markdown.rstrip() + "\n\n## Source Links"
        markdown = markdown.rstrip() + "\n" + "\n".join(additions)
        digest["markdown"] = markdown
    return digest


def score_item(item: dict, label: str) -> int:
    """Rank items for a human morning brief, not for archival completeness."""
    text = item_text(item)
    score = 0
    if HIGH_SIGNAL_TERMS.search(text):
        score += 6
    if LOW_VALUE_TERMS.search(text):
        score -= 5
    if NOISY_NEWS_TERMS.search(text):
        score -= 6
    if label == "iCloud Mail" and item.get("seen") is False:
        score += 3
    if item.get("mailbox_owner") == "Toni Turner":
        score -= 2
        if FAMILY_SCHOOL_TERMS.search(text):
            score += 12
    if label == "Tasks":
        if item.get("overdue"):
            score += 10
        score += int(item.get("priority") or 1) * 2
        if item.get("description"):
            score += 1
    if label == "Calendar":
        score += 8
    if label == "Weather":
        score += 20
    if label == "Inbox Cleanup":
        score += 4
    if label == "Blogs / News":
        topic_text = text.lower()
        if any(term in topic_text for term in ("openai", "anthropic", "healthcare", "charleston", "mt pleasant")):
            score += 4
    return score


def curate_items(label: str, items: list[dict]) -> list[dict]:
    caps = {
        "iCloud Mail": 5,
        "Gmail": 5,
        "Calendar": 10,
        "Blogs / News": 8,
        "Weather": 1,
        "Inbox Cleanup": 1,
        "Tasks": 9,
    }
    if label == "Weather":
        return items[:1]
    ranked = sorted(
        items,
        key=lambda item: (score_item(item, label), item.get("date") or item.get("published") or item.get("due_date") or ""),
        reverse=True,
    )
    if label == "Blogs / News":
        high_signal = [item for item in ranked if score_item(item, label) >= 0]
        return high_signal[: caps[label]]
    return ranked[: caps.get(label, 8)]


def extract_text(message: email.message.Message, limit: int = 360) -> str:
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            ctype = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
                    break
    else:
        payload = message.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(message.get_content_charset() or "utf-8", "replace"))
    return normalize_text(" ".join(chunks))[:limit]


def collect_icloud_mail(state: dict, hours: int, limit: int) -> SourceResult:
    accounts: list[tuple[str, str]] = []
    for suffix in [""] + [f"_{i}" for i in range(2, 10)]:
        addr = os.getenv(f"ICLOUD_EMAIL{suffix}", "")
        password = os.getenv(f"ICLOUD_APP_PASSWORD{suffix}", "").replace("-", "")
        if addr and password:
            accounts.append((addr, password))
    if not accounts:
        return SourceResult("icloud_mail", False, [], "ICLOUD_EMAIL/ICLOUD_APP_PASSWORD not configured")

    items: list[dict] = []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    for addr, password in accounts:
        mailbox_owner = FAMILY_MAIL_ACCOUNTS.get(addr.lower(), "Matt Turner")
        try:
            box = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
            box.login(addr, password)
            box.select("INBOX", readonly=True)
            typ, data = box.uid("search", None, "ALL")
            if typ != "OK":
                raise RuntimeError("IMAP search failed")
            for uid in (data[0] or b"").split()[-max(limit * 4, 20):]:
                typ, parts = box.uid("fetch", uid, "(FLAGS BODY.PEEK[])")
                if typ != "OK":
                    continue
                raw = None
                flags = ""
                for part in parts:
                    if isinstance(part, tuple):
                        flags += part[0].decode("utf-8", "ignore") if isinstance(part[0], bytes) else str(part[0])
                        raw = part[1]
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                date_raw = decode_mime(msg.get("Date"))
                try:
                    msg_dt = parsedate_to_datetime(date_raw)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    msg_dt = datetime.now(timezone.utc)
                key = f"icloud:{addr}:{uid.decode()}"
                if msg_dt < since or is_seen(state, key):
                    continue
                items.append(
                    {
                        "key": key,
                        "source": "iCloud Mail",
                        "account": addr,
                        "mailbox_owner": mailbox_owner,
                        "from": decode_mime(msg.get("From")),
                        "subject": decode_mime(msg.get("Subject")) or "(no subject)",
                        "title": decode_mime(msg.get("Subject")) or "(no subject)",
                        "date": msg_dt.astimezone(ET).isoformat(),
                        "seen": "\\Seen" in flags,
                        "snippet": extract_text(msg),
                    }
                )
            box.logout()
        except Exception as exc:  # keep other sources alive
            return SourceResult("icloud_mail", False, items, str(exc))
    items.sort(key=lambda item: item.get("date") or "", reverse=True)
    return SourceResult("icloud_mail", True, items[: max(limit * max(1, len(accounts)), limit)])


def google_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = os.getenv("GOOGLE_TOKEN_FILE", "google_token.json")
    if not os.path.exists(token_file):
        raise RuntimeError(f"Missing GOOGLE_TOKEN_FILE: {token_file}")
    creds = Credentials.from_authorized_user_file(token_file)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise RuntimeError("Google token is invalid")
    return creds


def google_service(service: str, version: str):
    from googleapiclient.discovery import build as google_build

    return google_build(service, version, credentials=google_credentials(), cache_discovery=False)


def collect_gmail(state: dict, query: str, limit: int) -> SourceResult:
    try:
        svc = google_service("gmail", "v1")
        result = svc.users().messages().list(userId="me", q=query, maxResults=limit * 3).execute()
        items = []
        for row in result.get("messages", []):
            key = f"gmail:{row['id']}"
            if is_seen(state, key):
                continue
            detail = svc.users().messages().get(
                userId="me",
                id=row["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h.get("name", ""): h.get("value", "") for h in detail.get("payload", {}).get("headers", [])}
            items.append(
                {
                    "key": key,
                    "source": "Gmail",
                    "from": headers.get("From"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "title": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date"),
                    "url": f"https://mail.google.com/mail/u/0/#all/{row['id']}",
                    "snippet": (detail.get("snippet") or "")[:360],
                }
            )
            if len(items) >= limit:
                break
        return SourceResult("gmail", True, items)
    except Exception as exc:
        return SourceResult("gmail", False, [], str(exc))


def collect_google_calendar(days: int, limit: int) -> SourceResult:
    try:
        svc = google_service("calendar", "v3")
        start = now_et()
        end = start + timedelta(days=days)
        result = svc.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=limit,
        ).execute()
        items = []
        for event in result.get("items", []):
            items.append(
                {
                    "key": f"gcal:{event.get('id')}",
                    "source": "Google Calendar",
                    "title": event.get("summary", "(no title)"),
                    "start": event.get("start", {}),
                    "end": event.get("end", {}),
                    "location": event.get("location"),
                    "url": event.get("htmlLink"),
                }
            )
        return SourceResult("google_calendar", True, items)
    except Exception as exc:
        return SourceResult("google_calendar", False, [], str(exc))


ARTICLE_RE = re.compile(
    r"^\s*\[(?P<id>\d+)\]\s+\[new\]\s+(?P<title>.*?)\n"
    r"\s+Blog:\s+(?P<blog>.*?)\n"
    r"\s+URL:\s+(?P<url>\S+)"
    r"(?:\n\s+Published:\s+(?P<published>.*?))?(?:\n|$)",
    re.MULTILINE,
)


def collect_blogwatcher(state: dict, limit: int) -> SourceResult:
    if not BLOGWATCHER_BIN.exists():
        return SourceResult("blogwatcher", False, [], f"Missing {BLOGWATCHER_BIN}")
    env = os.environ.copy()
    env["BLOGWATCHER_DB"] = str(BLOGWATCHER_DB)
    env["BLOGWATCHER_YES"] = "1"
    try:
        subprocess.run([str(BLOGWATCHER_BIN), "scan"], env=env, text=True, capture_output=True, timeout=120)
        articles = subprocess.run(
            [str(BLOGWATCHER_BIN), "articles"],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        output = (articles.stdout or articles.stderr or "").strip()
        items = []
        for match in ARTICLE_RE.finditer(output):
            url = match.group("url")
            key = f"blog:{url}"
            if is_seen(state, key):
                continue
            items.append(
                {
                    "key": key,
                    "source": "Blogwatcher",
                    "blog": match.group("blog"),
                    "title": normalize_text(match.group("title")),
                    "url": url,
                    "published": normalize_text(match.group("published") or ""),
                }
            )
            if len(items) >= limit:
                break
        return SourceResult("blogwatcher", True, items)
    except Exception as exc:
        return SourceResult("blogwatcher", False, [], str(exc))


def collect_weather() -> SourceResult:
    """Fetch current conditions + today's forecast for Mt Pleasant SC (29466) via wttr.in."""
    try:
        url = "https://wttr.in/29466?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        current = data.get("current_condition", [{}])[0]
        weather = data.get("weather", [{}])[0]
        hourly = weather.get("hourly", [])
        high = weather.get("maxtempF", "?")
        low = weather.get("mintempF", "?")
        feels_like = current.get("FeelsLikeF", "?")
        desc = (current.get("weatherDesc") or [{}])[0].get("value", "")
        humidity = current.get("humidity", "?")
        wind_mph = current.get("windspeedMiles", "?")
        uv = current.get("uvIndex", "?")
        rain_chance = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)
        items = [{
            "key": "weather:today",
            "source": "Weather",
            "title": f"Mt Pleasant SC — {desc}, {current.get('temp_F', '?')}°F (feels {feels_like}°F)",
            "high_f": high,
            "low_f": low,
            "humidity_pct": humidity,
            "wind_mph": wind_mph,
            "uv_index": uv,
            "rain_chance_pct": rain_chance,
            "description": desc,
        }]
        return SourceResult("weather", True, items)
    except Exception as exc:
        return SourceResult("weather", False, [], str(exc))


def collect_todoist() -> SourceResult:
    """Fetch today's + overdue Todoist tasks via API v1."""
    token = os.getenv("TODOIST_API_TOKEN") or os.getenv("TODOIST_TOKEN")
    if not token:
        return SourceResult("todoist", False, [], "TODOIST_API_TOKEN not configured")
    try:
        # Paginate through all tasks
        all_tasks = []
        url = "https://api.todoist.com/api/v1/tasks?limit=200"
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tasks = data.get("results", data) if isinstance(data, dict) else data
            all_tasks.extend(tasks)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            url = f"https://api.todoist.com/api/v1/tasks?limit=200&cursor={cursor}" if cursor else None

        today_str = now_et().date().isoformat()
        items = []
        for t in all_tasks:
            due = (t.get("due") or {})
            due_date = due.get("date", "")
            overdue = bool(due_date) and due_date < today_str
            is_today = due_date == today_str
            # Only include tasks due today or overdue
            if not (overdue or is_today):
                continue
            items.append({
                "key": f"todoist:{t['id']}",
                "source": "Todoist",
                "title": t.get("content", "(no title)"),
                "priority": t.get("priority", 1),  # 4=urgent, 3=high, 2=medium, 1=normal
                "due_string": due.get("string", ""),
                "due_date": due_date,
                "overdue": overdue,
                "description": t.get("description", ""),
                "url": t.get("url"),
            })
        # Sort: overdue first, then by priority desc, then due date
        items.sort(key=lambda x: (not x["overdue"], -x["priority"], x.get("due_date", "")))
        return SourceResult("todoist", True, items)
    except Exception as exc:
        return SourceResult("todoist", False, [], str(exc))


def collect_whoop(days: int = 45) -> SourceResult:
    """Run the Jarvis WHOOP monitor and return one analyzed recovery item."""
    if not WHOOP_MONITOR_BIN.exists():
        return SourceResult("whoop", False, [], f"Missing {WHOOP_MONITOR_BIN}")
    env = os.environ.copy()
    env.setdefault("WHOOP_TOKEN_FILE", str(WHOOP_TOKEN_FILE))
    env.setdefault("WHOOP_CACHE_FILE", str(WHOOP_CACHE_FILE))
    try:
        proc = subprocess.run(
            [
                WHOOP_PYTHON_BIN,
                str(WHOOP_MONITOR_BIN),
                "analyze",
                "--days",
                str(days),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return SourceResult("whoop", False, [], clean_source_error("WHOOP", detail or f"whoop monitor exited {proc.returncode}"))
        analysis = json.loads(proc.stdout)
        latest = analysis.get("latest") or {}
        if not latest:
            return SourceResult("whoop", True, [])
        recovery = first_value(latest, "recovery", "recovery_score")
        hrv = first_value(latest, "hrv_rmssd", "hrv_rmssd_milli")
        rhr = first_value(latest, "rhr", "resting_heart_rate")
        strain = first_value(latest, "strain")
        color = str(latest.get("color", "unknown")).upper()
        title_parts = [f"WHOOP {color}"]
        if recovery is not None:
            title_parts.append(f"{display_number(recovery)}% recovery")
        for part in (
            metric_text("HRV", hrv, " ms"),
            metric_text("RHR", rhr),
            metric_text("strain", strain),
        ):
            if part:
                title_parts.append(part)
        title = " | ".join(title_parts)
        return SourceResult(
            "whoop",
            True,
            [
                {
                    "key": f"whoop:{latest.get('date')}",
                    "source": "WHOOP",
                    "title": title,
                    "date": latest.get("date"),
                    "latest": latest,
                    "trends": analysis.get("trends", {}),
                    "pattern_insights": analysis.get("pattern_insights", []),
                    "streaks": analysis.get("streaks", {}),
                    "insights": analysis.get("insights", []),
                    "recent_days": analysis.get("recent_days", [])[-7:],
                    "recent_workouts": analysis.get("recent_workouts", [])[-5:],
                }
            ],
        )
    except Exception as exc:
        return SourceResult("whoop", False, [], clean_source_error("WHOOP", str(exc)))


def env_truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def collect_inbox_cleanup(limit: int = 20, apply: bool = False) -> SourceResult:
    """Run the conservative inbox cleanup dry-run and summarize the result."""
    if not INBOX_CLEANER_BIN.exists():
        return SourceResult("inbox_cleanup", True, [])
    try:
        cmd = [
            sys.executable,
            str(INBOX_CLEANER_BIN),
            "scan",
            "--format",
            "json",
            "--limit",
            str(limit),
        ]
        if apply:
            cmd.extend(["--apply", "--confirm", "APPLY_INBOX_CLEANUP"])
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode not in (0, 2):
            detail = (proc.stderr or proc.stdout or "").strip()
            return SourceResult("inbox_cleanup", False, [], detail or f"inbox cleaner exited {proc.returncode}")
        summary = json.loads(proc.stdout or "{}")
        candidates = summary.get("candidates", [])
        safe = [c for c in candidates if c.get("risk") == "low" and c.get("proposed_action") == "archive"]
        review = [c for c in candidates if c.get("bucket") == "review" or c.get("proposed_action") == "review"]
        item = {
            "source": "Inbox Cleanup",
            "title": (
                f"Inbox cleanup {summary.get('mode', 'dry-run')}: {summary.get('total', 0)} scanned, "
                f"{summary.get('safe_archive_count', 0)} safe archive candidate(s), "
                f"{summary.get('applied_count', 0)} applied, "
                f"{summary.get('review_count', 0)} needs review"
            ),
            "mode": summary.get("mode", "dry-run"),
            "counts": summary.get("counts", {}),
            "proposed_actions": summary.get("proposed_actions", {}),
            "safe_archive_candidates": safe[:5],
            "review_candidates": review[:5],
            "errors": summary.get("errors", []),
            "snippet": (
                "Apply mode: low-risk allowlisted archive actions may run; delete/purge remains disabled."
                if summary.get("mode") == "apply"
                else "Dry-run only. No messages archived, deleted, moved, labeled, or marked read."
            ),
        }
        return SourceResult("inbox_cleanup", True, [item])
    except Exception as exc:
        return SourceResult("inbox_cleanup", False, [], str(exc))


def feedback_text() -> str:
    if not FEEDBACK_PATH.exists():
        return ""
    return FEEDBACK_PATH.read_text(encoding="utf-8")[:4000]


def fallback_digest(packet: dict) -> dict:
    date_label = now_et().strftime("%B %d, %Y")
    title = canonical_digest_title()
    weather = packet_items(packet, "Weather")
    calendar = packet_items(packet, "Calendar")
    icloud = packet_items(packet, "iCloud Mail")
    gmail = packet_items(packet, "Gmail")
    news = packet_items(packet, "Blogs / News")
    whoop = packet_items(packet, "WHOOP")
    cleanup = packet_items(packet, "Inbox Cleanup")
    tasks = packet_items(packet, "Tasks")
    warnings = compact_source_warnings(packet.get("errors", []))
    whoop_warning = any("whoop" in warning.lower() for warning in warnings)

    def calendar_line(item: dict) -> str:
        start = item.get("start", {}) if isinstance(item.get("start"), dict) else {}
        raw = start.get("dateTime") or start.get("date") or ""
        when = raw
        try:
            when_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            when = when_dt.astimezone(ET).strftime("%a %-I:%M %p")
        except Exception:
            pass
        location = normalize_text(str(item.get("location") or ""))
        suffix = f" at {location}" if location else ""
        return f"{when}: {item_title(item)}{suffix}" if when else item_title(item)

    email_items = icloud + gmail
    action_email = [
        item for item in email_items
        if HIGH_SIGNAL_TERMS.search(item_text(item)) and not LOW_VALUE_TERMS.search(item_text(item))
    ]
    if not action_email:
        action_email = email_items[:3]
    overdue_tasks = [item for item in tasks if item.get("overdue")]
    today_tasks = [item for item in tasks if not item.get("overdue")]

    markdown_lines = [
        f"# Daily Digest - {date_label}",
        "",
        "Good morning Matt,",
        "",
        "## Key Events & Upcoming",
    ]
    if weather:
        markdown_lines.append(f"- Weather: {item_title(weather[0])}")
    if calendar:
        markdown_lines.extend(f"- {calendar_line(item)}" for item in calendar[:5])
    else:
        markdown_lines.append("- No calendar events surfaced for the next window.")
    if overdue_tasks:
        markdown_lines.append(f"- {len(overdue_tasks)} overdue task(s) need triage; start with the family/admin items below.")

    markdown_lines.extend(["", "## Email Summary & Action Items"])
    if action_email:
        markdown_lines.extend(f"- {markdown_item_link(item)}" for item in action_email[:6])
    else:
        markdown_lines.append("- No high-signal email actions surfaced.")

    markdown_lines.extend(["", "## News Summary"])
    if news:
        markdown_lines.extend(f"- {markdown_item_link(item)}" for item in news[:6])
    else:
        markdown_lines.append("- No high-signal news links surfaced.")

    markdown_lines.extend(["", "## Research Findings"])
    research_items = [
        item for item in news
        if re.search(r"\b(ai|openai|anthropic|healthcare|medicine|longevity|research|study|llm)\b", item_text(item), re.I)
    ]
    if research_items:
        markdown_lines.extend(f"- {markdown_item_link(item)}" for item in research_items[:4])
    else:
        markdown_lines.append("- No new research findings captured in this run.")

    markdown_lines.extend(["", "## Health & Recovery"])
    if whoop:
        latest = whoop[0].get("latest", {})
        insights = whoop[0].get("insights", [])
        pattern_insights = whoop[0].get("pattern_insights", [])
        trends = whoop[0].get("trends", {})
        hrv = first_value(latest, "hrv_rmssd", "hrv_rmssd_milli")
        rhr = first_value(latest, "rhr", "resting_heart_rate")
        recovery = first_value(latest, "recovery", "recovery_score")
        strain = first_value(latest, "strain")
        sleep_performance = first_value(latest, "sleep_performance", "sleep_performance_percentage")
        color = str(latest.get("color", "unknown")).upper()
        metrics = []
        if recovery is not None:
            metrics.append(f"{display_number(recovery)}% recovery")
        if hrv is not None:
            metrics.append(f"HRV {display_number(hrv)} ms")
        if rhr is not None:
            metrics.append(f"RHR {display_number(rhr)}")
        if sleep_performance is not None:
            metrics.append(f"sleep {display_number(sleep_performance)}%")
        if strain is not None:
            metrics.append(f"strain {display_number(strain)}")
        markdown_lines.append(f"- WHOOP: {color} " + ", ".join(metrics))
        markdown_lines.extend(f"- {normalize_text(str(insight))}" for insight in insights[:3])
        if pattern_insights:
            markdown_lines.extend(f"- Trend: {normalize_text(str(insight))}" for insight in pattern_insights[:2])
        elif trends:
            recovery_trend = trends.get("recovery", {})
            hrv_trend = trends.get("hrv_rmssd", {})
            trend_bits = []
            if recovery_trend.get("avg_7d") is not None and recovery_trend.get("avg_30d") is not None:
                trend_bits.append(f"recovery 7d {display_number(recovery_trend.get('avg_7d'))} vs 30d {display_number(recovery_trend.get('avg_30d'))}")
            if hrv_trend.get("avg_7d") is not None and hrv_trend.get("avg_30d") is not None:
                trend_bits.append(f"HRV 7d {display_number(hrv_trend.get('avg_7d'))} vs 30d {display_number(hrv_trend.get('avg_30d'))} ms")
            if trend_bits:
                markdown_lines.append("- Trend: " + "; ".join(trend_bits))
    else:
        markdown_lines.append("- WHOOP recovery data was not included in this run.")

    markdown_lines.extend(["", "## Other / Admin"])
    if cleanup:
        markdown_lines.append(f"- {item_title(cleanup[0])}")
    if overdue_tasks:
        markdown_lines.extend(f"- Overdue: {item_title(item)}" for item in overdue_tasks[:6])
    if today_tasks:
        markdown_lines.extend(f"- Today: {item_title(item)}" for item in today_tasks[:4])
    if not cleanup and not tasks:
        markdown_lines.append("- No additional admin items surfaced.")

    markdown_lines.extend(["", "## Source Notes"])
    if warnings:
        markdown_lines.extend(f"- {warning}" for warning in warnings[:5])
    else:
        markdown_lines.append("- All configured sources completed without user-visible warnings.")

    weather_line = item_title(weather[0]) if weather else "Morning brief is ready."
    telegram_lines = [f"Good morning Matt - {date_label}", weather_line]
    if calendar:
        telegram_lines.append(f"- Today: {calendar_line(calendar[0])}")
    elif overdue_tasks:
        telegram_lines.append(f"- Today: no calendar events surfaced; {len(overdue_tasks)} overdue task(s) need triage.")
    if action_email:
        telegram_lines.append(f"- Email action: {item_title(action_email[0])}")
    if whoop:
        telegram_lines.append(f"- Health: {item_title(whoop[0])}")
        pattern_insights = whoop[0].get("pattern_insights", [])
        if pattern_insights:
            telegram_lines.append(f"- Trend: {normalize_text(str(pattern_insights[0]))}")
    elif whoop_warning:
        telegram_lines.append("- Health: WHOOP did not report cleanly this run; details are in Notion.")
    if news:
        telegram_lines.append(f"- News: {item_title(news[0])}")
    telegram_lines.append("Full brief: {{NOTION_URL}}")

    markdown = "\n".join(markdown_lines)
    digest = {
        "title": title,
        "markdown": markdown,
        "telegram": "\n".join(telegram_lines),
        "used_item_keys": [item["key"] for source in packet["sources"] for item in source["items"] if item.get("key")],
        "model_note": "fallback_extractive_digest",
    }
    return normalize_digest_output(ensure_source_links(digest, packet))


def anthropic_model() -> str:
    configured = os.getenv("DAILY_DIGEST_MODEL") or os.getenv("HERMES_PRIMARY_MODEL", "")
    if configured.startswith("anthropic/"):
        return configured.split("/", 1)[1]
    return configured or "claude-sonnet-4-5-20250929"


def summarize_with_llm(packet: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_TOKEN")
    if not api_key:
        return fallback_digest(packet)

    prompt = {
        "role": "user",
        "content": (
            "You are Jarvis, Matt Turner's personal AI assistant in Mt Pleasant, SC. "
            "Create Matt's 6:30 AM morning brief from this source packet. "
            "Use the packet's calendar_context.today_label exactly for today's date and weekday; "
            "do not infer weekdays from memory. "
            "Matt is a husband and father interested in health/longevity (HRV, Zone 2, sleep), "
            "AI/tech, investing, faith, and family. "
            "Be warm, precise, source-backed, and ruthless about noise. "
            "Highlight anything time-sensitive: overdue tasks, deadlines, appointments, family logistics, "
            "weather alerts, travel/reservations, money/medical/admin items, and genuinely important news. "
            "Do not list every item. Synthesize what Matt needs to know and what he should do next. "
            "If there is no real action, say that clearly instead of manufacturing urgency. "
            "The tone should feel like: 'Good Morning Matt,' followed by an executive brief. "
            "Do not provide trading, medical, or legal advice. "
            "Return strict JSON with keys: title, markdown, telegram, used_item_keys. "
            "The markdown field is the full rich Notion page. Start with one H1 title only, then 'Good Morning Matt,'. "
            "Use these exact sections in this order: Key Events & Upcoming, Email Summary & Action Items, "
            "News Summary, Research Findings, Health & Recovery, Other / Admin, Source Notes. "
            "In markdown, use clean links like [short title](https://example.com); never paste long bare URLs. "
            "For news and research items, preserve the source URL as a clickable title link whenever an item has url. "
            "For Gmail items, use the provided Gmail url when useful. Do not create mailto links for senders; "
            "show sender names as plain text instead. "
            "Never include Python tracebacks, stack traces, raw exception text, or source-count filler. "
            "If a source has a warning, put a short plain-English note only in Source Notes. "
            "The telegram value is a short friendly morning message: "
            "start with 'Good morning Matt', then one weather/logistics line and 3-5 bullets max. "
            "Never make Telegram a source count report, and never append raw warnings there. "
            "Use plain language, no hype, no long article lists, no generic encouragement. "
            "For tasks, surface overdue/high-priority work first and group low-priority tasks. "
            "For WHOOP, include recovery color, score, HRV, RHR, sleep/strain context, and always look for patterns "
            "across rolling 7-day, 30-day, 60-day, and 90-day trends when available. Surface meaningful trend changes "
            "instead of only today's snapshot, then give one practical recovery/training recommendation. Do not give medical advice. "
            "For inbox, skip promotions unless they affect travel, family, school, finance, health, or scheduling. "
            "Treat mail from mailbox_owner Toni Turner as Matt's wife's mailbox: do not summarize it broadly. "
            "Only surface nuggets about kids, teachers, school, coaches, family logistics, deadlines, forms, "
            "appointments, travel, bills, or action items; ignore her promotions and generic newsletters. "
            "For Inbox Cleanup, treat it as a dry-run report unless the source says apply mode; summarize safe archive "
            "candidates and review counts, and make clear that no mail was changed in dry-run mode. "
            "For news, prefer AI, healthcare, Charleston/Mt Pleasant, markets/macro, and Kentucky sports only when notable. "
            "Include a placeholder {{NOTION_URL}} where the full digest link goes. "
            "Use the feedback/preferences if present.\n\n"
            + json.dumps(packet, ensure_ascii=False)
        ),
    }
    body = {
        "model": anthropic_model(),
        "max_tokens": 3000,
        "temperature": 0.2,
        "messages": [prompt],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
        parsed = json.loads(text)
        parsed.setdefault("model_note", anthropic_model())
        return normalize_digest_output(ensure_source_links(parsed, packet))
    except Exception as exc:
        digest = fallback_digest(packet)
        digest["model_note"] = f"llm_failed:{exc}"
        return digest


def notion_request(method: str, path: str, payload: dict | None = None) -> dict:
    token = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_API_KEY is missing")
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?|mailto):[^)\s]+)\)")
BARE_URL_RE = re.compile(r"(?:https?://|mailto:)[^\s)>\]]+")


def notion_text(content: str, href: str | None = None, annotations: dict | None = None) -> dict:
    text = {"content": content}
    if href:
        text["link"] = {"url": href}
    item = {"type": "text", "text": text, "href": href}
    if annotations:
        item["annotations"] = annotations
    return item


def append_text_chunks(
    parts: list[dict],
    content: str,
    href: str | None = None,
    annotations: dict | None = None,
) -> None:
    for chunk in [content[i : i + 1900] for i in range(0, len(content), 1900)] or [""]:
        if chunk:
            parts.append(notion_text(chunk, href, annotations))


def append_plain_rich_text(
    parts: list[dict],
    content: str,
    href: str | None = None,
    annotations: dict | None = None,
) -> None:
    if href:
        append_text_chunks(parts, content, href, annotations)
        return
    pos = 0
    for match in BARE_URL_RE.finditer(content):
        url = match.group(0).rstrip(".,;:")
        trailing = match.group(0)[len(url):]
        append_text_chunks(parts, content[pos : match.start()], None, annotations)
        append_text_chunks(parts, url, url, annotations)
        append_text_chunks(parts, trailing, None, annotations)
        pos = match.end()
    append_text_chunks(parts, content[pos:], None, annotations)


def append_markdown_rich_text(
    parts: list[dict],
    content: str,
    href: str | None = None,
    bold: bool = False,
) -> None:
    annotations = {"bold": True} if bold else None
    pos = 0
    while pos < len(content):
        link = MARKDOWN_LINK_RE.search(content, pos)
        strong = re.search(r"\*\*(.+?)\*\*", content[pos:])
        strong_start = pos + strong.start() if strong else None

        if link and (strong_start is None or link.start() < strong_start):
            append_plain_rich_text(parts, content[pos : link.start()], href, annotations)
            append_markdown_rich_text(parts, link.group(1), link.group(2), bold)
            pos = link.end()
            continue

        if strong:
            start = pos + strong.start()
            end = pos + strong.end()
            append_plain_rich_text(parts, content[pos:start], href, annotations)
            append_markdown_rich_text(parts, strong.group(1), href, True)
            pos = end
            continue

        append_plain_rich_text(parts, content[pos:], href, annotations)
        break


def rich_text(content: str) -> list[dict]:
    """Convert small markdown inline syntax into native Notion rich_text."""
    parts: list[dict] = []
    append_markdown_rich_text(parts, content)
    return parts or [notion_text("")]


def clean_bullet_text(text: str) -> str:
    """Prefer linked titles over huge visible URLs in Notion bullets."""
    match = re.match(r"^(?P<title>.+?)\s+-\s+(?P<url>https?://\S+)\s*$", text)
    if match:
        title = normalize_text(match.group("title"))
        url = match.group("url").rstrip(".,;:")
        return f"[{title}]({url})"
    return text


def markdown_blocks(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.strip() in {"---", "***", "___"}:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        standalone_bold = re.fullmatch(r"\*\*(.+?)\*\*", line.strip())
        if standalone_bold:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {"rich_text": rich_text(standalone_bold.group(1))},
                }
            )
            continue
        if line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": rich_text(line[2:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(line[3:])}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich_text(line[4:])}})
        elif line.startswith(("- ", "* ", "• ")):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text(clean_bullet_text(line[2:]))}})
        elif re.match(r"^\d+\.\s+", line):
            text = re.sub(r"^\d+\.\s+", "", line, count=1)
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": rich_text(clean_bullet_text(text))}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(line)}})
    return blocks[:90]


def write_notion_digest(digest: dict) -> str:
    data_source_id = os.getenv("NOTION_DIGESTS_DATA_SOURCE_ID")
    if not data_source_id:
        raise RuntimeError("NOTION_DIGESTS_DATA_SOURCE_ID is missing")
    title = digest.get("title") or f"Daily Digest - {now_et().strftime('%b %d, %Y')}"
    date_value = now_et().date().isoformat()
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": date_value}},
            "Type": {"select": {"name": "Daily Digest"}},
        },
        "children": markdown_blocks(strip_duplicate_title_heading(digest.get("markdown", ""), title)),
    }
    page = notion_request("POST", "/pages", payload)
    return page.get("url", "")


def render_telegram(digest: dict, notion_url: str = "", dry_run: bool = False) -> str:
    telegram = digest.get("telegram") or digest.get("title") or "Daily Digest"
    if notion_url:
        telegram = telegram.replace("{{NOTION_URL}}", notion_url)
        if notion_url not in telegram:
            telegram += f"\n\nFull digest: {notion_url}"
        return telegram

    telegram = re.sub(r"\n?Full (?:brief|digest): \{\{NOTION_URL\}\}", "", telegram).strip()
    telegram = telegram.replace("{{NOTION_URL}}", "").strip()
    if dry_run:
        telegram += "\n\n[DRY RUN] Notion was not written."
    return telegram


def mark_blogwatcher_read() -> None:
    if not BLOGWATCHER_BIN.exists():
        return
    env = os.environ.copy()
    env["BLOGWATCHER_DB"] = str(BLOGWATCHER_DB)
    env["BLOGWATCHER_YES"] = "1"
    subprocess.run([str(BLOGWATCHER_BIN), "read-all"], env=env, text=True, capture_output=True, timeout=60)


def build_packet(args: argparse.Namespace, state: dict) -> tuple[dict, list[dict], list[str]]:
    results = [
        ("iCloud Mail", collect_icloud_mail(state, args.mail_hours, args.mail_limit)),
        ("Gmail", collect_gmail(state, args.gmail_query, args.mail_limit)),
        ("Calendar", collect_google_calendar(args.calendar_days, args.calendar_limit)),
        ("Blogs / News", collect_blogwatcher(state, args.news_limit)),
        ("Weather", collect_weather()),
        ("WHOOP", collect_whoop(args.whoop_days)),
        ("Inbox Cleanup", collect_inbox_cleanup(args.inbox_cleanup_limit, args.inbox_cleanup_apply)),
        ("Tasks", collect_todoist()),
    ]
    all_reportable: list[dict] = []
    errors: list[str] = []
    sources = []
    for label, result in results:
        if not result.ok and result.error:
            errors.append(clean_source_error(label, result.error))
        curated = curate_items(label, result.items)
        sources.append(
            {
                "label": label,
                "ok": result.ok,
                "error": clean_source_error(label, result.error) if result.error else None,
                "items": curated,
                "raw_count": len(result.items),
                "omitted_count": max(0, len(result.items) - len(curated)),
            }
        )
        # Weather is always included but not counted as "new" for dedup purposes
        # Tasks always surfaced
        if label not in ("Calendar", "Weather"):
            all_reportable.extend(curated)
    packet = {
        "generated_at": now_et().isoformat(),
        "timezone": "America/New_York",
        "location": "Mt Pleasant, SC 29466",
        "user": "Matt Turner",
        "calendar_context": {
            "today_iso": now_et().date().isoformat(),
            "today_label": now_et().strftime("%A, %B %d, %Y"),
            "tomorrow_label": (now_et() + timedelta(days=1)).strftime("%A, %B %d, %Y"),
        },
        "instructions": {
            "notes_engine": "Notion",
            "telegram_style": "short, warm, executive morning brief",
            "avoid_repeating": True,
            "always_include": ["weather", "tasks", "calendar"],
            "include_if_available": ["whoop"],
            "inbox_cleanup_mode": "apply" if args.inbox_cleanup_apply else "dry-run",
            "target_delivery_time": "6:30 AM America/New_York",
            "telegram_max_bullets": 5,
            "notion_should_include_source_notes": True,
        },
        "feedback": feedback_text(),
        "recent_digest_titles": [d.get("title") for d in state.get("digests", [])[-10:]],
        "sources": sources,
        "errors": errors,
    }
    return packet, all_reportable, errors


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mail-hours", type=int, default=48)
    parser.add_argument("--mail-limit", type=int, default=8)
    parser.add_argument("--gmail-query", default="newer_than:2d")
    parser.add_argument("--calendar-days", type=int, default=2)
    parser.add_argument("--calendar-limit", type=int, default=12)
    parser.add_argument("--news-limit", type=int, default=20)
    parser.add_argument("--whoop-days", type=int, default=90)
    parser.add_argument("--inbox-cleanup-limit", type=int, default=20)
    parser.add_argument(
        "--inbox-cleanup-apply",
        action="store_true",
        default=env_truthy("INBOX_CLEANUP_APPLY", False),
        help="Apply low-risk allowlisted inbox cleanup actions.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-repeat", action="store_true", help="Allow more than one written digest per local day.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    state = load_state()
    today = now_et().date().isoformat()
    if not args.dry_run and not args.allow_repeat:
        for digest in reversed(state.get("digests", [])):
            created_at = digest.get("created_at", "")
            if created_at.startswith(today):
                print("[SILENT]")
                return 0

    packet, reportable_items, errors = build_packet(args, state)
    has_calendar = any(source["label"] == "Calendar" and source["items"] for source in packet["sources"])
    has_tasks = any(source["label"] == "Tasks" and source["items"] for source in packet["sources"])
    has_weather = any(source["label"] == "Weather" and source["items"] for source in packet["sources"])

    # Always run if we have weather, tasks, or calendar — not just new email
    if not reportable_items and not has_calendar and not has_tasks and not has_weather:
        if not errors:
            print("[SILENT]")
            return 0
        digest = fallback_digest(packet)
        notion_url = ""
        if not args.dry_run:
            notion_url = write_notion_digest(digest)
            state.setdefault("digests", []).append(
                {
                    "title": digest.get("title"),
                    "url": notion_url,
                    "created_at": now_et().isoformat(),
                    "used_item_keys": [],
                    "model_note": digest.get("model_note"),
                }
            )
            state["digests"] = state["digests"][-200:]
            save_state(state)
        print(render_telegram(digest, notion_url, args.dry_run))
        return 0

    digest = summarize_with_llm(packet)
    notion_url = ""
    if not args.dry_run:
        notion_url = write_notion_digest(digest)
        remember_seen(state, reportable_items)
        state.setdefault("digests", []).append(
            {
                "title": digest.get("title"),
                "url": notion_url,
                "created_at": now_et().isoformat(),
                "used_item_keys": digest.get("used_item_keys", []),
                "model_note": digest.get("model_note"),
            }
        )
        state["digests"] = state["digests"][-200:]
        save_state(state)
        mark_blogwatcher_read()

    print(render_telegram(digest, notion_url, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
