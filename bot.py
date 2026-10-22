"""The Lobster's Telegram intercom, plus the Spider/Elephant schedule.

Run with:  python bot.py

One process does everything:
  - answers you on Telegram (only you)
  - runs the Spider every few hours
  - runs the Elephant digest when the tablet is charging ("feast"),
    then unloads the model ("rest")
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
import spider
from actions import needs_approval, run_action
from brain import think

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lobster")

TAINT_MEMORY = 6          # after seeing web facts, stay cautious for this many turns
digest_lock = asyncio.Lock()
state = {"last_crawl": 0.0, "last_digest": None}


def allowed(update: Update) -> bool:
    """Only you can talk to the Lobster. Everyone else is silently ignored."""
    user = update.effective_user
    if user and user.id == config.ALLOWED_USER_ID:
        return True
    log.warning("Ignored message from user %s", user.id if user else "?")
    return False


# --- Conversation -------------------------------------------------------------

HELP = (
    "🦞 Just talk to me, or use:\n"
    "/status: memory, cache and model health\n"
    "/sources: what the Spider watches\n"
    "/addsource <url>: watch a new page or RSS feed\n"
    "/crawl: run the Spider now\n"
    "/digest: run the Elephant now"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(HELP)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    history = ctx.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})
    del history[:-10]  # short memory keeps a small model focused

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        decision = await think(history)
    except Exception as e:
        log.exception("Ollama call failed")
        await update.message.reply_text(f"🧠 My model isn't responding ({type(e).__name__}). Is `ollama serve` running?")
        return

    # Taint is sticky: web facts from earlier turns are still in the conversation.
    if decision["tainted"]:
        ctx.user_data["taint_turns"] = TAINT_MEMORY
    tainted = ctx.user_data.get("taint_turns", 0) > 0
    ctx.user_data["taint_turns"] = max(0, ctx.user_data.get("taint_turns", 0) - 1)

    action, text = decision["action"], decision["text"]
    log.info("Model chose %s(%r) tainted=%s", action, text, tainted)

    if needs_approval(action, tainted):
        ctx.user_data["pending"] = decision
        why = " (I used web facts just now, so I'm double-checking)" if tainted else ""
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data="yes"),
            InlineKeyboardButton("❌ No", callback_data="no"),
        ]])
        await update.message.reply_text(
            f"{decision['reply']}\n\nI'd like to run {action}({text!r}){why}. OK?",
            reply_markup=buttons,
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
    pending = ctx.user_data.pop("pending", None)
    if pending is None:
        await query.edit_message_text("Nothing waiting for approval.")
    elif query.data == "yes":
        result = await asyncio.to_thread(run_action, pending["action"], pending["text"])
        await query.edit_message_text(result or "Done.")
    else:
        await query.edit_message_text("Cancelled. Nothing was changed.")


# --- Commands you type yourself (no model involved) ---------------------------

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    s = await asyncio.to_thread(memory.stats)
    charging = await asyncio.to_thread(is_charging)
    lines = [
        f"🧠 Model server: {'up' if await asyncio.to_thread(ollama.alive) else 'DOWN'} ({config.MODEL})",
        f"🐘 Memory: {s['owner']} owner facts, {s['web']} web facts",
        f"🕷️ Inbox: {spider.inbox_count()} pages waiting, {len(spider.load_sources())} sources",
        f"🔌 Charging: {'yes' if charging else 'no' if charging is not None else 'unknown (install termux-api)'}",
        f"Last digest: {state['last_digest'] or 'never'}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        src = spider.load_sources()
        await update.message.reply_text("\n".join(src) if src else "No sources yet. Use /addsource <url>.")


async def cmd_addsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text(spider.add_source(" ".join(ctx.args)) if ctx.args else "Usage: /addsource <url>")


async def cmd_crawl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text("🕷️ Crawling...")
        await update.message.reply_text(await run_crawl())


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await update.message.reply_text("🐘 Digesting... (this can take a while)")
        await update.message.reply_text(await run_digest())


# --- Spider / Elephant schedule ------------------------------------------------

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


async def run_digest() -> str:
    if digest_lock.locked():
        return "Already digesting."
    async with digest_lock:
        r = await asyncio.to_thread(elephant.digest)
    state["last_digest"] = time.strftime("%Y-%m-%d %H:%M")
    return (f"🐘 Digested {r['pages']} pages: {r['facts']} new facts stored, "
            f"{r['rejected']} rejected by the filter, {r['failed']} pages failed. Model unloaded.")


async def background(app: Application) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            if spider.load_sources() and time.time() - state["last_crawl"] > config.SPIDER_EVERY_HOURS * 3600:
                log.info(await run_crawl())
            if (config.DIGEST_ON_CHARGE and spider.inbox_count() and not digest_lock.locked()
                    and await asyncio.to_thread(is_charging)):
                await app.bot.send_message(config.ALLOWED_USER_ID, await run_digest())
        except Exception:
            log.exception("Background loop error")
        await asyncio.sleep(120)


async def post_init(app: Application) -> None:
    app.create_task(background(app))


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.ALLOWED_USER_ID:
        raise SystemExit("Fill in TELEGRAM_TOKEN and ALLOWED_USER_ID in .env first.")
    builder = Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init)
    if config.PROXY:
        builder = builder.proxy(config.PROXY).get_updates_proxy(config.PROXY)
    app = builder.build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("addsource", cmd_addsource))
    app.add_handler(CommandHandler("crawl", cmd_crawl))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Lobster starting (model=%s, proxy=%s)", config.MODEL, config.PROXY or "none")
    app.run_polling()


if __name__ == "__main__":
    main()
