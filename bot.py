"""🦞 Sloth Lobster's Telegram door, and the schedule that keeps everything moving.

Run with:  python bot.py

This file only speaks Telegram. Every decision lives in conversation.py, which the
web app (web.py) shares, so both doors get exactly the same Shiba checks.

The schedule (one background loop):
  - research jobs, one at a time: the Spider collects, the Elephant files
  - your reviews in the vault are read back every few minutes
  - a nightly memory backup and leftover documents while the tablet is charging
"""
import asyncio
import functools
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import config
import conversation
import elephant
import memory
import ollama
import research
import shiba
import spider
import usage
import zim
import web
from store import timing_summary

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lobster")

digest_lock = asyncio.Lock()       # one Elephant job at a time
research_lock = asyncio.Lock()
state = {"now": "idle", "last_digest": None, "notified": set(), "last_backup_try": 0.0, "last_review_sync": 0.0}
REVIEW_SYNC_EVERY = 300            # seconds between reading your reviews back from the vault
BUSY = "🐘 The Elephant is digesting, so replies may be a little slow."

COMMANDS_MENU = [                  # what Telegram shows when you type /
    ("research", "Research a topic; the brief and cards land in your vault"),
    ("deep", "Deep research: you OK a plan, it runs overnight"),
    ("plan", "See or change a deep research plan: /plan 7"),
    ("ask", "Answer from your vault only, with sources"),
    ("note", "Save a note exactly as typed"),
    ("save", "Keep a document (e.g. an annual report) with a receipt"),
    ("status", "What every part is doing right now"),
    ("stop", "Stop the running research and cancel waiting jobs"),
    ("new", "Start a fresh chat"),
    ("pause", "Freeze every write"),
    ("resume", "Unfreeze"),
]


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram takes at most 4096 characters per message: split long briefs between paragraphs."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:                 # one huge paragraph: split between lines
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:cut])
            para = para[cut:].lstrip("\n")
        if cur and len(cur) + 2 + len(para) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts or [""]


def _title(url: str) -> str:
    return unquote(url.rstrip("/").rsplit("/", 1)[-1]).replace("_", " ")[:60] or "page"


def now_line() -> str:
    waiting = research.waiting_now()
    extra = f" · {waiting} research job{'s' if waiting != 1 else ''} waiting" if waiting else ""
    return f"⏳ Now: {state['now']}{extra}"


def allowed(update: Update) -> bool:
    """Only you can talk to Sloth Lobster. Everyone else is silently ignored."""
    user = update.effective_user
    if user and user.id == config.ALLOWED_USER_ID:
        return True
    shiba.audit("stranger_ignored", user=str(user.id if user else "?"))
    log.warning("Ignored message from user %s", user.id if user else "?")
    return False


def owner_only(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if allowed(update):
            return await handler(update, ctx)
    return wrapped


async def notify_once(bot, key, text: str) -> None:
    """Say something once per situation, not every 30 seconds."""
    if key not in state["notified"]:
        state["notified"].add(key)
        await bot.send_message(config.ALLOWED_USER_ID, text)


async def alert(bot, text: str) -> None:
    """Shiba's alerts, at most one every 10 minutes."""
    if shiba.should_alert():
        try:
            await bot.send_message(config.ALLOWED_USER_ID, f"🐕 Shiba: {text}")
        except Exception:
            log.exception("Alert failed")


# --- Talking ---------------------------------------------------------------------

HELP = "🦞 Just talk to me, or use:\n" + "\n".join(f"/{c}: {d}" for c, d in COMMANDS_MENU)


def _arg(update: Update, ctx) -> str:
    """Everything after the command, exactly as typed (line breaks and spacing kept)."""
    text = update.message.text or ""
    return text.partition(" ")[2].strip() if " " in text else " ".join(ctx.args or [])


def plan_buttons(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🌙 OK, tonight", callback_data=f"deep:{job_id}:night"),
                                  InlineKeyboardButton("▶️ Run now", callback_data=f"deep:{job_id}:now")],
                                 [InlineKeyboardButton("❌ Cancel", callback_data=f"deep:{job_id}:cancel")]])


