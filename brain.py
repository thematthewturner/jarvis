import asyncio
import anthropic
import logging
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL_CHAT

logger = logging.getLogger("jarvis.brain")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MAX_TOOL_TURNS = 8
MODEL_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = """You are Jarvis, a personal executive assistant for Matt.
You are direct, efficient, and proactive. You speak concisely.

Your tools — use them immediately without asking:
- Todoist: add_task, update_task, get_tasks
- Google Calendar: get_calendar_events
- Google Drive: search_drive, read_drive_file
- Gmail: get_emails, send_email, archive_gmail_email, star_gmail_email, mark_gmail_email_read
  → thematthewturner@gmail.com — work/professional email
  → archive_gmail_email/star_gmail_email/mark_gmail_email_read all take a Gmail search query (e.g. 'from:someone@example.com is:inbox')
- iCloud Mail: get_icloud_emails, search_icloud_emails, send_icloud_email, flag_icloud_email, archive_icloud_email, move_icloud_email, trash_icloud_email, mark_icloud_email_read
  → thematthewturner@icloud.com — personal/family/school email lives here
  → ALWAYS use search_icloud_emails for school, kids, teachers, family, or personal email queries
  → flag_icloud_email to star/flag, archive_icloud_email to archive, trash_icloud_email to delete
- iCloud Calendar: get_icloud_calendar, create_icloud_event — family/personal schedule
- Notion: save_notion_note, search_notion, read_notion_page
- Monarch Money: get_account_balances, get_recent_transactions, get_budget_summary
- Utility Bills: get_utility_bills
  → When Matt asks what his water, electric, or gas bill is this month → call get_utility_bills immediately
- Finance Digest: get_finance_digest
  → When Matt asks about his finance digest, open financial items, what needs review, what financial intel is new, or what bills are due soon → call get_finance_digest immediately
- Weekly Finance Digest: get_weekly_financial_digest
  → When Matt asks for a weekly financial digest, rolling 7-day / 30-day spending trends, category movers, outliers, or why spending changed recently → call get_weekly_financial_digest immediately
- Spending Trend: get_spending_trend
  → When Matt asks whether spending is trending up or down, how he is doing this week, or to analyze recent transaction spending trend → call get_spending_trend immediately
- Pool: log_pool_chemistry, log_pool_chemical, get_pool_status
  → When Matt mentions a pool test result (FC, pH, TA, salt, CYA, CH) → call log_pool_chemistry immediately
  → When Matt says he added chemicals → call log_pool_chemical immediately
  → When Matt describes a pool experience, observation, or issue (e.g. "water looked green", "pump was making noise") → ALSO call save_notion_note to preserve the context
- Fitness: get_fitness_status, log_pr, get_prs, log_workout
  → When Matt asks how his body feels, recovery, HRV, or readiness → call get_fitness_status immediately
  → When Matt mentions a new PR (lift, benchmark WOD time, any fitness milestone) → call log_pr immediately
  → When Matt asks about his PRs → call get_prs immediately
  → When Matt describes completing a workout or WOD → call log_workout immediately
- Intentionality: save_intentionality_signal, get_intentionality_status
  → When Matt shares a long-lived theme, scripture anchor, book, fatherhood focus, or recurring personal practice he wants held over time → call save_intentionality_signal immediately
  → When Matt asks what intentionality Jarvis is holding → call get_intentionality_status immediately

Rules:
- NEVER say you lack access to iCloud — you have full iCloud access via the tools above
- NEVER say WHOOP is not connected or that you can't access fitness data — call get_fitness_status immediately, it reads from local DB and always works
- NEVER tell Matt to authorize, link, or set up any service — just call the relevant tool
- When Matt asks about school, teachers, or kids email → call search_icloud_emails immediately
- When Matt asks about family/personal calendar or upcoming family events → call get_icloud_calendar immediately
- When Matt mentions something to do → add_task silently, no confirmation needed
- When Matt mentions an expense/idea/note → save_notion_note silently
- When Matt asks you to remember a personal growth theme or recurring relational commitment, store it in Intentionality instead of a generic note
- Always act first; never ask permission to use a tool

CRITICAL — tool use is MANDATORY for ALL actions:
- You MUST call the relevant tool BEFORE reporting any action as complete
- NEVER say "Done", "Added", "Created", "Sent", "Logged", "Saved", "Recorded", or similar without first receiving a tool result confirming it
- This applies to EVERY tool: Todoist, pool logging, Notion notes, calendar events, emails — everything
- Conversation history shows what you said before; it does NOT give you permission to skip tool calls now

Current date/time context will be provided with each message.
"""

