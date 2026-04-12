"""
Finance Inbox processor.

Workflow:
- Ensure Google Drive folders exist: "Finance Inbox" and child "Archive"
- Scan new files in the inbox
- Extract a normalized finance packet with Claude
- Store the packet in SQLite
- Save a review note to Notion when available
- Move successful files into Archive
"""
import asyncio
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL_REPORT
from google_services import (
    download_drive_file,
    ensure_drive_folder,
    list_drive_folder_files,
    move_drive_file,
)
from memory import db_connect

logger = logging.getLogger("jarvis.finance_inbox")

FINANCE_INBOX_NAME = os.getenv("FINANCE_DRIVE_INBOX_NAME", "Finance Inbox")
FINANCE_ARCHIVE_NAME = os.getenv("FINANCE_DRIVE_ARCHIVE_NAME", "Archive")

SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
SUPPORTED_TEXT_MIME_TYPES = {
    "text/plain",
    "text/csv",
}

FINANCE_PACKET_SYSTEM_PROMPT = """You extract structured data from personal financial documents.

Return exactly one valid JSON object with these keys:
- document_type
- institution
- account_last4
- statement_date
- due_date
- amount_due
- statement_balance
- minimum_due
- autopay_status
- usage_period
- key_points
- document_metadata
- anomalies
- summary
- needs_review
- confidence

Rules:
- document_type should be one of: utility_bill, credit_card_statement, bank_statement, invoice,
  insurance, tax, receipt, medical_bill, mortgage, loan, other
- account_last4 should be only the final 4 digits when visible, otherwise ""
- Dates must be YYYY-MM-DD when clear, otherwise ""
- amount_due, statement_balance, and minimum_due should be numbers when clear, otherwise null
- autopay_status should be yes, no, or unknown
- key_points should be a JSON array of the most important review items from the document
- document_metadata should be a JSON object containing useful extracted context beyond balances
- For utility bills, document_metadata should include any clear fields like gallons_used,
  service_period, usage_days, meter_number, rate_details, previous_balance, current_charges,
  total_due, account_number_masked, service_address, read_date, or due_date_context
- For other documents, document_metadata should still capture any meaningful structured context
- anomalies should be a JSON array of short strings
- summary should be concise and high-signal
- needs_review should usually be true unless the document is extremely straightforward
- confidence should be a number from 0 to 1

If a field is missing, return an empty string, null, or unknown as appropriate.
Return JSON only. No markdown, no commentary."""


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


async def ensure_finance_drive_folders() -> dict:
    inbox = await ensure_drive_folder({"name": FINANCE_INBOX_NAME})
    archive = await ensure_drive_folder(
        {"name": FINANCE_ARCHIVE_NAME, "parent_id": inbox["id"]}
    )
    return {"inbox": inbox, "archive": archive}


