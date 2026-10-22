"""The conversation engine: one decision path for every way you talk to Lobster.

Telegram and the web app are thin adapters that call these functions, so the
safety logic exists exactly once:
  handle()   - a free-form message: the model picks one allowlisted action
  note(), ask(), research(), save_link(), stop(), new_chat()
             - your commands: no model involved, so they're fast and work with the model down
  approve() / cancel() - answer an approval card

  your text -> brain (model picks an action) -> sticky taint -> Shiba
            -> ALLOW: run it · ASK: one-time approval card · BLOCK: say why and what to do

The session (recent history and the taint counter) is saved in
data/session.json, so a restart doesn't make Lobster forget to be careful.
"""
import asyncio
import json

import research as jobs
import shiba
import spider
from actions import run_action
from brain import think
from config import DATA_DIR
from store import write_atomic

SESSION = DATA_DIR / "session.json"
HISTORY_TURNS = 10      # short memory keeps a small model focused
TAINT_MEMORY = 6        # after seeing web facts, stay cautious for this many turns
_turn = asyncio.Lock()  # Telegram and the web app take turns, so neither overwrites the other's session

LABELS = {"save_note": "Save note", "research": "Start research"}   # what approval buttons say
NEXT_STEP = {           # what to tell you when Shiba blocks something
    "Lobster is paused": "Send /resume first.",
    "not on the allowlist": "That's not something I can do.",
    "control characters": "Remove the special characters and try again.",
    "longer than": "Try a shorter message, or split it in two.",
}


# --- Session (persisted) ------------------------------------------------------------

def load_session() -> dict:
    try:
        s = json.loads(SESSION.read_text(encoding="utf-8"))
        return {"history": list(s.get("history", []))[-HISTORY_TURNS:], "taint_turns": int(s.get("taint_turns", 0))}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {"history": [], "taint_turns": 0}


def save_session(s: dict) -> None:
    write_atomic(SESSION, json.dumps({"history": s["history"][-HISTORY_TURNS:], "taint_turns": s["taint_turns"]},
                                     ensure_ascii=False))


def reset_session() -> None:
    save_session({"history": [], "taint_turns": 0})


def mark_tainted() -> None:
    """Web content was just shown to you (e.g. a research brief): stay cautious for a while."""
    s = load_session()
    s["taint_turns"] = TAINT_MEMORY
    save_session(s)


# --- The decision path ---------------------------------------------------------------

def _blocked(action: str, reason: str) -> str:
    hint = next((v for k, v in NEXT_STEP.items() if k in reason), "")
    return f"🐕 Shiba blocked {action}: {reason}. {hint}".strip()


async def handle(text: str, busy: str | None = None) -> dict:
    """One message in, one reply out: {"reply": str, "code": approval code or None}."""
    async with _turn:
        return await _handle(text, busy)


async def _handle(text: str, busy: str | None) -> dict:
    s = load_session()
    s["history"].append({"role": "user", "content": text})
    s["history"] = s["history"][-HISTORY_TURNS:]
    prefix = f"{busy}\n\n" if busy else ""
    try:
        decision = await think(s["history"])
    except Exception as e:
        save_session(s)
        return {"reply": f"{prefix}🧠 My model isn't responding ({type(e).__name__}). "
                         "/note, /ask, /research, /save and /status still work.", "code": None}

    # Taint is sticky: web facts from earlier turns are still in the conversation.
    if decision["tainted"]:
        s["taint_turns"] = TAINT_MEMORY
    tainted = s["taint_turns"] > 0
    s["taint_turns"] = max(0, s["taint_turns"] - 1)

    action, arg = decision["action"], decision["text"]
    verdict, reason = shiba.decide(action, arg, tainted)
    if verdict == shiba.BLOCK:
        save_session(s)
        return {"reply": prefix + _blocked(action, reason), "code": None}
    if verdict == shiba.ASK:
        save_session(s)
        label = LABELS.get(action, action)
        why = "web facts are in this chat" if tainted else "this action always asks"
        return {"reply": f"{prefix}{decision['reply']}\n\n{label} “{arg}”? I'm asking because {why}. "
                         "(valid 10 minutes)",
                "code": shiba.new_approval(action, arg),
                "ask": {"label": label, "text": arg, "why": why}}

    result = await asyncio.to_thread(run_action, action, arg)
    out = "\n\n".join(p for p in (decision["reply"], result) if p) or "🦞"
    # Don't feed raw memory search results back into the model's context.
    s["history"].append({"role": "assistant", "content": decision["reply"] if action == "ask" else out})
    save_session(s)
    return {"reply": prefix + out, "code": None, "action": action}


async def approve(code: str) -> str:
    rec, why = shiba.redeem(code)
    if rec is None:
        return why
    return await asyncio.to_thread(run_action, rec["action"], rec["text"]) or "Done."


def cancel(code: str) -> str:
    shiba.cancel(code)
    return "Cancelled. Nothing was changed."


def note(text: str) -> str:
    """/note: you typed it yourself, so no model is involved and it isn't tainted."""
    if not text.strip():
        return "Usage: /note <text>"
    verdict, reason = shiba.decide("save_note", text, tainted=False)
    if verdict == shiba.BLOCK:
        return _blocked("save_note", reason)
    return run_action("save_note", text)


def ask(text: str) -> str:
    """/ask: answers from your vault only. No web, no model."""
    if not text.strip():
        return "Usage: /ask <question>"
    shiba.decide("ask", text[:500], tainted=False)          # logged; reading is never blocked
    return run_action("ask", text)


def research(text: str) -> str:
    if not text.strip():
        return "Usage: /research <question>"
    verdict, reason = shiba.decide("research", text, tainted=False)
    if verdict == shiba.BLOCK:
        return _blocked("research", reason)
    return run_action("research", text)


def save_link(url: str) -> str:
    """/save: keep a document you chose, with a receipt."""
    if not url.strip():
        return "Usage: /save <link>"
    if shiba.paused():
        return "⏸️ Lobster is paused. Send /resume first."
    return spider.save_document(url.strip())


def stop() -> str:
    n = jobs.stop_all()
    return (f"⏹️ Stopped {n} research job{'s' if n != 1 else ''}. Whatever was already stored is kept."
            if n else "Nothing is running.")


def new_chat() -> str:
    reset_session()
    return "🧽 Fresh start: chat history and caution cleared. Notes and memory are untouched."
