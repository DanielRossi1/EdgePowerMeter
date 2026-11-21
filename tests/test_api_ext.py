"""Extension mechanism of the automation API (app/api/ext).

A fake extension is registered through monkeypatch (EXTENSIONS plus two fake modules in
sys.modules), so the real registry is left as it is.
"""
from __future__ import annotations

import json
import sys
import types
import urllib.parse
from types import SimpleNamespace

import numpy as np
import pytest

from app.api import ext, mcp_bridge, schema
from app.api.mcp_bridge import McpBridge, Target, tool_timeout_s
from app.api.schema import Tool

SCHEMA_MOD = "app.api.ext.fake_ext_schema"
HANDLER_MOD = "app.api.ext.fake_ext"


def make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def fake_tools():
    echo = schema.tool(
        "fake_echo", "Echo the text n times.",
        {"text": schema.string("Text to echo.", max_length=20),
         "n": schema.integer("Repeat count.", default=1, minimum=1, maximum=5)},
        required=["text"])
    wait = schema.tool(
        "fake_wait", "Pretend to wait.",
        {"seconds": schema.number("Seconds.", default=5, minimum=0)})
    return [echo, wait]


@pytest.fixture
def fake_ext(monkeypatch):
    """Registers the fake extension; returns a dict with the handler-map build count."""
    state = {"builds": 0, "services": []}

    def echo(text, n=1):
        return {"echo": text * n}

    def wait(seconds=5):
        return {"waited": seconds}

    def handlers(service):
        state["builds"] += 1
        state["services"].append(service)
        return {"fake_echo": echo, "fake_wait": wait}

    schema_mod = make_module(SCHEMA_MOD, TOOLS=fake_tools(),
                             TIMEOUTS={"fake_wait": "param:seconds"})
    handler_mod = make_module(HANDLER_MOD, handlers=handlers)
    monkeypatch.setitem(sys.modules, SCHEMA_MOD, schema_mod)
    monkeypatch.setitem(sys.modules, HANDLER_MOD, handler_mod)
    monkeypatch.setattr(ext, "EXTENSIONS", ["fake_ext"])
    return state


@pytest.fixture
def registered(monkeypatch, fake_ext):
    """The fake tools visible to the service, the bridge and tools/list."""
    for t in fake_tools():
        monkeypatch.setitem(schema.TOOLS_BY_NAME, t.name, t)
    monkeypatch.setattr(schema, "TOOLS", list(schema.TOOLS) + fake_tools())
    monkeypatch.setitem(schema.EXTENSION_TIMEOUTS, "fake_wait", "param:seconds")
    return fake_ext


# ------------------------------------------------------------ public helpers

def test_public_helper_aliases_are_the_schema_helpers():
    assert schema.tool is schema._tool
    assert schema.number is schema._number
    assert schema.integer is schema._integer
    assert schema.boolean is schema._boolean
    assert schema.string is schema._string
    assert schema.enum is schema._enum
    assert schema.quantities is schema._quantities
    assert schema.range_props is schema._range
    assert schema.object_schema is schema._schema


BUILTIN_TOOL_NAMES = (
    "get_status", "list_ports", "start_acquisition", "stop_acquisition", "clear_data",
    "get_live", "add_marker", "list_markers", "update_marker", "remove_marker",
    "get_segments", "get_statistics", "measure", "benchmark", "get_samples", "export_csv",
    "export_pdf", "import_csv", "get_device", "configure_device", "send_command",
    "sync_clock", "get_settings", "update_settings", "wait",
)
EXTENSION_TOOL_NAMES = (
    "analyze_spectrum", "power_quality", "distribution", "detect_peaks",     # analysis
    "render_plot",                                                          # plot
    "wait_until", "capture_event",                                          # triggers
    "save_run", "list_runs", "get_run", "compare_runs", "delete_run",       # runs
    "get_events",                                                           # events
)


def test_real_registry_has_builtins_and_the_five_extensions_tools():
    assert ext.EXTENSIONS == ["analysis", "plot", "triggers", "runs", "events"]
    names = [t.name for t in schema.TOOLS]
    assert names == list(BUILTIN_TOOL_NAMES) + list(EXTENSION_TOOL_NAMES)
    assert len(names) == len(set(names)) == 38
    assert set(schema.TOOLS_BY_NAME) == set(names)
    assert all(name in schema.TOOLS_BY_NAME for name in schema.EXTENSION_TIMEOUTS)


def test_schema_modules_and_handler_modules_import_named_modules(fake_ext):
    assert [m.__name__ for m in ext.schema_modules()] == [SCHEMA_MOD]
    assert [m.__name__ for m in ext.handler_modules()] == [HANDLER_MOD]


# ----------------------------------------------------------- registry install

def test_install_adds_fake_tools_and_timeouts_to_registry(fake_ext):
    tools = list(schema.TOOLS)
    by_name = dict(schema.TOOLS_BY_NAME)
    timeouts = {}
    schema.install_extensions(ext.schema_modules(), tools, by_name, timeouts)
    assert "fake_echo" in by_name and "fake_wait" in by_name
    assert by_name["fake_echo"] in tools
    assert timeouts == {"fake_wait": "param:seconds"}


