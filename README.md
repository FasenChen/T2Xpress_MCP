# T2Xpress MCP Server

把 DekTec **T2Xpress**（DTC-378 DVB-T2 信号发生器）包装成 MCP 服务：加载 `.xt2`
配置与 PLP 码流，实时播出 DVB-T2（RF / T2-MI over ASI），或生成 T2-MI / I/Q 文件。

工具面刻意保持最小 —— 只有播放、停止、状态查询三个工具，对齐
[StreamXpress MCP](https://github.com/FasenChen/StreamXpress_MCP) 的播放面。

---

## 为什么是 GUI 自动化

T2Xpress v2.9.0 **没有远程控制 API**，官方只提供一个命令行开关：

```
T2Xpress.exe -f <config_file>
```

实测（Windows 11 + v2.9.0）：`-f` 不只是打开配置，而是**立即开始生成/播出**，RF 同样。
所以控制模型只能是这样：

| 动作 | 实现 |
|---|---|
| 加载配置、码流并播出 | 在原始 XML 文本上打补丁生成临时 `.xt2`，用 `-f` 启动 T2Xpress |
| 状态判定 | UIA 读工具栏：生成中 = Generate（`toolStripButton4`）禁用 + Cancel（`toolStripButton2`）启用 |
| 停止 | 先 `File → Cancel generation`；菜单在后台会话点不中时关闭 T2Xpress，释放调制卡 |

`.xt2` 是 .NET XmlSerializer 格式，**不能用 ElementTree 重写**（会丢命名空间/格式，
T2Xpress 静默退出）。本项目所有修改都是原始文本上的属性替换。

## 已验证环境

| 项 | 值 |
|---|---|
| 系统 | Windows 11 Pro（交互式桌面会话） |
| T2Xpress | v2.9.0，`C:\Program Files (x86)\DekTec\T2Xpress\T2Xpress.exe` |
| 调制卡 | DTU-315，serial `315002019`，port 1 |
| 播出 | DVB-T2 8 MHz / 32K FFT / GI 1/128 / L1 QAM64，474 MHz，-25 dBm |
| 多 PLP | PLP0 QAM256 + PLP1 QAM64 + PLP2 QAM16，3 路 TS 同时播出 |
| 实测结果 | `play → PLAYING`（`Parameter set is valid`）、`stop → STOPPED`；T2-MI 文件模式 `COMPLETED` |

## 环境要求

- Windows，**交互式桌面会话**（session 0 / 服务方式无法做 UIA，必须能看到 T2Xpress 窗口）
- Python ≥ 3.10
- T2Xpress 已安装并授权：生成需 **DTC-378-T2**，I/Q 输出另需 **DTC-371-IQ**
- 调制卡（DTU-315）已连接且未被其他程序占用

## 安装

```powershell
cd D:\T2Xpress_MCP
pip install -e ".[dev]"
python -m t2xpress_mcp        # 前台跑 stdio MCP 服务
```

依赖：`fastmcp`、`pywinauto`（Windows UIA）。

## 配置（`config.json`）

| 字段 | 说明 |
|---|---|
| `t2xpress_path` | `T2Xpress.exe` 完整路径 |
| `stop_exe_path` | 保留字段（DekTec 的 `StopT2Generation.exe`；实测总是报 "Failed to cancel"，不依赖） |
| `presets_dir` | 保留字段（预设目录，当前工具面未使用） |
| `startup_timeout_sec` | 启动后等待主窗口出现的秒数 |
| `state_timeout_sec` | 状态轮询等待秒数 |

查找顺序：`$T2XPRESS_MCP_CONFIG` → 项目根 `config.json` → 内置默认值。

## MCP 客户端配置

```json
{
  "mcpServers": {
    "t2xpress": {
      "command": "python",
      "args": ["-m", "t2xpress_mcp"]
    }
  }
}
```

装在 venv 时，`command` 必须指向该 venv 的 `python.exe` 绝对路径。
改动工具面后需要重启 MCP 客户端（重建 stdio 进程）才会生效。

## 工具

| 工具 | 说明 |
|---|---|
| `play` | 加载 `.xt2` 并开播，可覆盖码流 / 输出模式 / 频率 / 电平 / 设备 |
| `stop` | 停止播出（先菜单，失败则关闭应用释放调制卡） |
| `get_status` | 运行状态、参数有效性、错误弹窗、输出文件 |

### `play`

```text
play(
    settings_path,
    stream=None, ts_files=None, output_mode=None, basename=None,
    frequency_mhz=None, level_dbm=None, num_super_frames=None,
    device_serial=None, device_port=None,
) -> dict
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `settings_path` | str，必填 | `.xt2` 的**绝对路径**（相对路径会被拒绝：T2Xpress 以自身安装目录为 cwd 解析 `-f`） |
| `stream` | str | 单路 PLP 码流；与 `ts_files` 二选一 |
| `ts_files` | list[str] | 多路 PLP 码流，顺序对应 PLP 0..n-1，数量不能超过配置中的 PLP 数 |
| `output_mode` | str | `rf` / `t2mi` / `t2mi_over_asi` / `iq_int16` / `iq_int16_hex` |
| `basename` | str | 文件类输出的基名 |
| `frequency_mhz` | float | RF 频率，40–1000 |
| `level_dbm` | float | RF 电平，-60–0 |
| `num_super_frames` | int | 文件输出长度，≥1 |
| `device_serial` | int/str | 调制卡序列号（多卡时指定） |
| `device_port` | int | 调制卡端口 |

行为要点：

- 只要有一个覆盖参数非 `None`，就在 `%TEMP%\t2xpress_mcp\` 生成补丁副本并播出；
  **原文件不动**。全部为 `None` 时直接使用原文件。
- 仍在生成中调用 `play` 会报错 —— 先 `stop()` 再切换任务。
- 返回 `state`：`rf` / `t2mi_over_asi` 为连续输出，返回 `PLAYING`；文件模式跑完返回
  `COMPLETED`。同时返回 `parameter_status`、`config`（实际使用的路径）、`outputs`。

### `stop`

```text
stop() -> dict
```

无参数。已在生成时先尝试 `File → Cancel generation`（6 秒）；WinForms 菜单在后台
MCP 会话常常点不中，此时直接关闭 T2Xpress（进程退出即释放调制卡、停止 RF）。
返回 `state` = `STOPPED` / `IDLE` / `NOT_RUNNING`，`method` = `menu` / `closed_app`。

### `get_status`

```text
get_status() -> dict
```

无参数。返回 `running`、`generating`、`idle`、`parameter_status`（状态栏原文）、
`popups`（错误对话框文本）、`outputs`，以及归一化的 `state`：

| `state` | 含义 |
|---|---|
| `PLAYING` | 正在生成/播出 |
| `IDLE` | 应用在运行但未生成 |
| `ERROR` | 有错误弹窗（内容见 `popups`） |
| `NOT_RUNNING` | T2Xpress 未运行 |

MCP 重启后仍可接管已运行的实例：driver 按窗口标题重新 attach。

## 示例

```python
play(
    settings_path=r"D:\Resource\MALAYSIA\MYS_MPLP_settings_T2Xpress.xt2",
    ts_files=[
        r"D:\Resource\MALAYSIA\6.0 Multiple-PLP\MYS_MPLP_HD.ts",
        r"D:\Resource\MALAYSIA\6.0 Multiple-PLP\MYS_MPLP_SD.ts",
        r"D:\Resource\MALAYSIA\6.0 Multiple-PLP\MYS_MPLP_Radio.ts",
    ],
    frequency_mhz=474,
    level_dbm=-25,
)
# → {"state": "PLAYING", "output_mode": "rf", "parameter_status": "Parameter set is valid", ...}

get_status()   # → state=PLAYING
stop()         # → state=STOPPED, method=menu 或 closed_app
```

## 重要限制

- **会真实发射 RF**：`rf` 模式在 `-f` 启动瞬间就上射频，确认已接天线或负载。
- 生成需要 **DTC-378-T2** 授权；I/Q 输出另需 **DTC-371-IQ**。
- `stop` 兜底会关闭 T2Xpress，下次 `play` 重新启动（约 9 秒）；播出中断是必然的。
- 没有在线改参数的路径：任何参数变更都要 停播 → 生成补丁 → 重新 `-f`。
- 不读取信号质量（MER / 功率 / 星座图），T2Xpress 不提供测量接口。
- `StopT2Generation.exe` 实测总是 "Failed to cancel"，不依赖。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| `T2Xpress 启动后未出现主窗口` | 设置文件路径无效或被改写；确认是绝对路径、XML 未被重排 |
| `T2Xpress 认为参数集无效` | 状态栏 `Parameter set is not valid`；检查 `get_status().parameter_status` 与 `.xt2` 内容 |
| `stop()` 返回 `method=closed_app` | 后台会话点不中菜单，属预期兜底，RF 已随进程退出停止 |
| 一切 UIA 操作超时 | MCP 不在交互式桌面会话（session 0），改为登录桌面用户下运行 |
| `play` 报“正在生成中” | 先 `stop()`，再切换配置 |

## 开发与测试

```powershell
pip install -e ".[dev]"
pytest
```

`tests/test_settings.py` 覆盖 `.xt2` 解析、属性补丁、PLP 码流替换、设备覆盖与
格式保持；GUI 行为（启动/状态/停止）依赖真机，靠手动 E2E 验证。

## 仓库结构

```
src/t2xpress_mcp/
  server.py       # FastMCP 工具面：play / stop / get_status
  driver.py       # pywinauto(UIA) 驱动：启动、按钮状态、取消、关闭
  settings.py     # .xt2 解析与文本级补丁（不重写 XML）
  config.py       # config.json 加载
tests/            # 设置文件解析/补丁单测 + fixtures
```

`resource/`（示例配置、码流）、`research/`（可行性分析、PE/DLL 探测脚本）以及
厂商安装包均为本地目录，**不入库**。

## 许可与商标

本项目与 DekTec 无隶属关系。T2Xpress、StreamXpress、DTU-315 等为 DekTec
Digital Television 的商标；厂商安装包与手册不随仓库分发。
