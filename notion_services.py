"""
Notion API integration: save notes, search workspace, read pages.

Setup:
  1. notion.so/my-integrations → New integration → copy token
  2. Create a database with: Name (title), Category (select)
  3. Share the database with your integration (... menu → Connections)
  4. Copy database ID from the URL
  5. Add to .env: NOTION_TOKEN and NOTION_NOTES_DB_ID
"""
import os
import asyncio
import random
import time
import re
from datetime import date as date_cls, datetime as datetime_cls
from pathlib import Path

from config import NOTION_DIGESTS_DB_ID as CONFIG_NOTION_DIGESTS_DB_ID, NOTION_HABITS_DB_ID as CONFIG_NOTION_HABITS_DB_ID

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_NOTES_DB_ID = os.getenv("NOTION_NOTES_DB_ID")
NOTION_DIGESTS_DB_ID = CONFIG_NOTION_DIGESTS_DB_ID or os.getenv("NOTION_DIGESTS_DB_ID")
NOTION_HABITS_DB_ID = CONFIG_NOTION_HABITS_DB_ID or os.getenv("NOTION_HABITS_DB_ID", "2288ab07-f288-81e4-87c2-000b16a12f95")

DIGEST_TYPES = (
    "Daily Digest",
    "Morning Briefing",
    "Kids Digest",
    "Research Digest",
    "Financial Digest",
)


def _client():
    from notion_client import Client
    return Client(auth=NOTION_TOKEN)


def _is_transient_notion_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("rate limit", "429", "timeout", "tempor", "503", "502", "504"))


def _with_notion_retry(op_name: str, fn, attempts: int = 3):
    delay = 1.0
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts - 1 or not _is_transient_notion_error(e):
                raise
            time.sleep(delay + random.random() * 0.25)
            delay = min(delay * 2, 8.0)


def _normalize_notion_id(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("collection://"):
        value = value.split("collection://", 1)[1]
    if "notion.so" in value:
        value = value.rstrip("/").split("/")[-1].split("?")[0]
        if "-" in value:
            value = value.split("-")[-1]
    value = value.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"
    return raw or ""


def _notion_url_for_id(raw: str | None) -> str:
    normalized = _normalize_notion_id(raw).replace("-", "")
    return f"https://www.notion.so/{normalized}" if normalized else ""


def _persist_env_var(key: str, value: str):
    if not value:
        return
    os.environ[key] = value
    globals()[key] = value

    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return

    lines = env_path.read_text().splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def _rich_text_chunks(text: str, link: str | None = None, color: str | None = None) -> list[dict]:
    content = text or ""
    if not content:
        return []
    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)]
    out: list[dict] = []
    for chunk in chunks:
        entry = {
            "type": "text",
            "text": {
                "content": chunk,
                "link": {"url": link} if link else None,
            },
        }
        if color:
            entry["annotations"] = {"color": color}
        out.append(entry)
    return out


def build_plaintext_blocks(content: str) -> list[dict]:
    paragraphs = [p.strip() for p in (content or "").split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    blocks: list[dict] = []
    for paragraph in paragraphs:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text_chunks(paragraph)},
        })
    return blocks


def build_habits_reference_blocks(target_date: str | None = None) -> list[dict]:
    habits_url = _notion_url_for_id(NOTION_HABITS_DB_ID)
    if not habits_url:
        return []

    label = target_date or date_cls.today().isoformat()
    return [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text_chunks("Today's Habits")},
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": _rich_text_chunks(
                    f"Open the Habits database for {label} and review today's check-ins.",
                    link=habits_url,
                )
            },
        },
    ]


def _title_property_name(db: dict, db_id: str | None = None) -> str:
    props = db.get("properties", {})
    for name, meta in props.items():
        if meta.get("type") == "title":
            return name
    if _normalize_notion_id(db_id) == _normalize_notion_id(NOTION_NOTES_DB_ID):
        return "Name"
    return "Title"


def _retrieve_collection_sync(client, raw_id: str) -> tuple[str, dict]:
    normalized = _normalize_notion_id(raw_id)
    if not normalized:
        raise RuntimeError("Missing Notion collection ID.")

    if hasattr(client, "data_sources"):
        try:
            data_source = _with_notion_retry(
                "data_sources.retrieve",
                lambda: client.data_sources.retrieve(data_source_id=normalized),
            )
            return normalized, data_source
        except Exception:
            pass

    database = _with_notion_retry(
        "databases.retrieve",
        lambda: client.databases.retrieve(database_id=normalized),
    )
    return normalized, database


