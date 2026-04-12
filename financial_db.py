"""
Local financial database: transaction cache + budget targets.

Tables:
  transactions        — Monarch Money transactions (upserted on each sync)
  budget_targets      — Monthly category budgets (parsed from Google Sheet)
  financial_sync_state — cache/sync metadata
"""
import hashlib
import logging
import asyncio
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from memory import db_connect

logger = logging.getLogger("jarvis")
EXCLUDED_SPENDING_CATEGORIES = {"Transfer", "Credit Card Payment", "Loan Repayment"}


# ── Schema ─────────────────────────────────────────────────────────────────────

async def init_financial_db():
    async with db_connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                merchant TEXT,
                amount REAL,
                category TEXT,
                description TEXT,
                synced_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS budget_targets (
                category TEXT PRIMARY KEY,
                monthly_target REAL,
                notes TEXT,
                updated_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS financial_sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS utility_bill_signals (
                utility_key TEXT NOT NULL,
                statement_month TEXT NOT NULL,
                status TEXT NOT NULL,
                amount REAL,
                due_date TEXT,
                source TEXT,
                account TEXT,
                external_id TEXT,
                sender TEXT,
                subject TEXT,
                confidence REAL DEFAULT 0,
                excerpt TEXT,
                attachment_names TEXT,
                matched_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (utility_key, statement_month)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS finance_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file_id TEXT NOT NULL UNIQUE,
                source_file_name TEXT NOT NULL,
                source_mime_type TEXT,
                source_modified_time TEXT,
                source_web_view_link TEXT,
                status TEXT NOT NULL DEFAULT 'processed',
                document_type TEXT,
                institution TEXT,
                account_last4 TEXT,
                statement_date TEXT,
                due_date TEXT,
                amount_due REAL,
                statement_balance REAL,
                minimum_due REAL,
                autopay_status TEXT,
                usage_period TEXT,
                summary TEXT,
                key_points_json TEXT,
                document_metadata_json TEXT,
                anomalies_json TEXT,
                raw_packet_json TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                needs_review INTEGER DEFAULT 1,
                review_status TEXT DEFAULT 'open',
                reviewed_at TEXT,
                review_notes TEXT,
                notion_title TEXT,
                processed_at TEXT NOT NULL,
                archived_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        async with db.execute("PRAGMA table_info(finance_documents)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "key_points_json" not in columns:
            await db.execute("ALTER TABLE finance_documents ADD COLUMN key_points_json TEXT")
        if "document_metadata_json" not in columns:
            await db.execute("ALTER TABLE finance_documents ADD COLUMN document_metadata_json TEXT")
        if "review_status" not in columns:
            await db.execute("ALTER TABLE finance_documents ADD COLUMN review_status TEXT DEFAULT 'open'")
        if "reviewed_at" not in columns:
            await db.execute("ALTER TABLE finance_documents ADD COLUMN reviewed_at TEXT")
        if "review_notes" not in columns:
            await db.execute("ALTER TABLE finance_documents ADD COLUMN review_notes TEXT")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category)")
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_utility_bill_month
            ON utility_bill_signals(statement_month)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_finance_documents_status
            ON finance_documents(status, processed_at)
        """)
        await db.commit()


# ── Monarch sync ───────────────────────────────────────────────────────────────

SYNC_KEY_BACKFILL_DONE = "monarch_backfill_done_v1"
SYNC_KEY_LAST_SYNC_DATE = "monarch_last_sync_date"
SYNC_KEY_LAST_SYNC_MODE = "monarch_last_sync_mode"
SYNC_KEY_LAST_SYNC_COUNT = "monarch_last_sync_count"
SYNC_KEY_LAST_SYNC_RANGE_START = "monarch_last_sync_range_start"
SYNC_KEY_LAST_SYNC_RANGE_END = "monarch_last_sync_range_end"


def _is_transient_monarch_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "tempor", "rate limit", "503", "502", "504", "connection"))


async def _fetch_transactions_with_retry(mm, window_start: date, window_end: date, attempts: int = 3):
    delay = 1.0
    for attempt in range(attempts):
        try:
            return await mm.get_transactions(
                start_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
                limit=2000,
            )
        except Exception as e:
            if attempt == attempts - 1 or not _is_transient_monarch_error(e):
                raise
            logger.warning(
                "Retrying Monarch window fetch %s..%s (%s/%s): %s",
                window_start, window_end, attempt + 1, attempts, e,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)


def _tx_id(t: dict) -> str:
    """Generate a stable dedup key for a transaction."""
    if t.get("id"):
        return str(t["id"])
    merchant = (t.get("merchant") or {}).get("name") or t.get("description") or ""
    raw = f"{t.get('date', '')}{merchant}{t.get('amount', '')}"
    return hashlib.sha1(raw.encode()).hexdigest()


async def _state_get(db, key: str, default: str = "") -> str:
    cur = await db.execute(
        "SELECT value FROM financial_sync_state WHERE key=? LIMIT 1",
        (key,),
    )
    row = await cur.fetchone()
    return row[0] if row else default


async def _state_set(db, key: str, value: str):
    now = datetime.utcnow().isoformat()
    await db.execute(
        """
        INSERT INTO financial_sync_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, now),
    )


