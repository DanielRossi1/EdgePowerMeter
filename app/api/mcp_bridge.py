"""Model Context Protocol (MCP) bridge for the EdgePowerMeter automation API.

Runs as its own process (`python -m app.api.mcp_bridge`, or `edgepowermeter --mcp`) and
talks JSON-RPC 2.0 over stdio: one JSON object per line on stdin, one per line on stdout.
Tool calls are proxied to the running app's localhost HTTP API.

Stdlib only and Qt-free on purpose: it must start instantly and work without a display.
Logs go to stderr only; stdout carries protocol messages exclusively.

Security notes:
- The API token is only sent to a local http URL (127.0.0.1, ::1 or localhost), and only
  after the server proved it knows the token (an HMAC challenge on /api/v1/health).
- Redirects are refused and proxies are bypassed, so the token never leaves the target.
- Stdin lines are capped in size and parsed strictly (no NaN/Infinity), and every reply is
  encoded as strict JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from app.api import schema
from app.version import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "edgepowermeter"

NOT_RUNNING_TEXT = (
    "EdgePowerMeter is not running or its automation API is disabled "
    "(Settings → Automation API, or start it with `edgepowermeter --api` / `--headless`)"
)
REFUSED_URL_TEXT = (
    "the configured EdgePowerMeter API URL is not a local http address "
    "(http://127.0.0.1, http://[::1] or http://localhost); the API token was not sent"
)
NOT_IDENTIFIED_TEXT = (
    "the server at the configured API URL is not EdgePowerMeter (it did not prove knowledge "
    "of the API token); the API token was not sent"
)

INSTRUCTIONS = (
    "Drives EdgePowerMeter, a power meter (INA226 on an ESP32). Typical experiment: "
    "get_status; if not streaming, list_ports then start_acquisition (port defaults to the "
    "suggested one). Measure with measure(duration_s, label) for a timed window, or add_marker "
    "before and after a phase (use wait between them) and then get_statistics / get_samples / "
    "benchmark with marker_from / marker_to. Finish with stop_acquisition. All values are SI "
    "(V, A, W, Wh, Ah, J); times are seconds on the recording time axis. export_csv and "
    "export_pdf refuse to overwrite existing files unless overwrite=true."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEFAULT_TIMEOUT_S = 60.0
LONG_TIMEOUT_S = 600.0          # tools that read, write or analyse whole recordings
LONG_TOOLS = frozenset({"export_csv", "export_pdf", "import_csv", "get_statistics",
                        "get_segments", "benchmark", "get_samples"})
NON_FINITE_TEXT = "non-finite numbers are not allowed"
ACQUISITION_GRACE_S = 30.0
MEASURE_GRACE_S = 60.0
EXT_PARAM_GRACE_S = 60.0        # extension "param:<arg>" timeouts: argument + this
MAX_TIMEOUT_S = 86400.0 + 3600.0
IMAGE_MIME_TYPES = ("image/png", "image/jpeg")
IMAGE_MAX_BASE64_CHARS = 8 * 1024 * 1024
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
APP_NAME = "EdgePowerMeter"
HEALTH_PATH = "/api/v1/health"
HEALTH_TIMEOUT_S = 5.0
HEALTH_TTL_S = 30.0             # a verified (url, token) is trusted this long before checking again
MAX_LINE_BYTES = 2 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

PostFn = Callable[[str, str, Dict[str, Any], float], Dict[str, Any]]
GetFn = Callable[[str, float], Any]

_OVERSIZE = object()            # read_line marker: the line was longer than MAX_LINE_BYTES


class Target(NamedTuple):
    url: str
    token: str


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Surfaces every 3xx answer as an HTTPError instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# No proxies (a proxy would receive the token in clear text) and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def _reject_constant(name: str) -> Any:
    """parse_constant for stdin: NaN and Infinity are not JSON."""
    raise ValueError("non-standard JSON constant: {}".format(name))


def _app_constant(name: str) -> Any:
    """parse_constant for the app's answers: a non-finite number becomes null."""
    return None


