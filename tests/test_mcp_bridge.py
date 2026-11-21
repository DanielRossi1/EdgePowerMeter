"""Tests for the stdio MCP bridge (stdlib only, no Qt)."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import version as app_version
from app.api import mcp_bridge, schema
from app.api.mcp_bridge import NOT_RUNNING_TEXT, McpBridge, Target

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_TOOLS = list(schema.TOOLS)

FAKE_TOOLS = [
    schema.Tool(
        name="start_acquisition",
        description="Start.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    schema.Tool(
        name="get_status",
        description="Status of the app.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    schema.Tool(
        name="measure",
        description="Measure a window.",
        input_schema={
            "type": "object",
            "properties": {"duration_s": {"type": "number"}},
            "required": ["duration_s"],
            "additionalProperties": False,
        },
    ),
]


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
    monkeypatch.setattr(schema, "TOOLS", list(FAKE_TOOLS))
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in FAKE_TOOLS})


class Recorder:
    """Injected http_post: records calls and returns a canned envelope or raises."""

    def __init__(self, reply=None, raise_exc=None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, url, token, payload, timeout):
        self.calls.append({"url": url, "token": token, "payload": payload, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.reply


HEALTHY = {"ok": True, "result": {"app": "EdgePowerMeter", "version": "t", "api_version": 1}}
NONCE_QUERY = "nonce"


def proof_for(token, nonce):
    """The contract: HMAC-SHA256 keyed by the API token, over the nonce."""
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def nonce_of(url):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return query[NONCE_QUERY][0] if NONCE_QUERY in query else None


def proving_answer(token, url):
    """What a genuine EdgePowerMeter with this token answers to GET /api/v1/health?nonce=."""
    result = dict(HEALTHY["result"])
    nonce = nonce_of(url)
    if nonce is not None:
        result["proof"] = proof_for(token, nonce)
    return {"ok": True, "result": result}


def proving_get(token):
    return lambda url, timeout: proving_answer(token, url)


def make_bridge(post=None, target=Target("http://127.0.0.1:9", "tok"), get=None):
    get = get or proving_get(target.token if target else "tok")
    return McpBridge(io.StringIO(), io.StringIO(), resolve_target=lambda: target,
                     http_post=post, http_get=get)


def request(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def call_tool(bridge, name, arguments=None):
    reply = bridge.handle(request("tools/call", {"name": name, "arguments": arguments or {}}))
    assert "result" in reply, reply
    return reply["result"]


def strict_loads(text):
    def reject(constant):
        raise ValueError("non-standard JSON constant: " + constant)

    return json.loads(text, parse_constant=reject)


# ----- handle(): protocol ----------------------------------------------------------------


def test_initialize_echoes_supported_version():
    reply = make_bridge().handle(request("initialize", {"protocolVersion": "2024-11-05"}))
    result = reply["result"]
    assert reply["id"] == 1 and reply["jsonrpc"] == "2.0"
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "edgepowermeter", "version": app_version.__version__}
    assert "start_acquisition" in result["instructions"]


@pytest.mark.parametrize("params", [{"protocolVersion": "1999-01-01"}, {}, None])
def test_initialize_unknown_or_missing_version_gets_latest(params):
    reply = make_bridge().handle(request("initialize", params))
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_response():
    bridge = make_bridge()
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 3}}) is None
    assert bridge.handle({"jsonrpc": "2.0", "method": "something/unknown"}) is None


def test_ping():
    assert make_bridge().handle(request("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_static_and_uses_input_schema_key():
    tools = make_bridge().handle(request("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == [t.name for t in FAKE_TOOLS]
    for entry, tool in zip(tools, FAKE_TOOLS):
        assert set(entry) == {"name", "description", "inputSchema"}
        assert entry["inputSchema"] == tool.input_schema
        assert entry["description"] == tool.description


def test_tools_list_covers_every_registry_entry(monkeypatch):
    # The real registry, captured at import time before the autouse fixture swaps it out.
    monkeypatch.setattr(schema, "TOOLS", REAL_TOOLS)
    tools = make_bridge().handle(request("tools/list"))["result"]["tools"]
    assert len(tools) == len(REAL_TOOLS)
    for entry, tool in zip(tools, REAL_TOOLS):
        assert entry["name"] == tool.name
        assert "inputSchema" in entry and entry["inputSchema"] == tool.input_schema


def test_unknown_method_with_id_is_method_not_found():
    reply = make_bridge().handle(request("resources/list", rid="abc"))
    assert reply["id"] == "abc"
    assert reply["error"]["code"] == -32601


def test_tools_call_missing_name_is_invalid_params():
    reply = make_bridge().handle(request("tools/call", {"arguments": {}}))
    assert reply["error"]["code"] == -32602


def test_non_object_message_is_invalid_request():
    reply = make_bridge().handle(42)
    assert reply["error"]["code"] == -32600 and reply["id"] is None


# ----- item 9: JSON-RPC id conformance and null-id notifications -------------------------


@pytest.mark.parametrize("raw_id", ['{"a":1}', "[1]", "true", "false", "1e999", "-1e999"])
def test_id_must_be_string_number_or_null(raw_id):
    line = '{"jsonrpc":"2.0","id":%s,"method":"ping"}\n' % raw_id
    replies = run_serve(line)
    assert replies == [{"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600,
                                  "message": replies[0]["error"]["message"]}}]


def test_non_finite_python_ids_are_invalid_request():
    reply = make_bridge().handle({"jsonrpc": "2.0", "id": float("nan"), "method": "ping"})
    assert reply["error"]["code"] == -32600 and reply["id"] is None
    reply = make_bridge().handle({"jsonrpc": "2.0", "id": float("inf"), "method": "ping"})
    assert reply["error"]["code"] == -32600 and reply["id"] is None


@pytest.mark.parametrize("rid", ["abc", "", 0, -3, 2.5, 10 ** 30])
def test_valid_ids_are_echoed(rid):
    reply = make_bridge().handle(request("ping", rid=rid))
    assert reply == {"jsonrpc": "2.0", "id": rid, "result": {}}


def test_null_id_request_is_answered_with_null_id():
    reply = make_bridge().handle(request("ping", rid=None))
    assert reply == {"jsonrpc": "2.0", "id": None, "result": {}}


@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled"])
def test_null_id_notifications_get_no_response(method):
    message = {"jsonrpc": "2.0", "id": None, "method": method, "params": {"requestId": 3}}
    assert make_bridge().handle(message) is None
    assert run_serve(json.dumps(message) + "\n") == []


def test_null_id_notification_never_calls_a_tool():
    post = Recorder(reply={"ok": True, "result": {}})
    message = {"jsonrpc": "2.0", "id": None, "method": "notifications/tools/call",
               "params": {"name": "get_status", "arguments": {}}}
    assert make_bridge(post).handle(message) is None
    assert post.calls == []


# ----- tools/call -------------------------------------------------------------------------


def test_tools_call_success_posts_bearer_and_payload():
    post = Recorder(reply={"ok": True, "result": {"state": "idle", "samples": 0}})
    bridge = make_bridge(post, Target("http://127.0.0.1:8765/", "secret"))
    result = call_tool(bridge, "get_status")
    assert result["isError"] is False
    assert result["structuredContent"] == {"state": "idle", "samples": 0}
    assert json.loads(result["content"][0]["text"]) == {"state": "idle", "samples": 0}
    (call,) = post.calls
    assert call["url"] == "http://127.0.0.1:8765/api/v1/tools/get_status"
    assert call["token"] == "secret"
    assert call["payload"] == {}


def test_tools_call_passes_arguments_as_body():
    post = Recorder(reply={"ok": True, "result": {"measured": True}})
    call_tool(make_bridge(post), "measure", {"duration_s": 5, "label": "x"})
    assert post.calls[0]["payload"] == {"duration_s": 5, "label": "x"}
    assert post.calls[0]["timeout"] == 5 + 60


def test_tools_call_api_error_is_iserror():
    post = Recorder(reply={"ok": False, "error": "unsaved data; pass discard_unsaved=true"})
    result = call_tool(make_bridge(post), "start_acquisition")
    assert result["isError"] is True
    assert result["content"][0]["text"] == "unsaved data; pass discard_unsaved=true"


def test_tools_call_unreachable_is_friendly_iserror():
    post = Recorder(raise_exc=ConnectionRefusedError(111, "refused"))
    result = call_tool(make_bridge(post), "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT


def test_tools_call_without_target_does_not_post():
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(make_bridge(post, target=None), "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT
    assert post.calls == []


def test_tools_call_timeout_is_iserror():
    post = Recorder(raise_exc=socket.timeout("timed out"))
    result = call_tool(make_bridge(post), "get_status")
    assert result["isError"] is True
    assert "did not answer" in result["content"][0]["text"]


def test_tools_call_rejects_non_object_arguments():
    reply = make_bridge().handle(request("tools/call", {"name": "get_status", "arguments": [1]}))
    assert reply["error"]["code"] == -32602


def test_target_resolved_on_every_call():
    targets = [Target("http://127.0.0.1:1", "a"), Target("http://127.0.0.1:2", "b")]
    resolved = []

    def resolve():
        resolved.append(True)
        return targets[len(resolved) - 1]

    def get(url, timeout):
        return proving_answer("a" if ":1/" in url else "b", url)

    post = Recorder(reply={"ok": True, "result": {}})
    bridge = McpBridge(io.StringIO(), io.StringIO(), resolve_target=resolve, http_post=post,
                       http_get=get)
    call_tool(bridge, "get_status")
    call_tool(bridge, "get_status")
    assert len(resolved) == 2
    assert [c["token"] for c in post.calls] == ["a", "b"]


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("start_acquisition", {"wait_s": 4}, 34.0),
        ("start_acquisition", {}, 40.0),
        ("wait", {"seconds": 120}, 180.0),
        ("measure", {"duration_s": 10, "settle_s": 2}, 72.0),
        ("get_status", {}, 60.0),
        ("wait", {"seconds": True}, 60.0),
        ("export_csv", {"path": "/x.csv"}, 600.0),
        ("export_pdf", {"path": "/x.pdf"}, 600.0),
        ("import_csv", {"path": "/x.csv"}, 600.0),
        ("get_statistics", {}, 600.0),
        ("get_segments", {}, 600.0),
        ("benchmark", {"units": "x"}, 600.0),
        ("get_samples", {}, 600.0),
    ],
)
def test_per_tool_timeouts(name, args, expected):
    assert mcp_bridge.tool_timeout_s(name, args) == expected


def test_long_tools_are_real_tools():
    assert mcp_bridge.LONG_TOOLS <= {t.name for t in REAL_TOOLS}


# ----- item 7: huge integers in timeout arguments ----------------------------------------


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("measure", {"duration_s": 10 ** 400}, mcp_bridge.MEASURE_GRACE_S),
        ("measure", {"duration_s": 1, "settle_s": -(10 ** 400)}, 1.0 + mcp_bridge.MEASURE_GRACE_S),
        ("wait", {"seconds": -(10 ** 400)}, mcp_bridge.MEASURE_GRACE_S),
        ("start_acquisition", {"wait_s": 10 ** 400}, 10.0 + mcp_bridge.ACQUISITION_GRACE_S),
    ],
)
def test_huge_int_timeouts_fall_back_to_defaults(name, args, expected):
    assert mcp_bridge.tool_timeout_s(name, args) == expected


def test_huge_int_argument_does_not_crash_or_print_a_traceback(capsys):
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(make_bridge(post), "measure", {"duration_s": 10 ** 400})
    assert result["isError"] is False
    assert post.calls[0]["timeout"] == mcp_bridge.MEASURE_GRACE_S
    assert capsys.readouterr().err == ""


# ----- unknown tools (item 4) ------------------------------------------------------------


def test_unknown_tool_is_protocol_error_and_never_posts():
    post = Recorder(reply={"ok": True, "result": {}})
    reply = make_bridge(post).handle(request("tools/call", {"name": "nope", "arguments": {}}))
    assert reply["error"] == {"code": -32602, "message": "unknown tool: nope"}
    assert "result" not in reply
    assert post.calls == []


# ----- item 1: target URL must be local http ---------------------------------------------


BAD_URLS = [
    "https://127.0.0.1:8765",
    "http://192.168.1.121:8765",
    "http://0.0.0.0:8765",
    "http://example.com:8765",
    "http://localhost.evil.com:8765",
    "http://127.0.0.1.evil.com:8765",
    "http://evil.com@127.0.0.1:8765",
    "http://user:pw@127.0.0.1:8765",
    "http://[::2]:8765",
    "http://127.0.0.1:99999",
    "http://127.0.0.1:8765/?x=1",
    "127.0.0.1:8765",
    "file:///etc/passwd",
    "ftp://127.0.0.1",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_token_not_sent_to_a_non_local_url(url):
    post = Recorder(reply={"ok": True, "result": {}})
    gets = []

    def get(target_url, timeout):
        gets.append(target_url)
        return proving_answer("secret", target_url)

    result = call_tool(make_bridge(post, Target(url, "secret"), get=get), "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == mcp_bridge.REFUSED_URL_TEXT
    assert post.calls == [] and gets == []


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9", "http://localhost:9", "HTTP://LOCALHOST:9", "http://[::1]:9",
])
def test_local_http_urls_are_accepted(url):
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(make_bridge(post, Target(url, "secret")), "get_status")
    assert result["isError"] is False
    assert post.calls[0]["url"].endswith("/api/v1/tools/get_status")
    assert post.calls[0]["token"] == "secret"


def test_token_not_sent_when_discovery_url_is_remote(monkeypatch, tmp_path):
    discovery = tmp_path / "api.json"
    discovery.write_text(json.dumps({"url": "http://192.168.1.121:8765", "token": "secret",
                                     "pid": os.getpid()}), encoding="utf-8")
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(discovery))
    seen = []

    def get(url, timeout):
        seen.append(url)
        return proving_answer("secret", url)

    bridge = McpBridge(io.StringIO(), io.StringIO(), http_post=Recorder(), http_get=get)
    result = call_tool(bridge, "get_status")
    assert result["isError"] is True and seen == []


def test_env_url_is_validated_too(monkeypatch):
    monkeypatch.setenv("EPM_API_URL", "http://10.0.0.5:8765")
    monkeypatch.setenv("EPM_API_TOKEN", "secret")
    monkeypatch.setenv("EPM_API_DISCOVERY", "/nonexistent/api.json")
    post = Recorder(reply={"ok": True, "result": {}})
    bridge = McpBridge(io.StringIO(), io.StringIO(), http_post=post, http_get=proving_get("secret"))
    result = call_tool(bridge, "get_status")
    assert result["content"][0]["text"] == mcp_bridge.REFUSED_URL_TEXT
    assert post.calls == []


# ----- item 1: pid must be a positive int, and the health check is a proof ----------------


@pytest.mark.parametrize("pid_field", [
    {"pid": "1"}, {"pid": True}, {"pid": False}, {"pid": 0}, {"pid": -5}, {"pid": 1.5},
    {"pid": None}, {"pid": [1]}, {},
])
def test_discovery_file_without_a_usable_pid_is_refused(monkeypatch, tmp_path, pid_field):
    discovery = tmp_path / "api.json"
    data = {"url": "http://127.0.0.1:9", "token": "t"}
    data.update(pid_field)
    discovery.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(discovery))
    assert mcp_bridge.default_target() is None
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(McpBridge(io.StringIO(), io.StringIO(), http_post=post), "get_status")
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT
    assert post.calls == []


@pytest.mark.skipif(os.name != "posix", reason="pid liveness check is POSIX-only")
def test_pid_alive_survives_pids_too_large_for_the_os():
    assert mcp_bridge.pid_alive(10 ** 30) is False
    assert mcp_bridge.pid_alive(2 ** 63) is False


@pytest.mark.parametrize("answer", [
    HEALTHY,                                                       # no proof at all
    {"ok": True, "result": {"app": "EdgePowerMeter", "proof": "0" * 64}},
    {"ok": True, "result": {"app": "EdgePowerMeter", "proof": 12}},
    {"ok": True, "result": {"app": "SomethingElse"}},
    {"ok": True, "proof": "x"},                                    # proof outside result
    ["not", "an", "object"],
    None,
])
def test_health_without_a_matching_proof_is_refused(answer):
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(make_bridge(post, Target("http://127.0.0.1:9", "secret"),
                                   get=lambda url, timeout: answer), "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == mcp_bridge.NOT_IDENTIFIED_TEXT
    assert post.calls == []


def test_proof_computed_with_another_token_is_refused():
    post = Recorder(reply={"ok": True, "result": {}})
    result = call_tool(make_bridge(post, Target("http://127.0.0.1:9", "secret"),
                                   get=proving_get("other")), "get_status")
    assert result["isError"] is True and post.calls == []


def test_replayed_proof_for_another_nonce_is_refused():
    post = Recorder(reply={"ok": True, "result": {}})
    replay = {"ok": True, "result": {"app": "EdgePowerMeter",
                                     "proof": proof_for("secret", "00" * 16)}}
    result = call_tool(make_bridge(post, Target("http://127.0.0.1:9", "secret"),
                                   get=lambda url, timeout: replay), "get_status")
    assert result["isError"] is True and post.calls == []


def test_health_nonce_is_fresh_random_hex_each_time():
    nonces = []

    def get(url, timeout):
        nonces.append(nonce_of(url))
        return proving_answer("tok", url)

    for _ in range(2):
        call_tool(make_bridge(Recorder(reply={"ok": True, "result": {}}),
                              Target("http://127.0.0.1:9", "tok"), get=get), "get_status")
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    for nonce in nonces:
        assert len(nonce) == 32
        int(nonce, 16)


def test_token_not_sent_to_a_server_that_is_not_edgepowermeter():
    post = Recorder(reply={"ok": True, "result": {}})

    def impostor(url, timeout):
        return {"ok": True, "result": {"app": "SomethingElse"}}

    result = call_tool(make_bridge(post, get=impostor), "get_status")
    assert result["isError"] is True
    assert "not EdgePowerMeter" in result["content"][0]["text"]
    assert post.calls == []


def test_non_json_health_answer_is_not_trusted():
    post = Recorder(reply={"ok": True, "result": {}})

    def html(url, timeout):
        raise ValueError("not json")

    result = call_tool(make_bridge(post, get=html), "get_status")
    assert result["isError"] is True and post.calls == []


def test_unreachable_health_means_not_running():
    post = Recorder(reply={"ok": True, "result": {}})

    def refused(url, timeout):
        raise ConnectionRefusedError(111, "refused")

    result = call_tool(make_bridge(post, get=refused), "get_status")
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT and post.calls == []


def test_health_is_checked_at_the_health_url_with_nonce_and_without_token():
    seen = []

    def spy(url, timeout):
        seen.append((url, timeout))
        return proving_answer("secret", url)

    post = Recorder(reply={"ok": True, "result": {}})
    call_tool(make_bridge(post, Target("http://127.0.0.1:8765/", "secret"), get=spy),
              "get_status")
    ((url, timeout),) = seen
    assert url.startswith("http://127.0.0.1:8765/api/v1/health?nonce=")
    assert timeout == mcp_bridge.HEALTH_TIMEOUT_S
    assert len(nonce_of(url)) == 32


def test_verified_health_is_cached_for_30_seconds(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mcp_bridge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    checks = []

    def counting(url, timeout):
        checks.append(url)
        return proving_answer("tok", url)

    post = Recorder(reply={"ok": True, "result": {}})
    bridge = make_bridge(post, get=counting)
    call_tool(bridge, "get_status")
    clock[0] += 10
    call_tool(bridge, "get_status")
    assert len(checks) == 1 and len(post.calls) == 2
    clock[0] += 30
    call_tool(bridge, "get_status")
    assert len(checks) == 2


def test_verification_cache_is_keyed_by_url_and_token(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "time", SimpleNamespace(monotonic=lambda: 5.0))
    checks = []

    def counting(url, timeout):
        checks.append(url)
        return proving_answer("tok", url)

    targets = [Target("http://127.0.0.1:9", "tok"), Target("http://127.0.0.1:9", "new-token")]
    post = Recorder(reply={"ok": True, "result": {}})
    bridge = McpBridge(io.StringIO(), io.StringIO(), resolve_target=lambda: targets.pop(0),
                       http_post=post, http_get=counting)
    assert call_tool(bridge, "get_status")["isError"] is False
    # Same URL, another token: the cached verification does not apply, and the proof fails.
    result = call_tool(bridge, "get_status")
    assert len(checks) == 2
    assert result["isError"] is True and len(post.calls) == 1


def test_failed_health_is_not_cached(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mcp_bridge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    answers = [ConnectionRefusedError(111, "refused"), proving_get("tok")]

    def flaky(url, timeout):
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(url, timeout)

    post = Recorder(reply={"ok": True, "result": {}})
    bridge = make_bridge(post, get=flaky)
    assert call_tool(bridge, "get_status")["isError"] is True
    # The app started meanwhile: no 30 s wait.
    assert call_tool(bridge, "get_status")["isError"] is False


@pytest.mark.skipif(os.name != "posix", reason="pid liveness check is POSIX-only")
def test_discovery_file_of_a_dead_process_means_not_running(monkeypatch, tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    discovery = tmp_path / "api.json"
    discovery.write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "t",
                                     "pid": child.pid}), encoding="utf-8")
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(discovery))
    assert mcp_bridge.default_target() is None
    result = call_tool(McpBridge(io.StringIO(), io.StringIO(), http_post=Recorder()),
                       "get_status")
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT


def test_discovery_file_of_a_live_process_is_used(monkeypatch, tmp_path):
    discovery = tmp_path / "api.json"
    discovery.write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "t",
                                     "pid": os.getpid()}), encoding="utf-8")
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(discovery))
    assert mcp_bridge.default_target() == Target("http://127.0.0.1:9", "t")


# ----- items 2: redirects are refused, proxies bypassed -----------------------------------


class _Scripted(BaseHTTPRequestHandler):
    """A local server whose answers are chosen by the test (respond(method, path))."""

    def do_GET(self):
        self._answer()

    def do_POST(self):
        self._answer()

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.hits.append({"method": self.command, "path": self.path,
                                 "auth": self.headers.get("Authorization")})
        status, headers, body = self.server.respond(self.command, self.path)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def scripted_server(respond):
    server = HTTPServer(("127.0.0.1", 0), _Scripted)
    server.hits = []
    server.respond = respond
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_address[1]), server
    finally:
        server.shutdown()
        server.server_close()


def _json_answer(status, obj, headers=None):
    return status, dict(headers or {}), json.dumps(obj).encode("utf-8")


def test_redirected_tool_post_is_refused_and_token_not_forwarded():
    with scripted_server(lambda m, p: _json_answer(200, {"ok": True, "result": {}})) as (b_url, b):
        def a_respond(method, path):
            if path.startswith(mcp_bridge.HEALTH_PATH):
                return _json_answer(200, proving_answer("secret", path))
            return _json_answer(307, {}, {"Location": b_url + path})

        with scripted_server(a_respond) as (a_url, a):
            bridge = McpBridge(io.StringIO(), io.StringIO(),
                               resolve_target=lambda: Target(a_url, "secret"))
            result = call_tool(bridge, "get_status")
    assert result["isError"] is True
    assert "redirect" in result["content"][0]["text"]
    assert b.hits == []
    assert [h["method"] for h in a.hits] == ["GET", "POST"]


def test_redirected_health_check_is_refused_and_token_not_forwarded():
    with scripted_server(lambda m, p: _json_answer(200, proving_answer("secret", p))) as (b_url, b):
        def a_respond(method, path):
            if path.startswith(mcp_bridge.HEALTH_PATH):
                return _json_answer(302, {}, {"Location": b_url + path})
            return _json_answer(200, {"ok": True, "result": {}})

        with scripted_server(a_respond) as (a_url, a):
            bridge = McpBridge(io.StringIO(), io.StringIO(),
                               resolve_target=lambda: Target(a_url, "secret"))
            result = call_tool(bridge, "get_status")
    assert result["isError"] is True
    assert b.hits == []
    assert [h["method"] for h in a.hits] == ["GET"]  # the tool POST never happened


def test_proxy_environment_is_ignored(monkeypatch):
    def genuine(method, path):
        if path.startswith(mcp_bridge.HEALTH_PATH):
            return _json_answer(200, proving_answer("secret", path))
        return _json_answer(200, {"ok": True, "result": {"via": "direct"}})

    with scripted_server(genuine) as (url, server):
        # A proxy would receive the token in clear text; it must not be used for a local URL.
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        result = call_tool(McpBridge(io.StringIO(), io.StringIO(),
                                     resolve_target=lambda: Target(url, "secret")), "get_status")
    assert result["isError"] is False
    assert result["structuredContent"] == {"via": "direct"}


# ----- item 3 and 4: hostile input does not kill the bridge -------------------------------


def run_serve(text, post=None, target=None):
    return [json.loads(line) for line in serve_text(text, post, target).splitlines()
            if line.strip()]


def serve_text(text, post=None, target=None):
    """Feed raw stdin (str or bytes) through serve() and return the raw stdout text."""
    out = io.StringIO()
    stdin = io.BytesIO(text) if isinstance(text, bytes) else io.StringIO(text)
    bridge = McpBridge(stdin, out, resolve_target=lambda: target, http_post=post,
                       http_get=proving_get(target.token if target else "tok"))
    bridge.serve()
    return out.getvalue()


def test_serve_parse_error_then_keeps_going():
    replies = run_serve('{not json\n' + json.dumps(request("ping", rid=7)) + "\n")
    assert replies[0]["id"] is None and replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_deeply_nested_json_is_a_parse_error_and_serving_continues():
    deep = ('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_status",'
            '"arguments":{"x":' + "[" * 300000 + "]" * 300000 + "}}}")
    replies = run_serve(json.dumps(request("ping", rid=1)) + "\n" + deep + "\n"
                        + json.dumps(request("ping", rid=3)) + "\n")
    assert [r.get("id") for r in replies] == [1, None, 3]
    assert replies[1]["error"]["code"] == -32700
    assert replies[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_invalid_utf8_line_is_a_parse_error_and_serving_continues():
    data = (json.dumps(request("ping", rid=1)) + "\n").encode("utf-8") + b'\xff\xfe{"id":2}\n' \
        + (json.dumps(request("ping", rid=3)) + "\n").encode("utf-8")
    replies = run_serve(data)
    assert [r.get("id") for r in replies] == [1, None, 3]
    assert replies[1]["error"]["code"] == -32700
    assert replies[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_invalid_utf8_inside_a_string_is_a_parse_error():
    data = b'{"jsonrpc":"2.0","id":4,"method":"ping","params":{"s":"\xc3"}}\n'
    (reply,) = run_serve(data)
    assert reply["id"] is None and reply["error"]["code"] == -32700


def test_serve_eof_returns_cleanly_without_output():
    assert run_serve("") == []


def test_serve_ignores_blank_lines_and_notifications():
    text = "\n" + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n\n"
    assert run_serve(text) == []


def test_serve_batch_returns_array_of_replies():
    batch = [request("ping", rid=1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
             request("ping", rid=2)]
    (replies,) = run_serve(json.dumps(batch) + "\n")  # one line holding one JSON array
    assert replies == [{"jsonrpc": "2.0", "id": 1, "result": {}},
                       {"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_serve_batch_of_only_notifications_is_silent():
    batch = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
    assert run_serve(json.dumps(batch) + "\n") == []


def test_serve_empty_batch_is_invalid_request():
    (reply,) = run_serve("[]\n")
    assert reply["error"]["code"] == -32600


def test_serve_flushes_after_every_message():
    class Spy(io.StringIO):
        flushes = 0

        def flush(self):
            Spy.flushes += 1
            super().flush()

    out = Spy()
    text = json.dumps(request("ping", rid=1)) + "\n" + json.dumps(request("ping", rid=2)) + "\n"
    McpBridge(io.StringIO(text), out, resolve_target=lambda: None).serve()
    assert Spy.flushes == 2
    assert out.getvalue().count("\n") == 2


def test_serve_session_initialize_list_call():
    post = Recorder(reply={"ok": True, "result": {"state": "idle"}})
    text = "\n".join(json.dumps(m) for m in [
        request("initialize", {"protocolVersion": "2025-03-26"}, rid=1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("tools/list", rid=2),
        request("tools/call", {"name": "get_status", "arguments": {}}, rid=3),
    ]) + "\n"
    replies = run_serve(text, post, Target("http://127.0.0.1:9", "tok"))
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "2025-03-26"
    assert replies[2]["result"]["isError"] is False


# ----- item 5: NaN and Infinity in and out -----------------------------------------------


@pytest.mark.parametrize("raw", [
    '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
    '{"jsonrpc":"2.0","id":Infinity,"method":"ping"}',
    '{"jsonrpc":"2.0","id":-Infinity,"method":"ping"}',
    '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_status",'
    '"arguments":{"duration_s":NaN}}}',
])
def test_nan_and_infinity_on_stdin_are_parse_errors(raw):
    replies = run_serve(raw + "\n" + json.dumps(request("ping", rid=9)) + "\n")
    assert replies[0]["id"] is None and replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 9, "result": {}}


def test_replies_are_strict_json_even_for_non_finite_results():
    post = Recorder(reply={"ok": True, "result": {"x": float("nan"),
                                                  "y": [float("inf"), 1.5, {"z": float("-inf")}]}})
    out = serve_text(json.dumps(request("tools/call", {"name": "get_status", "arguments": {}}))
                     + "\n", post, Target("http://127.0.0.1:9", "tok"))
    (line,) = out.splitlines()
    reply = strict_loads(line)  # raises if the output carries NaN or Infinity
    assert reply["result"]["structuredContent"] == {"x": None, "y": [None, 1.5, {"z": None}]}
    text = strict_loads(json.dumps(reply["result"]["content"][0]["text"]))
    assert "NaN" not in text and "Infinity" not in text


def test_non_finite_app_answer_arrives_as_null_over_http():
    def answer(method, path):
        if path.startswith(mcp_bridge.HEALTH_PATH):
            return _json_answer(200, proving_answer("secret", path))
        body = b'{"ok": true, "result": {"x": NaN, "y": Infinity}}'
        return 200, {}, body

    with scripted_server(answer) as (url, server):
        result = call_tool(McpBridge(io.StringIO(), io.StringIO(),
                                     resolve_target=lambda: Target(url, "secret")), "get_status")
    assert result["isError"] is False
    assert result["structuredContent"] == {"x": None, "y": None}


def test_non_finite_id_is_never_echoed():
    # 1e999 parses to Infinity: it is not a usable id, so the reply carries null.
    out = serve_text('{"jsonrpc":"2.0","id":1e999,"method":"ping"}\n')
    (line,) = out.splitlines()
    assert strict_loads(line)["id"] is None
    assert strict_loads(line)["error"]["code"] == -32600


# ----- non-finite tool arguments are refused, not sent as null -------------------------


@pytest.mark.parametrize("args", [
    '{"duration_s":1e999}',
    '{"duration_s":-1e999}',
    '{"duration_s":[1, {"x": 1e400}]}',
])
def test_non_finite_tool_arguments_are_a_tool_error_not_null(args):
    # 1e999 is valid JSON that parses to infinity: it used to reach the app as null.
    post = Recorder(reply={"ok": True, "result": {}})
    line = ('{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"measure",'
            '"arguments":' + args + '}}')
    out = serve_text(line + "\n", post, Target("http://127.0.0.1:9", "tok"))
    (reply,) = [strict_loads(x) for x in out.splitlines()]
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == "non-finite numbers are not allowed"
    assert post.calls == []


def test_default_http_post_never_encodes_a_non_finite_number_as_null():
    body = mcp_bridge.default_http_post("http://127.0.0.1:9/api/v1/tools/wait", "tok",
                                        {"seconds": float("inf")}, 1.0)
    assert body == {"ok": False, "error": "the arguments could not be encoded as JSON"}


# ----- item 6: line size cap --------------------------------------------------------------


def test_line_over_2_mib_is_rejected_without_parsing_and_serving_continues(monkeypatch):
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                      "params": {"pad": "A" * mcp_bridge.MAX_LINE_BYTES}}) + "\n"
    parsed_sizes = []
    real_loads = json.loads

    def spy_loads(text, *args, **kwargs):
        parsed_sizes.append(len(text))
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(mcp_bridge, "json", SimpleNamespace(loads=spy_loads, dumps=json.dumps))
    replies = run_serve(big + json.dumps(request("ping", rid=2)) + "\n")
    assert replies[0]["id"] is None and replies[0]["error"]["code"] == -32600
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert all(size <= mcp_bridge.MAX_LINE_BYTES for size in parsed_sizes)


def test_oversized_line_without_trailing_newline_is_rejected():
    big = '{"pad":"' + "A" * (mcp_bridge.MAX_LINE_BYTES + 10)
    (reply,) = run_serve(big)
    assert reply["error"]["code"] == -32600


def test_line_of_exactly_the_cap_is_accepted():
    base = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": ""}}
    skeleton = json.dumps(base)
    pad = mcp_bridge.MAX_LINE_BYTES - len(skeleton)
    line = json.dumps(dict(base, params={"pad": "A" * pad}))
    assert len(line.encode("utf-8")) == mcp_bridge.MAX_LINE_BYTES
    replies = run_serve(line + "\n")
    assert replies == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


# ----- item 7 and 9 through the transport ------------------------------------------------


def test_null_id_ping_is_answered_and_null_id_notification_is_not():
    text = (json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": None, "method": "notifications/initialized"})
            + "\n")
    assert run_serve(text) == [{"jsonrpc": "2.0", "id": None, "result": {}}]


# ----- app identity: end to end ------------------------------------------------------------


class _FakeApp(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == mcp_bridge.HEALTH_PATH:
            self._send(200, proving_answer("secret", self.path))
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _FakeApp.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                              "body": body})
        if self.headers.get("Authorization") != "Bearer secret":
            self._send(401, {"ok": False, "error": "unauthorized"})
        elif self.path == "/api/v1/tools/clear_data":
            self._send(400, {"ok": False, "error": "unsaved data; pass discard_unsaved=true"})
        else:
            self._send(200, {"ok": True, "result": {"echo": body}})

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_app():
    _FakeApp.seen = []
    server = HTTPServer(("127.0.0.1", 0), _FakeApp)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_end_to_end_env_target(fake_app, monkeypatch, tmp_path):
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in REAL_TOOLS})
    monkeypatch.setenv("EPM_API_URL", fake_app)
    monkeypatch.setenv("EPM_API_TOKEN", "secret")
    monkeypatch.setenv("EPM_API_DISCOVERY", str(tmp_path / "missing.json"))
    bridge = McpBridge(io.StringIO(), io.StringIO())  # default resolver and HTTP poster

    result = call_tool(bridge, "measure", {"duration_s": 2})
    assert result["isError"] is False
    assert result["structuredContent"] == {"echo": {"duration_s": 2}}
    (seen,) = _FakeApp.seen
    assert seen["path"] == "/api/v1/tools/measure"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {"duration_s": 2}


def test_end_to_end_error_bodies_become_iserror(fake_app, monkeypatch, tmp_path):
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in REAL_TOOLS})
    monkeypatch.setenv("EPM_API_URL", fake_app)
    monkeypatch.setenv("EPM_API_TOKEN", "secret")
    monkeypatch.setenv("EPM_API_DISCOVERY", str(tmp_path / "missing.json"))
    bridge = McpBridge(io.StringIO(), io.StringIO())

    result = call_tool(bridge, "clear_data")
    assert result["isError"] is True
    assert result["content"][0]["text"] == "unsaved data; pass discard_unsaved=true"


def test_wrong_token_is_refused_before_any_tool_request(fake_app, monkeypatch, tmp_path):
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in REAL_TOOLS})
    monkeypatch.setenv("EPM_API_URL", fake_app)
    monkeypatch.setenv("EPM_API_TOKEN", "wrong")
    monkeypatch.setenv("EPM_API_DISCOVERY", str(tmp_path / "missing.json"))
    bridge = McpBridge(io.StringIO(), io.StringIO())

    result = call_tool(bridge, "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == mcp_bridge.NOT_IDENTIFIED_TEXT
    assert _FakeApp.seen == []


def test_discovery_file_is_used_when_env_unset(fake_app, monkeypatch, tmp_path):
    discovery = tmp_path / "api.json"
    discovery.write_text(json.dumps({"url": fake_app, "token": "secret", "pid": os.getpid()}))
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.delenv("EPM_API_TOKEN", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(discovery))
    bridge = McpBridge(io.StringIO(), io.StringIO())

    result = call_tool(bridge, "get_status")
    assert result["isError"] is False
    assert _FakeApp.seen[-1]["auth"] == "Bearer secret"


def test_missing_discovery_file_means_not_running(monkeypatch, tmp_path):
    monkeypatch.delenv("EPM_API_URL", raising=False)
    monkeypatch.setenv("EPM_API_DISCOVERY", str(tmp_path / "nope.json"))
    bridge = McpBridge(io.StringIO(), io.StringIO())
    result = call_tool(bridge, "get_status")
    assert result["isError"] is True
    assert result["content"][0]["text"] == NOT_RUNNING_TEXT


# ----- import hygiene and subprocess -----------------------------------------------------


def test_import_does_not_load_qt():
    code = (
        "import sys, app.api.mcp_bridge\n"
        "assert 'PySide6' not in sys.modules, 'PySide6 was imported'\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_module_entry_point_initialize_and_tools_list(tmp_path):
    env = dict(os.environ)
    env.pop("EPM_API_URL", None)
    env["EPM_API_DISCOVERY"] = str(tmp_path / "missing.json")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    stdin = "\n".join(json.dumps(m) for m in [
        request("initialize", {"protocolVersion": "2025-06-18"}, rid=1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("tools/list", rid=2),
    ]) + "\n"
    proc = subprocess.run([sys.executable, "-m", "app.api.mcp_bridge"], cwd=str(REPO_ROOT),
                          input=stdin.encode("utf-8"), capture_output=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert len(replies) == 2
    assert replies[0]["result"]["serverInfo"]["name"] == "edgepowermeter"
    assert isinstance(replies[1]["result"]["tools"], list)


def test_module_entry_point_survives_invalid_utf8_on_stdin(tmp_path):
    env = dict(os.environ)
    env.pop("EPM_API_URL", None)
    env["EPM_API_DISCOVERY"] = str(tmp_path / "missing.json")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    stdin = (b'\xff\xfe\n' + json.dumps(request("ping", rid=5)).encode("utf-8") + b"\n")
    proc = subprocess.run([sys.executable, "-m", "app.api.mcp_bridge"], cwd=str(REPO_ROOT),
                          input=stdin, capture_output=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 5, "result": {}}
