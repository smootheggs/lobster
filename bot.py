"""The Lobster's Telegram intercom, plus the Spider/Elephant schedule.

Run with:  python bot.py

One process does everything:
  - answers you on Telegram (only you)
  - asks Shiba before every action, with one-time approval cards
  - runs research jobs, the Spider every few hours, and the Elephant
    digest when the tablet is charging ("feast"), then unloads the model ("rest")
"""
import asyncio
import json
import logging
import shutil
import subprocess
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import elephant
import memory
import ollama
import research
import shiba
import spider
from actions import run_action
from brain import think

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lobster")

TAINT_MEMORY = 6          # after seeing web facts, stay cautious for this many turns
digest_lock = asyncio.Lock()
research_lock = asyncio.Lock()
state = {"last_crawl": 0.0, "last_digest": None, "offline_notified": set()}


def allowed(update: Update) -> bool:
    """Only you can talk to the Lobster. Everyone else is silently ignored."""
    user = update.effective_user
    if user and user.id == config.ALLOWED_USER_ID:
        return True
    shiba.audit("stranger_ignored", user=str(user.id if user else "?"))
    log.warning("Ignored message from user %s", user.id if user else "?")
    return False


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
    "/research <question>: research a topic and send a brief\n"
    "/save <url>: save a document (e.g. an annual report) to the Library\n"
    "/sources, /addsource <url>, /removesource <url>\n"
    "/crawl: run the Spider now · /digest: run the Elephant now · /retryfailed: retry failed pages\n"
    "/pause, /resume: freeze or unfreeze every write\n"
    "/audit: Shiba's last decisions"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(HELP)


def approval_buttons(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"ok:{code}"),
        InlineKeyboardButton("❌ No", callback_data=f"no:{code}"),
    ]])


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not shiba.message_allowed():
        await update.message.reply_text("🐕 Slow down a little: too many messages this minute.")
        return
    history = ctx.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})
    del history[:-10]  # short memory keeps a small model focused

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        decision = await think(history)
    except Exception as e:
        log.exception("Ollama call failed")
        await update.message.reply_text(f"🧠 My model isn't responding ({type(e).__name__}). "
                                        "Commands like /status, /pause and /save still work.")
        return

    # Taint is sticky: web facts from earlier turns are still in the conversation.
    if decision["tainted"]:
        ctx.user_data["taint_turns"] = TAINT_MEMORY
    tainted = ctx.user_data.get("taint_turns", 0) > 0
    ctx.user_data["taint_turns"] = max(0, ctx.user_data.get("taint_turns", 0) - 1)

    action, text = decision["action"], decision["text"]
    log.info("Model chose %s(%r) tainted=%s", action, text, tainted)
    verdict, reason = shiba.decide(action, text, tainted)

    if verdict == shiba.BLOCK:
        await update.message.reply_text(f"🐕 Shiba blocked {action}: {reason}.")
        return
    if verdict == shiba.ASK:
        code = shiba.new_approval(action, text)
        why = " (I used web facts just now, so I'm double-checking)" if tainted else ""
        await update.message.reply_text(
            f"{decision['reply']}\n\nI'd like to run {action}({text!r}){why}. OK? (valid 10 minutes)",
            reply_markup=approval_buttons(code),
        )
        return

    result = await asyncio.to_thread(run_action, action, text)
    out = "\n\n".join(p for p in (decision["reply"], result) if p) or "🦞"
    # Don't feed raw memory search results back into the model's context.
    history.append({"role": "assistant", "content": decision["reply"] if action == "search_memory" else out})
    await update.message.reply_text(out)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    choice, _, code = (query.data or "").partition(":")
    if choice == "no":
        shiba.cancel(code)
        await query.edit_message_text("Cancelled. Nothing was changed.")
        return
    rec, why = shiba.redeem(code) if choice == "ok" else (None, "Unknown button.")
    if rec is None:
        await query.edit_message_text(why)
        return
    result = await asyncio.to_thread(run_action, rec["action"], rec["text"])
    await query.edit_message_text(result or "Done.")


