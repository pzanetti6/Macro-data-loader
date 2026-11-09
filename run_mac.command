#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Macro Data Loader"
APP_FILE="$SCRIPT_DIR/app.py"
VENV_DIR="$SCRIPT_DIR/.venv"
CONDA_ENV_DIR="$SCRIPT_DIR/.conda-python311"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
REQ_STAMP="$VENV_DIR/.requirements.installed"
PORT=8534

printf '\n========================================\n'
printf ' %s\n' "$APP_NAME"
printf '========================================\n\n'

version_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)
PY
}

find_python() {
  for cmd in python3.12 python3.13 python3.11 python3.10 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && version_ok "$(command -v "$cmd")"; then
      command -v "$cmd"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN=""
if PYTHON_BIN=$(find_python); then
  echo "Using Python: $($PYTHON_BIN --version 2>&1)"
elif command -v conda >/dev/null 2>&1; then
  echo "Creating/reusing a private Python 3.11 environment..."
  if [ ! -x "$CONDA_ENV_DIR/bin/python" ]; then
    conda create -y -p "$CONDA_ENV_DIR" python=3.11 pip
  fi
  PYTHON_BIN="$CONDA_ENV_DIR/bin/python"
elif command -v brew >/dev/null 2>&1; then
  echo "Installing/reusing Homebrew Python 3.11..."
  brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
  PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
else
  echo "No compatible Python 3.10-3.13 was found."
  echo "Install Python 3.12 from python.org and run this launcher again."
  read "?Press Enter to close..."
  exit 1
fi

if [ -d "$VENV_DIR" ]; then
  if [ ! -x "$VENV_DIR/bin/python" ] || ! version_ok "$VENV_DIR/bin/python"; then
    echo "Removing an incompatible .venv..."
    rm -rf "$VENV_DIR"
  fi
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating the app environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# Install packages only when requirements.txt changed since the last install.
if [ ! -f "$REQ_STAMP" ] || ! cmp -s "$REQ_FILE" "$REQ_STAMP"; then
  echo "Installing required packages..."
  python -m pip install --upgrade pip setuptools wheel
  python -m pip install -r "$REQ_FILE"
  cp "$REQ_FILE" "$REQ_STAMP"
else
  echo "Packages are up to date (delete .venv to force a reinstall)."
fi

# Free the app's port if an earlier copy is still running.
if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    echo "Stopping a previous copy on port $PORT..."
    echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
    sleep 1
  fi
fi

echo
echo "Starting $APP_NAME on http://localhost:$PORT"
echo "To stop it, return to this window and press Ctrl+C."
echo

exec python -m streamlit run "$APP_FILE" \
  --server.port "$PORT" \
  --server.address "localhost" \
  --browser.serverAddress "localhost"
