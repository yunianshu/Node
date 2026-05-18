#!/usr/bin/env python3
"""小说项目配置读取。"""
from __future__ import annotations

import copy
import json
from pathlib import Path


DEFAULT_CONFIG = {
    "total_chapters": 2000,
    "model": "MiniMax-M2.7-highspeed",
    "mmx_path": "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs",
    "api_qps": 2.0,
    "writer": {
        "max_tokens": 8192,
        "temperature": 0.7,
        "max_retries": 3,
        "retry_delay": 5.0,
    },
    "reviewer": {
        "max_tokens": 4096,
        "temperature": 0.3,
        "min_score": 7.0,
    },
    "planner": {
        "max_tokens": 8192,
        "temperature": 0.5,
        "parallel_agents": 2,
    },
    "coordinator": {
        "batch_size": 40,
        "num_workers": 2,
        "review_workers": 2,
        "pause_between_batches": 3.0,
        "push_interval_seconds": 120,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(project_dir: Path) -> dict:
    config_file = project_dir / "config.json"
    if not config_file.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)
    return deep_merge(DEFAULT_CONFIG, data)
