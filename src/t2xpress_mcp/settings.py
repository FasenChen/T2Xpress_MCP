"""T2Xpress .xt2 settings: parse for inspection, patch for playout.

T2Xpress uses .NET XmlSerializer. Rewriting the file with ElementTree changes
namespace prefixes/formatting and makes T2Xpress exit silently, so all writes
here are *text-level* edits that leave the rest of the document untouched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# Values observed in DekTec's 296 sample configs (.xt2).
OUTPUT_MODES = {
    "rf": "rf",
    "t2mi": "t2mi",
    "t2mi_over_asi": "t2mi_over_asi",
    "iq_int16": "iq_int16_bin",
    "iq_int16_hex": "iq_int16_hex",
}

# File extensions T2Xpress appends to the output basename.
_FILE_SUFFIX = {"rf": None, "t2mi": ".ts", "t2mi_over_asi": None, "iq_int16_bin": ".iq", "iq_int16_hex": ".iq"}


class SettingsError(ValueError):
    """Invalid or unsupported T2Xpress settings file."""


def read_raw(path: str | Path) -> str:
    return Path(path).read_bytes().decode("utf-8-sig")


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _set_attr(text: str, tag: str, attr: str, value: object) -> str:
    """Set/replace one attribute on the first ``<tag ...>`` opening tag."""
    m = re.search(rf"<{tag}\b[^>]*>", text)
    if not m:
        raise SettingsError(f"设置文件中找不到 <{tag}> 元素")
    opening = m.group(0)
    escaped = _escape(value)
    if re.search(rf'\b{re.escape(attr)}="[^"]*"', opening):
        # Lambda replacement: a plain string would interpret Windows backslashes
        # (\U, \x ...) as regex replacement escapes.
        patched = re.sub(
            rf'\b{re.escape(attr)}="[^"]*"',
            lambda _m: f'{attr}="{escaped}"',
            opening,
            count=1,
        )
    else:
        # Insert before the closing ">" or "/>".
        if opening.endswith("/>"):
            patched = opening[:-2].rstrip() + f' {attr}="{escaped}" />'
        else:
            patched = opening[:-1].rstrip() + f' {attr}="{escaped}">'
    return text[: m.start()] + patched + text[m.end():]


def _plp_source_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of every <Source ...>...</Source> block, in order."""
    spans = []
    for m in re.finditer(r"<Source\b[^>]*>(.*?)</Source>", text, re.S):
        spans.append((m.start(), m.end()))
    # Self-closing <Source ... /> is not expected, but keep the count honest.
    return spans


def set_plp_sources(text: str, paths: list[str]) -> str:
    """Point PLP 0..n-1 at the given TS files, keeping each <Source> attributes."""
    spans = _plp_source_spans(text)
    if not spans:
        raise SettingsError("设置文件中没有 PLP <Source> 元素")
    if len(paths) > len(spans):
        raise SettingsError(
            f"提供了 {len(paths)} 个码流，但设置文件只有 {len(spans)} 个 PLP 源"
        )
    for (start, end), path in reversed(list(zip(spans, paths))):
        block = text[start:end]
        open_tag = re.match(r"<Source\b[^>]*>", block).group(0)
        new_block = f'{open_tag}\n        <TsFile in_file="{_escape(path)}" />\n      </Source>'
        text = text[:start] + new_block + text[end:]
    return text


def apply_overrides(
    text: str,
    *,
    output_mode: str | None = None,
    basename: str | None = None,
    num_super_frames: int | None = None,
    frequency_mhz: float | None = None,
    level_dbm: float | None = None,
    ts_files: list[str] | None = None,
    device_serial: int | str | None = None,
    device_port: int | None = None,
) -> str:
    if output_mode is not None:
        if output_mode not in OUTPUT_MODES:
            raise SettingsError(
                f"不支持的输出模式 {output_mode!r}，可选: {', '.join(OUTPUT_MODES)}"
            )
        text = _set_attr(text, "IqOutput", "iq_format", OUTPUT_MODES[output_mode])
    if basename is not None:
        text = _set_attr(text, "IqOutput", "basename", basename)
    if num_super_frames is not None:
        if num_super_frames < 1:
            raise SettingsError("num_super_frames 必须 >= 1")
        text = _set_attr(text, "IqOutput", "num_super_frames", num_super_frames)
    if frequency_mhz is not None:
        text = _set_attr(text, "IqOutput", "outp_rf_channel", int(round(frequency_mhz)))
    if level_dbm is not None:
        text = _set_attr(text, "IqOutput", "outp_rf_level", level_dbm)
    if ts_files:
        text = set_plp_sources(text, list(ts_files))
    if device_serial is not None:
        text = _set_attr(text, "IqOutput", "outp_serial", device_serial)
    if device_port is not None:
        text = _set_attr(text, "IqOutput", "outp_port", device_port)
    return text