def _find_attached_data_source_sync(client, database_id: str, preferred_title: str | None = None) -> tuple[str, dict] | tuple[None, None]:
    queries = [preferred_title] if preferred_title else []
    queries.append("")
    normalized_db = _normalize_notion_id(database_id)

    for query in queries:
        results = _with_notion_retry(
            "search.attached_data_source",
            lambda query=query: client.search(
                query=query,
                page_size=100,
            ),
        ).get("results", [])

        for result in results:
            if result.get("object") != "data_source":
                continue
            parent = result.get("parent", {})
            parent_db = _normalize_notion_id(parent.get("database_id"))
            if parent_db != normalized_db:
                continue
            return result["id"], result
    return None, None


def _page_parent_for_collection(collection_id: str, collection_obj: dict) -> dict:
    if collection_obj.get("object") == "data_source":
        return {"data_source_id": collection_id}
    return {"database_id": collection_id}


def _digest_schema_properties() -> dict:
    return {
        "Type": {
            "select": {
                "options": [
                    {"name": "Daily Digest", "color": "gray"},
                    {"name": "Morning Briefing", "color": "blue"},
                    {"name": "Kids Digest", "color": "green"},
                    {"name": "Research Digest", "color": "purple"},
                    {"name": "Financial Digest", "color": "yellow"},
                ]
            }
        },
        "Date": {"date": {}},
    }


def _ensure_digest_schema_sync(client, collection_id: str, collection_obj: dict) -> dict:
    props = collection_obj.get("properties", {}) or {}
    updates = {
        name: definition
        for name, definition in _digest_schema_properties().items()
        if name not in props
    }
    type_prop = props.get("Type") or {}
    if type_prop.get("type") == "select":
        existing_names = {
            option.get("name", "")
            for option in (type_prop.get("select", {}) or {}).get("options", [])
        }
        missing_options = [
            option
            for option in _digest_schema_properties()["Type"]["select"]["options"]
            if option.get("name") not in existing_names
        ]
        if missing_options:
            updates["Type"] = {"select": {"options": missing_options}}

    if not updates:
        return collection_obj

    if collection_obj.get("object") == "data_source" and hasattr(client, "data_sources"):
        return _with_notion_retry(
            "data_sources.update.digest_schema",
            lambda: client.data_sources.update(
                data_source_id=collection_id,
                properties=updates,
            ),
        )

    if collection_obj.get("object") == "database":
        return _with_notion_retry(
            "databases.update.digest_schema",
            lambda: client.databases.update(
                database_id=collection_id,
                properties=updates,
            ),
        )

    return collection_obj


def _find_digests_db_sync(client) -> tuple[str, dict] | tuple[None, None]:
    results = _with_notion_retry(
        "search.digests_db",
        lambda: client.search(
            query="Digests",
            page_size=20,
        ),
    ).get("results", [])

    for result in results:
        if result.get("object") not in {"database", "data_source"}:
            continue
        title = _extract_title(result)
        if title.strip().lower() == "digests":
            return result["id"], result
    return None, None


def _create_digests_db_sync(client) -> tuple[str, dict]:
    if not NOTION_NOTES_DB_ID:
        raise RuntimeError("NOTION_NOTES_DB_ID is required to create the Digests database.")

    notes_db = _with_notion_retry(
        "databases.retrieve.notes",
        lambda: client.databases.retrieve(database_id=NOTION_NOTES_DB_ID),
    )
    parent = notes_db.get("parent", {})
    page_id = parent.get("page_id")
    if not page_id:
        container = _with_notion_retry(
            "pages.create.digest_container",
            lambda: client.pages.create(
                parent={"type": "workspace", "workspace": True},
                properties={
                    "title": {
                        "title": [{"type": "text", "text": {"content": "Digests"}}]
                    }
                },
            ),
        )
        page_id = container["id"]

    created = _with_notion_retry(
        "databases.create.digests",
        lambda: client.databases.create(
            parent={"type": "page_id", "page_id": page_id},
            title=[{"type": "text", "text": {"content": "Digests"}}],
            properties={
                "Title": {"title": {}},
                "Type": {
                    "select": {
                        "options": [
                            {"name": "Daily Digest", "color": "gray"},
                            {"name": "Morning Briefing", "color": "blue"},
                            {"name": "Kids Digest", "color": "green"},
                            {"name": "Research Digest", "color": "purple"},
                            {"name": "Financial Digest", "color": "yellow"},
                        ]
                    }
                },
                "Date": {"date": {}},
            },
        ),
    )
    _persist_env_var("NOTION_DIGESTS_DB_ID", created["id"])
    return created["id"], created


