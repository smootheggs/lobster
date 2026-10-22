"""The Lobster's Telegram intercom, plus the Spider/Elephant schedule.

This file only speaks Telegram. What Lobster decides lives in conversation.py,
so a second way in (the local web app) gets exactly the same safety checks.

Run with:  python bot.py

One process does everything:
  - answers you on Telegram (only you)
  - asks Shiba before every action, with one-time approval cards
  - runs research jobs, the Spider every few hours, and the Elephant
    digest when the tablet is charging ("feast"), then unloads the model ("rest")
"""
import asyncio
import functools
import json
import logging
import shutil
import subprocess
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import conversation
import elephant
import memory
import ollama
import research
import shiba
import spider
from actions import run_action

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lobster")

digest_lock = asyncio.Lock()
research_lock = asyncio.Lock()
state = {"last_crawl": 0.0, "last_digest": None, "notified": set(), "now": "idle"}


def _title(url: str) -> str:
    from urllib.parse import unquote
    return unquote(url.rstrip("/").rsplit("/", 1)[-1]).replace("_", " ")[:60] or "page"


def now_line() -> str:
    pending = research.pending()
    waiting = f" · {len(pending)} research job(s) waiting" if pending else ""
    return f"⏳ Now: {state['now']}{waiting} · {spider.inbox_count()} pages in the inbox"


def allowed(update: Update) -> bool:
    """Only you can talk to the Lobster. Everyone else is silently ignored."""
    user = update.effective_user
    if user and user.id == config.ALLOWED_USER_ID:
        return True
    shiba.audit("stranger_ignored", user=str(user.id if user else "?"))
    log.warning("Ignored message from user %s", user.id if user else "?")
    return False


def owner_only(handler):
    """Every command and message handler runs only for you."""
    @functools.wraps(handler)
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if allowed(update):
            return await handler(update, ctx)
    return wrapped


def paused_reply() -> str | None:
    return "⏸️ Lobster is paused. /resume first." if shiba.paused() else None


async def notify_once(bot, key, text: str) -> None:
    """Say something once per situation (e.g. 'model offline' for one job), not every 30 seconds."""
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


# --- Conversation -------------------------------------------------------------

HELP = (
    "🦞 Just talk to me, or use:\n"
    "/status: memory, model, Shiba\n"
    "/note <text>: save a note exactly as typed (works with the model offline)\n"
    "/research <question>: research a topic and send a brief\n"
    "/save <url>: save a document (e.g. an annual report) to the Library\n"
    "/sources, /addsource <url>, /removesource <url>\n"
    "/crawl: run the Spider now · /digest: run the Elephant now · /retryfailed: retry failed pages\n"
    "/pause, /resume: freeze or unfreeze every write\n"
    "/jobs: what Lobster is doing right now · /newchat: clear chat history\n"
    "/backup: back up memory now (also nightly while charging)\n"
    "/audit: Shiba's last decisions"
)


@owner_only
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


def approval_buttons(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"ok:{code}"),
        InlineKeyboardButton("❌ No", callback_data=f"no:{code}"),
    ]])


BUSY = "🐘 The Elephant is digesting, so replies may be slow."


@owner_only
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram adapter: the decisions themselves live in conversation.py."""
    if not shiba.message_allowed():
        await update.message.reply_text("🐕 Slow down a little: too many messages this minute.")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    r = await conversation.handle(update.message.text, busy=BUSY if digest_lock.locked() else None)
    reply = r["reply"] + ("\n" + now_line() if r.get("action") == "research" else "")
    await update.message.reply_text(reply, reply_markup=approval_buttons(r["code"]) if r["code"] else None)


async def _edit(query, text: str) -> None:
    """Edit an approval card; tapping a card twice is harmless, not an error."""
    try:
        await query.edit_message_text(text)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """One readable line instead of a traceback. Network blips are routine on a tablet."""
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning("Telegram network hiccup: %s", err)
        return
    log.error("Handler error: %s: %s", type(err).__name__, err)
    shiba.audit("handler_error", error=f"{type(err).__name__}: {err}")
    await alert(ctx.bot, f"something went wrong ({type(err).__name__}). Lobster is still running; details in /audit.")


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


# --- Commands you type yourself (no model involved) ---------------------------

@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await asyncio.to_thread(memory.stats)
    charging = await asyncio.to_thread(is_charging)
    model_up = await asyncio.to_thread(ollama.alive)
    await update.message.reply_text("\n".join([
        f"🧠 Model server: {'up' if model_up else 'DOWN'} ({config.MODEL})",
        f"🐘 Memory: {s['owner']} owner facts, {s['web']} web facts",
        f"🕷️ {len(spider.load_sources())} sources · {len(research.pending())} research jobs queued",
        f"🐕 Shiba: {'⏸️ PAUSED' if shiba.paused() else 'clear'}, {shiba.blocks_last_day()} blocks or rejections in 24h",
        f"🔌 Charging: {'yes' if charging else 'no' if charging is not None else 'unknown (install termux-api)'}",
        now_line(),
        f"💾 Last backup: {memory.last_backup() or 'never'}",
        f"Last digest: {state['last_digest'] or 'never'} · version {config.VERSION}",
    ]))


@owner_only
async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    src = spider.load_sources()
    await update.message.reply_text("\n".join(src) if src else "No sources yet. Use /addsource <url>.")


@owner_only
async def cmd_addsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = paused_reply() or (await asyncio.to_thread(spider.add_source, " ".join(ctx.args))
                             if ctx.args else "Usage: /addsource <url>")
    await update.message.reply_text(msg)


@owner_only
async def cmd_removesource(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = paused_reply() or (spider.remove_source(" ".join(ctx.args)) if ctx.args else "Usage: /removesource <url>")
    await update.message.reply_text(msg)


@owner_only
async def cmd_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if (msg := paused_reply()) or not ctx.args:
        await update.message.reply_text(msg or "Usage: /save <url>")
        return
    await update.message.reply_text("📥 Fetching...")
    await update.message.reply_text(await asyncio.to_thread(spider.save_document, ctx.args[0]))


@owner_only
async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(ctx.args)
    if not question:
        await update.message.reply_text("Usage: /research <question>")
        return
    verdict, reason = shiba.decide("research", question, tainted=False)
    if verdict == shiba.BLOCK:
        await update.message.reply_text(conversation._blocked("research", reason))
        return
    await update.message.reply_text(run_action("research", question) + "\n" + now_line())


@owner_only
async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a note exactly as typed: no model, so it works even when the model is down."""
    text = update.message.text.partition(" ")[2] if update.message.text else " ".join(ctx.args)
    await update.message.reply_text(await asyncio.to_thread(conversation.note, text))


