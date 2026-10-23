# 🦞 Sloth Lobster

A private research agent on the Legion Y700. It collects, files and guards knowledge for your vault (Slothvault), so you, Obsidian and your Claude rooms can draw on it.

| Part | One job | Files | Never |
|---|---|---|---|
| 🦞 **Sloth Lobster** | Executes what you ask, through one decision path | `conversation.py`, `actions.py`, `brain.py`, `research.py`, `bot.py` (Telegram), `web.py` (the app) | Acts outside the allowlist |
| 🕷️ **Spider** | Collects: research from fixed providers, documents you `/save` | `spider.py` | Runs a model, picks a site itself, follows links inside pages |
| 🐘 **Elephant** | Stores and manages knowledge: facts with receipts, cards in your vault, `/ask`, your reviews | `elephant.py`, `memory.py`, `vault.py` | Rewords a source, deletes anything, writes outside its inbox |
| 🐕 **Shiba** | Guards every crossing: allows, asks you, or blocks; can pause everything | `shiba.py`, `seal.py` | Uses a model, or gets overruled by a web page |

## What it does for you

- **`/research <question>`:** quick research, outline first. Sloth Lobster splits your question into 2–3 sub-questions, using the overview article's own section headings as hints. The Spider collects Wikipedia articles for each one, plus news for recent events. The Elephant keeps each article's own sentences that answer them. The brief has three parts:
  - a **bottom line**;
  - a short answer per sub-question, with its cards;
  - **Still open**: sub-questions little was found on, and dates sources disagree on.

  Every sentence the model writes is checked by code against the facts it cites: each name and number must be in them, or the sentence is dropped. The cards land in `Slothvault/Lobster Inbox/`, with a **source page** per article and a **map** that follows the outline.
- **`/deep <question>`:** deep research, overnight.
  1. You get a plan of 3–5 sub-questions to OK, change (`/plan 7`, then one per line) or cancel.
  2. Once you OK it, it runs between 23:00 and 06:00 while the tablet charges. It does 1 broad round, then up to 2 rounds that search again only for the sub-questions code finds thin, with a 60-minute limit.
  3. It writes with the bigger model (`qwen2.5:7b`) and unloads it afterwards.
  4. It saves its place after every round, and steps aside for any quick research you ask for in between.
  5. The brief is here in the morning. In the app, use the **Deep** chip.