def test_install_refuses_builtin_name_clash_and_leaves_registry_untouched():
    clash = [Tool("get_status", "clash", {"type": "object", "properties": {}})]
    mod = make_module("fake_clash_schema", TOOLS=clash)
    tools = list(schema.TOOLS)
    by_name = dict(schema.TOOLS_BY_NAME)
    with pytest.raises(ValueError, match="duplicate tool name 'get_status'"):
        schema.install_extensions([mod], tools, by_name, {})
    assert tools == schema.TOOLS and by_name == schema.TOOLS_BY_NAME


def test_install_refuses_duplicate_between_extensions():
    a = make_module("fake_a_schema", TOOLS=[schema.tool("dup_tool", "a")])
    b = make_module("fake_b_schema", TOOLS=[schema.tool("dup_tool", "b")])
    with pytest.raises(ValueError, match="duplicate tool name 'dup_tool' in fake_b_schema"):
        schema.install_extensions([a, b], [], {}, {})


def test_install_checks_timeout_specs():
    base = make_module("fake_t_schema", TOOLS=fake_tools())
    cases = [
        ({"fake_wait": "soon"}, "must be 'long' or"),
        ({"fake_wait": "param:missing"}, "needs a numeric argument 'missing'"),
        ({"fake_echo": "param:text"}, "needs a numeric argument 'text'"),
        ({"nope": "long"}, "not a tool of that module"),
    ]
    for timeouts, message in cases:
        mod = make_module("fake_t_schema", TOOLS=fake_tools(), TIMEOUTS=timeouts)
        with pytest.raises(ValueError, match=message):
            schema.install_extensions([mod], [], {}, {})
    ok = make_module("fake_t_schema", TOOLS=fake_tools(),
                     TIMEOUTS={"fake_echo": "long", "fake_wait": "param:seconds"})
    out = {}
    schema.install_extensions([ok], [], {}, out)
    assert out == {"fake_echo": "long", "fake_wait": "param:seconds"}


def test_module_without_tools_is_a_clear_error():
    with pytest.raises(RuntimeError, match="defines no TOOLS list"):
        schema.install_extensions([make_module("fake_empty_schema")], [], {}, {})


# ---------------------------------------------------------- service dispatch

def make_service(data=None):
    pytest.importorskip("PySide6.QtWidgets")
    from app.api.service import ApiService, DirectContext
    from app.core import AppSettings
    ctrl = SimpleNamespace(data=lambda: data if data is not None else _empty(), markers=None,
                          state="idle")
    return ApiService(ctrl, AppSettings(), DirectContext())


def _empty():
    from app.core import Samples
    return Samples.empty()


def test_service_dispatches_extension_tool_with_validation(registered):
    svc = make_service()
    assert svc.call("fake_echo", {"text": "ab", "n": 3}) == {"echo": "ababab"}
    assert svc.call("fake_echo", {"text": "ab"}) == {"echo": "ab"}  # default n=1
    with pytest.raises(api_error(), match="text must be a string"):
        svc.call("fake_echo", {"text": 5})
    with pytest.raises(api_error(), match="additionalProperties|unknown|not allowed"):
        svc.call("fake_echo", {"text": "x", "bogus": 1})
    with pytest.raises(api_error(), match="at least 1"):
        svc.call("fake_echo", {"text": "x", "n": 0})
    with pytest.raises(api_error(), match="unknown tool"):
        svc.call("fake_missing", {})


def api_error():
    from app.api.service import ApiError
    return ApiError


def test_handler_map_is_built_once_per_service(registered):
    svc = make_service()
    svc.call("fake_echo", {"text": "a"})
    svc.call("fake_wait", {"seconds": 0})
    assert registered["builds"] == 1
    assert registered["services"][0] is svc
    assert svc.call("fake_wait", None) == {"waited": 5}


def test_handler_for_undeclared_or_builtin_tool_is_rejected(monkeypatch):
    bad = make_module(HANDLER_MOD, handlers=lambda s: {"not_declared": lambda: {}})
    monkeypatch.setitem(sys.modules, HANDLER_MOD, bad)
    monkeypatch.setattr(ext, "EXTENSIONS", ["fake_ext"])
    svc = make_service()
    with pytest.raises(RuntimeError, match="not a declared tool"):
        svc._extension_handlers()
    builtin = make_module(HANDLER_MOD, handlers=lambda s: {"get_status": lambda: {}})
    monkeypatch.setitem(sys.modules, HANDLER_MOD, builtin)
    svc = make_service()
    with pytest.raises(RuntimeError, match="built-in tool 'get_status'"):
        svc._extension_handlers()


