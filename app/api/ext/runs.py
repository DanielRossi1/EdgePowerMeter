"""Handlers of the "runs" extension: named experiment results stored as JSON files.

A run is one file, `<recordings folder>/runs/<name>.json`, written atomically. Reads
check the size (1 MiB) and the structure, so a corrupted or hand-edited file gives a
clean error instead of a crash. Handlers run on the HTTP worker thread and reach the
application only through `ApiService` (`ctx.gui` for every access to the controller).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ...core.statistics import Statistics
from ...version import __version__
from ..service import ApiError
from .runs_schema import METRIC_NAMES

RUNS_DIR = "runs"
SCHEMA_VERSION = 1
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_RUN_FILES = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024   # total bytes list_runs parses in one call
NOTES_MAX_CHARS = 2000
MAX_TAGS = 20
MAX_TAG_CHARS = 40
MAX_METADATA_KEYS = 50
META_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
META_VALUE_CHARS = 200
RANGE_KEYS = ("t0", "t1", "last_s", "segment", "marker_from", "marker_to")
# Settings that affect how the samples were measured, kept with each run.
SETTINGS_KEYS = ("shunt_ohm", "voltage_gain", "voltage_offset_v", "current_gain",
                 "current_offset_a", "host_averaging")
# Numeric fields of the stored statistics (null when not computable).
STAT_NUMBER_KEYS = ("duration_seconds", "sample_rate_hz", "voltage_min", "voltage_max",
                    "voltage_avg", "voltage_std", "current_min", "current_max", "current_avg",
                    "current_std", "current_rms", "power_min", "power_max", "power_avg",
                    "power_std", "energy_wh", "charge_ah", "energy_j", "charge_c",
                    "connected_seconds")
DISTRIBUTION_KEYS = ("p5", "p50", "p95", "p99")

# Metric name -> (label used in the summary sentence, extractor over a stored record).
_ENERGY_PER_SECOND = "energy_per_second_j"
METRIC_LABELS = {
    "mean_power_w": "mean power",
    "max_power_w": "maximum power",
    "min_power_w": "minimum power",
    "p95_power_w": "95th-percentile power",
    "p99_power_w": "99th-percentile power",
    "energy_j": "energy",
    "energy_per_second_j": "energy per second",
    "duration_s": "duration",
    "mean_current_a": "mean current",
    "mean_voltage_v": "mean voltage",
}

_write_lock = threading.Lock()   # serialises the check-then-write of save and delete

# Windows device names: refused on every platform so a run saved on Linux can be copied to Windows.
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{}".format(i) for i in range(1, 10)]
    + ["LPT{}".format(i) for i in range(1, 10)])
# Directory-relative file calls (no path re-resolution between the checks and the write).
# os.replace takes src_dir_fd/dst_dir_fd on POSIX (not listed in os.supports_dir_fd on every build).
_DIR_FD_OK = (os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
              and {os.open, os.unlink, os.lstat} <= os.supports_dir_fd)


# ----------------------------------------------------------------- names

def check_name(name: Any) -> str:
    """Return the run name, or raise ApiError when it is not a safe file stem."""
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None \
            or name.lower().endswith(".json") or name.endswith("."):
        raise ApiError("run name must be 1 to 64 characters: letters, digits, '.', '_' or "
                       "'-', starting with a letter or digit, not ending in .json or '.'")
    # Windows ignores the part after the first dot: 'con.report' is the device CON.
    if name.split(".", 1)[0].upper() in RESERVED_NAMES:
        raise ApiError("run name must not be a reserved Windows device name (CON, PRN, AUX, "
                       "NUL, COM1-COM9, LPT1-LPT9)")
    return name


# ----------------------------------------------------------------- folders and files

def _runs_dir(service, create: bool) -> Path:
    """`<recordings folder>/runs`. With create=True the folders are made (mode 0700 for runs).

    Refuses a runs folder that is a symbolic link or not a folder. Without create, a
    missing folder is returned as it is (the caller treats it as empty).
    """
    base = Path(service.recordings_folder())
    if create:
        try:
            os.makedirs(str(base), exist_ok=True)
        except OSError as e:
            raise ApiError(f"cannot create the recordings folder {base}: {e.strerror or e}")
    runs = base / RUNS_DIR
    try:
        st = os.lstat(str(runs))
    except FileNotFoundError:
        if not create:
            return runs
        try:
            os.mkdir(str(runs), 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise ApiError(f"cannot create {runs}: {e.strerror or e}")
        st = os.lstat(str(runs))
    except OSError as e:
        raise ApiError(f"cannot use {runs}: {e.strerror or e}")
    if stat.S_ISLNK(st.st_mode):
        raise ApiError(f"{runs} is a symbolic link: refusing to use it")
    if not stat.S_ISDIR(st.st_mode):
        raise ApiError(f"{runs} is not a folder")
    return runs


def _run_path(runs: Path, name: str) -> Path:
    return runs / (name + ".json")


def _check_target(path: Path, name: str) -> Optional[os.stat_result]:
    """None when the file does not exist; ApiError for a symbolic link or non-regular file."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ApiError(f"cannot access run '{name}': {e.strerror or e}")
    if stat.S_ISLNK(st.st_mode):
        raise ApiError(f"run '{name}' is a symbolic link: refusing to use it")
    if not stat.S_ISREG(st.st_mode):
        raise ApiError(f"run '{name}' is not a regular file")
    return st