def approval_buttons(code: str, label: str = "Yes") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {label}", callback_data=f"ok:{code}"),
                                  InlineKeyboardButton("❌ No", callback_data=f"no:{code}")]])


@owner_only
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


@owner_only
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not shiba.message_allowed():
        await update.message.reply_text("🐕 Easy there: a few too many messages this minute. Give it a moment.")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    r = await conversation.handle(update.message.text, busy=BUSY if digest_lock.locked() else None)
    reply = r["reply"] + ("\n" + now_line() if r.get("action") == "research" else "")
    buttons = approval_buttons(r["code"], r.get("ask", {}).get("label", "Yes")) if r["code"] else None
    await update.message.reply_text(reply, reply_markup=buttons)


async def _edit(query, text: str) -> None:
    """Edit an approval card; tapping a card twice is harmless, not an error."""
    try:
        await query.edit_message_text(text)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    choice, _, code = (query.data or "").partition(":")
    if choice == "deep":
        job_id, _, what = code.partition(":")
        if what in ("night", "now", "cancel"):
            await _edit(query, f"{query.message.text}\n\n" + await asyncio.to_thread(conversation.deep_choice, job_id, what))
        else:
            await _edit(query, "Unknown button.")
    elif choice == "no":
        await _edit(query, conversation.cancel(code))
    elif choice == "ok":
        await _edit(query, await conversation.approve(code))
    else:
        await _edit(query, "Unknown button.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """One readable line instead of a traceback. Network blips are routine on a tablet."""
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning("Telegram network hiccup: %s", err)
        return
    log.error("Handler error: %s: %s", type(err).__name__, err)
    shiba.audit("handler_error", error=f"{type(err).__name__}: {err}")
    await alert(ctx.bot, f"something went wrong ({type(err).__name__}). Sloth Lobster is still running.")


# --- Commands (no model involved) ----------------------------------------------------

def _command(fn):
    """A command whose reply is fn(argument), run off the event loop."""
    @owner_only
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await asyncio.to_thread(fn, _arg(update, ctx)))
    return handler


cmd_note = _command(conversation.note)
cmd_ask = _command(conversation.ask)
cmd_stop = _command(lambda _: conversation.stop())
cmd_new = _command(lambda _: conversation.new_chat())
cmd_pause = _command(lambda _: shiba.pause())
cmd_resume = _command(lambda _: shiba.resume())


@owner_only
async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = conversation.research(_arg(update, ctx))
    await update.message.reply_text(msg + ("\n" + now_line() if msg.startswith("🦞 On it") else ""))


@owner_only
async def cmd_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await asyncio.to_thread(conversation.deep, _arg(update, ctx)))


@owner_only
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan 7 shows the plan with its buttons; /plan 7 + lines changes it first."""
    arg = _arg(update, ctx)
    msg = await asyncio.to_thread(conversation.plan_command, arg)
    number = arg.split()[0] if arg.split() else ""
    show = msg.startswith("🐘 Plan for deep research") and number.isdigit()
    await update.message.reply_text(msg, reply_markup=plan_buttons(int(number)) if show else None)


@owner_only
async def cmd_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = _arg(update, ctx)
    if url and not shiba.paused():
        await update.message.reply_text("🕷️ Fetching…")
    await update.message.reply_text(await asyncio.to_thread(conversation.save_link, url))


def status_lines() -> list[str]:
    s = memory.stats()
    t = timing_summary()
    charging = is_charging()
    secs = t.get("research")
    speed = "" if secs is None else f" · research takes ~{secs:.0f} s" if secs < 90 else f" · research takes ~{secs / 60:.1f} min"
    return [
        f"🧠 Model: {'running' if ollama.alive() else 'DOWN'} ({config.MODEL})" + speed_note(),
        f"🐘 Memory: {s['owner']} of your facts, {s['web']} web facts · {s['cards']} cards, {s['unreviewed']} to review",
        f"🕷️ {research.waiting_now()} research job(s) waiting{speed}" + (
            f" · {n} deep job(s) for tonight" if (n := sum(j['mode'] == 'deep' for j in research.pending())) else ""),
        f"🐕 Shiba: {'⏸️ PAUSED' if shiba.paused() else 'on guard'}, {shiba.blocks_last_day()} blocked in 24h",
        f"🔌 Charging: {'yes' if charging else 'no' if charging is not None else 'unknown (backups still run daily)'}",
        now_line(),
        f"🌐 Internet: {'online' if spider.online() else 'OFFLINE (research waits in the queue)'}",
        *([f"📚 Offline Wikipedia: {', '.join(z.copy for z in zim.library(config.KIWIX_DIR))}"]
          if spider.offline_available() else []),
        usage.line(),
        *door_lines(),
        f"💾 Last backup: {memory.last_backup() or 'never'} · version {config.VERSION}",
    ]


def speed_note() -> str:
    sp = ollama.speed("chat")
    if not sp:
        return ""
    return f" · replies ≈ {sp['seconds']} s (reading {sp['read_s']} s, writing {sp['write_s']} s)"


def door_lines() -> list[str]:
    doors = state.get("web")
    lines = []
    if doors:
        lines += [f"🚪 App ({d['kind']}): {d['url']}" for d in doors.urls()]
        if doors.wifi_note:
            lines.append(f"🚪 Wi-Fi door: {doors.wifi_note}")
    problem = ollama.version_problem()
    if problem:
        lines.append(f"⚠️ {problem}")
    return lines


@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\n".join(await asyncio.to_thread(status_lines)))


@owner_only
async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(conversation.command(update.message.text or "/") or conversation.COMMAND_HELP)


COMMANDS = {"research": cmd_research, "deep": cmd_deep, "plan": cmd_plan, "ask": cmd_ask, "note": cmd_note, "save": cmd_save,
            "status": cmd_status, "stop": cmd_stop, "new": cmd_new, "pause": cmd_pause, "resume": cmd_resume}


# --- The schedule ------------------------------------------------------------------------

def is_charging() -> bool | None:
    """Termux:API if it's there; otherwise Android's battery files. None if neither can be read."""
    if not shutil.which("termux-battery-status"):
        for status in sorted(Path("/sys/class/power_supply").glob("*/status")):
            try:
                value = status.read_text().strip().lower()
            except OSError:
                continue
            if value in ("charging", "full"):
                return True
            if value in ("discharging", "not charging"):
                return False
        return None
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=15).stdout
        return json.loads(out).get("plugged", "UNPLUGGED") != "UNPLUGGED"
    except Exception:
        return None


