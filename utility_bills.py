"""
Utility bill extraction from email.

Scans Gmail and, when configured, iCloud mail for monthly bill signals for:
- Mount Pleasant Water Works
- Berkeley/Berkley Electric
- Dominion Energy
"""
import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from memory import db_connect

logger = logging.getLogger("jarvis.utility_bills")


UTILITY_PROFILES: dict[str, dict[str, Any]] = {
    "water": {
        "label": "Mount Pleasant Water Works",
        "match_terms": [
            "mount pleasant water works",
            "mount pleasant waterworks",
            "mpwonline.com",
            "mountpleasantwaterworks.com",
        ],
        "sender_terms": [
            "mpwonline.com",
            "mountpleasantwaterworks.com",
            "invoicecloud",
        ],
        "gmail_queries": [
            '"Mount Pleasant Water Works"',
            '"Mount Pleasant Waterworks"',
            'from:mpwonline.com',
            'from:mountpleasantwaterworks.com',
        ],
    },
    "electric": {
        "label": "Berkley Electric",
        "match_terms": [
            "berkeley electric",
            "berkeley electric cooperative",
            "berkley electric",
            "berkeleyelectric.coop",
        ],
        "sender_terms": [
            "berkeleyelectric.coop",
            "smarthub",
        ],
        "gmail_queries": [
            '"Berkeley Electric"',
            '"Berkeley Electric Cooperative"',
            '"Berkley Electric"',
            'from:berkeleyelectric.coop',
        ],
    },
    "gas": {
        "label": "Dominion Energy",
        "match_terms": [
            "dominion energy",
            "dominion energy south carolina",
            "dominionenergy.com",
        ],
        "sender_terms": [
            "dominionenergy.com",
        ],
        "gmail_queries": [
            '"Dominion Energy"',
            '"Dominion Energy South Carolina"',
            'from:dominionenergy.com',
        ],
    },
}

MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

BILL_AMOUNT_PATTERNS = [
    re.compile(r"(?:total amount due|amount due|total due|balance due|current charges|payment due|autopay amount)\D{0,40}\$([0-9][0-9,]*\.[0-9]{2})", re.I),
    re.compile(r"(?:new balance|statement balance)\D{0,20}\$([0-9][0-9,]*\.[0-9]{2})", re.I),
]

DUE_DATE_PATTERNS = [
    re.compile(r"(?:due date|payment due(?: date)?)\D{0,20}([A-Z][a-z]+ \d{1,2}, \d{4})", re.I),
    re.compile(r"(?:due date|payment due(?: date)?)\D{0,20}(\d{1,2}/\d{1,2}/\d{2,4})", re.I),
]

BILL_KEYWORDS = (
    "bill",
    "statement",
    "invoice",
    "amount due",
    "payment due",
    "autopay",
)

NOISE_TERMS = (
    "nextdoor",
    "substack",
    "nytimes",
    "apnews",
    "newsletter",
    "daily digest",
)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str):
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return " ".join(self.parts)


@dataclass
class EmailCandidate:
    utility_key: str
    source: str
    external_id: str
    account: str
    sender: str
    subject: str
    sent_at: str
    body_text: str
    attachment_names: list[str]


def _strip_html(value: str) -> str:
    if not value:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(value)
    return stripper.get_text()


def _normalize_text(*values: str) -> str:
    raw = "\n".join(v for v in values if v)
    return re.sub(r"\s+", " ", raw).strip()


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _days_lookback(year: int, month: int) -> int:
    start, end = _month_bounds(year, month)
    today = date.today()
    if today < start:
        anchor = start
    else:
        anchor = today
    return max(45, (anchor - start).days + 45)


