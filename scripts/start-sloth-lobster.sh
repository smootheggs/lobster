#!/data/data/com.termux/files/usr/bin/sh
# Starts Ollama and Sloth Lobster in the background, once. Safe to run again: it won't start a second copy.
# Termux:Boot runs this at boot (copy it to ~/.termux/boot/), the widget runs it from ~/.shortcuts/.
# It checks a few seconds later that Sloth Lobster is really running, and says why if it isn't
# (most often: sealed files changed and need your seal).
LOBSTER="$HOME/lobster"
LOG="$LOBSTER/data/bot.log"
mkdir -p "$LOBSTER/data"
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
  nohup ollama serve >>"$LOBSTER/data/ollama.log" 2>&1 &
  sleep 5
fi
if pgrep -f "python bot.py" >/dev/null 2>&1; then
  echo "Sloth Lobster is already running"
  exit 0
fi
MARK=$(wc -c < "$LOG" 2>/dev/null || echo 0)
cd "$LOBSTER" && nohup python bot.py >>"$LOG" 2>&1 &
sleep 6
if pgrep -f "python bot.py" >/dev/null 2>&1; then
  echo "Sloth Lobster started (log: $LOG)"
  exit 0
fi
WHY=$(tail -c +"$((MARK + 1))" "$LOG" 2>/dev/null | grep -v '^\s*$' | tail -n 3)
echo "⚠️ Sloth Lobster stopped right after starting:"
echo "$WHY" | sed 's/^/   /'
case "$WHY" in
  *Shiba*|*seal*) echo "   Read what changed, then: cd ~/lobster && python seal.py && sh ~/lobster/scripts/start-sloth-lobster.sh" ;;
  *) echo "   For a full check-up: cd ~/lobster && python doctor.py" ;;
esac
exit 1
