import json
import subprocess
from unittest.mock import Mock

import pytest

from t2xpress_mcp import driver


def test_detached_launch_passes_paths_as_data(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess(
        [], 0, stdout='{"ReturnValue":0,"ProcessId":12345}', stderr="",
    ))
    monkeypatch.setattr(driver.subprocess, "run", run)
    command = [r"C:\测试 files\$(literal)&\T2Xpress.exe", "-f", r"D:\配置 files\test.xt2"]
    cwd = r"C:\测试 files\$(literal)&"

    assert driver._start_detached_process(command, cwd) == 12345
    args, kwargs = run.call_args
    assert args[0][:6] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command"]
    assert command[0] not in args[0][-1]
    assert json.loads(kwargs["input"]) == {"command": subprocess.list2cmdline(command), "cwd": cwd}
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert kwargs["timeout"] == 15


@pytest.mark.parametrize("exit_code,stdout,stderr", [
    (1, "", "WMI unavailable"),
    (0, '{"ReturnValue":2}', ""),
    (0, '{"ReturnValue":false,"ProcessId":123}', ""),
    (0, '{"ReturnValue":0,"ProcessId":0}', ""),
    (0, '{"ReturnValue":0,"ProcessId":true}', ""),
    (0, "[]", ""),
    (0, "invalid json", ""),
])
def test_detached_launch_rejects_failed_or_invalid_result(monkeypatch, exit_code, stdout, stderr):
    monkeypatch.setattr(driver.subprocess, "run", Mock(return_value=subprocess.CompletedProcess(
        [], exit_code, stdout=stdout, stderr=stderr,
    )))
    with pytest.raises((driver.T2XpressError, ValueError)):
        driver._start_detached_process(["T2Xpress.exe"], ".")


@pytest.mark.parametrize("exit_code", [driver.win32con.STILL_ACTIVE, 42])
def test_startup_timeout_releases_only_the_launched_process(monkeypatch, exit_code):
    handle = Mock()
    start = Mock(return_value=12345)
    terminate = Mock()
    monkeypatch.setattr(driver, "_start_detached_process", start)
    monkeypatch.setattr(driver.win32api, "OpenProcess", Mock(return_value=handle))
    monkeypatch.setattr(driver.win32process, "GetExitCodeProcess", Mock(return_value=exit_code))
    monkeypatch.setattr(driver.win32api, "TerminateProcess", terminate)
    instance = driver.T2XpressDriver(r"C:\Program Files\T2Xpress.exe")
    monkeypatch.setattr(instance, "find_window", Mock(return_value=None))
    monkeypatch.setattr(instance, "_popups_for_pid", Mock(return_value=[]))

    with pytest.raises(driver.T2XpressError, match="启动后未出现主窗口"):
        instance.start(r"D:\settings.xt2")

    start.assert_called_once_with(
        [r"C:\Program Files\T2Xpress.exe", "-f", r"D:\settings.xt2"], cwd=r"C:\Program Files",
    )
    if exit_code == driver.win32con.STILL_ACTIVE:
        terminate.assert_called_once_with(handle, 1)
    else:
        terminate.assert_not_called()
    handle.Close.assert_called_once()
    assert instance._process_handle is None


def test_uia_startup_failure_cleans_up_launched_process(monkeypatch):
    handle = Mock()
    terminate = Mock()
    monkeypatch.setattr(driver, "_start_detached_process", Mock(return_value=12345))
    monkeypatch.setattr(driver.win32api, "OpenProcess", Mock(return_value=handle))
    monkeypatch.setattr(driver.win32process, "GetExitCodeProcess", Mock(return_value=driver.win32con.STILL_ACTIVE))
    monkeypatch.setattr(driver.win32api, "TerminateProcess", terminate)
    application = Mock()
    application.connect.side_effect = RuntimeError("UIA unavailable")
    monkeypatch.setattr(driver, "Application", Mock(return_value=application))
    instance = driver.T2XpressDriver(r"C:\Program Files\T2Xpress.exe")
    monkeypatch.setattr(instance, "find_window", Mock(side_effect=[None, (12345, Mock())]))

    with pytest.raises(RuntimeError, match="UIA unavailable"):
        instance.start(r"D:\settings.xt2")

    terminate.assert_called_once_with(handle, 1)
    handle.Close.assert_called_once()
    assert instance._process_handle is None
