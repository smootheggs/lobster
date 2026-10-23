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
import time

import clock
import research as jobs
import shiba
import spider
from actions import run_action
from brain import think
from config import DATA_DIR, VERSION, WORK_DIR
from store import write_atomic

SESSION = DATA_DIR / "session.json"
HISTORY_TURNS = 6       # short memory keeps a small model focused, and its prompt quick to read
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
        return {"history": list(s.get("history", []))[-HISTORY_TURNS:], "taint_turns": int(s.get("taint_turns", 0)),
                "greeted": str(s.get("greeted", ""))}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return {"history": [], "taint_turns": 0, "greeted": ""}


def save_session(s: dict) -> None:
    write_atomic(SESSION, json.dumps({"history": s["history"][-HISTORY_TURNS:], "taint_turns": s["taint_turns"],
                                      "greeted": s.get("greeted", "")}, ensure_ascii=False))


def reset_session() -> None:
    save_session({"history": [], "taint_turns": 0, "greeted": load_session().get("greeted", "")})


# The sloth's hello: warm, unhurried, never gushing (the app's start area uses the same tone).
HELLOS = {
    "night": ["Still up? I'll keep things quiet.", "A late one. Short and gentle, then.", "Night owl hours. No rush at all."],
    "morning": ["Good morning. Slow and steady today?", "Morning. Coffee first, curiosity second.",
                "Good morning. Take your time, I'm here."],
    "afternoon": ["Good afternoon. Nothing here is in a hurry.", "Afternoon. What's on your mind?",
                  "Good afternoon. A good hour for a little reading."],
    "evening": ["Good evening. Settle in.", "Evening. Feet up, mind open.", "Good evening. Glad you're here."],
}


def greeting(s: dict) -> str:
    """🦥 The first chat of the day starts with a warm hello, and nothing to do. Code-written,
    so it costs no model time; only once a day, so it stays a small touch."""
    today = time.strftime("%Y-%m-%d")
    if s.get("greeted") == today:
        return ""
    s["greeted"] = today
    t = time.localtime()
    hour = t.tm_hour
    part = "night" if hour < 5 or hour >= 23 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    hello = HELLOS[part][t.tm_yday % len(HELLOS[part])]      # the same all day, a different one tomorrow
    return f"🦥 {hello}\n\n"


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
COMMAND_HELP = ("Commands: /research <question>, /deep <question>, /ask <question>, /note <text>, /save <link>, "
                "/wrong (that answer was wrong), /status, /stop, /new, /pause, /resume")
NO_CLOCK = ("🦞 I can't read a clock in my head. Ask me “what time is it in Hong Kong” (or any city) "
            "and I'll work it out exactly.")
MISSES = WORK_DIR / "Sloth Lobster misses.md"


def wrong(correction: str = "") -> str:
    """/wrong: the last answer was wrong. It's written to a list in your work folder (which syncs
    to Drive), so it can become a test and get fixed for good. Nothing else changes."""
    s = load_session()
    hist = s["history"]
    last = next((i for i in range(len(hist) - 1, -1, -1) if hist[i]["role"] == "assistant"), None)
    if last is None:
        return "🐘 There's no answer in this chat to mark yet."
    asked = next((hist[i]["content"] for i in range(last - 1, -1, -1) if hist[i]["role"] == "user"), "")
    correction = " ".join(re.sub(r"\S*(?:https?://|www\.)\S*", "", correction).split())[:500]
    entry = (f"\n## {time.strftime('%Y-%m-%d %H:%M')} · version {VERSION}\n"
             f"- **You asked:** {_quote(asked)}\n- **Sloth Lobster said:** {_quote(hist[last]['content'])}\n"
             + (f"- **What's right:** {_quote(correction)}\n" if correction else ""))
    MISSES.parent.mkdir(parents=True, exist_ok=True)
    new = not MISSES.exists()
    with open(MISSES, "a", encoding="utf-8") as f:
        if new:
            f.write("# Sloth Lobster misses\n\nAnswers you marked wrong with /wrong or the app's Wrong? link. "
                    "Each one becomes a test in the next update.\n")
        f.write(entry)
    shiba.audit("miss_noted", chars=len(asked))
    n = sum(1 for line in MISSES.read_text(encoding="utf-8").splitlines() if line.startswith("## "))
    return (f"🐘 Noted as wrong ({n} so far), in “{MISSES.name}” in your work folder. "
            + ("Thanks for the correction. " if correction else "Add what's right with /wrong <the right answer>. ")
            + "It'll become a test in the next update.")


def _quote(text: str) -> str:
    return " ".join(str(text).split())[:800] or "(nothing)"