def _extract_json_object(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _coerce_float(value):
    if value in ("", None):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _normalize_packet(packet: dict, file_meta: dict) -> dict:
    key_points = packet.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = [str(key_points)]
    document_metadata = packet.get("document_metadata") or {}
    if not isinstance(document_metadata, dict):
        document_metadata = {"raw_value": document_metadata}
    return {
        "document_type": packet.get("document_type") or "other",
        "institution": packet.get("institution") or "",
        "account_last4": str(packet.get("account_last4") or ""),
        "statement_date": packet.get("statement_date") or "",
        "due_date": packet.get("due_date") or "",
        "amount_due": _coerce_float(packet.get("amount_due")),
        "statement_balance": _coerce_float(packet.get("statement_balance")),
        "minimum_due": _coerce_float(packet.get("minimum_due")),
        "autopay_status": (packet.get("autopay_status") or "unknown").lower(),
        "usage_period": packet.get("usage_period") or "",
        "key_points": [str(item) for item in key_points if str(item).strip()],
        "document_metadata": document_metadata,
        "anomalies": packet.get("anomalies") or [],
        "summary": packet.get("summary") or f"Processed {file_meta.get('name', 'document')}",
        "needs_review": bool(packet.get("needs_review", True)),
        "confidence": max(0.0, min(1.0, float(packet.get("confidence", 0) or 0))),
    }


def _call_anthropic_messages_api(payload: dict) -> str:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API error {exc.code}: {detail[:500]}") from exc

    blocks = data.get("content") or []
    text_blocks = [block.get("text", "") for block in blocks if block.get("type") == "text"]
    return "\n".join(t for t in text_blocks if t).strip()


def _build_content_blocks(file_meta: dict, data_base64: str) -> list[dict]:
    mime = file_meta.get("download_mimeType") or file_meta.get("mimeType") or ""
    filename = file_meta.get("name", "document")
    instruction = {
        "type": "text",
        "text": (
            f"File name: {filename}\n"
            f"MIME type: {mime}\n"
            "Extract the finance packet from this document."
        ),
    }

    if mime == "application/pdf":
        return [
            instruction,
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data_base64,
                },
            },
        ]

    if mime in SUPPORTED_IMAGE_MIME_TYPES:
        return [
            instruction,
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": data_base64,
                },
            },
        ]

    if mime in SUPPORTED_TEXT_MIME_TYPES:
        decoded = base64.b64decode(data_base64).decode("utf-8", errors="replace")
        return [
            {
                "type": "text",
                "text": (
                    f"File name: {filename}\n"
                    f"MIME type: {mime}\n"
                    "Extract the finance packet from this document text:\n\n"
                    f"{decoded[:18000]}"
                ),
            }
        ]

    raise ValueError(f"Unsupported file type for finance processing: {mime}")


async def extract_finance_packet(file_meta: dict) -> dict:
    content_blocks = _build_content_blocks(file_meta, file_meta["data_base64"])
    payload = {
        "model": CLAUDE_MODEL_REPORT,
        "max_tokens": 1400,
        "system": FINANCE_PACKET_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": content_blocks,
            }
        ],
    }
    raw = await asyncio.to_thread(_call_anthropic_messages_api, payload)
    packet = _extract_json_object(raw)
    normalized = _normalize_packet(packet, file_meta)
    normalized["_raw_json"] = packet
    return normalized


async def _save_finance_document_record(
    file_meta: dict,
    packet: dict,
    status: str,
    notion_title: str = "",
    archived_at: str | None = None,
    review_status: str = "open",
    reviewed_at: str | None = None,
    review_notes: str = "",
):
    now = _utcnow()
    async with db_connect() as db:
        await db.execute(
            """
            INSERT INTO finance_documents (
                source_file_id, source_file_name, source_mime_type, source_modified_time,
                source_web_view_link, status, document_type, institution, account_last4,
                statement_date, due_date, amount_due, statement_balance, minimum_due,
                autopay_status, usage_period, summary, key_points_json, document_metadata_json,
                anomalies_json, raw_packet_json,
                confidence, needs_review, review_status, reviewed_at, review_notes,
                notion_title, processed_at, archived_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_file_id) DO UPDATE SET
                source_file_name=excluded.source_file_name,
                source_mime_type=excluded.source_mime_type,
                source_modified_time=excluded.source_modified_time,
                source_web_view_link=excluded.source_web_view_link,
                status=excluded.status,
                document_type=excluded.document_type,
                institution=excluded.institution,
                account_last4=excluded.account_last4,
                statement_date=excluded.statement_date,
                due_date=excluded.due_date,
                amount_due=excluded.amount_due,
                statement_balance=excluded.statement_balance,
                minimum_due=excluded.minimum_due,
                autopay_status=excluded.autopay_status,
                usage_period=excluded.usage_period,
                summary=excluded.summary,
                key_points_json=excluded.key_points_json,
                document_metadata_json=excluded.document_metadata_json,
                anomalies_json=excluded.anomalies_json,
                raw_packet_json=excluded.raw_packet_json,
                confidence=excluded.confidence,
                needs_review=excluded.needs_review,
                review_status=excluded.review_status,
                reviewed_at=excluded.reviewed_at,
                review_notes=excluded.review_notes,
                notion_title=excluded.notion_title,
                processed_at=excluded.processed_at,
                archived_at=excluded.archived_at,
                updated_at=excluded.updated_at
            """,
            (
                file_meta["id"],
                file_meta.get("name", ""),
                file_meta.get("download_mimeType") or file_meta.get("mimeType", ""),
                file_meta.get("modifiedTime", ""),
                file_meta.get("webViewLink", ""),
                status,
                packet.get("document_type", "other"),
                packet.get("institution", ""),
                packet.get("account_last4", ""),
                packet.get("statement_date", ""),
                packet.get("due_date", ""),
                packet.get("amount_due"),
                packet.get("statement_balance"),
                packet.get("minimum_due"),
                packet.get("autopay_status", "unknown"),
                packet.get("usage_period", ""),
                packet.get("summary", ""),
                json.dumps(packet.get("key_points", [])),
                json.dumps(packet.get("document_metadata", {})),
                json.dumps(packet.get("anomalies", [])),
                json.dumps(packet.get("_raw_json", packet)),
                packet.get("confidence", 0),
                1 if packet.get("needs_review", True) else 0,
                review_status,
                reviewed_at,
                review_notes,
                notion_title,
                now,
                archived_at,
                now,
            ),
        )
        await db.commit()


