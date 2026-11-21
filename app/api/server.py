"""Localhost HTTP server for the automation API, plus the GUI-thread invoker.

HTTP requests are handled on worker threads. Anything that touches the controller,
settings or the store is handed to the GUI thread through `_Invoker.gui`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from PySide6 import QtCore

from app.version import __version__

from . import schema
from .service import ApiError, ApiService, RateLimited

log = logging.getLogger(__name__)

APP_NAME = "EdgePowerMeter"
MAX_BODY_BYTES = 1024 * 1024
API_PREFIX = "/api/v1"
HEALTH_PATH = API_PREFIX + "/health"
TOOLS_PATH = API_PREFIX + "/tools"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
SERVE_POLL_S = 0.05     # shutdown() waits up to one poll interval
HANDLER_TIMEOUT_S = 10  # socket timeout per connection: idle or stalled clients are dropped
MAX_HANDLER_THREADS = 32  # concurrent connections; more get 503 (health excepted, see below)
HEALTH_RESERVE_THREADS = 4  # extra connections that may only ask for health once the pool is full
MAX_LONG_WAIT_CALLS = 8     # concurrent schema.LONG_WAIT_TOOLS calls; the next ones get 429
REFUSAL_THREADS = 16        # threads that drain and answer refused connections at accept time
DRAIN_TIMEOUT_S = 1.0       # total time to read a refused request's head and body
HEAD_MAX_BYTES = 16 * 1024
BUSY_MESSAGE = "too many concurrent requests: retry later"
INTERNAL_ERROR = "internal error"   # the details go to the log, never to the client
HEALTH_NONCE_RE = re.compile(r"[0-9a-fA-F]{16,128}")
CONTENT_LENGTH_RE = re.compile(r"[0-9]+")
JSON_DEPTH_ERRORS = (ValueError, RecursionError, OverflowError)


class _Job:
    """One call waiting to run on the GUI thread.

    `cancel()` and the GUI thread's start of `fn` are serialized by `lock`: a job
    cancelled before it starts never runs; one that already started runs to the end
    and only its result is dropped.
    """

    def __init__(self, fn: Callable[[], Any]):
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.exc: Optional[BaseException] = None
        self.lock = threading.Lock()
        self.cancelled = False
        self.started = False

    def cancel(self) -> None:
        with self.lock:
            if not self.started:
                self.cancelled = True


class _Invoker(QtCore.QObject):
    """Runs callables on the thread this object lives in (the GUI thread)."""

    _request = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._request.connect(self._run, QtCore.Qt.QueuedConnection)

    @QtCore.Slot(object)
    def _run(self, job: _Job) -> None:
        with job.lock:
            if job.cancelled:      # the caller already timed out: do not act late
                job.done.set()
                return
            job.started = True
        try:
            job.result = job.fn()
        except BaseException as exc:  # re-raised on the calling thread
            job.exc = exc
        finally:
            job.done.set()

    def gui(self, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        """Run `fn` on the GUI thread and return its result (or re-raise its error)."""
        if QtCore.QThread.currentThread() is self.thread():
            return fn()
        job = _Job(fn)
        self._request.emit(job)
        if not job.done.wait(timeout):
            job.cancel()
            raise ApiError("timed out waiting for the application")
        if job.exc is not None:
            raise job.exc
        return job.result


class _Reject(Exception):
    """Aborts request handling with an HTTP status and a client-visible message."""

    def __init__(self, status: int, message: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


# Set on each connection thread: True when the connection holds a reserve slot, which
# only GET /api/v1/health may use (see _Httpd.process_request).
_conn = threading.local()


def _busy_response() -> bytes:
    body = json.dumps({"ok": False, "error": BUSY_MESSAGE}, ensure_ascii=True).encode("ascii")
    return (b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Retry-After: 1\r\nConnection: close\r\n\r\n" + body)


def _read_head(sock, deadline: float) -> bytes:
    """Bytes read from a new connection up to the end of its request head (bounded)."""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < HEAD_MAX_BYTES:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def _declared_length(head: bytes) -> int:
    """The Content-Length of a request head (0 when absent or not plain digits)."""
    for line in head.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            value = value.strip()
            return int(value) if value.isdigit() else 0
    return 0


def _discard(sock, count: int, deadline: float) -> None:
    while count > 0:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        sock.settimeout(left)
        try:
            chunk = sock.recv(min(count, 65536))
        except OSError:
            return
        if not chunk:
            return
        count -= len(chunk)


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: Tuple[str, int], handler: type):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self._slots = threading.BoundedSemaphore(MAX_HANDLER_THREADS)
        self._reserve_slots = threading.BoundedSemaphore(HEALTH_RESERVE_THREADS)
        self._long_slots = threading.BoundedSemaphore(MAX_LONG_WAIT_CALLS)
        self._refusal_slots = threading.BoundedSemaphore(REFUSAL_THREADS)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):  # accept thread
        # A full pool still lets a few connections in, but those may only ask for health:
        # an idle client cannot block the health check once the general pool is taken.
        reserved = False
        pool = self._slots
        if not pool.acquire(blocking=False):
            if not self._reserve_slots.acquire(blocking=False):
                self._refuse(request)
                return
            reserved = True
            pool = self._reserve_slots
        thread = threading.Thread(target=self._connection_thread,
                                  args=(request, client_address, pool, reserved),
                                  name="epm-api-conn", daemon=True)
        try:
            thread.start()
        except BaseException:
            pool.release()             # no thread was started: give the slot back
            raise

    def _connection_thread(self, request, client_address, pool, reserved: bool) -> None:
        _conn.reserved = reserved
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                pool.release()

    def _refuse(self, request) -> None:
        """Answer a connection the pools cannot take, without blocking the accept loop.

        The request head and declared body are drained first (bounded), so the client reads
        the 503 instead of hitting a reset when the server closes with unread input.
        """
        if not self._refusal_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        thread = threading.Thread(target=self._refuse_now, args=(request,),
                                  name="epm-api-refuse", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._refusal_slots.release()
            self.shutdown_request(request)

    def _refuse_now(self, request) -> None:
        try:
            deadline = time.monotonic() + DRAIN_TIMEOUT_S
            head = _read_head(request, deadline)
            _, _, rest = head.partition(b"\r\n\r\n")
            remaining = min(_declared_length(head), MAX_BODY_BYTES) - len(rest)
            _discard(request, remaining, deadline)
            request.settimeout(1.0)
            request.sendall(_busy_response())
        except OSError:
            pass
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._refusal_slots.release()

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse DNS lookup; skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    api: "ApiServer" = None  # type: ignore[assignment]  # set on a subclass per server
    timeout = HANDLER_TIMEOUT_S   # socket timeout: idle, slow or short-body clients are dropped

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("api %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # -- routing -----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except _Reject as rej:
            headers = dict(rej.headers)
            if rej.status == 503:
                # Capacity refusals drain the request body first and close the connection,
                # so the client gets the answer and not a reset.
                self._discard_declared_body()
                headers["Connection"] = "close"
                self.close_connection = True
            self._send_quietly(rej.status, {"ok": False, "error": rej.message}, headers)
        except OSError as exc:
            # The client went away or stalled past the socket timeout: drop the connection.
            self.close_connection = True
            log.debug("API connection %s dropped: %s", self.address_string(), exc)
        except Exception:
            log.exception("API request %s %s failed", method, self.path)
            self._send_quietly(500, {"ok": False, "error": INTERNAL_ERROR})

    def _send_quietly(self, status: int, payload: Dict[str, Any],
                      headers: Optional[Dict[str, str]] = None) -> None:
        try:
            self._send(status, payload, headers)
        except OSError:
            self.close_connection = True

    def _route(self, method: str) -> None:
        self._check_host_and_origin()
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            if method != "GET":
                raise _Reject(405, "method not allowed")
            result: Dict[str, Any] = {
                "app": APP_NAME,
                "version": __version__,
                "api_version": schema.API_VERSION,
            }
            nonce = self._health_nonce()
            if nonce is not None:
                # Proof of possession of the token for a client that sent a nonce.
                result["proof"] = hmac.new(self.api._token.encode("utf-8"),
                                           nonce.encode("utf-8"), hashlib.sha256).hexdigest()
            self._send(200, {"ok": True, "result": result})
            return
        if getattr(_conn, "reserved", False):
            # Reserve connections serve health only (checked above); everything else waits.
            raise _Reject(503, BUSY_MESSAGE, {"Retry-After": "1"})
        if path == TOOLS_PATH:
            if method != "GET":
                raise _Reject(405, "method not allowed")
            self._require_auth()
            tools = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in schema.TOOLS
            ]
            self._send(200, {"ok": True, "result": {"tools": tools}})
            return
        if path.startswith(TOOLS_PATH + "/"):
            if method != "POST":
                raise _Reject(405, "method not allowed")
            self._require_auth()
            name = path[len(TOOLS_PATH) + 1:]
            if name not in schema.TOOLS_BY_NAME:
                raise _Reject(404, f"unknown tool: {name}")
            params = self._read_params()
            long_call = name in schema.LONG_WAIT_TOOLS
            if long_call and not self.server._long_slots.acquire(blocking=False):
                raise _Reject(429, f"too many concurrent waiting calls "
                                   f"(max {MAX_LONG_WAIT_CALLS})", {"Retry-After": "1"})
            try:
                result = self.api._service.call(name, params)
            except RateLimited as exc:
                self._send(429, {"ok": False, "error": str(exc)}, {"Retry-After": "1"})
                return
            except ApiError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            except OverflowError:
                raise _Reject(400, "number out of range")
            except (ValueError, RecursionError):
                raise _Reject(400, "invalid argument")
            finally:
                if long_call:
                    self.server._long_slots.release()
            self._send(200, {"ok": True, "result": result})
            return
        raise _Reject(404, "not found")

    def _health_nonce(self) -> Optional[str]:
        """The optional ?nonce=<hex 16-128 chars> of the health check (None when absent)."""
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if "nonce" not in query:
            return None
        values = query["nonce"]
        if len(values) != 1 or not HEALTH_NONCE_RE.fullmatch(values[0]):
            raise _Reject(400, "nonce must be 16 to 128 hexadecimal characters")
        return values[0]

    # -- checks --------------------------------------------------------------

    def _check_host_and_origin(self) -> None:
        allowed = self.api._allowed_hosts()
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed:
            raise _Reject(403, "forbidden: unexpected Host header")
        if self.headers.get("Origin") is not None:
            raise _Reject(403, "forbidden: requests with an Origin header are not allowed")

    def _require_auth(self) -> None:
        token = self.api._token
        if not token:
            return  # auth disabled (loopback only, enforced by ApiServer)
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode("utf-8"), token.encode("utf-8"))
        if not ok:
            raise _Reject(401, "unauthorized: send 'Authorization: Bearer <token>'",
                          {"WWW-Authenticate": "Bearer"})

    def _discard_declared_body(self) -> None:
        """Read and drop the request body (bounded by MAX_BODY_BYTES, socket timeout)."""
        if getattr(self, "_body_read", False) or self.headers.get("Transfer-Encoding"):
            return
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1 or not CONTENT_LENGTH_RE.fullmatch(values[0]):
            return
        remaining = min(int(values[0]), MAX_BODY_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            self.close_connection = True
        self._body_read = True

    def _read_params(self) -> Dict[str, Any]:
        self._body_read = True
        if self.headers.get("Transfer-Encoding"):
            raise _Reject(400, "chunked request bodies are not supported; send Content-Length")
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            raise _Reject(400, "duplicate Content-Length header")
        if not values:
            length = 0
        else:
            raw_len = values[0]
            if not CONTENT_LENGTH_RE.fullmatch(raw_len):
                raise _Reject(400, "invalid Content-Length")
            try:
                length = int(raw_len)
            except ValueError:          # more digits than int() accepts
                raise _Reject(413, f"request body larger than {MAX_BODY_BYTES} bytes")
        if length > MAX_BODY_BYTES:
            raise _Reject(413, f"request body larger than {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            params = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError,) + JSON_DEPTH_ERRORS:
            raise _Reject(400, "request body is not valid JSON")
        if not isinstance(params, dict):
            raise _Reject(400, "request body must be a JSON object")
        return params

    # -- output --------------------------------------------------------------

    def _send(self, status: int, payload: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None) -> None:
        # ensure_ascii: lone surrogates (possible in client JSON) are escaped, never fatal.
        try:
            body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
        except (TypeError,) + JSON_DEPTH_ERRORS:
            log.exception("could not encode an API response")
            status = 500
            body = json.dumps({"ok": False, "error": INTERNAL_ERROR}).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _write_discovery(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write the discovery file readable by the current user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_DISCOVERY_MAX_BYTES = 64 * 1024


def _remove_discovery_if_ours(path: Path) -> None:
    """Delete the discovery file when it still names this process.

    Only a regular file is read: symlinks, FIFOs and devices are left alone, and the
    open does not follow a link or block on a FIFO swapped in after the lstat.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return
    except OSError:
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(_DISCOVERY_MAX_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except (OSError,) + JSON_DEPTH_ERRORS:
        return
    finally:
        if fd != -1:
            os.close(fd)
    if isinstance(data, dict) and data.get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


class ApiServer(QtCore.QObject):
    """Serves the automation API on `host:port` while the app runs."""

    def __init__(self, ctrl: Any, settings: Any, port: int = schema.DEFAULT_PORT,
                 token: str = "", host: str = "127.0.0.1",
                 on_settings_changed: Callable[[], None] = lambda: None,
                 parent: Optional[QtCore.QObject] = None,
                 service_factory: Optional[Callable[[Any], Any]] = None):
        super().__init__(parent)
        if not token and not _is_loopback(host):
            raise ValueError("an API token is required when binding to a non-loopback host")
        self._host = host
        self._port = int(port)
        self._bound_port: Optional[int] = None
        self._token = token or ""
        self._invoker = _Invoker(self)
        if service_factory is None:
            self._service = ApiService(ctrl, settings, self._invoker,
                                       on_settings_changed=on_settings_changed)
        else:
            self._service = service_factory(self._invoker)
        self._httpd: Optional[_Httpd] = None
        self._thread: Optional[threading.Thread] = None
        self._discovery_path: Optional[Path] = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        """The bound port (the real one when constructed with port=0)."""
        return self._bound_port if self._bound_port is not None else self._port

    @property
    def url(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        """Bind, serve on a daemon thread and publish the discovery file."""
        if self._httpd is not None:
            return
        api = self

        class Handler(_Handler):
            pass

        Handler.api = api
        try:
            httpd = _Httpd((self._host, self._port), Handler)
        except OSError as exc:
            detail = exc.strerror or str(exc)
            raise OSError(f"cannot start the automation API on {self._host}:{self._port}: "
                          f"{detail}") from exc
        connect = getattr(self._service, "connect", None)
        if connect is not None:
            connect()               # re-attach if an earlier stop() detached the service
        self._httpd = httpd
        self._bound_port = httpd.server_address[1]
        # A short poll interval keeps stop() quick: it runs on the GUI thread.
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": SERVE_POLL_S},
                                        name="epm-api", daemon=True)
        self._thread.start()
        self._write_discovery_file()

    def stop(self) -> None:
        """Stop serving, join the thread and remove the discovery file if it is ours."""
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        httpd.shutdown()
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        close = getattr(self._service, "close", None)
        if close is not None:
            close()                 # drop the controller signal connections of this run
        if self._discovery_path is not None:
            _remove_discovery_if_ours(self._discovery_path)
            self._discovery_path = None

    # -- helpers used by the request handler ----------------------------------

    def _allowed_hosts(self) -> set:
        port = self.port
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _write_discovery_file(self) -> None:
        path = schema.api_discovery_path()
        data = {"url": self.url, "token": self._token, "pid": os.getpid(),
                "version": __version__}
        try:
            _write_discovery(path, data)
        except OSError:
            log.warning("could not write API discovery file %s", path, exc_info=True)
            return
        self._discovery_path = path
