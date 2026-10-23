"""📊 What you actually use: counted from logs Sloth Lobster already keeps (no new tracking).

The point is the value test: after two weeks the numbers show what to polish and what to retire.
"""
import time

import shiba
from store import read_jsonl_tail

LABELS = [("research", "research"), ("ask", "asks"), ("note", "notes"), ("review", "reviews"),
          ("share", "shares"), ("held", "held notes"), ("chat", "chats")]


def tally(days: int = 7) -> dict[str, int]:
    """Counts for the last `days` days, from the audit log's newest part."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    counts = {k: 0 for k, _ in LABELS}
    for r in read_jsonl_tail(shiba.AUDIT, 3_000_000):
        if r.get("ts", "") < cutoff:
            continue
        kind, action = r.get("kind"), r.get("action")
        if kind == "research_queued":
            counts["research"] += 1
        elif kind == "decision" and r.get("verdict") == "ALLOW" and action in ("ask", "save_note", "reply"):
            counts[{"ask": "ask", "save_note": "note", "reply": "chat"}[action]] += 1
        elif kind == "card_reviewed":
            counts["review"] += 1
        elif kind == "shared":
            counts["share"] += 1
        elif kind == "held_note":
            counts["held"] += 1
    return counts


def line(days: int = 7) -> str:
    """'📊 This week: 4 research · 12 asks · 3 notes · 9 reviews', leaving out zeros."""
    c = tally(days)
    parts = [f"{c[k]} {label}" for k, label in LABELS if c[k]]
    return ("📊 This week: " + " · ".join(parts)) if parts else "📊 This week: nothing yet"
