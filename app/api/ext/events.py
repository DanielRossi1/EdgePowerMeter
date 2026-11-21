"""The `events` extension: an in-memory log of app events, read with the `get_events` long-poll.

`handlers(service)` is called once per API service, at its first extension tool call of any
kind. The EventLog is created by the first `get_events` call, not earlier, so other tools do
not connect to the controller's signals. The EventLog connects to the Qt signals on the GUI
thread and records each signal as an event. The log is bounded (RING_SIZE events);
`get_events` waits on a Condition on the HTTP worker thread, never on the GUI thread.
`service.close()` runs the close callback registered by the EventLog, which disconnects the
signals and wakes every waiter. When the service connects again, a connect callback starts a
new EventLog at once (if there was one), so events between the restart and the next
`get_events` are kept; `get_events` also starts one if that did not happen. The new log's ids
continue from the old one. While the service is closed, `get_events` returns at once with
"closed": true.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from .events_schema import RING_SIZE

log = logging.getLogger(__name__)


def _as_dict(obj: Any) -> Optional[dict]:
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return {"value": str(obj)}


class _Waiter:
    """One get_events call that waits: its scan cursor, kinds filter and own Condition."""

    __slots__ = ("cursor", "wanted", "cond")

    def __init__(self, cursor: int, wanted, cond: threading.Condition) -> None:
        self.cursor = cursor
        self.wanted = wanted
        self.cond = cond


class EventLog:
    """Bounded, thread-safe event log bound to one ApiService. Created on the HTTP thread.

    Waiters sleep on their own Condition (all on the log's lock). An event wakes only the
    waiters that can use it: the ones whose cursor is behind the new id and whose kinds
    filter matches. A waiter that is caught up is never woken, so the cost of an emit does
    not grow with the number of parked long-polls.
    """

    def __init__(self, service, first_id: int = 1) -> None:
        self._service = service
        self._ctx = service.ctx
        self._lock = threading.Lock()
        self._waiters: Set[_Waiter] = set()
        self._events: Deque[dict] = deque(maxlen=RING_SIZE)
        self._next_id = first_id
        self._closed = False
        self._connections: List[Tuple[Any, Callable]] = []
        self._lost_seen = 0
        self._rec_open = False
        self._rec_path: Optional[str] = None
        # The controller lives on the GUI thread: connect and take the snapshot there.
        self._ctx.gui(self._connect)
        service.add_close_callback(self.close)

    # ------------------------------------------------------------ recording

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_id(self) -> int:
        """Id of the newest event recorded (first_id - 1 before the first one)."""
        with self._lock:
            return self._next_id - 1

    def _record(self, kind: str, data: dict) -> None:
        with self._lock:
            if self._closed:
                return
            new_id = self._next_id
            self._events.append({"id": new_id, "time": time.time(),
                                 "kind": kind, "data": data})
            self._next_id += 1
            for w in self._waiters:
                if w.cursor < new_id and (w.wanted is None or kind in w.wanted):
                    w.cond.notify()

    # ------------------------------------------------------ GUI-thread side

    def _connect(self) -> None:
        c = self._service.ctrl
        pairs = [
            (c.state_changed, self._on_state),
            (c.message, self._on_message),
            (c.command_reply, self._on_reply),
            (c.device_info_received, self._on_device_info),
            (c.device_config_received, self._on_device_config),
            (c.data_reset, self._on_data_reset),
            # data_appended follows every batch, which is where lost samples grow.
            (c.data_appended, self._on_samples),
            (c.recording_changed, self._on_recording),
        ]
        for signal, slot in pairs:
            signal.connect(slot)
            self._connections.append((signal, slot))
        self._snapshot()

    def _snapshot(self) -> None:
        c = self._service.ctrl
        path = c.recording_path
        self._lost_seen = c.lost_samples
        self._rec_open = c.is_recording_to_disk
        self._rec_path = str(path) if path is not None else None
        self._record("snapshot", {
            "state": c.state,
            "port": c.port,
            "protocol": c.protocol,
            "samples": len(c.store),
            "lost_samples": c.lost_samples,
            "recording_path": self._rec_path,
            "recording_to_disk": self._rec_open,
            "device_info": _as_dict(c.device_info),
            "device_config": _as_dict(c.device_config),
        })

    def _on_state(self, state: str) -> None:
        self._record("state", {"state": state})

    def _on_message(self, level: str, text: str) -> None:
        self._record("message", {"level": level, "text": text})

    def _on_reply(self, ok: bool, cmd: str, msg: str) -> None:
        self._record("command_reply", {"ok": bool(ok), "cmd": cmd, "msg": msg})

    def _on_device_info(self, info) -> None:
        self._record("device_info", _as_dict(info) or {})

    def _on_device_config(self, cfg) -> None:
        data = _as_dict(cfg) or {}
        if cfg is not None:
            data["sample_rate_hz"] = cfg.sample_rate_hz
        self._record("device_config", data)

    def _on_data_reset(self) -> None:
        # clear() and load() re-base the lost count, so it may drop without a loss.
        self._lost_seen = self._service.ctrl.lost_samples
        self._record("data_reset", {})

    def _on_samples(self) -> None:
        lost = self._service.ctrl.lost_samples
        if lost == self._lost_seen:
            return
        delta = lost - self._lost_seen
        self._lost_seen = lost
        if delta > 0:
            self._record("lost_samples", {"delta": delta, "total": lost})

    def _on_recording(self) -> None:
        c = self._service.ctrl
        open_now = c.is_recording_to_disk
        path = c.recording_path
        path_str = str(path) if path is not None else None
        if self._rec_open and (not open_now or path_str != self._rec_path):
            self._record("recording_stopped", {"path": self._rec_path})
        if open_now and (not self._rec_open or path_str != self._rec_path):
            self._record("recording_started", {"path": path_str})
        self._rec_open = open_now
        self._rec_path = path_str

    def _disconnect(self) -> None:
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections = []

    # ------------------------------------------------------- any thread

    def close(self) -> None:
        """Stop recording, disconnect the signals and wake every waiter (service.close())."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for w in self._waiters:
                w.cond.notify()
        try:
            self._ctx.gui(self._disconnect)
        except Exception:
            log.debug("event log: could not disconnect on close", exc_info=True)

    def get_events(self, since_id: int = 0, wait_s: float = 0, kinds: Optional[List[str]] = None,
                   max_events: int = 100) -> dict:
        wanted = set(kinds) if kinds else None
        deadline = time.monotonic() + float(wait_s)
        found: List[dict] = []
        with self._lock:
            me = _Waiter(since_id, wanted, threading.Condition(self._lock))
            self._waiters.add(me)
            try:
                while True:
                    # Cheap check first: the ring is scanned only when an event newer than the
                    # cursor exists, and only those newer events are looked at.
                    if self._next_id - 1 > me.cursor:
                        found.extend(self._since(me.cursor, wanted))
                        me.cursor = self._next_id - 1
                    if found or self._closed:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    me.cond.wait(remaining)
            finally:
                self._waiters.discard(me)
            out = [dict(e) for e in found[:max_events]]
            oldest = self._events[0]["id"] if self._events else self._next_id
            dropped = since_id + 1 < oldest
            closed = self._closed
        last_id = out[-1]["id"] if out else since_id
        result = {"events": out, "last_id": last_id, "dropped": dropped}
        if closed:
            result["closed"] = True
        return result

    def _since(self, since_id: int, wanted) -> List[dict]:
        """Matching events with an id above since_id, oldest first. Caller holds the lock.

        Scans from the newest end and stops at the cursor, so the cost follows the number of
        newer events, not the size of the ring.
        """
        out: List[dict] = []
        for e in reversed(self._events):
            if e["id"] <= since_id:
                break
            if wanted is None or e["kind"] in wanted:
                out.append(e)
        out.reverse()
        return out


def _service_live(service) -> bool:
    """True while the service is attached to the controller (between connect() and close())."""
    return bool(getattr(service, "attached", True))


def _closed_result(since_id: int) -> dict:
    return {"events": [], "last_id": since_id, "dropped": False, "closed": True}


def handlers(service) -> Dict[str, Callable[..., dict]]:
    lock = threading.Lock()
    current: List[EventLog] = []     # the log in use: replaced after the service restarts

    def restart() -> None:
        # Connect callback: a log that was closed with the service gets its successor now,
        # so events from the restart on are recorded even before the next get_events.
        with lock:
            if current and current[0].closed and _service_live(service):
                current[0] = EventLog(service, first_id=current[0].last_id + 1)

    service.add_connect_callback(restart)

    def get_events(since_id: int = 0, **kwargs) -> dict:
        with lock:
            log_ = current[0] if current else None
            if log_ is None:
                if not _service_live(service):
                    return _closed_result(since_id)
                log_ = EventLog(service)
                current.append(log_)
            elif log_.closed and _service_live(service):
                # Stopped and started again: a new log, with ids continuing from the old one.
                log_ = EventLog(service, first_id=log_.last_id + 1)
                current[0] = log_
        return log_.get_events(since_id=since_id, **kwargs)

    return {"get_events": get_events}