def _finance_review_title(file_meta: dict, packet: dict) -> str:
    institution = packet.get("institution") or "Unknown Institution"
    date_label = packet.get("statement_date") or packet.get("due_date") or datetime.utcnow().date().isoformat()
    return f"Finance Review — {institution} — {date_label}"


def _finance_review_body(file_meta: dict, packet: dict) -> str:
    lines = [
        f"File: {file_meta.get('name', '')}",
        f"Drive link: {file_meta.get('webViewLink', '')}",
        f"Document type: {packet.get('document_type', 'other')}",
        f"Institution: {packet.get('institution', '')}",
        f"Account last4: {packet.get('account_last4', '')}",
        f"Statement date: {packet.get('statement_date', '')}",
        f"Due date: {packet.get('due_date', '')}",
        f"Amount due: {packet.get('amount_due', '')}",
        f"Statement balance: {packet.get('statement_balance', '')}",
        f"Minimum due: {packet.get('minimum_due', '')}",
        f"Autopay: {packet.get('autopay_status', 'unknown')}",
        f"Usage period: {packet.get('usage_period', '')}",
        f"Needs review: {'yes' if packet.get('needs_review', True) else 'no'}",
        f"Confidence: {packet.get('confidence', 0):.2f}",
        "",
        "Summary:",
        packet.get("summary", ""),
    ]
    key_points = packet.get("key_points", []) or []
    if key_points:
        lines.append("")
        lines.append("Key points:")
        for item in key_points:
            lines.append(f"- {item}")
    document_metadata = packet.get("document_metadata", {}) or {}
    if document_metadata:
        lines.append("")
        lines.append("Document metadata:")
        for key, value in sorted(document_metadata.items()):
            if value in ("", None, [], {}):
                continue
            lines.append(f"- {key}: {value}")
    anomalies = packet.get("anomalies", []) or []
    if anomalies:
        lines.append("")
        lines.append("Anomalies:")
        for item in anomalies:
            lines.append(f"- {item}")
    return "\n".join(lines)


async def _save_review_note(file_meta: dict, packet: dict) -> str:
    if not os.getenv("NOTION_TOKEN", "").strip() or not os.getenv("NOTION_NOTES_DB_ID", "").strip():
        return ""

    from notion_services import save_notion_note

    title = _finance_review_title(file_meta, packet)
    body = _finance_review_body(file_meta, packet)
    await save_notion_note({"title": title, "content": body, "category": "note"})
    return title