def _write_atomic(runs: Path, path: Path, data: bytes) -> None:
    """Write through a temporary file in the same folder, then replace the target."""
    if _DIR_FD_OK:
        _write_atomic_in_dir(runs, path.name, data)
    else:
        _write_atomic_by_path(runs, path, data)


def _write_atomic_in_dir(runs: Path, name: str, data: bytes) -> None:
    """Same as the path version, but every step is relative to the runs folder's descriptor.

    The folder is opened with O_NOFOLLOW|O_DIRECTORY, so a symbolic link swapped in for the
    folder after the checks cannot redirect the write, and the temporary file is created with
    O_EXCL|O_NOFOLLOW under a random name.
    """
    try:
        dfd = os.open(str(runs), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise ApiError(f"cannot write the run: {e.strerror or e}")
    try:
        tmp = None
        for _ in range(100):
            cand = ".tmp-" + secrets.token_hex(8) + ".tmp"
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | getattr(os, "O_BINARY", 0), 0o600, dir_fd=dfd)
            except FileExistsError:
                continue
            except OSError as e:
                raise ApiError(f"cannot write the run: {e.strerror or e}")
            tmp = cand
            break
        if tmp is None:
            raise ApiError("cannot write the run: no free temporary file name")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException as e:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            if isinstance(e, OSError):
                raise ApiError(f"cannot write the run: {e.strerror or e}")
            raise
    finally:
        os.close(dfd)


