"""Automation API service: argument checks, every tool family, against a real controller.

The controller is driven with synthetic samples (`feed` / `steps`); waits use a
fake clock so the suite runs fast and deterministically.
"""
from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.api import schema
from app.api.schema import TOOLS, TOOLS_BY_NAME
from app.api.service import (ApiError, ApiService, DirectContext, RateLimited, _jsonable,
                             _validate)
from app.core import AppSettings, Samples
from app.serial import PROTO_V2, DeviceConfig
from app.serial.protocol import DeviceInfo
from app.serial.protocol import AVG_CHOICES, CT_CHOICES_US
from app.ui import controller as C
from app.ui.controller import AcquisitionController
from app.ui.widgets.port_discovery import PortDiscovery

RATE = 100.0


# ----------------------------------------------------------------- helpers

def steps(spec, rate: float = RATE) -> Samples:
    """spec: [(duration_s, volts, amps), ...] from t=0; samples at k/rate, inclusive end."""
    total = sum(d for d, _, _ in spec)
    t = np.arange(int(round(total * rate)) + 1) / rate
    v = np.empty_like(t)
    i = np.empty_like(t)
    start = 0.0
    for dur, vv, ii in spec:
        m = (t >= start) & (t < start + dur)
        v[m] = vv
        i[m] = ii
        start += dur
    v[-1], i[-1] = spec[-1][1], spec[-1][2]
    return Samples.from_columns(t, 1.79e9 + t, v, i, v * i)


def feed(ctrl: AcquisitionController, seconds: float, v: float = 5.0, i: float = 0.2) -> None:
    """Append `seconds` of constant readings through the controller's ingest path."""
    n = int(round(seconds * RATE))
    if n <= 0:
        return
    start = ctrl.store.last_t + 1 / RATE if len(ctrl.store) else 0.0
    t = start + np.arange(n) / RATE
    ctrl._ingest(Samples.from_columns(t, 1.79e9 + t, np.full(n, v), np.full(n, i),
                                      np.full(n, v * i)))


