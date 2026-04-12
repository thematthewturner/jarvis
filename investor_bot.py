"""
Investor bot: paper-trade convex options scanner + journal.

This first version is intentionally conservative in scope:
- Uses a curated liquid large-cap universe instead of a full market sweep.
- Uses Yahoo Finance daily chart data over plain HTTP.
- Uses synthetic option pricing from historical volatility because live option
  chain endpoints are unreliable from this environment.

It is designed for Phase 1 / paper-trade style recommendations and journaling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis.investor")
ET = ZoneInfo("America/New_York")

YAHOO_USER_AGENT = "Mozilla/5.0 (Jarvis Investor Bot)"
DEFAULT_BANKROLL = 2000.0
MIN_TRADE_SCORE = 6.5
RISK_FREE_RATE = 0.045
DAILY_SCAN_TIMEOUT_SECONDS = 45
ANALYST_OVERLAY_TIMEOUT_SECONDS = 12

STATE_BANKROLL = "bankroll_current"
STATE_MANUAL_PAUSE = "manual_pause"

CURATED_UNIVERSE = [
    {"symbol": "NVDA", "sector_etf": "SMH", "themes": ["ai_infra"]},
    {"symbol": "AVGO", "sector_etf": "SMH", "themes": ["ai_infra"]},
    {"symbol": "AMD", "sector_etf": "SMH", "themes": ["ai_infra"]},
    {"symbol": "ANET", "sector_etf": "XLK", "themes": ["ai_infra"]},
    {"symbol": "VRT", "sector_etf": "XLI", "themes": ["ai_infra"]},
    {"symbol": "MSFT", "sector_etf": "XLK", "themes": ["ai_infra"]},
    {"symbol": "META", "sector_etf": "XLK", "themes": ["ai_infra"]},
    {"symbol": "AMZN", "sector_etf": "XLK", "themes": ["ai_infra"]},
    {"symbol": "PLTR", "sector_etf": "XLK", "themes": ["ai_infra", "defense"]},
    {"symbol": "LLY", "sector_etf": "XLV", "themes": ["glp1"]},
    {"symbol": "NVO", "sector_etf": "XLV", "themes": ["glp1"]},
    {"symbol": "LMT", "sector_etf": "XLI", "themes": ["defense"]},
    {"symbol": "RTX", "sector_etf": "XLI", "themes": ["defense"]},
    {"symbol": "GD", "sector_etf": "XLI", "themes": ["defense"]},
    {"symbol": "LNG", "sector_etf": "XLE", "themes": ["energy"]},
    {"symbol": "XOM", "sector_etf": "XLE", "themes": ["energy"]},
    {"symbol": "CVX", "sector_etf": "XLE", "themes": ["energy"]},
    {"symbol": "DHI", "sector_etf": "XHB", "themes": ["rates"]},
    {"symbol": "LEN", "sector_etf": "XHB", "themes": ["rates"]},
    {"symbol": "PLD", "sector_etf": "IYR", "themes": ["rates"]},
]

THEMES = {
    "ai_infra": {
        "label": "AI infrastructure buildout",
        "etf": "SMH",
        "catalyst": "AI capex + data center spend",
        "catalyst_days": 21,
        "tickers": ["NVDA", "AVGO", "AMD", "ANET", "VRT", "MSFT"],
    },
    "defense": {
        "label": "Defense / aerospace spending",
        "etf": "XLI",
        "catalyst": "Defense budget + backlog execution",
        "catalyst_days": 30,
        "tickers": ["LMT", "RTX", "GD", "PLTR"],
    },
    "glp1": {
        "label": "GLP-1 / obesity treatment expansion",
        "etf": "XLV",
        "catalyst": "Prescription growth + payer expansion",
        "catalyst_days": 28,
        "tickers": ["LLY", "NVO"],
    },
    "energy": {
        "label": "Energy security / LNG",
        "etf": "XLE",
        "catalyst": "LNG demand + export infrastructure",
        "catalyst_days": 35,
        "tickers": ["LNG", "XOM", "CVX"],
    },
    "rates": {
        "label": "Rate-sensitive recovery",
        "etf": "XHB",
        "catalyst": "Rate cuts / mortgage relief",
        "catalyst_days": 45,
        "tickers": ["DHI", "LEN", "PLD"],
    },
}

SECTOR_ETFS = ["SMH", "XLK", "XLI", "XLE", "XLV", "XHB", "IYR"]
REGIME_SYMBOLS = ["SPY", "^VIX", "^VIX3M", "HYG", "LQD"]


def _today_et() -> date:
    return datetime.now(ET).date()


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _round_strike(value: float) -> float:
    if value < 25:
        step = 0.5
    elif value < 75:
        step = 1.0
    elif value < 200:
        step = 2.5
    elif value < 400:
        step = 5.0
    else:
        step = 10.0
    return round(round(value / step) * step, 2)


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _load_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _extract_json_object(raw: str | None) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None

    parsed = _load_json(text, None)
    if isinstance(parsed, dict):
        return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    parsed = _load_json(text[start:end + 1], None)
    return parsed if isinstance(parsed, dict) else None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black_scholes_call(spot: float, strike: float, days_to_expiry: int, sigma: float) -> tuple[float, float]:
    t = max(days_to_expiry, 1) / 365.0
    sigma = max(0.10, min(sigma, 1.50))
    if spot <= 0 or strike <= 0:
        return 0.0, 0.0
    if sigma <= 0.0001:
        intrinsic = max(0.0, spot - strike)
        return intrinsic, 1.0 if spot > strike else 0.0

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    call = (spot * _norm_cdf(d1)) - (strike * math.exp(-RISK_FREE_RATE * t) * _norm_cdf(d2))
    delta = _norm_cdf(d1)
    return max(call, 0.01), max(0.0, min(delta, 1.0))


def _build_contract_model(
    spot: float,
    sigma: float,
    days_to_expiry: int,
    target_delta: float,
    risk_budget: float,
) -> dict[str, Any]:
    strike_guesses = []
    for pct in (0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.12, 0.15, 0.18, 0.20):
        strike = _round_strike(spot * (1.0 + pct))
        premium, delta = _black_scholes_call(spot, strike, days_to_expiry, sigma)
        strike_guesses.append(
            {
                "type": "call",
                "strike": strike,
                "premium": premium,
                "delta": delta,
                "contract_cost": premium * 100.0,
                "distance": abs(delta - target_delta),
            }
        )

    best_call = min(strike_guesses, key=lambda item: (item["distance"], item["contract_cost"]))
    affordable_calls = [c for c in strike_guesses if c["contract_cost"] <= risk_budget and c["delta"] >= 0.18]
    chosen_call = min(affordable_calls, key=lambda item: item["distance"]) if affordable_calls else best_call

    long_strike = _round_strike(max(spot, chosen_call["strike"]))
    short_strike = _round_strike(long_strike * 1.10)
    if short_strike <= long_strike:
        short_strike = _round_strike(long_strike + max(1.0, long_strike * 0.05))

    long_premium, long_delta = _black_scholes_call(spot, long_strike, days_to_expiry, sigma)
    short_premium, short_delta = _black_scholes_call(spot, short_strike, days_to_expiry, sigma)
    spread_premium = max(long_premium - short_premium, 0.05)
    spread_cost = spread_premium * 100.0
    spread_delta = max(long_delta - short_delta, 0.05)

    use_spread = spread_cost <= risk_budget and (
        chosen_call["contract_cost"] > risk_budget or spread_cost < chosen_call["contract_cost"] * 0.65
    )

    if use_spread:
        contract = {
            "instrument": "call_debit_spread",
            "label": f"{int(days_to_expiry)} DTE {long_strike:g}/{short_strike:g} call debit spread",
            "long_strike": long_strike,
            "short_strike": short_strike,
            "premium": spread_premium,
            "contract_cost": spread_cost,
            "delta": spread_delta,
            "days_to_expiry": days_to_expiry,
            "affordable": spread_cost <= risk_budget,
        }
    else:
        contract = {
            "instrument": "call",
            "label": f"{int(days_to_expiry)} DTE {chosen_call['strike']:g} call",
            "strike": chosen_call["strike"],
            "premium": chosen_call["premium"],
            "contract_cost": chosen_call["contract_cost"],
            "delta": chosen_call["delta"],
            "days_to_expiry": days_to_expiry,
            "affordable": chosen_call["contract_cost"] <= risk_budget,
        }

    contract["quantity"] = 1 if contract["affordable"] else 0
    contract["risk_budget"] = risk_budget
    return contract


def _http_get_json(url: str) -> dict:
    delay = 0.75
    last_err: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": YAHOO_USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except Exception as exc:
            last_err = exc
            import time
            time.sleep(delay)
            delay = min(delay * 2.0, 3.0)
    raise last_err if last_err else RuntimeError("Unknown HTTP fetch failure")


def _fetch_chart_sync(symbol: str, range_label: str = "6mo") -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_label}&interval=1d"
    data = _http_get_json(url)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"No chart data for {symbol}")

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[dict[str, Any]] = []
    for ts, opn, high, low, close, vol in zip(timestamps, opens, highs, lows, closes, volumes):
        if close is None or high is None or low is None:
            continue
        bars.append(
            {
                "ts": int(ts),
                "date": datetime.fromtimestamp(int(ts), ET).date().isoformat(),
                "open": float(opn) if opn is not None else float(close),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": int(vol or 0),
            }
        )

    if not bars:
        raise ValueError(f"No usable OHLC bars for {symbol}")

    return {
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "price": float(meta.get("regularMarketPrice") or bars[-1]["close"]),
        "bars": bars,
    }


async def _fetch_chart(symbol: str, range_label: str = "6mo") -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_chart_sync, symbol, range_label)


async def _fetch_many_charts(symbols: list[str], range_label: str = "6mo") -> dict[str, dict[str, Any]]:
    unique = sorted({s for s in symbols if s})
    results: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(4)

    async def _worker(sym: str):
        async with sem:
            try:
                results[sym] = await _fetch_chart(sym, range_label=range_label)
            except Exception as exc:
                logger.warning("Investor fetch failed for %s: %s", sym, exc)

    await asyncio.gather(*[_worker(sym) for sym in unique])
    return results


def _closes(bars: list[dict[str, Any]]) -> list[float]:
    return [float(bar["close"]) for bar in bars if bar.get("close") is not None]


def _volumes(bars: list[dict[str, Any]]) -> list[int]:
    return [int(bar.get("volume") or 0) for bar in bars]


def _sma(values: list[float], lookback: int) -> float | None:
    if len(values) < lookback:
        return None
    return mean(values[-lookback:])


def _roc(values: list[float], lookback: int = 20) -> float | None:
    if len(values) <= lookback:
        return None
    base = values[-lookback - 1]
    if not base:
        return None
    return ((values[-1] / base) - 1.0) * 100.0


def _rsi(values: list[float], lookback: int = 14) -> float | None:
    if len(values) <= lookback:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, curr in zip(values[-lookback - 1:-1], values[-lookback:]):
        change = curr - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _current_atr(bars: list[dict[str, Any]], lookback: int = 14) -> float | None:
    if len(bars) <= lookback:
        return None
    trs: list[float] = []
    prev_close: float | None = None
    for bar in bars[-lookback - 20:]:
        high = float(bar["high"])
        low = float(bar["low"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = float(bar["close"])
    if len(trs) < lookback:
        return None
    return mean(trs[-lookback:])


def _atr_baseline(bars: list[dict[str, Any]], lookback: int = 20) -> float | None:
    if len(bars) <= lookback:
        return None
    trs: list[float] = []
    prev_close: float | None = None
    for bar in bars[-lookback - 5:]:
        high = float(bar["high"])
        low = float(bar["low"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = float(bar["close"])
    return mean(trs[-lookback:]) if trs else None


def _realized_vol(values: list[float], lookback: int = 20) -> float | None:
    if len(values) <= lookback:
        return None
    returns: list[float] = []
    for prev, curr in zip(values[-lookback - 1:-1], values[-lookback:]):
        if prev <= 0 or curr <= 0:
            continue
        returns.append(math.log(curr / prev))
    if len(returns) < max(5, lookback // 2):
        return None
    std = pstdev(returns)
    return std * math.sqrt(252.0)


def _vol_rank(values: list[float], lookback: int = 20) -> float | None:
    if len(values) < 80:
        current = _realized_vol(values, lookback=lookback)
        return 50.0 if current is not None else None

    windows: list[float] = []
    for idx in range(lookback + 1, len(values) + 1):
        window = values[:idx]
        rv = _realized_vol(window, lookback=lookback)
        if rv is not None:
            windows.append(rv)
    current = windows[-1] if windows else None
    if current is None or len(windows) < 5:
        return 50.0 if current is not None else None
    lo = min(windows)
    hi = max(windows)
    if hi <= lo:
        return 50.0
    return ((current - lo) / (hi - lo)) * 100.0


def _percentile_rank(value: float | None, population: list[float]) -> float:
    if value is None or not population:
        return 50.0
    below = sum(1 for item in population if item <= value)
    return (below / len(population)) * 100.0


def _score_trend(percentile: float) -> float:
    if percentile >= 90:
        return 10.0
    if percentile >= 80:
        return 8.0
    if percentile >= 70:
        return 6.0
    if percentile >= 60:
        return 4.0
    return 2.0


def _score_volume(multiplier: float) -> float:
    if multiplier >= 3.0:
        return 10.0
    if multiplier >= 2.0:
        return 8.0
    if multiplier >= 1.5:
        return 6.0
    if multiplier >= 1.2:
        return 4.0
    return 2.0


def _score_vol(rank: float) -> float:
    if rank < 30:
        return 10.0
    if rank < 50:
        return 7.0
    if rank < 70:
        return 4.0
    return 2.0


def _score_liquidity(avg_dollar_volume: float) -> float:
    if avg_dollar_volume >= 2_000_000_000:
        return 10.0
    if avg_dollar_volume >= 1_000_000_000:
        return 8.0
    if avg_dollar_volume >= 500_000_000:
        return 6.0
    if avg_dollar_volume >= 200_000_000:
        return 4.0
    return 2.0


def _score_catalyst(days: int | None) -> float:
    if days is None:
        return 3.0
    if days <= 14:
        return 10.0
    if days <= 30:
        return 7.0
    if days <= 60:
        return 5.0
    return 3.0


def _score_sector(percentile: float) -> float:
    if percentile >= 80:
        return 10.0
    if percentile >= 60:
        return 7.0
    if percentile >= 40:
        return 5.0
    return 3.0


def _risk_stage(bankroll: float) -> dict[str, Any]:
    if bankroll < 4000:
        return {"label": "early", "risk_pct": 0.03, "max_positions": 2, "max_earnings": 1}
    if bankroll < 8000:
        return {"label": "growth", "risk_pct": 0.035, "max_positions": 3, "max_earnings": 1}
    return {"label": "scaled", "risk_pct": 0.04, "max_positions": 4, "max_earnings": 2}


async def init_investor_db():
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                play_type TEXT NOT NULL,
                option_desc TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                pnl REAL,
                pnl_pct REAL,
                bankroll_at_entry REAL NOT NULL,
                bankroll_at_exit REAL,
                score REAL,
                confidence TEXT,
                regime TEXT,
                exit_reason TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                regime TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]',
                recommended_ticker TEXT,
                recommended_score REAL,
                recommended_json TEXT,
                was_taken INTEGER NOT NULL DEFAULT 0,
                notion_url TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at)")
        now = _utc_iso()
        await db.execute(
            """
            INSERT OR IGNORE INTO investor_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (STATE_BANKROLL, f"{DEFAULT_BANKROLL:.2f}", now),
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO investor_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (STATE_MANUAL_PAUSE, "0", now),
        )
        await db.commit()