# --- Commands you type yourself (no model involved) ---------------------------

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    s = await asyncio.to_thread(memory.stats)
    charging = await asyncio.to_thread(is_charging)
    guard = "⏸️ PAUSED" if shiba.paused() else "clear"
    lines = [
        f"🧠 Model server: {'up' if await asyncio.to_thread(ollama.alive) else 'DOWN'} ({config.MODEL})",
        f"🐘 Memory: {s['owner']} owner facts, {s['web']} web facts",
        f"🕷️ Inbox: {spider.inbox_count()} pages waiting, {len(spider.load_sources())} sources, "
        f"{len(research.pending())} research jobs queued",
        f"🐕 Shiba: {guard}, {shiba.blocks_last_day()} blocks or rejections in 24h",
        f"🔌 Charging: {'yes' if charging else 'no' if charging is not None else 'unknown (install termux-api)'}",
        f"Last digest: {state['last_digest'] or 'never'} · version {config.VERSION}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        src = spider.load_sources()
        await update.message.reply_text("\n".join(src) if src else "No sources yet. Use /addsource <url>.")


def _writes_allowed() -> str | None:
    return "⏸️ Lobster is paused. /resume first." if shiba.paused() else None


async def cmd_addsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        msg = _writes_allowed() or (await asyncio.to_thread(spider.add_source, " ".join(ctx.args))
                                    if ctx.args else "Usage: /addsource <url>")
        await update.message.reply_text(msg)


async def cmd_removesource(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        msg = _writes_allowed() or (spider.remove_source(" ".join(ctx.args))
                                    if ctx.args else "Usage: /removesource <url>")
        await update.message.reply_text(msg)


async def cmd_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        if (msg := _writes_allowed()) or not ctx.args:
            await update.message.reply_text(msg or "Usage: /save <url>")
            return
        await update.message.reply_text("📥 Fetching...")
        await update.message.reply_text(await asyncio.to_thread(spider.save_document, ctx.args[0]))


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        question = " ".join(ctx.args)
        verdict, reason = shiba.decide("research", question, tainted=False)
        if verdict == shiba.BLOCK or not question:
            await update.message.reply_text(f"🐕 Not queued: {reason}." if question else "Usage: /research <question>")
            return
        await update.message.reply_text(run_action("research", question))


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(shiba.pause())


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(shiba.resume())


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        rows = shiba.recent(8)
        if not rows:
            await update.message.reply_text("🐕 Nothing logged yet.")
            return
        lines = [f"{r['ts'][5:16]} {r['kind']}"
                 + (f" {r.get('action', '')} {r.get('verdict', '')}" if r["kind"] == "decision" else "")
                 + (f" ({r['reason']})" if r.get("reason") and r.get("reason") != "ok" else "")
                 for r in rows]
        await update.message.reply_text("🐕 Last decisions:\n" + "\n".join(lines))


async def cmd_retryfailed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        n = await asyncio.to_thread(elephant.retry_failed)
        await update.message.reply_text(f"🐘 {n} failed pages moved back to the inbox. /digest to process them now.")


async def cmd_crawl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text("🕷️ Crawling...")
        await update.message.reply_text(await run_crawl())


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text("🐘 Digesting... (this can take a while)")
        await update.message.reply_text(await run_digest(ctx.bot))


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


async def run_digest(bot=None, job: int | None = None) -> str:
    """Digest the inbox. A research job (job=id) waits its turn instead of being skipped."""
    if shiba.paused():
        return "⏸️ Paused: the Elephant won't store anything until /resume."
    if job is None and digest_lock.locked():
        return "Already digesting."
    async with digest_lock:
        r = await asyncio.to_thread(elephant.digest, job)
    if r["model_down"]:
        return "🧠 The model is offline, so pages stay in the inbox until it's back. Check /status."
    state["last_digest"] = time.strftime("%Y-%m-%d %H:%M")
    if r["suspicious"] and bot is not None:
        await alert(bot, "rejected several instruction-like facts from:\n" + "\n".join(r["suspicious"])
                    + "\nDetails: /audit")
    return (f"🐘 Digested {r['pages']} pages: {r['facts']} new facts stored, "
            f"{r['rejected']} rejected by the filter, {r['failed']} pages failed. Model unloaded.")


async def run_research_jobs(app) -> None:
    """Work through queued research jobs, one at a time."""
    if shiba.paused() or research_lock.locked():
        return
    async with research_lock:
        for job in research.pending():
            send = app.bot.send_message
            if not await asyncio.to_thread(ollama.alive):
                if ("model", job["id"]) not in state["offline_notified"]:
                    state["offline_notified"].add(("model", job["id"]))
                    await send(config.ALLOWED_USER_ID, f"🧠 Model offline. Research #{job['id']} waits until it's back.")
                return
            await send(config.ALLOWED_USER_ID, f"🕷️ Researching #{job['id']}: {job['question']}")
            gathered = await asyncio.to_thread(research.gather, job)
            if gathered["offline"]:
                if job["id"] not in state["offline_notified"]:
                    state["offline_notified"].add(job["id"])
                    await send(config.ALLOWED_USER_ID, f"📴 Offline. Research #{job['id']} stays queued until I'm back online.")
                return
            if not gathered["urls"]:
                research.mark_done(job["id"], "nothing found")
                await send(config.ALLOWED_USER_ID, f"🔎 Research #{job['id']}: nothing found. "
                           + "; ".join(gathered["failed"][:3]))
                continue
            plan = ", ".join(gathered["plan"]["searches"])
            await send(config.ALLOWED_USER_ID, f"🔎 Searched: {plan}\n📚 {len(gathered['urls'])} articles fetched. "
                       "Digesting, which takes a few minutes on the tablet...")
            await send(config.ALLOWED_USER_ID, await run_digest(app.bot, job=job["id"]))
            text = await asyncio.to_thread(research.brief, job, gathered["urls"])
            research.mark_done(job["id"], "done", len(gathered["urls"]))
            try:                       # the brief is web content: be cautious for the next turns
                app.user_data[config.ALLOWED_USER_ID]["taint_turns"] = TAINT_MEMORY
            except Exception:
                pass
            await send(config.ALLOWED_USER_ID, text)


async def background(app: Application) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_research_jobs(app)
            if (not shiba.paused() and spider.load_sources()
                    and time.time() - state["last_crawl"] > config.SPIDER_EVERY_HOURS * 3600):
                log.info(await run_crawl())
            if (config.DIGEST_ON_CHARGE and spider.inbox_count() and not digest_lock.locked()
                    and await asyncio.to_thread(is_charging)):
                await app.bot.send_message(config.ALLOWED_USER_ID, await run_digest(app.bot))
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
    for name, fn in [("status", cmd_status), ("sources", cmd_sources), ("addsource", cmd_addsource),
                     ("removesource", cmd_removesource), ("save", cmd_save), ("research", cmd_research),
                     ("pause", cmd_pause), ("resume", cmd_resume), ("audit", cmd_audit),
                     ("crawl", cmd_crawl), ("digest", cmd_digest), ("retryfailed", cmd_retryfailed)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    shiba.audit("started", model=config.MODEL)
    log.info("Lobster %s starting (model=%s, proxy=%s)", config.VERSION, config.MODEL, config.PROXY or "none")
    app.run_polling()


if __name__ == "__main__":
    main()
