"""
Google API services: Calendar, Gmail, Drive.

Auth: OAuth2 with offline access. token.json holds user tokens and auto-refreshes.
Setup: run scripts/authorize_google.py locally once, then copy token.json to server.
"""
import os
import asyncio
import base64
import io
import json
import logging
import random
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("jarvis.google")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
]

CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "google_token.json")
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _write_secure_file(path: str, content: str):
    with open(path, "w") as f:
        f.write(content)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _should_retry_google(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
    if status in TRANSIENT_STATUSES:
        return True
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "tempor", "rate limit", "backend error"))


def _execute_with_retry(request, op_name: str, attempts: int = 4):
    delay = 1.0
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as e:
            if attempt == attempts - 1 or not _should_retry_google(e):
                raise
            logger.warning(
                "Retrying Google op %s (%s/%s) after error: %s",
                op_name,
                attempt + 1,
                attempts,
                e,
            )
            time.sleep(delay + random.random() * 0.3)
            delay = min(delay * 2, 8.0)


def _get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(
            f"Google token not found ({TOKEN_FILE}). "
            "Run scripts/authorize_google.py locally first, then copy token.json to the server."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_secure_file(TOKEN_FILE, creds.to_json())
        else:
            raise RuntimeError("Google token invalid. Re-run scripts/authorize_google.py.")
    return creds


def _read_token_scopes() -> set[str]:
    if not os.path.exists(TOKEN_FILE):
        return set()
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except Exception:
        return set()

    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return {s for s in scopes if s}


def _require_token_scopes(required_scopes: list[str]):
    token_scopes = _read_token_scopes()
    missing = [scope for scope in required_scopes if scope not in token_scopes]
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            "Google token is missing required scopes: "
            f"{missing_list}. Re-run scripts/authorize_google.py locally "
            "and copy the updated google_token.json to the server."
        )


def _build(service: str, version: str):
    from googleapiclient.discovery import build
    return build(service, version, credentials=_get_credentials())


# ── Calendar ─────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> date:
    today = date.today()
    mapping = {"today": today, "tomorrow": today + timedelta(days=1), "": today}
    if date_str.lower() in mapping:
        return mapping[date_str.lower()]
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return today


def _get_calendar_events_sync(date_str: str) -> str:
    target = _parse_date(date_str)
    start = datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=ET)
    end = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=ET)

    service = _build("calendar", "v3")
    result = _execute_with_retry(
        service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ),
        "calendar.events.list",
    )

    events = result.get("items", [])
    label = target.strftime("%A, %B %d")

    if not events:
        return f"No events on {label}."

    lines = [f"Calendar — {label}:"]
    for ev in events:
        s = ev["start"]
        if "dateTime" in s:
            dt = datetime.fromisoformat(s["dateTime"]).astimezone(ET)
            time_str = dt.strftime("%-I:%M %p")
        else:
            time_str = "All day"
        summary = ev.get("summary", "(No title)")
        location = f" @ {ev['location']}" if ev.get("location") else ""
        lines.append(f"  {time_str} — {summary}{location}")

    return "\n".join(lines)


async def get_calendar_events(input: dict) -> str:
    date_str = input.get("date", "today")
    return await asyncio.to_thread(_get_calendar_events_sync, date_str)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _get_emails_sync(query: str, max_results: int, include_body: bool) -> str:
    service = _build("gmail", "v1")
    result = _execute_with_retry(
        service.users().messages().list(userId="me", q=query, maxResults=max_results),
        "gmail.messages.list",
    )

    messages = result.get("messages", [])
    if not messages:
        return "No emails found."

    lines = []
    for msg in messages:
        fmt = "full" if include_body else "metadata"
        detail = _execute_with_retry(
            service.users().messages().get(
                userId="me",
                id=msg["id"],
                format=fmt,
                metadataHeaders=["From", "Subject", "Date"],
            ),
            "gmail.messages.get",
        )

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        snippet = detail.get("snippet", "")[:120]

        lines.append(f"ID: {msg['id']}")
        lines.append(f"From: {headers.get('From', '?')}")
        lines.append(f"Subject: {headers.get('Subject', '(No subject)')}")
        lines.append(f"Date: {headers.get('Date', '?')}")

        if include_body:
            body = _extract_body(detail["payload"])
            lines.append(f"Body:\n{body[:2000]}")
        else:
            lines.append(f"Preview: {snippet}")

        lines.append("")

    return "\n".join(lines).strip()


def _extract_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


