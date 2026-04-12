"""
Monarch Money integration: account balances, transactions, budget summary.

⚠️  Unofficial API — community-maintained, no MFA required for this setup.

Setup:
  Add to .env:
    MONARCH_EMAIL=your@email.com
    MONARCH_PASSWORD=your_monarch_password
"""
import os
import asyncio
import logging
import random
from datetime import date, timedelta

logger = logging.getLogger("jarvis")

MONARCH_EMAIL = os.getenv("MONARCH_EMAIL")
MONARCH_PASSWORD = os.getenv("MONARCH_PASSWORD")

# Module-level cached session — login once, reuse across calls
_mm = None


def _is_transient_monarch_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "tempor", "rate limit", "503", "502", "504", "connection"))


async def _with_retry(op_name: str, fn, attempts: int = 3):
    delay = 1.0
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as e:
            if attempt == attempts - 1 or not _is_transient_monarch_error(e):
                raise
            logger.warning(
                "Retrying Monarch op %s (%s/%s) after error: %s",
                op_name, attempt + 1, attempts, e
            )
            await asyncio.sleep(delay + random.random() * 0.25)
            delay = min(delay * 2, 8.0)


async def _client():
    global _mm
    from monarchmoney import MonarchMoney

    if not MONARCH_EMAIL or not MONARCH_PASSWORD:
        raise RuntimeError("MONARCH_EMAIL and MONARCH_PASSWORD must be set")

    if _mm is None:
        _mm = MonarchMoney()
        await _with_retry(
            "login",
            lambda: _mm.login(
                email=MONARCH_EMAIL,
                password=MONARCH_PASSWORD,
                use_saved_session=True,  # loads .mm/mm_session.pickle if present
                save_session=True,
            ),
        )
        logger.info("Monarch Money session established")

    return _mm


# ── Account balances ──────────────────────────────────────────────────────────

async def get_account_balances(input: dict) -> str:
    try:
        mm = await _client()
        data = await _with_retry("get_accounts", mm.get_accounts)
    except Exception as e:
        return f"Monarch error: {e}"

    accounts = data.get("accounts", [])
    if not accounts:
        return "No accounts found."

    assets, liabilities = [], []
    for acc in accounts:
        if not acc.get("isActive", True):
            continue
        name = acc.get("displayName") or acc.get("name", "Unknown")
        balance = acc.get("currentBalance") or 0
        institution = (acc.get("institution") or {}).get("name", "")
        label = f"{name}" + (f" ({institution})" if institution else "")
        acc_type = (acc.get("type") or {}).get("name", "").lower()

        if acc_type in ("credit", "loan", "mortgage"):
            liabilities.append((label, balance))
        else:
            assets.append((label, balance))

    lines = []
    if assets:
        lines.append("Assets:")
        for label, bal in assets:
            lines.append(f"  {label}: ${bal:,.2f}")

    if liabilities:
        lines.append("Liabilities:")
        for label, bal in liabilities:
            lines.append(f"  {label}: ${abs(bal):,.2f}")

    total_assets = sum(b for _, b in assets)
    total_liabilities = sum(abs(b) for _, b in liabilities)
    net = total_assets - total_liabilities
    lines.append(f"\nNet worth: ${net:,.2f}")

    return "\n".join(lines)


# ── Transactions ──────────────────────────────────────────────────────────────

async def get_recent_transactions(input: dict) -> str:
    days = int(input.get("days", 7))
    category = input.get("category", "")  # optional filter keyword

    try:
        mm = await _client()
        end = date.today()
        start = end - timedelta(days=days)

        data = await _with_retry(
            "get_transactions",
            lambda: mm.get_transactions(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                limit=50,
            ),
        )
    except Exception as e:
        return f"Monarch error: {e}"

    txns = (data.get("allTransactions") or {}).get("results", [])

    if category:
        txns = [
            t for t in txns
            if category.lower() in ((t.get("category") or {}).get("name", "")).lower()
        ]

    if not txns:
        return f"No transactions in the last {days} days."

    lines = [f"Transactions — last {days} days ({start} to {end}):"]
    total_spent = 0

    for t in txns:
        d = t.get("date", "?")
        amount = t.get("amount") or 0
        merchant = (
            (t.get("merchant") or {}).get("name")
            or t.get("description")
            or "Unknown"
        )
        cat = (t.get("category") or {}).get("name", "Uncategorized")
        sign = "+" if amount > 0 else "-"
        lines.append(f"  {d}  {sign}${abs(amount):,.2f}  {merchant}  [{cat}]")
        if amount < 0:
            total_spent += abs(amount)

    lines.append(f"\nTotal spent: ${total_spent:,.2f}")
    return "\n".join(lines)


# ── Budget summary ────────────────────────────────────────────────────────────

async def get_budget_summary(input: dict) -> str:
    try:
        mm = await _client()
        today = date.today()
        month_str = today.strftime("%Y-%m")

        data = await _with_retry(
            "get_budgets",
            lambda: mm.get_budgets(
                start_date=f"{month_str}-01",
                end_date=today.isoformat(),
            ),
        )
    except Exception as e:
        return f"Monarch error: {e}"

    # The budgets response structure varies — parse defensively
    budget_items = []

    # Try common response shapes
    if isinstance(data, dict):
        items = (
            data.get("budgets")
            or data.get("budget")
            or data.get("categoryBudgets")
            or []
        )
        if isinstance(items, dict):
            items = list(items.values())
        budget_items = items

    if not budget_items:
        return "No budget data available for this month."

    lines = [f"Budget — {today.strftime('%B %Y')}:"]
    for item in budget_items[:20]:  # cap output
        try:
            name = (
                item.get("category", {}).get("name")
                or item.get("name")
                or "Unknown"
            )
            budgeted = item.get("budgetedAmount") or item.get("amount") or 0
            actual = item.get("actualAmount") or item.get("spent") or 0
            remaining = budgeted - actual
            status = "✓" if remaining >= 0 else "⚠"
            lines.append(
                f"  {status} {name}: ${actual:,.0f} / ${budgeted:,.0f}"
                + (f" (${abs(remaining):,.0f} {'left' if remaining >= 0 else 'over'})" if budgeted else "")
            )
        except Exception:
            continue

    return "\n".join(lines)