@owner_only
async def cmd_newchat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conversation.reset_session()
    await update.message.reply_text("🧽 Fresh start: chat history cleared. (Notes, tasks and memory are untouched.)")


@owner_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(shiba.pause())


@owner_only
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(shiba.resume())


@owner_only
async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [now_line()] + [f"🔎 #{j['id']} waiting: {j['question']} (queued {j['ts'][11:]})"
                            for j in research.pending()]
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = shiba.recent(8)
    lines = [f"{r['ts'][5:16]} {r['kind']}"
             + (f" {r.get('action', '')} {r.get('verdict', '')}" if r["kind"] == "decision" else "")
             + (f" ({r['reason']})" if r.get("reason") not in (None, "ok") else "")
             for r in rows]
    await update.message.reply_text("🐕 Last decisions:\n" + "\n".join(lines) if lines else "🐕 Nothing logged yet.")


async def run_backup() -> str:
    try:
        r = await asyncio.to_thread(memory.backup)
    except Exception as e:
        shiba.audit("backup_failed", error=f"{type(e).__name__}: {e}")
        return f"❌ Backup failed: {e}"
    shiba.audit("backup", file=r["file"], facts=r["facts"])
    return f"💾 Backed up {r['facts']} facts to {r['file']} (keeping the newest {r['kept']})."


@owner_only
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await run_backup())


@owner_only
async def cmd_retryfailed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    n = await asyncio.to_thread(elephant.retry_failed)
    await update.message.reply_text(f"🐘 {n} failed pages moved back to the inbox. /digest to process them now.")


@owner_only
async def cmd_crawl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🕷️ Crawling...")
    await update.message.reply_text(await run_crawl())


@owner_only
async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🐘 Digesting... (this can take a while)")
    await update.message.reply_text(await run_digest(ctx.bot))


COMMANDS = {"status": cmd_status, "sources": cmd_sources, "addsource": cmd_addsource,
            "removesource": cmd_removesource, "save": cmd_save, "research": cmd_research,
            "note": cmd_note, "newchat": cmd_newchat, "pause": cmd_pause, "resume": cmd_resume,
            "jobs": cmd_jobs, "audit": cmd_audit,
            "retryfailed": cmd_retryfailed, "crawl": cmd_crawl, "digest": cmd_digest, "backup": cmd_backup}


# --- Spider / Elephant / research schedule --------------------------------------

def is_charging() -> bool | None:
    """Uses the Termux:API app. Returns None if it isn't installed."""
    if not shutil.which("termux-battery-status"):
        return None
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=15).stdout
        return json.loads(out).get("plugged", "UNPLUGGED") != "UNPLUGGED"
    except Exception:
        return None


async def run_crawl() -> str:
    r = await asyncio.to_thread(spider.crawl)
    state["last_crawl"] = time.time()
    msg = f"🕷️ {r['new']} new pages, {r['unchanged']} unchanged"
    return msg + (f", {len(r['failed'])} failed:\n" + "\n".join(r["failed"]) if r["failed"] else "")


