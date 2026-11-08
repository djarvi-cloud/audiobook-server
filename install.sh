#!/bin/sh
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi
if [ ! -f https-url.txt ]; then
  echo "https://audobooks.covertsa.com" > https-url.txt
fi
screen -S audiobooks -X quit 2>/dev/null || true
screen -dmS audiobooks python3 server.py
echo "Started in screen session audiobooks"
echo "Attach with: screen -r audiobooks"
echo "Log: $(pwd)/audiobook.log"
