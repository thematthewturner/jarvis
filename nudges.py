"""
Actionable Nudges automation.

Goals:
- Weekly pool chemistry reminder only when needed (stale or out-of-range readings).
- Daily email-to-task automation for clear action items only.
- Daily intentionality rhythm prompts when a stored cadence comes due.
- Low-noise operation: dedupe by source item, only notify when tasks are created.
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config import ANTHROPIC_API_KEY
from llm import complete_json
from memory import DB_PATH, db_connect
from tools import add_task, update_task

logger = logging.getLogger("jarvis.nudges")
ET = ZoneInfo("America/New_York")


ACTION_EXTRACTION_PROMPT = """You turn inbox signals into concrete Todoist tasks for Matt.

Given a JSON array of candidate items, decide if each item needs a task.
Return a JSON array in the SAME ORDER with objects:
{
  "key": "<same key>",
  "create_task": true|false,
  "task": "<short imperative task text, <= 95 chars>",
  "due_string": "<natural language due date like today/tomorrow/Friday, or empty>",
  "priority": 1|2|3|4,
  "reason": "<brief reason>"
}

Priority map:
- 4 urgent/deadline within 24h/payment due now
- 3 time-sensitive this week
- 2 useful follow-up
- 1 not urgent

Rules:
- Create task ONLY for clear, specific actions Matt must take.
- No task for FYI/news/marketing/non-actionable updates.
- Be aggressive about school, teacher, coach, classroom, or family logistics emails that ask for something specific.
- If an email asks a parent to send, bring, upload, reply, sign, RSVP, or remember something for a child, create a task.
- The email may have been sent to Matt or Toni; if it is a real family action item, still create the task for Matt.
- If candidate.suggested_action is explicit, prefer that wording.
- Keep tasks concrete and executable by one person.
- Output ONLY valid JSON. No markdown.
"""

async def _init_db():
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS nudge_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nudge_type  TEXT NOT NULL,
                unique_key  TEXT NOT NULL UNIQUE,
                status      TEXT NOT NULL,
                task_id     TEXT,
                payload_json TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.commit()


async def _event_exists(unique_key: str) -> bool:
    import aiosqlite

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 FROM nudge_events WHERE unique_key = ? LIMIT 1", (unique_key,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row)


async def _save_event(
    nudge_type: str,
    unique_key: str,
    status: str,
    task_id: str | None,
    payload: dict,
):
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO nudge_events
            (nudge_type, unique_key, status, task_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                nudge_type,
                unique_key,
                status,
                task_id,
                json.dumps(payload, ensure_ascii=True),
            ),
        )
        await db.commit()


def _extract_task_id(text: str) -> str | None:
    m = re.search(r"\[task_id:([^\]]+)\]", text or "")
    return m.group(1).strip() if m else None


async def _create_todoist_task(content: str, due_string: str = "", priority: int = 1) -> tuple[str, str | None]:
    result = await add_task({"content": content, "due_string": due_string or ""})
    task_id = _extract_task_id(result)
    if task_id and priority in (2, 3, 4):
        try:
            await update_task({"task_id": task_id, "priority": int(priority)})
        except Exception as e:
            logger.warning("Failed setting Todoist priority for task %s: %s", task_id, e)
    return result, task_id


async def _fetch_starred_triage_candidates(limit: int = 50) -> list[dict]:
    """Candidates from inbox triage (iCloud + Gmail), newest first."""
    import aiosqlite

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT uid, account, from_addr, subject, reason, run_at, created_at
            FROM inbox_triage_log
            WHERE action = 'star'
              AND created_at >= datetime('now', '-2 day')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    out = []
    for r in rows:
        key = f"email:{r.get('account','unknown')}:{r.get('uid','')}"
        out.append(
            {
                "key": key,
                "source": "triage",
                "account": r.get("account", ""),
                "from": r.get("from_addr", ""),
                "subject": r.get("subject", "(no subject)"),
                "reason": r.get("reason", ""),
                "suggested_action": "",
                "run_at": r.get("run_at", ""),
            }
        )
    return out


async def _fetch_kids_digest_candidates(limit: int = 30) -> list[dict]:
    """Candidates from kids digest items that already include explicit action text."""
    import aiosqlite

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT date, summary, items_json, created_at
            FROM kids_digest
            WHERE created_at >= datetime('now', '-2 day')
            ORDER BY created_at DESC
            LIMIT 2
            """
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    out: list[dict] = []
    for row in rows:
        items = json.loads(row.get("items_json") or "[]")
        for i, item in enumerate(items):
            action = (item.get("action") or "").strip()
            if not action:
                continue
            sig = f"{row.get('date','')}|{item.get('subject','')}|{action}|{i}"
            digest_key = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:14]
            out.append(
                {
                    "key": f"kids:{digest_key}",
                    "source": "kids_digest",
                    "account": item.get("account", ""),
                    "from": item.get("from", ""),
                    "subject": item.get("subject", "(no subject)"),
                    "reason": item.get("digest", ""),
                    "suggested_action": action,
                    "run_at": row.get("created_at", ""),
                }
            )
            if len(out) >= limit:
                return out
    return out


