#!/usr/bin/env python3
"""WHOOP monitor for Jarvis daily recovery analysis.

Uses WHOOP OAuth tokens stored outside source control, refreshes them when
needed, fetches recent recovery/cycle/sleep/workout data, and emits either JSON
for the daily digest or Markdown for operator review.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_BASE = "https://api.prod.whoop.com/developer/v2"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPES = [
    "offline",
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
]
ET = ZoneInfo("America/New_York")


def configured_path(env_name: str, defaults: list[Path]) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured)
    for path in defaults:
        if path.exists():
            return path
    return defaults[0]


def configured_cache_path(env_name: str, defaults: list[Path]) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured)
    for path in defaults:
        if path.exists() or path.parent.exists():
            return path
    return defaults[0]


TOKEN_PATH = configured_path(
    "WHOOP_TOKEN_FILE",
    [
        Path("/opt/data/secrets/whoop_token.json"),
        Path("/Users/toniturner/Library/Mobile Documents/com~apple~CloudDocs/projects/jarvis/hermes-jarvis-parity-pack/secrets.local/whoop_tokens.json"),
        Path("/Users/toniturner/Library/Mobile Documents/com~apple~CloudDocs/projects/jarvis/data/whoop_tokens.json"),
        Path("/private/tmp/whoop_token.json"),
    ],
)
CACHE_PATH = configured_cache_path(
    "WHOOP_CACHE_FILE",
    [
        Path("/opt/data/jarvis/whoop_cache.json"),
        Path("/private/tmp/whoop_cache.json"),
    ],
)


@dataclass
class WhoopConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def day_key(value: str | None) -> str | None:
    dt = parse_dt(value)
    if not dt:
        return None
    return dt.astimezone(ET).date().isoformat()


def hours(ms: float | int | None) -> float | None:
    if ms is None:
        return None
    return round(float(ms) / 1000 / 60 / 60, 2)


def pct(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))


def display_number(value) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}"
    return str(value)


def avg(values: list[float | int | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return round(statistics.mean(clean), 1)


def window_avg(daily: list[dict], metric: str, days: int) -> float | None:
    return avg([row.get(metric) for row in daily[-days:]])


def delta_text(current: float | int | None, baseline: float | int | None, unit: str = "") -> str | None:
    if current is None or baseline is None:
        return None
    delta = round(float(current) - float(baseline), 1)
    if abs(delta) < 0.1:
        return None
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:g}{unit}"


def load_config() -> WhoopConfig:
    client_id = os.getenv("WHOOP_CLIENT_ID", "").strip()
    client_secret = os.getenv("WHOOP_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8888/callback").strip()
    missing = [
        name
        for name, value in {
            "WHOOP_CLIENT_ID": client_id,
            "WHOOP_CLIENT_SECRET": client_secret,
            "WHOOP_REDIRECT_URI": redirect_uri,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing WHOOP config: " + ", ".join(missing))
    return WhoopConfig(client_id, client_secret, redirect_uri)


def load_token() -> dict:
    if not TOKEN_PATH.exists():
        raise RuntimeError(f"WHOOP token file missing: {TOKEN_PATH}")
    return json.loads(TOKEN_PATH.read_text())


def save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if "expires_in" in token:
        token["expires_at"] = int(time.time()) + int(token["expires_in"]) - 120
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(token, indent=2, sort_keys=True))
    tmp.chmod(0o600)
    tmp.replace(TOKEN_PATH)


def token_request(payload: dict, cfg: WhoopConfig) -> dict:
    payload = {**payload, "client_id": cfg.client_id, "client_secret": cfg.client_secret}
    form = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=form,
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as first_error:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            detail = first_error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"WHOOP token request failed: HTTP {first_error.code} {detail}") from first_error


def auth_url(cfg: WhoopConfig) -> str:
    state = secrets.token_urlsafe(8)[:8]
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    cfg = load_config()
    token = token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "scope": " ".join(SCOPES),
        },
        cfg,
    )
    save_token(token)
    return token


def access_token() -> str:
    cfg = load_config()
    token = load_token()
    if int(token.get("expires_at", 0)) <= int(time.time()) + 60:
        refresh = token.get("refresh_token")
        if not refresh:
            raise RuntimeError("WHOOP access token expired and no refresh token is available")
        token = token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": "offline",
            },
            cfg,
        )
        save_token(token)
    access = token.get("access_token")
    if not access:
        raise RuntimeError("WHOOP token file has no access_token")
    return access


def api_get(path: str, params: dict | None = None) -> dict:
    query = "?" + urllib.parse.urlencode(params or {}) if params else ""
    req = urllib.request.Request(
        API_BASE + path + query,
        headers={
            "Authorization": f"Bearer {access_token()}",
            "user-agent": "Mozilla/5.0",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"WHOOP API {path} failed: HTTP {exc.code} {detail}") from exc


def collection(path: str, days: int, limit: int = 25) -> list[dict]:
    start = (now_utc() - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    rows: list[dict] = []
    next_token = None
    while True:
        params = {"limit": min(limit, 25), "start": start}
        if next_token:
            params["nextToken"] = next_token
        data = api_get(path, params)
        rows.extend(data.get("records", []))
        next_token = data.get("next_token")
        if not next_token or len(rows) >= 250:
            break
    return rows


def fetch_whoop(days: int) -> dict:
    return {
        "profile": safe_fetch(lambda: api_get("/user/profile/basic")),
        "body": safe_fetch(lambda: api_get("/user/measurement/body")),
        "recoveries": collection("/recovery", days),
        "cycles": collection("/cycle", days),
        "sleeps": collection("/activity/sleep", days),
        "workouts": collection("/activity/workout", days),
    }


def safe_fetch(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


def recovery_color(score: int | float | None) -> str:
    if score is None:
        return "unknown"
    if score < 34:
        return "red"
    if score < 67:
        return "yellow"
    return "green"


def latest_by_day(rows: list[dict], date_fields: tuple[str, ...]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        date = None
        for field in date_fields:
            date = day_key(row.get(field))
            if date:
                break
        if not date:
            continue
        previous = out.get(date)
        if not previous or (row.get("updated_at") or row.get("created_at") or "") > (previous.get("updated_at") or previous.get("created_at") or ""):
            out[date] = row
    return out


def analyze(data: dict) -> dict:
    recoveries = latest_by_day(data.get("recoveries", []), ("created_at", "updated_at"))
    cycles = latest_by_day(data.get("cycles", []), ("start", "created_at"))
    sleeps = latest_by_day(
        [row for row in data.get("sleeps", []) if not row.get("nap")],
        ("start", "created_at"),
    )
    all_days = sorted(set(recoveries) | set(cycles) | set(sleeps))
    daily = []
    for date in all_days:
        rec = recoveries.get(date) or {}
        cycle = cycles.get(date) or {}
        sleep = sleeps.get(date) or {}
        rec_score = (rec.get("score") or {}).get("recovery_score")
        sleep_score = sleep.get("score") or {}
        stage = sleep_score.get("stage_summary") or {}
        sleep_needed = sleep_score.get("sleep_needed") or {}
        asleep = sum(
            stage.get(name, 0) or 0
            for name in (
                "total_light_sleep_time_milli",
                "total_slow_wave_sleep_time_milli",
                "total_rem_sleep_time_milli",
            )
        )
        need = sum(v or 0 for v in sleep_needed.values())
        daily.append(
            {
                "date": date,
                "recovery": rec_score,
                "color": recovery_color(rec_score),
                "hrv_rmssd": (rec.get("score") or {}).get("hrv_rmssd_milli"),
                "rhr": (rec.get("score") or {}).get("resting_heart_rate"),
                "spo2": (rec.get("score") or {}).get("spo2_percentage"),
                "skin_temp_c": (rec.get("score") or {}).get("skin_temp_celsius"),
                "strain": (cycle.get("score") or {}).get("strain"),
                "avg_hr": (cycle.get("score") or {}).get("average_heart_rate"),
                "max_hr": (cycle.get("score") or {}).get("max_heart_rate"),
                "sleep_performance": sleep_score.get("sleep_performance_percentage"),
                "sleep_efficiency": sleep_score.get("sleep_efficiency_percentage"),
                "sleep_consistency": sleep_score.get("sleep_consistency_percentage"),
                "sleep_hours": hours(asleep),
                "sleep_need_hours": hours(need),
                "disturbances": stage.get("disturbance_count"),
            }
        )
    daily = [row for row in daily if row.get("date")]
    recent = daily[-1] if daily else {}
    last7 = daily[-7:]
    prior7 = daily[-14:-7]
    last30 = daily[-30:]

    def trend(metric: str) -> dict:
        return {
            "latest": recent.get(metric),
            "avg_7d": avg([row.get(metric) for row in last7]),
            "avg_prev_7d": avg([row.get(metric) for row in prior7]),
            "avg_30d": avg([row.get(metric) for row in last30]),
            "avg_60d": window_avg(daily, metric, 60),
            "avg_90d": window_avg(daily, metric, 90),
        }

    red_streak = 0
    for row in reversed(daily):
        if row.get("color") == "red":
            red_streak += 1
        else:
            break
    yellow_or_red = 0
    for row in reversed(daily):
        if row.get("color") in ("red", "yellow"):
            yellow_or_red += 1
        else:
            break

    workouts = sorted(data.get("workouts", []), key=lambda row: row.get("start") or "")
    recent_workouts = []
    for workout in workouts[-10:]:
        score = workout.get("score") or {}
        recent_workouts.append(
            {
                "date": day_key(workout.get("start")),
                "sport": workout.get("sport_name"),
                "strain": score.get("strain"),
                "duration_hours": hours(
                    (parse_dt(workout.get("end")) - parse_dt(workout.get("start"))).total_seconds() * 1000
                    if parse_dt(workout.get("end")) and parse_dt(workout.get("start"))
                    else None
                ),
            }
        )

    trends = {
        "recovery": trend("recovery"),
        "hrv_rmssd": trend("hrv_rmssd"),
        "rhr": trend("rhr"),
        "strain": trend("strain"),
        "sleep_performance": trend("sleep_performance"),
        "sleep_hours": trend("sleep_hours"),
        "sleep_efficiency": trend("sleep_efficiency"),
    }
    pattern_insights = make_pattern_insights(trends, daily)
    insights = make_insights(recent, daily, red_streak, yellow_or_red, pattern_insights)
    return {
        "generated_at": now_utc().isoformat(),
        "latest": recent,
        "trends": trends,
        "pattern_insights": pattern_insights,
        "streaks": {
            "red_recovery_days": red_streak,
            "yellow_or_red_recovery_days": yellow_or_red,
        },
        "recent_days": daily[-30:],
        "recent_workouts": recent_workouts,
        "insights": insights,
    }


def make_pattern_insights(trends: dict[str, dict], daily: list[dict]) -> list[str]:
    insights: list[str] = []

    def values(metric: str) -> dict:
        return trends.get(metric, {})

    recovery = values("recovery")
    hrv = values("hrv_rmssd")
    rhr = values("rhr")
    sleep = values("sleep_hours")
    strain = values("strain")

    recovery_delta = delta_text(recovery.get("avg_7d"), recovery.get("avg_30d"), " pts")
    if recovery_delta and abs(float(recovery.get("avg_7d")) - float(recovery.get("avg_30d"))) >= 8:
        direction = "above" if recovery.get("avg_7d") > recovery.get("avg_30d") else "below"
        insights.append(f"7-day recovery is {direction} the 30-day baseline ({recovery.get('avg_7d'):g} vs {recovery.get('avg_30d'):g}, {recovery_delta}).")

    hrv_delta = delta_text(hrv.get("avg_7d"), hrv.get("avg_30d"), " ms")
    if hrv_delta and abs(float(hrv.get("avg_7d")) - float(hrv.get("avg_30d"))) >= 3:
        direction = "up" if hrv.get("avg_7d") > hrv.get("avg_30d") else "down"
        insights.append(f"HRV is trending {direction} over 7 days versus 30 days ({hrv.get('avg_7d'):g} vs {hrv.get('avg_30d'):g} ms, {hrv_delta}).")

    rhr_delta = delta_text(rhr.get("avg_7d"), rhr.get("avg_30d"), " bpm")
    if rhr_delta and abs(float(rhr.get("avg_7d")) - float(rhr.get("avg_30d"))) >= 2:
        direction = "elevated" if rhr.get("avg_7d") > rhr.get("avg_30d") else "lower"
        insights.append(f"RHR is {direction} versus the 30-day baseline ({rhr.get('avg_7d'):g} vs {rhr.get('avg_30d'):g} bpm, {rhr_delta}).")

    sleep_delta = delta_text(sleep.get("avg_7d"), sleep.get("avg_30d"), "h")
    if sleep_delta and abs(float(sleep.get("avg_7d")) - float(sleep.get("avg_30d"))) >= 0.5:
        direction = "more" if sleep.get("avg_7d") > sleep.get("avg_30d") else "less"
        insights.append(f"Sleep time is running {direction} than baseline ({sleep.get('avg_7d'):g}h vs {sleep.get('avg_30d'):g}h, {sleep_delta}).")

    strain_delta = delta_text(strain.get("avg_7d"), strain.get("avg_30d"))
    if strain_delta and abs(float(strain.get("avg_7d")) - float(strain.get("avg_30d"))) >= 2:
        direction = "higher" if strain.get("avg_7d") > strain.get("avg_30d") else "lower"
        insights.append(f"Training strain is {direction} than the 30-day baseline ({strain.get('avg_7d'):g} vs {strain.get('avg_30d'):g}, {strain_delta}).")

    if hrv.get("avg_30d") and hrv.get("avg_90d"):
        hrv_long_delta = float(hrv["avg_30d"]) - float(hrv["avg_90d"])
        if abs(hrv_long_delta) >= 3:
            direction = "above" if hrv_long_delta > 0 else "below"
            insights.append(f"30-day HRV is {direction} the 90-day baseline ({hrv.get('avg_30d'):g} vs {hrv.get('avg_90d'):g} ms).")

    if recovery.get("avg_30d") and recovery.get("avg_90d"):
        recovery_long_delta = float(recovery["avg_30d"]) - float(recovery["avg_90d"])
        if abs(recovery_long_delta) >= 8:
            direction = "above" if recovery_long_delta > 0 else "below"
            insights.append(f"30-day recovery is {direction} the 90-day baseline ({recovery.get('avg_30d'):g} vs {recovery.get('avg_90d'):g}).")

    low_recovery_days = sum(1 for row in daily[-14:] if row.get("color") in {"red", "yellow"})
    if low_recovery_days >= 8:
        insights.append(f"{low_recovery_days} of the last 14 days were yellow/red, so treat today's score in that broader recovery context.")

    return insights[:5]


def make_insights(
    recent: dict,
    daily: list[dict],
    red_streak: int,
    yellow_or_red: int,
    pattern_insights: list[str] | None = None,
) -> list[str]:
    insights: list[str] = []
    if not recent:
        return ["WHOOP is connected, but no scored daily records were returned yet."]
    if red_streak >= 2:
        insights.append(f"You have been red for {red_streak} days. Bias toward recovery today: sleep, hydration, easy movement, and no hero workout.")
    elif recent.get("color") == "red":
        insights.append("Recovery is red today. Treat strain as optional and keep the floor high: food, fluids, daylight, and an early night.")
    elif yellow_or_red >= 3:
        insights.append(f"You have been below green for {yellow_or_red} straight days. Keep training conservative until recovery turns.")
    elif recent.get("color") == "green":
        insights.append("Recovery is green. This is a good day for planned strain if calendar and sleep allow it.")

    last7 = daily[-7:]
    recovery_7 = avg([row.get("recovery") for row in last7])
    hrv_7 = avg([row.get("hrv_rmssd") for row in last7])
    hrv_30 = avg([row.get("hrv_rmssd") for row in daily[-30:]])
    rhr_7 = avg([row.get("rhr") for row in last7])
    rhr_30 = avg([row.get("rhr") for row in daily[-30:]])
    sleep_perf = recent.get("sleep_performance")
    strain = recent.get("strain")
    if hrv_7 is not None and hrv_30 is not None and hrv_7 < hrv_30 * 0.9:
        insights.append(f"HRV is running low versus the 30-day baseline ({hrv_7:g} vs {hrv_30:g} ms). Watch cumulative stress.")
    if rhr_7 is not None and rhr_30 is not None and rhr_7 > rhr_30 + 3:
        insights.append(f"Resting heart rate is elevated versus baseline ({rhr_7:g} vs {rhr_30:g} bpm). Recovery load may be high.")
    if sleep_perf is not None and sleep_perf < 75:
        insights.append(f"Sleep performance was {pct(sleep_perf)}%. Make sleep the main lever tonight.")
    if strain is not None and recovery_7 is not None and strain > 14 and recovery_7 < 50:
        insights.append("Recent strain is high while recovery is soft. Avoid stacking another hard day.")
    insights.extend(pattern_insights or [])
    if not insights:
        insights.append("No major WHOOP red flags. Keep the plan steady and watch the next trend shift.")
    return insights[:5]


def format_markdown(analysis: dict) -> str:
    latest = analysis.get("latest") or {}
    lines = ["# WHOOP Recovery Monitor", ""]
    if latest:
        metrics = [
            f"{display_number(latest.get('recovery'))}% recovery",
            f"HRV {display_number(latest.get('hrv_rmssd'))} ms",
            f"RHR {display_number(latest.get('rhr'))} bpm",
        ]
        if latest.get("strain") is not None:
            metrics.append(f"strain {display_number(latest.get('strain'))}")
        lines.append(f"Latest: {latest.get('date')} — {latest.get('color', 'unknown').upper()} ({', '.join(metrics)})")
        lines.append("")
    lines.append("## Jarvis Read")
    for insight in analysis.get("insights", []):
        lines.append(f"- {insight}")
    lines.append("")
    lines.append("## Rolling Trends")
    for name, values in analysis.get("trends", {}).items():
        lines.append(
            f"- {name}: latest {display_number(values.get('latest'))}, 7d {display_number(values.get('avg_7d'))}, "
            f"30d {display_number(values.get('avg_30d'))}, 60d {display_number(values.get('avg_60d'))}, "
            f"90d {display_number(values.get('avg_90d'))}"
        )
    if analysis.get("pattern_insights"):
        lines.extend(["", "## Patterns"])
        lines.extend(f"- {insight}" for insight in analysis.get("pattern_insights", []))
    return "\n".join(lines)


def format_telegram(analysis: dict) -> str:
    latest = analysis.get("latest") or {}
    if not latest:
        return "WHOOP is connected, but no scored recovery record is available yet."
    parts = [
        f"WHOOP: {latest.get('color', 'unknown').upper()} {display_number(latest.get('recovery'))}%",
        f"HRV {display_number(latest.get('hrv_rmssd'))} ms",
        f"RHR {display_number(latest.get('rhr'))}",
    ]
    if latest.get("strain") is not None:
        parts.append(f"strain {display_number(latest.get('strain'))}")
    line = " | ".join(parts)
    insight = (analysis.get("insights") or [""])[0]
    return line + ("\n" + insight if insight else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("auth-url")
    exchange = sub.add_parser("exchange-code")
    exchange.add_argument("code")
    analyze_cmd = sub.add_parser("analyze")
    analyze_cmd.add_argument("--days", type=int, default=90)
    analyze_cmd.add_argument("--format", choices=["json", "markdown", "telegram"], default="json")
    args = parser.parse_args()

    if args.cmd == "auth-url":
        print(auth_url(load_config()))
        return 0
    if args.cmd == "exchange-code":
        token = exchange_code(args.code)
        print(json.dumps({"ok": True, "scope": token.get("scope"), "expires_in": token.get("expires_in")}, indent=2))
        return 0
    if args.cmd == "analyze":
        data = fetch_whoop(args.days)
        analysis = analyze(data)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"raw": data, "analysis": analysis}, indent=2, sort_keys=True))
        if args.format == "json":
            print(json.dumps(analysis, indent=2, sort_keys=True))
        elif args.format == "markdown":
            print(format_markdown(analysis))
        else:
            print(format_telegram(analysis))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"WHOOP unavailable: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