def command(text: str) -> str | None:
    """A message starting with "/" is a command. Returns the reply, or None if it isn't one."""
    t = text.strip()
    if not t.startswith("/"):
        return None
    name, _, arg = t[1:].partition(" ")
    name = name.split("@")[0].lower()
    simple = {"research": research, "deep": deep, "ask": ask, "note": note, "save": save_link}
    if name in simple:
        return simple[name](arg)
    if name == "plan":
        return plan_command(arg)
    if name == "wrong":
        return wrong(arg)
    if name == "stop":
        return stop()
    if name == "new":
        return new_chat()
    if name == "pause":
        return shiba.pause()
    if name == "resume":
        return shiba.resume()
    if name == "status":
        return "🦞 Tap the dot at the top right for the full status." + (
            f" {len(jobs.pending())} research job(s) waiting." if jobs.pending() else "")
    return f"🦞 There's no /{name} command. {COMMAND_HELP}"


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


# --- Instant replies: common messages answered by code, in milliseconds -------------------
# (Home Assistant's local assistant works the same way: match plain phrases first, ask the
# model only for real conversation.) Everything here only reads, or goes through the same
# functions as the typed commands, so Shiba's checks are unchanged.

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


GREETINGS = {"hi", "hello", "hey", "hallo", "moin", "morning", "good morning", "good afternoon",
             "good evening", "evening", "hi there", "hello there", "hey there", "yo"}
THANKS = {"thanks", "thank you", "thx", "ty", "danke", "cheers", "thanks a lot", "thank you very much", "great thanks"}
HELP = {"help", "what can you do", "what can you do for me", "commands", "how does this work"}
RESEARCH_START = re.compile(r"^(?:please\s+)?(?:research|look up|look into|find out about|dig into)\s+(?!shows|suggests|says|has|is|was|were|are)(.{3,})$", re.I | re.S)
ASK_START = re.compile(r"^(?:what do (?:i|we) know about|what's in my vault about|what is in my vault about|"
                       r"search my vault for|do i have (?:any )?notes on)\s+(.{2,})$", re.I | re.S)


def quick(text: str) -> tuple[str, str] | None:
    """(kind, reply) for messages that need no model, else None."""
    n = _norm(text)
    if n in GREETINGS:
        return "reply", "🦞 Hello. What should we look into?"
    if n in THANKS:
        return "reply", "🦞 Anytime."
    if n in HELP:
        return "reply", ("🦞 I can research a question (/research), answer from your vault (/ask), "
                         "save a note exactly as you write it (/note) and keep a document (/save). "
                         "Or just talk to me. " + COMMAND_HELP)
    when = clock.match(text)                   # the time and date: worked out by code, never guessed
    if when is not None:
        return "reply", clock.answer(*when, lookup=spider.timezone_of)
    t = text.strip().rstrip("?").strip()
    m = RESEARCH_START.match(t)
    if m:
        return "research", research(m.group(1))
    m = ASK_START.match(t)
    if m:
        return "ask", ask(m.group(1))
    return None


