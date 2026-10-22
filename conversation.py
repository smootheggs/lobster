"""The conversation engine: one decision path for every way you talk to Sloth Lobster.

Telegram and the web app are thin adapters that call these functions, so the
safety logic exists exactly once:
  handle()   - a free-form message: the model picks one allowlisted action
  note(), ask(), research(), save_link(), stop(), new_chat()
             - your commands: no model involved, so they're fast and work with the model down
  approve() / cancel() - answer an approval card

  your text -> brain (model picks an action) -> sticky taint -> Shiba
            -> ALLOW: run it · ASK: one-time approval card · BLOCK: say why and what to do

The session (recent history and the taint counter) is saved in
data/session.json, so a restart doesn't make Sloth Lobster forget to be careful.
"""
import asyncio
import json
import re

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
    "Sloth Lobster is paused": "Send /resume first.",
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


# Typed commands are handled by code, never guessed at by the model.
COMMAND_HELP = ("Commands: /research <question>, /ask <question>, /note <text>, /save <link>, "
                "/status, /stop, /new, /pause, /resume")


def command(text: str) -> str | None:
    """A message starting with "/" is a command. Returns the reply, or None if it isn't one."""
    t = text.strip()
    if not t.startswith("/"):
        return None
    name, _, arg = t[1:].partition(" ")
    name = name.split("@")[0].lower()
    simple = {"research": research, "ask": ask, "note": note, "save": save_link}
    if name in simple:
        return simple[name](arg)
    if name == "stop":
        return stop()
    if name == "new":
        return new_chat()
    if name == "pause":
        return shiba.pause()
    if name == "resume":
        return shiba.resume()
    if name == "status":
        return "Tap the dot at the top right for the full status." + (
            f" {len(jobs.pending())} research job(s) waiting." if jobs.pending() else "")
    return f"There's no /{name} command. {COMMAND_HELP}"


# Small words that don't show what a message is about.
_FILLER = set("""research about please could would should what when where which that this these those with
look into find tell more some have want need does into from your there their them they then than also just
like make know give show start check status jobs digest hello thanks okay sure yeah""".split())


def _topic_words(text: str) -> set[str]:
    words = re.findall(r"[^\W\d_]{4,}", text.lower())
    return {w[:5] for w in words if w not in _FILLER}


def _research_fits(message: str, question: str) -> bool:
    """The model may only propose research about what your latest message is about.
    Stops a small model from repeating an old topic when you say "hello" or "/status"."""
    return bool(_topic_words(message) & _topic_words(question))


async def _handle(text: str, busy: str | None) -> dict:
    typed = command(text)
    if typed is not None:
        return {"reply": (f"{busy}\n\n" if busy else "") + typed, "code": None, "action": "command"}
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
    if action == "research":
        same = next((j for j in jobs.pending() if j["question"].lower() == " ".join(arg.split()).lower()), None)
        if same or not _research_fits(text, arg):
            hint = (f"Research #{same['id']} on that is already queued." if same
                    else f"If you'd like me to research “{arg}”, tap Research or send /research.")
            reply = "\n\n".join(p for p in (decision["reply"] if not same else "", hint) if p)
            s["history"].append({"role": "assistant", "content": reply})
            save_session(s)
            return {"reply": prefix + reply, "code": None, "action": "reply"}
    verdict, reason = shiba.decide(action, arg, tainted)
    if verdict == shiba.BLOCK:
        save_session(s)
        return {"reply": prefix + _blocked(action, reason), "code": None}
    if verdict == shiba.ASK:
        label = LABELS.get(action, action)
        why = "web facts are in this chat" if tainted else "this action always asks"
        s["history"].append({"role": "assistant", "content": f"(Asked for your OK: {label} “{arg}”.)"})
        save_session(s)
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


def _remember(line: str) -> None:
    """Put the outcome of an approval card into the chat, so the model knows it's settled."""
    s = load_session()
    s["history"].append({"role": "assistant", "content": line})
    save_session(s)


async def approve(code: str) -> str:
    rec, why = shiba.redeem(code)
    if rec is None:
        return why
    result = await asyncio.to_thread(run_action, rec["action"], rec["text"]) or "Done."
    _remember(f"(You approved: {LABELS.get(rec['action'], rec['action'])} “{rec['text']}”. Done; don't propose it again.)")
    return result


def cancel(code: str) -> str:
    shiba.cancel(code)
    _remember("(You said no to that. Don't propose it again unless you ask.)")
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
    reply = run_action("research", text)
    if not spider.online() and not shiba.paused():
        reply += "\n📴 The tablet is offline: it's queued and runs by itself when you're back online."
    return reply


def save_link(url: str) -> str:
    """/save: keep a document you chose, with a receipt."""
    if not url.strip():
        return "Usage: /save <link>"
    if shiba.paused():
        return "⏸️ Sloth Lobster is paused. Send /resume first."
    return spider.save_document(url.strip())


def stop() -> str:
    n = jobs.stop_all()
    return (f"⏹️ Stopped {n} research job{'s' if n != 1 else ''}. Whatever was already stored is kept."
            if n else "Nothing is running.")


def new_chat() -> str:
    reset_session()
    return "🧽 Fresh start: chat history and caution cleared. Notes and memory are untouched."
