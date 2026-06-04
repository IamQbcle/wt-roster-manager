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
$PYTHON_CMD -m pip install requests
mkdir -p data
$PYTHON_CMD scripts/update_from_api.py --workers 24 --fetch-wiki-names
echo SUCCESS
