# CLAUDE.md

## 项目概述

把 DekTec **T2Xpress**（DVB-T2 信号发生器）包装成 MCP 服务。T2Xpress 没有远程控制
API，唯一命令行开关是 `T2Xpress.exe -f <config>`，且**实测 `-f` 会立即开始生成/播出**
（RF 也是）。控制面因此是：

1. `settings.py` 在原始 XML 文本上打补丁（设置文件是 .NET XmlSerializer 格式）；
2. `driver.py` 用 pywinauto(UIA) 启动 `-f`、读按钮状态、必要时取消/关闭；
3. `server.py` 暴露 FastMCP 工具。

## 常用命令

```powershell
pip install -e ".[dev]"
python -m t2xpress_mcp        # stdio MCP
pytest
```

## 架构与关键事实

- **工具面**（server.py）：只保留 `play` / `stop` / `get_status` 三个必要播放工具
  （对齐 StreamXpress MCP 的最小播放面）。工具名不带前缀。配置解析/生成等能力
  仍在 `settings.py`，不再暴露为工具。
- **播出 = 启动进程**：`driver.start(config)` 用 `-f` 启动即播出；不要再依赖“点 Generate”。
- **状态判定**：生成中 = Generate 按钮（`toolStripButton4`）禁用 + Cancel 按钮
  （`toolStripButton2`）启用；结束/空闲时反转。状态栏 `statusStrip1` 显示参数有效性。
- **停止**：`File → Cancel generation` 只有在窗口获得前台焦点时才可靠；后台 MCP 会话
  经常失败。`cancel()` 只做一次短尝试（6s），`server.stop()` 失败后关闭应用
  （`close(cancel_first=False)`），必要时 `taskkill /F`，确保调制卡被释放。
- **配置写法**：禁止用 ElementTree 重写 `.xt2`（会丢命名空间/格式，T2Xpress 静默退出）。
  所有修改走 `settings._set_attr` 的文本替换。
- **相对路径陷阱**：T2Xpress 以自身安装目录为 cwd 解析 `-f`，必须传绝对路径。
- **多实例/重连**：`driver.running` 会在 `_pid` 丢失时按窗口标题重新 attach，支持 MCP
  重启后继续 `get_status`/`stop`。
- **测试**：`tests/test_settings.py` 覆盖 XML 解析/补丁/设备覆盖；GUI 行为靠手动 E2E。

## 已验证环境

Windows 11 + T2Xpress v2.9.0 + DTU-315 serial 315001075；生产配置
`resource/MYS_MPLP_settings_T2Xpress.xt2`（3 PLP，474 MHz，-18 dBm）。
`play → PLAYING → stop → STOPPED`，以及 T2-MI 文件模式 `COMPLETED` 均已实测。

## 坑

- `play` 的 `rf` 模式会立刻发射 RF。
- 生成需要 DTC-378-T2 授权；I/Q 需要 DTC-371-IQ。
- MCP 必须在交互式桌面会话运行，session 0 无法做 UIA。
- `StopT2Generation.exe`（DekTec 自带）在实测中总是报 “Failed to cancel”，不要依赖它。
- T2Xpress 关闭时若正在播出，可能弹确认框；`driver.close()` 会尝试点击确认并兜底强杀。
