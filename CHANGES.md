# What changed in each version

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
