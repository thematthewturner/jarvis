"""
Pool monitoring service.

- Hayward OmniLogic cloud: equipment status (temp, salt cell %, pump, heater)
- SQLite: chemistry readings (manual + Leslie's imports), chemical additions
- Claude Sonnet: pool report generation
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL_REPORT, HAYWARD_EMAIL, HAYWARD_PASSWORD
from memory import DB_PATH, db_connect

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

# Ideal ranges for a saltwater pool
IDEAL_RANGES = {
    "fc":         (2.0,  4.0,   "ppm"),
    "ph":         (7.4,  7.6,   None),
    "ta":         (80,   120,   "ppm"),
    "salt":       (2700, 3400,  "ppm"),
    "cya":        (30,   50,    "ppm"),
    "ch":         (200,  400,   "ppm"),
    "copper":     (0.0,  0.2,   "ppm"),
    "iron":       (0.0,  0.3,   "ppm"),
    "phosphate":  (0,    100,   "ppb"),
}


# ── Database ──────────────────────────────────────────────────────────────────

async def init_pool_db():
    async with db_connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pool_readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at     TEXT    NOT NULL,
                source        TEXT    DEFAULT 'manual',   -- 'manual' | 'leslies'
                fc            REAL,   -- Free Chlorine ppm
                tc            REAL,   -- Total Chlorine ppm (Leslie's)
                ph            REAL,
                ta            REAL,   -- Total Alkalinity ppm
                salt          INTEGER,
                cya           REAL,   -- Cyanuric Acid ppm
                ch            REAL,   -- Calcium Hardness ppm
                water_temp    REAL,
                iron          REAL,
                copper        REAL,
                phosphate     REAL,
                overall_score INTEGER,    -- Leslie's 0-100 score
                pdf_url       TEXT,
                notes         TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pool_chemical_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at TEXT    NOT NULL,
                chemical  TEXT    NOT NULL,
                amount    TEXT    NOT NULL,
                notes     TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pool_equipment_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at        TEXT NOT NULL,
                water_temp       REAL,
                chlorinator_pct  INTEGER,
                pump_state       TEXT,
                heater_state     TEXT,
                heater_setpoint  REAL,
                raw_json         TEXT
            )
        """)
        await db.commit()


# ── Hayward OmniLogic ─────────────────────────────────────────────────────────