async def run_digest(bot=None, job: int | None = None, progress=None, background: bool = False) -> str:
    """Digest the inbox. A research job (job=id) waits its turn instead of being skipped.
    A background digest stops between pages as soon as a research job is waiting."""
    if shiba.paused():
        return "⏸️ Paused: the Elephant won't store anything until /resume."
    if job is None and digest_lock.locked():
        return "Already digesting. " + now_line()
    label = f"research #{job}" if job is not None else ("background digest" if background else "digest")

    def track(i, n, url):
        state["now"] = f"Elephant on {label}, page {i}/{n} done"
        if progress is not None:
            progress(i, n, url)
    stop = (lambda: bool(research.pending())) if background else None
    async with digest_lock:
        state["now"] = f"Elephant starting {label}"
        try:
            r = await asyncio.to_thread(elephant.digest, job, track, stop, job is None)
        finally:
            state["now"] = "idle"
    if r["model_down"]:
        return "🧠 The model is offline, so pages stay in the inbox until it's back. Check /status."
    state["last_digest"] = time.strftime("%Y-%m-%d %H:%M")
    if r["suspicious"] and bot is not None:
        await alert(bot, "rejected several instruction-like facts from:\n" + "\n".join(r["suspicious"])
                    + "\nDetails: /audit")
    msg = (f"🐘 Digested {r['pages']} page{'' if r['pages'] == 1 else 's'}: {r['facts']} new facts stored, "
           f"{r['rejected']} rejected by the filter, {r['failed']} pages failed.")
    if r["stopped"]:
        msg += f" Paused with {r['remaining']} pages left, to run your research first."
    return msg


async def run_research_jobs(app) -> None:
    """Work through queued research jobs, one at a time."""
    if shiba.paused() or research_lock.locked():
        return
    async with research_lock:
        for job in research.pending():
            send = app.bot.send_message
            if not await asyncio.to_thread(ollama.alive):
                await notify_once(app.bot, ("model", job["id"]),
                                  f"🧠 Model offline. Research #{job['id']} waits until it's back.")
                return
            await send(config.ALLOWED_USER_ID, f"🕷️ Researching #{job['id']}: {job['question']}")
            state["now"] = f"Spider fetching for research #{job['id']}"
            gathered = await asyncio.to_thread(research.gather, job)
            if gathered["offline"]:
                await notify_once(app.bot, ("net", job["id"]),
                                  f"📴 Offline. Research #{job['id']} stays queued until I'm back online.")
                return
            if not gathered["urls"]:
                research.mark_done(job["id"], "nothing found")
                await send(config.ALLOWED_USER_ID, f"🔎 Research #{job['id']}: nothing found. "
                           + "; ".join(gathered["failed"][:3]))
                continue
            plan = ", ".join(gathered["plan"]["searches"])
            loop = asyncio.get_running_loop()

            def report_page(i, n, url, job_id=job["id"]):
                asyncio.run_coroutine_threadsafe(
                    send(config.ALLOWED_USER_ID, f"🐘 #{job_id}: {i}/{n} digested ({_title(url)})"), loop)
            skipped = f"\n⏭️ Skipped as off-topic: {', '.join(gathered['skipped'])}" if gathered["skipped"] else ""
            await send(config.ALLOWED_USER_ID, f"🔎 Searched: {plan}\n📚 {len(gathered['urls'])} articles kept.{skipped}\n"
                       "Digesting, which takes a few minutes on the tablet...")
            await send(config.ALLOWED_USER_ID, await run_digest(app.bot, job=job["id"], progress=report_page))
            state["now"] = f"writing the brief for research #{job['id']}"
            text = await asyncio.to_thread(research.brief, job, gathered["urls"])
            research.mark_done(job["id"], "done", len(gathered["urls"]))
            state["now"] = "idle"
            conversation.mark_tainted()   # the brief is web content: be cautious for the next turns
            await send(config.ALLOWED_USER_ID, text)


async def background(app: Application) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_research_jobs(app)
            if (not shiba.paused() and spider.load_sources()
                    and time.time() - state["last_crawl"] > config.SPIDER_EVERY_HOURS * 3600):
                log.info(await run_crawl())
            charging = await asyncio.to_thread(is_charging)
            if charging and time.time() - state.get("last_backup_try", 0) > 20 * 3600:
                state["last_backup_try"] = time.time()
                last = memory.last_backup()
                if last is None or last[:10] != time.strftime("%Y-%m-%d"):
                    log.info(await run_backup())
            if (config.DIGEST_ON_CHARGE and spider.inbox_count() and not digest_lock.locked()
                    and charging):
                msg = await run_digest(app.bot, background=True)
                if not msg.startswith("🐘 Digested 0 pages"):
                    await app.bot.send_message(config.ALLOWED_USER_ID, msg)
        except Exception:
            log.exception("Background loop error")
        await asyncio.sleep(30)


async def post_init(app: Application) -> None:
    state["background"] = asyncio.get_running_loop().create_task(background(app))


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.ALLOWED_USER_ID:
        raise SystemExit("Fill in TELEGRAM_TOKEN and ALLOWED_USER_ID in .env first.")
    problem = shiba.seal_problem()
    if problem:
        raise SystemExit(f"🐕 Shiba won't start: {problem}.\n"
                         "Read what changed in identity.txt, actions.py and shiba.py, then run: python seal.py")
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
    log.info("Lobster %s starting (model=%s, proxy=%s)", config.VERSION, config.MODEL, config.PROXY or "none")
    app.run_polling()


if __name__ == "__main__":
    main()