- **`/note <text>`:** saved exactly as typed: a trusted fact, and a note in your vault inbox.
- **`/ask <question>`:** answers from your vault only, your notes first, each web fact with its source. No web, no model, under a second.
- **`/save <link>`:** keeps a document (an annual report, say) in your work folder with a receipt, and indexes its text for `/ask`.
- **Reviews:** the question is *does the card say what its source says?* In the app, Cards to review shows one card at a time: the claim, the source sentence highlighted among its neighbours, and a link that opens the source at that sentence. Code checks each card first: other sources agreeing, sources disagreeing on a year, and reworded titles. Doubtful cards come first. Swipe right (matches) or left (doesn't), or tap Not sure / Not useful. In Obsidian, set `status` to `verified`, `wrong` or `set_aside`. The Elephant reads it back within 5 minutes. Wrong facts are never used again, and never deleted.

- **The time and date:** "what time is it in Hong Kong?" (or any city, or just "what time is it") is answered by code in a moment: exact, with daylight saving, never guessed. The model is told today's date and never to guess the time, prices, scores, weather or news.
- **`/wrong`** (or *Wrong?* under the latest answer in the app): that answer was wrong. It's added to `Sloth Lobster misses.md` in your work folder, with your correction if you add one (`/wrong it's 12:20 AM there`), and becomes a test in the next update.

The other commands are `/plan <number>` (see or change a deep plan), `/status`, `/stop` (stops research and keeps what was read), `/new` (a fresh chat), `/pause` and `/resume`.

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
pkg update && pkg install python python-numpy ollama termux-api openssl-tool
termux-setup-storage            # lets Sloth Lobster reach Documents/ (your vault)
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
- **The seal.** Sloth Lobster won't start if `identity.txt`, `actions.py`, `shiba.py` or `web.py` changed since you last ran `python seal.py`. Read the changes, run the tests, then re-seal. Never let anything else run it for you.
- **Nothing is deleted.** Notes and jobs are appended, and reviews mark things rather than remove them. The exception is backup copies beyond the newest 14 (nightly, in `WORK_DIR/Backups`).

## The app: on the tablet and the phones

It looks and works the same everywhere, and adapts to each screen:
- **Phones:** one column, with a drawer for the menu.
- **Y700 upright:** a centred column.
- **Sideways and the S11 Ultra:** a sidebar, with briefs opening beside the chat.

**The start screen** shows the time, the weather, a hello and four ways to begin. To set the weather's place, tap the weather line (or *Add weather*). Type a city, choose °F or °C, then Save. *Turn off* hides it. Only the place name goes out, once, to find it on the map, then rounded coordinates to Open-Meteo. The setting is kept in `data/settings.json`; `WEATHER_PLACE` in `.env` is only the starting value.

**Three doors, one app** (`WEB_BIND=local, 100.x.y.z, wifi`):

| Door | Address | When |
|---|---|---|
| This tablet | `https://127.0.0.1:8787` | always; install it to the home screen |
| Tailscale | `https://100.x.y.z:8787` | from anywhere with internet |
| Wi-Fi | `https://<Wi-Fi address>:8787` | only on networks named in `WEB_TRUSTED_WIFI` (your hotspot, home) |

- **HTTPS everywhere.** Sloth Lobster makes its own certificate authority. Install `Sloth Lobster CA.crt` from the work folder once per device. The authority can only vouch for private addresses, never for a real website. An app pretending to be Sloth Lobster can't get your password.
- **Offline.** With no internet, `/research` waits in the queue and runs by itself when you're back online. `/ask`, `/note`, reviews and the Library keep working through the tablet door, the hotspot door, or both.
- **Notes held on the phone.** When the tablet can't be reached, a note waits on the phone (up to 50) and is sent by itself later. Each note has an id, so it's never saved twice. It still goes through Shiba.
- **Logins** survive restarts, stored only as hashes. "Log out everywhere" is in the status panel, and a new password logs everyone out.
- **Ollama** older than 0.1.29 (the DNS-rebinding fix) is flagged in `/status` and the app.

**Starts by itself:** copy `scripts/start-sloth-lobster.sh` to `~/.termux/boot/` (Termux:Boot) and `scripts/open-sloth-lobster.sh` to `~/.shortcuts/` (Termux:Widget). Logs go to `data/bot.log`.

Never use `0.0.0.0`, and never forward a router port.

## The cast

| Who | Emoji | Speaks when |
|---|---|---|
| You, the sloth | 🦥 | rarely: the first chat of the day, a fresh start |
| Lobster | 🦞 | replies, research queued, stops, offline |
| Spider | 🕷️ | collecting, articles found, saved links |
| Elephant | 🐘 | briefs, cards, `/ask`, notes, backups |
| Shiba | 🐕 | approvals, blocks, pause, logins |

Warm and a little playful, never overdone: one emoji per message, whoever is acting. Cards and briefs stay in the sources' own words.

## Hotspot mode (no internet)

One-time setup:
1. On the S25: a hotspot password, "turn off when idle" set to Never, and the hotspot tile in Quick Settings.
2. On the tablet: join the hotspot with auto-reconnect, set a static IP for it, keep Location on, and put its exact name in `WEB_TRUSTED_WIFI`.
3. On the S25, with the hotspot on: open `https://<static IP>:8787` and install it as "Sloth Lobster (offline)".

Then going offline is two taps: hotspot on, open the app. The door opens within about 15 seconds. If an app can't reach the tablet, its bar offers "Try hotspot" or "Try Tailscale".

## Offline Wikipedia (Kiwix files)

Put a Kiwix Wikipedia file (e.g. `wikipedia_en_top_mini_2026-09.zim`) in `/storage/emulated/0/Kiwix`. The Spider reads it straight from disk: no server, no port, no network.
- **Offline, or when the internet drops mid-research:** research reads the offline copy instead of waiting.
- **Online:** live Wikipedia stays first (fresher, and it has every article).
- **Finding articles:** search words are matched to article titles and redirects by code, never by the model. The text then goes through the same cleaning and Shiba filter as web pages.
- **Marking:** briefs mark offline sources "(offline copy)", and their source pages carry `copy:` with the file's name.
- **Damaged or crafted files** are refused or skipped: offsets are checked, unpacked blocks are capped, and redirects stop after 5.
- **Check it on the tablet:** `python zim.py "Hanseatic League"`

## Looking after it

- **Check-up:** `python doctor.py` prints one line per part, ✅ or ⚠️ with the fix. It covers the seal, Ollama and models, app doors, certificate expiry, the vault, backups, space, offline Wikipedia, time zones, start-up and the internet. It only looks; it changes nothing and prints no secrets.
- **Updating:** `sh ~/lobster/scripts/update.sh` takes the newest `lobster-core*.zip` from Downloads.
  - It unpacks the new version into its own folder (`~/.lobster-next`) and tests it there. If any test fails, nothing changes.
  - It backs up your memory, keeps the previous version in `~/.lobster-prev`, installs, restarts, and shows what changed.
  - If sealed files change, it stops **before changing anything**, shows how to read each change, and gives one line to apply, seal (your signature) and restart.
  - `sh ~/lobster/scripts/update.sh --rollback` puts the previous version back.
  - Your data, `.env` and `.git` are never touched.
- **Starting:** the start script checks a few seconds later that Sloth Lobster is really running, and if not, says why (for example, waiting for your seal).
- **Certificates:** the app's certificate renews itself 30 days before it expires (checked hourly).
- **Research scorecard:** `python research_bench.py` scores your finished briefs by code. It shows sub-questions answered, sentences that passed the citation check, sources and time. It's read-only. Run it after a week and compare.
- **What you use:** `/status` and the app show this week's counts (research, asks, notes, reviews, shares, held notes). A short line arrives on Sunday evenings. These come from logs Sloth Lobster already keeps; nothing new is tracked.
- **Cards (guide v2):** new cards carry `about`, `question` and `section`. A title never starts with "it" or "its": the subject is put in, and the source sentence stays exact.
