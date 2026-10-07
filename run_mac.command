#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Macro Data Loader v24"
APP_FILE="$SCRIPT_DIR/app.py"
VENV_DIR="$SCRIPT_DIR/.venv"
CONDA_ENV_DIR="$SCRIPT_DIR/.conda-python311"
PORT=8534

printf '\n========================================\n'
printf ' %s\n' "$APP_NAME"
printf ' Latest UI: macro + ECB rates/yields\n'
printf '========================================\n\n'
echo "Launching app from:"
echo "$APP_FILE"
echo

version_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)
PY
}

find_python() {
  for cmd in python3.12 python3.11 python3.10 python3 python; do
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
  echo "Creating/reusing private Python 3.11 environment..."
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
  echo "Please install Python 3.11 and run this launcher again."
  read "?Press Enter to close..."
  exit 1
fi

if [ -d "$VENV_DIR" ]; then
  if [ ! -x "$VENV_DIR/bin/python" ] || ! version_ok "$VENV_DIR/bin/python"; then
    echo "Removing incompatible previous .venv..."
    rm -rf "$VENV_DIR"
  fi
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating isolated app environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "Installing/updating required packages..."
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "$SCRIPT_DIR/requirements.txt"

# Dedicated v9 port avoids reopening an older Streamlit app on the default 8501.
# If this exact v9 port is already occupied, terminate that stale listener first.
if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    echo "Stopping stale EA Macro Updater process on port $PORT..."
    echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
    sleep 1
  fi
fi

echo
echo "Starting Macro Data Loader v24 on http://localhost:$PORT"
echo "This launcher uses the app.py in THIS folder only."
echo "To stop it, return to this Terminal window and press Ctrl+C."
echo

exec python -m streamlit run "$APP_FILE" \
  --server.port "$PORT" \
  --server.address "localhost" \
  --browser.serverAddress "localhost"
