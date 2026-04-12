"""
Recurring bills configuration.

Edit this file to add/remove/update bills.
- amount: fixed dollar amount, or None if it varies (credit cards, utilities)
- due_day: day of month (1-31). Use 28 for "end of month" bills.
- category: for grouping in the meeting report
"""

RECURRING_BILLS = [
    # ── Housing ───────────────────────────────────────────────────────────────
    {"name": "Chase Mortgage", "amount": None, "due_day": 1, "category": "Housing"},
    {"name": "Third Federal LOC", "amount": None, "due_day": 15, "category": "Housing"},

    # ── Auto ──────────────────────────────────────────────────────────────────
    {"name": "GM Financial (truck)", "amount": None, "due_day": 10, "category": "Auto"},

    # ── Credit Cards ──────────────────────────────────────────────────────────
    {"name": "Chase Freedom (...6533)", "amount": None, "due_day": 20, "category": "Credit Cards"},
    {"name": "Chase Credit Card (...3403)", "amount": None, "due_day": 20, "category": "Credit Cards"},
    {"name": "Chase Credit Card (...5805)", "amount": None, "due_day": 20, "category": "Credit Cards"},
    {"name": "Amex Platinum (...1008)", "amount": None, "due_day": 25, "category": "Credit Cards"},
    {"name": "Pottery Barn (...8688)", "amount": None, "due_day": 25, "category": "Credit Cards"},
    {"name": "Home Depot (...8909)", "amount": None, "due_day": 25, "category": "Credit Cards"},

    # ── Subscriptions / Utilities ─────────────────────────────────────────────
    # Add yours here:
    # {"name": "Internet", "amount": 80.00, "due_day": 5, "category": "Utilities"},
    # {"name": "Electric", "amount": None, "due_day": 15, "category": "Utilities"},
    # {"name": "Netflix", "amount": 22.99, "due_day": 18, "category": "Subscriptions"},
]
