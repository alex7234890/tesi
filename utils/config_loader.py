from __future__ import annotations

import os
from typing import Any, Dict, Optional

import yaml


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(mode_config_path: Optional[str] = None) -> Dict[str, Any]:
    base_path = os.path.join(os.path.dirname(__file__), "..", "config", "base.yaml")
    base_path = os.path.normpath(base_path)

    with open(base_path, "r", encoding="utf-8") as f:
        config: Dict[str, Any] = yaml.safe_load(f)

    if mode_config_path and os.path.isfile(mode_config_path):
        with open(mode_config_path, "r", encoding="utf-8") as f:
            overrides = yaml.safe_load(f)
        if overrides:
            config = _deep_merge(config, overrides)

    return config