def _looks_like_family_action_signal(email_item: dict) -> bool:
    text_parts = [
        email_item.get("from", ""),
        email_item.get("subject", ""),
        email_item.get("body_text", ""),
        " ".join(email_item.get("attachments") or []),
    ]
    text = " ".join(text_parts).lower()
    sender = (email_item.get("from", "") or "").lower()

    strong_family_signals = (
        "teacher", "school", "classroom", "student", "field trip", "permission slip",
        "schoology", "teacher conference", "report card", "yearbook", "baby photo",
        "class photo", "coach", "cheer", "track", "practice", "uniform", "tryout",
        "parent meeting", "pta", "dismissal", "attendance office", "homeroom",
        "wando", "pack athletics", "charlestoncountyschools", "seacoast kids",
    )
    action_cues = (
        "bring", "send", "upload", "reply", "respond", "sign", "rsvp", "complete",
        "submit", "remember", "photo", "picture", "form", "due", "deadline",
        "tomorrow", "today", "urgent", "by ", "before ", "needed",
    )
    generic_non_family = (
        "bank", "mortgage", "credit card", "activation", "partnership", "sermon notes",
        "newsletter", "promotion", "offer", "receipt", "statement",
    )

    has_strong_signal = any(token in text or token in sender for token in strong_family_signals)
    has_action_cue = any(token in text for token in action_cues)
    looks_generic = any(token in text for token in generic_non_family)

    if has_strong_signal:
        return True
    if looks_generic:
        return False
    schoolish_sender = any(token in sender for token in ("school", "teacher", "coach", ".edu", "charlestoncountyschools"))
    return schoolish_sender and has_action_cue


