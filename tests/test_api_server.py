"""Tests for the automation API HTTP server and the GUI-thread invoker."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtCore")
from PySide6 import QtCore  # noqa: E402

from app.api import schema  # noqa: E402
from app.api.schema import Tool  # noqa: E402
from app.api import server as server_mod  # noqa: E402
from app.api.server import (  # noqa: E402
    MAX_BODY_BYTES,
    ApiServer,
    _Handler,
    _Invoker,
    _remove_discovery_if_ours,
)
from app.api.service import ApiError, RateLimited  # noqa: E402
from app.version import __version__  # noqa: E402

TOKEN = "test-token-123"
FAKE_TOOLS = [
    Tool("echo", "Echo the params back.", {"type": "object", "properties": {}}),
    Tool("bad", "Always a user error.", {"type": "object", "properties": {}}),
    Tool("boom", "Always an internal error.", {"type": "object", "properties": {}}),
    Tool("overflow", "Raises OverflowError.", {"type": "object", "properties": {}}),
    Tool("valerr", "Raises ValueError.", {"type": "object", "properties": {}}),
]


class FakeService:
    """Stands in for ApiService. Uses ctx.gui so the GUI-thread hop is exercised."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.gui_ident = None

    def call(self, name, params):
        if name == "echo":
            self.gui_ident = self.ctx.gui(threading.get_ident)
            return {"params": params}
        if name == "bad":
            raise ApiError("nope: bad input")
        if name == "boom":
            raise RuntimeError("kaput")
        if name == "overflow":
            raise OverflowError("int too large to convert to float")
        if name == "valerr":
            raise ValueError("secret detail /home/alice/x")
        raise ApiError(f"unknown tool {name}")


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(schema, "TOOLS", list(FAKE_TOOLS), raising=False)
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in FAKE_TOOLS},
                        raising=False)


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    path = tmp_path / "api.json"
    monkeypatch.setenv(schema.DISCOVERY_ENV, str(path))
    return path


@pytest.fixture
def server(qapp, tools, discovery):
    holder = {}

    def factory(ctx):
        holder["service"] = FakeService(ctx)
        return holder["service"]

    srv = ApiServer(None, None, port=0, token=TOKEN, service_factory=factory)
    srv.start()
    srv.service = holder["service"]
    yield srv
    srv.stop()


