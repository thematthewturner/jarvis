"""
Weekly finance digest generator.

Pipeline:
  1. Warm Monarch cache
  2. Build rolling 7-day / 30-day spend analysis
  3. Synthesize a tight narrative with Claude
  4. Optionally save to Notion
  5. Return text for Telegram or ad hoc use
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import ANTHROPIC_API_KEY
from financial_db import build_weekly_financial_digest_data, sync_financial_cache
from llm import complete_text

logger = logging.getLogger("jarvis.finance_weekly_digest")
ET = ZoneInfo("America/New_York")

WEEKLY_FINANCE_PROMPT = """You are Jarvis writing Matt's weekly financial digest.

Write in tight narrative form for Telegram. Plain text only.

Requirements:
- Open with one compact paragraph on rolling 7-day and rolling 30-day spend versus the prior comparable windows.
- Use dollar amounts, percentage changes, and daily average pace.
- Make the signal explicit: up, down, flat, or mixed.
- Then explain the "why" in one compact paragraph using the category movers and the most important specific transactions or outliers.
- If a category label looks suspicious or misleading, say so briefly instead of overclaiming.
- End with one short context line on excluded internal movement (transfers, credit card payments, loan repayments).
- Keep it under 220 words.
- Avoid bullets unless absolutely necessary.
"""


def _fmt_money(value: float | int | None) -> str:
    return f"${float(value or 0.0):,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def _trend_sentence(label: str, payload: dict) -> str:
    current = payload["current"]
    prior = payload["prior"]
    delta_pct = payload.get("delta_pct")
    trend = payload.get("trend", "flat")
    return (
        f"{label}: {_fmt_money(current['total'])} over {current['days']} days "
        f"({_fmt_money(current['daily_avg'])}/day) versus {_fmt_money(prior['total'])} "
        f"({_fmt_money(prior['daily_avg'])}/day) in the prior window, {trend} {_fmt_pct(delta_pct)}."
    )


def _format_movers(movers: list[dict], limit: int = 3) -> str:
    if not movers:
        return "none"
    parts = []
    for mover in movers[:limit]:
        pct = mover.get("pct_change")
        pct_text = f" ({pct:+.1f}%)" if pct is not None else ""
        parts.append(
            f"{mover['category']} {_fmt_money(mover['current'])} vs {_fmt_money(mover['prior'])}{pct_text}"
        )
    return "; ".join(parts)


def _format_transactions(items: list[dict], limit: int = 3) -> str:
    if not items:
        return "none"
    parts = []
    for item in items[:limit]:
        parts.append(
            f"{item['merchant']} {_fmt_money(item['amount'])} on {item['date']} ({item['category']})"
        )
    return "; ".join(parts)


def _format_outliers(items: list[dict], limit: int = 3) -> str:
    if not items:
        return "none"
    parts = []
    for item in items[:limit]:
        parts.append(
            f"{item['merchant']} {_fmt_money(item['amount'])} in {item['category']} "
            f"vs {_fmt_money(item['baseline'])} typical ({item['zscore']:.1f} sigma)"
        )
    return "; ".join(parts)


def _fallback_digest(payload: dict) -> str:
    rolling_7 = payload["rolling_7"]
    rolling_30 = payload["rolling_30"]
    movers_7 = payload["category_movers"]["rolling_7"]
    movers_30 = payload["category_movers"]["rolling_30"]
    outliers = payload["outliers"]
    top_7 = payload["top_transactions"]["rolling_7"]
    top_30 = payload["top_transactions"]["rolling_30"]
    excluded_7 = payload["excluded_movement"]["rolling_7"]
    excluded_30 = payload["excluded_movement"]["rolling_30"]

    signal = rolling_30["trend"]
    if rolling_7["trend"] != rolling_30["trend"] and "new" not in {rolling_7["trend"], rolling_30["trend"]}:
        signal = "mixed"

    paragraph_one = (
        f"Your spending signal is {signal}. {_trend_sentence('Rolling 7', rolling_7)} "
        f"{_trend_sentence('Rolling 30', rolling_30)}"
    )
    paragraph_two = (
        f"The main drivers over the last week were { _format_movers(movers_7['up']) }. "
        f"Over the full 30-day window, the biggest movers were { _format_movers(movers_30['up']) }. "
        f"The largest recent transactions were { _format_transactions(top_7 or top_30) }. "
        f"Notable outliers: { _format_outliers(outliers) }."
    )
    context = (
        f"Context: excluded internal movement was {_fmt_money(excluded_7['current']['total'])} over the last 7 days "
        f"and {_fmt_money(excluded_30['current']['total'])} over the last 30 days, versus "
        f"{_fmt_money(excluded_7['prior']['total'])} and {_fmt_money(excluded_30['prior']['total'])} in the prior windows."
    )
    return f"{paragraph_one}\n\n{paragraph_two}\n\n{context}"


def _build_model_context(payload: dict, sync_result: dict) -> str:
    rolling_7 = payload["rolling_7"]
    rolling_30 = payload["rolling_30"]
    movers_7 = payload["category_movers"]["rolling_7"]
    movers_30 = payload["category_movers"]["rolling_30"]
    excluded_7 = payload["excluded_movement"]["rolling_7"]
    excluded_30 = payload["excluded_movement"]["rolling_30"]

    lines = [
        f"Generated on: {payload['generated_on']}",
        f"Monarch sync: mode={sync_result.get('mode')} upserted={sync_result.get('upserted', 0)} range={sync_result.get('range_start')}..{sync_result.get('range_end')}",
        "",
        "ROLLING 7",
        _trend_sentence("Rolling 7", rolling_7),
        f"Top category increases: {_format_movers(movers_7['up'], limit=4)}",
        f"Top category pullbacks: {_format_movers(movers_7['down'], limit=3)}",
        f"Top transactions: {_format_transactions(payload['top_transactions']['rolling_7'], limit=4)}",
        "",
        "ROLLING 30",
        _trend_sentence("Rolling 30", rolling_30),
        f"Top category increases: {_format_movers(movers_30['up'], limit=5)}",
        f"Top category pullbacks: {_format_movers(movers_30['down'], limit=4)}",
        f"Top transactions: {_format_transactions(payload['top_transactions']['rolling_30'], limit=5)}",
        "",
        f"OUTLIERS: {_format_outliers(payload['outliers'], limit=5)}",
        "",
        "EXCLUDED INTERNAL MOVEMENT",
        f"Rolling 7 current {_fmt_money(excluded_7['current']['total'])} vs prior {_fmt_money(excluded_7['prior']['total'])}",
        f"Rolling 30 current {_fmt_money(excluded_30['current']['total'])} vs prior {_fmt_money(excluded_30['prior']['total'])}",
        f"Excluded categories: {', '.join(payload['excluded_categories'])}",
    ]
    return "\n".join(lines)


async def generate_weekly_financial_digest() -> tuple[str, str, dict]:
    now = datetime.now(ET)
    sync_result = await sync_financial_cache(force_backfill=False)
    payload = await build_weekly_financial_digest_data()
    notion_title = f"Weekly Financial Digest — {now.strftime('%b %d, %Y')}"

    if not ANTHROPIC_API_KEY:
        return _fallback_digest(payload), notion_title, payload

    user_content = _build_model_context(payload, sync_result)

    try:
        digest_text = await complete_text(
            model="report",
            max_tokens=700,
            system=WEEKLY_FINANCE_PROMPT,
            user_content=user_content,
        )
        return digest_text.strip(), notion_title, payload
    except Exception as e:
        logger.warning("Weekly finance digest synthesis failed, using fallback: %s", e)
        return _fallback_digest(payload), notion_title, payload


async def save_weekly_financial_digest_to_notion(title: str, content: str):
    from notion_services import build_plaintext_blocks, save_digest_page

    try:
        digest_date = datetime.now(ET).date().isoformat()
        await save_digest_page({
            "title": title,
            "digest_type": "Financial Digest",
            "digest_date": digest_date,
            "children": build_plaintext_blocks(content),
        })
        logger.info("Weekly finance digest saved to Notion: %s", title)
    except Exception as e:
        logger.error("Failed to save weekly finance digest to Notion: %s", e)


async def run_weekly_financial_digest(save_to_notion: bool = True) -> str:
    digest_text, notion_title, _payload = await generate_weekly_financial_digest()
    if save_to_notion:
        await save_weekly_financial_digest_to_notion(notion_title, digest_text)
    return digest_text