async def fetch_omnilogic_status() -> dict:
    """Fetch live equipment status from Hayward OmniLogic cloud API."""
    if not HAYWARD_EMAIL or not HAYWARD_PASSWORD:
        return {"error": "Hayward credentials not set (HAYWARD_EMAIL / HAYWARD_PASSWORD in .env)"}
    try:
        from omnilogic import OmniLogic
        import asyncio
        api = OmniLogic(username=HAYWARD_EMAIL, password=HAYWARD_PASSWORD)
        await asyncio.wait_for(api.connect(), timeout=15)
        telemetry = await asyncio.wait_for(api.get_telemetry_data(), timeout=15)
        await api.close()

        parsed = {
            "water_temp": None,
            "air_temp": None,
            "chlorinator_pct": None,
            "salt_avg": None,
            "salt_instant": None,
            "pump_state": None,
            "pump_speed": None,
            "heater_state": None,
            "heater_setpoint": None,
        }

        # telemetry is a list of systems; grab the first BOW (pool)
        system = telemetry[0] if isinstance(telemetry, list) else telemetry

        try:
            parsed["air_temp"] = float(system.get("airTemp", 0)) or None
        except (TypeError, ValueError):
            pass

        bows = system.get("BOWS") or system.get("Backyard", {}).get("Body-of-water", [])
        bow = bows[0] if isinstance(bows, list) and bows else {}

        # Water temp
        try:
            parsed["water_temp"] = float(bow.get("waterTemp", 0)) or None
        except (TypeError, ValueError):
            pass

        # Filter / pump
        filt = bow.get("Filter", {})
        if isinstance(filt, dict):
            state = filt.get("filterState")
            parsed["pump_state"] = "on" if state == "1" else ("off" if state == "0" else state)
            try:
                parsed["pump_speed"] = int(filt.get("filterSpeed", 0)) or None
            except (TypeError, ValueError):
                pass

        # Heater state + setpoint
        heater = bow.get("Heater", {})
        if isinstance(heater, dict):
            hs = heater.get("heaterState")
            parsed["heater_state"] = "on" if hs == "1" else ("off" if hs == "0" else hs)
        vheater = bow.get("VirtualHeater", {})
        if isinstance(vheater, dict):
            sp = vheater.get("Current-Set-Point")
            if sp:
                try:
                    parsed["heater_setpoint"] = float(sp)
                except (TypeError, ValueError):
                    pass

        # Chlorinator
        chlor = bow.get("Chlorinator", {})
        if isinstance(chlor, dict):
            try:
                parsed["chlorinator_pct"] = int(chlor.get("Timed-Percent", 0)) or None
            except (TypeError, ValueError):
                pass
            try:
                parsed["salt_avg"] = int(chlor.get("avgSaltLevel", 0)) or None
            except (TypeError, ValueError):
                pass
            try:
                parsed["salt_instant"] = int(chlor.get("instantSaltLevel", 0)) or None
            except (TypeError, ValueError):
                pass

        # Save snapshot
        async with db_connect() as db:
            await db.execute(
                """INSERT INTO pool_equipment_snapshots
                   (logged_at, water_temp, chlorinator_pct, pump_state,
                    heater_state, heater_setpoint, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().isoformat(),
                    parsed["water_temp"],
                    parsed["chlorinator_pct"],
                    parsed["pump_state"],
                    parsed["heater_state"],
                    parsed["heater_setpoint"],
                    json.dumps(telemetry, default=str)[:8000],  # cap size
                ),
            )
            await db.commit()

        parsed["raw"] = telemetry
        return parsed

    except ImportError:
        return {"error": "omnilogic package not installed — run: pip install omnilogic"}
    except Exception as e:
        logger.error(f"OmniLogic fetch error: {e}")
        return {"error": str(e)}


# ── Chemistry / Chemical Logging ──────────────────────────────────────────────

async def log_chemistry(
    fc=None, tc=None, ph=None, ta=None,
    salt=None, cya=None, ch=None, water_temp=None,
    iron=None, copper=None, phosphate=None,
    notes=None, source="manual",
) -> str:
    """Save a manual chemistry reading to the database."""
    async with db_connect() as db:
        await db.execute(
            """INSERT INTO pool_readings
               (logged_at, source, fc, tc, ph, ta, salt, cya, ch,
                water_temp, iron, copper, phosphate, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), source,
             fc, tc, ph, ta, salt, cya, ch,
             water_temp, iron, copper, phosphate, notes),
        )
        await db.commit()

    parts = []
    for label, val in [("FC", fc), ("TC", tc), ("pH", ph), ("TA", ta),
                       ("salt", salt), ("CYA", cya), ("CH", ch),
                       ("temp", water_temp), ("iron", iron),
                       ("copper", copper), ("phosphate", phosphate)]:
        if val is not None:
            parts.append(f"{label} {val}")
    return f"Pool chemistry logged: {', '.join(parts) or '(no values)'}."


async def log_chemical_addition(chemical: str, amount: str, notes: str = None) -> str:
    """Save a chemical addition to the pool log."""
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO pool_chemical_log (logged_at, chemical, amount, notes) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), chemical.lower().strip(), amount.strip(), notes),
        )
        await db.commit()
    return f"Logged: added {amount} of {chemical} to pool."


# ── Status / History ──────────────────────────────────────────────────────────

