---
name: jarvis-inbox-cleaner
description: Full-power conservative inbox cleanup workflow for Jarvis on Hermes using Himalaya for iCloud IMAP actions and Gmail API for Gmail actions.
version: 1.2.0
metadata:
  hermes:
    category: personal
---

# Jarvis Inbox Cleaner

Use this skill to run inbox cleanup in the daily digest without creating extra Telegram noise.

## Safety Contract

Default mode is dry-run. In dry-run mode, the skill may read and classify recent messages, diagnose the iCloud account, and list folders, but it must not archive, delete, move, mark read, label, flag, star, send, or create filters.

Write mode requires all of these:

- `--apply`
- `--confirm APPLY_INBOX_CLEANUP`
- a supported action from an explicit allowlist
- a low-risk classification

Write mode may archive low-risk allowlisted newsletter/promotional senders. It must not delete or purge mail.

When uncertain, classify as `keep` or `review`.

## Buckets

- `keep`: personal, family, school, finance, healthcare, legal, work-like, or ambiguous.
- `review`: likely actionable or needs human decision.
- `newsletter`: non-urgent subscription or digest.
- `receipt`: purchase, shipping, donation, subscription, or billing receipt.
- `noise`: promotional or low-signal item.
- `action`: likely actionable personal/admin item.
- `follow_up`: likely waiting/follow-up item.

## Himalaya Coverage

- Diagnose iCloud with `himalaya account doctor`.
- Discover iCloud folders.
- Scan `INBOX`, `Junk`, `Action`, and `Follow Up`.
- Move low-risk allowlisted iCloud messages to `Archive` only in confirmed apply mode.
- Keep Gmail on the Gmail API for archive behavior because labels are first-class there.

## Daily Digest Behavior

The daily digest may include an `Inbox Cleanup` source with a compact dry-run summary:

- counts by bucket
- safe archive candidates
- review candidates
- skipped risky items

It must not send a separate notification.

## Commands

Dry-run scan:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/inbox_cleaner.py scan --format markdown
```

Folder inventory:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/inbox_cleaner.py folders --format json
```

Account diagnosis:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/inbox_cleaner.py doctor --format json
```

JSON for the digest:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/inbox_cleaner.py scan --format json
```

Future write mode:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/inbox_cleaner.py scan --apply --confirm APPLY_INBOX_CLEANUP
```
