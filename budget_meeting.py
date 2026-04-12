"""
Weekly budget meeting prep.

Pipeline:
  1. Sync Monarch → local transactions DB
  2. Read Google Sheet for budget targets
  3. Aggregate month-to-date spending by category
  4. Get bills due in next 14 days
  5. Claude synthesis → tight meeting agenda
  6. Save full detail to Notion
  7. Return summary for Telegram
"""
import asyncio
import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from llm import complete_text
from financial_db import (
    sync_transactions,
    get_spending_by_category,
    get_transactions_for_month,
    get_bills_due,
    get_budget_targets,
    upsert_budget_targets,
)

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

MEETING_PROMPT = """You are Jarvis preparing Matt's weekly family budget meeting.

Be direct and specific. Format the output in plain text sections:

1. MONTH-TO-DATE SNAPSHOT — one line per category: status (on track / over / way over), amount spent vs budget, variance. Only show categories with activity or a budget target.

2. BILLS DUE (next 14 days) — list each with due date and amount (or "variable" if unknown).

3. ACTION ITEMS — 3-5 bullet points: what needs attention, what to pay, what to watch.

Keep it tight. This is for a focused 15-minute weekly meeting, not a report.
Plain text only — no markdown headers with #."""


# ── Google Sheet parsing ──────────────────────────────────────────────────────

