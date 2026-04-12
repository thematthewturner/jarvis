#!/usr/bin/env python3
"""
iCloud integration test script.

Run from the jarvis project root:
    python test_icloud.py

Tests IMAP, CalDAV, event creation, and prints a health report.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("test_icloud")
TZ = ZoneInfo("America/New_York")

SEP = "─" * 60


async def test_mail():
    print(f"\n{SEP}")
    print("TEST 1 — iCloud Mail (IMAP)")
    print(SEP)

    from icloud_mail import fetch_unread_emails, get_mailbox_folders

    print("Listing mailbox folders…")
    folders = await get_mailbox_folders()
    print(f"  Folders ({len(folders)}): {', '.join(folders[:10])}")

    print("\nFetching up to 3 recent unread emails (last 72h)…")
    emails = await fetch_unread_emails(since_hours=72)
    if not emails:
        print("  No unread emails found in the last 72 hours.")
    else:
        for i, e in enumerate(emails[:3], 1):
            print(f"\n  [{i}] From:    {e['from']}")
            print(f"      Subject: {e['subject']}")
            print(f"      Date:    {e['date']}")
            print(f"      Attachments: {e['attachments'] or 'none'}")
    return True


async def test_calendar():
    print(f"\n{SEP}")
    print("TEST 2 — iCloud Calendar (CalDAV)")
    print(SEP)

    from icloud_calendar import get_calendars, get_todays_events, get_weeks_events

    print("Discovering calendars…")
    cals = await get_calendars()
    if not cals:
        print("  ERROR: No calendars found — check credentials.")
        return False
    for c in cals:
        print(f"  · {c['name']}")

    print("\nToday's events:")
    events = await get_todays_events()
    if not events:
        print("  (none)")
    else:
        for e in events:
            all_day = " [all-day]" if e.get("all_day") else ""
            print(f"  · {e['summary']}{all_day} — {e.get('start', '?')} [{e['calendar_name']}]")

    print("\nThis week's events:")
    week = await get_weeks_events()
    if not week:
        print("  (none)")
    else:
        for e in week[:8]:
            print(f"  · {e['summary']} — {e.get('start', '?')[:16]}")

    return True


async def test_create_delete_event():
    print(f"\n{SEP}")
    print("TEST 3 — Create + Delete Calendar Event")
    print(SEP)

    from icloud_calendar import create_event, delete_event, get_calendars

    cals = await get_calendars()
    if not cals:
        print("  Skipped — no calendars available.")
        return False

    # Pick first writable calendar (skip shared/read-only ones marked with ⚠️)
    writable = [c for c in cals if "⚠" not in c["name"]]
    cal_name = (writable[0] if writable else cals[0])["name"]
    now = datetime.now(TZ)
    start = now + timedelta(minutes=5)
    end   = start + timedelta(minutes=30)

    print(f"Creating test event on '{cal_name}' calendar…")
    uid = await create_event(
        summary=f"Jarvis Test Event",
        start=start,
        end=end,
        calendar_name=cal_name,
        description="Automated test — safe to delete",
    )
    print(f"  Created! UID: {uid}")

    print("Deleting test event…")
    ok = await delete_event(uid, cal_name)
    print(f"  Deleted: {'✓' if ok else '✗ (may need manual cleanup)'}")
    return True


async def test_health():
    print(f"\n{SEP}")
    print("TEST 4 — Health Check")
    print(SEP)

    from icloud_service import icloud
    status = await icloud.health_check()
    print(f"  IMAP   : {status['imap']}")
    print(f"  CalDAV : {status['caldav']}")
    print(f"  Time   : {status['timestamp']}")


async def main():
    email = os.getenv("ICLOUD_EMAIL")
    if not email:
        print("ERROR: ICLOUD_EMAIL not set in .env")
        sys.exit(1)

    print(f"\niCloud Integration Test — {email}")

    try:
        await test_mail()
    except Exception as e:
        print(f"  MAIL TEST ERROR: {e}")

    try:
        await test_calendar()
    except Exception as e:
        print(f"  CALENDAR TEST ERROR: {e}")

    try:
        await test_create_delete_event()
    except Exception as e:
        print(f"  CREATE/DELETE TEST ERROR: {e}")

    try:
        await test_health()
    except Exception as e:
        print(f"  HEALTH CHECK ERROR: {e}")

    print(f"\n{SEP}")
    print("Tests complete.")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
