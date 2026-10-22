"""The allowlist. Sloth Lobster can do these things and nothing else.

Every action passes Shiba (shiba.decide) before it runs. Nothing here can
delete data: notes and research jobs are appended, never edited or removed.

Tiers:
  "read"    -> runs immediately; changes nothing
  "write"   -> runs immediately, BUT needs your Yes if the model has just
               seen web facts (they could be trying to plant something)
  "confirm" -> always asks you to tap Yes first (none today; kept for future powers)

To add a power: one function, one entry below with a tier, a Shiba rule if it
needs one, and its own tests. Start it as read-only.
"""
import elephant
import research
import shiba
from config import DATA_DIR
from store import append_jsonl, read_jsonl

NOTES = DATA_DIR / "notes.jsonl"


def save_note(text: str) -> str:
    if not text.strip():
        return "Nothing to save."
    append_jsonl(NOTES, {"text": text.strip()})
    return elephant.note(text)


def list_notes(_: str = "") -> str:
    notes = read_jsonl(NOTES)[-10:]
    if not notes:
        return "No notes yet."
    return "\n".join(f"• {n['text']}  ({n['ts']})" for n in notes)


def ask(text: str) -> str:
    return elephant.ask(text or "")["text"]


def research_topic(text: str) -> str:
    if not text.strip():
        return "What should I research?"
    job_id = research.enqueue(text.strip())
    return f"🦞 On it. Research #{job_id} queued; the brief lands here in a few minutes. /stop cancels it."


ACTIONS = {
    "reply":      {"fn": None,           "tier": "read",  "desc": "just talk; no action"},
    "ask":        {"fn": ask,            "tier": "read",  "desc": "answer from the owner's vault and notes; text = the question"},
    "list_notes": {"fn": list_notes,     "tier": "read",  "desc": "show recent notes"},
    "save_note":  {"fn": save_note,      "tier": "write", "desc": "save a note or a fact about the owner; text = the note"},
    "research":   {"fn": research_topic, "tier": "write", "desc": "research a topic on the web and send a brief; text = the question"},
}


def run_action(name: str, text: str) -> str:
    spec = ACTIONS.get(name)
    if spec is None:
        return "That action isn't allowed."
    if shiba.paused() and spec["tier"] != "read":        # second gate, on purpose
        return "🐕 Sloth Lobster is paused, so nothing was changed. /resume when you're ready."
    return spec["fn"](text) if spec["fn"] else ""