def _get_recent_gmail_messages_sync(query: str, max_results: int, include_body: bool = True) -> list[dict]:
    """
    Return structured Gmail messages for internal automations.
    """
    service = _build("gmail", "v1")
    result = _execute_with_retry(
        service.users().messages().list(userId="me", q=query, maxResults=max_results),
        "gmail.messages.list.recent",
    )

    messages = result.get("messages", [])
    if not messages:
        return []

    items: list[dict] = []
    for msg in messages:
        fmt = "full" if include_body else "metadata"
        detail = _execute_with_retry(
            service.users().messages().get(
                userId="me",
                id=msg["id"],
                format=fmt,
                metadataHeaders=["From", "Subject", "Date", "To"],
            ),
            "gmail.messages.get.recent",
        )
        headers = {h["name"]: h["value"] for h in detail["payload"].get("headers", [])}
        body_text = _extract_body(detail["payload"]) if include_body else ""
        attachment_names: list[str] = []
        for part in detail["payload"].get("parts", []) or []:
            filename = (part or {}).get("filename", "")
            if filename:
                attachment_names.append(filename)

        items.append({
            "id": msg["id"],
            "account": "gmail",
            "from": headers.get("From", "?"),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", "(No subject)"),
            "date": headers.get("Date", ""),
            "snippet": detail.get("snippet", "")[:500],
            "body_text": body_text[:4000],
            "attachments": attachment_names,
        })
    return items


async def get_emails(input: dict) -> str:
    query = input.get("query", "is:unread")
    max_results = int(input.get("max_results", 5))
    include_body = bool(input.get("include_body", False))
    return await asyncio.to_thread(_get_emails_sync, query, max_results, include_body)


async def fetch_recent_gmail_messages(
    query: str = "in:inbox newer_than:3d",
    max_results: int = 25,
    include_body: bool = True,
) -> list[dict]:
    return await asyncio.to_thread(
        _get_recent_gmail_messages_sync,
        query,
        max_results,
        include_body,
    )


def _send_email_sync(to: str, subject: str, body: str) -> str:
    service = _build("gmail", "v1")
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    _execute_with_retry(
        service.users().messages().send(userId="me", body={"raw": raw}),
        "gmail.messages.send",
    )
    return f"Email sent to {to} — Subject: {subject}"


async def send_email(input: dict) -> str:
    return await asyncio.to_thread(
        _send_email_sync, input["to"], input["subject"], input["body"]
    )


def _gmail_modify_by_query_sync(
    query: str, add_labels: list[str], remove_labels: list[str], max_msgs: int = 25
) -> str:
    """Find messages matching a Gmail query and apply label modifications."""
    service = _build("gmail", "v1")
    result = _execute_with_retry(
        service.users().messages().list(userId="me", q=query, maxResults=max_msgs),
        "gmail.messages.list.modify",
    )
    messages = result.get("messages", [])
    if not messages:
        return f"No messages found for query: {query}"
    body: dict = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels
    for msg in messages:
        _execute_with_retry(
            service.users().messages().modify(userId="me", id=msg["id"], body=body),
            "gmail.messages.modify",
        )
    return f"Done — applied to {len(messages)} message(s) matching '{query}'"


async def archive_gmail_email(input: dict) -> str:
    """Archive Gmail messages matching a search query (remove from INBOX)."""
    query = input.get("query", "").strip()
    if not query:
        return "Error: query is required"
    return await asyncio.to_thread(
        _gmail_modify_by_query_sync, query, [], ["INBOX"]
    )


async def star_gmail_email(input: dict) -> str:
    """Star or un-star Gmail messages matching a search query."""
    query = input.get("query", "").strip()
    if not query:
        return "Error: query is required"
    starred = bool(input.get("starred", True))
    add = ["STARRED"] if starred else []
    remove = [] if starred else ["STARRED"]
    return await asyncio.to_thread(
        _gmail_modify_by_query_sync, query, add, remove
    )


async def mark_gmail_email_read(input: dict) -> str:
    """Mark Gmail messages as read or unread."""
    query = input.get("query", "").strip()
    if not query:
        return "Error: query is required"
    seen = bool(input.get("seen", True))
    add = [] if seen else ["UNREAD"]
    remove = ["UNREAD"] if seen else []
    return await asyncio.to_thread(
        _gmail_modify_by_query_sync, query, add, remove
    )


# ── Drive ─────────────────────────────────────────────────────────────────────

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _search_drive_sync(query: str, max_results: int) -> str:
    service = _build("drive", "v3")
    result = _execute_with_retry(
        service.files().list(
            q=query,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ),
        "drive.files.list",
    )

    files = result.get("files", [])
    if not files:
        return "No files found."

    lines = []
    for f in files:
        type_label = f["mimeType"].split(".")[-1].replace("apps-", "")
        modified = f.get("modifiedTime", "")[:10]
        lines.append(f"- {f['name']}  [{type_label}]  id:{f['id']}  modified:{modified}")

    return "\n".join(lines)


async def search_drive(input: dict) -> str:
    query = input.get("query", "")
    max_results = int(input.get("max_results", 10))
    return await asyncio.to_thread(_search_drive_sync, query, max_results)


