"""T2Xpress MCP Server — FastMCP tools over the T2Xpress GUI driver."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
from functools import wraps
from pathlib import Path

from fastmcp import FastMCP

from .config import load_config
from .driver import T2XpressDriver
from .settings import (
    apply_overrides,
    expected_output_suffix,
    load_settings,
    read_raw,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("t2xpress-mcp")

_driver: T2XpressDriver | None = None
_driver_lock = threading.Lock()
_tool_lock = threading.RLock()
_last_play: dict = {}


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _tool_lock:
            return function(*args, **kwargs)

    return call


def get_driver() -> T2XpressDriver:
    global _driver
    if _driver is None:
        with _driver_lock:
            if _driver is None:
                cfg = load_config()
                if not Path(cfg.t2xpress_path).is_file():
                    raise RuntimeError(
                        f"T2Xpress 可执行文件不存在: {cfg.t2xpress_path}（检查 config.json）"
                    )
                _driver = T2XpressDriver(
                    cfg.t2xpress_path,
                    cfg.stop_exe_path,
                    startup_timeout=cfg.startup_timeout_sec,
                    state_timeout=cfg.state_timeout_sec,
                )
    return _driver


def _abs_file(path: str, kind: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f"{kind} 必须使用完整绝对路径: {path}")
    if not p.is_file():
        raise FileNotFoundError(f"{kind} 不存在: {path}")
    return p


def _materialize(settings_path: str, overrides: dict) -> tuple[str, dict]:
    """Write a patched copy of the settings file, or reuse the original."""
    summary = load_settings(settings_path)
    if not any(v is not None for v in overrides.values()):
        return settings_path, summary

    text = apply_overrides(read_raw(settings_path), **overrides)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    out_dir = Path(tempfile.gettempdir()) / "t2xpress_mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{Path(settings_path).stem}-{digest}.xt2"
    if not dest.exists() or dest.read_text(encoding="utf-8", errors="ignore") != text:
        dest.write_text(text, encoding="utf-8", newline="")
    return str(dest), load_settings(dest)


def _play_status(driver: T2XpressDriver, summary: dict, start_result: str) -> dict:
    output = summary.get("output", {})
    mode = output.get("mode")
    basename = output.get("basename")
    if start_result == "generating":
        state = "PLAYING"
    elif start_result == "finished":
        state = "COMPLETED"
    else:
        state = "UNKNOWN"
    return {
        "ok": True,
        "state": state,
        "output_mode": mode,
        "continuous": expected_output_suffix(mode) is None,
        "parameter_status": driver.parameter_status(),
        "config": driver.config_path,
        "outputs": driver.output_files(basename, mode),
    }


# ── tools ────────────────────────────────────────────────────────────────


@mcp.tool()
@_serialized
def play(
    settings_path: str,
    stream: str | None = None,
    ts_files: list[str] | None = None,
    output_mode: str | None = None,
    basename: str | None = None,
    frequency_mhz: float | None = None,
    level_dbm: float | None = None,
    num_super_frames: int | None = None,
    device_serial: int | str | None = None,
    device_port: int | None = None,
) -> dict:
    """Start T2Xpress playout/generation from a .xt2 settings file.

    Loads the settings, optionally repoints PLP TS sources (``stream`` for a
    single PLP or ``ts_files`` for several), switches the output mode
    (rf / t2mi / t2mi_over_asi / iq_int16 / iq_int16_hex), applies RF frequency
    and level, then starts the T2Xpress process.

    T2Xpress -f starts generation by itself, so this call is the playout
    action. For ``rf`` / ``t2mi_over_asi`` the output is continuous and the
    call returns state=PLAYING until stop(). File modes return COMPLETED when
    the job finishes before the first poll.

    stop() first tries the File→Cancel generation command; because WinForms
    menu commands are not always reachable from a background MCP session it
    falls back to closing T2Xpress (which releases the modulator).
    """
    driver = get_driver()
    settings_path = str(_abs_file(settings_path, "设置文件"))

    if stream and ts_files:
        raise ValueError("stream 与 ts_files 只能二选一")
    if stream:
        stream = str(_abs_file(stream, "码流文件"))
        ts_files = [stream]
    if ts_files:
        ts_files = [str(_abs_file(p, "码流文件")) for p in ts_files]
    if frequency_mhz is not None and not (40 <= frequency_mhz <= 1000):
        raise ValueError("frequency_mhz 应在 40–1000 MHz 之间")
    if level_dbm is not None and not (-60 <= level_dbm <= 0):
        raise ValueError("level_dbm 应在 -60–0 dBm 之间")

    if ts_files is not None and sum(1 for p in ts_files) != len(ts_files):
        raise ValueError("ts_files 含空值")

    overrides = {
        "output_mode": output_mode,
        "basename": basename,
        "num_super_frames": num_super_frames,
        "frequency_mhz": frequency_mhz,
        "level_dbm": level_dbm,
        "ts_files": ts_files,
        "device_serial": device_serial,
        "device_port": device_port,
    }
    config_path, summary = _materialize(settings_path, overrides)

    if driver.is_generating():
        raise RuntimeError("T2Xpress 正在生成中；先调用 stop() 再切换任务")

    driver.start(config_path)
    status = driver.parameter_status()
    if status and "not valid" in status.lower():
        raise RuntimeError(f"T2Xpress 认为参数集无效: {status}")

    global _last_play
    _last_play = {
        "settings_path": settings_path,
        "config": config_path,
        "mode": summary.get("output", {}).get("mode"),
        "basename": summary.get("output", {}).get("basename"),
    }

    # `T2Xpress -f` starts generation by itself. Only click Generate as a
    # fallback when the app clearly stayed idle (e.g. no output configured).
    start_result = driver.wait_generation_start(timeout=8)
    if start_result == "finished":
        output = summary.get("output", {})
        suffix = expected_output_suffix(output.get("mode"))
        outputs = driver.output_files(output.get("basename"), output.get("mode"))
        if suffix is None or not outputs:
            start_result = driver.generate()

    result = _play_status(driver, summary, start_result)
    result["settings_path"] = settings_path
    return result


@mcp.tool()
@_serialized
def stop() -> dict:
    """Stop T2Xpress playout/generation (clicks Cancel generation).

    Falls back to closing T2Xpress when the menu command does not land, which
    releases the modulator and always stops RF.
    """
    driver = get_driver()
    if not driver.running:
        return {"ok": True, "state": "NOT_RUNNING"}
    if not driver.is_generating():
        return {"ok": True, "state": "IDLE", "parameter_status": driver.parameter_status()}
    if driver.cancel():
        return {
            "ok": True,
            "state": "STOPPED",
            "method": "menu",
            "parameter_status": driver.parameter_status(),
        }
    # The GUI menu command is not always reachable from a background MCP
    # session. Closing T2Xpress releases the modulator and reliably stops RF.
    closed = driver.close(cancel_first=False)
    return {
        "ok": bool(closed.get("ok")),
        "state": "STOPPED" if closed.get("ok") else "STILL_PLAYING",
        "method": "closed_app",
        "closed": closed,
    }


@mcp.tool()
@_serialized
def get_status() -> dict:
    """Return T2Xpress state: running, generating, parameter validity, outputs."""
    driver = get_driver()
    mode = _last_play.get("mode")
    basename = _last_play.get("basename")
    status = driver.status(basename=basename, mode=mode)
    if status.get("running"):
        if status.get("generating"):
            status["state"] = "PLAYING"
        elif status.get("popups"):
            status["state"] = "ERROR"
        else:
            status["state"] = "IDLE"
    else:
        status["state"] = "NOT_RUNNING"
    status["output_mode"] = mode
    status["settings_path"] = _last_play.get("settings_path")
    return status


