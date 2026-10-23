#!/data/data/com.termux/files/usr/bin/sh
# Update Sloth Lobster from the newest lobster-core*.zip in Downloads, in one line:
#     sh ~/lobster/scripts/update.sh
# It unpacks, tests and restarts. It never seals: if sealed files changed, it stops and tells
# you, because sealing is your signature (python seal.py, after reading the changes).
# Everything runs inside main(), which the shell reads completely before starting, so
# unpacking a new copy of this script mid-run can't confuse it.
main() {
  LOBSTER="$HOME/lobster"
  Z=$(ls -t "$HOME"/storage/downloads/lobster-core*.zip 2>/dev/null | head -1)
  [ -n "$Z" ] || { echo "🦞 No lobster-core*.zip in Downloads."; return 1; }
  NEW=$(unzip -p "$Z" lobster/config.py | grep '^VERSION' | cut -d'"' -f2)
  OLD=$(grep '^VERSION' "$LOBSTER/config.py" 2>/dev/null | cut -d'"' -f2)
  echo "🦞 Using: $Z"
  echo "   installed: ${OLD:-none} → new: $NEW"
  [ "$NEW" = "$OLD" ] && echo "   (same version: unpacking again anyway)"
  cd "$HOME" && unzip -oq "$Z" || { echo "🦞 Unzip failed."; return 1; }
  cd "$LOBSTER" || return 1
  echo "🦞 Running the tests (a minute or two)…"
  if ! python test_lobster.py > data/last-test.log 2>&1; then
    tail -n 15 data/last-test.log
    echo "🦞 Tests failed: not restarting. Send me the lines above (full log: data/last-test.log)."
    return 1
  fi
  tail -n 1 data/last-test.log
  SEAL=$(python -c "import shiba; print(shiba.seal_problem() or '')")
  if [ -n "$SEAL" ]; then
    echo "🐕 $SEAL"
    echo "   Read what changed, then seal and restart yourself:"
    echo "   cd ~/lobster && python seal.py && pkill -f 'python bot.py'; sleep 3; sh ~/lobster/scripts/start-sloth-lobster.sh"
  else
    pkill -f "python bot.py"; sleep 3
    sh "$LOBSTER/scripts/start-sloth-lobster.sh"
  fi
  echo "🦞 Then save it: cd ~/lobster && git add -A && git commit -m \"Sloth Lobster $NEW\" && git push"
}
main "$@"
exit $?