async def _fetch_recent_family_email_candidates(limit: int = 25, since_hours: int = 72) -> list[dict]:
    """
    Directly inspect recent iCloud emails across all configured parent accounts.
    This catches school/family asks even when the email was read but forgotten.
    """
    from icloud_mail import fetch_recent_emails

    emails = await fetch_recent_emails(since_hours=since_hours, unread_only=False)
    out: list[dict] = []
    for email_item in emails:
        if not _looks_like_family_action_signal(email_item):
            continue

        body = (email_item.get("body_text") or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."

        out.append(
            {
                "key": f"email:{email_item.get('account','unknown')}:{email_item.get('uid','')}",
                "source": "family_direct_email",
                "account": email_item.get("account", ""),
                "from": email_item.get("from", ""),
                "subject": email_item.get("subject", "(no subject)"),
                "reason": body,
                "suggested_action": "",
                "run_at": email_item.get("date", ""),
            }
        )
        if len(out) >= limit:
            break
    return out


async def _fetch_recent_family_gmail_candidates(limit: int = 20, newer_than_days: int = 3) -> list[dict]:
    """
    Inspect recent Gmail for family/school asks using the same direct logic as iCloud.
    """
    from google_services import fetch_recent_gmail_messages

    query = f"in:inbox newer_than:{max(1, int(newer_than_days or 3))}d"
    emails = await fetch_recent_gmail_messages(query=query, max_results=max(limit * 2, 20), include_body=True)
    out: list[dict] = []
    for email_item in emails:
        if not _looks_like_family_action_signal(email_item):
            continue

        reason_bits = [email_item.get("snippet", "").strip(), (email_item.get("body_text") or "").strip()]
        reason = " ".join(bit for bit in reason_bits if bit).strip()
        if len(reason) > 500:
            reason = reason[:500] + "..."

        out.append(
            {
                "key": f"email:gmail:{email_item.get('id','')}",
                "source": "family_direct_gmail",
                "account": "gmail",
                "from": email_item.get("from", ""),
                "subject": email_item.get("subject", "(no subject)"),
                "reason": reason,
                "suggested_action": "",
                "run_at": email_item.get("date", ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def _candidate_source_rank(source: str) -> int:
    ranks = {
        "kids_digest": 0,
        "family_direct_email": 1,
        "family_direct_gmail": 1,
        "triage": 2,
    }
    return ranks.get(source, 9)


async def _classify_action_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    payload = json.dumps(candidates, ensure_ascii=True)
    decisions = await complete_json(
        model="extract",
        max_tokens=1800,
        system=ACTION_EXTRACTION_PROMPT,
        user_content=payload,
        timeout_seconds=90,
    )
    if not isinstance(decisions, list):
        raise ValueError("Action extraction response is not a JSON array")
    return decisions


async def run_daily_email_action_nudges(notify_empty: bool = False) -> str:
    """
    Create Todoist tasks from clear, actionable email signals, then run any
    due intentionality rhythm prompts.

    Sources:
    - Starred decisions from triage (last 48h)
    - Kids digest items with explicit action text
    - Direct recent family/school emails across both iCloud parent inboxes
    """
    await _init_db()
    email_text = ""
    from jarvis_ops import get_preferences

    prefs = await get_preferences()
    max_new_tasks = int(prefs.get("todoist_nudge_daily_limit", 3))
    nudge_mode = str(prefs.get("todoist_nudge_mode", "conservative"))

    if not ANTHROPIC_API_KEY:
        email_text = "Email nudges skipped: ANTHROPIC_API_KEY missing." if notify_empty else ""
    else:
        source_results = await asyncio.gather(
            _fetch_starred_triage_candidates(limit=60),
            _fetch_kids_digest_candidates(limit=20),
            _fetch_recent_family_email_candidates(limit=25, since_hours=72),
            _fetch_recent_family_gmail_candidates(limit=20, newer_than_days=3),
            return_exceptions=True,
        )

        merged: list[dict] = []
        source_names = ["triage", "kids_digest", "family_icloud", "family_gmail"]
        for source_name, result in zip(source_names, source_results):
            if isinstance(result, Exception):
                logger.error("Email nudge source %s failed: %s", source_name, result)
                continue
            merged.extend(result)

        # De-dup + prefer richer sources, then drop already-processed.
        unique: dict[str, dict] = {}
        for c in merged:
            dedupe_key = str(c.get("key", "")).strip()
            existing = unique.get(dedupe_key)
            if not existing or _candidate_source_rank(str(c.get("source", ""))) < _candidate_source_rank(str(existing.get("source", ""))):
                unique[dedupe_key] = c

        pending = []
        for key, item in unique.items():
            if not await _event_exists(key):
                pending.append(item)

        pending.sort(key=lambda item: str(item.get("run_at", "")), reverse=True)
        pending.sort(key=lambda item: _candidate_source_rank(str(item.get("source", ""))))

        if not pending:
            email_text = "No new actionable email candidates." if notify_empty else ""
        else:
            # Keep token/latency bounded
            pending = pending[:45]

            try:
                decisions = await _classify_action_candidates(pending)
            except Exception as e:
                logger.error("Email nudge classification failed: %s", e)
                email_text = f"Email action nudge failed: {e}" if notify_empty else ""
            else:
                cand_by_key = {c["key"]: c for c in pending}
                created: list[str] = []
                skipped = 0
                creation_candidates: list[dict] = []

                for d in decisions:
                    key = str(d.get("key", "")).strip()
                    cand = cand_by_key.get(key)
                    if not cand:
                        continue

                    create_task = bool(d.get("create_task", False))
                    task = str(d.get("task", "")).strip()
                    due_string = str(d.get("due_string", "")).strip()
                    reason = str(d.get("reason", "")).strip()
                    try:
                        priority = int(d.get("priority", 1))
                    except (TypeError, ValueError):
                        priority = 1
                    priority = max(1, min(4, priority))

                    payload = {
                        "candidate": cand,
                        "decision": {
                            "create_task": create_task,
                            "task": task,
                            "due_string": due_string,
                            "priority": priority,
                            "reason": reason,
                        },
                    }

                    if not create_task:
                        skipped += 1
                        await _save_event("email_action", key, "skipped", None, payload)
                        continue

                    if nudge_mode == "conservative" and priority <= 1:
                        skipped += 1
                        await _save_event("email_action", key, "skipped", None, payload)
                        continue

                    if not task:
                        task = f"Email follow-up: {cand.get('subject', '(no subject)')[:70]}"

                    creation_candidates.append(
                        {
                            "key": key,
                            "task": task[:95],
                            "due_string": due_string,
                            "priority": priority,
                            "payload": payload,
                            "run_at": str(cand.get("run_at", "")),
                        }
                    )

                creation_candidates.sort(key=lambda item: (-item["priority"], item["run_at"]), reverse=False)
                task_limit = max_new_tasks + 2 if nudge_mode == "aggressive" else max_new_tasks

                for item in creation_candidates[:task_limit]:
                    try:
                        _, task_id = await _create_todoist_task(
                            item["task"],
                            due_string=item["due_string"],
                            priority=item["priority"],
                        )
                        await _save_event("email_action", item["key"], "created", task_id, item["payload"])
                        created.append(item["task"])
                    except Exception as e:
                        logger.error("Failed creating Todoist task for %s: %s", item["key"], e)

                for item in creation_candidates[task_limit:]:
                    skipped += 1
                    await _save_event("email_action", item["key"], "skipped", None, item["payload"])

                if created:
                    lines = [
                        f"🧭 Action Nudges — created {len(created)} Todoist task(s) from email:",
                        "",
                    ]
                    for t in created[:8]:
                        lines.append(f"• {t}")
                    if len(created) > 8:
                        lines.append(f"• … and {len(created) - 8} more")

                    if skipped:
                        lines.append("")
                        lines.append(f"Skipped {skipped} non-actionable candidates.")

                    email_text = "\n".join(lines)
                else:
                    email_text = "No clear actionable emails needed new tasks today." if notify_empty else ""

    intentionality_text = ""
    try:
        from intentionality import run_intentionality_todo_nudges

        intentionality_text = await run_intentionality_todo_nudges(notify_empty=notify_empty)
    except Exception as e:
        logger.error("Intentionality nudge run failed: %s", e)
        intentionality_text = f"Intentionality nudges failed: {e}" if notify_empty else ""

    return "\n\n".join([text for text in [email_text, intentionality_text] if text])


def _pool_out_of_range_labels(reading: dict) -> list[str]:
    ideal = {
        "fc": (2.0, 4.0, "FC"),
        "ph": (7.4, 7.6, "pH"),
        "ta": (80, 120, "TA"),
        "salt": (2700, 3400, "Salt"),
        "cya": (30, 50, "CYA"),
    }
    labels: list[str] = []
    for key, (lo, hi, label) in ideal.items():
        val = reading.get(key)
        if val is None:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v < lo:
            labels.append(f"{label} low ({v:g})")
        elif v > hi:
            labels.append(f"{label} high ({v:g})")
    return labels


async def run_weekly_pool_nudge(force: bool = False, notify_empty: bool = False) -> str:
    """
    Weekly pool reminder automation.

    Creates a Todoist task only when:
    - no recent pool chemistry reading, or
    - latest reading is stale (>7 days), or
    - latest reading is out of ideal range.

    By default runs only on Mondays unless force=True.
    """
    await _init_db()

    now = datetime.now(ET)
    if not force and now.weekday() != 0:  # Monday only
        return ""

    import aiosqlite

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT logged_at, fc, ph, ta, salt, cya, ch, overall_score
            FROM pool_readings
            ORDER BY logged_at DESC
            LIMIT 1
            """
        ) as cur:
            row = await cur.fetchone()

    reading = dict(row) if row else {}
    reasons: list[str] = []

    days_since = None
    if reading.get("logged_at"):
        try:
            logged = datetime.fromisoformat(str(reading["logged_at"]).replace("Z", "+00:00"))
            days_since = (now.date() - logged.date()).days
        except Exception:
            days_since = None

    if not reading:
        reasons.append("no chemistry reading logged")
    elif days_since is None or days_since > 7:
        reasons.append(f"last chemistry test was {days_since if days_since is not None else '?'} days ago")

    out_of_range = _pool_out_of_range_labels(reading) if reading else []
    if out_of_range:
        reasons.append("out-of-range: " + ", ".join(out_of_range[:3]))

    if not reasons:
        return "Pool chemistry nudge not needed this week." if notify_empty else ""

    iso_year, iso_week, _ = now.isocalendar()
    unique_key = f"pool_check:{iso_year}-W{iso_week}"
    if await _event_exists(unique_key):
        return "Pool chemistry nudge already created this week." if notify_empty else ""

    due_string = "today" if out_of_range else "this week"
    task_content = "Check pool chemistry and log FC/pH/TA/salt/CYA/CH"

    try:
        _, task_id = await _create_todoist_task(task_content, due_string=due_string, priority=3 if out_of_range else 2)
    except Exception as e:
        logger.error("Pool nudge task creation failed: %s", e)
        return f"Pool chemistry nudge failed: {e}" if notify_empty else ""

    await _save_event(
        nudge_type="pool_check",
        unique_key=unique_key,
        status="created",
        task_id=task_id,
        payload={
            "reasons": reasons,
            "days_since": days_since,
            "out_of_range": out_of_range,
        },
    )

    return (
        "🏊 Pool Nudge — created Todoist task: "
        f"{task_content} (due: {due_string}).\n"
        f"Reason: {'; '.join(reasons)}"
    )
