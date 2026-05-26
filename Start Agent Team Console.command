#!/bin/zsh
set -e

cd "$(dirname "$0")"

HOST="127.0.0.1"
PORT="8765"
URL="http://${HOST}:${PORT}"

echo "Agent Team Console"
echo "Project: $(pwd)"
echo "URL: ${URL}"
echo

if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "Could not find python3 or python."
  echo "Install Python 3.11+ and try again."
  read "reply?Press Enter to close..."
  exit 1
fi

echo "Python: ${PYTHON}"

if ! "${PYTHON}" - <<'PY' >/dev/null 2>&1
import fastapi
import uvicorn
PY
then
  echo
  echo "UI dependencies are missing for this Python."
  echo "Run this once from the project folder:"
  echo "  ${PYTHON} -m pip install -e '.[ui]'"
  echo
  read "reply?Press Enter to close..."
  exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -ti tcp:${PORT} >/dev/null 2>&1; then
  echo
  echo "Port ${PORT} is already in use. Opening the existing console."
  open "${URL}" >/dev/null 2>&1 || true
  read "reply?Press Enter to close..."
  exit 0
fi

echo
echo "Starting server. Keep this Terminal window open while using the console."
echo "Press Control-C in this window to stop it."
echo

(sleep 2; open "${URL}" >/dev/null 2>&1 || true) &

export PYTHONPATH="src"
exec "${PYTHON}" -m agent_orchestrator.cli ui --host "${HOST}" --port "${PORT}"
