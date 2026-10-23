#!/data/data/com.termux/files/usr/bin/sh
# Home-screen button (Termux:Widget): starts Sloth Lobster if needed, then opens the app on this tablet.
sh "$HOME/lobster/scripts/start-sloth-lobster.sh"
sleep 3
termux-open-url "https://127.0.0.1:8787"