def _ensure_digests_db_sync(client) -> tuple[str, dict]:
    normalized = _normalize_notion_id(NOTION_DIGESTS_DB_ID)
    if normalized:
        normalized, db = _retrieve_collection_sync(client, normalized)
        db = _ensure_digest_schema_sync(client, normalized, db)
        _persist_env_var("NOTION_DIGESTS_DB_ID", normalized)
        return normalized, db

    found_id, found = _find_digests_db_sync(client)
    if found_id:
        found = _ensure_digest_schema_sync(client, found_id, found)
        _persist_env_var("NOTION_DIGESTS_DB_ID", found_id)
        return found_id, found

    try:
        created_id, created = _create_digests_db_sync(client)
        created = _ensure_digest_schema_sync(client, created_id, created)
        return created_id, created
    except Exception:
        if not NOTION_NOTES_DB_ID:
            raise
        notes_id, notes_db = _retrieve_collection_sync(client, NOTION_NOTES_DB_ID)
        if notes_db.get("object") == "database":
            data_source_id, data_source = _find_attached_data_source_sync(
                client,
                notes_id,
                preferred_title=_extract_title(notes_db),
            )
            if data_source_id:
                data_source = _ensure_digest_schema_sync(client, data_source_id, data_source)
                return data_source_id, data_source
        notes_db = _ensure_digest_schema_sync(client, notes_id, notes_db)
        return notes_id, notes_db


def _replace_page_children_sync(client, page_id: str, children: list[dict]):
    current = _with_notion_retry(
        "blocks.children.list",
        lambda: client.blocks.children.list(block_id=page_id, page_size=100),
    ).get("results", [])
    for block in current:
        _with_notion_retry(
            "blocks.delete",
            lambda block_id=block["id"]: client.blocks.delete(block_id=block_id),
        )

    for idx in range(0, len(children), 100):
        chunk = children[idx: idx + 100]
        if not chunk:
            continue
        _with_notion_retry(
            "blocks.children.append",
            lambda chunk=chunk: client.blocks.children.append(block_id=page_id, children=chunk),
        )


def _find_existing_digest_page_sync(
    client,
    db_id: str,
    db: dict,
    title_prop: str,
    digest_type: str,
    digest_date: str,
    title: str,
):
    normalized_db = _normalize_notion_id(db_id)
    results = _with_notion_retry(
        "search.digest.page",
        lambda: client.search(
            query=title,
            filter={"property": "object", "value": "page"},
            page_size=20,
        ),
    ).get("results", [])

    for result in results:
        if _extract_title(result).strip() != title.strip():
            continue
        parent = result.get("parent", {})
        parent_id = _normalize_notion_id(parent.get("database_id") or parent.get("data_source_id"))
        if normalized_db and parent_id and parent_id != normalized_db:
            continue
        return result
    return None


def _save_digest_page_sync(title: str, digest_type: str, digest_date: str, children: list[dict]) -> str:
    client = _client()
    db_id, db = _ensure_digests_db_sync(client)
    parent = _page_parent_for_collection(db_id, db)
    title_prop = _title_property_name(db, db_id)
    normalized_date = digest_date
    if "T" in normalized_date:
        normalized_date = normalized_date.split("T", 1)[0]

    properties = {
        title_prop: {"title": [{"text": {"content": title}}]},
    }
    db_properties = db.get("properties", {}) or {}
    if "Type" in db_properties:
        properties["Type"] = {"select": {"name": digest_type}}
    elif _normalize_notion_id(db_id) == _normalize_notion_id(NOTION_NOTES_DB_ID) and "Category" in db_properties:
        properties["Category"] = {"select": {"name": digest_type}}
    if "Date" in db_properties:
        properties["Date"] = {"date": {"start": normalized_date}}

    existing = _find_existing_digest_page_sync(
        client,
        db_id,
        db,
        title_prop,
        digest_type,
        normalized_date,
        title,
    )
    if existing:
        page_id = existing["id"]
        _with_notion_retry(
            "pages.update.digest",
            lambda: client.pages.update(page_id=page_id, properties=properties),
        )
        _replace_page_children_sync(client, page_id, children)
        page = _with_notion_retry(
            "pages.retrieve.digest",
            lambda: client.pages.retrieve(page_id=page_id),
        )
        return page.get("url", "")

    page = _with_notion_retry(
        "pages.create.digest",
        lambda: client.pages.create(
            parent=parent,
            properties=properties,
            children=children[:100],
        ),
    )
    return page.get("url", "")