def wait_in_thread(qapp, fn, timeout=10.0):
    """Run `fn` on a worker thread while pumping Qt events on this (GUI) thread."""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["exc"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive():
        qapp.processEvents()
        worker.join(0.005)
        if time.monotonic() > deadline:
            raise AssertionError("worker thread did not finish")
    if "exc" in box:
        raise box["exc"]
    return box["result"]


def http_request(srv, method, path, body=None, headers=None, raw=None):
    """Returns (status, parsed_json_or_None, response_headers)."""
    hdrs = dict(headers or {})
    payload = raw
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except ValueError:
            parsed = None
        return resp.status, parsed, dict(resp.getheaders())
    finally:
        conn.close()


def do(qapp, srv, method, path, **kw):
    return wait_in_thread(qapp, lambda: http_request(srv, method, path, **kw))


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_health_needs_no_auth(qapp, server):
    status, body, _ = do(qapp, server, "GET", "/api/v1/health")
    assert status == 200
    assert body["ok"] is True
    assert body["result"] == {"app": "EdgePowerMeter", "version": __version__,
                              "api_version": schema.API_VERSION}


def test_tools_requires_token(qapp, server):
    status, body, headers = do(qapp, server, "GET", "/api/v1/tools")
    assert status == 401 and body["ok"] is False
    assert headers.get("WWW-Authenticate") == "Bearer"
    status, body, _ = do(qapp, server, "GET", "/api/v1/tools",
                         headers={"Authorization": "Bearer wrong"})
    assert status == 401


def test_tools_list(qapp, server):
    status, body, _ = do(qapp, server, "GET", "/api/v1/tools", headers=AUTH)
    assert status == 200
    names = [t["name"] for t in body["result"]["tools"]]
    assert names == ["echo", "bad", "boom", "overflow", "valerr"]
    assert body["result"]["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


def test_successful_call_runs_on_gui_thread(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/echo",
                         body={"a": 1}, headers=AUTH)
    assert status == 200
    assert body == {"ok": True, "result": {"params": {"a": 1}}}
    assert server.service.gui_ident == threading.get_ident()  # this (GUI) thread


def test_empty_body_is_empty_params(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/echo", headers=AUTH)
    assert status == 200
    assert body["result"] == {"params": {}}


def test_api_error_is_400(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/bad", body={}, headers=AUTH)
    assert status == 400
    assert body == {"ok": False, "error": "nope: bad input"}


def test_unknown_tool_is_404(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/nope", body={}, headers=AUTH)
    assert status == 404 and body["ok"] is False


def test_unexpected_exception_is_500(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/boom", body={}, headers=AUTH)
    assert status == 500
    assert body == {"ok": False, "error": "internal error"}     # details only in the log


def test_bad_json_is_400(qapp, server):
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/echo",
                         raw=b"{not json", headers=AUTH)
    assert status == 400 and body["ok"] is False
    status, _, _ = do(qapp, server, "POST", "/api/v1/tools/echo", raw=b"[1, 2]", headers=AUTH)
    assert status == 400


def test_origin_header_is_403(qapp, server):
    headers = dict(AUTH, Origin="http://evil.example")
    status, body, _ = do(qapp, server, "POST", "/api/v1/tools/echo", body={}, headers=headers)
    assert status == 403 and body["ok"] is False
    status, _, _ = do(qapp, server, "GET", "/api/v1/health",
                      headers={"Origin": "http://localhost"})
    assert status == 403


@pytest.mark.parametrize("host", ["evil.example:%d", "127.0.0.1", "example.com:8765"])
def test_bad_host_is_403(qapp, server, host):
    host = host % server.port if "%d" in host else host
    status, body, _ = do(qapp, server, "GET", "/api/v1/health", headers={"Host": host})
    assert status == 403 and body["ok"] is False


def test_good_host_accepted(qapp, server):
    status, _, _ = do(qapp, server, "GET", "/api/v1/health",
                      headers={"Host": f"localhost:{server.port}"})
    assert status == 200


def test_oversize_body_is_413(qapp, server):
    # Only the header announces the size: the server must refuse before reading
    # the body (sending a real 1 MiB body races with the server closing).
    import socket

    def send():
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall((f"POST /api/v1/tools/echo HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{server.port}\r\n"
                          f"Authorization: {AUTH['Authorization']}\r\n"
                          f"Content-Type: application/json\r\n"
                          f"Content-Length: {MAX_BODY_BYTES + 1}\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data

    data = wait_in_thread(qapp, send)
    assert data.startswith(b"HTTP/1.") and b" 413 " in data.split(b"\r\n", 1)[0]


def test_discovery_file_content_and_permissions(qapp, server, discovery):
    assert discovery.is_file()
    data = json.loads(discovery.read_text(encoding="utf-8"))
    assert data == {"url": server.url, "token": TOKEN, "pid": os.getpid(),
                    "version": __version__}
    assert server.url == f"http://127.0.0.1:{server.port}"
    assert server.port != 0
    if os.name == "posix":
        assert stat.S_IMODE(discovery.stat().st_mode) == 0o600


def test_discovery_file_removed_on_stop(qapp, tools, discovery):
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    assert srv.running and discovery.exists()
    srv.stop()
    assert not srv.running
    assert not discovery.exists()


def test_discovery_file_kept_when_pid_differs(qapp, tools, discovery):
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    discovery.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
    srv.stop()
    assert discovery.exists()


def test_non_loopback_without_token_is_refused(qapp, tools, discovery):
    with pytest.raises(ValueError):
        ApiServer(None, None, port=0, token="", host="0.0.0.0",
                  service_factory=lambda ctx: FakeService(ctx))


def test_invoker_runs_on_gui_thread(qapp):
    invoker = _Invoker()
    gui = QtCore.QThread.currentThread()
    # Called from the GUI thread: runs inline.
    assert invoker.gui(lambda: QtCore.QThread.currentThread() is gui) is True

    def from_worker():
        return invoker.gui(lambda: QtCore.QThread.currentThread() is gui)

    assert wait_in_thread(qapp, from_worker) is True


def test_invoker_propagates_exceptions(qapp):
    invoker = _Invoker()

    def fail():
        raise ValueError("bad value")

    with pytest.raises(ValueError, match="bad value"):
        wait_in_thread(qapp, lambda: invoker.gui(fail))


def test_invoker_timeout(qapp):
    invoker = _Invoker()
    box = {}

    def worker():
        try:
            invoker.gui(lambda: "late", timeout=0.05)
        except ApiError as exc:
            box["exc"] = exc

    # No event pumping here, so the GUI thread never runs the queued job in time.
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(5)
    assert isinstance(box.get("exc"), ApiError)
    assert "timed out" in str(box["exc"])
    qapp.processEvents()  # drain the orphaned job; its result is discarded


def test_timed_out_job_never_runs(qapp):
    from app.api.server import _Job

    invoker = _Invoker()
    ran = []
    job = _Job(lambda: ran.append(True))
    job.cancel()                                   # what gui() does on a timeout
    invoker._run(job)                              # the GUI thread picks it up later
    assert ran == [] and job.done.is_set() and job.result is None


def test_started_job_runs_to_the_end_and_cancel_is_too_late(qapp):
    from app.api.server import _Job

    job = _Job(lambda: "value")
    job.started = True                             # fn already began on the GUI thread
    job.cancel()
    assert job.cancelled is False                  # the result is dropped, fn not stopped


def test_timeout_path_marks_job_cancelled(qapp, monkeypatch):
    import app.api.server as server_mod

    jobs = []
    real_job = server_mod._Job

    class Spy(real_job):
        def __init__(self, fn):
            super().__init__(fn)
            jobs.append(self)

    monkeypatch.setattr(server_mod, "_Job", Spy)
    invoker = _Invoker()

    def worker():
        try:
            invoker.gui(lambda: "late", timeout=0.05)
        except ApiError:
            pass

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(5)
    assert len(jobs) == 1 and jobs[0].cancelled is True
    qapp.processEvents()                           # the orphaned job is skipped


def test_serve_forever_uses_a_short_poll_interval(qapp, tools, discovery, monkeypatch):
    import socketserver

    from app.api import server as server_mod

    seen = []
    original = socketserver.BaseServer.serve_forever

    def spy(self, poll_interval=0.5):
        seen.append(poll_interval)
        return original(self, poll_interval)

    monkeypatch.setattr(socketserver.BaseServer, "serve_forever", spy)
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    srv.stop()
    assert seen == [server_mod.SERVE_POLL_S] and server_mod.SERVE_POLL_S <= 0.05


def test_stop_is_quick(qapp, tools, discovery):
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    time.sleep(0.1)
    started = time.monotonic()
    srv.stop()
    assert time.monotonic() - started < 1.0


class _SignalCtrl(QtCore.QObject):
    """Controller stand-in with the signals ApiService connects to."""

    message = QtCore.Signal(str, str)
    command_reply = QtCore.Signal(bool, str, str)
    data_reset = QtCore.Signal()


def test_restarts_do_not_leak_controller_connections(qapp, discovery):
    ctrl = _SignalCtrl()
    srv = ApiServer(ctrl, None, port=0, token=TOKEN)   # the real ApiService
    service = srv._service
    for _ in range(3):
        srv.start()
        srv.stop()
    srv.start()
    ctrl.message.emit("info", "once")
    assert [e["text"] for e in service._since(0)] == ["once"]   # one connection, not four
    srv.stop()
    ctrl.message.emit("info", "after stop")
    assert [e["text"] for e in service._since(0)] == ["once"]


# ------------------------------------------------------- request framing (L4, L7)

def raw_exchange(srv, payload: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes on a new connection and return everything the server sends back."""
    with socket.create_connection(("127.0.0.1", srv.port), timeout=timeout) as sock:
        sock.sendall(payload)
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data


def status_of(data: bytes) -> int:
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else -1


def post_with_length(srv, value: str, body: bytes = b"{}") -> bytes:
    return (f"POST /api/v1/tools/echo HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Length: {value}\r\n\r\n").encode() + body


@pytest.mark.parametrize("value", ["+2", "1_0", "-1", "0x10", "2.0", "", "abc", "٢"])
def test_content_length_must_be_plain_digits(qapp, server, value):
    data = wait_in_thread(qapp, lambda: raw_exchange(server, post_with_length(server, value)))
    assert status_of(data) == 400
    assert b"invalid Content-Length" in data


def test_duplicate_content_length_is_400(qapp, server):
    payload = (f"POST /api/v1/tools/echo HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
               f"Authorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n"
               f"Content-Length: 2\r\n\r\n{{}}").encode()
    data = wait_in_thread(qapp, lambda: raw_exchange(server, payload))
    assert status_of(data) == 400 and b"duplicate Content-Length" in data


def test_deeply_nested_json_is_a_400_not_a_500(qapp, server):
    body = b"[" * 200_000 + b"]" * 200_000
    status, parsed, _ = do(qapp, server, "POST", "/api/v1/tools/echo", raw=body, headers=AUTH)
    assert status == 400
    assert parsed == {"ok": False, "error": "request body is not valid JSON"}


def test_overflow_and_value_errors_from_tools_are_fixed_400s(qapp, server):
    status, parsed, _ = do(qapp, server, "POST", "/api/v1/tools/overflow", body={}, headers=AUTH)
    assert status == 400 and parsed == {"ok": False, "error": "number out of range"}
    status, parsed, _ = do(qapp, server, "POST", "/api/v1/tools/valerr", body={}, headers=AUTH)
    assert status == 400 and parsed == {"ok": False, "error": "invalid argument"}
    assert b"secret" not in json.dumps(parsed).encode()


def test_lone_surrogate_in_a_request_is_answered_with_ascii_json(qapp, server):
    body = b'{"a": "\\ud800 bad", "b": "caf\\u00e9"}'
    status, parsed, headers = do(qapp, server, "POST", "/api/v1/tools/echo", raw=body,
                                 headers=AUTH)
    assert status == 200
    assert parsed["result"]["params"] == {"a": "\ud800 bad", "b": "caf\u00e9"}


def test_response_bodies_are_ascii(qapp, server):
    data = wait_in_thread(qapp, lambda: raw_exchange(
        server, (f"GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1:{server.port}\r\n\r\n")
        .encode()))
    body = data.split(b"\r\n\r\n", 1)[1]
    assert body and all(b < 128 for b in body)


# ------------------------------------------------------- timeouts and capacity (M2)

def test_handler_has_a_socket_timeout():
    assert _Handler.timeout == server_mod.HANDLER_TIMEOUT_S == 10


def test_idle_connection_is_dropped(qapp, server, monkeypatch):
    monkeypatch.setattr(_Handler, "timeout", 0.3)
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        started = time.monotonic()
        assert sock.recv(1024) == b""               # the server closed it
        assert time.monotonic() - started < 4


def test_short_body_connection_is_dropped(qapp, server, monkeypatch):
    monkeypatch.setattr(_Handler, "timeout", 0.3)
    head = (f"POST /api/v1/tools/echo HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\n{{}}").encode()
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(head)
        assert sock.recv(1024) == b""
    status, _, _ = do(qapp, server, "GET", "/api/v1/health")   # the server is still fine
    assert status == 200


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_handler_threads_are_capped_and_slots_come_back(qapp, tools, discovery, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_HANDLER_THREADS", 2)
    monkeypatch.setattr(server_mod, "HEALTH_RESERVE_THREADS", 1)
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    try:
        slots = srv._httpd._slots
        reserve = srv._httpd._reserve_slots
        idle = [socket.create_connection(("127.0.0.1", srv.port), timeout=5) for _ in range(2)]
        assert _wait_for(lambda: slots._value == 0)      # the general pool is full

        # Health still answers, on a reserve connection, while idle clients hold the pool.
        data = wait_in_thread(qapp, lambda: raw_exchange(
            srv, (f"GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\n\r\n")
            .encode()))
        assert status_of(data) == 200

        # A reserve connection serves health only: other requests get a drained 503.
        body = b"{}"
        data = wait_in_thread(qapp, lambda: raw_exchange(
            srv, (f"POST /api/v1/tools/echo HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\n"
                  f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n")
            .encode() + body))
        assert status_of(data) == 503
        assert b"Connection: close" in data and b"too many concurrent requests" in data

        # With the reserve taken too, a new connection is refused at accept time.
        idle.append(socket.create_connection(("127.0.0.1", srv.port), timeout=5))
        assert _wait_for(lambda: reserve._value == 0)
        data = wait_in_thread(qapp, lambda: raw_exchange(
            srv, (f"GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\n\r\n")
            .encode()))
        assert status_of(data) == 503
        assert b"Retry-After: 1" in data and b"too many concurrent requests" in data

        for sock in idle:
            sock.close()
        assert _wait_for(lambda: slots._value == 2 and reserve._value == 1)

        # Every outcome releases its slot: 200, 401, 400, 404 in a row, with a cap of 2.
        cases = [
            ("GET", "/api/v1/health", None, None, 200),
            ("POST", "/api/v1/tools/echo", None, b"{}", 401),
            ("POST", "/api/v1/tools/echo", AUTH, b"{bad", 400),
            ("GET", "/api/v1/nope", None, None, 404),
        ]
        for _ in range(2):
            for method, path, headers, raw, expected in cases:
                status, _, _ = wait_in_thread(qapp, lambda m=method, p=path, h=headers, r=raw:
                                              http_request(srv, m, p, raw=r, headers=h))
                assert status == expected
        assert _wait_for(lambda: slots._value == 2 and reserve._value == 1)
    finally:
        srv.stop()


def test_refused_connection_gets_its_503_after_the_body_is_drained(qapp, tools, discovery,
                                                                   monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_HANDLER_THREADS", 1)
    monkeypatch.setattr(server_mod, "HEALTH_RESERVE_THREADS", 0)
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    try:
        idle = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        assert _wait_for(lambda: srv._httpd._slots._value == 0)
        body = b'{"pad": "' + b"x" * 5000 + b'"}'
        payload = (f"POST /api/v1/tools/echo HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n"
                   f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        data = wait_in_thread(qapp, lambda: raw_exchange(srv, payload))
        assert status_of(data) == 503
        head, _, rest = data.partition(b"\r\n\r\n")
        assert b"Connection: close" in head and b"Retry-After: 1" in head
        assert json.loads(rest.decode("utf-8")) == {"ok": False,
                                                    "error": "too many concurrent requests: "
                                                             "retry later"}
        idle.close()
    finally:
        srv.stop()


def test_capacity_503_on_a_reserve_connection_drains_the_body(qapp, tools, discovery,
                                                              monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_HANDLER_THREADS", 1)
    monkeypatch.setattr(server_mod, "HEALTH_RESERVE_THREADS", 1)
    srv = ApiServer(None, None, port=0, token=TOKEN,
                    service_factory=lambda ctx: FakeService(ctx))
    srv.start()
    try:
        idle = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        assert _wait_for(lambda: srv._httpd._slots._value == 0)
        body = b"{" + b'"pad": "' + b"y" * 3000 + b'"}'
        payload = (f"POST /api/v1/tools/echo HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n"
                   f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n"
                   ).encode() + body
        data = wait_in_thread(qapp, lambda: raw_exchange(srv, payload))
        assert status_of(data) == 503
        assert b"Connection: close" in data
        idle.close()
    finally:
        srv.stop()


class HoldService(FakeService):
    """Adds a long-wait tool (`hold`, blocks until released) and a rate-limited tool."""

    def call(self, name, params):
        if name == "hold":
            self.release.wait(10)
            return {"held": True}
        if name == "limited":
            raise RateLimited("too many send_command calls: at most 20 per second")
        return super().call(name, params)


HOLD_TOOLS = [
    Tool("hold", "Blocks until released.", {"type": "object", "properties": {}}),
    Tool("limited", "Always rate limited.", {"type": "object", "properties": {}}),
]


@pytest.fixture
def hold_server(qapp, tools, discovery, monkeypatch):
    monkeypatch.setattr(schema, "TOOLS", list(FAKE_TOOLS) + HOLD_TOOLS, raising=False)
    monkeypatch.setattr(schema, "TOOLS_BY_NAME", {t.name: t for t in FAKE_TOOLS + HOLD_TOOLS},
                        raising=False)
    monkeypatch.setattr(schema, "LONG_WAIT_TOOLS", frozenset({"hold"}), raising=False)
    release = threading.Event()

    def factory(ctx):
        service = HoldService(ctx)
        service.release = release
        return service

    srv = ApiServer(None, None, port=0, token=TOKEN, service_factory=factory)
    srv.start()
    srv.release_event = release
    yield srv
    release.set()
    srv.stop()


def test_long_wait_tools_are_the_listed_set():
    assert schema.LONG_WAIT_TOOLS == frozenset({"wait", "measure", "wait_until",
                                                "capture_event", "get_events"})
    assert server_mod.MAX_LONG_WAIT_CALLS == 8


def test_ninth_long_wait_gets_429_and_health_and_short_tools_still_answer(qapp, hold_server):
    srv = hold_server
    results = []

    def hold():
        results.append(http_request(srv, "POST", "/api/v1/tools/hold", body={}, headers=AUTH)[0])

    threads = [threading.Thread(target=hold, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    long_slots = srv._httpd._long_slots
    assert _wait_for(lambda: long_slots._value == 0)

    status, parsed, headers = do(qapp, srv, "POST", "/api/v1/tools/hold", body={}, headers=AUTH)
    assert status == 429
    assert parsed == {"ok": False, "error": "too many concurrent waiting calls (max 8)"}
    assert headers.get("Retry-After") == "1"
    assert do(qapp, srv, "GET", "/api/v1/health")[0] == 200
    assert do(qapp, srv, "POST", "/api/v1/tools/echo", body={"a": 1}, headers=AUTH)[0] == 200

    srv.release_event.set()
    for t in threads:
        t.join(10)
    assert results == [200] * 8
    assert _wait_for(lambda: long_slots._value == 8)


def test_rate_limited_service_answer_is_429(qapp, hold_server):
    status, parsed, headers = do(qapp, hold_server, "POST", "/api/v1/tools/limited", body={},
                                 headers=AUTH)
    assert status == 429
    assert parsed == {"ok": False, "error": "too many send_command calls: at most 20 per second"}
    assert headers.get("Retry-After") == "1"


# ----------------------------------------------------------- health proof (challenge)

def test_health_without_nonce_has_no_proof(qapp, server):
    status, body, _ = do(qapp, server, "GET", "/api/v1/health")
    assert status == 200 and "proof" not in body["result"]


@pytest.mark.parametrize("nonce", ["0123456789abcdef", "ABCDEF0123456789" * 8,
                                   "f" * 128, "a1b2c3d4e5f60718"])
def test_health_proof_is_hmac_of_token_and_nonce(qapp, server, nonce):
    status, body, _ = do(qapp, server, "GET", f"/api/v1/health?nonce={nonce}")
    assert status == 200
    expected = hmac.new(TOKEN.encode("utf-8"), nonce.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    assert body["result"]["proof"] == expected
    assert body["result"]["app"] == "EdgePowerMeter"


@pytest.mark.parametrize("query", ["nonce=", "nonce=abc", "nonce=" + "a" * 15,
                                   "nonce=" + "a" * 129, "nonce=" + "g" * 16,
                                   "nonce=0123456789abcdef&nonce=0123456789abcdef",
                                   "nonce=01234567%2089abcdef", "nonce"])
def test_invalid_health_nonce_is_400(qapp, server, query):
    status, body, _ = do(qapp, server, "GET", f"/api/v1/health?{query}")
    assert status == 400 and body["ok"] is False


def test_health_proof_needs_no_auth_but_still_checks_host(qapp, server):
    status, _, _ = do(qapp, server, "GET", "/api/v1/health?nonce=" + "a" * 16,
                      headers={"Host": "evil.example"})
    assert status == 403


# ------------------------------------------------------------- discovery file (L5)

@pytest.mark.skipif(os.name != "posix", reason="symlinks and FIFOs")
def test_discovery_symlink_is_not_followed_or_removed(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    link = tmp_path / "api.json"
    link.symlink_to(victim)
    _remove_discovery_if_ours(link)
    assert victim.exists() and link.is_symlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs")
def test_discovery_fifo_does_not_block_shutdown(tmp_path):
    fifo = tmp_path / "api.json"
    os.mkfifo(fifo)
    worker = threading.Thread(target=_remove_discovery_if_ours, args=(fifo,), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert fifo.exists()


def test_discovery_removed_only_when_it_names_this_process(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
    _remove_discovery_if_ours(path)
    assert path.exists()
    path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    _remove_discovery_if_ours(path)
    assert not path.exists()
    _remove_discovery_if_ours(path)                  # missing file: quiet