def _write_atomic_by_path(runs: Path, path: Path, data: bytes) -> None:
    """Fallback where directory file descriptors are not supported (Windows)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(runs), prefix=".tmp-", suffix=".tmp")
    except OSError as e:
        raise ApiError(f"cannot write the run: {e.strerror or e}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise ApiError(f"cannot write the run: {e.strerror or e}")
        raise


def _scan_run_files(runs: Path) -> Tuple[List[Tuple[str, int]], bool]:
    """(stem, size) of the run files, newest first by modification time, and whether there
    are more than MAX_RUN_FILES. Only the newest MAX_RUN_FILES are returned, so the cap never
    hides recent runs behind old ones.
    """
    entries: List[Tuple[int, str, int]] = []
    try:
        with os.scandir(str(runs)) as it:
            for e in it:
                if not e.name.endswith(".json"):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue            # removed since the listing
                except OSError as err:
                    raise ApiError(f"cannot list the runs: {err.strerror or err}")
                entries.append((st.st_mtime_ns, e.name[:-5], st.st_size))
    except FileNotFoundError:
        return [], False
    except OSError as e:
        raise ApiError(f"cannot list the runs: {e.strerror or e}")
    entries.sort(key=lambda item: (-item[0], item[1]))
    kept = [(stem, size) for _, stem, size in entries[:MAX_RUN_FILES]]
    return kept, len(entries) > MAX_RUN_FILES


# ----------------------------------------------------------------- reading and checking

def _no_constant(token: str) -> Any:
    raise ValueError(f"{token} is not allowed in a run file")


def _load(runs: Path, name: str) -> dict:
    """Read and check one run file. ApiError when missing, unsafe, too big or corrupted.

    Any other failure while reading or checking the file is reported as unreadable too,
    so one bad file never turns a listing or a comparison into a server error.
    """
    try:
        return _read_run(runs, name)
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(f"run '{name}' is unreadable: {type(e).__name__}")


def _read_run(runs: Path, name: str) -> dict:
    path = _run_path(runs, name)
    st = _check_target(path, name)
    if st is None:
        raise ApiError(f"no run named '{name}'")
    if st.st_size > MAX_FILE_BYTES:
        raise ApiError(f"run '{name}' is unreadable: the file is larger than 1 MiB")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as e:
        raise ApiError(f"run '{name}' cannot be opened: {e.strerror or e}")
    with os.fdopen(fd, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ApiError(f"run '{name}' is unreadable: the file is larger than 1 MiB")
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ApiError(f"run '{name}' is unreadable: not valid JSON")
    check_record(obj, name)
    return obj


def _is_num(v: Any) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:       # an int too large for a float (for example 400 digits)
        return False


def _is_num_or_null(v: Any) -> bool:
    return v is None or _is_num(v)


def check_record(obj: Any, name: str) -> None:
    """Raise ApiError('run ... is unreadable: ...') unless obj has the stored structure."""

    def bad(what: str) -> None:
        raise ApiError(f"run '{name}' is unreadable: {what}")

    def need(d: dict, key: str, ok: Callable[[Any], bool], kind: str) -> None:
        if key not in d or not ok(d[key]):
            bad(f"'{key}' must be {kind}")

    if not isinstance(obj, dict):
        bad("the top level is not a JSON object")
    if type(obj.get("schema_version")) is not int or obj["schema_version"] != SCHEMA_VERSION:
        bad("unsupported schema_version")
    if obj.get("name") != name:
        bad("the name inside the file does not match the file name")
    need(obj, "created", lambda v: isinstance(v, str), "text")
    need(obj, "created_epoch", _is_num, "a number")
    rng = obj.get("range")
    if not isinstance(rng, dict):
        bad("'range' must be an object")
    need(rng, "t0", _is_num, "a number")
    need(rng, "t1", _is_num, "a number")
    need(rng, "label", lambda v: isinstance(v, str), "text")
    stats = obj.get("statistics")
    if not isinstance(stats, dict):
        bad("'statistics' must be an object")
    need(stats, "count", lambda v: type(v) is int, "an integer")
    for key in STAT_NUMBER_KEYS:
        need(stats, key, _is_num_or_null, "a number or null")
    dist = obj.get("distribution")
    if not isinstance(dist, dict):
        bad("'distribution' must be an object")
    for key in DISTRIBUTION_KEYS:
        need(dist, key, _is_num_or_null, "a number or null")
    need(obj, "energy_j", _is_num_or_null, "a number or null")
    need(obj, "connected_seconds", _is_num_or_null, "a number or null")
    need(obj, "sample_rate_hz", _is_num_or_null, "a number or null")
    need(obj, "lost_samples", lambda v: v is None or type(v) is int, "an integer or null")
    need(obj, "device_info", lambda v: v is None or isinstance(v, dict), "an object or null")
    need(obj, "device_config", lambda v: v is None or isinstance(v, dict), "an object or null")
    need(obj, "firmware", lambda v: v is None or isinstance(v, str), "text or null")
    need(obj, "settings", lambda v: isinstance(v, dict), "an object")
    need(obj, "notes", lambda v: isinstance(v, str), "text")
    tags = obj.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        bad("'tags' must be a list of text")
    meta = obj.get("metadata")
    if not isinstance(meta, dict) or not all(
            isinstance(v, (str, bool)) or _is_num(v) for v in meta.values()):
        bad("'metadata' must be an object of text, numbers or booleans")
    need(obj, "app_version", lambda v: isinstance(v, str), "text")


# ----------------------------------------------------------------- input checks

def _validate_tags(tags: Any) -> List[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ApiError("tags must be an array of text")
    if len(tags) > MAX_TAGS:
        raise ApiError(f"at most {MAX_TAGS} tags are allowed")
    out: List[str] = []
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            raise ApiError("each tag must be non-empty text")
        if len(t) > MAX_TAG_CHARS:
            raise ApiError(f"each tag must be at most {MAX_TAG_CHARS} characters")
        if t not in out:
            out.append(t)
    return out


def _validate_metadata(meta: Any) -> Dict[str, Any]:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise ApiError("metadata must be a JSON object")
    if len(meta) > MAX_METADATA_KEYS:
        raise ApiError(f"metadata may hold at most {MAX_METADATA_KEYS} keys")
    for key, value in meta.items():
        if not isinstance(key, str) or META_KEY_RE.fullmatch(key) is None:
            raise ApiError(f"metadata key {key!r} is invalid: use 1 to 40 letters, digits, "
                           "'_', '.' or '-'")
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ApiError(f"metadata '{key}' must be a finite number")
        elif isinstance(value, str):
            if len(value) > META_VALUE_CHARS:
                raise ApiError(f"metadata '{key}' must be at most {META_VALUE_CHARS} characters")
        else:
            raise ApiError(f"metadata '{key}' must be text, a number or a boolean")
    return dict(meta)


def _plain(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars and tuples converted, NaN and infinities become null."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _plain(dataclasses.asdict(obj))
    return str(obj)


# ----------------------------------------------------------------- summaries

def _summary(rec: dict) -> dict:
    stats = rec["statistics"]
    return {
        "name": rec["name"],
        "created": rec["created"],
        "tags": rec["tags"],
        "duration_s": stats["duration_seconds"],
        "mean_power_w": stats["power_avg"],
        "energy_j": rec["energy_j"],
    }


def _metric_values(rec: dict) -> Dict[str, Optional[float]]:
    stats = rec["statistics"]
    dist = rec["distribution"]
    duration = stats["duration_seconds"]
    energy = rec["energy_j"]
    per_second = energy / duration if _is_num(energy) and _is_num(duration) and duration > 0 \
        else None
    return {
        "mean_power_w": stats["power_avg"],
        "max_power_w": stats["power_max"],
        "min_power_w": stats["power_min"],
        "p95_power_w": dist["p95"],
        "p99_power_w": dist["p99"],
        "energy_j": energy,
        "energy_per_second_j": per_second,
        "duration_s": duration,
        "mean_current_a": stats["current_avg"],
        "mean_voltage_v": stats["voltage_avg"],
    }


def compare_records(baseline: dict, candidate: dict, metrics: Sequence[str]) -> dict:
    """Per-metric comparison of two stored records, plus the summary sentence."""
    base_values = _metric_values(baseline)
    cand_values = _metric_values(candidate)
    out: Dict[str, dict] = {}
    for m in metrics:
        b, c = base_values[m], cand_values[m]
        delta = pct = None
        if b is not None and c is not None:
            # delta_percent is relative to the size of the baseline, so a negative baseline
            # keeps the sign of the change. Non-finite results (overflow) are reported as null.
            delta = _finite_or_none(float(c) - float(b))
            if delta is not None and b != 0:
                pct = _finite_or_none(delta / abs(float(b)) * 100.0)
        out[m] = {"baseline": b, "candidate": c, "delta": delta, "delta_percent": pct}
    return {"metrics": out, "summary": _summary_sentence(out, metrics)}


def _finite_or_none(x: Optional[float]) -> Optional[float]:
    return x if x is not None and math.isfinite(x) else None


def _summary_sentence(out: Dict[str, dict], metrics: Sequence[str]) -> str:
    if not metrics:
        return "no metrics were compared"
    chosen = next((m for m in (_ENERGY_PER_SECOND, "energy_j") if m in out), metrics[0])
    label = METRIC_LABELS[chosen]
    r = out[chosen]
    if r["baseline"] is None or r["candidate"] is None:
        return f"{label} is not available for both runs"
    if r["delta"] == 0:
        return f"candidate and baseline have the same {label}"
    if r["delta_percent"] is None:
        base_txt = "zero" if r["baseline"] == 0 else f"{r['baseline']:.4g}"
        return (f"candidate {label} is {r['candidate']:.4g} against a baseline of {base_txt}: "
                "no percentage change available")
    direction = "more" if r["delta"] > 0 else "less"
    verb = "uses" if chosen.startswith("energy") else "has"
    return (f"candidate {verb} {abs(r['delta_percent']):.1f}% {direction} {label} "
            "than baseline")


# ----------------------------------------------------------------- tools

def save_run(service, **kw) -> dict:
    name = check_name(kw["name"])
    overwrite = bool(kw.get("overwrite", False))
    notes = kw.get("notes") or ""
    if len(notes) > NOTES_MAX_CHARS:
        raise ApiError(f"notes must be at most {NOTES_MAX_CHARS} characters")
    tags = _validate_tags(kw.get("tags"))
    metadata = _validate_metadata(kw.get("metadata"))
    range_args = {k: kw[k] for k in RANGE_KEYS if k in kw}

    with _write_lock:
        runs = _runs_dir(service, create=True)
        path = _run_path(runs, name)
        exists = _check_target(path, name) is not None
        if exists and not overwrite:
            raise ApiError(f"run '{name}' already exists (pass overwrite=true to replace it)")
        if not exists and len(_scan_run_files(runs)[0]) >= MAX_RUN_FILES:
            raise ApiError(f"too many runs (the limit is {MAX_RUN_FILES}): delete some with "
                           "delete_run first")

    def capture() -> tuple:
        t0, t1, label, part = service._range_copy(range_args)
        return (t0, t1, label, part, service._status_now(), service._settings_now())

    t0, t1, label, part, status, settings_now = service.ctx.gui(capture)
    stats = Statistics.from_samples(part)
    if stats is None:
        raise ApiError("not enough samples in range (need at least 2)")
    stats_d = dataclasses.asdict(stats)
    stats_d["energy_j"] = stats.energy_wh * 3600.0
    stats_d["charge_c"] = stats.charge_ah * 3600.0
    p5, p50, p95, p99 = np.percentile(part.p.astype(np.float64), [5, 50, 95, 99])
    info = status.get("device_info")
    now = time.time()
    record = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "created": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "created_epoch": now,
        "range": {"t0": t0, "t1": t1, "label": label},
        "statistics": stats_d,
        "distribution": {"p5": float(p5), "p50": float(p50), "p95": float(p95),
                         "p99": float(p99)},
        "energy_j": stats_d["energy_j"],
        "connected_seconds": stats.connected_seconds,
        "sample_rate_hz": stats.sample_rate_hz,
        "lost_samples": status.get("lost_samples"),
        "device_info": info,
        "device_config": status.get("device_config"),
        "firmware": info.get("firmware") if isinstance(info, dict) else None,
        "protocol": status.get("protocol"),
        "settings": {k: settings_now["settings"][k] for k in SETTINGS_KEYS},
        "notes": notes,
        "tags": tags,
        "metadata": metadata,
        "app_version": __version__,
    }
    record = _plain(record)
    check_record(record, name)      # what is saved must read back
    data = (json.dumps(record, indent=2, allow_nan=False, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ApiError("the run is too large to store (the limit is 1 MiB): use a shorter range")

    with _write_lock:
        runs = _runs_dir(service, create=True)
        path = _run_path(runs, name)
        exists = _check_target(path, name) is not None
        if exists and not overwrite:
            raise ApiError(f"run '{name}' already exists (pass overwrite=true to replace it)")
        _write_atomic(runs, path, data)
    return dict(_summary(record), replaced=bool(exists))


def list_runs(service, limit: int = 50, tag: Optional[str] = None) -> dict:
    runs = _runs_dir(service, create=False)
    files, truncated = _scan_run_files(runs)
    records: List[dict] = []
    unreadable: List[str] = []
    scanned = 0
    for stem, size in files:
        scanned += min(size, MAX_FILE_BYTES)
        if scanned > MAX_SCAN_BYTES:
            truncated = True            # the rest is not parsed: the byte budget is spent
            break
        try:
            rec = _load(runs, check_name(stem))
        except ApiError:
            unreadable.append(stem)
            continue
        if tag is None or tag in rec["tags"]:
            records.append(rec)
    records.sort(key=lambda r: r["created_epoch"], reverse=True)
    shown = records[:limit]
    return {
        "runs": [_summary(r) for r in shown],
        "matched": len(records),
        "unreadable": sorted(unreadable),
        "truncated": truncated,
    }


def get_run(service, name: str) -> dict:
    check_name(name)
    return _load(_runs_dir(service, create=False), name)


def compare_runs(service, baseline: str, candidate: str,
                 metrics: Optional[List[str]] = None) -> dict:
    check_name(baseline)
    check_name(candidate)
    runs = _runs_dir(service, create=False)
    base = _load(runs, baseline)
    cand = _load(runs, candidate)
    wanted: List[str] = []
    for m in (metrics or METRIC_NAMES):
        if m not in wanted:
            wanted.append(m)
    result = compare_records(base, cand, wanted)
    return {"baseline": baseline, "candidate": candidate, **result}


def delete_run(service, name: str) -> dict:
    check_name(name)
    with _write_lock:
        runs = _runs_dir(service, create=False)
        path = _run_path(runs, name)
        if _check_target(path, name) is None:
            raise ApiError(f"no run named '{name}'")
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            raise ApiError(f"no run named '{name}'")
        except OSError as e:
            raise ApiError(f"cannot delete run '{name}': {e.strerror or e}")
    return {"deleted": name}


def handlers(service) -> Dict[str, Callable[..., dict]]:
    return {
        "save_run": lambda **kw: save_run(service, **kw),
        "list_runs": lambda **kw: list_runs(service, **kw),
        "get_run": lambda **kw: get_run(service, **kw),
        "compare_runs": lambda **kw: compare_runs(service, **kw),
        "delete_run": lambda **kw: delete_run(service, **kw),
    }
