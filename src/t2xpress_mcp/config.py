"""Configuration loading for the T2Xpress MCP server."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE = r"C:\Program Files (x86)\DekTec\T2Xpress\T2Xpress.exe"


@dataclass(frozen=True)
class Config:
    t2xpress_path: str = DEFAULT_EXE
    stop_exe_path: str = ""
    presets_dir: str = ""
    startup_timeout_sec: float = 60.0
    state_timeout_sec: float = 20.0


def _pick(raw: dict, key: str, default):
    value = raw.get(key, default)
    return default if value in (None, "") else value


def load_config() -> Config:
    candidates = []
    env = os.environ.get("T2XPRESS_MCP_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(PROJECT_ROOT / "config.json")

    raw: dict = {}
    for path in candidates:
        if not path.exists():
            if env and str(path) == env:
                raise ValueError(f"T2XPRESS_MCP_CONFIG 指向的文件不存在: {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件 JSON 解析失败 ({path}): {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"配置文件顶层必须是对象: {path}")
        raw = data
        break

    exe = _pick(raw, "t2xpress_path", DEFAULT_EXE)
    stop = _pick(raw, "stop_exe_path", "")
    if not stop:
        stop = str(Path(exe).with_name("StopT2Generation.exe"))
    return Config(
        t2xpress_path=str(exe),
        stop_exe_path=str(stop),
        presets_dir=str(_pick(raw, "presets_dir", "")),
        startup_timeout_sec=float(_pick(raw, "startup_timeout_sec", 60)),
        state_timeout_sec=float(_pick(raw, "state_timeout_sec", 20)),
    )