TOOLS = [
    {
        "name": "save_note",
        "description": "Save a note, task, idea, or expense to local storage. Use proactively when Matt mentions anything worth remembering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["task", "note", "expense", "idea"],
                    "description": "Type of item to save"
                },
                "content": {
                    "type": "string",
                    "description": "The item content"
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional structured data (amount, due_date, priority, etc.)"
                }
            },
            "required": ["category", "content"]
        }
    },
    {
        "name": "get_current_time",
        "description": "Get the current date and time in ET",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "save_intentionality_signal",
        "description": "Persist a long-lived theme, context note, scripture/book anchor, or recurring personal rhythm that Jarvis should keep in view for future digests and nudges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_type": {
                    "type": "string",
                    "enum": ["theme", "context", "source", "rhythm"],
                    "description": "Type of long-lived intentionality memory to store"
                },
                "content": {
                    "type": "string",
                    "description": "The theme text, context note, source reference, or rhythm description"
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional structured details. For source use source_kind=scripture|book|other and note. For rhythm use cadence_days, task_text, due_string, priority, and optional last_completed_at."
                }
            },
            "required": ["signal_type", "content"]
        }
    },
    {
        "name": "get_intentionality_status",
        "description": "Show the current intentionality theme, anchors, context, and recurring rhythms Jarvis is holding.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "add_task",
        "description": "Add a task to Todoist. Use when Matt mentions something he needs to do. Supports natural language due dates like 'tomorrow', 'Friday at 3pm', 'next week'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The task description"
                },
                "due_string": {
                    "type": "string",
                    "description": "Natural language due date, e.g. 'tomorrow', 'Friday at 2pm', 'next Monday'"
                }
            },
            "required": ["content"]
        }
    },
    {
        "name": "update_task",
        "description": "Update an existing Todoist task — change its due date, priority, or content. Use the task_id returned by add_task. Priority: 4=P1 urgent (red), 3=P2, 2=P3, 1=P4 normal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by add_task (e.g. '6g4g3467jH6p7VHh')"
                },
                "content": {
                    "type": "string",
                    "description": "New task text (omit to keep existing)"
                },
                "due_string": {
                    "type": "string",
                    "description": "New due date, e.g. 'today', 'tomorrow', 'Friday at 3pm'"
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority: 4=P1 urgent (red flag), 3=P2, 2=P3, 1=P4 normal"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "get_tasks",
        "description": "Get tasks from Todoist. Use when Matt asks what he needs to do, what's on his list, or what's due.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "Todoist filter string. Defaults to 'today | overdue'. Use 'all' for everything, or 'due before: +7 days' for the week ahead."
                }
            }
        }
    },
    {
        "name": "get_calendar_events",
        "description": "Get Google Calendar events for a given date. Use when Matt asks about his schedule, what's on his calendar, or what's happening today/tomorrow.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to retrieve events for. Use 'today', 'tomorrow', or ISO date YYYY-MM-DD. Defaults to 'today'."
                }
            }
        }
    },
    {
        "name": "get_emails",
        "description": "Get emails from Gmail. Use when Matt asks about his email, inbox, or messages. Supports Gmail search syntax.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query. Examples: 'is:unread', 'from:boss@example.com', 'subject:invoice is:unread'. Defaults to 'is:unread'."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max number of emails to return. Defaults to 5."
                },
                "include_body": {
                    "type": "boolean",
                    "description": "If true, return full email body. Defaults to false (headers + preview only)."
                }
            }
        }
    },
    {
        "name": "send_email",
        "description": "Send an email via Gmail. Only use when Matt explicitly asks you to send an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address"
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line"
                },
                "body": {
                    "type": "string",
                    "description": "Email body text"
                }
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "search_drive",
        "description": "Search Google Drive for files. Use when Matt asks about a document, spreadsheet, or file. Returns file names and IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Drive search query. Examples: \"name contains 'budget'\", \"mimeType='application/vnd.google-apps.document'\". Use empty string for recent files."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return. Defaults to 10."
                }
            }
        }
    },
    {
        "name": "read_drive_file",
        "description": "Read the content of a Google Doc or Sheet. Use the file ID from search_drive results. Only works with Google Docs and Sheets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID (from search_drive results)"
                }
            },
            "required": ["file_id"]
        }
    },
    {
        "name": "save_notion_note",
        "description": "Save a note, idea, or expense to Notion. Use this whenever Matt mentions anything worth remembering — proactively, without asking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title or summary of the note"
                },
                "content": {
                    "type": "string",
                    "description": "Full content or detail. Omit if title is sufficient."
                },
                "category": {
                    "type": "string",
                    "enum": ["note", "idea", "expense", "task"],
                    "description": "Type of entry"
                }
            },
            "required": ["title"]
        }
    },
    {
        "name": "search_notion",
        "description": "Search Notion for pages matching a query. Use when Matt asks to find something he wrote down, a previous note, or anything in Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_notion_page",
        "description": "Read the full content of a Notion page. Use the page ID from search_notion results, or a Notion page URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID (from search_notion) or a full Notion page URL"
                }
            },
            "required": ["page_id"]
        }
    },
    {
        "name": "get_account_balances",
        "description": "Get current balances for all financial accounts from Monarch Money. Use when Matt asks about his money, net worth, or account balances.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_recent_transactions",
        "description": "Get recent transactions from Monarch Money. Use when Matt asks about spending, recent purchases, or financial activity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look back. Defaults to 7."
                },
                "category": {
                    "type": "string",
                    "description": "Optional category keyword to filter by (e.g. 'groceries', 'dining')"
                }
            }
        }
    },
    {
        "name": "get_budget_summary",
        "description": "Get current month budget vs actual spending by category from Monarch Money.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_utility_bills",
        "description": "Scan Gmail and iCloud for the current month's Water, Electric, and Gas bill amounts, then return the most reliable monthly signal found for each utility.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {
                    "type": "integer",
                    "description": "Optional target year. Defaults to the current year."
                },
                "month": {
                    "type": "integer",
                    "description": "Optional target month 1-12. Defaults to the current month."
                },
                "refresh": {
                    "type": "boolean",
                    "description": "If true, rescan email before returning results. Defaults to true."
                }
            }
        }
    },
    {
        "name": "get_finance_digest",
        "description": "Return a concise digest of open financial review items, including past-due items, due-soon items, newly processed financial documents, and important highlights.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to consider due soon. Defaults to 10."
                },
                "recent_days": {
                    "type": "integer",
                    "description": "How many days back to consider newly processed. Defaults to 7."
                },
                "limit": {
                    "type": "integer",
                    "description": "How many open items to scan for the digest. Defaults to 20."
                }
            }
        }
    },
    {
        "name": "get_weekly_financial_digest",
        "description": "Write a tight weekly financial digest covering rolling 7-day and 30-day spending trends, category movers, major transactions, outliers, and excluded transfer context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "save_to_notion": {
                    "type": "boolean",
                    "description": "If true, save the digest to Notion. Defaults to false for ad hoc requests."
                }
            }
        }
    },
    {
        "name": "get_spending_trend",
        "description": "Analyze recent transaction spending trend versus prior weeks, focusing on true spending pace rather than transfers and internal payments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "integer",
                    "description": "How many prior weeks to use as the comparison baseline. Defaults to 4."
                }
            }
        }
    },

    # ── iCloud ────────────────────────────────────────────────────────────────
    {
        "name": "get_icloud_emails",
        "description": "Get emails from Matt's iCloud Mail account. Use for personal/family emails, school notifications, kid activity emails, and anything not in Gmail. Prefer over get_emails when Matt asks about his personal inbox, family mail, or iCloud.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since_hours": {
                    "type": "integer",
                    "description": "Hours to look back. Defaults to 24."
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "If true, only return unread emails. Defaults to true."
                }
            }
        }
    },
    {
        "name": "search_icloud_emails",
        "description": "Search Matt's iCloud emails by sender, subject, or keyword. Use when looking for school emails, bills, activity updates, or any personal email content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to find in sender address, subject line, or email body"
                },
                "since_days": {
                    "type": "integer",
                    "description": "Days to look back. Defaults to 7."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_icloud_calendar",
        "description": "Get events from Matt's iCloud Calendar (personal/family calendar). Use when Matt asks about family schedule, kids activities, personal appointments, or upcoming family events. Use 'upcoming' with days for a full upcoming view.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date range to fetch. Use 'today', 'tomorrow', 'week', 'upcoming', or ISO date YYYY-MM-DD. Defaults to 'today'."
                },
                "days": {
                    "type": "integer",
                    "description": "When date='upcoming', how many days ahead to include. Defaults to 14."
                },
                "calendar_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: specific calendar names to check (e.g. ['Home', 'Kids']). Omit to check all."
                }
            }
        }
    },
    {
        "name": "create_icloud_event",
        "description": "Create a new event on Matt's iCloud Calendar. Use when Matt asks to schedule something on his personal/family calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start":   {"type": "string", "description": "Start datetime in ISO format (e.g. '2026-03-01T14:00:00')"},
                "end":     {"type": "string", "description": "End datetime in ISO format"},
                "calendar_name": {"type": "string", "description": "Calendar to add the event to. Defaults to 'Home'."},
                "location":    {"type": "string", "description": "Event location (optional)"},
                "description": {"type": "string", "description": "Event notes (optional)"}
            },
            "required": ["summary", "start", "end"]
        }
    },
    {
        "name": "send_icloud_email",
        "description": "Send an email from Matt's iCloud account (thematthewturner@icloud.com). Use when Matt asks to send a personal email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body":    {"type": "string", "description": "Email body text"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "flag_icloud_email",
        "description": "Star (flag) or un-star an email in Matt's iCloud inbox. Use when Matt asks to flag, star, mark important, or un-star an iCloud email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "The email UID from get_icloud_emails or search_icloud_emails (format: 'account@email.com:12345')"
                },
                "starred": {
                    "type": "boolean",
                    "description": "True to star/flag, false to un-star. Defaults to true."
                }
            },
            "required": ["uid"]
        }
    },
    {
        "name": "archive_gmail_email",
        "description": "Archive Gmail messages matching a search query — removes them from inbox. Use when Matt wants to archive, clear, or clean up Gmail emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query. Examples: 'from:linkedin.com', 'subject:newsletter is:inbox', 'from:noreply@company.com'. Up to 25 matching messages will be archived."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "star_gmail_email",
        "description": "Star or un-star Gmail messages matching a search query. Use when Matt wants to flag or highlight a Gmail email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query. Examples: 'from:boss@company.com subject:contract', 'is:unread from:client@example.com'."
                },
                "starred": {
                    "type": "boolean",
                    "description": "True to star, false to un-star. Defaults to true."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "mark_gmail_email_read",
        "description": "Mark Gmail messages as read or unread. Use when Matt asks to mark emails as read/unread.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query. Example: 'is:unread from:newsletter@example.com', 'label:inbox is:unread'."
                },
                "seen": {
                    "type": "boolean",
                    "description": "True to mark as read, false to mark as unread. Defaults to true."
                }
            },
            "required": ["query"]
        }
    },

    # ── Pool ──────────────────────────────────────────────────────────────────
    {
        "name": "log_pool_chemistry",
        "description": "Log a pool water chemistry reading. Call immediately when Matt mentions pool test results — FC, pH, TA, salt, CYA, CH, or any chemistry values. Don't ask, just log it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fc":         {"type": "number", "description": "Free Chlorine ppm"},
                "tc":         {"type": "number", "description": "Total Chlorine ppm"},
                "ph":         {"type": "number", "description": "pH level"},
                "ta":         {"type": "number", "description": "Total Alkalinity ppm"},
                "salt":       {"type": "number", "description": "Salt ppm"},
                "cya":        {"type": "number", "description": "Cyanuric Acid (stabilizer) ppm"},
                "ch":         {"type": "number", "description": "Calcium Hardness ppm"},
                "water_temp": {"type": "number", "description": "Water temperature °F"},
                "iron":       {"type": "number", "description": "Iron ppm"},
                "copper":     {"type": "number", "description": "Copper ppm"},
                "phosphate":  {"type": "number", "description": "Phosphate ppb"},
                "notes":      {"type": "string",  "description": "Any additional notes"}
            }
        }
    },
    {
        "name": "log_pool_chemical",
        "description": "Log a chemical addition to the pool. Call when Matt says he added salt, acid, shock, stabilizer, calcium, algaecide, or any chemical to the pool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chemical": {
                    "type": "string",
                    "description": "Chemical name, e.g. 'salt', 'muriatic_acid', 'sodium_bicarb', 'shock', 'cya', 'calcium_chloride', 'algaecide'"
                },
                "amount": {
                    "type": "string",
                    "description": "Amount added, e.g. '40 lbs', '1 quart', '2 tabs'"
                },
                "notes": {"type": "string", "description": "Optional notes"}
            },
            "required": ["chemical", "amount"]
        }
    },
    {
        "name": "get_pool_status",
        "description": "Get current pool status — live equipment data from Hayward OmniLogic plus latest chemistry reading. Use when Matt asks about his pool.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },

    # ── Fitness ────────────────────────────────────────────────────────────────
    {
        "name": "get_fitness_status",
        "description": "Get today's fitness readiness from WHOOP — recovery score, HRV, resting HR, sleep performance, and strain. Use when Matt asks about his body, recovery, readiness, or how he slept.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "log_pr",
        "description": "Log a personal record (PR) for an exercise or benchmark WOD. Use immediately when Matt mentions a new lift PR, benchmark time, or fitness milestone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name, e.g. 'back squat', 'Fran', 'Clean & Jerk', 'mile run'"
                },
                "value": {
                    "type": "number",
                    "description": "The PR value — weight in lbs, time in seconds, reps, or rounds"
                },
                "unit": {
                    "type": "string",
                    "description": "Unit: 'lbs', 'kg', 'seconds', 'minutes', 'reps', 'rounds'. Defaults to 'lbs'."
                },
                "notes": {
                    "type": "string",
                    "description": "Optional context (e.g. 'touch-and-go', 'felt easy', 'belt assisted')"
                }
            },
            "required": ["exercise", "value"]
        }
    },
    {
        "name": "get_prs",
        "description": "Look up Matt's personal records. Omit exercise to see all PRs; provide exercise name to filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name to look up. Omit for all PRs."
                }
            }
        }
    },
    {
        "name": "log_workout",
        "description": "Log a completed CrossFit WOD or workout. Use when Matt describes a workout he finished.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "WOD name or description, e.g. 'Fran: 21-15-9 thrusters/pull-ups' or 'Open 25.1'"
                },
                "result": {
                    "type": "string",
                    "description": "Score or time, e.g. '4:32', '187 reps', '6 rounds + 12 reps'"
                },
                "rx": {
                    "type": "boolean",
                    "description": "True if completed as prescribed (Rx), false if scaled. Defaults to true."
                },
                "type": {
                    "type": "string",
                    "description": "Workout type: 'crossfit', 'weightlifting', 'cardio', 'gymnastics'. Defaults to 'crossfit'."
                },
                "notes": {
                    "type": "string",
                    "description": "How it felt, strategy notes, injuries, etc."
                }
            },
            "required": ["description"]
        }
    }
]

