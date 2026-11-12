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

if ! "$PYTHON_CMD" -c 'import requests' >/dev/null 2>&1; then
  echo "Installing Python dependency: requests"
  if ! "$PYTHON_CMD" -m pip install requests; then
    echo "Failed to install requests. Install it for $PYTHON_CMD and retry."
    exit 1
  fi
fi

mkdir -p data
"$PYTHON_CMD" scripts/update_from_api.py --workers 24 --fetch-wiki-names

echo SUCCESS
