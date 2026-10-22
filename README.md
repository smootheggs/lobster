# 🦞 Lobster: core (Lobster, Spider, Elephant, Shiba)

One process on the Y700 runs everything:

| Part | File | Job | Can it act? |
|---|---|---|---|
| 🦞 Lobster | `conversation.py`, `brain.py`, `actions.py`, `bot.py` | One decision path for every way you talk to it (Telegram today); picks actions from the allowlist | Only allowlisted actions |
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
- `/note <text>`: save a note exactly as typed, with no model involved, so it works even when the model is down
- `/newchat`: clear the chat history (notes, tasks and memory are untouched)
- `/research <question>` (or just "research English kings"): fetches articles, digests them, sends a brief with sources
- `/save <url>`: keeps a document (PDF, HTML, text, CSV) in the Library with a receipt. Set `LIBRARY_DIR` inside your vault to sync it to Google Drive. `pip install pypdf` to index PDF text
- `/removesource <url>`: stops watching a source (archived, not deleted)
- `/pause` and `/resume`: freeze every write instantly
- `/audit`: Shiba's last decisions
- `/jobs`: what Lobster is doing right now, and which research jobs are waiting
- While the Elephant is digesting, chat replies start with "🐘 The Elephant is digesting, so replies may be slow": chat and digest take turns with the model instead of fighting over it
- Chat history and Shiba's after-web-facts caution are saved in `data/session.json`, so a restart doesn't reset them
- `/backup`: copy the memory database now (it also runs nightly while charging; the newest 14 are kept). Set `BACKUP_DIR` inside your vault to sync backups to Google Drive
- `/retryfailed`: move pages that failed (e.g. while Ollama was down) back into the inbox

## How it stays safe

- **Only your Telegram ID** gets answers.
- **Action-selector design:** the model can only pick from `ACTIONS`; a JSON schema enforces it and code re-checks it.
- **Nothing deletes your data.** Notes and tasks are append-only; raw pages are moved, never removed.
- **Every fact is tagged** `owner` (from you, via `MEMORY.md`) or `web` (untrusted).
- **Taint policy** (`shiba.decide`): once web facts are in the conversation, any *write* action needs your Yes for the next few turns. `confirm`-tier actions always do.
- **Web text never sets parameters:** sources are added only by your `/addsource` command, never by the model.
- **The Elephant doesn't trust its model:** pages are cleaned by code first (scripts, comments and hidden elements removed), facts are forced into a schema, then a plain-code filter rejects anything with links, code or instruction-like wording.
- **No plugins, no shell, no file reading**, and a tiny pinned dependency list.

`data/MEMORY.md` is yours: edit it by hand any time, and the next digest re-syncs it.

## Shiba's rules

- Every action goes through `shiba.decide()`: ALLOW, ASK (one-time approval card, valid 10 minutes, bound to that exact action) or BLOCK.
- Links are refused if they point at the tablet, your home network or reserved addresses, checked again on every redirect.
- Everything Shiba decides is in `data/audit.jsonl`. Several rejected facts from one page trigger a Telegram alert (max one per 10 minutes).
- **Grounding:** every number in a web fact must appear in the sentence it came from, and every name in that sentence or the ones beside it; otherwise the fact is rejected and logged. Each stored fact keeps that sentence as its receipt.
- **The fact filter is context-aware:** it rejects orders addressed to an agent, credential requests, links and code, but lets honest history through ("secretly married", "the Zimmermann Telegram", "a token of friendship"). Look-alike letters, full-width text and invisible characters are folded to plain text first, so they can't sneak an instruction past it.
- **The seal:** Lobster won't start if `identity.txt`, `actions.py`, `shiba.py` or `web.py` changed since you last ran `python seal.py`. After any update: read the changes, run the tests, then re-seal. Never let anything else run `seal.py` for you.

## The phone app (optional)

A private app for the S25 Plus and the S11 Ultra: Chat (with approval cards), Research (queue and briefs), Library (saved documents with receipts), Memory (search and notes) and Shiba (status, audit, pause). It runs inside Lobster and uses the same conversation engine as Telegram, so every message gets the same checks. Telegram keeps working alongside it.

It stays off until you do both steps below.

1. **Choose how the phones reach the tablet.** Set `WEB_BIND` in `.env` to one address:
   - **Tailscale (recommended):** install Tailscale on the tablet and both phones, sign in with the same account, and use the tablet's `100.x.y.z` address. It works anywhere, and Tailscale encrypts everything.
   - **Home Wi-Fi or the tablet's hotspot:** use the tablet's Wi-Fi address (`termux-wifi-connectioninfo` shows it). Traffic is not encrypted on that network unless you add a certificate (`WEB_CERT`, `WEB_KEY`), so prefer Tailscale.
   - Never `0.0.0.0`, and never forward a router port. Lobster refuses `0.0.0.0` unless you set `WEB_ALLOW_ALL=1`.
2. **Set a password** on the tablet: `python set_password.py`. Only a scrypt hash is stored in `.env`. Then restart Lobster.

Open `http://<WEB_BIND>:8787` on a phone, log in, and choose **Add to Home screen**.

The locks: 5 wrong passwords lock the login for 15 minutes and alert you on Telegram; sessions last 12 hours; every change needs an anti-forgery token; requests for any other host name are refused (stops DNS-rebinding tricks); strict security headers; files can't be read outside `web/` and the Library; Telegram and the app share one rate limit. `web.py` is sealed like Shiba's rules.

## Next
- The rest of Build 6: Shiba's scorecard, a decoy canary, `/drill` and a Sunday summary
