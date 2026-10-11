#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${SHAREDSKILL_VENV_DIR:-$ROOT_DIR/.venv}"
INSTALL_EXTRAS="${SHAREDSKILL_INSTALL_EXTRAS:-evolve,sharing,server}"
RUN_SETUP=0
RUN_START=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--venv-dir PATH] [--python BIN] [--extras LIST] [--run-setup] [--run-start]

Installs sharedskill from the current repository checkout into a local virtualenv.

Examples:
  bash scripts/install_sharedskill.sh
  bash scripts/install_sharedskill.sh --venv-dir ~/.venvs/sharedskill --run-setup
  bash scripts/install_sharedskill.sh --extras all --run-setup --run-start

Default install command:
  python -m pip install -e ".[evolve,sharing,server]"

After install you can run:
  sharedskill setup
  sharedskill start

If you also plan to use the agent evolve server, install OpenClaw separately:
  npm install -g openclaw
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv-dir)
      VENV_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --extras)
      INSTALL_EXTRAS="$2"
      shift 2
      ;;
    --run-setup)
      RUN_SETUP=1
      shift
      ;;
    --run-start)
      RUN_START=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

echo "[install_sharedskill] repo root: $ROOT_DIR"
echo "[install_sharedskill] python: $PYTHON_BIN"
echo "[install_sharedskill] venv: $VENV_DIR"
echo "[install_sharedskill] extras: $INSTALL_EXTRAS"

cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install -U pip
python -m pip install -e ".[${INSTALL_EXTRAS}]"

echo
echo "[install_sharedskill] install complete"
echo "[install_sharedskill] activate with:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "[install_sharedskill] next steps:"
echo "  sharedskill setup"
echo "  sharedskill start"

if [[ "$RUN_SETUP" -eq 1 ]]; then
  echo
  echo "[install_sharedskill] running: sharedskill setup"
  sharedskill setup
fi

if [[ "$RUN_START" -eq 1 ]]; then
  echo
  echo "[install_sharedskill] running: sharedskill start"
  sharedskill start
fi
