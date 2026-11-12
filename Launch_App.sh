#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_CMD=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PYTHON_CMD="$candidate"
    break
  fi
done
if [ -z "$PYTHON_CMD" ]; then
  echo "Python 3.9 or newer is required."
  exit 1
fi

"$PYTHON_CMD" scripts/local_server.py &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 2
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "Local server failed to start."
  exit 1
fi

URL="http://127.0.0.1:8765/index.html?v=3.91"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true
else
  echo "Open: $URL"
fi

echo "WT Roster Local Server PID: $SERVER_PID"
echo "Press Ctrl+C to stop the server."
wait "$SERVER_PID"