def _month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into month-sized windows."""
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        w_end = min(month_end, end)
        windows.append((cursor, w_end))
        cursor = w_end + timedelta(days=1)
    return windows


def _tx_row(t: dict, synced_at: str) -> tuple:
    merchant = (
        (t.get("merchant") or {}).get("name")
        or t.get("description")
        or "Unknown"
    )
    category = (t.get("category") or {}).get("name", "Uncategorized")
    amount = float(t.get("amount") or 0)
    return (
        _tx_id(t),
        t.get("date", ""),
        merchant,
        amount,
        category,
        t.get("notes") or "",
        synced_at,
    )


async def _upsert_transactions(db, txns: list[dict], synced_at: str) -> int:
    rows = [_tx_row(t, synced_at) for t in txns if t.get("date")]
    if not rows:
        return 0

    await db.executemany(
        """
        INSERT INTO transactions
            (id, date, merchant, amount, category, description, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date=excluded.date,
            merchant=excluded.merchant,
            amount=excluded.amount,
            category=excluded.category,
            description=excluded.description,
            synced_at=excluded.synced_at
        """,
        rows,
    )
    return len(rows)


async def sync_transactions_range(start: date, end: date) -> dict:
    """
    Pull transactions from Monarch for [start, end] in month windows and upsert.
    Returns sync stats for observability.
    """
    if end < start:
        return {"windows": 0, "fetched": 0, "upserted": 0}

    try:
        from monarch_services import _client
        mm = await _client()
    except Exception as e:
        logger.error(f"Monarch sync failed: {e}")
        return {"windows": 0, "fetched": 0, "upserted": 0, "error": str(e)}

    windows = _month_windows(start, end)
    fetched = 0
    upserted = 0
    synced_at = datetime.utcnow().isoformat()

    async with db_connect() as db:
        for window_start, window_end in windows:
            data = await _fetch_transactions_with_retry(mm, window_start, window_end)
            txns = (data.get("allTransactions") or {}).get("results", [])
            fetched += len(txns)
            upserted += await _upsert_transactions(db, txns, synced_at)
            await db.commit()

    logger.info(
        "Monarch sync: windows=%s fetched=%s upserted=%s (%s to %s)",
        len(windows), fetched, upserted, start, end,
    )
    return {
        "windows": len(windows),
        "fetched": fetched,
        "upserted": upserted,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


async def sync_transactions(days: int = 90) -> int:
    """
    Backward-compatible wrapper used by existing budget meeting flow.
    Pull latest transactions from Monarch and upsert into local DB.
    """
    end = date.today()
    start = end - timedelta(days=days)
    stats = await sync_transactions_range(start, end)
    return int(stats.get("upserted", 0))


async def sync_financial_cache(force_backfill: bool = False) -> dict:
    """
    Keep local Monarch cache warm:
    - one-time 365-day backfill (or force_backfill)
    - otherwise once/day incremental sync
    """
    today = date.today()
    today_s = today.isoformat()

    async with db_connect() as db:
        tx_count_cur = await db.execute("SELECT COUNT(*) FROM transactions")
        tx_count_row = await tx_count_cur.fetchone()
        tx_count = int(tx_count_row[0] if tx_count_row else 0)
        backfill_done = await _state_get(db, SYNC_KEY_BACKFILL_DONE, "0")
        last_sync_date = await _state_get(db, SYNC_KEY_LAST_SYNC_DATE, "")
        last_sync_count = await _state_get(db, SYNC_KEY_LAST_SYNC_COUNT, "0")
        last_sync_range_start = await _state_get(db, SYNC_KEY_LAST_SYNC_RANGE_START, "")
        last_sync_range_end = await _state_get(db, SYNC_KEY_LAST_SYNC_RANGE_END, "")

    if (not force_backfill) and last_sync_date == today_s:
        return {
            "mode": "skipped",
            "reason": "already_synced_today",
            "upserted": int(last_sync_count or 0),
            "range_start": last_sync_range_start or None,
            "range_end": last_sync_range_end or None,
        }

    do_backfill = force_backfill or tx_count == 0 or backfill_done != "1"
    mode = "backfill_365d" if do_backfill else "daily_incremental"

    if do_backfill:
        start = today - timedelta(days=365)
        stats = await sync_transactions_range(start, today)
    else:
        # Sync a small overlap window to catch late-posting transactions.
        async with db_connect() as db:
            latest_cur = await db.execute("SELECT MAX(date) FROM transactions")
            latest_row = await latest_cur.fetchone()
            latest_date_raw = latest_row[0] if latest_row else ""

        if latest_date_raw:
            try:
                latest_date = date.fromisoformat(latest_date_raw)
                start = max(today - timedelta(days=14), latest_date - timedelta(days=2))
            except ValueError:
                start = today - timedelta(days=14)
        else:
            start = today - timedelta(days=14)

        stats = await sync_transactions_range(start, today)

    async with db_connect() as db:
        await _state_set(db, SYNC_KEY_LAST_SYNC_DATE, today_s)
        await _state_set(db, SYNC_KEY_LAST_SYNC_MODE, mode)
        await _state_set(db, SYNC_KEY_LAST_SYNC_COUNT, str(stats.get("upserted", 0)))
        await _state_set(db, SYNC_KEY_LAST_SYNC_RANGE_START, str(stats.get("start") or ""))
        await _state_set(db, SYNC_KEY_LAST_SYNC_RANGE_END, str(stats.get("end") or ""))
        if do_backfill:
            await _state_set(db, SYNC_KEY_BACKFILL_DONE, "1")
        await db.commit()

    return {
        "mode": mode,
        "upserted": int(stats.get("upserted", 0)),
        "fetched": int(stats.get("fetched", 0)),
        "windows": int(stats.get("windows", 0)),
        "range_start": stats.get("start"),
        "range_end": stats.get("end"),
    }


async def get_recent_spending_transactions(days: int = 7, limit: int = 8) -> dict:
    start = (date.today() - timedelta(days=days)).isoformat()
    async with db_connect() as db:
        count_cur = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE date >= ? AND amount < 0",
            (start,),
        )
        count_row = await count_cur.fetchone()
        cur = await db.execute(
            """
            SELECT date, merchant, amount, category
            FROM transactions
            WHERE date >= ? AND amount < 0
            ORDER BY date DESC, ABS(amount) DESC
            LIMIT ?
            """,
            (start, limit),
        )
        rows = await cur.fetchall()

    items = [
        {
            "date": r[0],
            "merchant": r[1] or "Unknown",
            "amount": round(float(r[2] or 0), 2),
            "category": r[3] or "Uncategorized",
        }
        for r in rows
    ]
    return {"count": int(count_row[0] if count_row else 0), "items": items}


# ── Spending analysis ─────────────────────────────────────────────────────────

THINGS_THAT_MOVE_HINTS = (
    "shop",
    "amazon",
    "general merchandise",
    "furniture",
    "home improvement",
    "electronics",
    "clothing",
    "apparel",
    "sporting",
    "toys",
    "supplies",
    "home goods",
    "department store",
)


def _is_things_that_move(category: str, merchant: str = "") -> bool:
    text = f"{(category or '').lower()} {(merchant or '').lower()}"
    return any(h in text for h in THINGS_THAT_MOVE_HINTS)


def _bucket_for_category(category: str) -> str:
    c = (category or "").lower()
    if any(k in c for k in ("grocery", "grocer", "supermarket")):
        return "Groceries"
    if any(k in c for k in ("restaurant", "dining", "food", "coffee", "bar")):
        return "Food"
    if _is_things_that_move(category):
        return "Things That Move"
    return "Other"


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    month_index = (d.year * 12 + (d.month - 1)) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    return date(year, month, 1)


def _rolling(values: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        w = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(mean(w) if w else None)
    return out


def _control_series(values: list[float], window: int = 12, sigma: float = 2.5) -> dict:
    cleaned: list[float | None] = []
    center: list[float | None] = []
    ucl: list[float | None] = []
    lcl: list[float | None] = []
    outlier_points: list[float | None] = []
    outlier_meta: list[dict] = []

    for i, val in enumerate(values):
        history = [v for v in cleaned[max(0, i - window):i] if v is not None]
        if len(history) >= 6:
            mu = mean(history)
            sigma_hat = pstdev(history) if len(history) > 1 else 0.0
            band = max(sigma * sigma_hat, max(mu * 0.08, 25.0))
            hi = mu + band
            lo = max(0.0, mu - band)
            is_outlier = val > hi or val < lo
            if is_outlier:
                cleaned.append(None)
                outlier_points.append(val)
                z = (val - mu) / (sigma_hat or 1.0)
                outlier_meta.append({"index": i, "value": round(val, 2), "zscore": round(z, 2), "ucl": round(hi, 2)})
            else:
                cleaned.append(val)
                outlier_points.append(None)
        else:
            mu = mean(history) if history else val
            sigma_hat = pstdev(history) if len(history) > 1 else 0.0
            hi = mu + sigma * sigma_hat
            lo = max(0.0, mu - sigma * sigma_hat)
            cleaned.append(val)
            outlier_points.append(None)

        center.append(round(mu, 2) if mu is not None else None)
        ucl.append(round(hi, 2) if hi is not None else None)
        lcl.append(round(lo, 2) if lo is not None else None)

    return {
        "cleaned": [round(v, 2) if v is not None else None for v in cleaned],
        "center": center,
        "ucl": ucl,
        "lcl": lcl,
        "outlier_points": [round(v, 2) if v is not None else None for v in outlier_points],
        "outliers": outlier_meta,
    }


def _attach_outlier_labels(ctrl: dict, labels: list[str], week_keys: list[str]) -> list[dict]:
    outliers: list[dict] = []
    for o in ctrl.get("outliers", []):
        idx = int(o.get("index", -1))
        if 0 <= idx < len(week_keys):
            outliers.append({
                "period_start": week_keys[idx],
                "week_start": week_keys[idx],
                "label": labels[idx],
                "amount": o.get("value"),
                "ucl": o.get("ucl"),
                "zscore": o.get("zscore"),
            })
    return outliers


def _series_signal(latest: float, baseline: float) -> str:
    if baseline <= 0:
        return "stable"
    ratio = latest / baseline
    if ratio >= 1.35:
        return "high"
    if ratio <= 0.7:
        return "low"
    return "stable"


def _month_label(month_key: str) -> str:
    try:
        return datetime.strptime(month_key, "%Y-%m-%d").strftime("%b %y")
    except ValueError:
        return month_key


def _trailing_pnl_stats(monthly_pnl: list[dict], months: int) -> dict:
    window = monthly_pnl[-months:]
    income = sum(float(m.get("income") or 0.0) for m in window)
    expenses = sum(float(m.get("expenses") or 0.0) for m in window)
    net = income - expenses
    savings_rate = (net / income * 100.0) if income > 0 else 0.0
    return {
        "label": f"{months}M",
        "months": months,
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "net": round(net, 2),
        "savings_rate": round(savings_rate, 1),
    }


def _alert_severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


async def build_finance_dashboard(months: int = 15, control_window: int = 6) -> dict:
    """
    Build finance analytics payload for the Finances dashboard UI.
    """
    today = date.today()
    start_365 = today - timedelta(days=365)
    start_90 = today - timedelta(days=90)
    start_30 = today - timedelta(days=30)
    chart_months = max(12, int(months or 15))
    baseline_window = max(3, min(int(control_window or 6), chart_months - 1))
    start_chart = _add_months(_month_start(today), -(chart_months - 1))

    async with db_connect() as db:
        cat_cur = await db.execute(
            """
            SELECT category, SUM(ABS(amount)) AS total, COUNT(*) AS tx_count
            FROM transactions
            WHERE date >= ? AND amount < 0
            GROUP BY category
            ORDER BY total DESC
            """,
            (start_365.isoformat(),),
        )
        cat_rows = await cat_cur.fetchall()

        cat_90_cur = await db.execute(
            """
            SELECT category, SUM(ABS(amount)) AS total, COUNT(*) AS tx_count
            FROM transactions
            WHERE date >= ? AND amount < 0
            GROUP BY category
            ORDER BY total DESC
            """,
            (start_90.isoformat(),),
        )
        cat_90_rows = await cat_90_cur.fetchall()

        sum_cur = await db.execute(
            """
            SELECT
              SUM(CASE WHEN date >= ? AND amount < 0 THEN ABS(amount) ELSE 0 END) AS spend_365,
              SUM(CASE WHEN date >= ? AND amount < 0 THEN ABS(amount) ELSE 0 END) AS spend_90,
              SUM(CASE WHEN date >= ? AND amount < 0 THEN ABS(amount) ELSE 0 END) AS spend_30,
              SUM(CASE WHEN date >= ? AND amount < 0 THEN 1 ELSE 0 END) AS tx_30
            FROM transactions
            """,
            (start_365.isoformat(), start_90.isoformat(), start_30.isoformat(), start_30.isoformat()),
        )
        sums = await sum_cur.fetchone()

        w_cur = await db.execute(
            """
            SELECT date, merchant, category, amount
            FROM transactions
            WHERE date >= ? AND amount < 0
            ORDER BY date ASC
            """,
            (start_chart.isoformat(),),
        )
        w_rows = await w_cur.fetchall()

        pnl_cur = await db.execute(
            """
            SELECT date, amount
            FROM transactions
            WHERE date >= ?
            ORDER BY date ASC
            """,
            (start_chart.isoformat(),),
        )
        pnl_rows = await pnl_cur.fetchall()

    spend_365 = float(sums[0] or 0.0)
    spend_90 = float(sums[1] or 0.0)
    spend_30 = float(sums[2] or 0.0)
    tx_30 = int(sums[3] or 0)

    category_stats: dict[str, dict] = {}
    for cat, total, tx_count in cat_rows:
        cat_name = (cat or "Uncategorized").strip() or "Uncategorized"
        t = float(total or 0.0)
        share = (t / spend_365 * 100) if spend_365 else 0.0
        category_stats[cat_name] = {
            "category": cat_name,
            "bucket": _bucket_for_category(cat_name),
            "total_365": round(t, 2),
            "monthly_avg": round(t / 12.0, 2),
            "tx_count": int(tx_count or 0),
            "share_pct": round(share, 1),
        }

    top_categories = sorted(category_stats.values(), key=lambda r: r["total_365"], reverse=True)[:12]

    spend_90 = sum(float(total or 0.0) for _, total, _ in cat_90_rows)
    treemap_categories: list[dict] = []
    covered_90 = 0.0
    for idx, (cat, total, tx_count) in enumerate(cat_90_rows):
        t = float(total or 0.0)
        if idx < 12:
            cat_name = (cat or "Uncategorized").strip() or "Uncategorized"
            treemap_categories.append({
                "category": cat_name,
                "bucket": _bucket_for_category(cat_name),
                "total_90": round(t, 2),
                "tx_count": int(tx_count or 0),
                "share_pct": round((t / spend_90 * 100), 1) if spend_90 else 0.0,
            })
            covered_90 += t
    other_90 = max(0.0, spend_90 - covered_90)
    if other_90 > 0.01:
        treemap_categories.append({
            "category": "Other",
            "bucket": "Other",
            "total_90": round(other_90, 2),
            "tx_count": max(0, sum(int(r[2] or 0) for r in cat_90_rows[12:])),
            "share_pct": round((other_90 / spend_90 * 100), 1) if spend_90 else 0.0,
        })

    month_starts = [_add_months(start_chart, i) for i in range(chart_months)]
    labels = [m.strftime("%b %y") for m in month_starts]
    month_keys = [m.isoformat() for m in month_starts]
    all_monthly = {mk: 0.0 for mk in month_keys}
    moving_monthly = {mk: 0.0 for mk in month_keys}
    income_monthly = {mk: 0.0 for mk in month_keys}
    expense_monthly = {mk: 0.0 for mk in month_keys}
    category_monthly: dict[str, dict[str, float]] = {}
    moving_90 = 0.0

    for d_raw, amount in pnl_rows:
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            continue
        month_key = _month_start(d).isoformat()
        if month_key not in income_monthly:
            continue
        amt = float(amount or 0.0)
        if amt >= 0:
            income_monthly[month_key] += amt
        else:
            expense_monthly[month_key] += abs(amt)

    for d_raw, merchant, category, amount in w_rows:
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            continue
        month_key = _month_start(d).isoformat()
        if month_key not in all_monthly:
            continue
        spend = abs(float(amount or 0.0))
        all_monthly[month_key] += spend
        if _is_things_that_move(category or "", merchant or ""):
            moving_monthly[month_key] += spend
            if d >= start_90:
                moving_90 += spend

        cat_name = (category or "Uncategorized").strip() or "Uncategorized"
        if cat_name not in category_monthly:
            category_monthly[cat_name] = {mk: 0.0 for mk in month_keys}
        category_monthly[cat_name][month_key] += spend

    moving_values = [round(moving_monthly[k], 2) for k in month_keys]
    all_values = [round(all_monthly[k], 2) for k in month_keys]
    ctrl = _control_series(moving_values, window=baseline_window, sigma=2.5)
    rolling_avg = _rolling(ctrl["cleaned"], baseline_window)
    outliers = _attach_outlier_labels(ctrl, labels, month_keys)

    latest_moving = moving_values[-1] if moving_values else 0.0
    baseline_vals = [v for v in ctrl["cleaned"][-baseline_window:] if v is not None]
    baseline = mean(baseline_vals) if baseline_vals else 0.0
    signal = _series_signal(latest_moving, baseline)

    variable_candidates: list[dict] = []
    for cat_name, monthly_map in category_monthly.items():
        stats = category_stats.get(cat_name)
        if not stats:
            continue

        monthly_values = [round(float(monthly_map[k] or 0.0), 2) for k in month_keys]
        active_months = sum(1 for v in monthly_values if v > 0)
        if active_months < 4:
            continue

        total_period = sum(monthly_values)
        mean_month = mean(monthly_values) if monthly_values else 0.0
        std_month = pstdev(monthly_values) if len(monthly_values) > 1 else 0.0
        if total_period < 600 or stats["total_365"] < 1200 or mean_month < 75:
            continue

        ctrl_cat = _control_series(monthly_values, window=baseline_window, sigma=2.5)
        rolling_cat = _rolling(ctrl_cat["cleaned"], baseline_window)
        cat_outliers = _attach_outlier_labels(ctrl_cat, labels, month_keys)
        baseline_vals_cat = [v for v in ctrl_cat["cleaned"][-baseline_window:] if v is not None]
        baseline_cat = mean(baseline_vals_cat) if baseline_vals_cat else 0.0
        latest_cat = monthly_values[-1] if monthly_values else 0.0
        magnitude_weight = min(2.0, (mean_month / 500.0) + 0.35)
        variability_score = std_month * magnitude_weight

        variable_candidates.append({
            "category": cat_name,
            "bucket": stats["bucket"],
            "total_365": stats["total_365"],
            "monthly_avg": stats["monthly_avg"],
            "tx_count": stats["tx_count"],
            "share_pct": stats["share_pct"],
            "latest_month": round(latest_cat, 2),
            "avg_monthly_baseline": round(baseline_cat, 2),
            "signal": _series_signal(latest_cat, baseline_cat),
            "variability": round(std_month, 2),
            "variability_score": round(variability_score, 2),
            "outlier_count": len(cat_outliers),
            "labels": labels,
            "period_starts": month_keys,
            "period_totals": monthly_values,
            "cleaned_totals": ctrl_cat["cleaned"],
            "rolling_avg": [round(v, 2) if v is not None else None for v in rolling_cat],
            "center_line": ctrl_cat["center"],
            "ucl": ctrl_cat["ucl"],
            "lcl": ctrl_cat["lcl"],
            "outlier_points": ctrl_cat["outlier_points"],
            "outliers": cat_outliers,
            "period_unit": "month",
            "baseline_window": baseline_window,
        })

    variable_candidates.sort(
        key=lambda c: (c["variability_score"], c["total_365"], c["share_pct"]),
        reverse=True,
    )
    variable_charts = variable_candidates[:10]

    monthly_pnl: list[dict] = []
    for mk, label in zip(month_keys, labels):
        income = round(income_monthly.get(mk, 0.0), 2)
        expenses = round(expense_monthly.get(mk, 0.0), 2)
        net = round(income - expenses, 2)
        savings_rate = round((net / income * 100.0), 1) if income > 0 else 0.0
        monthly_pnl.append({
            "month": mk[:7],
            "label": label,
            "income": income,
            "expenses": expenses,
            "net": net,
            "savings_rate": savings_rate,
        })

    terminal_summary = {
        "net_mtd": round(monthly_pnl[-1]["net"] if monthly_pnl else 0.0, 2),
        "burn_rate_daily": round((spend_30 / 30.0) if spend_30 else 0.0, 2),
        "avg_expense_ticket_30d": round((spend_30 / tx_30) if tx_30 else 0.0, 2),
        "savings_rate_3m": round(_trailing_pnl_stats(monthly_pnl, 3)["savings_rate"] if len(monthly_pnl) >= 3 else 0.0, 1),
        "current_month": labels[-1] if labels else "",
        "transaction_count_30d": tx_30,
    }

    merchant_window_keys = month_keys[-6:] if len(month_keys) >= 6 else month_keys
    merchant_monthly_labels = [_month_label(mk) for mk in merchant_window_keys]
    merchant_stats: dict[str, dict] = {}
    for d_raw, merchant, category, amount in w_rows:
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            continue
        if d < start_365:
            continue
        merchant_name = (merchant or "Unknown").strip() or "Unknown"
        cat_name = (category or "Uncategorized").strip() or "Uncategorized"
        spend = abs(float(amount or 0.0))
        entry = merchant_stats.setdefault(merchant_name, {
            "merchant": merchant_name,
            "category": cat_name,
            "total_365": 0.0,
            "tx_count": 0,
            "recent_90": 0.0,
            "previous_90": 0.0,
            "monthly": {mk: 0.0 for mk in merchant_window_keys},
        })
        entry["total_365"] += spend
        entry["tx_count"] += 1
        month_key = _month_start(d).isoformat()
        if month_key in entry["monthly"]:
            entry["monthly"][month_key] += spend
        if d >= start_90:
            entry["recent_90"] += spend
        elif d >= (start_90 - timedelta(days=90)):
            entry["previous_90"] += spend

    top_merchants: list[dict] = []
    for merchant_name, stats in merchant_stats.items():
        sparkline = [round(float(stats["monthly"].get(mk) or 0.0), 2) for mk in merchant_window_keys]
        avg_ticket = stats["total_365"] / stats["tx_count"] if stats["tx_count"] else 0.0
        latest_month = sparkline[-1] if sparkline else 0.0
        baseline_vals = [v for v in sparkline[-4:-1] if v > 0]
        baseline_latest = mean(baseline_vals) if baseline_vals else 0.0
        trend_signal = _series_signal(latest_month, baseline_latest) if baseline_latest > 0 else "stable"
        recent_90 = float(stats["recent_90"] or 0.0)
        previous_90 = float(stats["previous_90"] or 0.0)
        pct_change = ((recent_90 - previous_90) / previous_90 * 100.0) if previous_90 > 0 else 0.0
        top_merchants.append({
            "merchant": merchant_name,
            "category": stats["category"],
            "total_365": round(float(stats["total_365"] or 0.0), 2),
            "tx_count": int(stats["tx_count"] or 0),
            "avg_ticket": round(avg_ticket, 2),
            "recent_90": round(recent_90, 2),
            "previous_90": round(previous_90, 2),
            "pct_change_90": round(pct_change, 1),
            "flagged": previous_90 > 100 and recent_90 > previous_90 * 1.2,
            "trend_signal": trend_signal,
            "latest_month": round(latest_month, 2),
            "sparkline": sparkline,
        })

    top_merchants.sort(
        key=lambda m: (float(m["total_365"]), int(bool(m["flagged"])), float(m["recent_90"])),
        reverse=True,
    )

    anomaly_alerts: list[dict] = []
    tx_by_category: dict[str, list[dict]] = {}
    for d_raw, merchant, category, amount in w_rows:
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            continue
        if d < start_365:
            continue
        cat_name = (category or "Uncategorized").strip() or "Uncategorized"
        tx_by_category.setdefault(cat_name, []).append({
            "date": d,
            "merchant": (merchant or "Unknown").strip() or "Unknown",
            "amount": abs(float(amount or 0.0)),
            "category": cat_name,
        })

    for cat_name, txns in tx_by_category.items():
        amounts = [t["amount"] for t in txns if t["amount"] > 0]
        if len(amounts) < 6:
            continue
        avg_amt = mean(amounts)
        std_amt = pstdev(amounts) if len(amounts) > 1 else 0.0
        if std_amt < 1:
            continue
        for txn in txns:
            zscore = (txn["amount"] - avg_amt) / std_amt
            if zscore < 2.2:
                continue
            severity = "high" if zscore >= 3.2 else "medium" if zscore >= 2.6 else "low"
            anomaly_alerts.append({
                "kind": "transaction_zscore",
                "severity": severity,
                "title": f"{txn['merchant']} unusually large",
                "context": f"{cat_name} transaction vs 12m category history",
                "date": txn["date"].isoformat(),
                "label": txn["date"].strftime("%b %d"),
                "merchant": txn["merchant"],
                "category": cat_name,
                "amount": round(txn["amount"], 2),
                "baseline": round(avg_amt, 2),
                "delta_pct": round(((txn["amount"] - avg_amt) / avg_amt * 100.0), 1) if avg_amt > 0 else 0.0,
                "zscore": round(zscore, 2),
            })

    for cat_name, monthly_map in category_monthly.items():
        monthly_values = [float(monthly_map.get(mk) or 0.0) for mk in month_keys]
        for idx in range(3, len(month_keys)):
            current = monthly_values[idx]
            prior = [v for v in monthly_values[idx - 3:idx] if v > 0]
            if len(prior) < 2:
                continue
            prior_avg = mean(prior)
            if prior_avg < 75 or current <= prior_avg * 1.45:
                continue
            ratio = current / prior_avg if prior_avg > 0 else 0.0
            severity = "high" if ratio >= 2.0 else "medium" if ratio >= 1.7 else "low"
            anomaly_alerts.append({
                "kind": "category_spike",
                "severity": severity,
                "title": f"{cat_name} monthly spike",
                "context": f"{labels[idx]} vs prior 3-month baseline",
                "date": month_keys[idx],
                "label": labels[idx],
                "merchant": "",
                "category": cat_name,
                "amount": round(current, 2),
                "baseline": round(prior_avg, 2),
                "delta_pct": round((ratio - 1.0) * 100.0, 1),
                "zscore": None,
            })

    anomaly_alerts.sort(
        key=lambda a: (
            _alert_severity_rank(str(a.get("severity") or "")),
            -int(str(a.get("date") or "0").replace("-", "")[:8] or 0),
            -(float(a.get("amount") or 0.0)),
        ),
    )
    anomaly_alerts = anomaly_alerts[:18]
    anomaly_counts = {
        "high": sum(1 for a in anomaly_alerts if a.get("severity") == "high"),
        "medium": sum(1 for a in anomaly_alerts if a.get("severity") == "medium"),
        "low": sum(1 for a in anomaly_alerts if a.get("severity") == "low"),
    }

    variable_outliers: list[dict] = []
    for c in variable_charts:
        for o in c["outliers"]:
            variable_outliers.append({
                "category": c["category"],
                "period_start": o["week_start"],
                "label": o["label"],
                "amount": o["amount"],
                "ucl": o["ucl"],
                "zscore": o["zscore"],
            })
    variable_outliers.sort(
        key=lambda o: (o["period_start"], abs(float(o["amount"] or 0.0))),
        reverse=True,
    )

    return {
        "overview": {
            "spend_365": round(spend_365, 2),
            "spend_90": round(spend_90, 2),
            "spend_30": round(spend_30, 2),
            "tx_30": tx_30,
            "avg_monthly_baseline": round(baseline, 2),
            "moving_latest_month": round(latest_moving, 2),
            "moving_signal": signal,
            "moving_share_90": round((moving_90 / spend_90 * 100), 1) if spend_90 > 0 else 0.0,
            "periods_in_chart": chart_months,
            "period_unit": "month",
            "baseline_window": baseline_window,
            "outlier_count": len(outliers),
            "variable_chart_count": len(variable_charts),
            "variable_outlier_count": len(variable_outliers),
        },
        "top_categories": top_categories,
        "terminal_summary": terminal_summary,
        "monthly_pnl": monthly_pnl,
        "trailing_pnl": [_trailing_pnl_stats(monthly_pnl, window) for window in (3, 6, 12) if len(monthly_pnl) >= window],
        "merchant_watchlist": {
            "month_labels": merchant_monthly_labels,
            "items": top_merchants[:12],
            "flagged_count": sum(1 for m in top_merchants[:12] if m.get("flagged")),
        },
        "anomaly_radar": {
            "counts": anomaly_counts,
            "items": anomaly_alerts,
        },
        "spending_treemap_90d": {
            "total": round(spend_90, 2),
            "categories": treemap_categories,
        },
        "shopping_control_chart": {
            "labels": labels,
            "period_starts": month_keys,
            "period_totals": moving_values,
            "all_period_totals": all_values,
            "cleaned_totals": ctrl["cleaned"],
            "rolling_avg": [round(v, 2) if v is not None else None for v in rolling_avg],
            "center_line": ctrl["center"],
            "ucl": ctrl["ucl"],
            "lcl": ctrl["lcl"],
            "outlier_points": ctrl["outlier_points"],
            "outliers": outliers,
            "period_unit": "month",
            "baseline_window": baseline_window,
        },
        "variable_category_charts": variable_charts,
        "variable_outliers": variable_outliers[:24],
    }


async def get_spending_by_category(year: int, month: int) -> list[dict]:
    """
    Sum spending by category for a given month.
    Returns list of {category, total} sorted by total desc.
    Only includes expenses (amount < 0 in Monarch convention means spending).
    """
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"

    async with db_connect() as db:
        cursor = await db.execute("""
            SELECT category, SUM(ABS(amount)) as total
            FROM transactions
            WHERE date >= ? AND date < ?
              AND amount < 0
            GROUP BY category
            ORDER BY total DESC
        """, (start, end))
        rows = await cursor.fetchall()

    return [{"category": r[0], "total": round(r[1], 2)} for r in rows]


async def get_transactions_for_month(year: int, month: int) -> list[dict]:
    """Return all transactions for a month, sorted by date desc."""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"

    async with db_connect() as db:
        cursor = await db.execute("""
            SELECT date, merchant, amount, category
            FROM transactions
            WHERE date >= ? AND date < ?
            ORDER BY date DESC
        """, (start, end))
        rows = await cursor.fetchall()

    return [
        {"date": r[0], "merchant": r[1], "amount": r[2], "category": r[3]}
        for r in rows
    ]


async def get_spending_trend_summary(weeks: int = 4) -> str:
    """
    Compare this week-to-date spending against recent prior weeks,
    excluding internal money-movement categories so the signal is more actionable.
    """
    compare_weeks = max(2, int(weeks or 4))
    today = date.today()
    start_this_week = today - timedelta(days=today.weekday())
    days_elapsed = (today - start_this_week).days + 1
    excluded_categories = EXCLUDED_SPENDING_CATEGORIES

    async with db_connect() as db:
        cur = await db.execute(
            """
            SELECT date, merchant, category, amount
            FROM transactions
            WHERE date >= ?
            ORDER BY date DESC
            """,
            ((start_this_week - timedelta(days=7 * compare_weeks)).isoformat(),),
        )
        rows = await cur.fetchall()

    def _bucket(start: date, end: date, partial_days: int | None = None) -> dict:
        total = 0.0
        tx_count = 0
        by_category: dict[str, float] = {}
        by_merchant: dict[str, float] = {}
        for d_raw, merchant, category, amount in rows:
            try:
                d = date.fromisoformat(d_raw)
            except ValueError:
                continue
            if not (start <= d <= end):
                continue
            if partial_days is not None and (d - start).days >= partial_days:
                continue
            amt = float(amount or 0.0)
            cat = (category or "Uncategorized").strip() or "Uncategorized"
            if amt >= 0 or cat in excluded_categories:
                continue
            spend = abs(amt)
            total += spend
            tx_count += 1
            by_category[cat] = by_category.get(cat, 0.0) + spend
            merch = (merchant or "Unknown").strip() or "Unknown"
            by_merchant[merch] = by_merchant.get(merch, 0.0) + spend
        top_categories = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]
        top_merchants = sorted(by_merchant.items(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "total": round(total, 2),
            "tx_count": tx_count,
            "top_categories": [(k, round(v, 2)) for k, v in top_categories],
            "top_merchants": [(k, round(v, 2)) for k, v in top_merchants],
        }

    current = _bucket(start_this_week, today, partial_days=days_elapsed)
    prior_same_days = []
    prior_full_weeks = []
    for i in range(1, compare_weeks + 1):
        start = start_this_week - timedelta(days=7 * i)
        end = start + timedelta(days=6)
        prior_same_days.append((start, _bucket(start, end, partial_days=days_elapsed)))
        prior_full_weeks.append((start, _bucket(start, end, partial_days=None)))

    same_day_avg = mean(p["total"] for _, p in prior_same_days) if prior_same_days else 0.0
    full_week_avg = mean(p["total"] for _, p in prior_full_weeks) if prior_full_weeks else 0.0
    delta_pct = ((current["total"] - same_day_avg) / same_day_avg * 100.0) if same_day_avg > 0 else 0.0

    if delta_pct <= -15:
        trend = "down"
    elif delta_pct >= 15:
        trend = "up"
    else:
        trend = "roughly flat"

    projected_week = round((current["total"] / days_elapsed) * 7, 2) if days_elapsed else current["total"]

    lines = [
        f"Spending trend — week of {start_this_week.isoformat()} through {today.isoformat()}",
        f"Real spend this week-to-date: ${current['total']:,.2f} across {current['tx_count']} transactions",
        f"Compared with the same {days_elapsed}-day stretch of the prior {compare_weeks} weeks: ${same_day_avg:,.2f} average ({delta_pct:+.1f}%)",
        f"Trend signal: {trend}",
        f"Projected full-week pace at current run rate: ${projected_week:,.2f} vs ${full_week_avg:,.2f} average full week",
        "",
        "Top categories this week:",
    ]

    if current["top_categories"]:
        for cat, total in current["top_categories"]:
            lines.append(f"- {cat}: ${total:,.2f}")
    else:
        lines.append("- No qualifying spend yet.")

    if current["top_merchants"]:
        lines.append("")
        lines.append("Top merchants this week:")
        for merchant, total in current["top_merchants"]:
            lines.append(f"- {merchant}: ${total:,.2f}")

    lines.append("")
    lines.append("Weekly comparison:")
    lines.append(f"- This week ({start_this_week.isoformat()} to {today.isoformat()}): ${current['total']:,.2f}")
    for start, payload in prior_full_weeks:
        lines.append(f"- Week of {start.isoformat()}: ${payload['total']:,.2f}")

    lines.append("")
    lines.append("Excludes Transfer, Credit Card Payment, and Loan Repayment so the signal reflects actual spending pace.")
    return "\n".join(lines)


def _pct_change(current: float, prior: float) -> float | None:
    if prior <= 0:
        return None
    return round(((current - prior) / prior) * 100.0, 1)


def _trend_direction(delta_pct: float | None, threshold: float = 8.0) -> str:
    if delta_pct is None:
        return "new"
    if delta_pct >= threshold:
        return "up"
    if delta_pct <= -threshold:
        return "down"
    return "flat"


async def build_weekly_financial_digest_data() -> dict:
    """
    Build structured spend-trend context for a weekly finance digest.
    Uses rolling 7-day and 30-day windows and keeps internal money movement separate.
    """
    today = date.today()
    current_7_start = today - timedelta(days=6)
    prior_7_start = current_7_start - timedelta(days=7)
    prior_7_end = current_7_start - timedelta(days=1)
    current_30_start = today - timedelta(days=29)
    prior_30_start = current_30_start - timedelta(days=30)
    prior_30_end = current_30_start - timedelta(days=1)
    history_start = today - timedelta(days=365)

    async with db_connect() as db:
        cur = await db.execute(
            """
            SELECT date, merchant, category, amount
            FROM transactions
            WHERE date >= ?
            ORDER BY date DESC, ABS(amount) DESC
            """,
            (history_start.isoformat(),),
        )
        rows = await cur.fetchall()

    transactions: list[dict] = []
    for d_raw, merchant, category, amount in rows:
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            continue
        amt = float(amount or 0.0)
        cat = (category or "Uncategorized").strip() or "Uncategorized"
        merch = (merchant or "Unknown").strip() or "Unknown"
        kind = "inflow"
        if amt < 0 and cat in EXCLUDED_SPENDING_CATEGORIES:
            kind = "excluded"
        elif amt < 0:
            kind = "actual"
        transactions.append({
            "date": d,
            "merchant": merch,
            "category": cat,
            "amount": amt,
            "spend": abs(amt),
            "kind": kind,
        })

    actual_txns = [t for t in transactions if t["kind"] == "actual"]
    excluded_txns = [t for t in transactions if t["kind"] == "excluded"]

    def _window_summary(items: list[dict], start: date, end: date, top_n: int = 6) -> dict:
        filtered = [t for t in items if start <= t["date"] <= end]
        total = round(sum(float(t["spend"]) for t in filtered), 2)
        day_count = max(1, (end - start).days + 1)
        by_category: dict[str, float] = {}
        by_merchant: dict[str, float] = {}
        for t in filtered:
            by_category[t["category"]] = by_category.get(t["category"], 0.0) + float(t["spend"])
            by_merchant[t["merchant"]] = by_merchant.get(t["merchant"], 0.0) + float(t["spend"])
        top_categories = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:top_n]
        top_merchants = sorted(by_merchant.items(), key=lambda x: x[1], reverse=True)[:top_n]
        top_transactions = [
            {
                "date": t["date"].isoformat(),
                "merchant": t["merchant"],
                "category": t["category"],
                "amount": round(float(t["spend"]), 2),
            }
            for t in sorted(filtered, key=lambda x: (x["spend"], x["date"].toordinal()), reverse=True)[:top_n]
        ]
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": day_count,
            "total": total,
            "daily_avg": round(total / day_count, 2),
            "tx_count": len(filtered),
            "category_totals": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: x[1], reverse=True)},
            "merchant_totals": {k: round(v, 2) for k, v in sorted(by_merchant.items(), key=lambda x: x[1], reverse=True)},
            "top_categories": [{"category": k, "amount": round(v, 2)} for k, v in top_categories],
            "top_merchants": [{"merchant": k, "amount": round(v, 2)} for k, v in top_merchants],
            "top_transactions": top_transactions,
        }

    current_7 = _window_summary(actual_txns, current_7_start, today, top_n=5)
    prior_7 = _window_summary(actual_txns, prior_7_start, prior_7_end, top_n=5)
    current_30 = _window_summary(actual_txns, current_30_start, today, top_n=8)
    prior_30 = _window_summary(actual_txns, prior_30_start, prior_30_end, top_n=8)
    excluded_7 = _window_summary(excluded_txns, current_7_start, today, top_n=3)
    excluded_prior_7 = _window_summary(excluded_txns, prior_7_start, prior_7_end, top_n=3)
    excluded_30 = _window_summary(excluded_txns, current_30_start, today, top_n=3)
    excluded_prior_30 = _window_summary(excluded_txns, prior_30_start, prior_30_end, top_n=3)

    def _category_movers(current_map: dict[str, float], prior_map: dict[str, float], limit: int = 6) -> dict:
        all_categories = set(current_map.keys()) | set(prior_map.keys())
        movers: list[dict] = []
        for category_name in all_categories:
            current_total = float(current_map.get(category_name) or 0.0)
            prior_total = float(prior_map.get(category_name) or 0.0)
            delta = round(current_total - prior_total, 2)
            if abs(delta) < 25:
                continue
            movers.append({
                "category": category_name,
                "current": round(current_total, 2),
                "prior": round(prior_total, 2),
                "delta": delta,
                "pct_change": _pct_change(current_total, prior_total),
                "direction": "up" if delta > 0 else "down",
            })

        movers.sort(key=lambda x: abs(float(x["delta"])), reverse=True)
        up = [m for m in movers if m["delta"] > 0][:limit]
        down = [m for m in movers if m["delta"] < 0][:limit]
        return {"all": movers[:limit], "up": up, "down": down}

    category_movers_7 = _category_movers(current_7["category_totals"], prior_7["category_totals"], limit=5)
    category_movers_30 = _category_movers(current_30["category_totals"], prior_30["category_totals"], limit=6)

    outliers: list[dict] = []
    for txn in actual_txns:
        if txn["date"] < current_30_start:
            continue
        baseline = [
            float(other["spend"])
            for other in actual_txns
            if other["category"] == txn["category"] and other["date"] < txn["date"]
        ]
        if len(baseline) < 6:
            continue
        avg_amt = mean(baseline)
        std_amt = pstdev(baseline) if len(baseline) > 1 else 0.0
        threshold = avg_amt + max(std_amt * 2.25, avg_amt * 0.6, 50.0)
        if float(txn["spend"]) < threshold:
            continue
        zscore = (float(txn["spend"]) - avg_amt) / (std_amt or 1.0)
        outliers.append({
            "date": txn["date"].isoformat(),
            "merchant": txn["merchant"],
            "category": txn["category"],
            "amount": round(float(txn["spend"]), 2),
            "baseline": round(avg_amt, 2),
            "delta_vs_baseline": round(float(txn["spend"]) - avg_amt, 2),
            "zscore": round(zscore, 2),
        })

    outliers.sort(key=lambda x: (float(x["zscore"]), float(x["amount"])), reverse=True)
    outliers = outliers[:8]

    return {
        "generated_on": today.isoformat(),
        "excluded_categories": sorted(EXCLUDED_SPENDING_CATEGORIES),
        "rolling_7": {
            "current": current_7,
            "prior": prior_7,
            "delta": round(current_7["total"] - prior_7["total"], 2),
            "delta_pct": _pct_change(current_7["total"], prior_7["total"]),
            "trend": _trend_direction(_pct_change(current_7["total"], prior_7["total"])),
        },
        "rolling_30": {
            "current": current_30,
            "prior": prior_30,
            "delta": round(current_30["total"] - prior_30["total"], 2),
            "delta_pct": _pct_change(current_30["total"], prior_30["total"]),
            "trend": _trend_direction(_pct_change(current_30["total"], prior_30["total"])),
        },
        "excluded_movement": {
            "rolling_7": {
                "current": excluded_7,
                "prior": excluded_prior_7,
                "delta": round(excluded_7["total"] - excluded_prior_7["total"], 2),
                "delta_pct": _pct_change(excluded_7["total"], excluded_prior_7["total"]),
            },
            "rolling_30": {
                "current": excluded_30,
                "prior": excluded_prior_30,
                "delta": round(excluded_30["total"] - excluded_prior_30["total"], 2),
                "delta_pct": _pct_change(excluded_30["total"], excluded_prior_30["total"]),
            },
        },
        "category_movers": {
            "rolling_7": category_movers_7,
            "rolling_30": category_movers_30,
        },
        "top_transactions": {
            "rolling_7": current_7["top_transactions"],
            "rolling_30": current_30["top_transactions"],
        },
        "outliers": outliers,
    }


# ── Budget targets ────────────────────────────────────────────────────────────

async def upsert_budget_targets(targets: list[dict]):
    """Store parsed budget targets. Each dict: {category, monthly_target, notes}."""
    now = datetime.utcnow().isoformat()
    async with db_connect() as db:
        for t in targets:
            await db.execute("""
                INSERT OR REPLACE INTO budget_targets (category, monthly_target, notes, updated_at)
                VALUES (?, ?, ?, ?)
            """, (t["category"], t.get("monthly_target"), t.get("notes", ""), now))
        await db.commit()


async def get_budget_targets() -> dict[str, float]:
    """Return {category: monthly_target} dict."""
    async with db_connect() as db:
        cursor = await db.execute(
            "SELECT category, monthly_target FROM budget_targets WHERE monthly_target IS NOT NULL"
        )
        rows = await cursor.fetchall()
    return {r[0]: r[1] for r in rows}


# ── Bills due ─────────────────────────────────────────────────────────────────

def get_bills_due(days_ahead: int = 14) -> list[dict]:
    """
    Return bills from RECURRING_BILLS due within the next `days_ahead` days.
    Attaches the next due date to each bill.
    """
    from recurring_bills import RECURRING_BILLS

    today = date.today()
    window_end = today + timedelta(days=days_ahead)
    due_soon = []

    for bill in RECURRING_BILLS:
        due_day = bill["due_day"]

        # Try this month's due date, then next month's
        for delta_months in (0, 1):
            m = today.month + delta_months
            y = today.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            # Clamp due_day to last day of that month
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            clamped_day = min(due_day, last_day)
            due_date = date(y, m, clamped_day)

            if today <= due_date <= window_end:
                due_soon.append({**bill, "due_date": due_date.isoformat()})
                break

    return sorted(due_soon, key=lambda b: b["due_date"])
