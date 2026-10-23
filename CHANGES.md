# What changed in each version

## 2026-10-23.32.1
- A gentler wait: the sloth on his lobster first floats in place (fades in, bobs, one bubble rises). Quick answers end there.
- If the reply takes longer than about 1.3 seconds, the same pair sets off and swims across until it arrives.
- Replies settle in with a short fade. Redrawing the chat doesn't restart the animation. Reduced motion: a still sloth.
- Sealed files: none changed.

## 2026-10-23.32
- Exact answers by code: date math ("days until 25 July", "what weekday is…"), sums ("15% of 240"), units ("10 km in miles") and currency ("100 USD in HKD", European Central Bank daily rates, cached; offline it uses the last rates and shows their date).
- /ask and the chat's memory search by meaning and exact words together (names, dates and numbers are found exactly).
- A phrasing suite: more than 11,000 ways of asking land in the right place; 300 look-alikes are left to the model.
- The sloth takes a moment: instant answers show the swimming sloth (Telegram: "typing…") for about a second.
- Fixed: the time-zone fix is `pip install tzdata` (Termux has no tzdata package). doctor.py checks keyword search too.
- Sealed files: none changed.

## 2026-10-23.31.1
- The clock finds Termux's time-zone files (pkg install tzdata), wherever Python looks.
- Without time-zone data, the clock says how to fix it instead of failing the update; doctor.py shows it too.
- The update tests use the tablet's own Python (Android won't run programs from the temporary folder).
- Sealed files: none changed.

## 2026-10-23.31
- Time questions ("what time is it in Hong Kong?") are answered by code: exact, with daylight saving, never guessed.
- The model is told today's date and never to guess the time, prices, scores, weather or news.
- Wrong? under the latest answer in the app, or /wrong in Telegram: it's noted in "Sloth Lobster misses.md" in your work folder and becomes a test.
- The chat scrolls on its own: the keyboard no longer pushes it off the screen. A ↓ button takes you back to the latest.
- Safer updates: tested in their own folder first, memory backed up, previous version kept (update.sh --rollback).
- The start script says when it didn't really start, and why.
- Sealed files: none changed.
