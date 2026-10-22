#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo 'Usage: setup_auxiliary.sh CONFIG_PATH TORCH_ENV' >&2
    exit 2
fi
SERVICE_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_DIR="$(cd -- "$SERVICE_SOURCE_DIR/../../.." && pwd)"
CONFIG_PATH="$(realpath -- "$1")"
TORCH_ENV="$(realpath -- "$2")"
cd -- "$REPOSITORY_DIR"
EXPERIMENT_RUNTIME="$("$REPOSITORY_DIR/.venv/bin/python" -B - "$CONFIG_PATH" <<'PY'
import sys
from pathlib import Path
from scripts.wildclawbench.protocol import load_config
print(load_config(Path(sys.argv[1]))['runtime_dir'])
PY
)"
export UV_CACHE_DIR="$EXPERIMENT_RUNTIME/uv-cache"
AUX_PYTHON="$EXPERIMENT_RUNTIME/auxiliary-venv/bin/python"
if [[ ! -x "$AUX_PYTHON" ]]; then
    uv venv --python "$TORCH_ENV/bin/python" "$EXPERIMENT_RUNTIME/auxiliary-venv"
fi
"$AUX_PYTHON" - "$TORCH_ENV" <<'PY'
import sys
import sysconfig
from pathlib import Path
root = Path(sys.argv[1]) / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
if not root.is_dir():
    raise SystemExit(f'Inherited torch environment is missing: {root}')
(Path(sysconfig.get_paths()['purelib']) / 'shared_torch_environment.pth').write_text(str(root) + '\n')
PY
uv pip install --python "$AUX_PYTHON" --no-deps -r "$SERVICE_SOURCE_DIR/auxiliary_requirements.txt"
"$AUX_PYTHON" -B -m scripts.wildclawbench.services.auxiliary_setup --config "$CONFIG_PATH"
