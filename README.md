# 🦞 Lobster: core (Lobster, Spider, Elephant, Shiba)

One process on the Y700 runs everything:

| Part | File | Job | Can it act? |
|---|---|---|---|
| 🦞 Lobster | `bot.py`, `brain.py`, `actions.py` | Talks to you on Telegram, picks actions from the allowlist | Only allowlisted actions |
| 🕷️ Spider | `spider.py` | Downloads the pages in `sources.txt`, untouched | No model, no actions |
| 🐘 Elephant | `elephant.py`, `memory.py` | Cleans pages, extracts facts, filters them, stores them tagged `web` | No actions |
| 🐕 Shiba | `shiba.py`, `seal.py` | Gate for every action, approval, link and web fact; audit log; pause switch | Allows, asks you, or blocks. Plain code, no model |
| 🔎 Research | `research.py` | The model proposes search words; the Spider fetches only from fixed providers; Lobster writes a brief | Words only, never links |

**Daily rhythm ("feast and rest"):** the Spider fetches every 6 hours. When you plug the tablet in to charge, the Elephant digests the inbox, messages you a summary, and unloads the model to free RAM.

## One-time setup (Termux from F-Droid, not the Play Store)

```bash
pkg update && pkg install python python-numpy ollama termux-api
termux-wake-lock
ollama serve &
ollama pull qwen2.5:3b          # ~2 GB. Pull everything BEFORE travelling to China
ollama pull nomic-embed-text    # small embedding model for memory search
cd lobster
pip install -r requirements.txt
pip freeze > requirements.lock  # then use the lock file for future installs
cp .env.example .env && nano .env
python test_lobster.py         # everything must pass
python seal.py                 # seal Shiba's rules (your signature)
python bot.py
```

- **Token:** @BotFather → /newbot. **Your ID:** @userinfobot.
- **Charging detection** needs the *Termux:API* app (F-Droid) as well as `pkg install termux-api`. Without it, use `/digest` by hand.
- **Proxy:** set `PROXY=` in `.env` for Telegram and the Spider. For `ollama pull` through a proxy use `HTTPS_PROXY=... ollama pull ...` only; don't set `HTTP_PROXY` for Ollama.

### Keep Android from killing it (do this once)

1. Developer options → turn on **Wireless debugging** (tap Build number 7× to unlock Developer options).
2. In Termux: `pkg install android-tools`, then `adb pair localhost:<pair-port> <code>` and `adb connect localhost:<port>`.
3. `adb shell "settings put global settings_enable_monitor_phantom_procs false"`
4. Settings → Battery → set Termux to **Unrestricted**. Consider an 80% charge limit.

## Using it

- Talk normally: "remind me to buy a USB-C SSD", "remember I prefer window seats", "what do you know about copper?"
- `/addsource https://example.com/feed.xml`: watch a page or RSS feed
- `/crawl`, `/digest`: run the Spider or Elephant now
- `/status`: model, memory, inbox, Shiba, charging
- `/research <question>` (or just "research English kings"): fetches articles, digests them, sends a brief with sources
- `/save <url>`: keeps a document (PDF, HTML, text, CSV) in the Library with a receipt. Set `LIBRARY_DIR` inside your vault to sync it to Google Drive. `pip install pypdf` to index PDF text
- `/removesource <url>`: stops watching a source (archived, not deleted)
- `/pause` and `/resume`: freeze every write instantly
- `/audit`: Shiba's last decisions
- `/retryfailed`: move pages that failed (e.g. while Ollama was down) back into the inbox

## How it stays safe

- **Only your Telegram ID** gets answers.
- **Action-selector design:** the model can only pick from `ACTIONS`; a JSON schema enforces it and code re-checks it.
- **Nothing deletes your data.** Notes and tasks are append-only; raw pages are moved, never removed.
- **Every fact is tagged** `owner` (from you, via `MEMORY.md`) or `web` (untrusted).
- **Taint policy** (`needs_approval` in `actions.py`): once web facts are in the conversation, any *write* action needs your Yes for the next few turns. `confirm`-tier actions always do.
- **Web text never sets parameters:** sources are added only by your `/addsource` command, never by the model.
- **The Elephant doesn't trust its model:** pages are cleaned by code first (scripts, comments and hidden elements removed), facts are forced into a schema, then a plain-code filter rejects anything with links, code or instruction-like wording.
- **No plugins, no shell, no file reading**, and a tiny pinned dependency list.

`data/MEMORY.md` is yours: edit it by hand any time, and the next digest re-syncs it.

## Shiba's rules

- Every action goes through `shiba.decide()`: ALLOW, ASK (one-time approval card, valid 10 minutes, bound to that exact action) or BLOCK.
- Links are refused if they point at the tablet, your home network or reserved addresses, checked again on every redirect.
- Everything Shiba decides is in `data/audit.jsonl`. Several rejected facts from one page trigger a Telegram alert (max one per 10 minutes).
- **The seal:** Lobster won't start if `identity.txt`, `actions.py` or `shiba.py` changed since you last ran `python seal.py`. After any update: read the changes, run the tests, then re-seal. Never let anything else run `seal.py` for you.

## Next
4. Always-on local dashboard with a password (for when Telegram is blocked)