async def run_backup() -> str:
    try:
        r = await asyncio.to_thread(memory.backup)
    except Exception as e:
        shiba.audit("backup_failed", error=f"{type(e).__name__}: {e}")
        return f"🐘 Backup failed: {e}"
    shiba.audit("backup", file=r["file"], facts=r["facts"])
    return f"🐘 Backed up {r['facts']} facts to {r['file']}."


async def run_digest(bot=None, background: bool = False) -> str:
    """Digest leftover pages (e.g. /save documents). Stops between pages if research is waiting."""
    if shiba.paused() or digest_lock.locked():
        return ""
    stop = (lambda: bool(research.waiting_now())) if background else None
    async with digest_lock:
        state["now"] = "🐘 Indexing saved documents"
        try:
            r = await asyncio.to_thread(elephant.digest, None, None, stop, True)
        finally:
            state["now"] = "idle"
    state["last_digest"] = time.strftime("%Y-%m-%d %H:%M")
    if r["suspicious"] and bot is not None:
        await alert(bot, "rejected several instruction-like sentences from:\n" + "\n".join(r["suspicious"]))
    return f"🐘 Indexed {r['pages']} document(s): {r['facts']} sentences for /ask." if r["pages"] else ""


async def run_research_jobs(app) -> None:
    """Work through research jobs, one at a time: quick jobs whenever they come, a plan for each
    new deep job straight away, and approved deep jobs at night (they step aside for quick ones)."""
    if shiba.paused() or research_lock.locked():
        return
    async with research_lock:
        while not shiba.paused():
            charging = await asyncio.to_thread(is_charging)
            job, what = await asyncio.to_thread(research.next_job, None, charging)
            if job is None:
                if what:
                    state["now"] = what
                    if what.startswith("🔌"):
                        await notify_once(app.bot, ("charger", time.strftime("%Y-%m-%d")),
                                          f"{what}: plug the tablet in and it starts by itself.")
                elif state["now"].startswith(("🌙", "🔌", "🐘 Deep research")):
                    state["now"] = "idle"
                break
            send = app.bot.send_message
            if what == "plan":
                state["now"] = f"🐘 Deep research #{job['id']}: drafting a plan"
                try:
                    p = await asyncio.to_thread(research.make_plan, job)
                finally:
                    state["now"] = "idle"
                await send(config.ALLOWED_USER_ID, research.plan_text(job["id"], job["question"], p["sections"]),
                           reply_markup=plan_buttons(job["id"]))
                continue
            if not await asyncio.to_thread(spider.online) and not await asyncio.to_thread(spider.offline_available):
                state["now"] = f"🦞 Offline: research #{job['id']} waits for the internet"
                return                               # tried again every 15 s; runs by itself when back
            research.STOP.clear()
            loop = asyncio.get_running_loop()
            deep = job["mode"] == "deep"

            def say(text, job_id=job["id"]):
                who, _, rest = text.partition(" ")      # "🕷️ Out collecting: …" -> the app's live step
                if who and rest and not who.isalnum():
                    state["now"] = f"{who} Research #{job_id}: {rest[:1].lower()}{rest[1:]}"
                if not deep or rest.startswith(("Deep research", "Round 1", "Writing")):
                    asyncio.run_coroutine_threadsafe(send(config.ALLOWED_USER_ID, f"#{job_id} {text}"), loop)

            def page_done(i, n, url, job_id=job["id"]):
                state["now"] = f"🐘 Research #{job_id}: reading {i} of {n} articles"
                if not deep:
                    asyncio.run_coroutine_threadsafe(
                        send(config.ALLOWED_USER_ID, f"🐘 #{job_id}: {i}/{n} read ({_title(url)})"), loop)

            def quick_waiting():
                return any(j["mode"] != "deep" for j in research.pending())

            if not deep:
                await send(config.ALLOWED_USER_ID, f"🕷️ Researching #{job['id']}: {job['question']}")
            async with digest_lock:                  # waits for a running document digest
                state["now"] = f"🕷️ Research #{job['id']}: collecting"
                try:
                    r = await asyncio.to_thread(research.run_job, job, say, page_done, quick_waiting)
                finally:
                    state["now"] = "idle"
            if r["status"] == "yield":
                continue                             # a quick job goes first; the deep one keeps its place
            if r["status"] == "offline":
                await notify_once(app.bot, ("net", job["id"]),
                                  f"🦞 We're offline, so research #{job['id']} waits here until we're back.")
                return
            if r["status"] == "paused":
                return
            if r["status"] == "stopped":
                await send(config.ALLOWED_USER_ID, f"🦞 Research #{job['id']} stopped; what was read is kept.")
                continue
            if r["status"] == "nothing":
                await send(config.ALLOWED_USER_ID, f"🕷️ Research #{job['id']}: came back empty-handed. "
                           + "; ".join(r.get("failed", [])[:3]))
                continue
            if r.get("suspicious"):
                await alert(app.bot, "rejected several instruction-like sentences from:\n" + "\n".join(r["suspicious"]))
            conversation.mark_tainted()              # the brief is web content: be cautious for a few turns
            for part in split_message(r["brief"]):
                await send(config.ALLOWED_USER_ID, part)