def _read_drive_file_sync(file_id: str) -> str:
    service = _build("drive", "v3")
    meta = _execute_with_retry(
        service.files().get(fileId=file_id, fields="name,mimeType"),
        "drive.files.get_meta",
    )
    name = meta["name"]
    mime = meta["mimeType"]

    if mime == GOOGLE_DOC_MIME:
        content = _execute_with_retry(
            service.files().export(fileId=file_id, mimeType="text/plain"),
            "drive.files.export_doc",
        )
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        return f"--- {name} ---\n{text[:4000]}"

    if mime == GOOGLE_SHEET_MIME:
        content = _execute_with_retry(
            service.files().export(fileId=file_id, mimeType="text/csv"),
            "drive.files.export_sheet",
        )
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        return f"--- {name} (CSV) ---\n{text[:4000]}"

    return f"Cannot read file type: {mime}. Only Google Docs and Sheets are supported."


async def read_drive_file(input: dict) -> str:
    return await asyncio.to_thread(_read_drive_file_sync, input["file_id"])


def _drive_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ensure_drive_folder_sync(name: str, parent_id: str | None = None) -> dict:
    _require_token_scopes(["https://www.googleapis.com/auth/drive"])
    service = _build("drive", "v3")

    parent_clause = f"'{parent_id}' in parents" if parent_id else "'root' in parents"
    query = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{_drive_escape(name)}' and trashed=false and {parent_clause}"
    )
    result = _execute_with_retry(
        service.files().list(
            q=query,
            pageSize=10,
            fields="files(id,name,parents,webViewLink)",
            orderBy="createdTime asc",
        ),
        "drive.files.list.ensure_folder",
    )
    files = result.get("files", [])
    if files:
        return files[0]

    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        body["parents"] = [parent_id]
    created = _execute_with_retry(
        service.files().create(
            body=body,
            fields="id,name,parents,webViewLink",
        ),
        "drive.files.create.folder",
    )
    return created


async def ensure_drive_folder(input: dict) -> dict:
    return await asyncio.to_thread(
        _ensure_drive_folder_sync,
        input["name"],
        input.get("parent_id"),
    )


def _list_drive_folder_files_sync(folder_id: str, max_results: int = 25) -> list[dict]:
    service = _build("drive", "v3")
    result = _execute_with_retry(
        service.files().list(
            q=(
                f"'{folder_id}' in parents and trashed=false "
                "and mimeType != 'application/vnd.google-apps.folder'"
            ),
            pageSize=max_results,
            fields=(
                "files(id,name,mimeType,modifiedTime,createdTime,parents,"
                "size,webViewLink,iconLink)"
            ),
            orderBy="createdTime asc",
        ),
        "drive.files.list.folder_files",
    )
    return result.get("files", [])


async def list_drive_folder_files(input: dict) -> list[dict]:
    return await asyncio.to_thread(
        _list_drive_folder_files_sync,
        input["folder_id"],
        int(input.get("max_results", 25)),
    )


def _download_drive_file_sync(file_id: str) -> dict:
    service = _build("drive", "v3")
    meta = _execute_with_retry(
        service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,modifiedTime,parents,webViewLink,size",
            supportsAllDrives=False,
        ),
        "drive.files.get.download_meta",
    )

    mime = meta["mimeType"]
    if mime == GOOGLE_DOC_MIME:
        content = _execute_with_retry(
            service.files().export(fileId=file_id, mimeType="text/plain"),
            "drive.files.export_doc.download",
        )
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        export_mime = "text/plain"
    elif mime == GOOGLE_SHEET_MIME:
        content = _execute_with_retry(
            service.files().export(fileId=file_id, mimeType="text/csv"),
            "drive.files.export_sheet.download",
        )
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        export_mime = "text/csv"
    else:
        from googleapiclient.http import MediaIoBaseDownload

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = fh.getvalue()
        export_mime = mime

    meta["download_mimeType"] = export_mime
    meta["data_base64"] = base64.b64encode(data).decode("ascii")
    return meta


async def download_drive_file(input: dict) -> dict:
    return await asyncio.to_thread(_download_drive_file_sync, input["file_id"])


def _move_drive_file_sync(file_id: str, destination_folder_id: str) -> dict:
    _require_token_scopes(["https://www.googleapis.com/auth/drive"])
    service = _build("drive", "v3")
    meta = _execute_with_retry(
        service.files().get(fileId=file_id, fields="id,name,parents,webViewLink"),
        "drive.files.get.move_meta",
    )
    current_parents = meta.get("parents", []) or []
    updated = _execute_with_retry(
        service.files().update(
            fileId=file_id,
            addParents=destination_folder_id,
            removeParents=",".join(current_parents) if current_parents else None,
            fields="id,name,parents,webViewLink",
        ),
        "drive.files.update.move",
    )
    return updated


async def move_drive_file(input: dict) -> dict:
    return await asyncio.to_thread(
        _move_drive_file_sync,
        input["file_id"],
        input["destination_folder_id"],
    )