async def process_finance_inbox(limit: int = 10) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    folders = await ensure_finance_drive_folders()
    inbox_files = await list_drive_folder_files(
        {"folder_id": folders["inbox"]["id"], "max_results": max(1, int(limit))}
    )

    if not inbox_files:
        return (
            f"Finance inbox is ready.\n"
            f"Inbox: {FINANCE_INBOX_NAME}\n"
            f"Archive: {FINANCE_ARCHIVE_NAME}\n"
            "No new files found."
        )

    processed: list[str] = []
    archived: list[str] = []
    failed: list[str] = []

    for file_summary in inbox_files:
        file_name = file_summary.get("name", "(unnamed)")
        try:
            file_meta = await download_drive_file({"file_id": file_summary["id"]})
            packet = await extract_finance_packet(file_meta)
            notion_title = await _save_review_note(file_meta, packet)
            await _save_finance_document_record(file_meta, packet, status="processed", notion_title=notion_title)
            await move_drive_file(
                {
                    "file_id": file_meta["id"],
                    "destination_folder_id": folders["archive"]["id"],
                }
            )
            archived_at = _utcnow()
            await _save_finance_document_record(
                file_meta,
                packet,
                status="archived",
                notion_title=notion_title,
                archived_at=archived_at,
                review_status="open",
            )
            processed.append(f"{file_name} -> {packet.get('document_type', 'other')} / {packet.get('institution', 'Unknown')}")
            archived.append(file_name)
        except Exception as exc:
            logger.error("Finance inbox processing failed for %s: %s", file_name, exc)
            failed.append(f"{file_name}: {exc}")

    lines = [
        f"Finance inbox run complete.",
        f"Processed: {len(processed)}",
        f"Archived: {len(archived)}",
        f"Failed: {len(failed)}",
    ]
    open_count = await get_open_financial_item_count()
    lines.append(f"Open financial items: {open_count}")
    if processed:
        lines.append("")
        lines.append("Processed documents:")
        for item in processed[:8]:
            lines.append(f"- {item}")
        if len(processed) > 8:
            lines.append(f"- ... and {len(processed) - 8} more")
    if failed:
        lines.append("")
        lines.append("Failures:")
        for item in failed[:5]:
            lines.append(f"- {item}")
        if len(failed) > 5:
            lines.append(f"- ... and {len(failed) - 5} more")
    return "\n".join(lines)


async def get_open_financial_item_count() -> int:
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM finance_documents WHERE review_status = 'open'"
        )
        row = await cur.fetchone()
    return int(row[0] if row else 0)