async def _state_get(key: str, default: str = "") -> str:
    await init_investor_db()
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM investor_state WHERE key = ? LIMIT 1",
            (key,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else default


async def _state_set(key: str, value: str):
    await init_investor_db()
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO investor_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _utc_iso()),
        )
        await db.commit()


async def _get_bankroll() -> float:
    raw = await _state_get(STATE_BANKROLL, f"{DEFAULT_BANKROLL:.2f}")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BANKROLL


async def _set_bankroll(value: float):
    await _state_set(STATE_BANKROLL, f"{max(0.0, value):.2f}")


async def _is_manual_pause() -> bool:
    return (await _state_get(STATE_MANUAL_PAUSE, "0")).strip() == "1"


async def set_bankroll_amount(amount: float) -> str:
    value = max(0.0, float(amount))
    await _set_bankroll(value)
    return f"Investor bankroll set to {_fmt_money(value)}."


async def set_investor_pause(paused: bool) -> str:
    await _state_set(STATE_MANUAL_PAUSE, "1" if paused else "0")
    return "Investor bot paused." if paused else "Investor bot resumed."


async def _get_today_scan() -> dict[str, Any] | None:
    return await _get_scan_by_date(_today_et().isoformat())


async def _get_scan_by_date(scan_date: str) -> dict[str, Any] | None:
    await init_investor_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_scans WHERE date = ? LIMIT 1",
            (scan_date,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return _hydrate_scan(dict(row))


async def _get_latest_scan() -> dict[str, Any] | None:
    await init_investor_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_scans ORDER BY date DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return _hydrate_scan(dict(row))


def _hydrate_scan(row: dict[str, Any]) -> dict[str, Any]:
    row["candidates"] = _load_json(row.pop("candidates_json", None), [])
    wrapped = _load_json(row.pop("recommended_json", None), None)
    if isinstance(wrapped, dict) and "recommendation" in wrapped:
        row["recommended"] = wrapped.get("recommendation")
        row["regime_detail"] = wrapped.get("regime_detail") or {"regime": row.get("regime", "unknown")}
        row["portfolio_guard"] = wrapped.get("portfolio_guard") or {}
        row["bankroll"] = float(wrapped.get("bankroll") or 0.0)
        row["analyst_overlay"] = wrapped.get("analyst_overlay")
    else:
        row["recommended"] = wrapped
    row["was_taken"] = bool(row.get("was_taken", 0))
    return row


async def _save_scan(scan: dict[str, Any]):
    await init_investor_db()
    candidates_json = json.dumps(scan.get("candidates") or [], ensure_ascii=True)
    recommended = scan.get("recommended")
    payload = {
        "recommendation": recommended,
        "regime_detail": scan.get("regime_detail") or {"regime": scan["regime"]},
        "portfolio_guard": scan.get("portfolio_guard") or {},
        "bankroll": scan.get("bankroll") or 0.0,
        "analyst_overlay": scan.get("analyst_overlay"),
    }
    recommended_json = json.dumps(payload, ensure_ascii=True) if recommended or scan.get("regime_detail") else None
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO daily_scans
                (date, regime, candidates_json, recommended_ticker, recommended_score,
                 recommended_json, was_taken, notion_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                regime = excluded.regime,
                candidates_json = excluded.candidates_json,
                recommended_ticker = excluded.recommended_ticker,
                recommended_score = excluded.recommended_score,
                recommended_json = excluded.recommended_json,
                notion_url = excluded.notion_url
            """,
            (
                scan["date"],
                scan["regime"],
                candidates_json,
                recommended.get("symbol") if recommended else None,
                float(recommended.get("score")) if recommended else None,
                recommended_json,
                0,
                scan.get("notion_url"),
                _utc_iso(),
            ),
        )
        await db.commit()


def _build_market_metrics(chart_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    roc_values: list[float] = []
    sector_rocs: list[float] = []

    for item in CURATED_UNIVERSE:
        symbol = item["symbol"]
        data = chart_map.get(symbol)
        if not data:
            continue
        closes = _closes(data["bars"])
        vols = _volumes(data["bars"])
        close = closes[-1]
        avg_vol20 = _safe_mean([float(v) for v in vols[-20:]]) or 0.0
        avg_dollar_volume = (_safe_mean(closes[-20:]) or close) * avg_vol20
        roc20 = _roc(closes, 20)
        if roc20 is not None:
            roc_values.append(roc20)
        metrics[symbol] = {
            "symbol": symbol,
            "name": data["name"],
            "price": close,
            "bars": data["bars"],
            "close": close,
            "ma20": _sma(closes, 20),
            "ma50": _sma(closes, 50),
            "rsi14": _rsi(closes, 14),
            "roc20": roc20,
            "avg_vol20": avg_vol20,
            "avg_dollar_volume": avg_dollar_volume,
            "today_volume": float(vols[-1]) if vols else 0.0,
            "atr14": _current_atr(data["bars"], 14),
            "atr20_baseline": _atr_baseline(data["bars"], 20),
            "vol_rank": _vol_rank(closes, 20),
            "realized_vol": _realized_vol(closes, 20),
            "sector_etf": item["sector_etf"],
            "themes": item["themes"],
        }

    sector_metrics: dict[str, dict[str, Any]] = {}
    for etf in SECTOR_ETFS:
        data = chart_map.get(etf)
        if not data:
            continue
        closes = _closes(data["bars"])
        roc20 = _roc(closes, 20)
        if roc20 is not None:
            sector_rocs.append(roc20)
        sector_metrics[etf] = {
            "close": closes[-1],
            "ma20": _sma(closes, 20),
            "ma50": _sma(closes, 50),
            "roc20": roc20,
        }

    for metric in metrics.values():
        metric["roc_percentile"] = _percentile_rank(metric["roc20"], roc_values)
        sector = sector_metrics.get(metric["sector_etf"]) or {}
        metric["sector_close"] = sector.get("close")
        metric["sector_ma20"] = sector.get("ma20")
        metric["sector_ma50"] = sector.get("ma50")
        metric["sector_roc20"] = sector.get("roc20")
        metric["sector_percentile"] = _percentile_rank(metric.get("sector_roc20"), sector_rocs)

    return {"stocks": metrics, "sectors": sector_metrics}


def _classify_regime(
    chart_map: dict[str, dict[str, Any]],
    stock_metrics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    spy = chart_map.get("SPY")
    vix = chart_map.get("^VIX")
    vix3m = chart_map.get("^VIX3M")
    hyg = chart_map.get("HYG")
    lqd = chart_map.get("LQD")

    spy_closes = _closes(spy["bars"]) if spy else []
    ma20_now = _sma(spy_closes, 20) or 0.0
    ma20_prev = mean(spy_closes[-25:-5]) if len(spy_closes) >= 25 else ma20_now
    spy_slope_pct = ((ma20_now / ma20_prev) - 1.0) * 100.0 if ma20_prev else 0.0

    vix_level = float(vix["price"]) if vix else 0.0
    vix3m_level = float(vix3m["price"]) if vix3m else 0.0
    term_structure = "contango" if vix_level and vix3m_level and vix_level < vix3m_level else "backwardation"

    breadth_items = [
        item for item in stock_metrics.values()
        if item.get("close") and item.get("ma50")
    ]
    breadth = (
        (sum(1 for item in breadth_items if float(item["close"]) > float(item["ma50"])) / len(breadth_items)) * 100.0
        if breadth_items else 50.0
    )

    hyg_closes = _closes(hyg["bars"]) if hyg else []
    lqd_closes = _closes(lqd["bars"]) if lqd else []
    credit_now = 0.0
    credit_prev = 0.0
    if hyg_closes and lqd_closes and len(hyg_closes) == len(lqd_closes):
        credit_now = hyg_closes[-1] / max(lqd_closes[-1], 0.0001)
        if len(hyg_closes) >= 21:
            credit_prev = hyg_closes[-21] / max(lqd_closes[-21], 0.0001)
    credit_trend = "up" if credit_now >= credit_prev else "down"

    if (
        spy_slope_pct > 0.10
        and breadth >= 60.0
        and vix_level < 18.0
        and term_structure == "contango"
        and credit_trend == "up"
    ):
        regime = "green"
        action = "Full allocation, all eligible play types active."
    elif (
        spy_slope_pct < -0.10
        and breadth < 45.0
        and (vix_level >= 28.0 or term_structure == "backwardation" or credit_trend == "down")
    ):
        regime = "red"
        action = "System paused. Cash only."
    else:
        regime = "yellow"
        action = "Half allocation. Momentum and strongest thematic only."

    return {
        "regime": regime,
        "action": action,
        "spy_slope_pct": spy_slope_pct,
        "vix_level": vix_level,
        "vix_bucket": "low" if vix_level < 18 else "normal" if vix_level < 28 else "elevated",
        "term_structure": term_structure,
        "breadth": breadth,
        "credit_trend": credit_trend,
    }


def _build_momentum_candidates(
    stock_metrics: dict[str, dict[str, Any]],
    regime: dict[str, Any],
) -> list[dict[str, Any]]:
    if regime["regime"] == "red":
        return []

    candidates: list[dict[str, Any]] = []
    for item in stock_metrics.values():
        bars = item["bars"]
        if len(bars) < 60:
            continue
        closes = _closes(bars)
        highs = [float(bar["high"]) for bar in bars]
        prior_high = max(highs[-21:-1]) if len(highs) >= 21 else None
        if prior_high is None or item["close"] <= prior_high:
            continue

        avg_vol20 = item.get("avg_vol20") or 0.0
        volume_multiple = (item.get("today_volume") or 0.0) / avg_vol20 if avg_vol20 else 0.0
        if volume_multiple < 1.5:
            continue

        rsi14 = item.get("rsi14")
        if rsi14 is None or rsi14 < 55.0 or rsi14 > 78.0:
            continue

        atr14 = item.get("atr14")
        atr20 = item.get("atr20_baseline")
        if atr14 is None or atr20 is None or atr14 <= atr20:
            continue

        sector_ma20 = item.get("sector_ma20")
        sector_close = item.get("sector_close")
        if sector_ma20 is None or sector_close is None or sector_close <= sector_ma20:
            continue

        vol_rank = float(item.get("vol_rank") or 50.0)
        factor_scores = {
            "trend": _score_trend(float(item["roc_percentile"])),
            "volume": _score_volume(volume_multiple),
            "vol": _score_vol(vol_rank),
            "liquidity": _score_liquidity(float(item.get("avg_dollar_volume") or 0.0)),
            "catalyst": _score_catalyst(7),
            "sector": _score_sector(float(item.get("sector_percentile") or 50.0)),
        }
        score = (
            factor_scores["trend"] * 0.25
            + factor_scores["volume"] * 0.20
            + factor_scores["vol"] * 0.15
            + factor_scores["liquidity"] * 0.20
            + factor_scores["catalyst"] * 0.10
            + factor_scores["sector"] * 0.10
        )

        candidates.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "play_type": "momentum",
                "setup": f"20-day breakout above {prior_high:.2f} on {volume_multiple:.1f}x volume",
                "score": round(score, 2),
                "price": item["close"],
                "roc20": item.get("roc20"),
                "roc_percentile": item.get("roc_percentile"),
                "volume_multiple": volume_multiple,
                "vol_rank": vol_rank,
                "sector_percentile": item.get("sector_percentile"),
                "realized_vol": float(item.get("realized_vol") or 0.35),
                "notes": [
                    f"RSI {rsi14:.1f}",
                    f"{item['sector_etf']} confirming above 20d MA",
                    "Breakout is the immediate catalyst",
                ],
            }
        )

    return candidates


def _build_thematic_candidates(
    stock_metrics: dict[str, dict[str, Any]],
    regime: dict[str, Any],
) -> list[dict[str, Any]]:
    if regime["regime"] == "red":
        return []

    allowed = {"green": True, "yellow": True}
    if not allowed.get(regime["regime"], False):
        return []

    out: list[dict[str, Any]] = []
    for theme_key, theme in THEMES.items():
        etf_symbol = theme["etf"]
        theme_pool = [stock_metrics[s] for s in theme["tickers"] if s in stock_metrics]
        if not theme_pool:
            continue

        best = None
        for item in theme_pool:
            sector_ma50 = item.get("sector_ma50")
            sector_close = item.get("sector_close")
            ma20 = item.get("ma20")
            if ma20 is None or item["close"] <= ma20:
                continue
            if sector_ma50 is None or sector_close is None or sector_close <= sector_ma50:
                continue
            vol_rank = float(item.get("vol_rank") or 50.0)
            if vol_rank > 65.0:
                continue

            factor_scores = {
                "trend": _score_trend(float(item.get("roc_percentile") or 50.0)),
                "volume": _score_volume(max(1.0, (item.get("today_volume") or 0.0) / max(item.get("avg_vol20") or 1.0, 1.0))),
                "vol": _score_vol(vol_rank),
                "liquidity": _score_liquidity(float(item.get("avg_dollar_volume") or 0.0)),
                "catalyst": _score_catalyst(int(theme["catalyst_days"])),
                "sector": _score_sector(float(item.get("sector_percentile") or 50.0)),
            }
            score = (
                factor_scores["trend"] * 0.25
                + factor_scores["volume"] * 0.20
                + factor_scores["vol"] * 0.15
                + factor_scores["liquidity"] * 0.20
                + factor_scores["catalyst"] * 0.10
                + factor_scores["sector"] * 0.10
            )
            candidate = {
                "symbol": item["symbol"],
                "name": item["name"],
                "play_type": "thematic",
                "setup": f"{theme['label']} — {theme['catalyst']}",
                "score": round(score, 2),
                "price": item["close"],
                "roc20": item.get("roc20"),
                "roc_percentile": item.get("roc_percentile"),
                "volume_multiple": max(1.0, (item.get("today_volume") or 0.0) / max(item.get("avg_vol20") or 1.0, 1.0)),
                "vol_rank": vol_rank,
                "sector_percentile": item.get("sector_percentile"),
                "realized_vol": float(item.get("realized_vol") or 0.30),
                "theme": theme_key,
                "catalyst_days": int(theme["catalyst_days"]),
                "notes": [
                    f"Theme ETF {etf_symbol} is above 50d MA",
                    f"Catalyst window about {theme['catalyst_days']} days",
                    "Sizing stays smaller if trend cools",
                ],
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate

        if best:
            out.append(best)

    return out


async def _portfolio_guard(bankroll: float) -> dict[str, Any]:
    await init_investor_db()
    today = _today_et()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE exit_price IS NULL"
        ) as cur:
            open_row = await cur.fetchone()
        async with db.execute(
            """
            SELECT COALESCE(SUM(pnl), 0)
            FROM trades
            WHERE closed_at IS NOT NULL
              AND date(closed_at) >= ?
            """,
            (week_start.isoformat(),),
        ) as cur:
            weekly_row = await cur.fetchone()
        async with db.execute(
            """
            SELECT COALESCE(SUM(pnl), 0)
            FROM trades
            WHERE closed_at IS NOT NULL
              AND date(closed_at) >= ?
            """,
            (month_start.isoformat(),),
        ) as cur:
            monthly_row = await cur.fetchone()
        async with db.execute(
            """
            SELECT pnl, closed_at
            FROM trades
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 10
            """
        ) as cur:
            recent_rows = await cur.fetchall()

    open_positions = int(open_row[0] if open_row else 0)
    weekly_pnl = float(weekly_row[0] if weekly_row else 0.0)
    monthly_pnl = float(monthly_row[0] if monthly_row else 0.0)

    consecutive_losses = 0
    last_closed_at: datetime | None = None
    for row in recent_rows:
        pnl = float(row[0] or 0.0)
        closed_at = str(row[1] or "")
        if not last_closed_at and closed_at:
            try:
                last_closed_at = datetime.fromisoformat(closed_at)
            except ValueError:
                last_closed_at = None
        if pnl < 0:
            consecutive_losses += 1
        else:
            break

    manual_pause = await _is_manual_pause()
    pause_reason = ""
    paused = False

    if manual_pause:
        paused = True
        pause_reason = "manual pause is active"
    elif weekly_pnl <= -(bankroll * 0.08):
        paused = True
        pause_reason = "weekly drawdown limit hit"
    elif monthly_pnl <= -(bankroll * 0.15):
        paused = True
        pause_reason = "monthly drawdown limit hit"
    elif consecutive_losses >= 5:
        if last_closed_at is None or (datetime.now() - last_closed_at) <= timedelta(days=2):
            paused = True
            pause_reason = "5 straight losses triggered the cooldown"

    return {
        "paused": paused,
        "pause_reason": pause_reason,
        "manual_pause": manual_pause,
        "open_positions": open_positions,
        "weekly_pnl": weekly_pnl,
        "monthly_pnl": monthly_pnl,
        "consecutive_losses": consecutive_losses,
    }


def _confidence(score: float) -> str:
    if score >= 8.0:
        return "high"
    if score >= 7.0:
        return "medium"
    return "low"


def _build_recommendation(
    regime: dict[str, Any],
    portfolio_guard: dict[str, Any],
    candidates: list[dict[str, Any]],
    bankroll: float,
) -> dict[str, Any] | None:
    if regime["regime"] == "red" or portfolio_guard["paused"]:
        return None
    if not candidates:
        return None

    ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)
    best = ranked[0]
    if best["score"] < MIN_TRADE_SCORE:
        return None

    stage = _risk_stage(bankroll)
    risk_pct = stage["risk_pct"] if regime["regime"] == "green" else stage["risk_pct"] / 2.0
    if portfolio_guard["open_positions"] >= stage["max_positions"]:
        return None

    dte = 45 if best["play_type"] == "momentum" else 90
    target_delta = 0.42 if best["play_type"] == "momentum" else 0.35
    sigma = max(0.20, min(float(best.get("realized_vol") or 0.35), 1.20))
    contract = _build_contract_model(
        spot=float(best["price"]),
        sigma=sigma,
        days_to_expiry=dte,
        target_delta=target_delta,
        risk_budget=bankroll * risk_pct,
    )

    entry = float(contract["premium"])
    hard_stop = max(entry * 0.40, 0.05)
    first_target = entry * (2.0 if best["play_type"] == "momentum" else 2.5)
    recommendation = dict(best)
    recommendation.update(
        {
            "risk_pct": risk_pct,
            "risk_budget": bankroll * risk_pct,
            "contract": contract,
            "entry_price": entry,
            "hard_stop": hard_stop,
            "first_target": first_target,
            "confidence": _confidence(float(best["score"])),
            "bankroll": bankroll,
            "stage": stage["label"],
            "notes": list(best.get("notes") or []) + [
                "Synthetic option quote from historical-vol model (paper-trade grade).",
                "Earnings-specific setup is not enabled until a clean earnings data feed is added.",
            ],
        }
    )
    return recommendation


async def _run_analyst_overlay(
    regime: dict[str, Any],
    portfolio_guard: dict[str, Any],
    recommendation: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    bankroll: float,
) -> dict[str, Any] | None:
    if not recommendation:
        return None

    try:
        from config import ANTHROPIC_API_KEY
    except Exception as exc:
        logger.warning("Investor analyst overlay unavailable: %s", exc)
        return None

    if not ANTHROPIC_API_KEY:
        return None

    contract = recommendation.get("contract") or {}
    context = {
        "bankroll": round(bankroll, 2),
        "regime": {
            "regime": regime.get("regime"),
            "spy_slope_pct": round(float(regime.get("spy_slope_pct") or 0.0), 2),
            "vix_level": round(float(regime.get("vix_level") or 0.0), 2),
            "breadth": round(float(regime.get("breadth") or 0.0), 1),
            "term_structure": regime.get("term_structure"),
            "credit_trend": regime.get("credit_trend"),
        },
        "guardrails": {
            "paused": bool(portfolio_guard.get("paused")),
            "open_positions": int(portfolio_guard.get("open_positions") or 0),
            "weekly_pnl": round(float(portfolio_guard.get("weekly_pnl") or 0.0), 2),
            "monthly_pnl": round(float(portfolio_guard.get("monthly_pnl") or 0.0), 2),
            "consecutive_losses": int(portfolio_guard.get("consecutive_losses") or 0),
        },
        "candidate": {
            "symbol": recommendation.get("symbol"),
            "play_type": recommendation.get("play_type"),
            "setup": recommendation.get("setup"),
            "score": round(float(recommendation.get("score") or 0.0), 2),
            "price": round(float(recommendation.get("price") or 0.0), 2),
            "roc20": round(float(recommendation.get("roc20") or 0.0), 2) if recommendation.get("roc20") is not None else None,
            "volume_multiple": round(float(recommendation.get("volume_multiple") or 0.0), 2),
            "vol_rank": round(float(recommendation.get("vol_rank") or 0.0), 1),
            "sector_percentile": round(float(recommendation.get("sector_percentile") or 0.0), 1),
            "entry_price": round(float(recommendation.get("entry_price") or 0.0), 2),
            "hard_stop": round(float(recommendation.get("hard_stop") or 0.0), 2),
            "first_target": round(float(recommendation.get("first_target") or 0.0), 2),
            "risk_pct": round(float(recommendation.get("risk_pct") or 0.0) * 100.0, 2),
            "risk_budget": round(float(recommendation.get("risk_budget") or 0.0), 2),
            "contract": {
                "instrument": contract.get("instrument"),
                "label": contract.get("label"),
                "delta": round(float(contract.get("delta") or 0.0), 2),
                "days_to_expiry": int(contract.get("days_to_expiry") or 0),
                "contract_cost": round(float(contract.get("contract_cost") or 0.0), 2),
                "quantity": int(contract.get("quantity") or 0),
            },
            "notes": list(recommendation.get("notes") or [])[:5],
        },
        "runner_up": [
            {
                "symbol": item.get("symbol"),
                "play_type": item.get("play_type"),
                "score": round(float(item.get("score") or 0.0), 2),
                "setup": item.get("setup"),
            }
            for item in candidates[:3]
        ],
        "constraints": [
            "Use only the supplied data. Do not invent news or catalysts.",
            "This is a paper-trade overlay. The base scanner has already passed the setup through deterministic rules.",
            "Veto only when the setup is fragile, low-quality, or likely not worth today's single slot.",
        ],
    }

    system_prompt = (
        "You are Jarvis's second-pass investor analyst. "
        "Review the deterministic convex options setup and decide whether it deserves today's single slot. "
        "Be conservative. Prefer veto over weak conviction. "
        "Return JSON only with keys: decision, confidence, summary, risks, adjustments."
    )

    user_prompt = (
        "Review this candidate and return JSON only.\n"
        "decision must be one of: approve, watch, veto.\n"
        "confidence must be one of: high, medium, low.\n"
        "summary should be one sentence.\n"
        "risks should be an array of 1-3 short strings.\n"
        "adjustments should be an array of 0-3 short strings.\n\n"
        f"{json.dumps(context, ensure_ascii=True)}"
    )

    try:
        from llm import complete_json

        parsed = await complete_json(
            model="report",
            max_tokens=300,
            system=system_prompt,
            user_content=user_prompt,
            timeout_seconds=ANALYST_OVERLAY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Investor analyst overlay failed; using deterministic pick: %s", exc)
        return {
            "decision": "watch",
            "confidence": "low",
            "summary": "Analyst overlay unavailable. Using deterministic rules only.",
            "risks": ["Second-pass AI review did not complete cleanly."],
            "adjustments": [],
            "status": "error",
        }

    decision = str(parsed.get("decision") or "watch").strip().lower()
    if decision not in {"approve", "watch", "veto"}:
        decision = "watch"

    confidence = str(parsed.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    summary = str(parsed.get("summary") or "").strip() or "Second-pass analyst review completed."
    risks = [str(item).strip() for item in parsed.get("risks") or [] if str(item).strip()][:3]
    adjustments = [str(item).strip() for item in parsed.get("adjustments") or [] if str(item).strip()][:3]

    return {
        "decision": decision,
        "confidence": confidence,
        "summary": summary,
        "risks": risks,
        "adjustments": adjustments,
        "status": "ok",
    }


def _scan_narrative(
    regime: dict[str, Any],
    guard: dict[str, Any],
    recommendation: dict[str, Any] | None,
    bankroll: float,
    analyst_overlay: dict[str, Any] | None = None,
) -> str:
    regime_name = str(regime.get("regime", "unknown"))
    regime_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(regime_name, "⚪")
    header = [
        f"📈 INVESTOR BOT: {regime_emoji} {regime_name.upper()}",
        (
            f"SPY trend: {_fmt_pct(float(regime.get('spy_slope_pct') or 0.0))} | "
            f"VIX: {float(regime.get('vix_level') or 0.0):.1f} ({regime.get('vix_bucket', 'n/a')}) | "
            f"Breadth: {float(regime.get('breadth') or 0.0):.0f}% > 50d"
        ),
        f"Risk tone: {regime.get('term_structure', 'n/a')} | Credit: {regime.get('credit_trend', 'n/a')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    footer = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        (
            f"Bankroll: {_fmt_money(bankroll)} | "
            f"Open positions: {guard['open_positions']} | "
            f"Weekly P&L: {_fmt_money(guard['weekly_pnl'])}"
        ),
    ]

    if guard["paused"]:
        body = [
            "System is paused.",
            f"Reason: {guard['pause_reason']}.",
            "No new convex positions today.",
        ]
        return "\n".join(header + body + footer)

    if not recommendation:
        body = ["No play today."]
        if analyst_overlay and analyst_overlay.get("decision") == "veto":
            body.append(f"Analyst veto: {analyst_overlay.get('summary', 'Second-pass review rejected the setup.')}")
            for risk in (analyst_overlay.get("risks") or [])[:2]:
                body.append(f"- {risk}")
            body.append("Cash is a valid position.")
        else:
            body.extend([
                "No momentum/thematic candidate cleared the 6.5 score gate without breaking risk rules.",
                "Cash is a valid position.",
            ])
        return "\n".join(header + body + footer)

    contract = recommendation["contract"]
    cost = float(contract["contract_cost"])
    cost_text = f"{contract['premium']:.2f} (${cost:,.0f}/contract)"
    qty_text = "BUY 1" if contract["quantity"] >= 1 else "PAPER ONLY (over bankroll cap)"
    body = [
        "🎯 PLAY OF THE DAY",
        f"Ticker: {recommendation['symbol']}",
        f"Setup: {recommendation['setup']}",
        f"Score: {recommendation['score']:.1f} / 10",
        "",
        "📋 OPTION",
        f"Contract: {recommendation['symbol']} {contract['label']}",
        f"Delta: {contract['delta']:.2f} | Vol proxy rank: {recommendation['vol_rank']:.0f}% | DTE: {contract['days_to_expiry']}",
        f"Est. cost: {cost_text} → {qty_text}",
        "",
        "💰 RISK / REWARD",
        f"Entry: {recommendation['entry_price']:.2f}",
        f"Max loss: {cost_text} ({recommendation['risk_pct'] * 100:.1f}% risk budget)",
        f"Target 1: {recommendation['first_target']:.2f} ({'100%' if recommendation['play_type'] == 'momentum' else '150%'})",
        f"Trail stop: 40% from peak after first scale-out",
        f"Time stop: Close with <= 15 DTE if under target",
        f"Hard stop: Exit near {recommendation['hard_stop']:.2f}",
        "",
        "⚠️ NOTES",
    ]
    for note in recommendation.get("notes", [])[:5]:
        body.append(f"- {note}")
    if analyst_overlay:
        body.append(f"- Analyst overlay ({analyst_overlay['decision']}): {analyst_overlay['summary']}")
        for risk in (analyst_overlay.get("risks") or [])[:2]:
            body.append(f"- Risk: {risk}")
    body.append("")
    body.append(f"Confidence: {recommendation['confidence'].upper()}")

    return "\n".join(header + body + footer)


def _briefing_narrative(scan: dict[str, Any]) -> str:
    regime = scan.get("regime", "unknown").upper()
    recommended = scan.get("recommended")
    overlay = scan.get("analyst_overlay")
    if not recommended:
        if overlay and overlay.get("decision") == "veto":
            return f"Regime {regime}. No investor play: analyst vetoed the top setup ({overlay.get('summary', 'fragile setup')})."
        return f"Regime {regime}. No investor play cleared the score/risk gate."
    contract = recommended.get("contract") or {}
    qty = "paper-only" if contract.get("quantity", 0) < 1 else "1x"
    text = (
        f"{recommended['symbol']} {recommended['play_type']} ({recommended['score']:.1f}/10). "
        f"{recommended['setup']}. "
        f"Use {qty} {contract.get('label', 'call setup')} at est. {recommended['entry_price']:.2f}."
    )
    if overlay:
        text = f"{text} Analyst: {overlay['decision']} ({overlay['summary']})."
    return text


async def _save_scan_to_notion(scan: dict[str, Any]) -> str | None:
    recommended = scan.get("recommended")
    if not recommended:
        return None
    title = f"Investor Scan — {scan['date']}"
    content = _scan_narrative(
        scan["regime_detail"],
        scan["portfolio_guard"],
        recommended,
        scan["bankroll"],
        scan.get("analyst_overlay"),
    )
    try:
        from notion_services import save_notion_note
        await save_notion_note({"title": title, "content": content, "category": "note"})
        return None
    except Exception as exc:
        logger.warning("Investor scan Notion save skipped: %s", exc)
        return None


async def _build_scan_result() -> dict[str, Any]:
    await init_investor_db()
    bankroll = await _get_bankroll()
    chart_symbols = REGIME_SYMBOLS + SECTOR_ETFS + [item["symbol"] for item in CURATED_UNIVERSE]
    chart_map = await _fetch_many_charts(chart_symbols, range_label="6mo")
    if not chart_map.get("SPY") or not chart_map.get("^VIX"):
        raise RuntimeError("Investor scan could not fetch core market regime data")

    metrics = _build_market_metrics(chart_map)
    regime = _classify_regime(chart_map, metrics["stocks"])
    guard = await _portfolio_guard(bankroll)
    candidates = _build_momentum_candidates(metrics["stocks"], regime) + _build_thematic_candidates(metrics["stocks"], regime)
    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
    recommendation = _build_recommendation(regime, guard, candidates, bankroll)
    analyst_overlay = await _run_analyst_overlay(regime, guard, recommendation, candidates, bankroll)
    if recommendation and analyst_overlay:
        if analyst_overlay.get("decision") == "veto":
            recommendation = None
        else:
            recommendation = dict(recommendation)
            recommendation["analyst_overlay"] = analyst_overlay
            notes = list(recommendation.get("notes") or [])
            notes.append(f"Analyst overlay ({analyst_overlay['decision']}): {analyst_overlay['summary']}")
            for risk in (analyst_overlay.get("risks") or [])[:2]:
                notes.append(f"Risk: {risk}")
            recommendation["notes"] = notes

    return {
        "date": _today_et().isoformat(),
        "regime": regime["regime"],
        "regime_detail": regime,
        "portfolio_guard": guard,
        "bankroll": bankroll,
        "candidates": candidates[:8],
        "recommended": recommendation,
        "analyst_overlay": analyst_overlay,
        "notion_url": None,
    }


async def get_or_run_investor_scan(force_refresh: bool = False, save_to_notion: bool = True) -> dict[str, Any]:
    if not force_refresh:
        existing = await _get_today_scan()
        if existing:
            bankroll = await _get_bankroll()
            guard = await _portfolio_guard(bankroll)
            existing["bankroll"] = bankroll or existing.get("bankroll") or 0.0
            existing["portfolio_guard"] = guard
            existing["regime_detail"] = existing.get("regime_detail") or {"regime": existing.get("regime", "unknown")}
            return existing

    scan = await asyncio.wait_for(_build_scan_result(), timeout=DAILY_SCAN_TIMEOUT_SECONDS)
    if save_to_notion:
        scan["notion_url"] = await _save_scan_to_notion(scan)
    await _save_scan(scan)
    return scan


async def run_investor_scan(force_refresh: bool = False, notify_when_no_play: bool = True) -> str:
    scan = await get_or_run_investor_scan(force_refresh=force_refresh, save_to_notion=True)
    text = _scan_narrative(
        scan["regime_detail"],
        scan["portfolio_guard"],
        scan.get("recommended"),
        scan["bankroll"],
        scan.get("analyst_overlay"),
    )
    if scan.get("recommended") is None and not notify_when_no_play:
        return ""
    return text


async def get_investor_briefing_text() -> str:
    scan = await get_or_run_investor_scan(force_refresh=False, save_to_notion=False)
    return _briefing_narrative(scan)


async def format_investor_status() -> str:
    await init_investor_db()
    bankroll = await _get_bankroll()
    guard = await _portfolio_guard(bankroll)
    latest = await _get_latest_scan()

    stage = _risk_stage(bankroll)
    lines = [
        "Investor Bot Status",
        f"Bankroll: {_fmt_money(bankroll)}",
        f"Stage: {stage['label']} | Max risk/trade: {stage['risk_pct'] * 100:.1f}% | Max open positions: {stage['max_positions']}",
        f"Open positions: {guard['open_positions']}",
        f"Weekly P&L: {_fmt_money(guard['weekly_pnl'])}",
        f"Monthly P&L: {_fmt_money(guard['monthly_pnl'])}",
        f"Consecutive losses: {guard['consecutive_losses']}",
        f"Manual pause: {'ON' if guard['manual_pause'] else 'OFF'}",
    ]
    if guard["paused"]:
        lines.append(f"Trade gate: PAUSED ({guard['pause_reason']})")
    else:
        lines.append("Trade gate: ACTIVE")

    if latest:
        lines.append("")
        lines.append(f"Latest scan: {latest['date']} ({latest['regime'].upper()})")
        if latest.get("recommended"):
            rec = latest["recommended"]
            lines.append(f"Play: {rec['symbol']} {rec['play_type']} @ score {rec['score']:.1f}")
            overlay = latest.get("analyst_overlay")
            if overlay:
                lines.append(f"Analyst overlay: {overlay['decision'].upper()} — {overlay['summary']}")
        elif latest.get("analyst_overlay", {}).get("decision") == "veto":
            overlay = latest["analyst_overlay"]
            lines.append(f"Play: no trade (analyst veto — {overlay['summary']})")
        else:
            lines.append("Play: no trade")

    return "\n".join(lines)


async def log_trade_fill(ticker: str, entry_price: float, quantity: int = 1) -> str:
    await init_investor_db()
    ticker = ticker.upper().strip()
    entry_price = float(entry_price)
    quantity = max(1, int(quantity))
    bankroll = await _get_bankroll()
    today_scan = await _get_today_scan()
    recommended = today_scan.get("recommended") if today_scan else None
    if not recommended or recommended.get("symbol") != ticker:
        latest = await _get_latest_scan()
        recommended = latest.get("recommended") if latest and latest.get("recommended", {}).get("symbol") == ticker else None

    play_type = recommended.get("play_type") if recommended else "manual"
    option_desc = (
        f"{ticker} {recommended['contract']['label']}"
        if recommended and recommended.get("contract")
        else f"{ticker} manual option"
    )
    score = float(recommended.get("score")) if recommended else None
    confidence = recommended.get("confidence") if recommended else "low"
    regime = today_scan.get("regime") if today_scan else (recommended.get("regime") if recommended else "manual")
    notes = recommended.get("setup") if recommended else "Manual fill"

    async with db_connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO trades
                (date, ticker, play_type, option_desc, entry_price, quantity,
                 bankroll_at_entry, score, confidence, regime, notes,
                 created_at, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _today_et().isoformat(),
                ticker,
                play_type,
                option_desc,
                entry_price,
                quantity,
                bankroll,
                score,
                confidence,
                regime,
                notes,
                _utc_iso(),
                _utc_iso(),
            ),
        )
        if today_scan and today_scan.get("recommended") and today_scan["recommended"]["symbol"] == ticker:
            await db.execute(
                "UPDATE daily_scans SET was_taken = 1 WHERE date = ?",
                (today_scan["date"],),
            )
        await db.commit()

    max_loss = entry_price * 100.0 * quantity
    return (
        f"Logged fill: {ticker} x{quantity} @ {entry_price:.2f}\n"
        f"Max premium at risk: {_fmt_money(max_loss)}\n"
        f"Bankroll unchanged until close: {_fmt_money(bankroll)}"
    )


async def log_trade_close(ticker: str, exit_price: float, exit_reason: str = "manual") -> str:
    await init_investor_db()
    ticker = ticker.upper().strip()
    exit_price = float(exit_price)
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM trades
            WHERE ticker = ?
              AND exit_price IS NULL
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (ticker,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"No open investor trade found for {ticker}")

        trade = dict(row)
        entry_price = float(trade["entry_price"])
        quantity = int(trade["quantity"] or 1)
        pnl = (exit_price - entry_price) * 100.0 * quantity
        pnl_pct = ((exit_price / entry_price) - 1.0) * 100.0 if entry_price else 0.0
        bankroll_at_exit = await _get_bankroll() + pnl

        await db.execute(
            """
            UPDATE trades
            SET exit_price = ?,
                pnl = ?,
                pnl_pct = ?,
                bankroll_at_exit = ?,
                exit_reason = ?,
                closed_at = ?
            WHERE id = ?
            """,
            (
                exit_price,
                pnl,
                pnl_pct,
                bankroll_at_exit,
                exit_reason,
                _utc_iso(),
                int(trade["id"]),
            ),
        )
        await db.commit()

    await _set_bankroll(bankroll_at_exit)
    return (
        f"Closed {ticker} @ {exit_price:.2f}\n"
        f"P&L: {_fmt_money(pnl)} ({_fmt_pct(pnl_pct)})\n"
        f"Updated bankroll: {_fmt_money(bankroll_at_exit)}"
    )


async def mark_trade_skipped() -> str:
    await init_investor_db()
    today = _today_et().isoformat()
    scan = await _get_scan_by_date(today)
    if not scan:
        return "No investor scan found for today."

    async with db_connect(DB_PATH) as db:
        await db.execute(
            "UPDATE daily_scans SET was_taken = 0 WHERE date = ?",
            (today,),
        )
        await db.commit()
    return f"Logged skip for {today}."


async def get_trade_review(limit: int = 10) -> str:
    await init_investor_db()
    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT ticker, play_type, entry_price, exit_price, quantity, pnl, pnl_pct, closed_at
            FROM trades
            WHERE exit_price IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return "No closed investor trades yet."

    wins = [r for r in rows if float(r.get("pnl") or 0.0) > 0]
    losses = [r for r in rows if float(r.get("pnl") or 0.0) <= 0]
    win_rate = (len(wins) / len(rows)) * 100.0
    avg_win = mean([float(r["pnl"]) for r in wins]) if wins else 0.0
    avg_loss = mean([abs(float(r["pnl"])) for r in losses]) if losses else 0.0
    expectancy = (len(wins) / len(rows) * avg_win) - (len(losses) / len(rows) * avg_loss)

    lines = [
        f"Investor review (last {len(rows)} closed trades)",
        f"Win rate: {win_rate:.1f}%",
        f"Avg winner: {_fmt_money(avg_win)}",
        f"Avg loser: {_fmt_money(-avg_loss) if avg_loss else '$0.00'}",
        f"Expectancy / trade: {_fmt_money(expectancy)}",
        "",
        "Recent trades:",
    ]
    for row in rows[:10]:
        lines.append(
            f"- {row['ticker']} {row['play_type']} | "
            f"{row['entry_price']:.2f} → {row['exit_price']:.2f} | "
            f"{_fmt_money(float(row['pnl'] or 0.0))} ({_fmt_pct(float(row['pnl_pct'] or 0.0))})"
        )
    return "\n".join(lines)


async def get_investor_dashboard(force_refresh: bool = False) -> dict[str, Any]:
    await init_investor_db()
    scan = await get_or_run_investor_scan(force_refresh=force_refresh, save_to_notion=False)
    bankroll = float(scan.get("bankroll") or await _get_bankroll())
    guard = scan.get("portfolio_guard") or await _portfolio_guard(bankroll)

    async with db_connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT ticker, play_type, option_desc, entry_price, quantity, score, confidence, opened_at
            FROM trades
            WHERE exit_price IS NULL
            ORDER BY opened_at DESC
            LIMIT 5
            """
        ) as cur:
            open_rows = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            """
            SELECT ticker, play_type, entry_price, exit_price, quantity, pnl, pnl_pct, exit_reason, closed_at
            FROM trades
            WHERE exit_price IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 8
            """
        ) as cur:
            closed_rows = [dict(r) for r in await cur.fetchall()]

    wins = [r for r in closed_rows if float(r.get("pnl") or 0.0) > 0]
    losses = [r for r in closed_rows if float(r.get("pnl") or 0.0) <= 0]
    win_rate = (len(wins) / len(closed_rows) * 100.0) if closed_rows else 0.0
    avg_win = mean([float(r["pnl"]) for r in wins]) if wins else 0.0
    avg_loss = mean([abs(float(r["pnl"])) for r in losses]) if losses else 0.0
    expectancy = (
        (len(wins) / len(closed_rows) * avg_win) - (len(losses) / len(closed_rows) * avg_loss)
        if closed_rows else 0.0
    )

    return {
        "scan_date": scan.get("date"),
        "regime": scan.get("regime"),
        "regime_detail": scan.get("regime_detail") or {},
        "recommended": scan.get("recommended"),
        "analyst_overlay": scan.get("analyst_overlay"),
        "candidates": (scan.get("candidates") or [])[:6],
        "bankroll": bankroll,
        "guard": guard,
        "open_positions": open_rows,
        "recent_closed": closed_rows,
        "review": {
            "closed_count": len(closed_rows),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
        },
    }
