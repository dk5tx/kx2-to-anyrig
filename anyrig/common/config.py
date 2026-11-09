"""Configuration loading: YAML file deep-merged onto built-in defaults."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

LOCAL_DEFAULTS: dict[str, Any] = {
    "remote": {"host": None, "port": 7355},
    "kx2": {
        "port": "/dev/ttyUSB0",
        "baud": 38400,
        "k31": True,
        # poll intervals in seconds per priority class
        "intervals": {"fast": 0.08, "normal": 0.5, "slow": 2.0, "ai": 5.0},
        "query_timeout": 0.3,
        "gap": 0.002,
    },
    "wintest": {
        "cat_pty_link": "/tmp/anyrig-com1",
        "winkeyer_pty_link": "/tmp/anyrig-com2",
        "traffic_log": "anyrig-wintest.log",
    },
    "router": {
        # where commands from Win-Test go that the K3 emulation does not handle itself:
        # "kx2" (local-only mode, behaves like a directly connected KX2) or "remote"
        "default": "remote",
        "ignore": ["KS"],
        "to_both": ["RC"],
        "translate": {},
        "block": ["TX", "RX", "PS0", "SR", "KY"],
        "prefix_remote": "@",
    },
    "kx2_keys": {"apf": "beat_cancel_toggle"},
    "audio": {"port": 7356, "jitter_ms": 40, "sink": "default", "af_curve": "log"},
    "webui": {"host": "127.0.0.1", "port": 8073},
    "log": {"level": "INFO", "file": None},
}

REMOTE_DEFAULTS: dict[str, Any] = {
    "listen": {"host": "0.0.0.0", "port": 7355},
    "rig": {"profile": "profiles/ts590sg.yaml", "port": "/dev/ttyUSB1", "baud": 115200},
    "cw": {"method": "cat"},
    "keyline": {"port": "/dev/ttyUSB0", "key": "dtr", "ptt": "rts"},
    "audio": {"source": "hw:CARD=CODEC", "port": 7356, "bitrate": 48000, "frame_ms": 10},
    "safety": {"link_timeout_ms": 1000, "tx_timeout_s": 60, "max_power_w": 100},
    "log": {"level": "INFO", "file": None},
}


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: override merged recursively onto base."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(path: str | Path | None, defaults: dict) -> dict:
    """Load a YAML config file (may be None) on top of defaults."""
    data: dict = {}
    if path:
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
    return deep_merge(defaults, data)
