"""Service locations independent of the source package directory."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.wildclawbench.protocol import REPOSITORY, load_config


def runtime_directory() -> Path:
    return Path(os.environ.get("SHAREDSKILL_RUNTIME_DIR", REPOSITORY / "runtime")).resolve()


def service_config(parser):
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    os.environ["SHAREDSKILL_RUNTIME_DIR"] = config["runtime_dir"]
    return args, config