async def save_digest_page(input: dict) -> str:
    title = input["title"]
    digest_type = input["digest_type"]
    digest_date = input["digest_date"]
    children = input.get("children") or []
    url = await asyncio.to_thread(_save_digest_page_sync, title, digest_type, digest_date, children)
    return url or f"Saved digest to Notion: {title}"


# ── Save note ─────────────────────────────────────────────────────────────────

def _save_notion_note_sync(title: str, content: str, category: str) -> str:
    client = _client()

    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
    }

    # Add Category only if the database has that property
    try:
        db = _with_notion_retry(
            "databases.retrieve",
            lambda: client.databases.retrieve(database_id=NOTION_NOTES_DB_ID),
        )
        if "Category" in db.get("properties", {}):
            properties["Category"] = {"select": {"name": category}}
    except Exception:
        pass

    children = []
    if content and content != title:
        children = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
            }
        }]

    _with_notion_retry(
        "pages.create",
        lambda: client.pages.create(
            parent={"database_id": NOTION_NOTES_DB_ID},
            properties=properties,
            children=children,
        ),
    )

    return f"Saved to Notion ({category}): {title}"


async def save_notion_note(input: dict) -> str:
    title = input["title"]
    content = input.get("content", "")
    category = input.get("category", "note")
    return await asyncio.to_thread(_save_notion_note_sync, title, content, category)


# ── Search ────────────────────────────────────────────────────────────────────

def _search_notion_sync(query: str) -> str:
    client = _client()
    results = _with_notion_retry(
        "search",
        lambda: client.search(query=query, page_size=10),
    ).get("results", [])

    if not results:
        return "No Notion pages found."

    lines = []
    for obj in results:
        obj_type = obj["object"]
        title = _extract_title(obj)
        page_id = obj["id"].replace("-", "")
        lines.append(f"- {title}  [id: {obj['id']}]  ({obj_type})")

    return "\n".join(lines)


def _extract_title(obj: dict) -> str:
    top_level_title = obj.get("title", [])
    if top_level_title:
        text = "".join(part.get("plain_text", "") for part in top_level_title).strip()
        if text:
            return text
    props = obj.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            if parts:
                return parts[0].get("plain_text", "Untitled")
    return "Untitled"


async def search_notion(input: dict) -> str:
    query = input.get("query", "")
    return await asyncio.to_thread(_search_notion_sync, query)


# ── Read page ─────────────────────────────────────────────────────────────────

def _read_notion_page_sync(page_id: str) -> str:
    client = _client()

    # Clean up page ID (strip URL if pasted)
    if "notion.so" in page_id:
        page_id = page_id.rstrip("/").split("/")[-1].split("?")[0]
        # Handle slug-style IDs like "Title-abc123def456"
        if "-" in page_id:
            page_id = page_id.split("-")[-1]

    # Get page title
    try:
        page = _with_notion_retry(
            "pages.retrieve",
            lambda: client.pages.retrieve(page_id=page_id),
        )
        title = _extract_title(page)
    except Exception:
        title = "Page"

    # Get blocks
    blocks_result = _with_notion_retry(
        "blocks.children.list",
        lambda: client.blocks.children.list(block_id=page_id),
    )
    lines = [f"--- {title} ---"]

    for block in blocks_result.get("results", []):
        text = _block_to_text(block)
        if text:
            lines.append(text)

    return "\n".join(lines) if len(lines) > 1 else f"{title}\n(Empty page)"


def _block_to_text(block: dict) -> str:
    btype = block.get("type", "")
    content = block.get(btype, {})
    rich_text = content.get("rich_text", [])
    text = "".join(rt.get("plain_text", "") for rt in rich_text)

    prefixes = {
        "heading_1": "# ",
        "heading_2": "## ",
        "heading_3": "### ",
        "bulleted_list_item": "• ",
        "numbered_list_item": "  ",
        "to_do": "☐ " if not content.get("checked") else "☑ ",
        "quote": "> ",
        "code": "```\n" + text + "\n```" if text else "",
    }

    if btype == "code":
        return prefixes["code"]
    if btype == "divider":
        return "---"

    prefix = prefixes.get(btype, "")
    return f"{prefix}{text}" if text else ""


async def read_notion_page(input: dict) -> str:
    page_id = input["page_id"]
    return await asyncio.to_thread(_read_notion_page_sync, page_id)