async def get_pool_status() -> str:
    """Current equipment status (from OmniLogic) + latest chemistry reading."""
    now = datetime.now(ET)

    equipment = await fetch_omnilogic_status()

    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM pool_readings ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        cols = [d[0] for d in cur.description] if row else []
        reading = dict(zip(cols, row)) if row else None

        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        cur2 = await db.execute(
            """SELECT chemical, amount, logged_at FROM pool_chemical_log
               WHERE logged_at > ? ORDER BY logged_at DESC LIMIT 5""",
            (cutoff,),
        )
        recent_chems = await cur2.fetchall()

    lines = [f"Pool Status — {now.strftime('%a %b %-d, %-I:%M %p')}"]

    # Equipment
    if "error" in equipment:
        lines.append(f"\nHayward OmniLogic: {equipment['error']}")
    else:
        lines.append("\nHayward OmniLogic:")
        if equipment.get("water_temp"):
            lines.append(f"  Water Temp:  {equipment['water_temp']:.0f}°F")
        if equipment.get("air_temp"):
            lines.append(f"  Air Temp:    {equipment['air_temp']:.0f}°F")
        if equipment.get("chlorinator_pct") is not None:
            lines.append(f"  Salt Cell:   {equipment['chlorinator_pct']}%")
        if equipment.get("salt_avg"):
            salt_icon = _range_icon("salt", equipment["salt_avg"])
            lines.append(f"  Salt (avg):  {equipment['salt_avg']} ppm {salt_icon}")
        if equipment.get("salt_instant"):
            lines.append(f"  Salt (now):  {equipment['salt_instant']} ppm")
        if equipment.get("pump_state"):
            speed = f" @ {equipment['pump_speed']}%" if equipment.get("pump_speed") else ""
            lines.append(f"  Pump:        {equipment['pump_state'].title()}{speed}")
        if equipment.get("heater_state"):
            hs = equipment["heater_state"].title()
            if equipment.get("heater_setpoint"):
                hs += f" (set {equipment['heater_setpoint']:.0f}°F)"
            lines.append(f"  Heater:      {hs}")

    # Chemistry
    if reading:
        src_label = " (Leslie's)" if reading.get("source") == "leslies" else ""
        age = _reading_age(reading["logged_at"])
        lines.append(f"\nLast Chemistry Test ({age}{src_label}):")
        if reading.get("overall_score") is not None:
            lines.append(f"  Leslie's Score: {reading['overall_score']}/100")
        for key, label, unit in [
            ("fc",        "Free Chlorine", "ppm"),
            ("tc",        "Total Chlorine","ppm"),
            ("ph",        "pH",            None),
            ("ta",        "Alkalinity",    "ppm"),
            ("salt",      "Salt",          "ppm"),
            ("cya",       "CYA",           "ppm"),
            ("ch",        "Calcium",       "ppm"),
            ("iron",      "Iron",          "ppm"),
            ("copper",    "Copper",        "ppm"),
            ("phosphate", "Phosphate",     "ppb"),
        ]:
            val = reading.get(key)
            if val is None:
                continue
            icon = _range_icon(key, val)
            unit_str = f" {unit}" if unit else ""
            lines.append(f"  {icon} {label}: {val}{unit_str}")
        if reading.get("notes"):
            lines.append(f"  Notes: {reading['notes']}")
    else:
        lines.append("\nNo chemistry readings yet. Use /pool log or sync Leslie's (/pool sync).")

    # Recent additions
    if recent_chems:
        lines.append("\nRecent Additions (7 days):")
        for chem, amount, ts in recent_chems:
            d = datetime.fromisoformat(ts).strftime("%-m/%-d")
            lines.append(f"  {d} — {amount} {chem}")

    return "\n".join(lines)