def _strict_value(value: Any) -> Any:
    """Copy of value with non-finite floats replaced by None (JSON has no NaN/Infinity)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _strict_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict_value(item) for item in value]
    return value


def _dumps(payload: Any, indent: Optional[int] = None) -> str:
    return json.dumps(_strict_value(payload), allow_nan=False, indent=indent)


def _has_non_finite(value: Any) -> bool:
    """True when a parsed JSON value holds a NaN or an infinity anywhere inside it.

    Such a value cannot come from a NaN literal (stdin rejects those), but a literal like
    1e999 parses to infinity, and the strict encoder would turn it into null: "not given".
    """
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_non_finite(item) for item in value)
    return False


def _as_bytes(chunk: Any) -> bytes:
    if isinstance(chunk, str):
        return chunk.encode("utf-8", "surrogatepass")
    return chunk


def _valid_id(value: Any) -> bool:
    """JSON-RPC ids: a string, a finite number or null (booleans are not numbers here)."""
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. Always True where it cannot be told (Windows)."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:  # too large to be any process id
        return False
    except OSError:       # e.g. PermissionError: the process exists, owned by someone else
        return True
    return True


def default_target() -> Optional[Target]:
    """Resolve the app's API from EPM_API_URL / EPM_API_TOKEN, else the discovery file.

    A discovery file left behind by a killed app (its pid is gone) means "not running". A
    discovery file without a usable pid is refused the same way: the pid is the only
    liveness evidence it carries.
    """
    env_url = os.environ.get("EPM_API_URL", "").strip()
    if env_url:
        return Target(env_url, os.environ.get("EPM_API_TOKEN", "").strip())
    try:
        with open(schema.api_discovery_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url:
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not pid_alive(pid):
        return None
    token = data.get("token")
    return Target(url, token if isinstance(token, str) else "")


def _api_url_problem(url: str) -> Optional[str]:
    """None if url is a plain local http URL the token may be sent to, else the text to report."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return REFUSED_URL_TEXT
    if (parts.scheme != "http" or parts.hostname not in ALLOWED_HOSTS
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        return REFUSED_URL_TEXT
    return None


def _proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _proves_identity(body: Any, token: str, nonce: str) -> bool:
    """Whether a health answer is EdgePowerMeter's, proven with the token for this nonce."""
    if not isinstance(body, dict) or body.get("ok") is not True:
        return False
    result = body.get("result")
    if not isinstance(result, dict) or result.get("app") != APP_NAME:
        return False
    proof = result.get("proof")
    if not isinstance(proof, str):
        return False
    return hmac.compare_digest(proof.encode("utf-8"), _proof(token, nonce).encode("utf-8"))


def default_http_get(url: str, timeout: float) -> Any:
    """GET a URL and return the decoded JSON body (no credentials are sent).

    Connection failures, timeouts and redirects propagate as OSError; a body that is not
    JSON raises ValueError.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"), parse_constant=_app_constant)


def default_http_post(url: str, token: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST a JSON body and return the decoded `{ok, result|error}` envelope.

    HTTP 4xx/5xx responses carry the same envelope, so their bodies are returned, not raised.
    A redirect is an error, never followed. Connection failures and timeouts propagate as
    OSError.
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        # allow_nan=False: a non-finite number is an error here, never a silent null.
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        return {"ok": False, "error": "the arguments could not be encoded as JSON"}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return {"ok": False, "error": "EdgePowerMeter answered with a redirect "
                                          "(HTTP {}); redirects are refused".format(exc.code)}
        raw = exc.read()
        status = exc.code
        reason = exc.reason
    else:
        status, reason = 200, ""
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_app_constant)
    except (ValueError, RecursionError):
        body = None
    if isinstance(body, dict) and "ok" in body:
        return body
    return {"ok": False, "error": "HTTP {} {}".format(status, reason).strip()}


def tool_timeout_s(name: str, arguments: Dict[str, Any]) -> float:
    """Seconds to wait for the app's answer to a tool call (long waits get extra time)."""

    def num(key: str, default: float) -> float:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        try:
            number = float(value)
        except OverflowError:  # a huge int: not a usable duration
            return default
        if not math.isfinite(number):
            return default
        return min(max(number, 0.0), MAX_TIMEOUT_S)

    if name == "measure":
        return num("duration_s", 0.0) + num("settle_s", 0.0) + MEASURE_GRACE_S
    if name == "wait":
        return num("seconds", 0.0) + MEASURE_GRACE_S
    if name == "start_acquisition":
        return num("wait_s", 10.0) + ACQUISITION_GRACE_S
    if name in LONG_TOOLS:
        return LONG_TIMEOUT_S
    spec = schema.EXTENSION_TIMEOUTS.get(name)
    if spec == schema.TIMEOUT_LONG:
        return LONG_TIMEOUT_S
    if spec is not None:
        arg = spec[len(schema.TIMEOUT_PARAM_PREFIX):]
        tool = schema.TOOLS_BY_NAME.get(name)
        prop = tool.input_schema.get("properties", {}).get(arg, {}) if tool else {}
        default = prop.get("default")
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            default = 0.0
        return min(num(arg, float(default)) + EXT_PARAM_GRACE_S, MAX_TIMEOUT_S)
    return DEFAULT_TIMEOUT_S


def _error(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _split_image(result: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Turn a tool result's "image" entry into an MCP image block.

    Returns (image content block or None, the result without the base64 data). Raises
    ValueError with the reason when the image entry is malformed.
    """
    if "image" not in result:
        return None, result
    image = result["image"]
    if not isinstance(image, dict):
        raise ValueError("image must be an object with mime_type and data_base64")
    mime = image.get("mime_type")
    data = image.get("data_base64")
    if mime not in IMAGE_MIME_TYPES:
        raise ValueError("image mime_type must be one of: " + ", ".join(IMAGE_MIME_TYPES))
    if not isinstance(data, str) or not data:
        raise ValueError("image data_base64 must be a non-empty string")
    if len(data) > IMAGE_MAX_BASE64_CHARS:
        raise ValueError("image is larger than 8 MiB")
    if _BASE64_RE.fullmatch(data) is None:
        raise ValueError("image data_base64 is not base64")
    rest = {key: value for key, value in result.items() if key != "image"}
    rest["image"] = {"mime_type": mime}
    return {"type": "image", "data": data, "mimeType": mime}, rest


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, socket.timeout):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, socket.timeout)


class McpBridge:
    """JSON-RPC 2.0 / MCP server over a pair of streams.

    stdin may be a binary stream (each line is decoded on its own) or a text stream.
    """

    def __init__(
        self,
        stdin: IO[Any],
        stdout: IO[str],
        resolve_target: Optional[Callable[[], Optional[Target]]] = None,
        http_post: Optional[PostFn] = None,
        http_get: Optional[GetFn] = None,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._resolve_target = resolve_target or default_target
        self._http_post = http_post or default_http_post
        self._http_get = http_get or default_http_get
        self._verified: Dict[Tuple[str, str], float] = {}  # (url, token) -> monotonic time

    # ----- transport -------------------------------------------------------------------

    def serve(self) -> None:
        """Read messages until EOF, answering each one. Returns cleanly on EOF."""
        while True:
            line = self._read_line()
            if line is None:
                return
            if line is _OVERSIZE:
                reply: Optional[Any] = _error(
                    None, INVALID_REQUEST,
                    "Invalid Request: line longer than {} bytes".format(MAX_LINE_BYTES))
            else:
                try:
                    reply = self._process_line(line)
                except Exception:  # keep serving whatever a line does
                    self._log("internal error reading a message: " + traceback.format_exc())
                    reply = _error(None, INTERNAL_ERROR, "Internal error")
            if reply is not None:
                self._send(reply)

    def _read_line(self) -> Any:
        """Next line as bytes (newline included), _OVERSIZE, or None at EOF. Never unbounded."""
        chunk = self._stdin.readline(MAX_LINE_BYTES + 1)
        if not chunk:
            return None
        data = _as_bytes(chunk)
        if data.endswith(b"\n"):
            return _OVERSIZE if len(data) - 1 > MAX_LINE_BYTES else data
        if len(data) > MAX_LINE_BYTES:
            self._discard_to_newline()
            return _OVERSIZE
        return data  # last line at EOF, without a newline

    def _discard_to_newline(self) -> None:
        while True:
            chunk = self._stdin.readline(64 * 1024)
            if not chunk or _as_bytes(chunk).endswith(b"\n"):
                return

    def _send(self, payload: Any) -> None:
        try:
            text = _dumps(payload)
        except (ValueError, TypeError, RecursionError):
            rid = payload.get("id") if isinstance(payload, dict) else None
            text = _dumps(_error(rid, INTERNAL_ERROR, "Internal error: reply could not be encoded"))
        self._stdout.write(text + "\n")
        self._stdout.flush()

    def _process_line(self, line: Any) -> Optional[Any]:
        try:
            text = (line.decode("utf-8") if isinstance(line, bytes) else line).strip()
            if not text:
                return None
            message = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:  # bad UTF-8, bad JSON, too deeply nested
            return _error(None, PARSE_ERROR, "Parse error: {}".format(exc))
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "Invalid Request: empty batch")
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        return self.handle(message)

    def _log(self, text: str) -> None:
        sys.stderr.write("[edgepowermeter-mcp] " + text + "\n")
        sys.stderr.flush()

    # ----- JSON-RPC dispatch -----------------------------------------------------------

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Answer one JSON-RPC message. Returns None for notifications (no "id")."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")
        if "id" not in message:
            return None  # notification: never answered, even when it fails
        rid = message["id"]
        method = message.get("method")
        if rid is None and isinstance(method, str) and method.startswith("notifications/"):
            return None  # a notification that carries "id": null is still a notification
        if not _valid_id(rid):
            return _error(None, INVALID_REQUEST,
                          "Invalid Request: id must be a string, a finite number or null")
        if not isinstance(method, str):
            return _error(rid, INVALID_REQUEST, "Invalid Request: missing method")
        try:
            result = self._dispatch(method, message.get("params"))
        except _RpcError as exc:
            return _error(rid, exc.code, exc.message)
        except Exception as exc:  # keep the server alive whatever a handler does
            self._log("internal error in {}: {}".format(method, traceback.format_exc()))
            return _error(rid, INTERNAL_ERROR, "Internal error: {}".format(exc))
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: Any) -> Dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._tools_list()
        if method == "tools/call":
            return self._tools_call(params)
        raise _RpcError(METHOD_NOT_FOUND, "Method not found: {}".format(method))

    def _initialize(self, params: Any) -> Dict[str, Any]:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self) -> Dict[str, Any]:
        tools: List[Dict[str, Any]] = [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in schema.TOOLS
        ]
        return {"tools": tools}

    def _tools_call(self, params: Any) -> Dict[str, Any]:
        if params is not None and not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        params = params or {}
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS, "Missing tool name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        # Unknown tools are a protocol error (MCP spec). Argument checks stay in the app.
        if name not in schema.TOOLS_BY_NAME:
            raise _RpcError(INVALID_PARAMS, "unknown tool: {}".format(name))
        if _has_non_finite(arguments):
            return _tool_error(NON_FINITE_TEXT)
        return self._run_tool(name, arguments)

    def _check_app(self, base_url: str, token: str) -> Optional[str]:
        """None if the server at base_url proves it is EdgePowerMeter, else the text to report.

        Runs before the bearer token is sent. The server must answer a fresh random nonce
        with an HMAC of it under the token, so a stale discovery file pointing at another
        local server (or at the app with a different token) gets nothing. Positive answers
        are cached for HEALTH_TTL_S per (url, token); failures are not, so a freshly started
        app is found at once.
        """
        key = (base_url, token)
        now = time.monotonic()
        seen = self._verified.get(key)
        if seen is not None and now - seen < HEALTH_TTL_S:
            return None
        nonce = secrets.token_hex(16)
        url = base_url + HEALTH_PATH + "?" + urllib.parse.urlencode({"nonce": nonce})
        try:
            body = self._http_get(url, HEALTH_TIMEOUT_S)
        except (OSError, ValueError, RecursionError):
            return NOT_RUNNING_TEXT
        if not _proves_identity(body, token, nonce):
            return NOT_IDENTIFIED_TEXT
        self._verified[key] = now
        return None

    def _run_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Proxy one tool call to the app. Failures become isError results, not RPC errors."""
        try:
            target = self._resolve_target()
        except Exception:
            self._log("target resolution failed: " + traceback.format_exc())
            target = None
        if target is None:
            return _tool_error(NOT_RUNNING_TEXT)
        problem = _api_url_problem(target.url)
        if problem is not None:
            return _tool_error(problem)

        base = target.url.rstrip("/")
        problem = self._check_app(base, target.token)
        if problem is not None:
            return _tool_error(problem)
        url = base + "/api/v1/tools/" + urllib.parse.quote(name, safe="")
        timeout = tool_timeout_s(name, arguments)
        try:
            body = self._http_post(url, target.token, arguments, timeout)
        except OSError as exc:
            if _is_timeout(exc):
                return _tool_error(
                    "EdgePowerMeter did not answer within {:.0f} s".format(timeout)
                )
            return _tool_error(NOT_RUNNING_TEXT)
        except (ValueError, RecursionError):
            return _tool_error("The request could not be encoded as JSON")

        if not isinstance(body, dict) or "ok" not in body:
            return _tool_error("Unexpected response from EdgePowerMeter")
        if body.get("ok") is True:
            try:
                result = _strict_value(body.get("result"))
                if result is None:
                    result = {}
            except (ValueError, TypeError, RecursionError):
                return _tool_error("EdgePowerMeter returned a result that could not be encoded")
            blocks: List[Dict[str, Any]] = []
            if isinstance(result, dict):
                try:
                    image, result = _split_image(result)
                except ValueError as exc:
                    return _tool_error("EdgePowerMeter returned an invalid image: {}".format(exc))
                if image is not None:
                    blocks.append(image)
            try:
                blocks.append({"type": "text", "text": _dumps(result, indent=2)})
            except (ValueError, TypeError, RecursionError):
                return _tool_error("EdgePowerMeter returned a result that could not be encoded")
            out: Dict[str, Any] = {"content": blocks, "isError": False}
            if isinstance(result, dict):
                out["structuredContent"] = result
            return out
        return _tool_error(str(body.get("error") or "unknown error"))


def main() -> int:
    """Entry point for `python -m app.api.mcp_bridge`."""
    stdin = getattr(sys.stdin, "buffer", sys.stdin)  # bytes: a bad line cannot poison the rest
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    bridge = McpBridge(stdin, sys.stdout)
    try:
        bridge.serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
