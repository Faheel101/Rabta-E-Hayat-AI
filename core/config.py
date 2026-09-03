"""Loader for config/network.yaml.

Every engine reads its domain parameters through this module so that a clinical
or logistical assumption exists in exactly one place (spec §18).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parents[1]
NETWORK_CONFIG_PATH = BASE_DIR / "config" / "network.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict:
    with NETWORK_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    if not isinstance(config, dict):
        raise ValueError(f"{NETWORK_CONFIG_PATH} did not parse to a mapping.")

    return config


def get(path: str, default: Any = None) -> Any:
    """Read a dotted path out of the config, e.g. get("risk.horizon_days")."""

    node: Any = load_config()

    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default

        node = node[part]

    return node


def require(path: str) -> Any:
    sentinel = object()
    value = get(path, sentinel)

    if value is sentinel:
        raise KeyError(f"Missing required config key: {path}")

    return value


SEED = int(require("seed"))
HISTORY_DAYS = int(require("history_days"))
