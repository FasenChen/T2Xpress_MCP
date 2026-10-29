from pathlib import Path

import pytest

from t2xpress_mcp.settings import (
    SettingsError,
    apply_overrides,
    expected_output_suffix,
    load_settings,
    read_raw,
    write_patched,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_settings_reports_output_and_plp_source():
    info = load_settings(FIXTURES / "vv413_rf_tsfile.xt2")

    assert info["output"]["mode"] == "rf"
    assert info["output"]["frequency_mhz"] == "666"
    assert info["output"]["level_dbm"] == "-20"
    assert info["dvb_t2"]["bandwidth"] == "bw_8"
    assert [p["id"] for p in info["plps"]] == ["0", "1"]
    assert info["plps"][0]["source"] == "ts_file"
    assert info["plps"][0]["ts_file"].endswith("PLP0.ts")
    assert info["plps"][1]["ts_file"].endswith("PLP1.ts")


def test_load_settings_reports_prbs_source():
    info = load_settings(FIXTURES / "vv500_t2mi_prbs.xt2")
    assert info["output"]["mode"] == "t2mi"
    assert info["plps"][0]["source"] == "prbs"


def test_apply_overrides_keeps_document_shape():
    original = read_raw(FIXTURES / "vv800_rf_prbs.xt2")
    patched = apply_overrides(
        original,
        output_mode="t2mi",
        basename=r"D:\tmp\out",
        num_super_frames=7,
        frequency_mhz=474.0,
        level_dbm=-12.5,
    )

    # The namespace declarations live on the root element and must survive.
    assert 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' in patched
    assert 'iq_format="t2mi"' in patched
    assert 'basename="D:\\tmp\\out"' in patched
    assert 'num_super_frames="7"' in patched
    assert 'outp_rf_channel="474"' in patched
    assert 'outp_rf_level="-12.5"' in patched
    # Untouched sections are byte-identical.
    assert "<General " in patched and "<Plp " in patched


def test_apply_overrides_inserts_missing_basename():
    text = read_raw(FIXTURES / "vv001_iq_prbs.xt2")
    assert 'basename=' not in text
    patched = apply_overrides(text, basename=r"D:\out\base")
    assert '<IqOutput iq_format="iq_int16_bin"' in patched
    assert 'basename="D:\\out\\base"' in patched
    assert ' />' in patched


def test_set_plp_sources_replaces_prbs_with_tsfile(tmp_path):
    text = read_raw(FIXTURES / "vv500_t2mi_prbs.xt2")
    patched = apply_overrides(text, ts_files=[r"D:\streams\a.ts"])
    assert "<Prbs" not in patched
    assert 'in_file="D:\\streams\\a.ts"' in patched
    assert 'ts_rate="' in patched


def test_set_plp_sources_for_each_plp():
    text = read_raw(FIXTURES / "vv413_rf_tsfile.xt2")
    patched = apply_overrides(
        text, ts_files=[r"D:\streams\a.ts", r"D:\streams\b.ts"]
    )
    assert 'in_file="D:\\streams\\a.ts"' in patched
    assert 'in_file="D:\\streams\\b.ts"' in patched
    assert "PLP0.ts" not in patched


def test_device_serial_and_port_override():
    text = read_raw(FIXTURES / "vv800_rf_prbs.xt2")
    patched = apply_overrides(text, device_serial=315001075, device_port=1)
    assert 'outp_serial="315001075"' in patched
    assert 'outp_port="1"' in patched


def test_write_patched_roundtrips(tmp_path):
    dest = tmp_path / "patched.xt2"
    write_patched(
        FIXTURES / "vv500_t2mi_prbs.xt2",
        dest,
        output_mode="iq_int16",
        basename=str(tmp_path / "gen"),
        num_super_frames=1,
    )
    info = load_settings(dest)
    assert info["output"]["mode"] == "iq_int16_bin"
    assert info["output"]["num_super_frames"] == "1"
    assert expected_output_suffix(info["output"]["mode"]) == ".iq"


def test_invalid_mode_and_missing_element():
    text = read_raw(FIXTURES / "vv500_t2mi_prbs.xt2")
    with pytest.raises(SettingsError):
        apply_overrides(text, output_mode="nope")
    with pytest.raises(SettingsError):
        apply_overrides(text, ts_files=["a", "b", "c"])  # only one PLP in VV500


def test_playlist_rejects_more_streams_than_plps():
    text = read_raw(FIXTURES / "vv500_t2mi_prbs.xt2")
    with pytest.raises(SettingsError, match="PLP"):
        apply_overrides(text, ts_files=[r"D:\a.ts", r"D:\b.ts"])