async def get_pool_history(days: int = 7) -> str:
    """Chemistry readings and chemical additions for the past N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM pool_readings WHERE logged_at > ? ORDER BY logged_at DESC",
            (cutoff,),
        )
        readings = await cur.fetchall()
        cols = [d[0] for d in cur.description]

        cur2 = await db.execute(
            """SELECT chemical, amount, logged_at FROM pool_chemical_log
               WHERE logged_at > ? ORDER BY logged_at DESC""",
            (cutoff,),
        )
        additions = await cur2.fetchall()

    if not readings and not additions:
        return f"No pool activity in the last {days} days."

    lines = [f"Pool History — Last {days} Days"]

    if readings:
        lines.append(f"\nChemistry ({len(readings)} readings):")
        for row in readings:
            r = dict(zip(cols, row))
            ts = datetime.fromisoformat(r["logged_at"]).strftime("%-m/%-d")
            src = " [L]" if r.get("source") == "leslies" else ""
            score = f" score={r['overall_score']}" if r.get("overall_score") else ""
            parts = []
            for k in ("fc", "ph", "ta", "salt", "cya", "ch"):
                if r.get(k) is not None:
                    icon = _range_icon(k, r[k])
                    parts.append(f"{k.upper()}:{r[k]}{icon}")
            lines.append(f"  {ts}{src}{score}: {' | '.join(parts)}")

    if additions:
        lines.append(f"\nChemical Additions ({len(additions)}):")
        for chem, amount, ts in additions:
            d = datetime.fromisoformat(ts).strftime("%-m/%-d")
            lines.append(f"  {d}: {amount} {chem}")

    return "\n".join(lines)


# ── Pool Report ───────────────────────────────────────────────────────────────

async def generate_pool_report() -> str:
    """AI-generated pool report using Claude Sonnet."""
    now = datetime.now(ET)
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()

    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM pool_readings WHERE logged_at > ? ORDER BY logged_at DESC",
            (cutoff,),
        )
        readings = await cur.fetchall()
        cols = [d[0] for d in cur.description]

        cur2 = await db.execute(
            """SELECT chemical, amount, logged_at FROM pool_chemical_log
               WHERE logged_at > ? ORDER BY logged_at DESC""",
            (cutoff,),
        )
        additions = await cur2.fetchall()

        cur3 = await db.execute(
            """SELECT water_temp, chlorinator_pct, pump_state, heater_state, logged_at
               FROM pool_equipment_snapshots ORDER BY id DESC LIMIT 5"""
        )
        snapshots = await cur3.fetchall()

    def fmt_reading(r):
        parts = []
        for k in ("fc", "tc", "ph", "ta", "salt", "cya", "ch", "iron", "copper"):
            if r.get(k) is not None:
                icon = _range_icon(k, r[k])
                parts.append(f"{k}={r[k]}{icon}")
        score = f" [score:{r['overall_score']}]" if r.get("overall_score") else ""
        src = " [Leslie's]" if r.get("source") == "leslies" else ""
        ts = r["logged_at"][:10]
        return f"{ts}{src}{score}: {', '.join(parts)}"

    readings_text = "\n".join(fmt_reading(dict(zip(cols, r))) for r in readings) or "None logged."
    additions_text = "\n".join(
        f"{ts[:10]}: {amount} {chem}" for chem, amount, ts in additions
    ) or "None logged."
    equipment_text = "\n".join(
        f"{ts[:10]}: temp={wt}°F, salt_cell={cp}%, pump={ps}, heater={hs}"
        for wt, cp, ps, hs, ts in snapshots
    ) or "No OmniLogic data."

    prompt = f"""You are Jarvis preparing a pool maintenance report for Matt's saltwater pool in Mount Pleasant, SC.

Today: {now.strftime("%A, %B %d, %Y")}

CHEMISTRY READINGS (last 30 days):
{readings_text}

CHEMICAL ADDITIONS (last 30 days):
{additions_text}

EQUIPMENT STATUS (recent snapshots):
{equipment_text}

IDEAL RANGES (saltwater pool):
FC: 2-4 ppm | pH: 7.4-7.6 | TA: 80-120 ppm
Salt: 2700-3400 ppm | CYA: 30-50 ppm | CH: 200-400 ppm
Copper: <0.2 ppm | Iron: <0.3 ppm

Provide a concise pool report:
1. Status summary (2-3 sentences)
2. Parameters out of range → specific corrections with amounts
3. Effect of recent chemical additions (if any data)
4. What to do next / recommended test date
5. One seasonal note if relevant (SC summers are brutal on FC)

[L] = Leslie's store test. Plain text, no markdown headers, direct and actionable."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = await asyncio.to_thread(
        client.messages.create,
        model=CLAUDE_MODEL_REPORT,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reading_age(logged_at: str) -> str:
    dt = datetime.fromisoformat(logged_at)
    delta = datetime.utcnow() - dt
    if delta.days == 0:
        h = delta.seconds // 3600
        return f"{h}h ago" if h else "just now"
    elif delta.days == 1:
        return "yesterday"
    return f"{delta.days}d ago"


def _range_icon(key: str, val: float) -> str:
    r = IDEAL_RANGES.get(key)
    if not r:
        return ""
    lo, hi, _ = r
    if val < lo:
        return "↓"
    if val > hi:
        return "↑"
    return "✓"
