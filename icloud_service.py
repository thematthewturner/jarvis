"""
Unified iCloud service — wraps mail + calendar modules into a single interface.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from icloud_mail import (
    fetch_unread_emails,
    search_emails,
    prepare_email_for_claude,
)
from icloud_calendar import get_todays_events, format_events_for_claude

logger = logging.getLogger("jarvis")
TZ = ZoneInfo("America/New_York")


class ICloudService:
    async def morning_briefing(self) -> dict:
        """Today's calendar + unread email summary for the morning brief."""
        events, emails = await asyncio.gather(
            get_todays_events(),
            fetch_unread_emails(since_hours=12),
        )
        return {
            "calendar": format_events_for_claude(events),
            "unread_count": len(emails),
            "unread_subjects": [e["subject"] for e in emails[:5]],
        }

    async def scan_for_activities(self, since_hours: int = 24) -> list[dict]:
        """
        Scan recent iCloud emails for kid activities, bills, and action items.
        Returns emails prepared for Claude analysis.
        """
        activity_keywords = [
            "schedule", "practice", "game", "tournament", "school",
            "homework", "permission", "field trip", "registration",
            "payment", "bill", "due", "invoice",
        ]
        results = []
        seen: set[str] = set()

        from icloud_mail import fetch_unread_emails, fetch_emails_by_subject
        emails = await fetch_unread_emails(since_hours=since_hours)
        for e in emails:
            if e["uid"] not in seen:
                seen.add(e["uid"])
                results.append(e)

        keyword_emails = await fetch_emails_by_subject(activity_keywords, since_days=1)
        for e in keyword_emails:
            if e["uid"] not in seen:
                seen.add(e["uid"])
                results.append(e)

        return [prepare_email_for_claude(e) for e in results[:10]]

    async def health_check(self) -> dict:
        """Test both IMAP and CalDAV connections. Returns status dict."""
        mail_ok = False
        cal_ok  = False
        mail_err = ""
        cal_err  = ""

        try:
            await fetch_unread_emails(since_hours=1)
            mail_ok = True
        except Exception as e:
            mail_err = str(e)

        try:
            from icloud_calendar import get_calendars
            cals = await get_calendars()
            cal_ok = len(cals) > 0
        except Exception as e:
            cal_err = str(e)

        return {
            "imap": "ok" if mail_ok else f"error: {mail_err}",
            "caldav": "ok" if cal_ok else f"error: {cal_err}",
            "timestamp": datetime.now(TZ).isoformat(),
        }


# Module-level singleton
icloud = ICloudService()
