"""Exercise real Windows/UIA lifecycle across fresh MCP sessions without RF."""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA and Job Objects")


def test_play_status_stop_and_switch_across_mcp_sessions(tmp_path):
    import win32api
    import win32con
    import win32gui
    import win32process
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    title = "T2Xpress - lifecycle-" + tmp_path.name
    folder = tmp_path / "测试 files & $(literal)"
    folder.mkdir()
    exe = folder / "T2Xpress.exe"
    source = """
using System;
using System.Windows.Forms;
public class Player {
    [STAThread] public static void Main(string[] args) {
        Form form = new Form { Text = TITLE, Width = 500 };
        Button generate = new Button { Text = "toolStripButton4", Enabled = false, Top = 50, Width = 180 };
        Button cancel = new Button { Text = "toolStripButton2", Enabled = true, Top = 90, Width = 180 };
        MenuStrip menu = new MenuStrip();
        ToolStripMenuItem file = new ToolStripMenuItem("File");
        ToolStripMenuItem stop = new ToolStripMenuItem("Cancel generation");
        stop.Click += (sender, e) => { generate.Enabled = true; cancel.Enabled = false; };
        file.DropDownItems.Add(stop); menu.Items.Add(file);
        StatusStrip status = new StatusStrip { Name = "statusStrip1" };
        status.Items.Add(new ToolStripStatusLabel("Parameter set is valid"));
        form.Controls.Add(generate); form.Controls.Add(cancel);
        form.Controls.Add(menu); form.Controls.Add(status); form.MainMenuStrip = menu;
        Application.Run(form);
    }
}
""".replace("TITLE", json.dumps(title))
    compile_script = """
$ErrorActionPreference = 'Stop'
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -TypeDefinition $data.source -ReferencedAssemblies System.Windows.Forms,System.Drawing -OutputAssembly $data.exe -OutputType WindowsApplication
"""
    compiled = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", compile_script],
        input=json.dumps({"source": source, "exe": str(exe)}),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert compiled.returncode == 0, compiled.stderr
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"t2xpress_path": str(exe), "startup_timeout_sec": 10}), encoding="utf-8")
    server = tmp_path / "server.py"
    server.write_text(
        "import re\nfrom t2xpress_mcp import driver\n"
        f"driver.TITLE_RE = {re.escape(title) + '$'!r}\n"
        "driver.MAIN_TITLE_RE = re.compile(driver.TITLE_RE)\n"
        "from t2xpress_mcp.server import mcp\nmcp.run(show_banner=False)\n",
        encoding="utf-8",
    )
    settings_a = Path(__file__).parent / "fixtures" / "vv413_rf_tsfile.xt2"
    settings_b = tmp_path / "Enabled settings.xt2"
    settings_b.write_bytes(settings_a.read_bytes())
    env = {**os.environ, "T2XPRESS_MCP_CONFIG": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
    handles = []

    async def call(name, arguments):
        client = Client(StdioTransport(
            command=sys.executable, args=["-B", str(server)], env=env, keep_alive=False,
        ))
        async with client:
            result = await client.call_tool(name, arguments)
            data = json.loads(result.content[0].text)
            if name == "play":
                window = win32gui.FindWindow(None, title)
                assert window
                pid = win32process.GetWindowThreadProcessId(window)[1]
                handles.append(win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_TERMINATE, False, pid,
                ))
        return data

    async def exercise():
        for settings in (settings_a, settings_b):
            played = await call("play", {"settings_path": str(settings.resolve())})
            assert played["ok"] and played["state"] == "PLAYING"
            assert played["settings_path"] == str(settings.resolve())
            assert win32process.GetExitCodeProcess(handles[-1]) == win32con.STILL_ACTIVE
            for _ in range(2):
                status = await call("get_status", {})
                assert status["running"] and status["generating"] and status["state"] == "PLAYING"
                assert status["parameter_status"] == "Parameter set is valid"
                assert status["popups"] == []
            stopped = await call("stop", {})
            assert stopped["ok"] and stopped["state"] == "STOPPED"
            status = await call("get_status", {})
            assert not status["generating"] and status["state"] in ("IDLE", "NOT_RUNNING")

    try:
        asyncio.run(exercise())
    finally:
        for handle in handles:
            try:
                if win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE:
                    win32api.TerminateProcess(handle, 0)
            finally:
                handle.Close()
