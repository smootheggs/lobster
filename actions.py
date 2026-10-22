"""The allowlist. The Lobster can do these things and nothing else.

Storage is append-only: records are added, never edited or removed, so no
action here can destroy data. Completing a task appends a "done" record.

Tiers:
  "read"    -> runs immediately; changes nothing
  "write"   -> runs immediately, BUT needs your Yes if the model has just
               seen web facts (they could be trying to plant something)
  "confirm" -> always asks you to tap Yes first
"""
import json
import time

import memory
from config import DATA_DIR

NOTES = DATA_DIR / "notes.jsonl"
TASKS = DATA_DIR / "tasks.jsonl"


def _append(path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def save_note(text: str) -> str:
    if not text.strip():
        return "Nothing to save."
    _append(NOTES, {"text": text.strip()})
    return "📝 Saved."


def list_notes(_: str = "") -> str:
    notes = _read(NOTES)[-10:]
    if not notes:
        return "No notes yet."
    return "\n".join(f"• {n['text']}  ({n['ts']})" for n in notes)


def _open_tasks() -> dict[int, dict]:
    tasks: dict[int, dict] = {}
    for rec in _read(TASKS):
        if rec["type"] == "add":
            tasks[rec["id"]] = rec
        elif rec["type"] == "done":
            tasks.pop(rec["id"], None)
    return tasks


def add_task(text: str) -> str:
    if not text.strip():
        return "No task given."
    next_id = max((r["id"] for r in _read(TASKS)), default=0) + 1
    _append(TASKS, {"type": "add", "id": next_id, "text": text.strip()})
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
    _append(TASKS, {"type": "done", "id": task_id})
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


ACTIONS = {
    "reply":         {"fn": None,          "tier": "read",    "desc": "just talk; no action"},
    "list_notes":    {"fn": list_notes,    "tier": "read",    "desc": "show recent notes"},
    "list_tasks":    {"fn": list_tasks,    "tier": "read",    "desc": "show open tasks"},
    "search_memory": {"fn": search_memory, "tier": "read",    "desc": "look something up in long-term memory; text = what to look for"},
    "save_note":     {"fn": save_note,     "tier": "write",   "desc": "save a note; text = the note"},
    "add_task":      {"fn": add_task,      "tier": "write",   "desc": "add a to-do; text = the task"},
    "remember":      {"fn": remember,      "tier": "write",   "desc": "store a lasting fact about the owner; text = the fact"},
    "complete_task": {"fn": complete_task, "tier": "confirm", "desc": "mark a task done; text = task number"},
}


def needs_approval(action: str, tainted: bool) -> bool:
    """The whole security policy in one place."""
    tier = ACTIONS[action]["tier"]
    return tier == "confirm" or (tier == "write" and tainted)


def run_action(name: str, text: str) -> str:
    spec = ACTIONS.get(name)
    if spec is None:
        return "That action isn't allowed."
    return spec["fn"](text) if spec["fn"] else ""
