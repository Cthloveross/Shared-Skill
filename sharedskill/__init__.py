"""Public package surface for sharedskill.

Keep package import lightweight: callers that only need metadata or a single
symbol should not pull in the whole proxy/server stack at import time.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api_server import SharedSkillAPIServer
    from .config import SharedSkillConfig
    from .config_store import ConfigStore
    from .launcher import SharedSkillLauncher
    from .skill_manager import SkillManager

__all__ = [
    "SharedSkillConfig",
    "ConfigStore",
    "SharedSkillAPIServer",
    "SkillManager",
    "SharedSkillLauncher",
]


_EXPORT_MAP = {
    "SharedSkillConfig": ("sharedskill.config", "SharedSkillConfig"),
    "ConfigStore": ("sharedskill.config_store", "ConfigStore"),
    "SharedSkillAPIServer": ("sharedskill.api_server", "SharedSkillAPIServer"),
    "SkillManager": ("sharedskill.skill_manager", "SkillManager"),
    "SharedSkillLauncher": ("sharedskill.launcher", "SharedSkillLauncher"),
}


def __getattr__(name: str):
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module 'sharedskill' has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