WEEKLY_FILE = config.DATA_DIR / "usage_week.txt"


async def weekly_usage(app) -> None:
    """Sunday evening, once a week: one line of what you used. Two weeks of these decide
    what's worth polishing and what can go."""
    t = time.localtime()
    week = time.strftime("%G-W%V", t)
    if t.tm_wday != 6 or t.tm_hour < 18:
        return
    try:
        if WEEKLY_FILE.exists() and WEEKLY_FILE.read_text().strip() == week:
            return
        WEEKLY_FILE.write_text(week)
        await app.bot.send_message(config.ALLOWED_USER_ID, await asyncio.to_thread(usage.line))
    except Exception:
        log.exception("Weekly usage line failed")


async def background(app: Application) -> None:
    await asyncio.sleep(20)
    try:
        await asyncio.to_thread(spider.warm_offline)      # once: offline Wikipedia's title index
    except Exception:
        log.exception("Couldn't prepare offline Wikipedia")
    while True:
        try:
            await run_research_jobs(app)
            if time.time() - state["last_review_sync"] > REVIEW_SYNC_EVERY:
                state["last_review_sync"] = time.time()
                await asyncio.to_thread(elephant.sync_reviews)
            await weekly_usage(app)
            charging = await asyncio.to_thread(is_charging)
            # Unknown (no Termux:API): still back up once a day rather than never.
            if (charging or charging is None) and time.time() - state["last_backup_try"] > 20 * 3600:
                state["last_backup_try"] = time.time()
                last = memory.last_backup()
                if last is None or last[:10] != time.strftime("%Y-%m-%d"):
                    log.info(await run_backup())
                    await asyncio.to_thread(elephant.retry_failed)
            if config.DIGEST_ON_CHARGE and (charging or charging is None) and spider.inbox_count() \
                    and not research.waiting_now():
                msg = await run_digest(app.bot, background=True)
                if msg:
                    await app.bot.send_message(config.ALLOWED_USER_ID, msg)
        except Exception:
            log.exception("Background loop error")
        await asyncio.sleep(15)