class FakeClock:
    """Injected sleep/monotonic: sleeping advances time and runs an optional hook."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeReader:
    """Minimal stand-in for SerialReader: records commands sent to the device."""

    def __init__(self) -> None:
        self.sent: list = []

    def isRunning(self) -> bool:
        return True

    def send_command(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """The recordings folder: the only allowed export/import folder unless a test adds one."""
    folder = tmp_path / "rec"
    folder.mkdir()
    monkeypatch.delenv(schema.EXPORT_DIRS_ENV, raising=False)
    return folder


@pytest.fixture
def ctrl(qapp, rec):
    c = AcquisitionController(AppSettings())
    c.settings.recordings_dir = str(rec)
    yield c
    c.reader = None
    c.shutdown()


@pytest.fixture
def clock():
    return FakeClock()


# Serial devices the port check accepts (the list_ports listing); tests that need other
# discovery results replace get_ports themselves.
DISCOVERED = ["/dev/fake", "/dev/x", "/dev/ttyACM0", "/dev/ttyUSB7"]


@pytest.fixture(autouse=True)
def discovered_ports(monkeypatch):
    monkeypatch.setattr(PortDiscovery, "get_ports", staticmethod(
        lambda show_all=False: [(p, f"{p} — test") for p in DISCOVERED]))
    monkeypatch.setattr(PortDiscovery, "find_esp32_ports", staticmethod(lambda: []))


def make_service(ctrl, clock, on_changed=lambda: None) -> ApiService:
    return ApiService(ctrl, ctrl.settings, DirectContext(), on_settings_changed=on_changed,
                      sleep=clock.sleep, monotonic=clock.monotonic)


@pytest.fixture
def svc(ctrl, clock):
    return make_service(ctrl, clock)


def expect_error(svc, name, params, match):
    with pytest.raises(ApiError, match=match):
        svc.call(name, params)


# ------------------------------------------------------------------ schema

def test_registry_has_the_25_builtin_tools_then_the_extension_tools():
    names = [t.name for t in TOOLS]
    builtin = names[:25]
    assert builtin == [
        "get_status", "list_ports", "start_acquisition", "stop_acquisition", "clear_data",
        "get_live", "add_marker", "list_markers", "update_marker", "remove_marker",
        "get_segments", "get_statistics", "measure", "benchmark", "get_samples", "export_csv",
        "export_pdf", "import_csv", "get_device", "configure_device", "send_command",
        "sync_clock", "get_settings", "update_settings", "wait",
    ]
    assert len(names) == len(set(names)) == 38           # plus the 13 extension tools
    assert set(TOOLS_BY_NAME) == set(names)
    for t in TOOLS:
        assert t.description.strip()
        assert t.input_schema["type"] == "object"
    for t in TOOLS[:25]:
        assert t.input_schema["additionalProperties"] is False
        assert hasattr(ApiService, f"_tool_{t.name}")


def test_schema_constants_match_protocol_and_settings():
    assert schema.INA226_AVG_VALUES == AVG_CHOICES
    assert schema.INA226_CT_VALUES_US == CT_CHOICES_US
    fields = {f.name for f in dataclasses.fields(AppSettings)}
    assert set(schema.SETTINGS_SCHEMA) <= fields
    assert "api_token" not in schema.SETTINGS_SCHEMA


def test_defaults_are_applied():
    out = _validate(TOOLS_BY_NAME["get_samples"].input_schema, {"t0": 1.0})
    assert out == {"t0": 1.0, "max_points": 2000}
    out = _validate(TOOLS_BY_NAME["measure"].input_schema, {"duration_s": 2})
    assert out["label"] == "measurement" and out["settle_s"] == 0
    assert isinstance(out["duration_s"], float)


# ------------------------------------------------------- argument validation

def test_unknown_tool_and_non_object_arguments(svc):
    expect_error(svc, "frobnicate", {}, "unknown tool")
    expect_error(svc, "get_status", [], "must be a JSON object")


def test_unknown_key_is_rejected(svc):
    expect_error(svc, "list_ports", {"bogus": 1}, "unknown key 'bogus'")


def test_wrong_type_is_rejected(svc):
    expect_error(svc, "get_live", {"window_s": "fast"}, "window_s must be a number")
    expect_error(svc, "list_ports", {"show_all": "yes"}, "show_all must be true or false")


def test_bool_is_not_an_integer_or_number(svc):
    expect_error(svc, "remove_marker", {"id": True}, "id must be an integer")
    expect_error(svc, "wait", {"seconds": True}, "seconds must be a number")


def test_min_max_and_exclusive_bounds(svc):
    expect_error(svc, "wait", {"seconds": 4000}, "at most 3600")
    expect_error(svc, "get_samples", {"max_points": 5}, "at least 10")
    expect_error(svc, "measure", {"duration_s": 0}, "greater than 0")
    expect_error(svc, "get_live", {"window_s": 0}, "greater than 0")


def test_missing_required_argument(svc):
    expect_error(svc, "remove_marker", {}, "missing required 'id'")


def test_enum_values_are_checked(svc):
    expect_error(svc, "configure_device", {"avg": 3}, "must be one of")
    expect_error(svc, "configure_device", {"vct_us": 1000}, "must be one of")


def test_update_settings_rejects_non_whitelisted_keys(svc):
    expect_error(svc, "update_settings", {"settings": {"api_token": "x"}},
                 "unknown key 'api_token'")
    expect_error(svc, "update_settings", {"settings": {"baud_rate": 9600}},
                 "unknown key 'baud_rate'")


def test_update_settings_checks_types_and_choices(svc):
    expect_error(svc, "update_settings", {"settings": {"auto_reconnect": "yes"}},
                 "must be true or false")
    expect_error(svc, "update_settings", {"settings": {"csv_separator": "|"}},
                 "must be one of")


def test_send_command_rejects_line_breaks(svc):
    expect_error(svc, "send_command", {"command": "GET\nSET STREAM 0"}, "single line")


# ------------------------------------------------------------------ status

def test_get_status_when_idle(svc):
    s = svc.call("get_status", None)
    assert s["state"] == "idle" and s["running"] is False
    assert s["samples"] == 0 and s["recording_path"] is None
    assert s["api_version"] == 1 and s["app_version"]
    assert s["device_info"] is None and s["device_config"] is None


def test_get_status_after_import_reports_source(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "bench.csv")
    s = svc.call("get_status", {})
    assert s["source"] == "import" and s["source_name"] == "bench.csv"
    assert s["samples"] == 1001 and s["duration_s"] == pytest.approx(10.0)


def test_get_live_needs_data_and_reports_values(ctrl, svc):
    expect_error(svc, "get_live", {}, "no data")
    feed(ctrl, 4.0, v=5.0, i=0.2)
    out = svc.call("get_live", {"window_s": 1.0})
    assert out["voltage_v"] == pytest.approx(5.0)
    assert out["current_a"] == pytest.approx(0.2)
    assert out["power_w"] == pytest.approx(1.0)
    assert out["avg_power_w"] == pytest.approx(1.0)
    assert out["energy_wh"] == pytest.approx(4.0 / 3600.0, rel=0.05)
    assert out["samples"] == len(ctrl.store)


# ----------------------------------------------------------------- markers

def test_marker_crud(ctrl, svc):
    expect_error(svc, "add_marker", {"label": "x"}, "no data yet")
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    late = svc.call("add_marker", {"label": "late", "t": 6.0})
    early = svc.call("add_marker", {"label": "early", "t": 1.0})
    assert late["t"] == 6.0 and early["label"] == "early"
    listed = svc.call("list_markers", {})["markers"]
    assert [m["t"] for m in listed] == [1.0, 6.0]

    upd = svc.call("update_marker", {"id": late["id"], "label": "renamed", "t": 7.5})
    assert upd == {"id": late["id"], "t": 7.5, "label": "renamed"}
    expect_error(svc, "update_marker", {"id": 999, "label": "x"}, "no marker with id 999")
    expect_error(svc, "update_marker", {"id": early["id"]}, "nothing to update")

    assert svc.call("remove_marker", {"id": early["id"]}) == {"removed": True}
    assert svc.call("remove_marker", {"id": early["id"]}) == {"removed": False}
    assert len(svc.call("list_markers", {})["markers"]) == 1


# ------------------------------------------------------------- statistics

def test_statistics_default_range_is_all_data(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    out = svc.call("get_statistics", {})
    st = out["stats"]
    assert out["range"]["label"] == "all data"
    assert st["count"] == 1001
    assert st["duration_seconds"] == pytest.approx(10.0)
    assert st["power_avg"] == pytest.approx(1.0, rel=1e-4)
    assert st["energy_j"] == pytest.approx(10.0, rel=1e-4)
    assert st["energy_j"] == pytest.approx(st["energy_wh"] * 3600.0)
    assert st["charge_c"] == pytest.approx(st["charge_ah"] * 3600.0)


def test_statistics_explicit_times_and_last_s(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    out = svc.call("get_statistics", {"t0": 2.0, "t1": 4.0})
    assert out["range"]["t0"] == 2.0 and out["range"]["t1"] == 4.0
    assert out["stats"]["duration_seconds"] == pytest.approx(2.0)
    last = svc.call("get_statistics", {"last_s": 3})
    assert last["stats"]["duration_seconds"] == pytest.approx(3.0)
    assert last["range"]["label"] == "last 3 s"


def test_statistics_marker_range_and_segment_precedence(ctrl, svc):
    ctrl.load(steps([(2, 5.0, 0.1), (8, 5.0, 0.2)]), "x")   # power 0.5 W then 1.0 W
    a = svc.call("add_marker", {"label": "a", "t": 2.0})
    b = svc.call("add_marker", {"label": "b", "t": 6.0})
    out = svc.call("get_statistics", {"marker_from": a["id"], "marker_to": b["id"]})
    assert out["stats"]["duration_seconds"] == pytest.approx(4.0)
    assert out["stats"]["power_avg"] == pytest.approx(1.0, rel=1e-4)
    # segment beats markers and t0/t1 (the boundary sample at 2.0 s belongs to segment 0)
    seg = svc.call("get_statistics", {"segment": 0, "marker_from": a["id"],
                                      "marker_to": b["id"], "t0": 5.0})
    assert seg["stats"]["duration_seconds"] == pytest.approx(2.0)
    assert seg["stats"]["power_avg"] == pytest.approx(0.5, rel=1e-2)


def test_range_errors(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "get_statistics", {"t0": 5.0, "t1": 1.0}, "greater than t0")
    expect_error(svc, "get_statistics", {"t0": 50.0, "t1": 60.0}, "not enough samples")
    expect_error(svc, "get_statistics", {"segment": 3}, "no segment 3")
    expect_error(svc, "get_statistics", {"marker_from": 1}, "given together")
    expect_error(svc, "get_statistics", {"marker_from": 7, "marker_to": 8}, "no marker")


def test_get_segments_splits_at_markers(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    svc.call("add_marker", {"label": "phase 2", "t": 3.0})
    svc.call("add_marker", {"label": "phase 3", "t": 6.0})
    segs = svc.call("get_segments", {})["segments"]
    assert [s["index"] for s in segs] == [0, 1, 2]      # start->m1, m1->m2, m2->end
    assert segs[0]["t0"] == 0.0 and segs[0]["t1"] == pytest.approx(3.0)
    assert segs[1]["t1"] == pytest.approx(6.0)
    assert segs[-1]["t1"] == pytest.approx(10.0) and segs[-1]["t0"] == pytest.approx(6.0)
    assert segs[1]["stats"]["duration_seconds"] == pytest.approx(3.0)
    assert "phase 2" in segs[1]["name"]


def test_benchmark_units_rate_and_idle_segment(ctrl, svc):
    # 0-2 s idle at 0.5 W, 2-10 s work at 1.0 W
    ctrl.load(steps([(2, 5.0, 0.1), (8, 5.0, 0.2)]), "x")
    svc.call("add_marker", {"label": "work", "t": 2.0})

    by_units = svc.call("benchmark", {"segment": 1, "units": 80})["benchmark"]
    assert by_units["duration_s"] == pytest.approx(8.0)
    assert by_units["energy_j"] == pytest.approx(8.0, rel=1e-4)
    assert by_units["energy_per_unit_j"] == pytest.approx(0.1, rel=1e-4)
    assert by_units["units_per_joule"] == pytest.approx(10.0, rel=1e-4)
    assert by_units["rate"] == pytest.approx(10.0)
    assert by_units["rate_per_watt"] == pytest.approx(10.0, rel=1e-4)
    assert by_units["idle_power_w"] is None

    by_rate = svc.call("benchmark", {"t0": 2.0, "t1": 10.0, "rate": 4.0,
                                     "idle_power_w": 0.4})["benchmark"]
    assert by_rate["units"] == pytest.approx(32.0)
    assert by_rate["net_power_w"] == pytest.approx(0.6, rel=1e-3)

    idle = svc.call("benchmark", {"segment": 1, "units": 80, "idle_segment": 0})["benchmark"]
    assert idle["idle_power_w"] == pytest.approx(0.5, rel=1e-2)
    assert idle["net_power_w"] == pytest.approx(0.5, rel=1e-2)


def test_benchmark_input_errors(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "benchmark", {"units": 5, "rate": 2}, "exactly one")
    expect_error(svc, "benchmark", {}, "exactly one")
    expect_error(svc, "benchmark", {"units": 5, "idle_power_w": 1, "idle_segment": 0},
                 "not both")
    expect_error(svc, "benchmark", {"units": 0}, "greater than 0")


def test_get_samples_decimates_uniformly(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    out = svc.call("get_samples", {"max_points": 100})
    assert out["decimated"] is True and out["count_in_range"] == 1001
    assert out["returned"] == 100 == len(out["t"]) == len(out["v"]) == len(out["p"])
    assert out["t"][0] == 0.0 and out["t"][-1] == pytest.approx(10.0)
    assert out["p"][0] == pytest.approx(1.0, rel=1e-4)


def test_get_samples_full_resolution_and_quantities(ctrl, svc):
    ctrl.load(steps([(1, 5.0, 0.2)]), "x")
    out = svc.call("get_samples", {"quantities": ["p"]})
    assert out["decimated"] is False and out["returned"] == 101
    assert set(out) >= {"t", "p", "decimated", "count_in_range"}
    assert "v" not in out and "i" not in out


# ------------------------------------------------------------------ measure

def test_measure_requires_streaming(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "measure", {"duration_s": 1}, "not streaming")


def test_measure_returns_stats_of_its_window(ctrl, svc, clock):
    feed(ctrl, 10.0)                               # 10 s of data at 1 W
    ctrl.state = C.STATE_STREAMING
    clock.on_sleep = lambda s: feed(ctrl, s)       # the device keeps streaming meanwhile
    out = svc.call("measure", {"duration_s": 2.0, "label": "load step"})
    assert out["start_marker"]["label"] == "load step"
    assert out["end_marker"]["label"] == "load step end"
    assert out["range"]["t1"] - out["range"]["t0"] == pytest.approx(2.0, abs=0.05)
    st = out["stats"]
    assert st["duration_seconds"] == pytest.approx(2.0, abs=0.05)
    assert st["power_avg"] == pytest.approx(1.0, rel=1e-3)
    assert st["energy_j"] == pytest.approx(2.0, rel=0.05)
    labels = [m["label"] for m in svc.call("list_markers", {})["markers"]]
    assert labels == ["load step", "load step end"]


def test_measure_fails_when_acquisition_stops(ctrl, svc, clock):
    feed(ctrl, 5.0)
    ctrl.state = C.STATE_STREAMING

    def stop_after_a_while(seconds):
        feed(ctrl, seconds)
        if clock.now > 1.0:
            ctrl.state = C.STATE_IDLE

    clock.on_sleep = stop_after_a_while
    expect_error(svc, "measure", {"duration_s": 10}, "acquisition stopped during measurement")


# ----------------------------------------------------------------- export

def test_export_csv_overwrite_guard_and_markers(ctrl, svc, rec: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    svc.call("add_marker", {"label": "mid", "t": 5.0})
    path = rec / "out.csv"
    out = svc.call("export_csv", {"path": str(path)})
    assert out["path"] == str(path) and out["rows"] == 1001
    text = path.read_text(encoding="utf-8")
    assert text.startswith("Timestamp,RelativeTime[s]") and "#MARKER" in text and "mid" in text

    expect_error(svc, "export_csv", {"path": str(path)}, "already exists")
    svc.call("export_csv", {"path": str(path), "overwrite": True})


def test_export_path_checks(ctrl, svc, rec: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "export_csv", {"path": str(rec / "nope" / "x.csv")},
                 "folder does not exist")
    (rec / "dir.csv").mkdir()
    expect_error(svc, "export_csv", {"path": str(rec / "dir.csv")}, "is a folder")


def test_export_pdf_writes_a_report(ctrl, svc, rec: Path):
    pytest.importorskip("reportlab")
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    path = rec / "report.pdf"
    out = svc.call("export_pdf", {"path": str(path), "title": "Run 4"})
    assert out["path"] == str(path)
    assert path.read_bytes()[:4] == b"%PDF"


def test_import_csv_round_trip_restores_samples_and_markers(ctrl, svc, rec: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    svc.call("add_marker", {"label": "phase", "t": 4.0})
    path = rec / "rt.csv"
    svc.call("export_csv", {"path": str(path)})     # whole recording saved: no unsaved data
    assert not svc.call("get_status", {})["has_unsaved_data"]
    svc.call("clear_data", {})
    assert svc.call("get_status", {})["samples"] == 0

    st = svc.call("import_csv", {"path": str(path)})
    assert st["samples"] == 1001 and st["source"] == "import" and st["source_name"] == "rt.csv"
    assert st["markers"] == 1
    m = svc.call("list_markers", {})["markers"][0]
    assert m["label"] == "phase" and m["t"] == pytest.approx(4.0, abs=1e-5)
    assert svc.call("get_statistics", {})["stats"]["power_avg"] == pytest.approx(1.0, rel=1e-4)


def test_import_refused_while_running_and_for_missing_file(ctrl, svc, rec: Path):
    ctrl.state = C.STATE_STREAMING
    expect_error(svc, "import_csv", {"path": str(rec / "x.csv")}, "file not found")
    (rec / "x.csv").write_text("Timestamp,V,I,P\n", encoding="utf-8")
    expect_error(svc, "import_csv", {"path": str(rec / "x.csv")}, "stop the acquisition")


# ------------------------------------------------------- unsaved-data guard

def test_unsaved_data_guard(ctrl, svc, rec: Path, monkeypatch):
    feed(ctrl, 2.0)                                # live data never written to disk
    assert ctrl.has_unsaved_data
    expect_error(svc, "clear_data", {}, "unsaved data; pass discard_unsaved=true")
    expect_error(svc, "start_acquisition", {"port": "/dev/fake"}, "unsaved data")
    (rec / "x.csv").write_text("Timestamp,V,I,P\n", encoding="utf-8")
    expect_error(svc, "import_csv", {"path": str(rec / "x.csv")}, "unsaved data")

    st = svc.call("clear_data", {"discard_unsaved": True})
    assert st["samples"] == 0 and not st["has_unsaved_data"]


def test_start_acquisition_uses_suggested_port_and_waits(ctrl, svc, clock, monkeypatch):
    monkeypatch.setattr(PortDiscovery, "get_ports", staticmethod(
        lambda show_all=False: [("/dev/ttyUSB7", "/dev/ttyUSB7 — CP2102")]))
    monkeypatch.setattr(PortDiscovery, "find_esp32_ports", staticmethod(
        lambda: [SimpleNamespace(device="/dev/ttyUSB7")]))
    started = []

    def fake_start(port):
        started.append(port)
        ctrl.state = C.STATE_CONNECTING        # samples arrive later, in the reader thread

    ctrl.start = fake_start
    out = svc.call("start_acquisition", {"wait_s": 3})
    assert started == ["/dev/ttyUSB7"]
    assert out["warning"] == "not streaming yet"
    assert out["waited_s"] == pytest.approx(3.0, abs=0.2)


def test_start_acquisition_returns_once_streaming(ctrl, svc, clock):
    def fake_start(port):
        ctrl.state = C.STATE_STREAMING
        feed(ctrl, 0.5)

    ctrl.start = fake_start
    out = svc.call("start_acquisition", {"port": "/dev/fake", "wait_s": 5})
    assert out["state"] == "streaming" and "warning" not in out
    assert out["samples"] > 0


def test_list_ports_marks_esp32_as_suggested(svc, monkeypatch):
    monkeypatch.setattr(PortDiscovery, "get_ports", staticmethod(
        lambda show_all=False: [("/dev/ttyS0", "/dev/ttyS0 — n/a"),
                                ("/dev/ttyUSB0", "/dev/ttyUSB0 — CP2102")]))
    monkeypatch.setattr(PortDiscovery, "find_esp32_ports", staticmethod(
        lambda: [SimpleNamespace(device="/dev/ttyUSB0")]))
    out = svc.call("list_ports", {})
    assert out["suggested"] == "/dev/ttyUSB0"
    assert out["ports"][1] == {"port": "/dev/ttyUSB0", "description": "CP2102"}


# ------------------------------------------------------------------ device

def test_get_device_without_connection(svc):
    out = svc.call("get_device", {})
    assert out["device_info"] is None and out["device_config"] is None
    assert out["calibration"]["shunt_ohm"] == pytest.approx(0.010)


def test_configure_device_sends_commands_and_waits_for_confirmation(ctrl, svc, clock):
    expect_error(svc, "configure_device", {"avg": 16}, "connected device")
    expect_error(svc, "configure_device", {}, "nothing to configure")

    reader = FakeReader()
    ctrl.reader = reader
    ctrl.protocol = PROTO_V2

    def send(text):
        reader.sent.append(text)
        if text == "SET AVG 16":          # the device confirms only the averaging
            ctrl.device_config = DeviceConfig(avg=16, vct_us=140, ict_us=140)

    reader.send_command = send
    try:
        out = svc.call("configure_device", {"avg": 16, "vct_us": 140, "oled": False,
                                            "save_default": True})
    finally:
        ctrl.reader = None
    assert reader.sent == ["SET AVG 16", "SET VCT 140", "SET OLED 0", "SAVE"]
    assert out["device_config"]["avg"] == 16
    assert "warning" in out                 # vct/oled were never confirmed by the fake device


def test_configure_device_confirmed(ctrl, svc, clock):
    reader = FakeReader()
    ctrl.reader = reader
    ctrl.protocol = PROTO_V2
    ctrl.device_config = DeviceConfig(avg=4, vct_us=140, ict_us=140)

    def send(text):
        reader.sent.append(text)
        ctrl.device_config = DeviceConfig(avg=64, vct_us=332, ict_us=332)

    reader.send_command = send
    try:
        out = svc.call("configure_device", {"avg": 64, "vct_us": 332, "ict_us": 332})
    finally:
        ctrl.reader = None
    assert reader.sent == ["SET AVG 64", "SET VCT 332", "SET ICT 332"]
    assert "warning" not in out and out["device_config"]["sample_rate_hz"] > 0


def test_send_command_and_sync_clock_without_device(svc):
    assert svc.call("send_command", {"command": "GET"}) == {"sent": False}
    assert svc.call("sync_clock", {}) == {"requested": False}


def test_wait_reports_waited_seconds(svc, clock):
    assert svc.call("wait", {"seconds": 2.5}) == {"waited_s": 2.5}
    assert clock.now == pytest.approx(2.5)


# ---------------------------------------------------------------- settings

def test_get_settings_returns_only_whitelisted_keys(svc):
    settings = svc.call("get_settings", {})["settings"]
    assert set(settings) == set(schema.SETTINGS_SCHEMA)


def test_update_settings_applies_saves_and_notifies(ctrl, clock):
    calls = []
    svc = make_service(ctrl, clock, on_changed=lambda: calls.append("gui"))
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    before = ctrl.calibration_changed_t
    out = svc.call("update_settings", {"settings": {"csv_separator": ";",
                                                    "shunt_ohm": 0.02}})
    assert ctrl.settings.csv_separator == ";"
    assert ctrl.settings.shunt_ohm == pytest.approx(0.02)
    assert out["settings"]["shunt_ohm"] == pytest.approx(0.02)
    assert calls == ["gui"]
    assert ctrl.calibration_changed_t != before        # calibration re-applied


def test_update_settings_range_checks(svc):
    expect_error(svc, "update_settings", {"settings": {"shunt_ohm": 500}}, "at most 100")
    expect_error(svc, "update_settings", {"settings": {"host_averaging": 0}}, "at least 1")


# ----------------------------------------------------------- JSON safety

def test_jsonable_handles_nan_inf_and_numpy_types(tmp_path: Path):
    @dataclasses.dataclass
    class Box:
        x: float
        y: np.ndarray

    raw = {
        "nan": float("nan"),
        "inf": np.float32("inf"),
        "f32": np.float32(1.5),
        "i64": np.int64(7),
        "flag": np.bool_(True),
        "arr": np.array([1.0, np.nan, 3.0], dtype=np.float32),
        "box": Box(x=np.float64("nan"), y=np.arange(2)),
        "path": tmp_path,
        "tuple": (1, np.int32(2)),
        1: "non-str key",
    }
    out = _jsonable(raw)
    assert out["nan"] is None and out["inf"] is None
    assert out["f32"] == 1.5 and type(out["f32"]) is float
    assert out["i64"] == 7 and type(out["i64"]) is int
    assert out["flag"] is True
    assert out["arr"] == [1.0, None, 3.0]
    assert out["box"] == {"x": None, "y": [0, 1]}
    assert out["path"] == str(tmp_path)
    assert out["tuple"] == [1, 2]
    assert out["1"] == "non-str key"
    import json
    json.dumps(out, allow_nan=False)        # must be strict JSON


def test_call_result_is_strict_json(ctrl, svc):
    import json
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    for name, params in (("get_statistics", {}), ("get_status", {}), ("get_samples", {})):
        json.dumps(svc.call(name, params), allow_nan=False)


# ------------------------------------------------------ error reporting

def _failing_start(ctrl, texts, level="error"):
    """A start that fails like a refused port: the app reports, then the acquisition stops."""
    def fake_start(port):
        for text in texts:
            ctrl.message.emit(level, text)
        ctrl.state = C.STATE_IDLE
    ctrl.start = fake_start


def test_app_messages_are_logged_and_last_ten_returned(ctrl, svc):
    assert svc.call("get_status", {})["messages"] == []
    for k in range(60):
        ctrl.message.emit("info", f"line {k}")
    msgs = svc.call("get_status", {})["messages"]
    assert [m["text"] for m in msgs] == [f"line {k}" for k in range(50, 60)]
    assert set(msgs[0]) == {"time", "level", "text"} and msgs[0]["level"] == "info"


def test_command_replies_are_logged(ctrl, svc):
    ctrl.command_reply.emit(False, "SET", "bad avg")
    msgs = svc.call("get_status", {})["messages"]
    assert msgs[-1]["level"] == "error"
    assert msgs[-1]["text"] == "device rejected SET: bad avg"


def test_service_without_controller_signals_still_constructs(clock):
    svc = ApiService(SimpleNamespace(), AppSettings(), DirectContext(), sleep=clock.sleep,
                     monotonic=clock.monotonic)
    assert svc.call("get_settings", {})["settings"]


def test_failed_start_raises_with_the_app_message(ctrl, svc):
    _failing_start(ctrl, ["Serial error on /dev/ttyACM0: [Errno 13] Permission denied: "
                          "'/dev/ttyACM0'"])
    expect_error(svc, "start_acquisition", {"port": "/dev/ttyACM0", "wait_s": 5},
                 r"acquisition failed to start: Serial error on /dev/ttyACM0: "
                 r"\[Errno 13\] Permission denied")


def test_failed_start_without_message_uses_generic_text(ctrl, svc):
    _failing_start(ctrl, [])
    expect_error(svc, "start_acquisition", {"port": "/dev/ttyACM0"},
                 "acquisition failed to start: the app reported no reason")


def test_failed_start_only_reports_messages_from_this_call(ctrl, svc):
    ctrl.message.emit("error", "old problem from an earlier start")
    _failing_start(ctrl, ["new problem"])
    expect_error(svc, "start_acquisition", {"port": "/dev/x"},
                 "acquisition failed to start: new problem$")


def test_still_connecting_at_timeout_is_a_warning_not_an_error(ctrl, svc, clock):
    def fake_start(port):
        ctrl.state = C.STATE_CONNECTING
    ctrl.start = fake_start
    out = svc.call("start_acquisition", {"port": "/dev/x", "wait_s": 2})
    assert out["warning"] == "not streaming yet" and out["running"] is True


def test_failed_start_does_not_report_a_recording_file(ctrl, svc, tmp_path: Path):
    missing = tmp_path / "gone.csv"                 # the writer's file does not exist

    def fake_start(port):
        ctrl.writer = SimpleNamespace(path=missing, is_open=True, error=None)
        ctrl.message.emit("error", "Permission denied")
        ctrl.writer = None
        ctrl.state = C.STATE_IDLE
    ctrl.start = fake_start
    expect_error(svc, "start_acquisition", {"port": "/dev/x"}, "Permission denied")
    s = svc.call("get_status", {})
    assert s["recording_path"] is None and s["recording_to_disk"] is False


def test_configure_device_reports_a_rejected_command(ctrl, svc, clock):
    reader = FakeReader()
    ctrl.reader = reader
    ctrl.protocol = PROTO_V2

    def send(text):
        reader.sent.append(text)
        if text == "SET AVG 16":
            ctrl.command_reply.emit(False, "SET", "bad value")

    reader.send_command = send
    try:
        expect_error(svc, "configure_device", {"avg": 16}, "device rejected 'SET': bad value")
    finally:
        ctrl.reader = None


def test_send_command_includes_the_firmware_reply(ctrl, svc):
    reader = FakeReader()
    ctrl.reader = reader

    def send(text):
        reader.sent.append(text)
        ctrl.command_reply.emit(True, text, "")

    reader.send_command = send
    try:
        out = svc.call("send_command", {"command": "SET STREAM 0"})
    finally:
        ctrl.reader = None
    assert out == {"sent": True, "reply": {"ok": True, "cmd": "SET STREAM 0", "msg": ""}}


def test_send_command_reports_a_rejection_without_failing(ctrl, svc):
    reader = FakeReader()
    ctrl.reader = reader

    def send(text):
        reader.sent.append(text)
        ctrl.command_reply.emit(False, "SET", "unknown command")

    reader.send_command = send
    try:
        out = svc.call("send_command", {"command": "SET BOGUS"})
    finally:
        ctrl.reader = None
    assert out["sent"] is True
    assert out["reply"] == {"ok": False, "cmd": "SET", "msg": "unknown command"}


def test_send_command_waits_at_most_one_second_for_a_reply(ctrl, svc, clock):
    reader = FakeReader()
    ctrl.reader = reader
    try:
        out = svc.call("send_command", {"command": "GET"})
    finally:
        ctrl.reader = None
    assert reader.sent == ["GET"]
    assert out == {"sent": True}
    assert 1.0 <= clock.now <= 1.1


def _device_reply_reader(ctrl, emit):
    """A fake reader whose send_command makes the device answer through `emit`."""
    reader = FakeReader()
    ctrl.reader = reader

    def send(text):
        reader.sent.append(text)
        emit(text)

    reader.send_command = send
    return reader


def test_send_command_get_returns_the_parsed_config(ctrl, svc, clock):
    _device_reply_reader(ctrl, lambda text: ctrl.device_config_received.emit(
        DeviceConfig(avg=4, vct_us=140, ict_us=140)))
    try:
        out = svc.call("send_command", {"command": "GET"})
    finally:
        ctrl.reader = None
    assert out["sent"] is True
    assert out["reply"]["type"] == "config"
    cfg = out["reply"]["config"]
    assert cfg["avg"] == 4 and cfg["vct_us"] == 140 and cfg["ict_us"] == 140
    assert cfg["sample_rate_hz"] > 0
    assert clock.now < 0.5          # returned as soon as the config arrived


def test_send_command_set_returns_the_config_that_follows_it(ctrl, svc):
    _device_reply_reader(ctrl, lambda text: ctrl.device_config_received.emit(
        DeviceConfig(avg=16, vct_us=332, ict_us=332)))
    try:
        out = svc.call("send_command", {"command": "SET AVG 16"})
    finally:
        ctrl.reader = None
    assert out["reply"]["type"] == "config" and out["reply"]["config"]["avg"] == 16


def test_send_command_hello_returns_device_info(ctrl, svc):
    _device_reply_reader(ctrl, lambda text: ctrl.device_info_received.emit(
        DeviceInfo("EdgePowerMeter", "2.0.0", 2)))
    try:
        out = svc.call("send_command", {"command": "HELLO"})
    finally:
        ctrl.reader = None
    assert out == {"sent": True, "reply": {"type": "hello", "name": "EdgePowerMeter",
                                           "firmware": "2.0.0", "protocol": 2}}


def test_send_command_ignores_config_received_before_the_command(ctrl, svc, clock):
    ctrl.device_config_received.emit(DeviceConfig(avg=4))     # an older reply
    _device_reply_reader(ctrl, lambda text: None)             # device stays silent
    try:
        out = svc.call("send_command", {"command": "GET"})
    finally:
        ctrl.reader = None
    assert out == {"sent": True}
    assert 1.0 <= clock.now <= 1.1


def test_send_command_other_verbs_do_not_take_config_replies(ctrl, svc, clock):
    _device_reply_reader(ctrl, lambda text: ctrl.device_config_received.emit(DeviceConfig()))
    try:
        out = svc.call("send_command", {"command": "SYNC"})
    finally:
        ctrl.reader = None
    assert out == {"sent": True}


def test_send_command_command_reply_still_wins_over_config(ctrl, svc):
    def send(text):
        ctrl.command_reply.emit(False, "SET", "bad avg")
        ctrl.device_config_received.emit(DeviceConfig())

    _device_reply_reader(ctrl, send)
    try:
        out = svc.call("send_command", {"command": "SET AVG 3"})
    finally:
        ctrl.reader = None
    assert out["reply"] == {"ok": False, "cmd": "SET", "msg": "bad avg"}


def test_empty_recording_says_no_data_for_every_range_tool(ctrl, svc, rec: Path):
    expect_error(svc, "get_statistics", {}, "no data: start an acquisition or import a CSV")
    expect_error(svc, "get_samples", {}, "no data: start an acquisition")
    expect_error(svc, "benchmark", {"units": 10}, "no data: start an acquisition")
    expect_error(svc, "export_csv", {"path": str(rec / "a.csv")}, "no data:")
    assert not (rec / "a.csv").exists()


def test_t0_t1_order_is_rejected_even_without_data(ctrl, svc):
    expect_error(svc, "get_statistics", {"t0": 5.0, "t1": 1.0}, "t1 must be greater than t0")
    expect_error(svc, "get_samples", {"t0": 2.0, "t1": 2.0}, "t1 must be greater than t0")


def test_marker_range_is_swapped_when_reversed(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    a = svc.call("add_marker", {"label": "a", "t": 2.0})
    b = svc.call("add_marker", {"label": "b", "t": 6.0})
    fwd = svc.call("get_statistics", {"marker_from": a["id"], "marker_to": b["id"]})
    rev = svc.call("get_statistics", {"marker_from": b["id"], "marker_to": a["id"]})
    assert rev["range"]["t0"] == 2.0 and rev["range"]["t1"] == 6.0
    assert rev["stats"] == fwd["stats"]


# ------------------------------------------------ review fixes: measure, import, nulls, close

def test_measure_fails_when_data_is_cleared_during_the_window(ctrl, svc, clock):
    feed(ctrl, 5.0)
    ctrl.state = C.STATE_STREAMING
    cleared = []

    def clear_once(seconds):
        feed(ctrl, seconds)
        if not cleared and clock.now > 1.0:
            cleared.append(True)
            ctrl.clear()                       # restarts the time axis at 0
            feed(ctrl, 1.0)

    clock.on_sleep = clear_once
    expect_error(svc, "measure", {"duration_s": 3.0, "label": "w"},
                 "the data was cleared during the measurement")


def test_measure_fails_when_its_start_marker_is_removed(ctrl, svc, clock):
    feed(ctrl, 5.0)
    ctrl.state = C.STATE_STREAMING

    def drop_start_marker(seconds):
        feed(ctrl, seconds)
        for m in ctrl.markers.sorted():
            if m.label == "w":
                ctrl.remove_marker(m.id)

    clock.on_sleep = drop_start_marker
    expect_error(svc, "measure", {"duration_s": 2.0, "label": "w"},
                 "start marker was removed")


def test_measure_unaffected_by_clear_before_it_starts(ctrl, svc, clock):
    feed(ctrl, 5.0)
    ctrl.clear()                                   # before the window: fine
    feed(ctrl, 5.0)
    ctrl.state = C.STATE_STREAMING
    clock.on_sleep = lambda s: feed(ctrl, s)
    out = svc.call("measure", {"duration_s": 2.0, "label": "ok"})
    assert out["stats"]["power_avg"] == pytest.approx(1.0, rel=1e-3)


def test_import_of_a_malformed_csv_is_a_user_error(ctrl, svc, rec: Path):
    big = "x" * 200_000                            # over csv's default 128 KiB field limit
    path = rec / "big.csv"
    path.write_text("Timestamp,RelativeTime[s],Voltage[V],Current[A],Power[W]\n"
                    f"2024-01-01 00:00:00.000,0.0,{big},0.1,0.5\n"
                    "2024-01-01 00:00:01.000,1.0,5.0,0.1,0.5\n"
                    "2024-01-01 00:00:02.000,2.0,5.0,0.1,0.5\n", encoding="utf-8")
    expect_error(svc, "import_csv", {"path": str(path)}, "malformed CSV at line 2")


def test_import_with_nul_bytes_is_a_user_error_or_loads(ctrl, svc, rec: Path):
    path = rec / "nul.csv"
    path.write_bytes(b"Timestamp,V,I,P\n2024-01-01 00:00:00,5.0,0.1,\x000.5\n"
                     b"2024-01-01 00:00:01,5.0,0.1,0.5\n")
    try:
        svc.call("import_csv", {"path": str(path)})
    except ApiError:
        pass                                       # a clean user error, never a crash


def test_explicit_null_for_an_optional_property_means_absent():
    schema_ = TOOLS_BY_NAME["get_samples"].input_schema
    assert _validate(schema_, {"t0": None, "max_points": None}) == {"max_points": 2000}
    out = _validate(TOOLS_BY_NAME["measure"].input_schema, {"duration_s": 2, "label": None})
    assert out["label"] == "measurement"


def test_null_still_rejected_for_required_and_unknown_keys():
    measure = TOOLS_BY_NAME["measure"].input_schema
    with pytest.raises(ApiError, match="duration_s must be a number"):
        _validate(measure, {"duration_s": None})
    with pytest.raises(ApiError, match="missing required 'duration_s'"):
        _validate(measure, {})
    with pytest.raises(ApiError, match="unknown key 'bogus'"):
        _validate(measure, {"duration_s": 1, "bogus": None})


def test_array_min_and_max_items_are_enforced():
    arr = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 3}
    assert _validate(arr, [1, 2]) == [1, 2]
    assert _validate(arr, [1, 2, 3]) == [1, 2, 3]
    with pytest.raises(ApiError, match=r"^values needs at least 2 item\(s\)$"):
        _validate(arr, [1], "values")
    with pytest.raises(ApiError, match=r"^values must have at most 3 items$"):
        _validate(arr, [1, 2, 3, 4], "values")
    one = {"type": "array", "items": {"type": "string"}, "maxItems": 1}
    with pytest.raises(ApiError, match="must have at most 1 item$"):
        _validate(one, ["a", "b"], "tags")


def test_array_unique_items_rejects_repeats_with_the_index():
    arr = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    assert _validate(arr, ["a", "b"]) == ["a", "b"]
    with pytest.raises(ApiError, match=r"^tags\[2\] duplicates tags\[0\]"):
        _validate(arr, ["a", "b", "a"], "tags")
    row = {"type": "object", "properties": {"x": {"type": "integer"},
                                            "y": {"type": "integer"}}}
    objs = {"type": "array", "items": row, "uniqueItems": True}
    assert _validate(objs, [{"x": 1, "y": 2}, {"x": 2, "y": 2}]) == [{"x": 1, "y": 2},
                                                                   {"x": 2, "y": 2}]
    with pytest.raises(ApiError, match="duplicates"):
        _validate(objs, [{"x": 1, "y": 2}, {"y": 2, "x": 1}], "rows")


def test_unique_items_compares_converted_values():
    nums = {"type": "array", "items": {"type": "number"}, "uniqueItems": True}
    with pytest.raises(ApiError, match="duplicates"):
        _validate(nums, [1, 1.0], "xs")            # both are the number 1.0
    ints = {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}
    assert _validate(ints, [1, 2]) == [1, 2]


def test_array_without_the_keywords_keeps_any_length():
    free = {"type": "array", "items": {"type": "integer"}}
    assert _validate(free, []) == []
    assert _validate(free, list(range(500))) == list(range(500))
    assert _validate(free, [1, 1, 1]) == [1, 1, 1]  # duplicates are fine without uniqueItems


def test_nested_object_with_additional_properties_false_rejects_unknown_keys():
    inner = {"type": "object", "properties": {"x": {"type": "number"}},
             "additionalProperties": False}
    outer = {"type": "object", "properties": {"cfg": inner}}
    assert _validate(outer, {"cfg": {"x": 1}}) == {"cfg": {"x": 1.0}}
    with pytest.raises(ApiError, match="unknown key 'y' in cfg \\(allowed: x\\)"):
        _validate(outer, {"cfg": {"x": 1, "y": 2}})
    with pytest.raises(ApiError, match="unknown key 'z' in cfg"):
        _validate(outer, {"cfg": {"x": 1, "z": None}})     # a null unknown key is still unknown


def test_nested_object_without_the_keyword_drops_unknown_keys_as_before():
    inner = {"type": "object", "properties": {"x": {"type": "number"}}}
    outer = {"type": "object", "properties": {"cfg": inner}}
    assert _validate(outer, {"cfg": {"x": 1, "y": 2}}) == {"cfg": {"x": 1.0}}


def test_nested_additional_properties_check_applies_to_array_items():
    row = {"type": "object", "properties": {"a": {"type": "integer"}},
           "additionalProperties": False}
    rows = {"type": "array", "items": row}
    with pytest.raises(ApiError, match=r"unknown key 'b' in rows\[1\]"):
        _validate(rows, [{"a": 1}, {"a": 2, "b": 3}], "rows")


def test_closed_service_ignores_controller_signals(ctrl, svc):
    ctrl.message.emit("info", "before close")
    assert [e["text"] for e in svc._since(0)] == ["before close"]
    svc.close()
    ctrl.message.emit("info", "after close")
    ctrl.data_reset.emit()
    assert [e["text"] for e in svc._since(0)] == ["before close"]
    svc.close()                                    # closing twice is harmless


# ------------------------------------------------- file access: export roots (H1, L1)

def _outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir(exist_ok=True)
    return folder


def test_export_refuses_files_outside_the_allowed_folders(ctrl, svc, rec: Path, tmp_path: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    victim = _outside(tmp_path) / "keep.csv"
    victim.write_text("keep me\n", encoding="utf-8")
    expect_error(svc, "export_csv", {"path": str(victim), "overwrite": True},
                 "outside the allowed folders")
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    bashrc = _outside(tmp_path) / ".bashrc"
    expect_error(svc, "export_csv", {"path": str(bashrc), "overwrite": True}, "must end in .csv")
    assert not bashrc.exists()
    traversal = str(rec / "sub" / ".." / ".." / "outside" / "x.pdf")
    expect_error(svc, "export_pdf", {"path": traversal}, "outside the allowed folders")
    assert not (tmp_path / "outside" / "x.pdf").exists()


def test_export_refusal_lists_the_allowed_folders(ctrl, svc, rec: Path, tmp_path: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    with pytest.raises(ApiError) as info:
        svc.call("export_csv", {"path": str(_outside(tmp_path) / "x.csv")})
    assert str(rec.resolve()) in str(info.value)


@pytest.mark.parametrize("name", ["x.txt", "x.sh", "x", ".csv", "x.csv.exe", "x.pdf"])
def test_export_csv_requires_the_csv_extension(ctrl, svc, rec: Path, name):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "export_csv", {"path": str(rec / name)}, "must end in .csv")
    assert not any(rec.iterdir())


@pytest.mark.parametrize("name", ["x.csv", "x.txt", "x.PDF.exe"])
def test_export_pdf_requires_the_pdf_extension(ctrl, svc, rec: Path, name):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "export_pdf", {"path": str(rec / name)}, "must end in .pdf")
    assert not any(rec.iterdir())


def test_extension_check_is_case_insensitive(ctrl, svc, rec: Path):
    pytest.importorskip("reportlab")
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    out = svc.call("export_csv", {"path": str(rec / "UPPER.CSV")})
    assert Path(out["path"]).name == "UPPER.CSV" and Path(out["path"]).is_file()
    out = svc.call("export_pdf", {"path": str(rec / "Report.Pdf")})
    assert Path(out["path"]).read_bytes()[:4] == b"%PDF"


def test_relative_path_is_taken_from_the_recordings_folder(ctrl, svc, rec: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    (rec / "run1").mkdir()
    out = svc.call("export_csv", {"path": "run1/out.csv"})
    assert Path(out["path"]) == (rec / "run1" / "out.csv").resolve()
    assert (rec / "run1" / "out.csv").is_file()
    st = svc.call("import_csv", {"path": "run1/out.csv", "discard_unsaved": True})
    assert st["samples"] == 1001


def test_extra_export_folders_come_from_the_environment(ctrl, svc, rec: Path, tmp_path: Path,
                                                        monkeypatch):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    extra = _outside(tmp_path)
    monkeypatch.setenv(schema.EXPORT_DIRS_ENV,
                       os.pathsep.join([str(extra), "relative/folder"]))
    out = svc.call("export_csv", {"path": str(extra / "ok.csv")})
    assert (extra / "ok.csv").is_file() and out["path"].startswith(str(extra.resolve()))
    settings = svc.call("get_settings", {})
    assert str(extra.resolve()) in settings["export_roots"]
    assert str(rec.resolve()) == settings["recordings_folder"]
    assert not any("relative" in r for r in settings["export_roots"])


def test_relative_entries_in_export_dirs_are_ignored(ctrl, svc, rec: Path, monkeypatch):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    monkeypatch.setenv(schema.EXPORT_DIRS_ENV, "not/absolute")
    assert svc.call("get_settings", {})["export_roots"] == [str(rec.resolve())]


@pytest.mark.skipif(os.name != "posix", reason="symbolic links")
def test_export_never_writes_through_a_symlink(ctrl, svc, rec: Path, tmp_path: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    target = _outside(tmp_path) / "target.csv"
    target.write_text("keep me\n", encoding="utf-8")
    link = rec / "out.csv"
    link.symlink_to(target)
    expect_error(svc, "export_csv", {"path": str(link), "overwrite": True}, "symbolic link")
    assert target.read_text(encoding="utf-8") == "keep me\n" and link.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="symbolic links")
def test_symlinked_folder_cannot_lead_outside_the_roots(ctrl, svc, rec: Path, tmp_path: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    (rec / "jump").symlink_to(_outside(tmp_path), target_is_directory=True)
    expect_error(svc, "export_csv", {"path": str(rec / "jump" / "x.csv")},
                 "outside the allowed folders")
    assert not (_outside(tmp_path) / "x.csv").exists()


def test_import_failures_give_one_uniform_message(ctrl, svc, rec: Path, tmp_path: Path):
    outside = _outside(tmp_path) / "secret.csv"
    outside.write_text("Timestamp,V,I,P\n", encoding="utf-8")
    (rec / "notes.txt").write_text("Timestamp,V,I,P\n", encoding="utf-8")
    (rec / "a_folder.csv").mkdir()
    cases = [str(outside), str(rec / "missing.csv"), str(rec / "notes.txt"),
             str(rec / "a_folder.csv"), str(rec / ".." / "outside" / "secret.csv"),
             "missing.csv"]
    if os.name == "posix":
        (rec / "linked.csv").symlink_to(outside)
        cases.append(str(rec / "linked.csv"))
    messages = set()
    for raw in cases:
        with pytest.raises(ApiError) as info:
            svc.call("import_csv", {"path": raw})
        messages.add(str(info.value))
    assert messages == {"file not found or not allowed"}


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                    reason="needs a non-root POSIX user to be unable to read a file")
def test_unreadable_import_gives_the_same_message(ctrl, svc, rec: Path):
    path = rec / "locked.csv"
    path.write_text("Timestamp,V,I,P\n", encoding="utf-8")
    path.chmod(0)
    try:
        expect_error(svc, "import_csv", {"path": str(path)}, "^file not found or not allowed$")
    finally:
        path.chmod(0o644)


def test_recordings_dir_is_not_settable_by_the_api(svc, rec: Path, tmp_path: Path):
    assert "recordings_dir" not in TOOLS_BY_NAME["update_settings"].input_schema[
        "properties"]["settings"]["properties"]
    expect_error(svc, "update_settings", {"settings": {"recordings_dir": str(tmp_path)}},
                 "unknown key 'recordings_dir'")
    assert "recordings_dir" not in svc.call("get_settings", {})["settings"]


# ------------------------------------------------------------- text and sizes (M3)

def test_lone_surrogates_are_rejected_as_text(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    expect_error(svc, "add_marker", {"label": "bad\ud800", "t": 1.0}, "valid UTF-8")
    ok = svc.call("add_marker", {"label": "fine", "t": 1.0})
    expect_error(svc, "update_marker", {"id": ok["id"], "label": "x\udfff"}, "valid UTF-8")
    expect_error(svc, "export_pdf", {"path": "x.pdf", "title": "t\ud800"}, "valid UTF-8")
    expect_error(svc, "update_settings", {"settings": {"report_notes": "n\ud800"}},
                 "valid UTF-8")
    expect_error(svc, "send_command", {"command": "GET\ud800"}, "valid UTF-8")
    assert [m["label"] for m in svc.call("list_markers", {})["markers"]] == ["fine"]


def test_marker_label_length_is_limited(ctrl, svc):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    svc.call("add_marker", {"label": "a" * 60, "t": 1.0})
    expect_error(svc, "add_marker", {"label": "a" * 61, "t": 2.0}, "at most 60")
    expect_error(svc, "update_marker", {"id": 1, "label": "a" * 61}, "at most 60")


def test_free_text_settings_have_a_length_limit():
    schema_ = TOOLS_BY_NAME["update_settings"].input_schema["properties"]["settings"]
    assert schema_["properties"]["report_title"]["maxLength"] == 2000
    assert schema_["properties"]["report_notes"]["maxLength"] == 2000
    _validate(schema_, {"report_title": "A" * 2000})
    with pytest.raises(ApiError, match="at most 2000"):
        _validate(schema_, {"report_title": "A" * 2001})
    pdf = TOOLS_BY_NAME["export_pdf"].input_schema
    with pytest.raises(ApiError, match="at most 2000"):
        _validate(pdf, {"path": "/x.pdf", "title": "A" * 2001})
    with pytest.raises(ApiError, match="at most 2000"):
        _validate(pdf, {"path": "/x.pdf", "notes": "A" * 2001})


def test_out_of_range_numbers_are_a_user_error(svc):
    expect_error(svc, "wait", {"seconds": 10 ** 400}, "out of range")
    expect_error(svc, "wait", {"seconds": 1e308}, "at most 3600")


# ----------------------------------------------------- send_command allowlist (L3)

def test_send_command_forwards_only_the_allowed_verbs(ctrl, svc):
    reader = FakeReader()
    ctrl.reader = reader
    try:
        for line in ["HELLO", "GET", "SET STREAM 0", "SYNC 2026-01-01 00:00:00", "  get  "]:
            assert svc.call("send_command", {"command": line})["sent"] is True
    finally:
        ctrl.reader = None
    assert reader.sent == ["HELLO", "GET", "SET STREAM 0", "SYNC 2026-01-01 00:00:00", "get"]


@pytest.mark.parametrize("line, match", [
    ("SAVE", "configure_device"),
    ("save", "configure_device"),
    ("MODE RAW", "managed by the app"),
    ("SET MODE CSV", "managed by the app"),
    ("set mode csv", "managed by the app"),
    ("FLASH", "'FLASH' is not allowed"),
    ("BOGUS 1", "'BOGUS' is not allowed"),
    ("GET\x00", "control characters"),
    ("GET\x1b[31m", "control characters"),
    ("GET\tAVG", "control characters"),
    ("GET\x7f", "control characters"),
    ("GET\nSAVE", "single line"),
])
def test_send_command_refuses_disallowed_commands(ctrl, svc, line, match):
    reader = FakeReader()
    ctrl.reader = reader
    try:
        expect_error(svc, "send_command", {"command": line}, match)
    finally:
        ctrl.reader = None
    assert reader.sent == []


# ------------------------------------------------------- hardening: ports, overwrite, settings

@pytest.mark.parametrize("port", ["/dev/null", "/etc/passwd", "/dev/tty", "/dev/ttyACM9",
                                  "relative", "/dev/ttyUSB7/../null"])
def test_start_acquisition_refuses_ports_discovery_does_not_list(ctrl, svc, port):
    started = []
    ctrl.start = lambda p: started.append(p)
    expect_error(svc, "start_acquisition", {"port": port},
                 re.escape(f"unknown port {port!r} (use list_ports)"))
    assert started == []


def test_start_acquisition_accepts_show_all_ports(ctrl, svc, monkeypatch):
    monkeypatch.setattr(PortDiscovery, "get_ports", staticmethod(
        lambda show_all=False: [("/dev/ttyUSB7", "x")]
        + ([("/dev/ttyS1", "/dev/ttyS1 — n/a")] if show_all else [])))
    started = []

    def fake_start(port):
        started.append(port)
        ctrl.state = C.STATE_STREAMING
        feed(ctrl, 0.5)

    ctrl.start = fake_start
    svc.call("start_acquisition", {"port": "/dev/ttyS1", "wait_s": 1})
    assert started == ["/dev/ttyS1"]


def _write_source_csv(ctrl, svc, rec: Path) -> Path:
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    src = rec / "src.csv"
    svc.call("export_csv", {"path": str(src)})
    return src


def test_overwrite_refuses_the_file_the_data_was_imported_from(ctrl, svc, rec: Path):
    src = _write_source_csv(ctrl, svc, rec)
    before = src.read_bytes()
    svc.call("clear_data", {})
    svc.call("import_csv", {"path": str(src)})
    expect_error(svc, "export_csv", {"path": str(src), "overwrite": True},
                 "loaded from this file")
    assert src.read_bytes() == before                  # the source is untouched


def test_overwrite_refuses_a_file_with_the_name_of_the_imported_recording(
        ctrl, svc, rec: Path, tmp_path: Path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(schema.EXPORT_DIRS_ENV, str(other))
    _write_source_csv(ctrl, svc, rec)
    svc.call("clear_data", {})
    svc.call("import_csv", {"path": str(rec / "src.csv")})
    copy = other / "src.csv"
    copy.write_text("keep me", encoding="utf-8")
    expect_error(svc, "export_csv", {"path": str(copy), "overwrite": True},
                 "loaded from a file with this name")
    assert copy.read_text(encoding="utf-8") == "keep me"


def test_overwrite_refuses_the_live_recording_file_csv_and_pdf(ctrl, svc, rec: Path):
    ctrl.load(steps([(10, 5.0, 0.2)]), "x")
    live_csv, live_pdf = rec / "live.csv", rec / "live.pdf"
    live_csv.write_text("recording", encoding="utf-8")
    live_pdf.write_text("recording", encoding="utf-8")
    ctrl.writer = SimpleNamespace(path=live_csv, is_open=True, error=None)
    try:
        expect_error(svc, "export_csv", {"path": str(live_csv), "overwrite": True},
                     "live recording file")
        ctrl.writer = SimpleNamespace(path=live_pdf, is_open=True, error=None)
        expect_error(svc, "export_pdf", {"path": str(live_pdf), "overwrite": True},
                     "live recording file")
    finally:
        ctrl.writer = None
    assert live_csv.read_text(encoding="utf-8") == "recording"


def test_overwrite_is_allowed_once_the_loaded_data_is_gone(ctrl, svc, rec: Path):
    src = _write_source_csv(ctrl, svc, rec)
    svc.call("clear_data", {})
    svc.call("import_csv", {"path": str(src)})
    svc.call("clear_data", {"discard_unsaved": True})
    ctrl.load(steps([(10, 5.0, 0.2)]), "other")       # other data, not this file
    out = svc.call("export_csv", {"path": str(src), "overwrite": True})
    assert out["path"] == str(src)


def test_offset_limits_match_the_app_and_the_schema():
    from app.core.settings import OFFSET_LIMIT
    assert schema.OFFSET_LIMIT == OFFSET_LIMIT == 100.0
    for key in ("voltage_offset_v", "current_offset_a"):
        assert schema.SETTINGS_SCHEMA[key]["minimum"] == -OFFSET_LIMIT
        assert schema.SETTINGS_SCHEMA[key]["maximum"] == OFFSET_LIMIT


@pytest.mark.parametrize("key", ["voltage_offset_v", "current_offset_a"])
@pytest.mark.parametrize("value, match", [(150, "at most 100"), (-100.5, "at least -100")])
def test_offsets_are_range_checked(svc, key, value, match):
    expect_error(svc, "update_settings", {"settings": {key: value}}, match)


def test_update_settings_reports_changed_keys_and_validation_adjustments(ctrl, clock):
    svc = make_service(ctrl, clock)
    ctrl.settings.voltage_gain = 9.0                   # out of range before this update
    out = svc.call("update_settings", {"settings": {"csv_separator": ";"}})
    assert out["applied"] == ["csv_separator"]
    assert "csv_separator" in out["changed"] and "voltage_gain" in out["changed"]
    assert out["adjusted"] == [{"key": "voltage_gain", "requested": 9.0, "stored": 1.0}]
    assert out["settings"]["voltage_gain"] == 1.0


def test_update_settings_without_adjustments_reports_none(ctrl, clock):
    svc = make_service(ctrl, clock)
    out = svc.call("update_settings", {"settings": {"voltage_offset_v": 2.5}})
    assert out["applied"] == ["voltage_offset_v"] and out["changed"] == ["voltage_offset_v"]
    assert out["adjusted"] == []


@pytest.mark.parametrize("text", ["ab\x07c", "ab\x7fc", "ab\r\nc", "ab\x1bc", "ab\u202ec",
                                  "ab\u2066c", "ab\u2069c", "ab\u202ac"])
def test_control_and_bidi_characters_are_refused_in_text_settings(svc, text):
    for key in ("report_title", "report_notes"):
        expect_error(svc, "update_settings", {"settings": {key: text}},
                     "control characters or bidi override")


@pytest.mark.parametrize("text", ["ab\nc", "ab\tc", "line1\n\tline2\n"])
def test_newline_and_tab_allowed_in_report_notes_only(svc, text):
    out = svc.call("update_settings", {"settings": {"report_notes": text}})
    assert out["settings"]["report_notes"] == text
    expect_error(svc, "update_settings", {"settings": {"report_title": text}},
                 "control characters or bidi override")


def test_plain_unicode_text_settings_are_accepted(svc):
    out = svc.call("update_settings", {"settings": {"report_title": "Lauf – Ω café 測試"}})
    assert out["settings"]["report_title"] == "Lauf – Ω café 測試"


def test_tool_descriptions_carry_the_new_guidance():
    upd = TOOLS_BY_NAME["update_settings"].description
    for word in ("shunt_ohm", "autosave", "calibration", "adjusted", "Report"):
        assert word in upd
    for name in ("export_csv", "export_pdf"):
        assert ("overwrite replaces the whole file even if the export covers only part of the "
                "data") in TOOLS_BY_NAME[name].description
    assert "list_ports" in TOOLS_BY_NAME["start_acquisition"].description
    assert "ASCII" in TOOLS_BY_NAME["send_command"].description


@pytest.mark.parametrize("line, match", [
    ("GЕT", "ASCII"),             # Cyrillic E: would look like GET to a reader
    ("SAVE\u200b", "ASCII"),      # zero-width space after a refused verb
    ("GET é", "ASCII"),
    ("GET\u00a0AVG", "ASCII"),
])
def test_send_command_refuses_non_ascii_before_the_verb_check(ctrl, svc, line, match):
    reader = FakeReader()
    ctrl.reader = reader
    try:
        expect_error(svc, "send_command", {"command": line}, match)
    finally:
        ctrl.reader = None
    assert reader.sent == []


def test_send_command_is_rate_limited_to_20_per_second(ctrl, svc, clock, monkeypatch):
    reader = FakeReader()
    ctrl.reader = reader
    monkeypatch.setattr(svc, "_await_device_reply", lambda *a, **k: None)
    try:
        for _ in range(20):
            assert svc.call("send_command", {"command": "GET"}) == {"sent": True}
        with pytest.raises(RateLimited, match="at most 20 per second"):
            svc.call("send_command", {"command": "GET"})
        assert len(reader.sent) == 20
        clock.now += 1.0                                 # the window slides
        assert svc.call("send_command", {"command": "GET"}) == {"sent": True}
    finally:
        ctrl.reader = None
