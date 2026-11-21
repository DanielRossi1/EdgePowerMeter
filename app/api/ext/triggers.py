"""Handlers of the `triggers` extension: wait_until and capture_event.

Runs on the HTTP worker thread. Every access to the controller goes through
`service.ctx.gui(...)`; the waiting loops run in the calling thread and use the service's
injected clock (`service.now()` / `service.sleep()`), so tests can drive them with a fake.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Dict, Optional, Set, Tuple

import numpy as np

from ...core.samples import Samples
from ..service import ApiError, ApiService, _statistics, _stats_dict
from .triggers_schema import OP_VALUES, QUANTITY_VALUES

POLL_S = 0.05             # poll interval while waiting for the trigger or the post window
POST_GRACE_S = 5.0        # extra time allowed for the post-trigger recording to reach t1
TIME_EPS = 1e-9           # tolerance for comparing recording times (hold, smoothing windows)
GAP_FACTOR = 2.0          # a hold run breaks at a gap longer than GAP_FACTOR x the median interval
GAP_MIN_S = 0.05          # ... and never for a gap shorter than this
RECENT_SAMPLES = 256      # samples used for the median interval of the recent data

_KEYS = {"voltage": "v", "current": "i", "power": "p"}
_OPS: Dict[str, Callable[[Any, float], Any]] = {
    ">": lambda x, th: x > th,
    ">=": lambda x, th: x >= th,
    "<": lambda x, th: x < th,
    "<=": lambda x, th: x <= th,
}
assert set(_KEYS) == set(QUANTITY_VALUES) and set(_OPS) == set(OP_VALUES)


class _CloseWatch:
    """Wakes the waits of a service when it is closed (the API server stops).

    ApiService.close() runs its close callbacks once and then forgets them, so the callback
    is registered again whenever a wait begins and none is armed. Each wait gets its own
    Event, set by the close, which the wait loops check on every poll.
    """

    def __init__(self, service: ApiService) -> None:
        self._svc = service
        self._lock = threading.Lock()
        self._armed = False
        self._stops: Set[threading.Event] = set()

    def arm(self) -> None:
        with self._lock:
            if not self._armed:
                self._svc.add_close_callback(self._on_close)
                self._armed = True

    def _on_close(self) -> None:
        with self._lock:
            self._armed = False
            for stop in self._stops:
                stop.set()

    def begin(self) -> threading.Event:
        stop = threading.Event()
        with self._lock:
            self._stops.add(stop)
        self.arm()
        return stop

    def end(self, stop: threading.Event) -> None:
        with self._lock:
            self._stops.discard(stop)


class _Watch:
    """Evaluates the condition on the samples recorded after `start()`.

    Samples are taken from the controller under `ctx.gui` in one read per poll: only the
    samples newer than the last one seen are copied. Stop and clear are checked on every
    read, so a waiting call ends with ApiError instead of hanging.
    """

    def __init__(self, service: ApiService, quantity: str, op: str, threshold: float,
                 hold_s: float, smoothing_s: float, closing: _CloseWatch) -> None:
        self.svc = service
        self._key = _KEYS[quantity]
        self._cmp = _OPS[op]
        self._threshold = float(threshold)
        self._hold = float(hold_s)
        self._smooth = float(smoothing_s)
        self._after = math.inf        # recording time of the newest sample already seen
        self._gen = 0                 # service._data_gen at start(): a change means cleared
        self._run_start: Optional[float] = None
        self._buf_t = np.empty(0)     # smoothing buffer: samples since start() within the window
        self._buf_x = np.empty(0)
        self._t0: Optional[float] = None      # first sample recorded after start()
        self._last_t: Optional[float] = None  # recording time of the last sample evaluated
        self._recent = np.empty(0)            # recent recording times (median interval)
        self._closing = closing
        self._stop = closing.begin()          # set when the service closes (server stop)
        self.armed = True             # False once triggered: later samples are not evaluated
        self.phase = "waiting for the trigger"

    def release(self) -> None:
        """Forget the close watch of this wait (call when the wait ends, on every path)."""
        self._closing.end(self._stop)

    def start(self) -> None:
        """Baseline: only samples recorded after this call can trigger."""
        self.svc.require_streaming()
        snap = self._read(math.inf)
        self._gen = snap["gen"]
        self._after = snap["latest"] if snap["latest"] is not None else -math.inf

    def _read(self, after: float) -> dict:
        svc = self.svc

        def snap() -> dict:
            c = svc.ctrl
            data = c.data()
            n = len(data)
            k = int(np.searchsorted(data.t, after, side="right")) if n else 0
            return {
                "running": c.is_running,
                "state": c.state,
                "gen": svc._data_gen,
                "latest": float(data.t[-1]) if n else None,
                "new": data.slice(k, n).copy() if n > k else Samples.empty(),
            }

        return svc.ctx.gui(snap)

    def step(self) -> Tuple[Optional[float], Optional[Tuple[float, float]]]:
        """One poll: (newest recording time or None, (t, value) when the trigger fires)."""
        if self._stop.is_set():
            raise ApiError("the API server was stopped while " + self.phase)
        snap = self._read(self._after)
        if snap["gen"] != self._gen:
            raise ApiError(f"the data was cleared while {self.phase}: the wait was abandoned")
        if not snap["running"]:
            raise ApiError(f"acquisition stopped while {self.phase} (state: {snap['state']})")
        new: Samples = snap["new"]
        hit = None
        if len(new):
            self._after = float(new.t[-1])
            if self.armed:
                hit = self._feed(new)
                if hit is not None:
                    self.armed = False
        return snap["latest"], hit

    def _gap_limit(self, t: np.ndarray) -> float:
        """Longest gap between consecutive samples that does not break a hold run.

        max(GAP_FACTOR x median sample interval of the recent data, GAP_MIN_S).
        """
        comb = np.concatenate((self._recent, t))
        self._recent = comb[-RECENT_SAMPLES:]
        intervals = np.diff(comb[-RECENT_SAMPLES:])
        if intervals.size == 0:
            return GAP_MIN_S
        median = float(np.median(intervals))
        return max(GAP_FACTOR * median, GAP_MIN_S)

    def _feed(self, new: Samples) -> Optional[Tuple[float, float]]:
        t = np.asarray(new.t, dtype=np.float64)
        x = np.asarray(getattr(new, self._key), dtype=np.float64)
        if self._t0 is None:
            self._t0 = float(t[0])
        gap = self._gap_limit(t)
        if self._smooth > 0:
            bt = np.concatenate((self._buf_t, t))
            bx = np.concatenate((self._buf_x, x))
            first = len(self._buf_t)
            cs = np.concatenate(([0.0], np.cumsum(bx)))
            idx = np.arange(first, len(bt))
            lo = np.searchsorted(bt, t - self._smooth - TIME_EPS, side="left")
            xs = (cs[idx + 1] - cs[lo]) / (idx + 1 - lo)
            keep = int(np.searchsorted(bt, t[-1] - self._smooth - TIME_EPS, side="left"))
            self._buf_t, self._buf_x = bt[keep:], bx[keep:]
            # Warm-up: a sample is evaluated only once a full smoothing window of samples
            # recorded after the call lies behind it.
            met = self._cmp(xs, self._threshold) & (t - self._t0 + TIME_EPS >= self._smooth)
        else:
            xs = x
            met = self._cmp(xs, self._threshold)
        for j in range(len(t)):
            tj = float(t[j])
            if self._last_t is not None and tj - self._last_t > gap:
                self._run_start = None        # a data gap is not a continuous run
            self._last_t = tj
            if not met[j]:
                self._run_start = None
                continue
            if self._run_start is None:
                self._run_start = tj
            if tj - self._run_start + TIME_EPS >= self._hold:
                return tj, float(xs[j])
        return None


Hit = Optional[Tuple[float, float]]


def _wait_for_trigger(watch: _Watch, timeout_s: float) -> Tuple[Hit, float]:
    svc = watch.svc
    started = svc.now()
    deadline = started + timeout_s
    while True:
        _, hit = watch.step()
        now = svc.now()
        if hit is not None:
            return hit, round(now - started, 3)
        if now >= deadline:
            return None, round(now - started, 3)
        svc.sleep(min(POLL_S, deadline - now))


def _record_until(watch: _Watch, t_end: float, budget_s: float) -> None:
    """Keep polling (the recording goes on) until the newest sample reaches t_end."""
    svc = watch.svc
    watch.phase = "recording after the trigger"
    deadline = svc.now() + budget_s
    while True:
        latest, _ = watch.step()
        if latest is not None and latest >= t_end:
            return
        now = svc.now()
        if now >= deadline:
            raise ApiError(f"the recording did not reach t={t_end:.3f} s: no new samples "
                           "arrived in time")
        svc.sleep(min(POLL_S, deadline - now))


def _make_watch(svc: ApiService, p: dict, closing: _CloseWatch) -> _Watch:
    watch = _Watch(svc, p["quantity"], p["op"], p["threshold"], p["hold_s"], p["smoothing_s"],
                   closing)
    try:
        watch.start()
    except BaseException:
        watch.release()
        raise
    return watch


def handlers(service: ApiService) -> Dict[str, Callable[..., dict]]:
    svc = service
    closing = _CloseWatch(service)
    closing.arm()                 # every wait re-arms it: close() forgets the callback

    def wait_until(quantity: str, op: str, threshold: float, hold_s: float = 0,
                   smoothing_s: float = 0, timeout_s: float = 60) -> dict:
        p = dict(quantity=quantity, op=op, threshold=threshold, hold_s=hold_s,
                 smoothing_s=smoothing_s)
        watch = _make_watch(svc, p, closing)
        try:
            hit, waited = _wait_for_trigger(watch, timeout_s)
        finally:
            watch.release()
        if hit is None:
            return {"triggered": False, "t": None, "value": None, "waited_s": waited}
        return {"triggered": True, "t": hit[0], "value": hit[1], "waited_s": waited}

    def capture_event(quantity: str, op: str, threshold: float, hold_s: float = 0,
                      smoothing_s: float = 0, pre_s: float = 1, post_s: float = 2,
                      timeout_s: float = 60, label: str = "event") -> dict:
        p = dict(quantity=quantity, op=op, threshold=threshold, hold_s=hold_s,
                 smoothing_s=smoothing_s)
        watch = _make_watch(svc, p, closing)
        try:
            return _capture(watch, pre_s, post_s, timeout_s, label)
        finally:
            watch.release()

    def _capture(watch: _Watch, pre_s: float, post_s: float, timeout_s: float,
                 label: str) -> dict:
        hit, waited = _wait_for_trigger(watch, timeout_s)
        if hit is None:
            return {"triggered": False, "t_trigger": None, "value": None, "waited_s": waited,
                    "window": None, "markers": None, "stats": None}
        t_trig, value = hit
        t_end = t_trig + post_s
        _record_until(watch, t_end, post_s + POST_GRACE_S)
        gen = watch._gen

        def cut() -> Tuple[float, Samples]:
            if svc._data_gen != gen:
                raise ApiError("the data was cleared while recording the event: no window")
            data = svc.ctrl.data()
            if not len(data):
                raise ApiError("no data in the recording")
            t0 = max(t_trig - pre_s, float(data.t[0]))
            return t0, data.between(t0, t_end).copy()

        t0, part = svc.ctx.gui(cut)
        if len(part) < 2:
            raise ApiError("the event window has fewer than 2 samples: widen pre_s or post_s")
        stats = _stats_dict(_statistics(part))
        start = svc.call("add_marker", {"label": f"{label} start", "t": t0})
        end = svc.call("add_marker", {"label": f"{label} end", "t": t_end})
        return {"triggered": True, "t_trigger": t_trig, "value": value, "waited_s": waited,
                "window": {"t0": t0, "t1": t_end},
                "markers": {"start": start["id"], "end": end["id"]},
                "stats": stats}

    return {"wait_until": wait_until, "capture_event": capture_event}