async def post_init(app: Application) -> None:
    loop = asyncio.get_running_loop()
    state["background"] = loop.create_task(background(app))
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS_MENU])
    except Exception as e:                      # the menu is a nicety; never block startup on it
        log.warning("Couldn't set the command menu: %s", e)

    def notify(text: str) -> None:             # called from the web app's threads
        asyncio.run_coroutine_threadsafe(app.bot.send_message(config.ALLOWED_USER_ID, text), loop)

    try:
        state["web"] = web.start(loop, busy=digest_lock.locked, notify=notify, now=lambda: state["now"])
    except OSError as e:                       # e.g. Tailscale isn't up yet, so its address doesn't exist
        log.warning("Web app couldn't start on %s:%s (%s). Telegram still works.",
                    config.WEB_BIND, config.WEB_PORT, e)


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.ALLOWED_USER_ID:
        raise SystemExit("Fill in TELEGRAM_TOKEN and ALLOWED_USER_ID in .env first.")
    problem = shiba.seal_problem()
    if problem:
        raise SystemExit(f"🐕 Shiba won't start: {problem}.\n"
                         "Read what changed in identity.txt, actions.py, shiba.py and web.py, then run: python seal.py")
    builder = Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init)
    if config.PROXY:
        builder = builder.proxy(config.PROXY).get_updates_proxy(config.PROXY)
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], start))
    for name, fn in COMMANDS.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))     # e.g. /jobs: say what exists
    shiba.audit("started", model=config.MODEL)
    log.info("%s %s starting (model=%s, vault=%s)", config.APP_NAME, config.VERSION, config.MODEL, config.VAULT_DIR)
    problem = ollama.version_problem()
    if problem:
        log.warning(problem)
    app.run_polling()


if __name__ == "__main__":
    main()
