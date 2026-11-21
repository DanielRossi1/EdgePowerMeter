"""The `events` extension: the EventLog, the get_events long-poll and the service close hook.

Runs against the real AcquisitionController (its Qt signals are emitted directly). The
extension is in the real registry (app/api/ext/__init__.py).
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.api import ext, schema
from app.api.ext import events, events_schema
from app.api.service import ApiError, ApiService, DirectContext
from app.core import AppSettings
from app.serial import DeviceConfig
from app.serial.protocol import DeviceInfo
from app.ui.controller import AcquisitionController


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def ctrl(qapp):
    settings = AppSettings()
    settings.autosave = False
    c = AcquisitionController(settings)
    yield c
    c.shutdown()


@pytest.fixture
def svc(ctrl):
    s = ApiService(ctrl, ctrl.settings, DirectContext(), sleep=lambda seconds: None)
    yield s
    s.close()


@pytest.fixture
def log(svc):
    return events.EventLog(svc)


@pytest.fixture
def registered(svc):
    """get_events is in the real registry, so ApiService.call dispatches it."""
    assert "get_events" in schema.TOOLS_BY_NAME
    return svc


def kinds_of(result) -> list:
    return [e["kind"] for e in result["events"]]


def ids_of(result) -> list:
    return [e["id"] for e in result["events"]]


# ------------------------------------------------------------- snapshot, ids

def test_log_starts_with_snapshot_event(log, ctrl):
    res = log.get_events()
    assert kinds_of(res) == ["snapshot"]
    assert ids_of(res) == [1]
    assert res["last_id"] == 1
    assert res["dropped"] is False
    data = res["events"][0]["data"]
    assert data["state"] == ctrl.state
    assert data["samples"] == 0
    assert data["recording_to_disk"] is False


def test_events_are_ordered_with_increasing_ids_and_epoch_times(log, ctrl):
    before = time.time()
    ctrl.state_changed.emit("connecting")
    ctrl.message.emit("warning", "careful")
    ctrl.command_reply.emit(True, "GET", "")
    ctrl.device_info_received.emit(DeviceInfo(name="EPM", firmware="2.0.0", protocol=2))
    ctrl.device_config_received.emit(DeviceConfig())
    ctrl.data_reset.emit()
    res = log.get_events()
    assert kinds_of(res) == ["snapshot", "state", "message", "command_reply", "device_info",
                             "device_config", "data_reset"]
    assert ids_of(res) == list(range(1, 8))
    assert res["last_id"] == 7
    times = [e["time"] for e in res["events"]]
    assert all(t >= before for t in times[1:])      # the snapshot predates this test body
    assert times == sorted(times)
    by_kind = {e["kind"]: e["data"] for e in res["events"]}
    assert by_kind["state"] == {"state": "connecting"}
    assert by_kind["message"] == {"level": "warning", "text": "careful"}
    assert by_kind["command_reply"] == {"ok": True, "cmd": "GET", "msg": ""}
    assert by_kind["device_info"] == {"name": "EPM", "firmware": "2.0.0", "protocol": 2}
    assert by_kind["device_config"]["avg"] == DeviceConfig().avg
    assert by_kind["device_config"]["sample_rate_hz"] == DeviceConfig().sample_rate_hz
    assert by_kind["data_reset"] == {}


def test_since_id_and_max_events_page_through_the_log(log, ctrl):
    for n in range(5):
        ctrl.message.emit("info", f"m{n}")
    first = log.get_events(since_id=0, max_events=2)
    assert ids_of(first) == [1, 2]
    assert first["last_id"] == 2
    second = log.get_events(since_id=first["last_id"], max_events=2)
    assert ids_of(second) == [3, 4]
    rest = log.get_events(since_id=second["last_id"], max_events=500)
    assert ids_of(rest) == [5, 6]
    assert rest["last_id"] == 6
    empty = log.get_events(since_id=6)
    assert empty == {"events": [], "last_id": 6, "dropped": False}


def test_kinds_filter_returns_only_matching_events(log, ctrl):
    ctrl.message.emit("info", "a")
    ctrl.state_changed.emit("streaming")
    ctrl.message.emit("error", "b")
    res = log.get_events(kinds=["message"])
    assert kinds_of(res) == ["message", "message"]
    assert [e["data"]["text"] for e in res["events"]] == ["a", "b"]
    assert res["last_id"] == 4
    assert log.get_events(kinds=["lost_samples"])["events"] == []


# ------------------------------------------------------------- state, reconnect

def test_state_changes_are_recorded_in_order(log, ctrl):
    for state in ("connecting", "streaming", "stale", "reconnecting", "idle"):
        ctrl.state_changed.emit(state)
    res = log.get_events(kinds=["state"])
    assert [e["data"]["state"] for e in res["events"]] == [
        "connecting", "streaming", "stale", "reconnecting", "idle"]


# --------------------------------------------------------------- lost samples

def test_lost_samples_event_carries_the_delta(log, ctrl):
    ctrl.lost_samples = 3
    ctrl.data_appended.emit()
    ctrl.data_appended.emit()          # no change: no event
    ctrl.lost_samples = 5
    ctrl.data_appended.emit()
    res = log.get_events(kinds=["lost_samples"])
    assert [e["data"] for e in res["events"]] == [{"delta": 3, "total": 3},
                                                  {"delta": 2, "total": 5}]


def test_lost_count_rebased_by_data_reset_is_not_reported_as_loss(log, ctrl):
    ctrl.lost_samples = 4
    ctrl.data_appended.emit()
    ctrl.lost_samples = 0               # clear() re-bases the count
    ctrl.data_reset.emit()
    ctrl.lost_samples = 2
    ctrl.data_appended.emit()
    res = log.get_events(kinds=["lost_samples"])
    assert [e["data"] for e in res["events"]] == [{"delta": 4, "total": 4},
                                                  {"delta": 2, "total": 2}]


# ------------------------------------------------------------- recording files

def test_recording_started_and_stopped_events(monkeypatch, log, ctrl, tmp_path):
    state = {"open": False, "path": None}
    monkeypatch.setattr(AcquisitionController, "is_recording_to_disk",
                        property(lambda self: state["open"]))
    monkeypatch.setattr(AcquisitionController, "recording_path",
                        property(lambda self: state["path"]))
    path = tmp_path / "run.csv"
    state.update(open=True, path=path)
    ctrl.recording_changed.emit()
    ctrl.recording_changed.emit()       # same state: no duplicate event
    state.update(open=False)
    ctrl.recording_changed.emit()
    res = log.get_events(kinds=["recording_started", "recording_stopped"])
    assert kinds_of(res) == ["recording_started", "recording_stopped"]
    assert res["events"][0]["data"] == {"path": str(path)}
    assert res["events"][1]["data"] == {"path": str(path)}


# --------------------------------------------------------- long-poll and wake

def test_wait_timeout_returns_empty_after_about_wait_s(log):
    since = log.get_events()["last_id"]
    t0 = time.monotonic()
    res = log.get_events(since_id=since, wait_s=0.3)
    elapsed = time.monotonic() - t0
    assert res == {"events": [], "last_id": since, "dropped": False}
    assert 0.2 <= elapsed < 5


def test_zero_wait_returns_at_once(log):
    t0 = time.monotonic()
    res = log.get_events(since_id=1, wait_s=0)
    assert time.monotonic() - t0 < 1
    assert res["events"] == []


def test_long_poll_wakes_when_an_event_arrives_from_another_thread(log, ctrl):
    since = log.get_events()["last_id"]
    box = {}

    def poll():
        box["res"] = log.get_events(since_id=since, wait_s=10)

    waiter = threading.Thread(target=poll)
    waiter.start()
    time.sleep(0.2)
    assert waiter.is_alive() and "res" not in box      # still blocked, nothing to report
    ctrl.message.emit("info", "wake up")
    waiter.join(5)
    assert not waiter.is_alive()
    assert kinds_of(box["res"]) == ["message"]
    assert box["res"]["events"][0]["data"]["text"] == "wake up"


def test_long_poll_ignores_events_that_do_not_match_kinds(log, ctrl):
    since = log.get_events()["last_id"]
    box = {}

    def poll():
        box["res"] = log.get_events(since_id=since, wait_s=5, kinds=["lost_samples"])

    waiter = threading.Thread(target=poll)
    waiter.start()
    time.sleep(0.1)
    ctrl.message.emit("info", "not wanted")
    time.sleep(0.1)
    assert waiter.is_alive()
    ctrl.lost_samples = 1
    ctrl.data_appended.emit()
    waiter.join(5)
    assert kinds_of(box["res"]) == ["lost_samples"]


def test_multiple_concurrent_waiters_all_wake(log, ctrl):
    since = log.get_events()["last_id"]
    results = {}

    def poll(name):
        results[name] = log.get_events(since_id=since, wait_s=10)

    waiters = [threading.Thread(target=poll, args=(n,)) for n in range(4)]
    for w in waiters:
        w.start()
    time.sleep(0.2)
    ctrl.state_changed.emit("streaming")
    for w in waiters:
        w.join(5)
    assert all(not w.is_alive() for w in waiters)
    assert len(results) == 4
    for res in results.values():
        assert kinds_of(res) == ["state"]


def test_waiters_with_different_cursors_each_get_their_own_events(log, ctrl):
    ctrl.message.emit("info", "old")                   # id 2
    box = {}

    def poll():
        box["res"] = log.get_events(since_id=2, wait_s=10)

    waiter = threading.Thread(target=poll)
    waiter.start()
    time.sleep(0.1)
    assert waiter.is_alive()                           # id 2 is not newer than since_id 2
    ctrl.message.emit("info", "new")                   # id 3
    waiter.join(5)
    assert ids_of(box["res"]) == [3]
    # A cursor behind the newest event returns at once, without waiting.
    t0 = time.monotonic()
    assert ids_of(log.get_events(since_id=1, wait_s=10)) == [2, 3]
    assert time.monotonic() - t0 < 1


def test_close_wakes_waiters_and_returns_what_is_there(svc, log, ctrl):
    since = log.get_events()["last_id"]
    box = {}

    def poll():
        box["res"] = log.get_events(since_id=since, wait_s=60)

    waiter = threading.Thread(target=poll)
    waiter.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    svc.close()
    waiter.join(5)
    assert not waiter.is_alive()
    assert time.monotonic() - t0 < 5
    assert box["res"]["events"] == []
    assert box["res"]["last_id"] == since


def test_close_disconnects_signals_and_records_nothing_more(svc, log, ctrl):
    ctrl.message.emit("info", "before")
    last = log.get_events()["last_id"]
    svc.close()
    ctrl.message.emit("info", "after")
    ctrl.state_changed.emit("streaming")
    ctrl.lost_samples = 9
    ctrl.data_appended.emit()
    res = log.get_events(since_id=last, wait_s=0)
    assert res["events"] == []
    assert log._connections == []


def test_get_events_after_close_returns_immediately(svc, log):
    since = log.get_events()["last_id"]
    svc.close()
    t0 = time.monotonic()
    res = log.get_events(since_id=since, wait_s=60)
    assert time.monotonic() - t0 < 5
    assert res["events"] == []


def test_ring_overflow_drops_oldest_and_sets_dropped(log, ctrl):
    for n in range(events_schema.RING_SIZE + 1):     # 1001 messages + the snapshot
        ctrl.message.emit("info", str(n))
    # Snapshot is id 1, so ids 2..1002 were recorded and ids 3..1002 are kept.
    res = log.get_events(since_id=0, max_events=500)
    assert res["dropped"] is True
    assert ids_of(res)[0] == 3
    assert len(log.get_events(since_id=0, max_events=500)["events"]) == 500
    assert log.get_events(since_id=1, max_events=5)["dropped"] is True    # id 2 is gone
    assert log.get_events(since_id=2, max_events=5)["dropped"] is False   # next is id 3
    assert log.get_events(since_id=2000)["dropped"] is False


def test_no_dropped_flag_without_overflow(log, ctrl):
    ctrl.message.emit("info", "x")
    assert log.get_events(since_id=0)["dropped"] is False


# ----------------------------------------------------------- tool and schema

def test_tool_call_validates_arguments(registered):
    cases = [
        ({"since_id": -1}, "since_id must be at least 0"),
        ({"since_id": 1.5}, "since_id must be an integer"),
        ({"wait_s": -1}, "wait_s must be at least 0"),
        ({"wait_s": 301}, "wait_s must be at most 300"),
        ({"max_events": 0}, "max_events must be at least 1"),
        ({"max_events": 501}, "max_events must be at most 500"),
        ({"kinds": "message"}, "kinds must be an array"),
        ({"kinds": []}, "kinds needs at least 1 item"),
        ({"kinds": ["bogus"]}, "kinds\\[0\\] must be one of"),
        ({"since": 1}, "unknown key 'since'"),
    ]
    for params, match in cases:
        with pytest.raises(ApiError, match=match):
            registered.call("get_events", params)


def test_tool_call_end_to_end(registered, ctrl):
    first = registered.call("get_events", {})         # creates the log (snapshot)
    ctrl.message.emit("info", "hello")
    res = registered.call("get_events", {"since_id": first["last_id"], "kinds": ["message"]})
    assert kinds_of(res) == ["message"]
    assert res["last_id"] == res["events"][0]["id"]


def test_log_starts_at_the_first_get_events_call(registered, ctrl):
    ctrl.message.emit("info", "before the first call")
    res = registered.call("get_events", {})
    assert kinds_of(res) == ["snapshot"]          # the earlier message was not recorded
    ctrl.message.emit("info", "after")
    res = registered.call("get_events", {"since_id": res["last_id"]})
    assert kinds_of(res) == ["message"]


def test_handlers_return_get_events_and_bind_to_the_service(svc):
    table = events.handlers(svc)
    assert set(table) == {"get_events"}
    assert table["get_events"](since_id=0)["events"][0]["kind"] == "snapshot"


def test_schema_installs_with_param_timeout():
    tools, by_name, timeouts = [], {}, {}
    schema.install_extensions([events_schema], tools, by_name, timeouts)
    assert [t.name for t in tools] == ["get_events"]
    assert timeouts == {"get_events": "param:wait_s"}
    props = by_name["get_events"].input_schema["properties"]
    assert set(props) == {"since_id", "wait_s", "kinds", "max_events"}
    assert props["max_events"]["default"] == 100


def test_extension_is_registered_by_default():
    assert "events" in ext.EXTENSIONS


# ----------------------------------------------------------- service close hook

def test_add_close_callback_runs_once_at_close(svc):
    calls = []
    svc.add_close_callback(lambda: calls.append("a"))
    svc.close()
    svc.close()
    assert calls == ["a"]


def test_close_callbacks_run_before_signals_disconnect(ctrl):
    s = ApiService(ctrl, ctrl.settings, DirectContext(), sleep=lambda seconds: None)
    seen = {}
    s.add_close_callback(lambda: seen.update(connected=len(s._connections)))
    s.close()
    assert seen["connected"] > 0
    assert s._connections == []


def test_failing_close_callback_does_not_stop_others(ctrl):
    s = ApiService(ctrl, ctrl.settings, DirectContext(), sleep=lambda seconds: None)
    calls = []

    def boom():
        raise RuntimeError("callback failure")

    s.add_close_callback(boom)
    s.add_close_callback(lambda: calls.append("second"))
    s.close()                                    # must not raise
    assert calls == ["second"]
    assert s._connections == []


# ------------------------------------------------------- restart and closed state

def test_restart_starts_a_new_log_with_ids_continuing_from_the_old_one(svc, ctrl):
    first = svc.call("get_events", {})                  # snapshot, id 1
    ctrl.message.emit("info", "before stop")            # id 2
    last = svc.call("get_events", {"since_id": first["last_id"]})["last_id"]
    assert last == 2
    svc.close()
    closed = svc.call("get_events", {"since_id": last, "wait_s": 60})
    assert closed == {"events": [], "last_id": last, "dropped": False, "closed": True}
    svc.connect()                                       # the server starts again
    res = svc.call("get_events", {"since_id": last})    # new log: its snapshot is id 3
    assert kinds_of(res) == ["snapshot"]
    assert ids_of(res) == [3]
    assert res["dropped"] is False
    assert "closed" not in res
    ctrl.message.emit("info", "after restart")          # id 4: recorded again
    res = svc.call("get_events", {"since_id": res["last_id"], "wait_s": 5})
    assert kinds_of(res) == ["message"]
    assert ids_of(res) == [4]
    assert res["events"][0]["data"]["text"] == "after restart"


def test_a_waiter_woken_by_the_stop_gets_closed_and_then_the_restart_gets_a_log(svc, ctrl):
    svc.call("get_events", {})
    since = svc.call("get_events", {})["last_id"]
    box = {}

    def poll():
        box["res"] = svc.call("get_events", {"since_id": since, "wait_s": 60})

    waiter = threading.Thread(target=poll)
    waiter.start()
    time.sleep(0.1)
    svc.close()
    waiter.join(5)
    assert not waiter.is_alive()
    assert box["res"].get("closed") is True
    svc.connect()
    ctrl.message.emit("info", "again")
    res = svc.call("get_events", {"since_id": since})
    assert res.get("closed") is None
    assert ids_of(res)[0] == since + 1


def test_closed_service_never_creates_a_log_and_answers_at_once(svc):
    svc.close()
    t0 = time.monotonic()
    res = svc.call("get_events", {"since_id": 5, "wait_s": 60})
    assert time.monotonic() - t0 < 5
    assert res == {"events": [], "last_id": 5, "dropped": False, "closed": True}
    assert svc._connections == []                       # no signals connected meanwhile


def test_waiter_skips_non_matching_events_and_returns_the_match_in_order(log, ctrl):
    since = log.get_events()["last_id"]
    box = {}

    def poll():
        box["res"] = log.get_events(since_id=since, wait_s=10, kinds=["lost_samples"])

    waiter = threading.Thread(target=poll)
    waiter.start()
    for n in range(20):
        ctrl.message.emit("info", f"noise {n}")
    time.sleep(0.1)
    ctrl.lost_samples = 4
    ctrl.data_appended.emit()
    ctrl.message.emit("info", "more noise")
    waiter.join(5)
    assert not waiter.is_alive()
    assert kinds_of(box["res"]) == ["lost_samples"]
    assert box["res"]["last_id"] == since + 21


def test_many_waiters_do_not_slow_the_emit_by_a_full_ring_scan(log, ctrl):
    """Each waiter only looks at events newer than its cursor (the ring is not rescanned)."""
    for n in range(events_schema.RING_SIZE):
        ctrl.message.emit("info", str(n))
    since = log.get_events(since_id=0, max_events=1)["last_id"]
    threads = []
    results = {}

    def poll(k):
        results[k] = log.get_events(since_id=since, wait_s=10, kinds=["state"])

    for k in range(31):
        th = threading.Thread(target=poll, args=(k,))
        th.start()
        threads.append(th)
    time.sleep(0.2)
    ctrl.state_changed.emit("streaming")
    for th in threads:
        th.join(5)
    assert len(results) == 31
    assert all(kinds_of(r) == ["state"] for r in results.values())


# ------------------------------------------------------------ eager restart

def test_reconnect_starts_the_new_log_at_once(svc, ctrl):
    first = svc.call("get_events", {})                  # log 1: snapshot id 1
    svc.close()
    svc.connect()                                       # log 2 is created here: snapshot id 2
    ctrl.message.emit("info", "between restart and the next call")   # id 3
    res = svc.call("get_events", {"since_id": first["last_id"]})
    assert kinds_of(res) == ["snapshot", "message"]
    assert ids_of(res) == [2, 3]
    assert res["events"][1]["data"]["text"] == "between restart and the next call"


def test_connect_without_an_earlier_log_creates_none(svc, ctrl):
    svc.close()
    svc.connect()
    ctrl.message.emit("info", "not recorded")
    res = svc.call("get_events", {})                    # lazy: the first call starts the log
    assert kinds_of(res) == ["snapshot"]
    assert ids_of(res) == [1]


def test_attached_tracks_connect_and_close(svc):
    assert svc.attached is True
    svc.close()
    assert svc.attached is False
    svc.connect()
    assert svc.attached is True


def test_connect_callbacks_run_on_every_connect_and_survive_close(ctrl):
    s = ApiService(ctrl, ctrl.settings, DirectContext(), sleep=lambda seconds: None)
    calls = []

    def boom():
        raise RuntimeError("callback failure")

    s.add_connect_callback(boom)
    s.add_connect_callback(lambda: calls.append(s.attached))
    s.close()
    s.connect()
    s.close()
    s.connect()
    assert calls == [True, True]                        # run at the end of connect(), attached
    s.close()