async def list_open_financial_items(limit: int = 20) -> str:
    async with db_connect() as db:
        cur = await db.execute(
            """
            SELECT id, source_file_name, document_type, institution, due_date,
                   amount_due, summary, key_points_json, processed_at
            FROM finance_documents
            WHERE review_status = 'open'
            ORDER BY
                CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
                due_date ASC,
                processed_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        )
        rows = await cur.fetchall()

    if not rows:
        return "No open financial items."

    lines = [f"Open financial items ({len(rows)} shown):"]
    for row in rows:
        item_id, file_name, document_type, institution, due_date, amount_due, summary, key_points_json, _ = row
        amount_text = f"${amount_due:,.2f}" if amount_due is not None else "amount unknown"
        due_text = due_date or "no due date"
        lines.append(
            f"[{item_id}] {institution or 'Unknown'} — {document_type or 'other'} — {amount_text} — due {due_text}"
        )
        lines.append(f"  {summary}")
        try:
            key_points = json.loads(key_points_json) if key_points_json else []
        except Exception:
            key_points = []
        if key_points:
            lines.append(f"  Key point: {key_points[0]}")
        lines.append(f"  File: {file_name}")

    return "\n".join(lines)


async def get_finance_digest(days_ahead: int = 10, recent_days: int = 7, limit: int = 20) -> str:
    async with db_connect() as db:
        cur = await db.execute(
            """
            SELECT id, source_file_name, document_type, institution, due_date, amount_due,
                   summary, key_points_json, anomalies_json, processed_at, review_status
            FROM finance_documents
            WHERE review_status = 'open'
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        )
        rows = await cur.fetchall()

    if not rows:
        return "Finance digest\n\nNo open financial items."

    today = date.today()
    due_soon_cutoff = today + timedelta(days=max(1, int(days_ahead)))
    recent_cutoff = datetime.utcnow() - timedelta(days=max(1, int(recent_days)))

    def _parse_due_date(raw: str):
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _parse_processed_at(raw: str):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    past_due = []
    due_soon = []
    newly_processed = []
    anomaly_items = []

    for row in rows:
        item_id, file_name, document_type, institution, due_date_raw, amount_due, summary, key_points_json, anomalies_json, processed_at_raw, _ = row
        item = {
            "id": item_id,
            "file_name": file_name,
            "document_type": document_type or "other",
            "institution": institution or "Unknown",
            "due_date": _parse_due_date(due_date_raw),
            "due_date_raw": due_date_raw or "",
            "amount_due": amount_due,
            "summary": summary or "",
            "processed_at": _parse_processed_at(processed_at_raw),
            "key_points": json.loads(key_points_json) if key_points_json else [],
            "anomalies": json.loads(anomalies_json) if anomalies_json else [],
        }
        if item["due_date"] and item["due_date"] < today:
            past_due.append(item)
        elif item["due_date"] and item["due_date"] <= due_soon_cutoff:
            due_soon.append(item)
        if item["processed_at"] and item["processed_at"] >= recent_cutoff:
            newly_processed.append(item)
        if item["anomalies"] or item["key_points"]:
            anomaly_items.append(item)

    def _line(item: dict) -> str:
        amount_text = f"${item['amount_due']:,.2f}" if item["amount_due"] is not None else "amount unknown"
        due_text = item["due_date_raw"] or "no due date"
        return f"[{item['id']}] {item['institution']} — {amount_text} — due {due_text}"

    lines = [
        "Finance digest",
        "",
        f"Open items: {len(rows)}",
        f"Past due: {len(past_due)}",
        f"Due in next {days_ahead} days: {len(due_soon)}",
        f"Newly processed in last {recent_days} days: {len(newly_processed)}",
    ]

    if past_due:
        lines.append("")
        lines.append("Past due:")
        for item in sorted(past_due, key=lambda x: x["due_date"] or today)[:5]:
            lines.append(f"- {_line(item)}")
            if item["key_points"]:
                lines.append(f"  {item['key_points'][0]}")

    if due_soon:
        lines.append("")
        lines.append(f"Due soon ({days_ahead} days):")
        for item in sorted(due_soon, key=lambda x: x["due_date"] or due_soon_cutoff)[:5]:
            lines.append(f"- {_line(item)}")
            if item["summary"]:
                lines.append(f"  {item['summary']}")

    if newly_processed:
        lines.append("")
        lines.append("New financial intel:")
        for item in newly_processed[:5]:
            lines.append(f"- {_line(item)}")

    highlighted = [item for item in anomaly_items if item["anomalies"] or item["key_points"]]
    if highlighted:
        lines.append("")
        lines.append("Highlights:")
        for item in highlighted[:5]:
            detail = ""
            if item["anomalies"]:
                detail = item["anomalies"][0]
            elif item["key_points"]:
                detail = item["key_points"][0]
            lines.append(f"- [{item['id']}] {item['institution']}: {detail}")

    lines.append("")
    lines.append("Use /finance_open for the full queue.")
    lines.append("Use /finance_done <id> once reviewed.")
    return "\n".join(lines)


async def mark_financial_item_done(item_id: int, note: str = "") -> str:
    reviewed_at = _utcnow()
    async with db_connect() as db:
        cur = await db.execute(
            """
            SELECT institution, document_type, amount_due, due_date
            FROM finance_documents
            WHERE id = ?
            """,
            (item_id,),
        )
        row = await cur.fetchone()
        if not row:
            return f"No financial item found for id {item_id}."

        await db.execute(
            """
            UPDATE finance_documents
            SET review_status = 'done',
                reviewed_at = ?,
                review_notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (reviewed_at, note.strip(), reviewed_at, item_id),
        )
        await db.commit()

    institution, document_type, amount_due, due_date = row
    amount_text = f"${amount_due:,.2f}" if amount_due is not None else "amount unknown"
    due_text = due_date or "no due date"
    note_text = f"\nNote: {note.strip()}" if note.strip() else ""
    return (
        f"Marked financial item {item_id} done.\n"
        f"{institution or 'Unknown'} — {document_type or 'other'} — {amount_text} — due {due_text}"
        f"{note_text}"
    )
