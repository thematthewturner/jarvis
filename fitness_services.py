"""
Fitness services for Jarvis.

WHOOP API v2 (OAuth2):
  - Base URL: https://api.prod.whoop.com/developer/v1
  - Tokens stored in data/whoop_tokens.json
  - Auth flow: run `python fitness_services.py auth` to authorize

Manual tracking:
  - PRs: exercise, value, unit, date
  - Workouts: WOD description, result, Rx/scaled, notes

Setup (one-time):
  1. Create app at developer.whoop.com
  2. Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env
  3. Set WHOOP_REDIRECT_URI to http://localhost:8888/callback (or any valid URI)
  4. Run: python fitness_services.py auth
  5. Complete OAuth flow in browser — tokens saved automatically
"""
import json
import logging
import os
import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis.fitness")

WHOOP_BASE     = "https://api.prod.whoop.com/developer"
WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
TOKENS_PATH    = Path(__file__).parent / "data" / "whoop_tokens.json"
WHOOP_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


# ── Database ───────────────────────────────────────────────────────────────────

async def init_fitness_db():
    async with db_connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fitness_whoop_daily (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT    NOT NULL UNIQUE,   -- YYYY-MM-DD
                recovery_score      INTEGER,   -- 0-100
                hrv                 REAL,      -- ms (rMSSD)
                resting_hr          INTEGER,   -- bpm
                sleep_performance   INTEGER,   -- 0-100%
                sleep_duration_hrs  REAL,
                strain              REAL,      -- 0-21 WHOOP strain scale
                calories            INTEGER,
                vo2max              REAL,
                synced_at           TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fitness_prs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise   TEXT    NOT NULL,
                value      REAL    NOT NULL,
                unit       TEXT    NOT NULL DEFAULT 'lbs',
                logged_at  TEXT    NOT NULL,
                notes      TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fitness_workouts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at   TEXT    NOT NULL,
                type        TEXT    DEFAULT 'crossfit',
                description TEXT,
                result      TEXT,
                rx          INTEGER DEFAULT 0,
                notes       TEXT
            )
        """)
        await db.commit()


# ── WHOOP token management ─────────────────────────────────────────────────────

def _load_tokens() -> dict:
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text())
    return {}


def _save_tokens(tokens: dict):
    TOKENS_PATH.parent.mkdir(exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    if os.name != "nt":
        os.chmod(TOKENS_PATH, 0o600)


def _is_transient_whoop_status(status: int) -> bool:
    return status in WHOOP_TRANSIENT_STATUSES


async def _refresh_access_token(tokens: dict) -> dict:
    import aiohttp
    client_id     = os.getenv("WHOOP_CLIENT_ID", "")
    client_secret = os.getenv("WHOOP_CLIENT_SECRET", "")
    timeout = aiohttp.ClientTimeout(total=20)
    for attempt in range(3):
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resp = await session.post(WHOOP_TOKEN_URL, data={
                "grant_type":    "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id":     client_id,
                "client_secret": client_secret,
            })
            if _is_transient_whoop_status(resp.status) and attempt < 2:
                await asyncio.sleep((2 ** attempt) + random.random() * 0.25)
                continue
            resp.raise_for_status()
            new_tokens = await resp.json()
            break
    tokens.update(new_tokens)
    _save_tokens(tokens)
    return tokens


async def _get_valid_token() -> str:
    """Return a valid access token, refreshing if needed."""
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError("WHOOP not authorized. Run: python fitness_services.py auth")

    # Refresh if expires_in suggests we're close or token is missing
    expires_at = tokens.get("expires_at", 0)
    now = datetime.now(timezone.utc).timestamp()
    if now >= expires_at - 60:
        tokens = await _refresh_access_token(tokens)
        # Store absolute expiry if API returns expires_in
        if "expires_in" in tokens and "expires_at" not in tokens:
            tokens["expires_at"] = now + tokens["expires_in"]
            _save_tokens(tokens)

    return tokens["access_token"]


# ── WHOOP API calls ────────────────────────────────────────────────────────────

async def _whoop_get(path: str, params: dict = None) -> dict:
    import aiohttp
    url = f"{WHOOP_BASE}{path}"
    timeout = aiohttp.ClientTimeout(total=20)
    token_refreshed = False

    for attempt in range(3):
        token = await _get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resp = await session.get(url, headers=headers, params=params or {})
            if resp.status == 401 and not token_refreshed:
                tokens = _load_tokens()
                tokens = await _refresh_access_token(tokens)
                token_refreshed = True
                continue
            if _is_transient_whoop_status(resp.status) and attempt < 2:
                await asyncio.sleep((2 ** attempt) + random.random() * 0.25)
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError(f"WHOOP request failed after retries: {path}")


async def get_latest_recovery() -> dict:
    """Fetch most recent recovery record."""
    data = await _whoop_get("/v2/recovery", params={"limit": 1})
    records = data.get("records", [])
    return records[0] if records else {}


async def get_latest_sleep() -> dict:
    """Fetch most recent sleep record."""
    data = await _whoop_get("/v2/activity/sleep", params={"limit": 1})
    records = data.get("records", [])
    return records[0] if records else {}


async def get_latest_cycle() -> dict:
    """Fetch most recent strain/cycle record."""
    data = await _whoop_get("/v2/cycle", params={"limit": 1})
    records = data.get("records", [])
    return records[0] if records else {}


async def get_body_measurement() -> dict:
    """Fetch body measurements."""
    return await _whoop_get("/v2/user/measurement/body")


async def sync_today() -> dict:
    """Pull today's WHOOP data and upsert into DB. Returns summary dict."""
    try:
        recovery = await get_latest_recovery()
        sleep    = await get_latest_sleep()
        cycle    = await get_latest_cycle()
    except Exception as e:
        logger.error("WHOOP sync failed: %s", e)
        return {"error": str(e)}

    today = datetime.now(timezone.utc).date().isoformat()

    # Parse recovery
    rec_score = None
    hrv       = None
    resting_hr = None
    sleep_perf = None
    sleep_hrs  = None
    if recovery:
        rec_score  = recovery.get("score", {}).get("recovery_score")
        hrv        = recovery.get("score", {}).get("hrv_rmssd_milli")
        resting_hr = recovery.get("score", {}).get("resting_heart_rate")

    # Parse sleep
    if sleep:
        sp = sleep.get("score", {}).get("sleep_performance_percentage")
        sleep_perf = int(sp) if sp is not None else None
        dur_ms = sleep.get("score", {}).get("stage_summary", {}).get("total_in_bed_time_milli", 0)
        sleep_hrs = round(dur_ms / 3_600_000, 1) if dur_ms else None

    # Parse strain
    strain = None
    calories = None
    if cycle:
        strain   = cycle.get("score", {}).get("strain")
        kj = cycle.get("score", {}).get("kilojoule")
        calories = int(kj * 0.239) if kj else None  # kJ → kcal

    async with db_connect() as db:
        await db.execute("""
            INSERT INTO fitness_whoop_daily
                (date, recovery_score, hrv, resting_hr, sleep_performance,
                 sleep_duration_hrs, strain, calories, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                recovery_score    = excluded.recovery_score,
                hrv               = excluded.hrv,
                resting_hr        = excluded.resting_hr,
                sleep_performance = excluded.sleep_performance,
                sleep_duration_hrs = excluded.sleep_duration_hrs,
                strain            = excluded.strain,
                calories          = excluded.calories,
                synced_at         = excluded.synced_at
        """, (today, rec_score, hrv, resting_hr, sleep_perf,
              sleep_hrs, strain, calories,
              datetime.utcnow().isoformat()))
        await db.commit()

    return {
        "date": today,
        "recovery_score": rec_score,
        "hrv": hrv,
        "resting_hr": resting_hr,
        "sleep_performance": sleep_perf,
        "sleep_duration_hrs": sleep_hrs,
        "strain": strain,
        "calories": calories,
    }