def _decode_gmail_part_body(payload: dict) -> str:
    data = (payload.get("body") or {}).get("data", "")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_gmail_parts(payload: dict) -> tuple[list[str], list[str], list[str]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    mime_type = payload.get("mimeType", "")
    filename = payload.get("filename", "") or ""
    if filename:
        attachments.append(filename)

    if mime_type == "text/plain":
        text = _decode_gmail_part_body(payload)
        if text:
            text_parts.append(text)
    elif mime_type == "text/html":
        html = _decode_gmail_part_body(payload)
        if html:
            html_parts.append(html)

    for part in payload.get("parts", []) or []:
        sub_text, sub_html, sub_attachments = _walk_gmail_parts(part)
        text_parts.extend(sub_text)
        html_parts.extend(sub_html)
        attachments.extend(sub_attachments)

    return text_parts, html_parts, attachments


def _gmail_candidates_sync(utility_key: str, profile: dict[str, Any], year: int, month: int) -> list[EmailCandidate]:
    from google_services import _build, _execute_with_retry

    service = _build("gmail", "v1")
    newer_than_days = _days_lookback(year, month)
    ids: dict[str, None] = {}

    for base_query in profile["gmail_queries"]:
        query = f"({base_query}) newer_than:{newer_than_days}d"
        try:
            result = _execute_with_retry(
                service.users().messages().list(userId="me", q=query, maxResults=10),
                "gmail.messages.list.utility",
            )
        except Exception as e:
            logger.warning("Utility Gmail query failed for %s: %s", utility_key, e)
            continue
        for msg in result.get("messages", []) or []:
            ids[msg["id"]] = None

    candidates: list[EmailCandidate] = []
    for msg_id in ids:
        detail = _execute_with_retry(
            service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full",
                metadataHeaders=["From", "Subject", "Date"],
            ),
            "gmail.messages.get.utility",
        )
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        text_parts, html_parts, attachment_names = _walk_gmail_parts(detail.get("payload", {}))
        body_text = _normalize_text(
            "\n".join(text_parts),
            _strip_html("\n".join(html_parts)),
            detail.get("snippet", ""),
        )
        sent_at = ""
        if detail.get("internalDate"):
            sent_at = datetime.fromtimestamp(int(detail["internalDate"]) / 1000).isoformat()
        elif headers.get("Date"):
            try:
                sent_at = parsedate_to_datetime(headers["Date"]).isoformat()
            except Exception:
                sent_at = headers["Date"]

        candidates.append(
            EmailCandidate(
                utility_key=utility_key,
                source="gmail",
                external_id=msg_id,
                account="gmail",
                sender=headers.get("From", ""),
                subject=headers.get("Subject", ""),
                sent_at=sent_at,
                body_text=body_text,
                attachment_names=attachment_names,
            )
        )

    return candidates


async def _icloud_candidates(utility_key: str, profile: dict[str, Any], year: int, month: int) -> list[EmailCandidate]:
    try:
        from icloud_mail import search_emails
    except Exception as e:
        logger.warning("iCloud search import failed for %s: %s", utility_key, e)
        return []

    since_days = _days_lookback(year, month)
    seen: dict[str, EmailCandidate] = {}
    for term in profile["match_terms"]:
        try:
            matches = await search_emails(term, since_days=since_days)
        except Exception as e:
            logger.warning("iCloud search failed for %s term '%s': %s", utility_key, term, e)
            return []
        for msg in matches:
            body_text = _normalize_text(
                msg.get("body_text", ""),
                _strip_html(msg.get("body_html", "")),
            )
            candidate = EmailCandidate(
                utility_key=utility_key,
                source="icloud",
                external_id=msg.get("uid", ""),
                account=msg.get("account", ""),
                sender=msg.get("from", ""),
                subject=msg.get("subject", ""),
                sent_at=msg.get("date", "") or "",
                body_text=body_text,
                attachment_names=list(msg.get("attachments", []) or []),
            )
            seen[candidate.external_id] = candidate
    return list(seen.values())


