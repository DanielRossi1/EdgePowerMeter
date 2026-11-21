"""Automation API: the implementation of every tool in `schema.TOOLS`.

Qt-free except through the GUI context: every access to the controller,
settings or markers runs inside `ctx.gui(...)` (the GUI thread in the app,
the calling thread in tests). Waiting, statistics, file writes and CSV
parsing run in the calling thread on copies taken on the GUI thread.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
import os
import re
import stat
import threading
import time
from collections import deque
from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Protocol, Tuple, TypeVar

import numpy as np

from ..core.benchmark import BenchmarkInput, compute
from ..core.markers import Marker, MarkerList
from ..core.samples import Samples
from ..core.settings import CHOICES, AppSettings
from ..core.statistics import Statistics
from ..export.csv_io import export_csv, import_recording
from ..export.pdf_report import ReportOptions, export_pdf
from ..export.recorder import default_folder
from ..export.units import format_si
from ..i18n import tr
from ..ui.controller import SOURCE_IMPORT, STATE_STREAMING
from ..ui.widgets.port_discovery import PortDiscovery
from ..version import __version__
from . import ext as _ext
from .schema import (API_VERSION, EXPORT_DIRS_ENV, MARKER_LABEL_MAX_CHARS,
                     SEND_COMMAND_PER_SECOND, SETTINGS_SCHEMA, TOOLS_BY_NAME)

T = TypeVar("T")
log = logging.getLogger(__name__)

CALIBRATION_KEYS = ("shunt_ohm", "voltage_gain", "voltage_offset_v", "current_gain",
                    "current_offset_a")
_PY_TYPES = {"bool": bool, "int": int, "float": float, "str": str}

NO_DATA = "no data: start an acquisition or import a CSV first"
LOG_SIZE = 50             # app messages and command replies kept for the client
STATUS_MESSAGES = 10      # how many of them get_status returns
REPLY_WAIT_S = 1.0        # send_command waits this long for the firmware reply
# Verbs send_command forwards. SAVE (flash) and MODE (the app manages the output
# format) go through their own tools or not at all.
RAW_COMMAND_VERBS = ("HELLO", "GET", "SET", "SYNC")
NOT_RAW_VERBS = {"SAVE": "SAVE is not sent directly: use configure_device with "
                         "save_default=true",
                 "MODE": "MODE is managed by the app and cannot be sent"}


class ApiError(Exception):
    """User-facing error: message goes back to the client verbatim (HTTP 400 / MCP isError)."""


class RateLimited(ApiError):
    """A tool was called too often (HTTP 429)."""


class GuiContext(Protocol):
    def gui(self, fn: Callable[[], T], timeout: float = 30.0) -> T: ...   # run on GUI thread


class DirectContext:
    """Runs the call in the current thread (tests, or headless code already on the GUI thread)."""

    def gui(self, fn: Callable[[], T], timeout: float = 30.0) -> T:
        return fn()


# ------------------------------------------------------------ argument checks

def _validate(schema: dict, value: Any, name: str = "arguments") -> Any:
    """Check a JSON value against the subset of JSON Schema used by the tools.

    Applies defaults of object properties and returns the converted value
    (integers as int, numbers as float). Raises ApiError with a readable message.
    """
    kind = schema.get("type")
    if kind == "object":
        return _validate_object(schema, value, name)
    if kind == "array":
        return _validate_array(schema, value, name)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ApiError(f"{name} must be true or false")
        out: Any = value
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (isinstance(value, float) and not value.is_integer()):
            raise ApiError(f"{name} must be an integer")
        out = int(value)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(f"{name} must be a number")
        try:
            finite = math.isfinite(value)
            out = float(value)
        except OverflowError:
            raise ApiError(f"{name} is out of range")
        if not finite:
            raise ApiError(f"{name} must be a finite number")
    elif kind == "string":
        if not isinstance(value, str):
            raise ApiError(f"{name} must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError(f"{name} must be valid UTF-8 text (lone surrogates are not allowed)")
        if len(value) < schema.get("minLength", 0):
            raise ApiError(f"{name} must not be empty")
        if len(value) > schema.get("maxLength", len(value)):
            raise ApiError(f"{name} must be at most {schema['maxLength']} characters")
        out = value
    else:
        return value
    if "enum" in schema and out not in schema["enum"]:
        allowed = ", ".join(repr(v) if isinstance(v, str) else str(v) for v in schema["enum"])
        raise ApiError(f"{name} must be one of: {allowed}")
    if "minimum" in schema and out < schema["minimum"]:
        raise ApiError(f"{name} must be at least {schema['minimum']:g}")
    if "maximum" in schema and out > schema["maximum"]:
        raise ApiError(f"{name} must be at most {schema['maximum']:g}")
    if "exclusiveMinimum" in schema and out <= schema["exclusiveMinimum"]:
        raise ApiError(f"{name} must be greater than {schema['exclusiveMinimum']:g}")
    return out


def _validate_array(schema: dict, value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise ApiError(f"{name} must be an array")
    if len(value) < schema.get("minItems", 0):
        raise ApiError(f"{name} needs at least {schema['minItems']} item(s)")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        unit = "item" if schema["maxItems"] == 1 else "items"
        raise ApiError(f"{name} must have at most {schema['maxItems']} {unit}")
    items = schema.get("items")
    out = [_validate(items, v, f"{name}[{k}]") if items else v for k, v in enumerate(value)]
    if schema.get("uniqueItems"):
        seen: Dict[str, int] = {}
        for k, v in enumerate(out):
            key = json.dumps(v, sort_keys=True)
            if key in seen:
                raise ApiError(f"{name}[{k}] duplicates {name}[{seen[key]}] (items must be "
                               "unique)")
            seen[key] = k
    return out


def _validate_object(schema: dict, value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ApiError(f"{name} must be a JSON object")
    props = schema.get("properties", {})
    required = schema.get("required", [])
    # MCP clients often send null for "not set": an explicit null on an optional
    # known property means absent (defaults apply). Unknown keys still get rejected.
    value = {k: v for k, v in value.items()
             if not (v is None and k in props and k not in required)}
    unknown = sorted(str(k) for k in value if k not in props)
    if unknown and schema.get("additionalProperties", True) is False:
        allowed = ", ".join(sorted(props)) or "none"
        keys = ", ".join(repr(k) for k in unknown)
        raise ApiError(f"unknown {'key' if len(unknown) == 1 else 'keys'} {keys} in {name} "
                       f"(allowed: {allowed})")
    for req in required:
        if req not in value:
            raise ApiError(f"missing required '{req}' in {name}")
    out: dict = {}
    for key, sub in props.items():
        child = key if name == "arguments" else f"{name}.{key}"
        if key in value:
            out[key] = _validate(sub, value[key], child)
        elif "default" in sub:
            out[key] = sub["default"]
    return out


def _coerce_setting(key: str, value: Any) -> Any:
    """Type and choice check of one setting against its AppSettings field."""
    kind = _SETTING_TYPES.get(key)
    if kind is None:
        raise ApiError(f"'{key}' is not a setting the API can change")
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(f"{key} must be an integer")
    elif kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(f"{key} must be a number")
        value = float(value)
    elif kind is bool:
        if not isinstance(value, bool):
            raise ApiError(f"{key} must be true or false")
    elif kind is str:
        if not isinstance(value, str):
            raise ApiError(f"{key} must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError(f"{key} must be valid UTF-8 text")
        forbidden = _FORBIDDEN_NOTES_RE if key in _MULTILINE_SETTINGS else _FORBIDDEN_TEXT_RE
        if forbidden.search(value):
            raise ApiError(f"{key} must not contain control characters or bidi override "
                           "characters")
    choices = CHOICES.get(key)
    if choices and value not in choices:
        raise ApiError(f"{key} must be one of: {', '.join(repr(c) for c in choices)}")
    return value


def _same_command(reply_cmd: str, sent: str) -> bool:
    """The firmware echoes the whole command on success but only its verb on errors."""
    r = reply_cmd.strip().upper()
    s = sent.strip().upper()
    return bool(r) and (s == r or s.startswith(r + " "))


def _field_type(f) -> Any:
    # settings.py uses postponed annotations, so f.type is the type name.
    return _PY_TYPES.get(f.type, f.type) if isinstance(f.type, str) else f.type


_SETTING_TYPES: Dict[str, Any] = {f.name: _field_type(f) for f in fields(AppSettings)
                                  if f.name in SETTINGS_SCHEMA}

# Text that can hide or reorder what the user reads: C0 controls, DEL, and the Unicode
# bidi embedding/override/isolate characters. Every string setting is checked.
_FORBIDDEN_TEXT_RE = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
# Free text that may span lines (the report notes) also allows TAB and LF.
_MULTILINE_SETTINGS = frozenset({"report_notes"})
_FORBIDDEN_NOTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


# ------------------------------------------------------------- JSON helpers

def _jsonable(obj: Any) -> Any:
    """Plain JSON types: numpy scalars/arrays, dataclasses and paths converted,
    NaN and infinities turned into null."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return _jsonable(obj.value)
    return str(obj)


