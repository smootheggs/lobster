#!/data/data/com.termux/files/usr/bin/sh
# Update Sloth Lobster from the newest lobster-core*.zip in Downloads, in one line:
#     sh ~/lobster/scripts/update.sh
#
# Safe by design (the way Home Assistant updates): the new version is unpacked into its own
# folder (~/.lobster-next) and tested there. Your running copy isn't touched unless every test
# passes. Your memory is backed up first, and the previous version is kept, so
#     sh ~/lobster/scripts/update.sh --rollback
# puts it back. Your data, .env and .git are never touched.
#
# It never seals. If sealed files change, it stops before changing anything and shows how to
# read the changes, then one line to apply, seal (your signature) and restart.
# Everything runs inside main(), which the shell reads completely before starting.
main() {
  LOBSTER="$HOME/lobster"
  NEXT="$HOME/.lobster-next"
  PREV="$HOME/.lobster-prev"
  mkdir -p "$LOBSTER/data"

  if [ "$1" = "--rollback" ]; then
    python "$LOBSTER/scripts/swap.py" rollback "$LOBSTER" "$PREV" || return 1
    SEAL=$(cd "$LOBSTER" && python -c "import shiba; print(shiba.seal_problem() or '')")
    if [ -n "$SEAL" ]; then
      echo "🐕 $SEAL"
      echo "   The older version's sealed files are back. Read them, then seal and restart:"
      echo "   cd ~/lobster && python seal.py && pkill -f 'python bot.py'; sleep 3; sh ~/lobster/scripts/start-sloth-lobster.sh"
      return 0
    fi
    restart
    return 0
  fi

  if [ "$1" = "--apply" ]; then
    apply_staged
    return $?
  fi

  Z=$(ls -t "$HOME"/storage/downloads/lobster-core*.zip 2>/dev/null | head -1)
  [ -n "$Z" ] || { echo "🦞 No lobster-core*.zip in Downloads."; return 1; }
  echo "🦞 Using: $Z"
  rm -rf "$NEXT" && mkdir -p "$NEXT"
  python -m zipfile -e "$Z" "$NEXT" || { echo "🦞 Couldn't unpack the zip. Nothing was changed."; return 1; }
  [ -f "$NEXT/lobster/config.py" ] || { echo "🦞 That zip isn't a Sloth Lobster package. Nothing was changed."; return 1; }
  NEW=$(grep '^VERSION' "$NEXT/lobster/config.py" | cut -d'"' -f2)
  OLD=$(grep '^VERSION' "$LOBSTER/config.py" 2>/dev/null | cut -d'"' -f2)
  echo "   installed: ${OLD:-none} → new: $NEW"

  echo "🦞 Testing the new version on its own (a minute or two). Your running copy isn't touched…"
  if ! (cd "$NEXT/lobster" && python test_lobster.py) > "$LOBSTER/data/last-test.log" 2>&1; then
    tail -n 15 "$LOBSTER/data/last-test.log"
    echo "🦞 Tests failed: nothing was changed, still on ${OLD:-the old version}. Send me the lines above (full log: data/last-test.log)."
    return 1
  fi
  tail -n 1 "$LOBSTER/data/last-test.log"
  echo "$NEW" > "$NEXT/tested"

  python "$NEXT/lobster/scripts/swap.py" changes "$NEXT/lobster" "$LOBSTER"
  if [ $? -eq 3 ]; then
    echo "   Nothing has been changed yet: Sloth Lobster keeps running ${OLD:-the old version}."
    echo "   After reading, apply, seal and restart in one line:"
    echo "   sh ~/lobster/scripts/update.sh --apply && cd ~/lobster && python seal.py && pkill -f 'python bot.py'; sleep 3; sh ~/lobster/scripts/start-sloth-lobster.sh"
    return 0
  fi
  apply_staged || return 1
  restart
  echo "🦞 Then save it: cd ~/lobster && git add -A && git commit -m \"Sloth Lobster $NEW\" && git push"
}

apply_staged() {
  [ -f "$NEXT/tested" ] || { echo "🦞 Nothing tested to apply. Run: sh ~/lobster/scripts/update.sh"; return 1; }
  echo "🐘 Backing up your memory first…"
  (cd "$LOBSTER" && python -c "import memory; r = memory.backup(); print('   ' + r['file'] + ': ' + str(r['facts']) + ' facts')") \
    || { echo "🐘 The backup failed, so nothing was changed. Run: cd ~/lobster && python doctor.py"; return 1; }
  python "$NEXT/lobster/scripts/swap.py" apply "$NEXT/lobster" "$LOBSTER" "$PREV" || return 1
  rm -f "$NEXT/tested"
}

restart() {
  if [ "$LOBSTER_NO_RESTART" = "1" ]; then        # the tests use this: never stop the real one
    echo "   (restart skipped)"
    return 0
  fi
  pkill -f "python bot.py"; sleep 3
  sh "$LOBSTER/scripts/start-sloth-lobster.sh"
}

main "$@"
exit $?