def _extract_amount(text: str) -> float | None:
    for pattern in BILL_AMOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _extract_due_date(text: str) -> str | None:
    for pattern in DUE_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip()
        for fmt in ("%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _extract_statement_month(text: str) -> tuple[int, int] | None:
    lowered = text.lower()
    for month_name, month_num in MONTH_NAMES.items():
        match = re.search(rf"\b{month_name}\s+(\d{{4}})\b", lowered)
        if match:
            return int(match.group(1)), month_num
    return None


def _candidate_sent_month(candidate: EmailCandidate) -> tuple[int, int] | None:
    if not candidate.sent_at:
        return None
    try:
        dt = datetime.fromisoformat(candidate.sent_at.replace("Z", "+00:00"))
        return dt.year, dt.month
    except Exception:
        return None


def _score_candidate(candidate: EmailCandidate, profile: dict[str, Any], year: int, month: int) -> tuple[int, float | None, str | None]:
    sender = (candidate.sender or "").lower()
    subject = (candidate.subject or "").lower()
    body = (candidate.body_text or "").lower()
    attachments = " ".join(candidate.attachment_names).lower()
    text = _normalize_text(sender, subject, body, attachments).lower()

    score = 0
    if any(term in sender for term in profile["sender_terms"]):
        score += 8
    if any(term in text for term in profile["match_terms"]):
        score += 6
    if any(keyword in text for keyword in BILL_KEYWORDS):
        score += 3
    if any(noise in sender or noise in subject for noise in NOISE_TERMS):
        score -= 8

    amount = _extract_amount(candidate.body_text)
    if amount is not None:
        score += 6

    due_date = _extract_due_date(candidate.body_text)
    if due_date:
        score += 4
        try:
            due = date.fromisoformat(due_date)
            if due.year == year and due.month == month:
                score += 5
        except ValueError:
            pass

    statement_month = _extract_statement_month(_normalize_text(candidate.subject, candidate.body_text))
    if statement_month == (year, month):
        score += 5

    sent_month = _candidate_sent_month(candidate)
    if sent_month == (year, month):
        score += 2

    return score, amount, due_date


def _build_excerpt(candidate: EmailCandidate) -> str:
    raw = _normalize_text(candidate.subject, candidate.body_text)
    return raw[:600]


async def _upsert_signal(row: dict[str, Any]):
    now = datetime.utcnow().isoformat()
    async with db_connect() as db:
        await db.execute(
            """
            INSERT INTO utility_bill_signals (
                utility_key, statement_month, status, amount, due_date, source, account,
                external_id, sender, subject, confidence, excerpt, attachment_names,
                matched_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(utility_key, statement_month) DO UPDATE SET
                status=excluded.status,
                amount=excluded.amount,
                due_date=excluded.due_date,
                source=excluded.source,
                account=excluded.account,
                external_id=excluded.external_id,
                sender=excluded.sender,
                subject=excluded.subject,
                confidence=excluded.confidence,
                excerpt=excluded.excerpt,
                attachment_names=excluded.attachment_names,
                matched_at=excluded.matched_at,
                updated_at=excluded.updated_at
            """,
            (
                row["utility_key"],
                row["statement_month"],
                row["status"],
                row.get("amount"),
                row.get("due_date"),
                row.get("source", ""),
                row.get("account", ""),
                row.get("external_id", ""),
                row.get("sender", ""),
                row.get("subject", ""),
                row.get("confidence", 0.0),
                row.get("excerpt", ""),
                row.get("attachment_names", ""),
                row.get("matched_at", now),
                now,
            ),
        )
        await db.commit()


async def scan_monthly_utility_bills(year: int | None = None, month: int | None = None) -> list[dict[str, Any]]:
    today = date.today()
    year = year or today.year
    month = month or today.month
    statement_month = f"{year:04d}-{month:02d}"

    results: list[dict[str, Any]] = []

    for utility_key, profile in UTILITY_PROFILES.items():
        gmail_candidates = await asyncio.to_thread(_gmail_candidates_sync, utility_key, profile, year, month)
        icloud_candidates = await _icloud_candidates(utility_key, profile, year, month)
        candidates = gmail_candidates + icloud_candidates

        best_candidate: EmailCandidate | None = None
        best_score = -999
        best_amount: float | None = None
        best_due_date: str | None = None

        for candidate in candidates:
            score, amount, due_date = _score_candidate(candidate, profile, year, month)
            if score > best_score:
                best_candidate = candidate
                best_score = score
                best_amount = amount
                best_due_date = due_date

        if best_candidate and best_score >= 12 and best_amount is not None:
            row = {
                "utility_key": utility_key,
                "statement_month": statement_month,
                "status": "found",
                "amount": best_amount,
                "due_date": best_due_date,
                "source": best_candidate.source,
                "account": best_candidate.account,
                "external_id": best_candidate.external_id,
                "sender": best_candidate.sender,
                "subject": best_candidate.subject,
                "confidence": round(min(0.99, 0.45 + (best_score / 25.0)), 2),
                "excerpt": _build_excerpt(best_candidate),
                "attachment_names": ", ".join(best_candidate.attachment_names),
                "matched_at": datetime.utcnow().isoformat(),
                "utility_name": profile["label"],
                "search_status": "gmail+icloud" if icloud_candidates else "gmail-only",
            }
        else:
            row = {
                "utility_key": utility_key,
                "statement_month": statement_month,
                "status": "not_found",
                "amount": None,
                "due_date": None,
                "source": best_candidate.source if best_candidate else "",
                "account": best_candidate.account if best_candidate else "",
                "external_id": best_candidate.external_id if best_candidate else "",
                "sender": best_candidate.sender if best_candidate else "",
                "subject": best_candidate.subject if best_candidate else "",
                "confidence": 0.0,
                "excerpt": _build_excerpt(best_candidate) if best_candidate else "",
                "attachment_names": ", ".join(best_candidate.attachment_names) if best_candidate else "",
                "matched_at": datetime.utcnow().isoformat(),
                "utility_name": profile["label"],
                "search_status": "gmail+icloud" if icloud_candidates else "gmail-only",
            }

        await _upsert_signal(row)
        results.append(row)

    return results


async def get_cached_utility_bills(statement_month: str | None = None) -> list[dict[str, Any]]:
    if not statement_month:
        today = date.today()
        statement_month = f"{today.year:04d}-{today.month:02d}"

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT utility_key, statement_month, status, amount, due_date, source, account,
                   external_id, sender, subject, confidence, excerpt, attachment_names,
                   matched_at, updated_at
            FROM utility_bill_signals
            WHERE statement_month = ?
            ORDER BY utility_key
            """,
            (statement_month,),
        )
        rows = await cursor.fetchall()

    return [
        {
            "utility_key": r[0],
            "statement_month": r[1],
            "status": r[2],
            "amount": r[3],
            "due_date": r[4],
            "source": r[5],
            "account": r[6],
            "external_id": r[7],
            "sender": r[8],
            "subject": r[9],
            "confidence": r[10],
            "excerpt": r[11],
            "attachment_names": r[12],
            "matched_at": r[13],
            "updated_at": r[14],
            "utility_name": UTILITY_PROFILES.get(r[0], {}).get("label", r[0]),
        }
        for r in rows
    ]


async def format_monthly_utility_bills(year: int | None = None, month: int | None = None, refresh: bool = True) -> str:
    today = date.today()
    year = year or today.year
    month = month or today.month
    statement_month = f"{year:04d}-{month:02d}"

    rows = await scan_monthly_utility_bills(year=year, month=month) if refresh else await get_cached_utility_bills(statement_month)
    if not rows:
        rows = await get_cached_utility_bills(statement_month)

    month_label = datetime(year, month, 1).strftime("%B %Y")
    lines = [f"Utility bill signal — {month_label}"]
    for row in rows:
        if row["status"] == "found":
            amount = f"${row['amount']:,.2f}" if row.get("amount") is not None else "amount missing"
            due = f", due {row['due_date']}" if row.get("due_date") else ""
            lines.append(
                f"- {row['utility_name']}: {amount}{due} "
                f"[{row['source']}, confidence {row.get('confidence', 0):.2f}]"
            )
            lines.append(f"  Sender: {row.get('sender', '(unknown)')}")
            lines.append(f"  Subject: {row.get('subject', '(no subject)')}")
        else:
            sender_hint = f" Best candidate sender: {row['sender']}" if row.get("sender") else ""
            lines.append(f"- {row['utility_name']}: no reliable bill found.{sender_hint}")
    return "\n".join(lines)
