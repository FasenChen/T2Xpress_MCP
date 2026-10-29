"""Drive the T2Xpress GUI.

T2Xpress has no remote-control API. It only accepts ``-f <settings>`` on the
command line; starting/stopping generation must go through its WinForms UI.
This module wraps that with pylwinauto's UIA backend and exposes a small,
deterministic state machine:

    idle      Generate enabled,  Cancel disabled
    generating Generate disabled, Cancel enabled
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

import win32api
import win32con
import win32process
from pywinauto import Application, Desktop

MAIN_TITLE_RE = re.compile(r"^T2Xpress - ")
TITLE_RE = r"T2Xpress - .*"
BUTTON_GENERATE = "toolStripButton4"
BUTTON_CANCEL = "toolStripButton2"
STATUS_BAR_ID = "statusStrip1"
TOOLBAR_ID = "toolStrip1"

class T2XpressError(RuntimeError):
    pass


def _start_detached_process(command: list[str], cwd: str) -> int:
    # WMI keeps the player outside the MCP client's kill-on-close Job Object.
    script = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow = [uint16]1}
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $data.command
    CurrentDirectory = $data.cwd
    ProcessStartupInformation = $startup
} | Select-Object ReturnValue, ProcessId | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        input=json.dumps({"command": subprocess.list2cmdline(command), "cwd": cwd}),
        capture_output=True, text=True, encoding="utf-8", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise T2XpressError(f"PowerShell 创建进程失败: {completed.stderr.strip()}")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or type(result.get("ReturnValue")) is not int or result["ReturnValue"] != 0:
        raise T2XpressError(f"Win32_Process.Create 失败: {result!r}")
    pid = result.get("ProcessId")
    if type(pid) is not int or pid <= 0:
        raise T2XpressError(f"Win32_Process.Create 未返回有效进程 ID: {result!r}")
    return pid


def _click(control) -> None:
    """Click via UIA Invoke when possible, fall back to a real mouse click."""
    try:
        control.iface_invoke.Invoke()
        return
    except Exception:
        pass
    control.click_input()


class T2XpressDriver:
    def __init__(
        self,
        exe_path: str,
        stop_exe_path: str | None = None,
        *,
        startup_timeout: float = 60.0,
        state_timeout: float = 15.0,
    ) -> None:
        self.exe_path = exe_path
        self.stop_exe_path = stop_exe_path or str(Path(exe_path).with_name("StopT2Generation.exe"))
        self.startup_timeout = startup_timeout
        self.state_timeout = state_timeout
        self._process_handle = None
        self._app: Application | None = None
        self._dlg = None
        self._pid: int | None = None
        self.config_path: str | None = None

    # ── discovery / lifecycle ────────────────────────────────────────────

    def find_window(self, timeout: float = 0.0) -> tuple[int, object] | None:
        """Return (pid, window wrapper) for a T2Xpress main window."""
        deadline = time.time() + timeout
        while True:
            for w in Desktop(backend="uia").windows():
                try:
                    if MAIN_TITLE_RE.match(w.window_text() or ""):
                        return w.process_id(), w
                except Exception:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def attach(self) -> bool:
        found = self.find_window()
        if not found:
            return False
        pid, _ = found
        self._pid = pid
        self._app = Application(backend="uia").connect(process=pid, timeout=10)
        self._dlg = self._app.window(title_re=TITLE_RE)
        return True

    @property
    def running(self) -> bool:
        if self._process_handle is not None and win32process.GetExitCodeProcess(self._process_handle) != win32con.STILL_ACTIVE:
            return False
        try:
            found = self.find_window()
        except Exception:
            return False
        if not found:
            return False
        pid, _ = found
        if self._pid != pid:
            # Attach to an instance started by a previous MCP session.
            self._pid = pid
            self._dlg = None
            self._app = None
            try:
                self._app = Application(backend="uia").connect(process=pid, timeout=10)
                self._dlg = self._app.window(title_re=TITLE_RE)
            except Exception:
                return False
        return True

    def start(self, config_path: str | None = None) -> None:
        if config_path is not None:
            # T2Xpress resolves -f relative to its own install directory.
            config_path = str(Path(config_path).resolve())
        if self.running:
            if config_path is not None and self.config_path != config_path:
                if not self.close()["ok"]:
                    raise T2XpressError("T2Xpress 未能关闭，不能启动新的播出")
            else:
                return
        self._reset()
        args = [self.exe_path]
        if config_path:
            args += ["-f", config_path]
        launched_pid = _start_detached_process(args, cwd=str(Path(self.exe_path).parent))
        self._process_handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_TERMINATE, False, launched_pid,
        )
        try:
            found = self.find_window(self.startup_timeout)
            if not found:
                code = win32process.GetExitCodeProcess(self._process_handle)
                popups = self._popups_for_pid(launched_pid)
                detail = ("；弹窗: " + " || ".join(popups)) if popups else ""
                raise T2XpressError(
                    f"T2Xpress 启动后未出现主窗口（退出码 {code}）{detail}。"
                    "常见原因：设置文件路径无效或被改写。"
                )
            pid, _ = found
            self._pid = pid
            self._app = Application(backend="uia").connect(process=pid, timeout=15)
            self._dlg = self._app.window(title_re=TITLE_RE)
            # WinForms does not always report "ready" to UIA; poll the controls instead.
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    self._state()
                    break
                except Exception:
                    time.sleep(0.25)
            else:
                raise T2XpressError("T2Xpress 窗口已出现，但工具栏控件不可用")
            self.config_path = config_path
        except BaseException:
            try:
                if win32process.GetExitCodeProcess(self._process_handle) == win32con.STILL_ACTIVE:
                    win32api.TerminateProcess(self._process_handle, 1)
            finally:
                self._reset()
            raise
        # NOTE: `-f` starts generation immediately (observed v2.9.0), so we must
        # not wait for idle here — a long RF run never becomes idle.

    def close(self, cancel_first: bool = True) -> dict:
        if not self.running:
            self._reset()
            return {"ok": True, "status": "not_running"}
        try:
            if cancel_first and self.is_generating():
                self.cancel()
        except Exception:
            pass
        pid = self._pid
        try:
            self._dlg.close()
        except Exception:
            pass
        # A running generation may raise a confirmation dialog.
        time.sleep(1.0)
        try:
            self.dismiss_popups()
        except Exception:
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            if not self.running:
                break
            time.sleep(0.25)
        if self.running and pid:
            # Last resort: make sure the device is released.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
            time.sleep(1.5)
        running = self.running
        self._reset()
        return {"ok": not running, "status": "closed" if not running else "still_running", "pid": pid}

    def _reset(self) -> None:
        if self._process_handle is not None:
            self._process_handle.Close()
            self._process_handle = None
        self._app = None
        self._dlg = None
        self._pid = None
        self.config_path = None

    # ── UI primitives ────────────────────────────────────────────────────

    def _require(self):
        if self._dlg is None and not self.attach():
            raise T2XpressError("T2Xpress 未运行")
        return self._dlg

    def _refresh(self) -> bool:
        """Re-resolve the main window after menu interactions invalidate handles."""
        if self._pid is None:
            return self.attach()
        try:
            self._app = Application(backend="uia").connect(process=self._pid, timeout=5)
            self._dlg = self._app.window(title_re=TITLE_RE)
            self._dlg.wait("exists", timeout=3)
            return True
        except Exception:
            return False

    def _button(self, name: str):
        return self._require().child_window(title=name, control_type="Button")

    def _focus(self) -> None:
        """menu_select only lands when the WinForms window has focus."""
        try:
            dlg = self._require()
            dlg.restore()
            dlg.set_focus()
        except Exception:
            pass

    def _state(self) -> tuple[bool, bool]:
        gen = self._button(BUTTON_GENERATE).is_enabled()
        cancel = self._button(BUTTON_CANCEL).is_enabled()
        return gen, cancel

    def _state_safe(self) -> tuple[bool, bool] | None:
        try:
            return self._state()
        except Exception:
            if self._refresh():
                try:
                    return self._state()
                except Exception:
                    return None
            return None

    def is_generating(self) -> bool:
        state = self._state_safe()
        return bool(state and state[1] and not state[0])

    def is_idle(self) -> bool:
        state = self._state_safe()
        return bool(state and state[0] and not state[1])

    def wait_idle(self, timeout: float | None = None) -> None:
        deadline = time.time() + (timeout or self.state_timeout)
        stable = 0
        while time.time() < deadline:
            if self.is_idle():
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            time.sleep(0.2)
        raise T2XpressError("T2Xpress 未进入空闲状态（可能仍在生成或界面无响应）")

    def wait_generation_end(self, timeout: float | None = None) -> bool:
        deadline = time.time() + (timeout or self.state_timeout)
        while time.time() < deadline:
            if self.is_idle():
                return True
            time.sleep(0.2)
        return False

    def parameter_status(self) -> str:
        for attempt in (0, 1):
            try:
                dlg = self._require()
                bar = dlg.child_window(auto_id=STATUS_BAR_ID, control_type="StatusBar")
                parts = [c.window_text() for c in bar.children() if c.window_text()]
                return parts[-1] if parts else ""
            except Exception:
                if attempt == 0 and self._refresh():
                    continue
                return ""
        return ""

    def _popups_for_pid(self, pid: int | None) -> list[str]:
        if pid is None:
            return []
        out = []
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    if w.process_id() != pid:
                        continue
                    text = w.window_text() or ""
                    if MAIN_TITLE_RE.match(text) or not text:
                        continue
                    children = [c.window_text() for c in w.children() if c.window_text()]
                    out.append(" | ".join([text, *children])[:500])
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def popups(self) -> list[str]:
        if self._pid is None:
            return []
        out = []
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    if w.process_id() != self._pid:
                        continue
                    text = w.window_text() or ""
                    if MAIN_TITLE_RE.match(text):
                        continue
                    if not text:
                        continue
                    children = [c.window_text() for c in w.children() if c.window_text()]
                    out.append(" | ".join([text, *children])[:500])
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def dismiss_popups(self) -> list[str]:
        texts = self.popups()
        if not texts:
            return []
        try:
            for w in Desktop(backend="uia").windows():
                if w.process_id() != self._pid:
                    continue
                if MAIN_TITLE_RE.match(w.window_text() or ""):
                    continue
                for label in ("确定", "OK", "OK ", "Yes", "是", "否", "No"):
                    try:
                        btn = w.child_window(title=label, control_type="Button")
                        if btn.exists():
                            _click(btn)
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        return texts

    # ── actions ──────────────────────────────────────────────────────────

    def wait_generation_start(self, timeout: float = 5.0) -> str:
        """Classify what `-f` did: still playing, or already finished."""
        deadline = time.time() + timeout
        saw_generating = False
        while time.time() < deadline:
            popups = self.dismiss_popups()
            if popups:
                raise T2XpressError("T2Xpress 弹出错误对话框: " + " || ".join(popups))
            if self.is_generating():
                saw_generating = True
                break
            if self.is_idle():
                break
            time.sleep(0.1)
        if saw_generating or self.is_generating():
            return "generating"
        return "finished" if self.is_idle() else "unknown"

    def generate(self) -> str:
        """Click Generate. Returns 'generating' or 'finished'."""
        self._require()
        if self.is_generating():
            raise T2XpressError("已在生成中，请先 stop()")
        self._focus()
        try:
            self._dlg.menu_select("File->Generate output")
        except Exception:
            _click(self._button(BUTTON_GENERATE))
        deadline = time.time() + 5
        saw_generating = False
        while time.time() < deadline:
            popups = self.dismiss_popups()
            if popups:
                raise T2XpressError("T2Xpress 弹出错误对话框: " + " || ".join(popups))
            if self.is_generating():
                saw_generating = True
                break
            time.sleep(0.1)
        if saw_generating:
            return "generating"
        # A short job may have completed between two polls.
        if self.is_idle():
            return "finished"
        return "unknown"

    def cancel(self, timeout: float = 6.0) -> bool:
        """Best-effort graceful cancel via the File menu.

        The WinForms menu command only lands when the window owns the
        foreground; in a headless/MCP session it is not always reliable, so
        callers should fall back to closing the app (which always stops the
        modulator because the process releases the device).
        """
        if not self.running:
            return True
        if not self.is_generating():
            return True
        try:
            self._focus()
            time.sleep(0.4)
            self._dlg.menu_select("File->Cancel generation")
            time.sleep(0.8)
            self._refresh()
        except Exception:
            pass
        return self.wait_generation_end(timeout=timeout)

    # ── diagnostics ──────────────────────────────────────────────────────

    def output_files(self, basename: str | None, mode: str | None) -> list[dict]:
        if not basename:
            return []
        matches = glob.glob(basename + "*")
        return [
            {"path": p, "size": os.path.getsize(p)}
            for p in sorted(matches)
            if os.path.isfile(p) and not p.lower().endswith(".xt2")
        ]

    def status(self, basename: str | None = None, mode: str | None = None) -> dict:
        if not self.running:
            return {"running": False, "generating": False}
        try:
            gen, cancel = self._state()
        except Exception:
            return {"running": True, "generating": False, "ui": "unavailable"}
        return {
            "running": True,
            "generating": bool(cancel and not gen),
            "idle": bool(gen and not cancel),
            "parameter_status": self.parameter_status(),
            "config": self.config_path,
            "outputs": self.output_files(basename, mode),
            "popups": self.popups(),
        }
