#!/usr/bin/env python3
"""Conservative inbox cleanup scanner for Jarvis.

Dry-run is the default. Write mode is intentionally narrow and requires both
--apply and --confirm APPLY_INBOX_CLEANUP.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HIMALAYA_BIN = Path(os.getenv("HIMALAYA_BIN", "/opt/data/bin/himalaya"))
HIMALAYA_CONFIG = Path(os.getenv("HIMALAYA_CONFIG", "/opt/data/himalaya/config.toml"))
RULES_PATH = Path(os.getenv("INBOX_CLEANER_RULES", "/opt/data/jarvis/inbox_cleanup_rules.json"))
GMAIL_QUERY = os.getenv("INBOX_CLEANER_GMAIL_QUERY", "newer_than:2d")
CONFIRM_TEXT = "APPLY_INBOX_CLEANUP"
DEFAULT_ICLOUD_FOLDERS = ["INBOX", "Junk", "Action", "Follow Up"]

PERSONAL_TERMS = re.compile(
    r"\b("
    r"school|teacher|coach|tryout|team|family|doctor|dentist|appointment|"
    r"reservation|confirmation|flight|hotel|vehicle|pass|permit|bank|tax|"
    r"invoice|payment|receipt|refund|insurance|legal|deadline|action required|"
    r"security alert|sign-in|password|account"
    r")\b",
    re.IGNORECASE,
)
ACTION_TERMS = re.compile(
    r"\b("
    r"action required|verify|sign|complete|respond|reply|approval|approve|"
    r"deadline|due|overdue|appointment|reservation|confirmation|insurance|"
    r"school|teacher|coach|tryout|team|doctor|dentist|bank|tax|legal"
    r")\b",
    re.IGNORECASE,
)
FOLLOW_UP_TERMS = re.compile(r"\b(follow up|following up|checking in|reminder|waiting|next step)\b", re.IGNORECASE)
NEWSLETTER_TERMS = re.compile(
    r"\b(newsletter|digest|today's paper|breaking news|daily briefing|roundup|read more|trending posts)\b",
    re.IGNORECASE,
)
PROMO_TERMS = re.compile(
    r"\b("
    r"sale|deal|coupon|promo|promotion|offer|limited time|shop now|cart|"
    r"rewards|points|clearance|free shipping|exclusive"
    r")\b",
    re.IGNORECASE,
)
RECEIPT_TERMS = re.compile(
    r"\b(receipt|order|shipped|delivered|invoice|billing|payment|renewal|subscription)\b",
    re.IGNORECASE,
)
NOREPLY_TERMS = re.compile(r"\b(no-?reply|donotreply|mailer|notifications?)\b", re.IGNORECASE)

SAFE_ARCHIVE_SENDERS = {
    "breakingnews@nytimes.com",
    "access@interactive.wsj.com",
    "store-news@amazon.com",
    "cooking-recommendations@nytimes.com",
    "hello@mail.grammarly.com",
    "email@promotion.bedbathandbeyond.com",
    "reply@is.email.nextdoor.com",
}

DEFAULT_RULES = {
    "icloud_scan_folders": DEFAULT_ICLOUD_FOLDERS,
    "icloud_archive_folder": "Archive",
    "icloud_action_folder": "Action",
    "icloud_follow_up_folder": "Follow Up",
    "gmail_query": GMAIL_QUERY,
    "safe_archive_senders": sorted(SAFE_ARCHIVE_SENDERS),
    "never_touch_senders": [
        "support@whoop.com",
        "reservations@res-marriott.com",
        "noreply@email.apple.com",
    ],
    "auto_apply_actions": ["archive"],
    "max_apply": 25,
}


@dataclass
class Candidate:
    source: str
    folder: str
    message_id: str
    sender: str
    subject: str
    date: str
    bucket: str
    proposed_action: str
    target: str
    reason: str
    risk: str
    applied: bool = False
    apply_error: str | None = None


def normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sender_email(sender: str) -> str:
    match = re.search(r"<([^>]+)>", sender or "")
    raw = match.group(1) if match else sender
    return raw.strip().strip('"').lower()


def load_rules() -> dict[str, Any]:
    rules = dict(DEFAULT_RULES)
    if RULES_PATH.exists():
        try:
            loaded = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                rules.update(loaded)
        except Exception:
            pass
    rules["safe_archive_senders"] = sorted(set(rules.get("safe_archive_senders", [])))
    rules["never_touch_senders"] = sorted(set(rules.get("never_touch_senders", [])))
    return rules


def classify(sender: str, subject: str, snippet: str = "", rules: dict[str, Any] | None = None) -> tuple[str, str, str, str, str]:
    rules = rules or load_rules()
    text = f"{sender} {subject} {snippet}"
    email_addr = sender_email(sender)
    if email_addr in rules.get("never_touch_senders", []):
        return "keep", "none", "", "sender is on never-touch list", "high"
    if PERSONAL_TERMS.search(text):
        if FOLLOW_UP_TERMS.search(text):
            return "follow_up", "move", rules.get("icloud_follow_up_folder", "Follow Up"), "follow-up/actionable terms present", "medium"
        if ACTION_TERMS.search(text):
            return "action", "move", rules.get("icloud_action_folder", "Action"), "actionable personal/admin terms present", "medium"
        return "keep", "none", "", "personal/actionable terms present", "high"
    if email_addr in rules.get("safe_archive_senders", []):
        return "newsletter", "archive", rules.get("icloud_archive_folder", "Archive"), "known low-risk news/promotional sender", "low"
    if NEWSLETTER_TERMS.search(text):
        return "newsletter", "review", "", "newsletter-like but sender is not allowlisted", "medium"
    if PROMO_TERMS.search(text) or NOREPLY_TERMS.search(sender):
        return "noise", "review", "", "promotional or automated pattern", "medium"
    if RECEIPT_TERMS.search(text):
        return "receipt", "keep", "", "receipt or transaction record", "medium"
    return "review", "none", "", "not enough signal for automation", "medium"


def himalaya_cmd(*args: str) -> list[str]:
    # Himalaya accepts account/config/output on subcommands, after the command name.
    return [
        str(HIMALAYA_BIN),
        *args,
        "--config",
        str(HIMALAYA_CONFIG),
        "--account",
        "icloud",
        "--output",
        "json",
    ]


def himalaya_plain_cmd(*args: str) -> list[str]:
    return [str(HIMALAYA_BIN), *args, "--config", str(HIMALAYA_CONFIG), "--output", "plain"]


def himalaya_move_cmd(source_folder: str, target_folder: str, message_id: str) -> list[str]:
    return [
        str(HIMALAYA_BIN),
        "message",
        "move",
        "--folder",
        source_folder,
        "--config",
        str(HIMALAYA_CONFIG),
        "--account",
        "icloud",
        "--output",
        "json",
        target_folder,
        message_id,
    ]


def run_json(cmd: list[str], timeout: int = 60) -> Any:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    text = proc.stdout.strip()
    return json.loads(text) if text else None


def run_text(cmd: list[str], timeout: int = 60) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())
    return proc.stdout.strip()


def list_icloud_folders() -> list[str]:
    if not HIMALAYA_BIN.exists() or not HIMALAYA_CONFIG.exists():
        return []
    payload = run_json(himalaya_cmd("folder", "list"))
    if isinstance(payload, list):
        return [str(row.get("name", "")) for row in payload if isinstance(row, dict) and row.get("name")]
    return []


def account_doctor() -> dict[str, Any]:
    if not HIMALAYA_BIN.exists() or not HIMALAYA_CONFIG.exists():
        return {"ok": False, "error": "Himalaya is not installed or configured"}
    try:
        output = run_text(
            [str(HIMALAYA_BIN), "account", "doctor", "icloud", "--config", str(HIMALAYA_CONFIG), "--output", "plain"],
            timeout=90,
        )
        lowered = output.lower()
        return {"ok": "error" not in lowered and "failed" not in lowered, "output": output[-2000:]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def flatten_envelopes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("envelopes", "messages", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def envelope_field(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if isinstance(value, dict):
            for child in ("addr", "address", "email", "name"):
                if value.get(child):
                    return str(value[child])
        if isinstance(value, list) and value:
            return normalize(", ".join(str(v) for v in value))
        if value is not None:
            return normalize(str(value))
    return ""


def scan_icloud(limit: int, folders: list[str], rules: dict[str, Any]) -> list[Candidate]:
    if not HIMALAYA_BIN.exists() or not HIMALAYA_CONFIG.exists():
        return []
    existing = set(list_icloud_folders())
    candidates: list[Candidate] = []
    for folder in folders:
        if existing and folder not in existing:
            continue
        payload = run_json(himalaya_cmd("envelope", "list", "--folder", folder, "--page-size", str(limit)))
        rows = flatten_envelopes(payload)
        for row in rows[:limit]:
            msg_id = envelope_field(row, "id", "message_id", "uid") or envelope_field(row, "idx")
            sender = envelope_field(row, "from", "sender")
            subject = envelope_field(row, "subject") or "(no subject)"
            date = envelope_field(row, "date", "received_at")
            bucket, action, target, reason, risk = classify(sender, subject, rules=rules)
            if folder != "INBOX" and action in ("archive", "move"):
                action, target, reason, risk = "none", "", f"already outside INBOX in {folder}; " + reason, "medium"
            candidates.append(
                Candidate(
                    source="iCloud",
                    folder=folder,
                    message_id=msg_id,
                    sender=sender,
                    subject=subject,
                    date=date,
                    bucket=bucket,
                    proposed_action=action,
                    target=target,
                    reason=reason,
                    risk=risk,
                )
            )
    return candidates


def google_service(service: str, version: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as google_build

    token_file = os.getenv("GOOGLE_TOKEN_FILE", "google_token.json")
    if not os.path.exists(token_file):
        raise RuntimeError(f"Missing GOOGLE_TOKEN_FILE: {token_file}")
    creds = Credentials.from_authorized_user_file(token_file)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise RuntimeError("Google token is invalid")
    return google_build(service, version, credentials=creds, cache_discovery=False)


def scan_gmail(limit: int, rules: dict[str, Any]) -> list[Candidate]:
    svc = google_service("gmail", "v1")
    result = svc.users().messages().list(userId="me", q=rules.get("gmail_query", GMAIL_QUERY), maxResults=limit).execute()
    candidates: list[Candidate] = []
    for row in result.get("messages", []):
        detail = svc.users().messages().get(
            userId="me",
            id=row["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h.get("name", ""): h.get("value", "") for h in detail.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")
        bucket, action, target, reason, risk = classify(sender, subject, detail.get("snippet", ""), rules)
        if action == "move":
            action, target, reason, risk = "review", "", "Gmail action/follow-up moves need label mapping; " + reason, "medium"
        candidates.append(
            Candidate(
                source="Gmail",
                folder="INBOX",
                message_id=row["id"],
                sender=sender,
                subject=subject,
                date=headers.get("Date", ""),
                bucket=bucket,
                proposed_action=action,
                target="Archive" if action == "archive" else target,
                reason=reason,
                risk=risk,
            )
        )
    return candidates


def apply_candidate(candidate: Candidate, rules: dict[str, Any]) -> Candidate:
    if candidate.proposed_action not in set(rules.get("auto_apply_actions", [])):
        return candidate
    if candidate.risk != "low" or candidate.proposed_action != "archive":
        return candidate
    try:
        if candidate.source == "iCloud":
            target = candidate.target or rules.get("icloud_archive_folder", "Archive")
            run_json(himalaya_move_cmd(candidate.folder, target, candidate.message_id), timeout=60)
        elif candidate.source == "Gmail":
            svc = google_service("gmail", "v1")
            svc.users().messages().modify(
                userId="me",
                id=candidate.message_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
        else:
            return candidate
        candidate.applied = True
    except Exception as exc:
        candidate.apply_error = str(exc)
    return candidate


def summarize(candidates: list[Candidate], applied: bool) -> dict[str, Any]:
    counts = Counter(c.bucket for c in candidates)
    actions = Counter(c.proposed_action for c in candidates)
    safe = [c for c in candidates if c.risk == "low" and c.proposed_action == "archive"]
    review = [c for c in candidates if c.bucket == "review" or c.proposed_action == "review"]
    applied_items = [c for c in candidates if c.applied]
    folder_counts = Counter(f"{c.source}:{c.folder}" for c in candidates)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if applied else "dry-run",
        "total": len(candidates),
        "counts": dict(counts),
        "proposed_actions": dict(actions),
        "folder_counts": dict(folder_counts),
        "safe_archive_count": len(safe),
        "review_count": len(review),
        "applied_count": len(applied_items),
        "candidates": [asdict(c) for c in candidates],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Inbox Cleaner Dry Run" if summary["mode"] == "dry-run" else "# Inbox Cleaner Apply Report",
        f"Generated: {summary['generated_at']}",
        f"Total scanned: {summary['total']}",
        f"Buckets: {json.dumps(summary['counts'], sort_keys=True)}",
        f"Folders: {json.dumps(summary.get('folder_counts', {}), sort_keys=True)}",
        f"Safe archive candidates: {summary['safe_archive_count']}",
        f"Needs review: {summary['review_count']}",
        f"Applied: {summary.get('applied_count', 0)}",
        "",
        "| Proposed | Source | Folder | Sender | Subject | Reason | Risk |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in summary["candidates"][:30]:
        sender = normalize(c["sender"])[:48]
        subject = normalize(c["subject"])[:80]
        lines.append(
            f"| {c['bucket']} / {c['proposed_action']} | {c['source']} | {c.get('folder', '')} | {sender} | "
            f"{subject} | {c['reason']} | {c['risk']} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["scan", "folders", "doctor", "rules"], default="scan")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--folder-limit", type=int, default=10)
    parser.add_argument("--folders", default="")
    parser.add_argument("--source", choices=["all", "icloud", "gmail"], default="all")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.apply and args.confirm != CONFIRM_TEXT:
        parser.error(f"--apply requires --confirm {CONFIRM_TEXT}")

    rules = load_rules()
    if args.command == "rules":
        print(json.dumps(rules, indent=2, sort_keys=True))
        return 0
    if args.command == "folders":
        folders = list_icloud_folders()
        print(json.dumps({"icloud_folders": folders}, indent=2) if args.format == "json" else "\n".join(folders))
        return 0
    if args.command == "doctor":
        result = account_doctor()
        print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else result.get("output") or result.get("error"))
        return 0 if result.get("ok") else 2

    candidates: list[Candidate] = []
    errors: list[str] = []
    if args.source in ("all", "icloud"):
        try:
            folders = [f.strip() for f in args.folders.split(",") if f.strip()] or list(rules.get("icloud_scan_folders", DEFAULT_ICLOUD_FOLDERS))
            candidates.extend(scan_icloud(args.folder_limit, folders, rules))
        except Exception as exc:
            errors.append(f"iCloud: {exc}")
    if args.source in ("all", "gmail"):
        try:
            candidates.extend(scan_gmail(args.limit, rules))
        except Exception as exc:
            errors.append(f"Gmail: {exc}")

    if args.apply:
        max_apply = int(rules.get("max_apply", 25))
        applied = 0
        updated: list[Candidate] = []
        for candidate in candidates:
            if applied < max_apply and candidate.risk == "low" and candidate.proposed_action == "archive":
                candidate = apply_candidate(candidate, rules)
                if candidate.applied:
                    applied += 1
            updated.append(candidate)
        candidates = updated

    summary = summarize(candidates, args.apply)
    summary["errors"] = errors
    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_markdown(summary))
        if errors:
            print("\nWarnings:")
            for error in errors:
                print(f"- {error}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
