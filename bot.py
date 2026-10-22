"""🦞 The Lobster's Telegram door, and the schedule that keeps everything moving.

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
import web
from store import timing_summary

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lobster")

digest_lock = asyncio.Lock()       # one Elephant job at a time
research_lock = asyncio.Lock()
state = {"now": "idle", "last_digest": None, "notified": set(), "last_backup_try": 0.0, "last_review_sync": 0.0}
REVIEW_SYNC_EVERY = 300            # seconds between reading your reviews back from the vault
BUSY = "🐘 The Elephant is digesting, so replies may be slow."

COMMANDS_MENU = [                  # what Telegram shows when you type /
    ("research", "Research a topic; the brief and cards land in your vault"),
    ("ask", "Answer from your vault only, with sources"),
    ("note", "Save a note exactly as typed"),
    ("save", "Keep a document (e.g. an annual report) with a receipt"),
    ("status", "What every part is doing right now"),
    ("stop", "Stop the running research and cancel waiting jobs"),
    ("new", "Start a fresh chat"),
    ("pause", "Freeze every write"),
    ("resume", "Unfreeze"),
]


def _title(url: str) -> str:
    return unquote(url.rstrip("/").rsplit("/", 1)[-1]).replace("_", " ")[:60] or "page"


def now_line() -> str:
    waiting = len(research.pending())
    extra = f" · {waiting} research job{'s' if waiting != 1 else ''} waiting" if waiting else ""
    return f"⏳ Now: {state['now']}{extra}"


def allowed(update: Update) -> bool:
    """Only you can talk to the Lobster. Everyone else is silently ignored."""
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


def approval_buttons(code: str, label: str = "Yes") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {label}", callback_data=f"ok:{code}"),
                                  InlineKeyboardButton("❌ No", callback_data=f"no:{code}")]])


@owner_only
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


@owner_only
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not shiba.message_allowed():
        await update.message.reply_text("🐕 Slow down a little: too many messages this minute.")
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
    if choice == "no":
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
    await alert(ctx.bot, f"something went wrong ({type(err).__name__}). Lobster is still running.")


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
    await update.message.reply_text(msg + ("\n" + now_line() if msg.startswith("🕷️") else ""))


@owner_only
async def cmd_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = _arg(update, ctx)
    if url and not shiba.paused():
        await update.message.reply_text("📥 Fetching...")
    await update.message.reply_text(await asyncio.to_thread(conversation.save_link, url))


def status_lines() -> list[str]:
    s = memory.stats()
    t = timing_summary()
    charging = is_charging()
    secs = t.get("research")
    speed = "" if secs is None else f" · research takes ~{secs:.0f} s" if secs < 90 else f" · research takes ~{secs / 60:.1f} min"
    return [
        f"🧠 Model: {'running' if ollama.alive() else 'DOWN'} ({config.MODEL})",
        f"🐘 Memory: {s['owner']} of your facts, {s['web']} web facts · {s['cards']} cards, {s['unreviewed']} to review",
        f"🕷️ {len(research.pending())} research job(s) waiting{speed}",
        f"🐕 Shiba: {'⏸️ PAUSED' if shiba.paused() else 'on guard'}, {shiba.blocks_last_day()} blocked in 24h",
        f"🔌 Charging: {'yes' if charging else 'no' if charging is not None else 'unknown (install termux-api)'}",
        now_line(),
        f"💾 Last backup: {memory.last_backup() or 'never'} · version {config.VERSION}",
    ]


@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\n".join(await asyncio.to_thread(status_lines)))


COMMANDS = {"research": cmd_research, "ask": cmd_ask, "note": cmd_note, "save": cmd_save,
            "status": cmd_status, "stop": cmd_stop, "new": cmd_new, "pause": cmd_pause, "resume": cmd_resume}


# --- The schedule ------------------------------------------------------------------------

def is_charging() -> bool | None:
    """Uses the Termux:API app. Returns None if it isn't installed."""
    if not shutil.which("termux-battery-status"):
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
        return f"❌ Backup failed: {e}"
    shiba.audit("backup", file=r["file"], facts=r["facts"])
    return f"💾 Backed up {r['facts']} facts to {r['file']}."