async def sync_history(days: int = 30) -> list[dict]:
    """Fetch up to `days` days of WHOOP data, upsert into DB, return records."""
    from datetime import timedelta
    start_dt = datetime.now(timezone.utc) - timedelta(days=days)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    async def _fetch_all(path: str) -> list:
        records, token = [], None
        while True:
            params: dict = {"limit": 25, "start": start_iso}
            if token:
                params["nextToken"] = token
            data = await _whoop_get(path, params=params)
            records.extend(data.get("records", []))
            token = data.get("next_token")
            if not token:
                break
        return records

    try:
        recoveries = await _fetch_all("/v2/recovery")
        sleeps     = await _fetch_all("/v2/activity/sleep")
        cycles     = await _fetch_all("/v2/cycle")
    except Exception as e:
        logger.error("WHOOP history sync failed: %s", e)
        return []

    # Index by date (YYYY-MM-DD using start timestamp)
    def _date(record: dict) -> str:
        ts = record.get("start") or record.get("created_at", "")
        return ts[:10]

    rec_by_cycle  = {r["cycle_id"]: r for r in recoveries}
    sleep_by_date = {_date(s): s for s in sleeps}

    rows = []
    for cycle in cycles:
        date       = _date(cycle)
        recovery   = rec_by_cycle.get(cycle["id"], {})
        sleep      = sleep_by_date.get(date, {})

        rec_score  = recovery.get("score", {}).get("recovery_score")
        hrv        = recovery.get("score", {}).get("hrv_rmssd_milli")
        resting_hr = recovery.get("score", {}).get("resting_heart_rate")

        sp         = sleep.get("score", {}).get("sleep_performance_percentage")
        sleep_perf = int(sp) if sp is not None else None
        dur_ms     = sleep.get("score", {}).get("stage_summary", {}).get("total_in_bed_time_milli", 0)
        sleep_hrs  = round(dur_ms / 3_600_000, 1) if dur_ms else None

        strain     = cycle.get("score", {}).get("strain")
        kj         = cycle.get("score", {}).get("kilojoule")
        calories   = int(kj * 0.239) if kj else None

        rows.append((date, rec_score, hrv, resting_hr, sleep_perf,
                     sleep_hrs, strain, calories))

    async with db_connect() as db:
        await db.executemany("""
            INSERT INTO fitness_whoop_daily
                (date, recovery_score, hrv, resting_hr, sleep_performance,
                 sleep_duration_hrs, strain, calories, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(date) DO UPDATE SET
                recovery_score     = excluded.recovery_score,
                hrv                = excluded.hrv,
                resting_hr         = excluded.resting_hr,
                sleep_performance  = excluded.sleep_performance,
                sleep_duration_hrs = excluded.sleep_duration_hrs,
                strain             = excluded.strain,
                calories           = excluded.calories,
                synced_at          = excluded.synced_at
        """, rows)
        await db.commit()

    return await get_history_from_db(days)