def write_patched(
    template_path: str | Path,
    dest_path: str | Path,
    **overrides,
) -> Path:
    text = apply_overrides(read_raw(template_path), **overrides)
    dest = Path(dest_path)
    if not dest.is_absolute():
        dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="")
    return dest


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def load_settings(path: str | Path) -> dict:
    """Return a JSON-friendly summary of a .xt2 file (read-only, no rewriting)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"设置文件不存在: {p}")
    try:
        root = ET.parse(p).getroot()
    except ET.ParseError as exc:
        raise SettingsError(f"设置文件 XML 解析失败: {exc}") from exc

    components = [c for c in root if _local(c.tag) == "DvbT2"]
    if not components:
        raise SettingsError("不是 T2Xpress 设置文件（缺少 <DvbT2>）")
    dvbt2 = components[0]

    general = next((c for c in dvbt2 if _local(c.tag) == "General"), None)
    frame = next((c for c in dvbt2 if _local(c.tag) == "T2Frame"), None)
    output = next((c for c in root if _local(c.tag) == "IqOutput"), None)
    if output is None:
        raise SettingsError("设置文件缺少 <IqOutput> 输出配置")

    plps = []
    for plp in (c for c in dvbt2 if _local(c.tag) == "Plp"):
        source = next((c for c in plp if _local(c.tag) == "Source"), None)
        src_kind, src_path = "unknown", None
        if source is not None:
            for child in source:
                if _local(child.tag) == "TsFile":
                    src_kind, src_path = "ts_file", child.get("in_file")
                elif _local(child.tag) == "Prbs":
                    src_kind = "prbs"
        plps.append(
            {
                "id": plp.get("plp_id"),
                "type": plp.get("type"),
                "modulation": plp.get("modulation"),
                "code_rate": plp.get("code_rate"),
                "source": src_kind,
                "ts_file": src_path,
                "ts_rate": source.get("ts_rate") if source is not None else None,
            }
        )

    return {
        "path": str(p.resolve()),
        "components": len(components),
        "dvb_t2": {
            "bandwidth": general.get("bandwidth") if general is not None else None,
            "fft": general.get("fft_mode") if general is not None else None,
            "guard_interval": general.get("guard_itv") if general is not None else None,
            "l1_modulation": general.get("l1_modulation") if general is not None else None,
            "pilot_pattern": general.get("pilot") if general is not None else None,
            "t2_version": general.get("t2_version") if general is not None else None,
            "profile": general.get("t2_profile") if general is not None else None,
            "miso": general.get("miso") if general is not None else None,
        },
        "frame": {
            "num_t2_frames": frame.get("num_t2_frames") if frame is not None else None,
            "num_data_syms": frame.get("num_data_syms") if frame is not None else None,
        },
        "output": {
            "mode": output.get("iq_format"),
            "basename": output.get("basename"),
            "num_super_frames": output.get("num_super_frames"),
            "frequency_mhz": output.get("outp_rf_channel"),
            "level_dbm": output.get("outp_rf_level"),
            "device_serial": output.get("outp_serial"),
            "device_port": output.get("outp_port"),
            "ip": [
                output.get(f"outp_ip_ip{i}") for i in range(4)
            ],
            "ip_port": output.get("outp_ip_port"),
            "protocol": output.get("outp_ip_protocol"),
            "t2mi_pid": output.get("t2mi_pid"),
            "t2mi_rate": output.get("t2mi_rate"),
        },
        "plps": plps,
    }


def expected_output_suffix(mode: str | None) -> str | None:
    return _FILE_SUFFIX.get(mode or "")
