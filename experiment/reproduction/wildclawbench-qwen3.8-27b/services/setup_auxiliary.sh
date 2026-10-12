#!/usr/bin/env bash
set -euo pipefail

EXPERIMENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TORCH_ENV="${SHAREDSKILL_TORCH_ENV:-/home/tc442/projects/Skill-Creation/experiments/tau-knowledge/skill-evolution/data/embedding/.venv}"
export UV_CACHE_DIR="$EXPERIMENT_DIR/runtime/uv-cache"
AUX_PYTHON="$EXPERIMENT_DIR/runtime/auxiliary-venv/bin/python"
if [[ ! -x "$AUX_PYTHON" ]]; then
    uv venv --python "$TORCH_ENV/bin/python" "$EXPERIMENT_DIR/runtime/auxiliary-venv"
fi
"$AUX_PYTHON" - "$TORCH_ENV" <<'PY'
import sys
import sysconfig
from pathlib import Path
root = Path(sys.argv[1]) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if not root.is_dir():
    raise SystemExit(f"Inherited torch environment is missing: {root}")
(Path(sysconfig.get_paths()["purelib"]) / "shared_torch_environment.pth").write_text(str(root) + "\n")
PY
uv pip install --python "$AUX_PYTHON" --no-deps -r "$EXPERIMENT_DIR/services/auxiliary_requirements.txt"
"$AUX_PYTHON" "$EXPERIMENT_DIR/services/auxiliary_setup.py"