def _stats_dict(stats: Statistics) -> dict:
    d = dataclasses.asdict(stats)
    d["energy_j"] = stats.energy_wh * 3600.0
    d["charge_c"] = stats.charge_ah * 3600.0
    return d


def _statistics(part: Samples) -> Statistics:
    stats = Statistics.from_samples(part)
    if stats is None:
        raise ApiError("not enough samples in range (need at least 2)")
    return stats


def _marker_dict(m: Marker) -> dict:
    return {"id": m.id, "t": m.t, "label": m.label}


def _range_info(t0: float, t1: float, label: str) -> dict:
    return {"t0": float(t0), "t1": float(t1), "label": label}


def _device_config_dict(cfg) -> Optional[dict]:
    if cfg is None:
        return None
    d = dataclasses.asdict(cfg)
    d["sample_rate_hz"] = cfg.sample_rate_hz
    return d


def _config_matches(cfg, wanted: Dict[str, Any]) -> bool:
    actual = {"avg": cfg.avg, "vct_us": cfg.vct_us, "ict_us": cfg.ict_us, "oled": cfg.oled}
    return all(actual[k] == v for k, v in wanted.items())


def _within(path: str, root: str) -> bool:
    """True when `path` (both already real paths) is `root` or inside it."""
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) \
            == os.path.normcase(root)
    except ValueError:      # different drives (Windows)
        return False


