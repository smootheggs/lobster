# 🦞 Lobster

A private research agent on the Legion Y700. It collects, files and guards knowledge for your vault (Slothvault), so you, Obsidian and your Claude rooms can draw on it.

| Part | One job | Files | Never |
|---|---|---|---|
| 🦞 **Lobster** | Executes what you ask, through one decision path | `conversation.py`, `actions.py`, `brain.py`, `research.py`, `bot.py` (Telegram), `web.py` (the app) | Acts outside the allowlist |
| 🕷️ **Spider** | Collects: research from fixed providers, documents you `/save` | `spider.py` | Runs a model, picks a site itself, follows links inside pages |
| 🐘 **Elephant** | Stores and manages knowledge: facts with receipts, cards in your vault, `/ask`, your reviews | `elephant.py`, `memory.py`, `vault.py` | Rewords a source, deletes anything, writes outside its inbox |
| 🐕 **Shiba** | Guards every crossing: allows, asks you, or blocks; can pause everything | `shiba.py`, `seal.py` | Uses a model, or gets overruled by a web page |

## What it does for you

- **`/research <question>`:** the Spider collects Wikipedia articles (and news for recent events). The Elephant keeps each article's own most relevant sentences, then writes a brief. The brief's key points become **cards** in `Slothvault/Lobster Inbox/`, with a **source page** per article and a **map** linking them.
- **`/note <text>`:** saved exactly as typed: a trusted fact, and a note in your vault inbox.
- **`/ask <question>`:** answers from your vault only, your notes first, each web fact with its source. No web, no model, under a second.
- **`/save <link>`:** keeps a document (an annual report, say) in your work folder with a receipt, and indexes its text for `/ask`.
- **Reviews:** set a card's `status` to `verified` or `wrong` in Obsidian, or tap Mark verified or Mark wrong in the app. The Elephant reads it back within 5 minutes. Wrong facts are never used again, and never deleted.

The other commands are `/status`, `/stop` (stops research and keeps what was read), `/new` (a fresh chat), `/pause` and `/resume`.

## The card guide (version 1; every note carries `card_version`, so it can grow)

```markdown
---
id: lb-f42                       # stable: rename or move the file freely
type: card                       # card | note | source | map
loci: [world history]            # from LOCI in .env
status: unreviewed               # unreviewed | verified | wrong
made_by: lobster                 # lobster | you
sources: ["[[Hanseatic League (Wikipedia)]]"]
maps: ["[[Hanseatic League]]"]
created: 2026-10-22
card_version: 1
---
# Lübeck led the Hanseatic League       ← the claim, as a full phrase (also the file name)

The claim, as the source states it.

> The source sentence with its neighbours
> — [[Hanseatic League (Wikipedia)]], fetched 2026-10-22
```

The rules:
- The Elephant writes only inside the inbox and never overwrites (a taken name gets " (2)").
- Every file is written whole, then renamed, so sync never uploads half a note.
- After writing, the Elephant changes only a card's `status:` line, and only when you tap a button in the app.
- `Lobster review.base` is a ready-made Obsidian Bases view of every card waiting for review, and `Lobster log <date>.md` lists everything written that day.

## Speed

- **No model on most paths.** Commands, `/ask` and digesting use no chat model. Research uses it twice: once to plan search words, once to choose the brief's points and title them.
- **Titles are checked.** Every title must match its source sentence (Shiba's grounding check), or the sentence itself is used.
- **Search stays fast.** It's tested at 20,000 facts.
- **Everything is timed.** Every slow step is recorded in `data/timings.jsonl`, and `/status` shows how long research typically takes.

## One-time setup (Termux from F-Droid)

```bash
pkg update && pkg install python python-numpy ollama termux-api
termux-setup-storage            # lets Lobster reach Documents/ (your vault)
termux-wake-lock
ollama serve &
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
cd ~/lobster && pip install -r requirements.txt
cp .env.example .env && nano .env     # token, your ID, VAULT_DIR, WORK_DIR
python test_lobster.py && python seal.py && python bot.py
```

**Sync (Autosync for Google Drive):**
- **Pair 1:** Slothvault ↔ Drive, two-way.
- **Pair 2:** `WORK_DIR` → Drive, upload only.
- Disable the Obsidian Google Drive plugin on every device. It can't see files written by other tools, and its own documentation warns that this can lose data.

**Keep Android from killing it:**
- Disable the phantom process killer with `adb shell "settings put global settings_enable_monitor_phantom_procs false"`.
- Set Termux and Autosync to Unrestricted battery use.

## How it stays safe

- **Only your Telegram ID gets answers.** The app needs your password: a scrypt hash, 5 tries, then a 15-minute lockout.
- **The model chooses; code decides.**
  - The model picks one allowlisted action. Shiba decides whether it runs, asks you, or blocks it.
  - After web facts appear in a chat, every write asks first (for 6 turns, even across a restart).
- **Web text is data, never instructions.**
  - Pages are cleaned by code.
  - Facts are the source's own sentences.
  - Shiba's context-aware filter rejects orders, credential requests, links and code, after folding look-alike and invisible characters.
- **Links are guarded.** No link may point at the tablet or your home network, checked again on every redirect.
- **The seal.** Lobster won't start if `identity.txt`, `actions.py`, `shiba.py` or `web.py` changed since you last ran `python seal.py`. Read the changes, run the tests, then re-seal. Never let anything else run it for you.
- **Nothing is deleted.** Notes and jobs are appended, and reviews mark things rather than remove them. The exception is backup copies beyond the newest 14 (nightly, in `WORK_DIR/Backups`).

## The phone app

Open it on the S25 Plus, the S11 Ultra or the Y700 itself. It adapts to each:
- **Phones:** one column, with a drawer for the menu.
- **Y700 upright:** a centred column.
- **Sideways and the S11 Ultra:** a sidebar, with briefs opening beside the chat.

To turn it on, set `WEB_BIND` in `.env` to the tablet's Tailscale address (`100.x.y.z`), run `python set_password.py`, restart, and open `http://100.x.y.z:8787`. Never use `0.0.0.0`, and never forward a router port.