async def get_history_from_db(days: int = 30) -> list[dict]:
    """Return the last `days` rows from the DB as dicts, oldest first."""
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM fitness_whoop_daily ORDER BY date DESC LIMIT ?", (days,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return list(reversed(rows))


async def get_fitness_status() -> str:
    """Return today's WHOOP status as a plain-text summary for Jarvis."""
    # Try live sync first, fall back to DB
    try:
        data = await sync_today()
    except Exception:
        data = {}

    if not data or data.get("error"):
        # Fall back to last DB row
        async with db_connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM fitness_whoop_daily ORDER BY date DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            data = dict(row) if row else {}

    if not data:
        return "No WHOOP data available yet. Make sure WHOOP is authorized (python fitness_services.py auth)."

    rec   = data.get("recovery_score")
    hrv   = data.get("hrv")
    rhr   = data.get("resting_hr")
    sleep = data.get("sleep_performance")
    slph  = data.get("sleep_duration_hrs")
    strain = data.get("strain")
    date  = data.get("date", "today")

    def rec_label(s):
        if s is None: return "unknown"
        if s >= 67: return f"{s}% 🟢 Green"
        if s >= 34: return f"{s}% 🟡 Yellow"
        return f"{s}% 🔴 Red"

    lines = [f"WHOOP — {date}"]
    lines.append(f"  Recovery:  {rec_label(rec)}")
    if hrv:        lines.append(f"  HRV:       {hrv:.0f} ms")
    if rhr:        lines.append(f"  Resting HR: {rhr} bpm")
    if sleep is not None: lines.append(f"  Sleep:     {sleep}% ({slph}h)")
    if strain is not None: lines.append(f"  Strain:    {strain:.1f}/21")

    return "\n".join(lines)


