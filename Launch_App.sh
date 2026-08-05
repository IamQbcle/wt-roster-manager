#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then PYTHON_CMD="python3"; elif command -v python >/dev/null 2>&1; then PYTHON_CMD="python"; fi
if [ -z "$PYTHON_CMD" ]; then echo "Python 3.9+ is required."; exit 1; fi
$PYTHON_CMD - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3,9) else 1)
PY
if [ $? -ne 0 ]; then echo "Python 3.9+ is required."; exit 1; fi
$PYTHON_CMD scripts/local_server.py &
SERVER_PID=$!
sleep 2
URL="http://127.0.0.1:8765/index.html?v=3.83"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true; else echo "Open: $URL"; fi
echo "WT Roster Local Server PID: $SERVER_PID"
echo "Press Ctrl+C to stop the server."
wait $SERVER_PID
