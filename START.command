#!/bin/bash
# CryptoAPI — double-click to launch
# Place this file in your sara_project folder and double-click it in Finder.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Find Python 3
PYTHON=""
for candidate in python3 /usr/local/bin/python3 /usr/bin/python3 python; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Python not found" message "Please install Python 3 from python.org" as critical'
  exit 1
fi

# Check launcher.py exists
if [ ! -f "$DIR/launcher.py" ]; then
  osascript -e 'display alert "launcher.py not found" message "Make sure launcher.py is in the same folder as this script." as critical'
  exit 1
fi

# Install deps silently if needed
"$PYTHON" -m pip install flask flask-cors requests --quiet --break-system-packages 2>/dev/null || \
"$PYTHON" -m pip install flask flask-cors requests --quiet 2>/dev/null

# Run launcher
"$PYTHON" "$DIR/launcher.py"