def _export_roots(recordings: Path) -> List[str]:
    """Real folders the export/import tools may use: the recordings folder first, then the
    absolute folders of EPM_API_EXPORT_DIRS (os.pathsep-separated; others are ignored)."""
    roots = [os.path.realpath(os.path.expanduser(str(recordings)))]
    for entry in os.environ.get(EXPORT_DIRS_ENV, "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        candidate = os.path.expanduser(entry)
        if not os.path.isabs(candidate):
            log.warning("ignoring relative folder in %s: %r", EXPORT_DIRS_ENV, entry)
            continue
        real = os.path.realpath(candidate)
        if real not in roots:
            roots.append(real)
    return roots


def _roots_text(roots: List[str]) -> str:
    return ", ".join(roots)


def _resolve_file(raw: str, roots: List[str], base: Path, extension: str) -> Tuple[str, str]:
    """Split a client path into (real parent folder, file name) and check it is allowed.

    Relative paths are taken from `base` (the recordings folder). The parent folder is
    resolved with realpath, so '..' and symlinked folders cannot leave an allowed root.
    Raises ApiError with a message naming the allowed folders.
    """
    if "\x00" in raw:
        raise ApiError("invalid path: contains a NUL character")
    expanded = os.path.expanduser(raw)
    candidate = expanded if os.path.isabs(expanded) else os.path.join(str(base), expanded)
    name = os.path.basename(candidate)
    if not name or name in (".", ".."):
        raise ApiError(f"path must name a file ending in {extension}")
    if os.path.splitext(name)[1].lower() != extension:
        raise ApiError(f"file name must end in {extension} (allowed folders: "
                       f"{_roots_text(roots)})")
    parent = os.path.realpath(os.path.dirname(candidate) or ".")
    if not any(_within(parent, root) for root in roots):
        raise ApiError(f"path is outside the allowed folders: {_roots_text(roots)}")
    return parent, name


def _output_path(raw: str, overwrite: bool, roots: List[str], base: Path,
                 extension: str) -> Path:
    parent, name = _resolve_file(raw, roots, base, extension)
    if not os.path.isdir(parent):
        raise ApiError(f"folder does not exist: {parent}")
    final = os.path.join(parent, name)
    try:
        st = os.lstat(final)
    except FileNotFoundError:
        return Path(final)
    except OSError as e:
        raise ApiError(f"cannot write {final}: {e.strerror or e}") from e
    if stat.S_ISLNK(st.st_mode):
        raise ApiError(f"{final} is a symbolic link: refusing to write through it")
    if stat.S_ISDIR(st.st_mode):
        raise ApiError(f"{final} is a folder")
    if not overwrite:
        raise ApiError(f"{final} already exists (pass overwrite=true to replace it)")
    return Path(final)


# The same message for every reason an import path is refused, so a client cannot
# probe which files exist outside the allowed folders.
_NOT_ALLOWED = "file not found or not allowed"


def _input_path(raw: str, roots: List[str], base: Path, extension: str) -> Path:
    try:
        parent, name = _resolve_file(raw, roots, base, extension)
    except ApiError:
        raise ApiError(_NOT_ALLOWED)
    final = os.path.realpath(os.path.join(parent, name))
    if not any(_within(final, root) for root in roots):
        raise ApiError(_NOT_ALLOWED)
    try:
        st = os.stat(final)
    except (OSError, ValueError):
        raise ApiError(_NOT_ALLOWED)
    if not stat.S_ISREG(st.st_mode) or not os.access(final, os.R_OK):
        raise ApiError(_NOT_ALLOWED)
    return Path(final)


# ------------------------------------------------------------------ service

class ApiService:
    """Implements the tools. `call()` is safe to call from any (HTTP worker) thread.

    Extension tools (app/api/ext) are dispatched to their `handlers(service)` callables
    when there is no `_tool_<name>` method. Extension code may use these members:

    - public wrappers (safe from any thread): `range_samples(params)`, `file_roots()`,
      `recordings_folder()`, `output_path(raw, overwrite, extension)`,
      `input_path(raw, extension)`, `require_streaming()`, `sleep(seconds)`, `now()`,
      `live_samples(window_s)`;
    - `ctx.gui(fn)`: run `fn` on the GUI thread; every access to `ctrl` must go through it;
    - `ctrl` (the AcquisitionController) and `settings` (AppSettings), read or change them
      inside `ctx.gui`;
    - the same helpers the built-in tools use, stable for extensions: `_range_copy(params)`
      and `_range_view(params)` (GUI thread only) return (t0, t1, label, Samples);
      `_file_roots()` returns (recordings folder, allowed roots); `_output_path` and
      `_input_path` check file names; `_mark()` / `_since(mark)` read the app message log;
      `_require_streaming()`; `_sleep(s)`; `_monotonic()`.
    """

    def __init__(self, ctrl, settings: AppSettings, ctx: GuiContext,
                 on_settings_changed: Callable[[], None] = lambda: None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.ctrl = ctrl
        self.settings = settings
        self.ctx = ctx
        self.on_settings_changed = on_settings_changed
        self._sleep = sleep
        self._monotonic = monotonic
        self._log: Deque[dict] = deque(maxlen=LOG_SIZE)
        self._log_seq = 0
        self._log_lock = threading.Lock()
        self._data_gen = 0        # bumped on every data_reset (clear, import): see measure
        self._connections: List[Tuple[Any, Callable]] = []
        self._close_callbacks: List[Callable[[], None]] = []
        self._connect_callbacks: List[Callable[[], None]] = []
        # Latest parsed device replies; the sequence numbers let send_command
        # tell a reply that arrived after its command from an older one.
        self._cfg_seq = 0
        self._cfg_last: Optional[dict] = None
        self._info_seq = 0
        self._info_last: Optional[dict] = None
        self._ext_handlers: Optional[Dict[str, Callable[..., dict]]] = None
        self._ext_lock = threading.Lock()
        # Real path of the CSV the current data was imported from through this API (None
        # when the data came from a live run or was changed since). See _overwrite_guard.
        self._import_path: Optional[str] = None
        self._send_times: Deque[float] = deque()    # send_command call times (monotonic)
        self._send_lock = threading.Lock()
        self.ctx.gui(self._connect_signals)

    @property
    def attached(self) -> bool:
        """True while the controller's signals are connected (between connect() and close())."""
        return bool(self._connections)

    def connect(self) -> None:
        """Attach to the controller's signals (again after close()), then run the connect
        callbacks."""
        self.ctx.gui(self._connect_signals)
        for fn in list(self._connect_callbacks):
            try:
                fn()
            except Exception:
                log.exception("API connect callback %r failed", fn)

    def add_connect_callback(self, fn: Callable[[], None]) -> None:
        """Run `fn` at the end of every connect() (kept across close()).

        Each callback runs in its own try/except: an error is logged and the others still run.
        Extensions use it to re-create what they need when the service starts again.
        """
        self._connect_callbacks.append(fn)

    def _connect_signals(self) -> None:
        # GUI thread: the controller emits these from there.
        c = self.ctrl
        if self._connections or c is None or not (hasattr(c, "message")
                                                  and hasattr(c, "command_reply")):
            return
        pairs: List[Tuple[Any, Callable]] = [(c.message, self._on_message),
                                             (c.command_reply, self._on_reply)]
        if hasattr(c, "data_reset"):
            pairs.append((c.data_reset, self._on_data_reset))
        if hasattr(c, "device_config_received"):
            pairs.append((c.device_config_received, self._on_device_config))
        if hasattr(c, "device_info_received"):
            pairs.append((c.device_info_received, self._on_device_info))
        for signal, slot in pairs:
            signal.connect(slot)
            self._connections.append((signal, slot))

    def add_close_callback(self, fn: Callable[[], None]) -> None:
        """Run `fn` at the start of close(), before the controller signals are disconnected.

        Each callback runs in its own try/except: an error is logged and the others still run.
        Extensions use it to stop their own signal connections and wake their waiters.
        """
        self._close_callbacks.append(fn)

    def close(self) -> None:
        """Disconnect from the controller's signals (the server calls this on stop)."""
        callbacks, self._close_callbacks = self._close_callbacks, []
        for fn in callbacks:
            try:
                fn()
            except Exception:
                log.exception("API close callback %r failed", fn)
        try:
            self.ctx.gui(self._disconnect_signals)
        except (ApiError, RuntimeError):
            pass

    def _disconnect_signals(self) -> None:
        # GUI thread. A signal that is already gone is not an error.
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections = []

    def _on_data_reset(self) -> None:
        self._data_gen += 1
        self._import_path = None

    def _on_device_config(self, cfg) -> None:
        self._cfg_last = {"type": "config", "config": _device_config_dict(cfg)}
        self._cfg_seq += 1

    def _on_device_info(self, info) -> None:
        self._info_last = {"type": "hello", **dataclasses.asdict(info)}
        self._info_seq += 1

    def _on_message(self, level: str, text: str) -> None:
        self._append({"level": level, "text": text})

    def _on_reply(self, ok: bool, cmd: str, msg: str) -> None:
        if ok:
            text = f"device accepted {cmd}"
        else:
            text = f"device rejected {cmd}: {msg or 'no reason given'}"
        self._append({"level": "info" if ok else "error", "text": text,
                      "reply": {"ok": bool(ok), "cmd": cmd, "msg": msg}})

    def _append(self, entry: dict) -> None:
        with self._log_lock:
            self._log_seq += 1
            entry["seq"] = self._log_seq
            entry["time"] = time.time()
            self._log.append(entry)

    def _mark(self) -> int:
        """Position in the message log; entries after it were emitted later."""
        with self._log_lock:
            return self._log_seq

    def _since(self, mark: int) -> List[dict]:
        with self._log_lock:
            return [e for e in self._log if e["seq"] > mark]

    def _recent_messages(self, n: int) -> List[dict]:
        with self._log_lock:
            items = list(self._log)[-n:]
        return [{"time": e["time"], "level": e["level"], "text": e["text"]} for e in items]

    def _reply_since(self, mark: int, sent: List[str], ok: Optional[bool]) -> Optional[dict]:
        """First command reply after `mark` for one of the `sent` commands (ok=None: any)."""
        for e in self._since(mark):
            r = e.get("reply")
            if r is None or (ok is not None and r["ok"] != ok):
                continue
            if any(_same_command(r["cmd"], s) for s in sent):
                return r
        return None

    def call(self, name: str, params: Optional[dict]) -> dict:
        """Validate `params` against the tool's schema, run it, return a JSON-safe dict."""
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise ApiError(f"unknown tool '{name}'")
        args = _validate(tool.input_schema, {} if params is None else params)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            handler = self._extension_handlers().get(name)
            if handler is None:
                raise ApiError(f"tool '{name}' is not implemented")
        return _jsonable(handler(**args))

    def _extension_handlers(self) -> Dict[str, Callable[..., dict]]:
        """Handlers of all extensions, built once on first use (thread-safe)."""
        with self._ext_lock:
            if self._ext_handlers is None:
                self._ext_handlers = self._build_extension_handlers()
            return self._ext_handlers

    def _build_extension_handlers(self) -> Dict[str, Callable[..., dict]]:
        out: Dict[str, Callable[..., dict]] = {}
        for module in _ext.handler_modules():
            mod_name = module.__name__
            table = module.handlers(self)
            if not isinstance(table, dict):
                raise RuntimeError(f"{mod_name}.handlers() must return a dict")
            for name, fn in table.items():
                if not callable(fn):
                    raise RuntimeError(f"{mod_name}: handler for '{name}' is not callable")
                if name not in TOOLS_BY_NAME:
                    raise RuntimeError(f"{mod_name} has a handler for '{name}', which is "
                                       "not a declared tool")
                if hasattr(self, f"_tool_{name}"):
                    raise RuntimeError(f"{mod_name} has a handler for the built-in tool "
                                       f"'{name}'")
                if name in out:
                    raise RuntimeError(f"{mod_name}: a second handler for '{name}'")
                out[name] = fn
        return out

    # ------------------------------------------ public wrappers for extensions

    def range_samples(self, params: dict) -> Tuple[float, float, str, Samples]:
        """(t0, t1, label, samples copy) for range arguments (see schema.range_props).

        Errors (ApiError) when there is no data or too few samples. Any thread.
        """
        return self.ctx.gui(lambda: self._range_copy(params))

    def file_roots(self) -> List[str]:
        """Real folders the export and import tools may use (recordings folder first)."""
        return self.ctx.gui(self._file_roots)[1]

    def recordings_folder(self) -> Path:
        """The recordings folder (base for relative file paths)."""
        return self.ctx.gui(self._file_roots)[0]

    def output_path(self, raw: str, overwrite: bool, extension: str) -> Path:
        """Checked path for a file to write (e.g. extension='.png'). Raises ApiError."""
        base, roots = self.ctx.gui(self._file_roots)
        return _output_path(raw, overwrite, roots, base, extension)

    def input_path(self, raw: str, extension: str) -> Path:
        """Checked path of an existing file to read. Raises ApiError (a generic message)."""
        base, roots = self.ctx.gui(self._file_roots)
        return _input_path(raw, roots, base, extension)

    def require_streaming(self) -> None:
        """Raise ApiError unless the acquisition is streaming."""
        self.ctx.gui(self._require_streaming)

    def sleep(self, seconds: float) -> None:
        """Pause the calling thread (uses the injected sleep, e.g. a fake clock in tests)."""
        self._sleep(seconds)

    def now(self) -> float:
        """Monotonic seconds (uses the injected clock)."""
        return self._monotonic()

    def live_samples(self, window_s: float) -> Samples:
        """Copy of the last `window_s` seconds of recorded data (empty when there is none)."""
        if not window_s > 0:
            raise ApiError("window_s must be greater than 0")

        def snap() -> Samples:
            data = self.ctrl.data()
            if not len(data):
                return Samples.empty()
            k = int(np.searchsorted(data.t, data.t[-1] - window_s))
            return data.slice(k, len(data)).copy()

        return self.ctx.gui(snap)

    # ------------------------------------------------------- GUI-side helpers
    # Everything below named _*_now / _range_* / _guard_* runs on the GUI thread.

    def _status_now(self) -> dict:
        c = self.ctrl
        path = c.recording_path
        return {
            "state": c.state,
            "running": c.is_running,
            "port": c.port,
            "protocol": c.protocol,
            "source": c.source,
            "source_name": c.source_name,
            "samples": len(c.store),
            "duration_s": c.data().duration,
            "last_t": c.store.last_t if len(c.store) else None,
            "sample_rate_hz": c.recent_rate(),
            "lost_samples": c.lost_samples,
            "gaps": c.gaps,
            "recording_path": str(path) if path is not None else None,
            "recording_to_disk": path is not None and c.is_recording_to_disk,
            "has_unsaved_data": c.has_unsaved_data,
            "markers": len(c.markers),
            "device_info": dataclasses.asdict(c.device_info) if c.device_info is not None else None,
            "device_config": _device_config_dict(c.device_config),
            "last_clock_sync": c.last_clock_sync,
            "messages": self._recent_messages(STATUS_MESSAGES),
            "app_version": __version__,
            "api_version": API_VERSION,
        }

    def _device_now(self) -> dict:
        c = self.ctrl
        return {
            "device_info": dataclasses.asdict(c.device_info) if c.device_info is not None else None,
            "device_config": _device_config_dict(c.device_config),
            "protocol": c.protocol,
            "calibration": {k: getattr(self.settings, k) for k in CALIBRATION_KEYS},
        }

    def _settings_now(self) -> dict:
        base, roots = self._file_roots()
        return {"settings": {k: getattr(self.settings, k) for k in SETTINGS_SCHEMA},
                "recordings_folder": str(base),
                "export_roots": roots}

    def _streaming_now(self) -> bool:
        return self.ctrl.state == STATE_STREAMING and len(self.ctrl.store) > 0

    def _require_streaming(self) -> None:
        if self.ctrl.state != STATE_STREAMING:
            raise ApiError(f"not streaming (state: {self.ctrl.state}): start the acquisition "
                           "with start_acquisition first")

    def _guard_discard(self, discard: bool) -> None:
        c = self.ctrl
        if c.has_unsaved_data and len(c.store) and not discard:
            raise ApiError("unsaved data; pass discard_unsaved=true")

    def _guard_load(self, discard: bool) -> None:
        if self.ctrl.is_running:
            raise ApiError("stop the acquisition first (stop_acquisition)")
        self._guard_discard(discard)

    def _begin(self, port: str, discard: bool) -> None:
        if self.ctrl.is_running:
            raise ApiError("the acquisition is already running: stop_acquisition first")
        self._guard_discard(discard)
        self.ctrl.start(port)

    def _marker_now(self, label: str) -> Marker:
        m = self.ctrl.add_marker(None, label)
        if m is None:
            raise ApiError("no samples yet: wait until the device streams data")
        return m

    def _segment_bounds(self, data: Samples, markers: MarkerList,
                        index: int) -> Tuple[float, float, str]:
        bounds = markers.boundaries(data)
        n = max(0, len(bounds) - 1)
        if not 0 <= index < n:
            if n == 0:
                raise ApiError(f"no segment {index}: the recording has no segments")
            raise ApiError(f"no segment {index}: the recording has {n} segment(s), numbered 0 "
                           f"to {n - 1}")
        (a, la), (z, lz) = bounds[index], bounds[index + 1]
        return a, z, f"segment {index} ({la or 'start'} to {lz or 'end'})"

    def _range_bounds(self, data: Samples, markers: MarkerList,
                      p: dict) -> Tuple[float, float, str]:
        """Resolve the range arguments: segment > marker_from/marker_to > last_s > t0/t1."""
        first, last = float(data.t[0]), float(data.t[-1])
        if p.get("segment") is not None:
            return self._segment_bounds(data, markers, p["segment"])
        mf, mt = p.get("marker_from"), p.get("marker_to")
        if mf is not None or mt is not None:
            if mf is None or mt is None:
                raise ApiError("marker_from and marker_to must be given together")
            a, b = markers.get(mf), markers.get(mt)
            for mid, m in ((mf, a), (mt, b)):
                if m is None:
                    raise ApiError(f"no marker with id {mid}")
            lo, hi = sorted((a.t, b.t))
            return lo, hi, f"markers {mf} to {mt}"
        if p.get("last_s") is not None:
            return max(first, last - p["last_s"]), last, f"last {p['last_s']:g} s"
        explicit = p.get("t0") is not None or p.get("t1") is not None
        t0 = first if p.get("t0") is None else float(p["t0"])
        t1 = last if p.get("t1") is None else float(p["t1"])
        if t1 <= t0:
            raise ApiError("t1 must be greater than t0")
        return t0, t1, "custom time range" if explicit else "all data"

    def _range_view(self, p: dict) -> Tuple[float, float, str, Samples]:
        """Samples in the requested range as a view (valid on the GUI thread only)."""
        if p.get("t0") is not None and p.get("t1") is not None \
                and float(p["t1"]) <= float(p["t0"]):
            raise ApiError("t1 must be greater than t0")
        c = self.ctrl
        data = c.data()
        if not len(data):
            raise ApiError(NO_DATA)
        if len(data) < 2:
            raise ApiError("not enough samples in range (need at least 2)")
        t0, t1, label = self._range_bounds(data, c.markers, p)
        part = data.between(t0, t1)
        if len(part) < 2:
            raise ApiError("not enough samples in range (need at least 2)")
        return t0, t1, label, part

    def _range_copy(self, p: dict) -> Tuple[float, float, str, Samples]:
        t0, t1, label, part = self._range_view(p)
        return t0, t1, label, part.copy()

    def _mark_saved_if_unchanged(self, n: int) -> None:
        # Mirrors FileActions: exporting the whole recording counts as saving it,
        # unless samples arrived meanwhile (they are not in the file).
        if len(self.ctrl.store) == n:
            self.ctrl.mark_saved()

    def _slice_copy(self, t0: float, t1: float) -> Samples:
        part = self.ctrl.data().between(t0, t1)
        if len(part) < 2:
            raise ApiError("not enough samples in range (need at least 2)")
        return part.copy()

    def _file_roots(self) -> Tuple[Path, List[str]]:
        """The recordings folder (base for relative paths) and the allowed export roots."""
        if hasattr(self.ctrl, "recordings_folder"):
            base = Path(self.ctrl.recordings_folder())
        else:                       # a controller-less service (tests, headless helpers)
            text = (getattr(self.settings, "recordings_dir", "") or "").strip()
            base = Path(text).expanduser() if text else default_folder()
        return base, _export_roots(base)

    def _report_options(self, title: Optional[str], notes: Optional[str]) -> ReportOptions:
        c, s = self.ctrl, self.settings
        return ReportOptions(
            title=s.report_title if title is None else title,
            notes=s.report_notes if notes is None else notes,
            include_graphs=s.pdf_include_graphs,
            include_psu=s.pdf_include_psu,
            include_spectrum=s.pdf_include_spectrum,
            spectrum_signal=s.spectrum_signal,
            metadata=self._report_metadata(),
            markers=copy.deepcopy(c.markers),
            benchmark=c.benchmark,
            device=self._device_section(),
        )

    def _report_metadata(self) -> Dict[str, str]:
        # Mirrors FileActions.report_metadata (kept in step with the GUI export).
        c = self.ctrl
        meta: Dict[str, str] = {}
        if c.source_name:
            meta[tr("Source")] = c.source_name
        if c.device_info is not None:
            meta[tr("Firmware")] = c.device_info.firmware
        if c.device_config is not None:
            cfg = c.device_config
            meta[tr("Sensor settings")] = tr(
                "averaging {avg}, conversion {vct}/{ict} µs ({rate})",
                avg=cfg.avg, vct=cfg.vct_us, ict=cfg.ict_us, rate=f"{cfg.sample_rate_hz:.1f} Hz")
        if c.is_v2:
            meta[tr("Lost samples")] = f"{c.lost_samples:,}"
        return meta

    def _device_section(self) -> Dict[str, str]:
        # Mirrors FileActions._device_section (kept in step with the GUI export).
        c, s = self.ctrl, self.settings
        out: Dict[str, str] = {}
        if c.device_config is not None:
            cfg = c.device_config
            out[tr("Averaging")] = str(cfg.avg)
            out[tr("Voltage conversion time")] = f"{cfg.vct_us} µs"
            out[tr("Current conversion time")] = f"{cfg.ict_us} µs"
            out[tr("Nominal sample rate")] = f"{cfg.sample_rate_hz:.1f} Hz"
            out[tr("Time reference")] = tr("RTC + SQW pulse (µs accurate)") if cfg.sqw \
                else tr("RTC polling (ms accurate)")
        out[tr("Shunt resistance")] = format_si(s.shunt_ohm, "Ohm")
        out[tr("Current offset")] = format_si(s.current_offset_a, "A")
        out[tr("Current gain")] = f"{s.current_gain:.5f}"
        out[tr("Voltage offset")] = format_si(s.voltage_offset_v, "V")
        out[tr("Voltage gain")] = f"{s.voltage_gain:.5f}"
        if s.host_averaging > 1:
            out[tr("Host averaging")] = str(s.host_averaging)
        if c.last_clock_sync:
            out[tr("Last synchronization")] = c.last_clock_sync
        return out

    def _list_ports(self, show_all: bool) -> dict:
        ports = []
        for device, label in PortDiscovery.get_ports(show_all):
            desc = label.split(" — ", 1)[1] if " — " in label else ""
            ports.append({"port": device, "description": desc})
        esp = [info.device for info in PortDiscovery.find_esp32_ports()]
        if esp:
            suggested: Optional[str] = esp[0]
        else:
            suggested = ports[0]["port"] if ports else None
        return {"ports": ports, "suggested": suggested}

    # ----------------------------------------------------- acquisition tools

    def _tool_get_status(self, **p) -> dict:
        return self.ctx.gui(self._status_now)

    def _tool_list_ports(self, **p) -> dict:
        return self._list_ports(p["show_all"])

    def _tool_start_acquisition(self, **p) -> dict:
        port = p.get("port")
        if port is None:
            port = self._list_ports(False)["suggested"]
            if port is None:
                raise ApiError("no serial port found: connect the meter or pass port")
        elif port not in self._known_ports():
            raise ApiError(f"unknown port {port!r} (use list_ports)")
        mark = self._mark()
        self.ctx.gui(lambda: self._begin(port, p["discard_unsaved"]))
        started = self._monotonic()
        deadline = started + p["wait_s"]
        while p["wait_s"] > 0:
            if self.ctx.gui(lambda: self._streaming_now() or not self.ctrl.is_running):
                break
            if self._monotonic() >= deadline:
                break
            self._sleep(0.1)
        status = self.ctx.gui(self._status_now)
        status["waited_s"] = round(self._monotonic() - started, 3)
        if not status["running"]:
            raise ApiError(self._start_failure(mark))
        if not (status["state"] == STATE_STREAMING and status["samples"] > 0):
            status["warning"] = "not streaming yet"
        return status

    @staticmethod
    def _known_ports() -> set:
        """Every serial device discovery lists (show_all included), as list_ports shows them."""
        return {device for device, _label in PortDiscovery.get_ports(True)}

    def _start_failure(self, mark: int) -> str:
        texts = [e["text"] for e in self._since(mark) if e["level"] in ("error", "warning")]
        if not texts:
            return "acquisition failed to start: the app reported no reason"
        return "acquisition failed to start: " + "; ".join(dict.fromkeys(texts))

    def _tool_stop_acquisition(self, **p) -> dict:
        def stop() -> dict:
            self.ctrl.stop()
            return self._status_now()
        return self.ctx.gui(stop)

    def _tool_clear_data(self, **p) -> dict:
        def clear() -> dict:
            self._guard_discard(p["discard_unsaved"])
            self.ctrl.clear()
            return self._status_now()
        return self.ctx.gui(clear)

    def _tool_get_live(self, **p) -> dict:
        window_s = p["window_s"]

        def snap():
            c = self.ctrl
            data = c.data()
            if not len(data):
                raise ApiError("no data yet")
            k = int(np.searchsorted(data.t, data.t[-1] - window_s))
            return {
                "t": float(data.t[-1]),
                "voltage_v": float(data.v[-1]),
                "current_a": float(data.i[-1]),
                "power_w": float(data.p[-1]),
                "window": data.slice(k, len(data)).copy(),
                "sample_rate_hz": c.recent_rate(),
                "energy_ws": c.stats.energy_ws,
                "charge_as": c.stats.charge_as,
                "samples": len(data),
                "state": c.state,
            }

        s = self.ctx.gui(snap)
        win = s.pop("window")
        energy_ws, charge_as = s.pop("energy_ws"), s.pop("charge_as")
        s.update({
            "window_s": window_s,
            "avg_voltage_v": float(np.mean(win.v, dtype=np.float64)),
            "avg_current_a": float(np.mean(win.i, dtype=np.float64)),
            "avg_power_w": float(np.mean(win.p, dtype=np.float64)),
            "energy_wh": energy_ws / 3600.0,
            "charge_ah": charge_as / 3600.0,
        })
        return s

    # -------------------------------------------------------------- markers

    def _tool_add_marker(self, **p) -> dict:
        def add() -> dict:
            m = self.ctrl.add_marker(p.get("t"), p["label"])
            if m is None:
                raise ApiError("cannot place a marker: no data yet (start the acquisition first)")
            return _marker_dict(m)
        return self.ctx.gui(add)

    def _tool_list_markers(self, **p) -> dict:
        def markers() -> dict:
            return {"markers": [_marker_dict(m) for m in self.ctrl.markers.sorted()]}
        return self.ctx.gui(markers)

    def _tool_update_marker(self, **p) -> dict:
        mid = p["id"]

        def update() -> dict:
            c = self.ctrl
            if c.markers.get(mid) is None:
                raise ApiError(f"no marker with id {mid}")
            if p.get("label") is None and p.get("t") is None:
                raise ApiError("nothing to update: give label and/or t")
            if p.get("label") is not None:
                c.rename_marker(mid, p["label"])
            if p.get("t") is not None:
                c.move_marker(mid, p["t"])
            return _marker_dict(c.markers.get(mid))
        return self.ctx.gui(update)

    def _tool_remove_marker(self, **p) -> dict:
        mid = p["id"]

        def remove() -> dict:
            if self.ctrl.markers.get(mid) is None:
                return {"removed": False}
            self.ctrl.remove_marker(mid)
            return {"removed": True}
        return self.ctx.gui(remove)

    # ------------------------------------------------------------- analysis

    def _tool_get_segments(self, **p) -> dict:
        data, markers = self.ctx.gui(lambda: (self.ctrl.data().copy(),
                                              copy.deepcopy(self.ctrl.markers)))
        out = []
        for k, seg in enumerate(markers.segments(data)):
            out.append({
                "index": k,
                "t0": seg.start,
                "t1": seg.end,
                "name": seg.name,
                "stats": _stats_dict(seg.stats) if seg.stats is not None else None,
            })
        return {"segments": out}

    def _tool_get_statistics(self, **p) -> dict:
        t0, t1, label, part = self.ctx.gui(lambda: self._range_copy(p))
        return {"range": _range_info(t0, t1, label), "stats": _stats_dict(_statistics(part))}

    def _tool_measure(self, **p) -> dict:
        duration, label = p["duration_s"], p["label"]
        self.ctx.gui(self._require_streaming)
        if p["settle_s"] > 0:
            self._pause(p["settle_s"])
        # The generation is read in the same GUI call as the start marker, so a
        # clear/import after it is detected below (it would restart the time axis).
        gen, start = self.ctx.gui(lambda: (self._data_gen, self._marker_now(label)))
        self._pause(duration)
        end = self.ctx.gui(lambda: self._marker_now(f"{label} end"))
        self._wait_for_samples(end.t)
        part = self.ctx.gui(lambda: self._measured_slice(gen, start, end))
        return {
            "start_marker": _marker_dict(start),
            "end_marker": _marker_dict(end),
            "range": _range_info(start.t, end.t, label),
            "stats": _stats_dict(_statistics(part)),
        }

    def _measured_slice(self, gen: int, start: Marker, end: Marker) -> Samples:
        """GUI thread: the window copy, or an error if the data was cleared or the start marker is gone."""
        if self._data_gen != gen:
            raise ApiError("the data was cleared during the measurement")
        if self.ctrl.markers.get(start.id) is None:
            raise ApiError("the measurement's start marker was removed during the measurement")
        return self._slice_copy(start.t, end.t)

    def _pause(self, seconds: float) -> None:
        """Sleep in short steps; fails early if the acquisition stops meanwhile."""
        end = self._monotonic() + seconds
        while True:
            left = end - self._monotonic()
            if left <= 0:
                return
            self._sleep(min(0.1, left))
            if not self.ctx.gui(lambda: self.ctrl.is_running):
                raise ApiError("acquisition stopped during measurement")

    def _wait_for_samples(self, t_end: float, max_s: float = 5.0) -> None:
        """Wait (bounded) until the stream has delivered samples up to t_end."""
        deadline = self._monotonic() + max_s
        while self.ctx.gui(lambda: self.ctrl.store.last_t) < t_end:
            if self._monotonic() >= deadline:
                return
            self._sleep(0.05)

    def _tool_benchmark(self, **p) -> dict:
        units, rate = p.get("units"), p.get("rate")
        if (units is None) == (rate is None):
            raise ApiError("give exactly one of units or rate")
        if p.get("idle_power_w") is not None and p.get("idle_segment") is not None:
            raise ApiError("give idle_power_w or idle_segment, not both")

        def snap():
            c = self.ctrl
            t0, t1, label, part = self._range_copy(p)
            idle_part = None
            if p.get("idle_segment") is not None:
                a, z, _ = self._segment_bounds(c.data(), c.markers, p["idle_segment"])
                idle_part = self._slice_copy(a, z)
            return t0, t1, label, part, idle_part

        t0, t1, label, part, idle_part = self.ctx.gui(snap)
        idle = p.get("idle_power_w")
        if idle_part is not None:
            idle = _statistics(idle_part).power_avg
        result = compute(_statistics(part), BenchmarkInput(units=units, rate=rate,
                                                          idle_power_w=idle))
        if result is None:
            raise ApiError("range or inputs not usable")
        return {"range": _range_info(t0, t1, label), "benchmark": dataclasses.asdict(result)}

    def _tool_get_samples(self, **p) -> dict:
        max_points = p["max_points"]
        names = p.get("quantities") or ["v", "i", "p"]

        def pick():
            t0, t1, label, part = self._range_view(p)
            n = len(part)
            decimated = n > max_points
            idx = np.linspace(0, n - 1, max_points).astype(np.int64) if decimated else None
            cols = {"t": part.t, "v": part.v, "i": part.i, "p": part.p}
            picked = {}
            for name in ("t", *names):
                col = cols[name]
                picked[name] = np.array(col[idx] if decimated else col, dtype=np.float64)
            return t0, t1, label, n, decimated, picked

        t0, t1, label, n, decimated, picked = self.ctx.gui(pick)
        out: Dict[str, Any] = {
            "range": _range_info(t0, t1, label),
            "count_in_range": n,
            "returned": len(picked["t"]),
            "decimated": decimated,
        }
        for name, arr in picked.items():
            out[name] = arr.tolist()
        return out

    # ---------------------------------------------------------- import/export

    def _tool_export_csv(self, **p) -> dict:
        base, roots = self._file_roots()
        path = _output_path(p["path"], p["overwrite"], roots, base, ".csv")
        if p["overwrite"]:
            self.ctx.gui(lambda: self._overwrite_guard(str(path)))

        def snap():
            t0, t1, label, part = self._range_copy(p)
            s = self.settings
            return (t0, t1, label, part, (s.csv_separator, s.csv_decimal, s.csv_time_format),
                    copy.deepcopy(self.ctrl.markers))

        t0, t1, label, part, (sep, dec, fmt), markers = self.ctx.gui(snap)
        try:
            export_csv(path, part, sep, dec, fmt, None, markers)
        except OSError as e:
            raise ApiError(f"cannot write {path}: {e.strerror or e}") from e
        if label == "all data":
            self.ctx.gui(lambda: self._mark_saved_if_unchanged(len(part)))
        return {"path": str(path), "rows": len(part), "range": _range_info(t0, t1, label)}

    def _tool_export_pdf(self, **p) -> dict:
        base, roots = self._file_roots()
        path = _output_path(p["path"], p["overwrite"], roots, base, ".pdf")
        if p["overwrite"]:
            self.ctx.gui(lambda: self._overwrite_guard(str(path)))

        def snap():
            t0, t1, label, part = self._range_copy(p)
            return t0, t1, label, part, self._report_options(p.get("title"), p.get("notes"))

        t0, t1, label, part, opts = self.ctx.gui(snap)
        try:
            export_pdf(path, part, opts)
        except OSError as e:
            raise ApiError(f"cannot write {path}: {e.strerror or e}") from e
        except ValueError as e:
            raise ApiError(str(e)) from e
        if label == "all data":
            self.ctx.gui(lambda: self._mark_saved_if_unchanged(len(part)))
        return {"path": str(path), "samples": len(part), "range": _range_info(t0, t1, label)}

    def _overwrite_guard(self, final: str) -> None:
        """GUI thread: refuse to overwrite the file the data comes from or the live recording.

        Overwriting replaces the whole file, so overwriting the source would destroy the
        recording the user is working on.
        """
        c = self.ctrl
        target = os.path.normcase(os.path.realpath(final))
        live = getattr(c, "recording_path", None)
        if live is not None and os.path.normcase(os.path.realpath(str(live))) == target:
            raise ApiError(f"refusing to overwrite {final}: it is the live recording file of "
                           "the current data")
        loaded = self._import_path
        if loaded is not None and os.path.normcase(loaded) == target:
            raise ApiError(f"refusing to overwrite {final}: the current data was loaded from "
                           "this file")
        # Data imported in the app (not through this API) is only known by its name.
        if getattr(c, "source", None) == SOURCE_IMPORT and getattr(c, "source_name", "") \
                and os.path.basename(final).lower() == str(c.source_name).lower():
            raise ApiError(f"refusing to overwrite {final}: the current data was loaded from a "
                           "file with this name")

    def _tool_import_csv(self, **p) -> dict:
        base, roots = self._file_roots()
        src = _input_path(p["path"], roots, base, ".csv")
        self.ctx.gui(lambda: self._guard_load(p["discard_unsaved"]))
        try:
            samples, markers = import_recording(src)
        except OSError:
            raise ApiError(_NOT_ALLOWED)
        except ValueError as e:
            raise ApiError(f"cannot read {src.name}: {e}") from e

        def load() -> dict:
            self._guard_load(p["discard_unsaved"])
            self.ctrl.load(samples, src.name, markers)
            self._import_path = os.path.normcase(os.path.realpath(str(src)))
            return self._status_now()
        return self.ctx.gui(load)

    # ----------------------------------------------------------------- device

    def _tool_get_device(self, **p) -> dict:
        return self.ctx.gui(self._device_now)

    def _tool_configure_device(self, **p) -> dict:
        avg, vct, ict = p.get("avg"), p.get("vct_us"), p.get("ict_us")
        oled, save = p.get("oled"), p["save_default"]
        wanted = {k: v for k, v in (("avg", avg), ("vct_us", vct), ("ict_us", ict),
                                    ("oled", oled)) if v is not None}
        if not wanted and not save:
            raise ApiError("nothing to configure: give avg, vct_us, ict_us, oled or save_default")
        cmds: List[str] = []
        if avg is not None:
            cmds.append(f"SET AVG {avg}")
        if vct is not None:
            cmds.append(f"SET VCT {vct}")
        if ict is not None:
            cmds.append(f"SET ICT {ict}")
        if oled is not None:
            cmds.append(f"SET OLED {1 if oled else 0}")
        if save:
            cmds.append("SAVE")

        def send() -> None:
            c = self.ctrl
            if c.reader is None or not c.is_v2:
                raise ApiError("configure_device needs a connected device with firmware 2.x "
                               "(start_acquisition first)")
            for cmd in cmds:
                if not c.send_command(cmd):
                    raise ApiError("the device is not connected")

        mark = self._mark()
        self.ctx.gui(send)
        confirmed = not wanted
        rejected = None
        deadline = self._monotonic() + 3.0
        while wanted:
            cfg = self.ctx.gui(lambda: self.ctrl.device_config)
            if cfg is not None and _config_matches(cfg, wanted):
                confirmed = True
                break
            rejected = self._reply_since(mark, cmds, False)
            if rejected is not None or self._monotonic() >= deadline:
                break
            self._sleep(0.1)
        if rejected is None:
            rejected = self._reply_since(mark, cmds, False)
        if rejected is not None:
            raise ApiError(f"the device rejected '{rejected['cmd']}': "
                           f"{rejected['msg'] or 'no reason given'}")
        result = self.ctx.gui(self._device_now)
        if not confirmed:
            result["warning"] = "the device has not confirmed the new configuration yet"
        return result

    def _tool_send_command(self, **p) -> dict:
        command = p["command"]
        if "\n" in command or "\r" in command:
            raise ApiError("command must be a single line")
        if any(ord(ch) > 127 for ch in command):
            # The wire encoder drops non-ASCII characters, so the device would see another
            # command than the one checked here.
            raise ApiError("command must be ASCII only")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in command):
            raise ApiError("command must not contain control characters")
        line = command.strip()
        if not line:
            raise ApiError("command is empty")
        words = line.split()
        verb = words[0].upper()
        if verb in NOT_RAW_VERBS:
            raise ApiError(NOT_RAW_VERBS[verb])
        if verb == "SET" and len(words) > 1 and words[1].upper() == "MODE":
            raise ApiError(NOT_RAW_VERBS["MODE"])
        if verb not in RAW_COMMAND_VERBS:
            raise ApiError(f"command '{verb}' is not allowed (allowed: "
                           f"{', '.join(RAW_COMMAND_VERBS)})")
        self._take_send_slot()

        def send() -> Optional[Tuple[int, int, int]]:
            # Marks are taken before sending: the reply may already be queued.
            marks = (self._mark(), self._cfg_seq, self._info_seq)
            return marks if self.ctrl.send_command(line) else None

        marks = self.ctx.gui(send)
        if marks is None:
            return {"sent": False}
        out: Dict[str, Any] = {"sent": True}
        reply = self._await_device_reply(marks, line, verb, REPLY_WAIT_S)
        if reply is not None:
            out["reply"] = reply
        return out

    def _take_send_slot(self) -> None:
        """Count one send_command call; RateLimited beyond SEND_COMMAND_PER_SECOND per second."""
        now = self._monotonic()
        with self._send_lock:
            while self._send_times and now - self._send_times[0] >= 1.0:
                self._send_times.popleft()
            if len(self._send_times) >= SEND_COMMAND_PER_SECOND:
                raise RateLimited(f"too many send_command calls: at most "
                                  f"{SEND_COMMAND_PER_SECOND} per second")
            self._send_times.append(now)

    def _await_device_reply(self, marks: Tuple[int, int, int], line: str, verb: str,
                            timeout: float) -> Optional[dict]:
        """First reply to `line`: a !OK/!ERR, or for GET/SET the !CFG and for HELLO the
        device info that arrived after the command was sent."""
        mark, cfg_seq, info_seq = marks
        deadline = self._monotonic() + timeout
        while True:
            r = self._reply_since(mark, [line], None)
            if r is not None:
                return {"ok": r["ok"], "cmd": r["cmd"], "msg": r["msg"]}
            if verb in ("GET", "SET") and self._cfg_seq != cfg_seq:
                return self._cfg_last
            if verb == "HELLO" and self._info_seq != info_seq:
                return self._info_last
            if self._monotonic() >= deadline:
                return None
            self._sleep(0.05)

    def _tool_sync_clock(self, **p) -> dict:
        return {"requested": self.ctx.gui(lambda: self.ctrl.request_clock_sync())}

    # --------------------------------------------------------------- settings

    def _tool_get_settings(self, **p) -> dict:
        return self.ctx.gui(self._settings_now)

    def _tool_update_settings(self, **p) -> dict:
        values = {k: _coerce_setting(k, v) for k, v in p["settings"].items()}

        def apply() -> dict:
            s = self.settings
            before = {k: getattr(s, k) for k in SETTINGS_SCHEMA}
            for key, value in values.items():
                setattr(s, key, value)
            requested = {k: getattr(s, k) for k in SETTINGS_SCHEMA}
            # The same validation AppSettings.load() applies, so this session and a restarted
            # app agree on the stored values.
            s.validate()
            adjusted = [{"key": k, "requested": requested[k], "stored": getattr(s, k)}
                        for k in SETTINGS_SCHEMA if requested[k] != getattr(s, k)]
            changed = sorted(k for k in SETTINGS_SCHEMA if before[k] != getattr(s, k))
            s.save()
            if any(k in CALIBRATION_KEYS for k in values):
                self.ctrl.apply_calibration()
            self.on_settings_changed()
            out = self._settings_now()
            out["applied"] = sorted(values)
            out["changed"] = changed
            out["adjusted"] = adjusted
            return out
        return self.ctx.gui(apply)

    def _tool_wait(self, **p) -> dict:
        started = self._monotonic()
        self._sleep(p["seconds"])
        return {"waited_s": round(self._monotonic() - started, 3)}