async def run_digest(bot=None, background: bool = False) -> str:
    """Digest leftover pages (e.g. /save documents). Stops between pages if research is waiting."""
    if shiba.paused() or digest_lock.locked():
        return ""
    stop = (lambda: bool(research.pending())) if background else None
    async with digest_lock:
        state["now"] = "Elephant indexing saved documents"
        try:
            r = await asyncio.to_thread(elephant.digest, None, None, stop, True)
        finally:
            state["now"] = "idle"
    state["last_digest"] = time.strftime("%Y-%m-%d %H:%M")
    if r["suspicious"] and bot is not None:
        await alert(bot, "rejected several instruction-like sentences from:\n" + "\n".join(r["suspicious"]))
    return f"🐘 Indexed {r['pages']} document(s): {r['facts']} sentences for /ask." if r["pages"] else ""


async def run_research_jobs(app) -> None:
    """Work through queued research jobs, one at a time."""
    if shiba.paused() or research_lock.locked():
        return
    async with research_lock:
        while not shiba.paused():
            waiting = research.pending()
            if not waiting:
                break
            job = waiting[0]
            research.STOP.clear()
            send = app.bot.send_message
            loop = asyncio.get_running_loop()

            def say(text, job_id=job["id"]):
                asyncio.run_coroutine_threadsafe(send(config.ALLOWED_USER_ID, f"#{job_id} {text}"), loop)

            def page_done(i, n, url, job_id=job["id"]):
                state["now"] = f"research #{job_id}: read {i} of {n} articles"
                asyncio.run_coroutine_threadsafe(
                    send(config.ALLOWED_USER_ID, f"🐘 #{job_id}: {i}/{n} read ({_title(url)})"), loop)

            await send(config.ALLOWED_USER_ID, f"🕷️ Researching #{job['id']}: {job['question']}")
            async with digest_lock:                  # waits for a running document digest
                state["now"] = f"research #{job['id']}: collecting"
                try:
                    r = await asyncio.to_thread(research.run_job, job, say, page_done)
                finally:
                    state["now"] = "idle"
            if r["status"] == "offline":
                await notify_once(app.bot, ("net", job["id"]),
                                  f"📴 Offline. Research #{job['id']} stays queued until I'm back online.")
                return
            if r["status"] == "paused":
                return
            if r["status"] == "stopped":
                await send(config.ALLOWED_USER_ID, f"⏹️ Research #{job['id']} stopped; what was read is kept.")
                continue
            if r["status"] == "nothing":
                await send(config.ALLOWED_USER_ID, f"🔎 Research #{job['id']}: nothing found. "
                           + "; ".join(r.get("failed", [])[:3]))
                continue
            if r.get("suspicious"):
                await alert(app.bot, "rejected several instruction-like sentences from:\n" + "\n".join(r["suspicious"]))
            conversation.mark_tainted()              # the brief is web content: be cautious for a few turns
            await send(config.ALLOWED_USER_ID, r["brief"])


async def background(app: Application) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await run_research_jobs(app)
            if time.time() - state["last_review_sync"] > REVIEW_SYNC_EVERY:
                state["last_review_sync"] = time.time()
                await asyncio.to_thread(elephant.sync_reviews)
            charging = await asyncio.to_thread(is_charging)
            if charging and time.time() - state["last_backup_try"] > 20 * 3600:
                state["last_backup_try"] = time.time()
                last = memory.last_backup()
                if last is None or last[:10] != time.strftime("%Y-%m-%d"):
                    log.info(await run_backup())
                    await asyncio.to_thread(elephant.retry_failed)
            if config.DIGEST_ON_CHARGE and charging and spider.inbox_count() and not research.pending():
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
    shiba.audit("started", model=config.MODEL)
    log.info("Lobster %s starting (model=%s, vault=%s)", config.VERSION, config.MODEL, config.VAULT_DIR)
    app.run_polling()


if __name__ == "__main__":
    main()