async def _handle(text: str, busy: str | None) -> dict:
    prefix = f"{busy}\n\n" if busy else ""
    typed = command(text)
    if typed is not None:
        return {"reply": prefix + typed, "code": None, "action": "command"}
    fast = await asyncio.to_thread(quick, text)
    if fast is not None:
        kind, reply = fast
        s = load_session()
        hello = greeting(s) if kind == "reply" else ""
        # /ask results stay out of the model's memory, like before; a short marker keeps the thread.
        s["history"] += [{"role": "user", "content": text},
                         {"role": "assistant", "content": reply if kind != "ask" else "(answered from the vault)"}]
        s["history"] = s["history"][-HISTORY_TURNS:]
        save_session(s)
        return {"reply": prefix + hello + reply, "code": None, "action": kind}
    s = load_session()
    s["history"].append({"role": "user", "content": text})
    s["history"] = s["history"][-HISTORY_TURNS:]
    try:
        decision = await think(s["history"])
    except Exception as e:
        save_session(s)
        return {"reply": f"{prefix}🦞 My model is napping ({type(e).__name__}). "
                         "/note, /ask, /research, /save and /status still work.", "code": None}

    if clock.model_guessed_time(text, decision.get("reply", "")):
        # Backstop: a time question reached the model and it stated a time. It has no clock.
        decision.update(action="reply", text="", reply=NO_CLOCK)

    # Taint is sticky: web facts from earlier turns are still in the conversation.
    if decision["tainted"]:
        s["taint_turns"] = TAINT_MEMORY
    tainted = s["taint_turns"] > 0
    s["taint_turns"] = max(0, s["taint_turns"] - 1)

    action, arg = decision["action"], decision["text"]
    if action == "research":
        same = next((j for j in jobs.pending() if j["question"].lower() == " ".join(arg.split()).lower()), None)
        if same or not _research_fits(text, arg):
            hint = (f"🦞 Research #{same['id']} on that is already queued." if same
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
    hello = greeting(s) if action == "reply" else ""
    # Don't feed raw memory search results back into the model's context.
    s["history"].append({"role": "assistant", "content": decision["reply"] if action == "ask" else out})
    save_session(s)
    return {"reply": prefix + hello + out, "code": None, "action": action}


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
        reply += ("\nWe're offline, so the Spider reads your offline Wikipedia copy instead."
                  if spider.offline_available() else
                  "\nWe're offline, so it waits here and runs by itself once we're back online.")
    return reply


def deep(text: str) -> str:
    """/deep: a long research job. It drafts a plan for your OK, then runs overnight."""
    if not text.strip():
        return ("Usage: /deep <question>. I draft a plan of sub-questions for your OK; "
                f"then it runs overnight ({jobs.window_text()}) and the brief is here in the morning.")
    verdict, reason = shiba.decide("deep_research", text, tainted=False)
    if verdict == shiba.BLOCK:
        return _blocked("deep research", reason)
    return run_action("deep_research", text)


def _deep_job(job_id) -> tuple[dict | None, str]:
    try:
        j = jobs.job(int(job_id))
    except (TypeError, ValueError):
        j = None
    if j is None or j["mode"] != "deep":
        return None, "🐘 There's no deep research with that number."
    if j["done"] is not None:
        return None, f"🐘 Deep research #{j['id']} is already {j['done'].get('status', 'finished')}."
    return j, ""


def deep_choice(job_id, choice: str) -> str:
    """Your answer to a plan: "night" (OK, run tonight), "now" (OK, run now) or "cancel"."""
    j, problem = _deep_job(job_id)
    if j is None:
        return problem
    if choice == "cancel":
        jobs.cancel(j["id"])
        return f"🐘 Deep research #{j['id']} cancelled. Nothing was collected."
    if shiba.paused():
        return "🐕 Sloth Lobster is paused. Send /resume first."
    if j["plan"] is None:
        return f"🐘 The plan for #{j['id']} isn't ready yet; it comes in a minute."
    jobs.approve(j["id"], choice)
    if choice == "now":
        return f"🐘 OK: deep research #{j['id']} starts now. It takes up to an hour; I'll send the brief here."
    return (f"🐘 OK: deep research #{j['id']} runs tonight ({jobs.window_text()}), while the tablet charges. "
            "The brief will be here in the morning.")


def deep_edit(job_id, lines: list[str]) -> str:
    """Your own sub-questions for a deep job (one per line). Asks for your OK again."""
    j, problem = _deep_job(job_id)
    if j is None:
        return problem
    if shiba.paused():
        return "🐕 Sloth Lobster is paused. Send /resume first."
    import outline
    sections = outline.owner_plan(lines)
    if not sections:
        return "🐘 I couldn't read any sub-questions there. Send /plan <number>, then one question per line."
    jobs.set_plan(j["id"], sections, (j["plan"] or {}).get("needs_news", False), by="owner")
    return jobs.plan_text(j["id"], j["question"], sections)


def plan_command(arg: str) -> str:
    """/plan 7 (shows it), or /plan 7 followed by one sub-question per line (changes it)."""
    head, _, rest = arg.strip().partition("\n")
    number = head.split()[0] if head.split() else ""
    lines = [x for x in ([" ".join(head.split()[1:])] + rest.split("\n")) if x.strip()]
    if not number.isdigit():
        return "Usage: /plan <number>, then one sub-question per line."
    if lines:
        return deep_edit(number, lines)
    j, problem = _deep_job(number)
    if j is None:
        return problem
    if j["plan"] is None:
        return f"🐘 The plan for #{j['id']} isn't ready yet; it comes in a minute."
    return jobs.plan_text(j["id"], j["question"], j["plan"]["sections"])


def save_link(url: str) -> str:
    """/save: keep a document you chose, with a receipt."""
    if not url.strip():
        return "Usage: /save <link>"
    if shiba.paused():
        return "🐕 Sloth Lobster is paused. Send /resume first."
    return spider.save_document(url.strip())


def stop() -> str:
    n = jobs.stop_all()
    return (f"🦞 Stopped {n} research job{'s' if n != 1 else ''}. Everything already filed stays put."
            if n else "🦞 Nothing's running.")


def new_chat() -> str:
    reset_session()
    return "🦥 Fresh start. Chat and caution cleared; your notes and memory are untouched."