def _parse_budget_sheet(raw_text: str) -> list[dict]:
    """
    Defensively parse exported sheet text for category + dollar amount pairs.
    Handles common patterns like "Groceries  $600" or "Groceries, 600.00".
    """
    targets = []
    seen = set()
    # Match lines with a word/phrase followed by a dollar amount
    pattern = re.compile(
        r'^([A-Za-z][A-Za-z &/\-]{2,30})\s*[,\t|]*\s*\$?\s*([\d,]+(?:\.\d{1,2})?)\s*$'
    )
    for line in raw_text.splitlines():
        line = line.strip()
        m = pattern.match(line)
        if m:
            cat = m.group(1).strip().title()
            try:
                amount = float(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if cat not in seen and 10 <= amount <= 50000:
                seen.add(cat)
                targets.append({"category": cat, "monthly_target": amount})
    return targets


async def _load_budget_from_sheet() -> list[dict]:
    """Search Drive for the budget sheet, read it, parse targets."""
    from google_services import search_drive, read_drive_file

    try:
        search_result = await search_drive({
            "query": "name contains 'Team Turner Budget 2026' and trashed=false",
            "max_results": 3,
        })
        # Extract first file ID from search result text
        file_id_match = re.search(r"\bid:\s*([A-Za-z0-9_\-]{20,})", search_result, re.IGNORECASE)
        if not file_id_match:
            logger.warning("Budget sheet not found in Drive")
            return []

        file_id = file_id_match.group(1)
        raw = await read_drive_file({"file_id": file_id})
        targets = _parse_budget_sheet(raw)

        if targets:
            await upsert_budget_targets(targets)
            logger.info(f"Loaded {len(targets)} budget targets from sheet")

        return targets
    except Exception as e:
        logger.warning(f"Could not load budget sheet: {e}")
        return []


# ── Meeting prep ──────────────────────────────────────────────────────────────

async def run_budget_meeting() -> str:
    """
    Full pipeline. Returns the Telegram summary string.
    Also saves full detail to Notion.
    """
    now = datetime.now(ET)
    today = date.today()
    year, month = today.year, today.month
    days_in_month = 30  # close enough for display
    day_of_month = today.day
    pct_through = round(day_of_month / days_in_month * 100)

    # 1. Sync Monarch transactions
    synced = await sync_transactions(days=90)
    logger.info(f"Synced {synced} transactions from Monarch")

    # 2. Load budget targets from sheet (also caches to DB)
    await _load_budget_from_sheet()

    # 3. Get spending and budget targets
    spending = await get_spending_by_category(year, month)  # [{category, total}]
    budget_targets = await get_budget_targets()              # {category: target}
    bills = get_bills_due(days_ahead=14)

    # 4. Build context for Claude
    month_label = now.strftime("%B %Y")
    lines = [
        f"Budget meeting prep — {now.strftime('%A, %B %d, %Y')}",
        f"Month: {month_label} — day {day_of_month} of ~{days_in_month} ({pct_through}% through month)\n",
        "SPENDING VS BUDGET (month-to-date):",
    ]

    # Merge spending with targets
    all_categories = set(s["category"] for s in spending) | set(budget_targets.keys())
    spending_map = {s["category"]: s["total"] for s in spending}

    for cat in sorted(all_categories):
        spent = spending_map.get(cat, 0)
        target = budget_targets.get(cat)
        if target:
            variance = spent - target
            pct = round(spent / target * 100) if target else 0
            status = "WAY OVER" if variance > target * 0.2 else "OVER" if variance > 0 else "on track"
            lines.append(f"  {cat}: ${spent:,.0f} spent / ${target:,.0f} budget ({pct}%) — {status}")
        elif spent > 0:
            lines.append(f"  {cat}: ${spent:,.0f} spent (no budget set)")

    lines.append("\nBILLS DUE (next 14 days):")
    if bills:
        for bill in bills:
            amount_str = f"${bill['amount']:,.2f}" if bill.get("amount") else "variable"
            lines.append(f"  {bill['due_date']}  {bill['name']}  {amount_str}")
    else:
        lines.append("  None in the next 14 days.")

    context = "\n".join(lines)

    # 5. Claude synthesis
    agenda = await complete_text(
        model="report",
        max_tokens=800,
        system=MEETING_PROMPT,
        user_content=context,
    )

    # 6. Save to Notion
    notion_title = f"Budget Meeting — {now.strftime('%b %d, %Y')}"
    await _save_to_notion(notion_title, agenda, spending, budget_targets, bills, year, month)

    return agenda


async def _save_to_notion(
    title: str,
    agenda: str,
    spending: list[dict],
    budget_targets: dict,
    bills: list[dict],
    year: int,
    month: int,
):
    """Save full meeting detail to Notion: agenda + all transactions by category."""
    from notion_services import _client, NOTION_NOTES_DB_ID

    all_txns = await get_transactions_for_month(year, month)

    def _save_sync():
        client = _client()
        children = []

        # Agenda at top
        children.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": agenda[:2000]}}]},
        })
        children.append({"object": "block", "type": "divider", "divider": {}})

        # Spending by category with all transactions
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Transactions by Category"}}]},
        })

        # Group transactions by category
        by_cat: dict[str, list] = {}
        for tx in all_txns:
            by_cat.setdefault(tx["category"], []).append(tx)

        for cat, txns in sorted(by_cat.items()):
            cat_total = sum(abs(t["amount"]) for t in txns if t["amount"] < 0)
            target = budget_targets.get(cat)
            header = f"{cat}  —  ${cat_total:,.0f}"
            if target:
                header += f" / ${target:,.0f} budget"
            children.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": header}}]},
            })
            for tx in txns[:30]:  # cap per category
                sign = "-" if tx["amount"] < 0 else "+"
                children.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                        "content": f"{tx['date']}  {sign}${abs(tx['amount']):,.2f}  {tx['merchant']}"
                    }}]},
                })

        # Bills section
        if bills:
            children.append({"object": "block", "type": "divider", "divider": {}})
            children.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Bills Due (next 14 days)"}}]},
            })
            for bill in bills:
                amount_str = f"${bill['amount']:,.2f}" if bill.get("amount") else "variable"
                children.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"type": "text", "text": {
                        "content": f"{bill['due_date']}  {bill['name']}  {amount_str}"
                    }}]},
                })

        client.pages.create(
            parent={"database_id": NOTION_NOTES_DB_ID},
            properties={"Name": {"title": [{"text": {"content": title}}]}},
            children=children[:100],
        )

    try:
        await asyncio.to_thread(_save_sync)
        logger.info(f"Budget meeting saved to Notion: {title}")
    except Exception as e:
        logger.error(f"Failed to save budget meeting to Notion: {e}")
