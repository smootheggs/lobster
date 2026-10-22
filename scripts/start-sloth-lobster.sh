#!/data/data/com.termux/files/usr/bin/sh
# Starts Ollama and Sloth Lobster in the background, once. Safe to run again: it won't start a second copy.
# Termux:Boot runs this at boot (copy it to ~/.termux/boot/), the widget runs it from ~/.shortcuts/.
LOBSTER="$HOME/lobster"
LOG="$LOBSTER/data/bot.log"
mkdir -p "$LOBSTER/data"
termux-wake-lock
if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
  nohup ollama serve >>"$LOBSTER/data/ollama.log" 2>&1 &
  sleep 5
fi
if ! pgrep -f "python bot.py" >/dev/null 2>&1; then
  cd "$LOBSTER" && nohup python bot.py >>"$LOG" 2>&1 &
  echo "Sloth Lobster started (log: $LOG)"
else
  echo "Sloth Lobster is already running"
fi