# ── PR tracking ────────────────────────────────────────────────────────────────

async def log_pr(exercise: str, value: float, unit: str = "lbs",
                 notes: str = None) -> str:
    logged_at = datetime.utcnow().isoformat()
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO fitness_prs (exercise, value, unit, logged_at, notes) VALUES (?, ?, ?, ?, ?)",
            (exercise.lower().strip(), value, unit, logged_at, notes)
        )
        await db.commit()
    return f"PR logged: {exercise} — {value} {unit}"


async def get_prs(exercise: str = None) -> str:
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        if exercise:
            async with db.execute("""
                SELECT exercise, value, unit, logged_at, notes
                FROM fitness_prs
                WHERE exercise LIKE ?
                ORDER BY logged_at DESC LIMIT 10
            """, (f"%{exercise.lower()}%",)) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        else:
            # Best PR per exercise
            async with db.execute("""
                SELECT exercise, MAX(value) as value, unit, MAX(logged_at) as logged_at
                FROM fitness_prs
                GROUP BY exercise
                ORDER BY logged_at DESC
            """) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        q = f" for '{exercise}'" if exercise else ""
        return f"No PRs found{q}."

    lines = ["Personal Records:"]
    for r in rows:
        dt = r["logged_at"][:10]
        note = f" — {r['notes']}" if r.get("notes") else ""
        lines.append(f"  {r['exercise'].title()}: {r['value']} {r['unit']} ({dt}){note}")
    return "\n".join(lines)


# ── Workout logging ────────────────────────────────────────────────────────────

async def log_workout(description: str, result: str = None,
                      rx: bool = True, workout_type: str = "crossfit",
                      notes: str = None) -> str:
    logged_at = datetime.utcnow().isoformat()
    async with db_connect() as db:
        await db.execute("""
            INSERT INTO fitness_workouts (logged_at, type, description, result, rx, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (logged_at, workout_type, description, result, 1 if rx else 0, notes))
        await db.commit()
    rx_label = "Rx" if rx else "Scaled"
    result_part = f" — {result}" if result else ""
    return f"Workout logged ({rx_label}): {description}{result_part}"


async def get_recent_workouts(limit: int = 7) -> str:
    async with db_connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM fitness_workouts ORDER BY logged_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return "No workouts logged yet."
    lines = ["Recent workouts:"]
    for r in rows:
        dt = r["logged_at"][:10]
        rx = "Rx" if r.get("rx") else "Scaled"
        result = f" → {r['result']}" if r.get("result") else ""
        lines.append(f"  {dt} [{rx}] {r['description']}{result}")
    return "\n".join(lines)


# ── OAuth authorization flow (run once) ───────────────────────────────────────

def authorize():
    """One-time OAuth2 authorization. Run from terminal."""
    import urllib.parse, urllib.request, http.server, threading, webbrowser

    client_id     = os.getenv("WHOOP_CLIENT_ID", "")
    client_secret = os.getenv("WHOOP_CLIENT_SECRET", "")
    redirect_uri  = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8888/callback")
    scope         = "offline read:recovery read:sleep read:workout read:body_measurement read:cycles"

    if not client_id:
        print("ERROR: Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env first")
        return

    # Build auth URL
    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         scope,
    }
    auth_url = f"{WHOOP_AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"\nOpen this URL in your browser:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Capture callback
    code_holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code_holder["code"] = qs.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

        def log_message(self, *args):
            pass

    port = int(urllib.parse.urlparse(redirect_uri).port or 8888)
    server = http.server.HTTPServer(("", port), Handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    t.join(timeout=120)

    code = code_holder.get("code")
    if not code:
        print("No code received — timed out or redirect URI mismatch.")
        return

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(WHOOP_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())

    import time
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    _save_tokens(tokens)
    print(f"\nAuthorized! Tokens saved to {TOKENS_PATH}")
    print(f"Access token expires in {tokens.get('expires_in', '?')}s")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from dotenv import load_dotenv
        load_dotenv()
        authorize()
    else:
        print("Usage: python fitness_services.py auth")