def _load_context() -> str:
    """Load context.json and format as a system prompt appendix."""
    import json, os
    ctx_path = os.path.join(os.path.dirname(__file__), "data", "context.json")
    try:
        with open(ctx_path) as f:
            ctx = json.load(f)
        lines = ["\n\nContext about Matt:"]
        for cat in ctx.values():
            lines.append(f"\n{cat['label']}:")
            for entry in cat.get("entries", []):
                lines.append(f"- {entry}")
        return "\n".join(lines)
    except Exception:
        return ""


async def think(user_message: str, conversation_history: list[dict]) -> str:
    from tools import handle_tool_call
    messages = list(conversation_history)
    if not (
        messages
        and messages[-1].get("role") == "user"
        and messages[-1].get("content") == user_message
    ):
        messages.append({"role": "user", "content": user_message})
    system = SYSTEM_PROMPT + _load_context()

    for _ in range(MAX_TOOL_TURNS):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.messages.create,
                    model=CLAUDE_MODEL_CHAT,
                    max_tokens=1024,
                    system=system,
                    tools=TOOLS,
                    messages=messages,
                ),
                timeout=MODEL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Claude request timed out after %ss", MODEL_TIMEOUT_SECONDS)
            return "I ran into a timeout while thinking through that. Please try again."

        if response.stop_reason == "tool_use":
            tool_results = []
            assistant_content = response.content

            for block in response.content:
                if block.type == "tool_use":
                    logger.info("TOOL_CALL %s %s", block.name, block.input)
                    try:
                        result = await handle_tool_call(block.name, block.input)
                        logger.info("TOOL_RESULT %s: %s", block.name, str(result)[:200])
                    except Exception as tool_err:
                        result = f"tool error: {tool_err}"
                        logger.error("TOOL_ERROR %s: %s", block.name, tool_err)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
        else:
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_parts)

    logger.error("Claude exceeded max tool turns (%s)", MAX_TOOL_TURNS)
    return "I got stuck in a tool loop and stopped to avoid unintended actions. Please rephrase and try again."
