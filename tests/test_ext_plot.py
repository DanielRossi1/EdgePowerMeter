"""The `plot` extension: render_plot (numpy rasterizer, PNG output, MCP image block).

The handler is driven through a fake service (no Qt) for pixel checks, and through the real
ApiService (skipped without PySide6) for argument validation.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import time
import types
import urllib.parse
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from app.api import ext, mcp_bridge, schema
from app.api.ext import plot, plot_schema
from app.api.mcp_bridge import McpBridge, Target
from app.core.samples import Samples

PNG_SIG = b"\x89PNG\r\n\x1a\n"
POWER_TINT = plot._tint(plot.COLORS["power"])


class FakeService:
    """Stands in for ApiService: range_samples and the marker list."""

    def __init__(self, data, markers=()):
        self.data = data
        self.markers = list(markers)     # (id, t) pairs

    def range_samples(self, params):
        if not len(self.data):
            return 0.0, 1.0, "all data", self.data
        return float(self.data.t[0]), float(self.data.t[-1]), "all data", self.data

    def call(self, name, params):
        assert name == "list_markers"
        return {"markers": [{"id": i, "t": t, "label": ""} for i, t in self.markers]}


def render(data, markers=(), **args):
    handler = plot.handlers(FakeService(data, markers))["render_plot"]
    return handler(**args)


def decode_png(png: bytes):
    """(width, height, rgb array); checks the signature, CRCs and filter bytes."""
    assert png[:8] == PNG_SIG
    pos, chunks, idat = 8, {}, b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        assert zlib.crc32(tag + data) & 0xFFFFFFFF == crc
        if tag == b"IDAT":
            idat += data
        else:
            chunks[tag] = data
        pos += 12 + length
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB",
                                                                       chunks[b"IHDR"])
    assert (depth, ctype, comp, filt, interlace) == (8, 2, 0, 0, 0)
    rows = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, 1 + 3 * width)
    assert (rows[:, 0] == 0).all()
    return width, height, rows[:, 1:].reshape(height, width, 3)


def samples_of(t, p, v=None, i=None):
    t = np.asarray(t, dtype=np.float64)
    zeros = np.zeros(t.size)
    return Samples.from_columns(t, t, zeros if v is None else v, zeros if i is None else i, p)


def frame_columns(rgb, row):
    return np.nonzero((rgb[row] == plot.FRAME).all(axis=-1))[0]


def column_count(rgb, x, color):
    return int((rgb[:, x] == np.array(color, dtype=np.uint8)).all(axis=-1).sum())


# ------------------------------------------------------------ PNG and layout

def test_png_signature_and_ihdr_dimensions():
    t = np.linspace(0, 10, 5000)
    out = render(samples_of(t, np.sin(t) + 2), width=500, height=200)
    png = base64.b64decode(out["image"]["data_base64"], validate=True)
    assert png.startswith(PNG_SIG)
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (500, 200) == (out["width"], out["height"])
    assert out["image"]["mime_type"] == "image/png"


def test_default_size_and_stacked_panel_height():
    t = np.linspace(0, 10, 2000)
    one = render(samples_of(t, np.sin(t) + 2))
    assert (one["width"], one["height"]) == (900, 360)
    three = render(samples_of(t, np.sin(t) + 2, v=t, i=t),
                   quantities=["voltage", "current", "power"])
    assert (three["width"], three["height"]) == (900, 1080)
    assert set(three["per_quantity"]) == {"voltage", "current", "power"}


def test_decoded_pixels_background_and_envelope_colour():
    t = np.linspace(0, 10, 4000)
    out = render(samples_of(t, np.full(t.size, 3.0)), width=400, height=200)
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    assert tuple(rgb[0, 0]) == plot.WHITE and tuple(rgb[199, 399]) == plot.WHITE
    # the constant line sits in the plot: mean colour somewhere in the interior
    assert column_count(rgb, 200, plot.COLORS["power"]) == 1


def test_single_sample_spike_appears_as_coloured_column_at_its_x():
    n = 200_000
    t = np.linspace(0, 1000.0, n)
    p = np.full(n, 10.0)
    spike_index = 123_456
    p[spike_index] = 500.0
    out = render(samples_of(t, p), width=400, height=200)
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    frame = frame_columns(rgb, 100)
    left, right = int(frame[0]), int(frame[-1])
    pw = right - left - 1
    edges = np.linspace(t[0], t[-1], pw + 1)
    expected = left + 1 + min(int(np.searchsorted(edges, t[spike_index], side="right")) - 1,
                              pw - 1)
    # the spike column is painted from the spike down to the baseline: many envelope pixels
    tall = [x for x in range(left + 1, right) if column_count(rgb, x, POWER_TINT) > 100]
    assert tall == [expected]


def test_constant_signal_is_drawn_with_expanded_axis():
    t = np.linspace(0, 5, 1000)
    out = render(samples_of(t, np.full(t.size, 7.5)), width=300, height=160)
    assert out["per_quantity"]["power"] == {"min": 7.5, "max": 7.5, "mean": 7.5}
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    assert column_count(rgb, 150, plot.COLORS["power"]) == 1   # the mean line pixel
    zero = render(samples_of(t, np.zeros(t.size)), width=300, height=160)
    assert zero["per_quantity"]["power"]["max"] == 0.0


def test_markers_are_drawn_as_dashed_lines_at_their_time():
    t = np.linspace(0, 100.0, 20_000)
    p = np.full(t.size, 1.0)
    out = render(samples_of(t, p), markers=[(1, 25.0), (2, 80.0)], width=400, height=200)
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    frame = frame_columns(rgb, 100)
    left, right = int(frame[0]), int(frame[-1])
    pw = right - left - 1
    for mid, tm in [(1, 25.0), (2, 80.0)]:
        x = left + 1 + int(round(tm / 100.0 * (pw - 1)))
        assert column_count(rgb, x, plot.MARKER) >= 20, mid
        dashes = int((rgb[:, x] == 0).all(axis=-1).sum())
        assert dashes < 200                       # dashed, not solid
    hidden = render(samples_of(t, p), markers=[(1, 25.0)], show_markers=False,
                    width=400, height=200)
    _, _, rgb_off = decode_png(base64.b64decode(hidden["image"]["data_base64"]))
    assert not (rgb_off == 0).all(axis=-1).any()


def test_stacked_panels_share_one_left_margin_so_markers_line_up():
    # Panels whose y tick labels differ in width ("1.0" against "10000") used to get
    # different left margins, so the same time landed on different columns per panel.
    t = np.linspace(0, 100.0, 20_000)
    series = [(t, np.full(t.size, 1.0), "V", "voltage"),
              (t, np.linspace(0.0, 12345.0, t.size), "W", "power")]
    width, h = 400, 200
    rgb = plot.render_rgb(series, 0.0, 100.0, [(1, 25.0), (2, 80.0)], width, h)
    blocks = [rgb[k * h:(k + 1) * h] for k in range(len(series))]
    # the left frame is the column with the most frame pixels (y tick marks have only one)
    left = [int(np.argmax((b == plot.FRAME).all(axis=-1).sum(axis=0))) for b in blocks]
    assert left[0] == left[1]                                 # one common left margin
    fr = width - 9
    pw = fr - left[0] - 1
    expected = [left[0] + 1 + int(round(tm / 100.0 * (pw - 1))) for tm in (25.0, 80.0)]
    for block in blocks:
        lines = [x for x in range(width) if column_count(block, x, plot.MARKER) >= 20]
        assert lines == expected                              # same two columns in every panel


def test_markers_outside_the_range_are_ignored():
    t = np.linspace(0, 10.0, 2000)
    out = render(samples_of(t, np.ones(t.size)), markers=[(9, 50.0)], width=300, height=160)
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    assert not (rgb == 0).all(axis=-1).any()


def test_per_quantity_stats_and_panel_order_follow_the_request():
    t = np.linspace(0, 4, 400)
    v = np.linspace(10, 20, t.size)
    i = np.full(t.size, 0.5)
    p = v * i
    out = render(samples_of(t, p, v=v, i=i), quantities=["current", "voltage"],
                 width=300, height=150)
    assert set(out["per_quantity"]) == {"current", "voltage"}
    assert out["per_quantity"]["voltage"]["min"] == pytest.approx(10.0)
    assert out["per_quantity"]["voltage"]["max"] == pytest.approx(20.0)
    assert out["per_quantity"]["current"]["mean"] == pytest.approx(0.5)
    assert out["height"] == 300 and out["samples"] == t.size
    assert out["range"] == {"t0": 0.0, "t1": 4.0, "label": "all data"}


def test_non_finite_values_are_skipped_not_drawn():
    t = np.linspace(0, 10, 1000)
    p = np.ones(t.size)
    p[10:20] = np.nan
    p[30] = np.inf
    out = render(samples_of(t, p), width=300, height=160)
    assert out["per_quantity"]["power"]["max"] == 1.0
    json.dumps(out)


def test_huge_values_do_not_break_the_axis():
    t = np.linspace(0, 10, 500)
    p = np.where(np.arange(t.size) % 2, 3.0e38, -3.0e38).astype(np.float32)
    out = render(samples_of(t, p), width=300, height=160)
    assert out["per_quantity"]["power"]["min"] == pytest.approx(-3.0e38, rel=1e-6)
    png = base64.b64decode(out["image"]["data_base64"])
    assert png.startswith(PNG_SIG)


def test_two_million_samples_render_under_two_seconds_with_spike_kept():
    n = 2_000_000
    t = np.arange(n) / 1000.0
    rng = np.random.default_rng(7)
    p = (5.0 + rng.normal(0.0, 0.05, n)).astype(np.float32)
    spike = 1_234_567
    p[spike] = 900.0
    data = samples_of(t, p)
    started = time.perf_counter()
    out = render(data, markers=[(1, 500.0), (2, 1500.0)], quantities=["power"],
                 width=900, height=360)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    _, _, rgb = decode_png(base64.b64decode(out["image"]["data_base64"]))
    frame = frame_columns(rgb, 180)
    left, right = int(frame[0]), int(frame[-1])
    tall = [x for x in range(left + 1, right) if column_count(rgb, x, POWER_TINT) > 100]
    assert len(tall) == 1
    assert out["per_quantity"]["power"]["max"] == pytest.approx(900.0)
    assert out["samples"] == n


def test_size_guard_shrinks_the_image_when_base64_is_too_big(monkeypatch):
    t = np.linspace(0, 10, 3000)
    monkeypatch.setattr(plot, "MAX_BASE64_CHARS", 4000)
    out = render(samples_of(t, np.sin(t) + 2), width=900, height=360)
    assert out["width"] < 900 and len(out["image"]["data_base64"]) <= 4000


def test_size_guard_gives_up_at_the_minimum(monkeypatch):
    t = np.linspace(0, 10, 3000)
    monkeypatch.setattr(plot, "MAX_BASE64_CHARS", 100)
    from app.api.service import ApiError
    with pytest.raises(ApiError, match="too large"):
        render(samples_of(t, np.sin(t) + 2), width=200, height=150)


# ------------------------------------------------------------ errors

@pytest.mark.parametrize("data", [
    Samples.empty(),
    samples_of([1.0], [2.0]),
])
def test_fewer_than_two_samples_is_an_api_error(data):
    from app.api.service import ApiError
    with pytest.raises(ApiError, match="need at least 2"):
        render(data)


def test_all_non_finite_quantity_is_an_api_error():
    from app.api.service import ApiError
    t = np.linspace(0, 1, 10)
    with pytest.raises(ApiError, match="no finite power values"):
        render(samples_of(t, np.full(10, np.nan)))


def test_handler_rejects_bad_quantity_lists():
    from app.api.service import ApiError
    t = np.linspace(0, 1, 10)
    data = samples_of(t, t, v=t, i=t)
    with pytest.raises(ApiError, match="1 to 3"):
        render(data, quantities=[])
    with pytest.raises(ApiError, match="1 to 3"):
        render(data, quantities=["voltage", "current", "power", "voltage"])
    with pytest.raises(ApiError, match="must not repeat"):
        render(data, quantities=["power", "power"])


# ------------------------------------------------------------ schema

def test_schema_declares_render_plot_with_defaults_and_limits():
    tool = {t.name: t for t in plot_schema.TOOLS}["render_plot"]
    props = tool.input_schema["properties"]
    assert tool.input_schema["additionalProperties"] is False
    assert props["quantities"]["default"] == ["power"]
    assert props["quantities"]["items"]["enum"] == ["voltage", "current", "power"]
    assert (props["quantities"]["minItems"], props["quantities"]["maxItems"]) == (1, 3)
    assert (props["width"]["default"], props["width"]["minimum"],
            props["width"]["maximum"]) == (900, 200, 2000)
    assert (props["height"]["default"], props["height"]["minimum"],
            props["height"]["maximum"]) == (360, 150, 1200)
    assert props["show_markers"]["default"] is True
    for key in ("t0", "t1", "last_s", "segment", "marker_from", "marker_to"):
        assert key in props
    assert plot_schema.TIMEOUTS == {"render_plot": "long"}


def test_real_registry_contains_the_plot_tool():
    assert "plot" in ext.EXTENSIONS
    assert "render_plot" in schema.TOOLS_BY_NAME


# ------------------------------------------------------------ service and bridge

@pytest.fixture
def plot_registered():
    """render_plot is in the real registry (app/api/ext), so there is nothing to add."""
    assert "plot" in ext.EXTENSIONS
    assert "render_plot" in schema.TOOLS_BY_NAME


def make_service(data, markers=None):
    pytest.importorskip("PySide6.QtWidgets")
    from app.api.service import ApiService, DirectContext
    from app.core import AppSettings
    from app.core.markers import MarkerList
    ctrl = SimpleNamespace(data=lambda: data, markers=markers or MarkerList(), state="idle")
    return ApiService(ctrl, AppSettings(), DirectContext())


def test_service_dispatch_renders_and_validates(plot_registered):
    from app.api.service import ApiError
    from app.core.markers import MarkerList
    t = np.linspace(0, 20, 4000)
    markers = MarkerList()
    markers.add(5.0, "a")
    svc = make_service(samples_of(t, np.sin(t) + 2, v=t, i=t), markers)
    out = svc.call("render_plot", {"quantities": ["voltage"], "width": 300, "height": 150,
                                   "last_s": 5})
    assert out["image"]["mime_type"] == "image/png"
    assert out["range"]["label"] == "last 5 s"
    assert svc.call("render_plot", {})["height"] == 360
    bad = [
        ({"quantities": []}, "at least 1"),
        ({"quantities": ["power", "power"]}, "must not repeat"),
        ({"quantities": ["voltage", "current", "power", "power"]}, "at most 3 items"),
        ({"quantities": ["amps"]}, "must be one of"),
        ({"width": 199}, "at least 200"),
        ({"height": 1201}, "at most 1200"),
        ({"show_markers": "yes"}, "must be true or false"),
        ({"t0": 5, "t1": 2}, "t1 must be greater than t0"),
    ]
    for args, message in bad:
        with pytest.raises(ApiError, match=message):
            svc.call("render_plot", args)


def test_service_reports_missing_data_and_single_sample(plot_registered):
    from app.api.service import ApiError
    with pytest.raises(ApiError, match="no data"):
        make_service(Samples.empty()).call("render_plot", {})
    one = make_service(samples_of([1.0], [2.0]))
    with pytest.raises(ApiError, match="need at least 2"):
        one.call("render_plot", {})


def make_bridge(post):
    target = Target("http://127.0.0.1:9", "tok")

    def get(url, timeout):
        nonce = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["nonce"][0]
        return {"ok": True, "result": {"app": "EdgePowerMeter",
                                       "proof": mcp_bridge._proof("tok", nonce)}}

    return McpBridge(sys.stdin, sys.stdout, resolve_target=lambda: target, http_post=post,
                     http_get=get)


def test_bridge_turns_the_plot_into_an_image_block(plot_registered):
    t = np.linspace(0, 10, 2000)
    result = plot.handlers(FakeService(samples_of(t, np.sin(t) + 2)))["render_plot"]()
    bridge = make_bridge(lambda url, token, payload, timeout: {"ok": True, "result": result})
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "render_plot", "arguments": {}}}
    out = bridge.handle(msg)["result"]
    assert out["isError"] is False
    image, text = out["content"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert image["data"] == result["image"]["data_base64"]
    assert base64.b64decode(image["data"], validate=True).startswith(PNG_SIG)
    assert result["image"]["data_base64"] not in text["text"]
    assert json.loads(text["text"])["image"] == {"mime_type": "image/png"}


def test_bridge_lists_render_plot_and_uses_the_long_timeout(plot_registered):
    bridge = make_bridge(lambda *a: {"ok": True, "result": {}})
    names = [t["name"] for t in bridge._tools_list()["tools"]]
    assert "render_plot" in names
    assert mcp_bridge.tool_timeout_s("render_plot", {}) == mcp_bridge.LONG_TIMEOUT_S
