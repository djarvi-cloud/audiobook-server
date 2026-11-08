#!/bin/sh
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi
# Assemble server.py from parts if present
if [ -d parts ] && [ -f parts/p00.txt ]; then
  echo "Assembling server.py from parts..."
  cat parts/p*.txt > server.py
  chmod +x server.py
fi
if [ ! -f https-url.txt ]; then
  echo "https://audobooks.covertsa.com" > https-url.txt
fi
screen -S audiobooks -X quit 2>/dev/null || true
screen -dmS audiobooks python3 server.py
echo "Started in screen session audiobooks"
echo "Attach with: screen -r audiobooks"
