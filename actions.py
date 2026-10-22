"""The allowlist. The Lobster can do these things and nothing else.

Every action passes Shiba (shiba.decide) before it runs.

Storage is append-only: records are added, never edited or removed, so no
action here can destroy data. Completing a task appends a "done" record.

Tiers:
  "read"    -> runs immediately; changes nothing
  "write"   -> runs immediately, BUT needs your Yes if the model has just
               seen web facts (they could be trying to plant something)
  "confirm" -> always asks you to tap Yes first
"""
import memory
import research
import shiba
from store import append_jsonl, read_jsonl
from config import DATA_DIR

NOTES = DATA_DIR / "notes.jsonl"
TASKS = DATA_DIR / "tasks.jsonl"


def save_note(text: str) -> str:
    if not text.strip():
        return "Nothing to save."
    append_jsonl(NOTES, {"text": text.strip()})
    return "📝 Saved."


def list_notes(_: str = "") -> str:
    notes = read_jsonl(NOTES)[-10:]
    if not notes:
        return "No notes yet."
    return "\n".join(f"• {n['text']}  ({n['ts']})" for n in notes)


def _open_tasks() -> dict[int, dict]:
    tasks: dict[int, dict] = {}
    for rec in read_jsonl(TASKS):
        if rec["type"] == "add":
            tasks[rec["id"]] = rec
        elif rec["type"] == "done":
            tasks.pop(rec["id"], None)
    return tasks


def add_task(text: str) -> str:
    if not text.strip():
        return "No task given."
    next_id = max((r["id"] for r in read_jsonl(TASKS)), default=0) + 1
    append_jsonl(TASKS, {"type": "add", "id": next_id, "text": text.strip()})
    return f"✅ Task #{next_id} added."


def list_tasks(_: str = "") -> str:
    tasks = _open_tasks()
    if not tasks:
        return "No open tasks."
    return "\n".join(f"#{i} {t['text']}" for i, t in sorted(tasks.items()))


def complete_task(text: str) -> str:
    digits = "".join(c for c in text if c.isdigit())
    if not digits:
        return "Which task number?"
    task_id = int(digits)
    if task_id not in _open_tasks():
        return f"No open task #{task_id}."
    append_jsonl(TASKS, {"type": "done", "id": task_id})
    return f"Task #{task_id} marked done."


def remember(text: str) -> str:
    if not text.strip():
        return "Nothing to remember."
    memory.remember(text)
    return "🐘 Remembered (added to MEMORY.md)."


def search_memory(text: str) -> str:
    hits = memory.search(text or "", k=8)
    if not hits:
        return "Nothing in memory about that."
    icon = {"owner": "👤", "web": "🌐"}
    return "\n".join(f"{icon[h['tag']]} {h['text']}" + (f"  ({h['source']})" if h["tag"] == "web" else "")
                     for h in hits)


def research_topic(text: str) -> str:
    if not text.strip():
        return "What should I research?"
    job_id = research.enqueue(text.strip())
    return f"🕷️ Research job #{job_id} queued. I'll send a brief when it's done, usually within a few minutes."


ACTIONS = {
    "reply":         {"fn": None,          "tier": "read",    "desc": "just talk; no action"},
    "list_notes":    {"fn": list_notes,    "tier": "read",    "desc": "show recent notes"},
    "list_tasks":    {"fn": list_tasks,    "tier": "read",    "desc": "show open tasks"},
    "search_memory": {"fn": search_memory, "tier": "read",    "desc": "look something up in long-term memory; text = what to look for"},
    "save_note":     {"fn": save_note,     "tier": "write",   "desc": "save a note; text = the note"},
    "add_task":      {"fn": add_task,      "tier": "write",   "desc": "add a to-do; text = the task"},
    "remember":      {"fn": remember,      "tier": "write",   "desc": "store a lasting fact about the owner; text = the fact"},
    "research":      {"fn": research_topic, "tier": "write",  "desc": "research a topic on the web and send a brief; text = the question"},
    "complete_task": {"fn": complete_task, "tier": "confirm", "desc": "mark a task done; text = task number"},
}


def run_action(name: str, text: str) -> str:
    spec = ACTIONS.get(name)
    if spec is None:
        return "That action isn't allowed."
    if shiba.paused() and spec["tier"] != "read":        # second gate, on purpose
        return "⏸️ Lobster is paused. Nothing was changed."
    return spec["fn"](text) if spec["fn"] else ""