def test_public_service_wrappers(registered):
    from app.core import Samples
    t = np.arange(0, 10.0, 0.5)
    data = Samples.from_columns(t, 1.79e9 + t, np.full(t.size, 5.0), np.full(t.size, 0.2),
                                np.full(t.size, 1.0))
    svc = make_service(data)
    t0, t1, label, part = svc.range_samples({"last_s": 2})
    assert label == "last 2 s" and t1 == pytest.approx(9.5) and len(part) >= 2
    tail = svc.live_samples(1.0)
    assert float(tail.t[0]) == pytest.approx(8.5) and float(tail.t[-1]) == pytest.approx(9.5)
    assert isinstance(svc.now(), float)
    svc.sleep(0)
    with pytest.raises(api_error(), match="window_s must be greater than 0"):
        svc.live_samples(0)
    assert len(make_service().live_samples(1.0)) == 0
    with pytest.raises(api_error(), match="not streaming"):
        svc.require_streaming()


# --------------------------------------------------------------- bridge

def _token_proof_get(token):
    def get(url, timeout):
        nonce = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["nonce"][0]
        return {"ok": True, "result": {"app": "EdgePowerMeter",
                                       "proof": mcp_bridge._proof(token, nonce)}}
    return get


def make_bridge(post):
    target = Target("http://127.0.0.1:9", "tok")
    return McpBridge(sys.stdin, sys.stdout, resolve_target=lambda: target, http_post=post,
                     http_get=_token_proof_get("tok"))


def call(bridge, name, arguments=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {}}}
    return bridge.handle(msg)["result"]


def answering(result):
    return lambda url, token, payload, timeout: {"ok": True, "result": result}


PNG_B64 = "iVBORw0KGgo="


def test_bridge_lists_extension_tools(registered):
    bridge = make_bridge(answering({}))
    names = [t["name"] for t in bridge._tools_list()["tools"]]
    assert "fake_echo" in names and "fake_wait" in names


def test_bridge_turns_image_into_image_block(registered):
    result = {"width": 2, "image": {"mime_type": "image/png", "data_base64": PNG_B64}}
    out = call(make_bridge(answering(result)), "fake_echo", {"text": "x"})
    assert out["isError"] is False
    image, text = out["content"]
    assert image == {"type": "image", "data": PNG_B64, "mimeType": "image/png"}
    assert text["type"] == "text"
    assert PNG_B64 not in text["text"]
    assert json.loads(text["text"]) == {"width": 2, "image": {"mime_type": "image/png"}}
    assert out["structuredContent"] == {"width": 2, "image": {"mime_type": "image/png"}}


@pytest.mark.parametrize("image", [
    "not an object",
    {"mime_type": "image/gif", "data_base64": PNG_B64},
    {"mime_type": "image/png", "data_base64": ""},
    {"mime_type": "image/png", "data_base64": "@@not base64@@"},
    {"mime_type": "image/png", "data_base64": 12},
    {"mime_type": "image/png", "data_base64": "A" * (8 * 1024 * 1024 + 4)},
])
def test_bridge_rejects_bad_images_as_tool_error(registered, image):
    out = call(make_bridge(answering({"image": image})), "fake_echo", {"text": "x"})
    assert out["isError"] is True
    assert "invalid image" in out["content"][0]["text"]


def test_bridge_timeout_for_param_extension(monkeypatch, registered):
    assert tool_timeout_s("fake_wait", {"seconds": 100}) == 160.0
    assert tool_timeout_s("fake_wait", {}) == 65.0          # schema default 5 + 60
    assert tool_timeout_s("fake_wait", {"seconds": 1e9}) == mcp_bridge.MAX_TIMEOUT_S
    monkeypatch.setitem(schema.EXTENSION_TIMEOUTS, "fake_echo", "long")
    assert tool_timeout_s("fake_echo", {}) == mcp_bridge.LONG_TIMEOUT_S
    assert tool_timeout_s("never_declared", {}) == mcp_bridge.DEFAULT_TIMEOUT_S


def test_bridge_passes_extension_timeout_to_http(registered):
    seen = []

    def post(url, token, payload, timeout):
        seen.append(timeout)
        return {"ok": True, "result": {"waited": 1}}

    call(make_bridge(post), "fake_wait", {"seconds": 100})
    assert seen == [160.0]


# ------------------------------------------------------------- MCP smoke test

def test_mcp_tools_list_returns_all_builtin_and_extension_tools():
    target = Target("http://127.0.0.1:9", "tok")
    bridge = McpBridge(sys.stdin, sys.stdout, resolve_target=lambda: target, http_post=None,
                       http_get=_token_proof_get("tok"))
    reply = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = reply["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == list(BUILTIN_TOOL_NAMES) + list(EXTENSION_TOOL_NAMES)
    assert len(names) == len(set(names)) == 38
    for t in tools:
        assert t["description"].strip(), t["name"]
        input_schema = t["inputSchema"]
        assert isinstance(input_schema, dict), t["name"]
        assert input_schema.get("type") == "object", t["name"]
        # strict JSON: no NaN or Infinity in any default or bound
        assert json.loads(json.dumps(input_schema, allow_nan=False)) == input_schema, t["name"]
