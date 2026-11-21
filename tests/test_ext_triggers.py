"""The `triggers` extension: wait_until and capture_event.

A fake controller holds a real SampleStore. The fake clock's sleep appends samples at
100 Hz (recording time k / RATE for sample k), so every test is deterministic: the
"stream" advances exactly when the waiting loop sleeps.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from app.api import schema
from app.api.ext import triggers_schema
from app.api.mcp_bridge import tool_timeout_s
from app.api.service import ApiError, ApiService, DirectContext
from app.core.markers import MarkerList
from app.core.samples import Samples, SampleStore
from app.ui.controller import STATE_IDLE, STATE_STREAMING

RATE = 100.0
CURRENT = 0.2


class FakeClock:
    """Injected sleep/monotonic. Sleeping advances the time and runs the stream."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeCtrl:
    """The controller attributes the trigger handlers and the marker tool use."""

    def __init__(self) -> None:
        self.state = STATE_STREAMING
        self.store = SampleStore()
        self.markers = MarkerList()

    @property
    def is_running(self) -> bool:
        return self.state != STATE_IDLE

    def data(self) -> Samples:
        return self.store.view()

    def add_marker(self, t=None, label: str = ""):
        if t is None:
            if not len(self.store):
                return None
            t = self.store.last_t
        return self.markers.add(t, label)


class Plant:
    """A fake acquisition: controller, service on the fake clock, and a scripted signal."""

    def __init__(self) -> None:
        self.ctrl = FakeCtrl()
        self.clock = FakeClock()
        self.clock.on_sleep = self._tick
        self.svc = ApiService(self.ctrl, SimpleNamespace(), DirectContext(),
                              sleep=self.clock.sleep, monotonic=self.clock.monotonic)
        self.volts = lambda t: np.full(t.shape, 5.0)   # voltage of the streamed samples
        self._k = 0                                    # index of the next sample
        self._pending = []                             # (time, fn) run once the clock passes

    def _append(self, n: int, volts) -> None:
        if n <= 0:
            return
        t = (self._k + np.arange(n)) / RATE
        v = np.asarray(volts(t), dtype=np.float64) * np.ones(n)
        i = np.full(n, CURRENT)
        self.ctrl.store.append(Samples.from_columns(t, 1.79e9 + t, v, i, v * i))
        self._k += n

    def _tick(self, seconds: float) -> None:
        if self.ctrl.state == STATE_STREAMING:
            self._append(int(round(seconds * RATE)), self.volts)
        for when, fn in list(self._pending):
            if self.clock.now >= when:
                self._pending.remove((when, fn))
                fn()

    # ---- scripting helpers

    def prefill(self, seconds: float, volts: float) -> None:
        """Samples recorded before the call (never allowed to trigger it)."""
        self._append(int(round(seconds * RATE)), lambda t: np.full(t.shape, volts))

    def at(self, clock_s: float, fn) -> None:
        self._pending.append((clock_s, fn))

    def skip(self, seconds: float) -> None:
        """A gap in the recording: the stream moves on without samples for `seconds`."""
        self._k += int(round(seconds * RATE))

    def stop(self) -> None:
        self.ctrl.state = STATE_IDLE

    def clear(self) -> None:
        self.ctrl.store.clear()
        self.ctrl.markers.clear()
        self._k = 0
        self.svc._on_data_reset()          # what the controller's data_reset signal does


def high_during(intervals, base: float, high: float):
    """Voltage function: `high` inside the half-open recording-time intervals, else `base`."""
    def f(t):
        v = np.full(t.shape, base, dtype=np.float64)
        for a, b in intervals:
            v[(t >= a - 1e-9) & (t < b - 1e-9)] = high
        return v
    return f


def step_at(when: float, before: float, after: float):
    return lambda t: np.where(t >= when - 1e-9, after, before)


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def plant():
    return Plant()


def wait(plant, **params):
    base = {"quantity": "voltage", "op": ">", "threshold": 10.0}
    base.update(params)
    return plant.svc.call("wait_until", base)


def capture(plant, **params):
    base = {"quantity": "voltage", "op": ">", "threshold": 10.0}
    base.update(params)
    return plant.svc.call("capture_event", base)


# ------------------------------------------------------------------- schema

def test_schema_installs_into_a_registry_and_names_are_unique():
    tools, by_name, timeouts = [], {}, {}
    schema.install_extensions([triggers_schema], tools, by_name, timeouts)
    assert [t.name for t in tools] == ["wait_until", "capture_event"]
    assert timeouts == {"wait_until": "param:timeout_s", "capture_event": "param:timeout_s"}


def test_schema_rejects_a_clash_with_a_builtin_tool():
    clash = types.ModuleType("clash_schema")
    clash.TOOLS = [schema.tool("get_live", "clash", {})]
    with pytest.raises(ValueError, match="duplicate tool name 'get_live'"):
        schema.install_extensions([clash], list(schema.TOOLS), dict(schema.TOOLS_BY_NAME), {})


def test_bridge_timeouts_are_timeout_plus_grace():
    assert tool_timeout_s("wait_until", {"timeout_s": 30}) == 90.0
    assert tool_timeout_s("capture_event", {"timeout_s": 100, "post_s": 50}) == 160.0
    assert tool_timeout_s("wait_until", {}) == 120.0     # default timeout_s 60 + 60


def test_defaults_are_applied_by_the_schema(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = lambda t: np.full(t.shape, 12.0)
    out = capture(plant)
    assert out["triggered"] is True
    assert out["window"]["t1"] == pytest.approx(out["t_trigger"] + 2.0, abs=1e-9)
    assert out["window"]["t0"] == pytest.approx(out["t_trigger"] - 1.0, abs=1e-9)
    labels = sorted(m.label for m in plant.ctrl.markers)
    assert labels == ["event end", "event start"]


@pytest.mark.parametrize("params, match", [
    ({"threshold": 10.0}, "quantity"),                                  # missing quantity
    ({"quantity": "energy", "op": ">", "threshold": 1}, "must be one of"),
    ({"quantity": "voltage", "op": "==", "threshold": 1}, "must be one of"),
    ({"quantity": "voltage", "op": ">", "threshold": "5"}, "must be a number"),
    ({"quantity": "voltage", "op": ">", "threshold": 1, "hold_s": 61}, "at most 60"),
    ({"quantity": "voltage", "op": ">", "threshold": 1, "smoothing_s": 10.5}, "at most 10"),
    ({"quantity": "voltage", "op": ">", "threshold": 1, "timeout_s": 0.01}, "at least 0.1"),
    ({"quantity": "voltage", "op": ">", "threshold": 1, "timeout_s": 86401}, "at most"),
    ({"quantity": "voltage", "op": ">", "threshold": 1, "extra": 1}, "extra"),
])
def test_wait_until_validation_errors(plant, params, match):
    with pytest.raises(ApiError, match=match):
        plant.svc.call("wait_until", params)


@pytest.mark.parametrize("params, match", [
    ({"post_s": 51}, "at most 50"),
    ({"pre_s": -1}, "at least 0"),
    ({"pre_s": 61}, "at most 60"),
    ({"label": ""}, "must not be empty"),
    ({"label": "x" * 41}, "at most 40"),
])
def test_capture_event_validation_errors(plant, params, match):
    base = {"quantity": "power", "op": "<", "threshold": 1.0}
    base.update(params)
    with pytest.raises(ApiError, match=match):
        plant.svc.call("capture_event", base)


# ------------------------------------------------------------ wait_until

def test_requires_streaming(plant):
    plant.stop()
    with pytest.raises(ApiError, match="not streaming"):
        wait(plant, timeout_s=1)


def test_data_recorded_before_the_call_cannot_trigger(plant):
    plant.prefill(1.0, 12.0)           # above the threshold, but before the call
    plant.volts = lambda t: np.full(t.shape, 5.0)
    out = wait(plant, timeout_s=0.5)
    assert out == {"triggered": False, "t": None, "value": None, "waited_s": 0.5}


def test_old_data_does_not_trigger_even_when_smoothed(plant):
    plant.prefill(1.0, 12.0)
    plant.volts = lambda t: np.full(t.shape, 5.0)
    out = wait(plant, timeout_s=0.5, smoothing_s=0.5)
    assert out["triggered"] is False


def test_triggers_on_the_first_new_sample(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = lambda t: np.full(t.shape, 12.0)
    out = wait(plant, timeout_s=5)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(1.0, abs=1e-9)
    assert out["value"] == pytest.approx(12.0)
    assert out["waited_s"] == pytest.approx(0.05, abs=1e-9)   # arrives in the first tick


def test_triggers_when_new_data_crosses_the_threshold(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = high_during([(2.0, 9.0)], base=5.0, high=12.0)
    out = wait(plant, timeout_s=5, op=">=", threshold=12.0)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(2.0, abs=1e-9)
    assert out["waited_s"] == pytest.approx(1.0, abs=0.06)


def test_hold_requires_a_continuous_run(plant):
    plant.prefill(1.0, 5.0)
    # 0.2 s above, a 0.1 s break, then above from t=1.3 on. Hold 0.5 s: the run that
    # starts at 1.30 must last 0.5 s, so the trigger is at 1.80, not in the first run.
    plant.volts = high_during([(1.0, 1.2), (1.3, 9.0)], base=5.0, high=12.0)
    out = wait(plant, timeout_s=5, hold_s=0.5)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(1.8, abs=1e-6)


def test_hold_is_reset_by_a_sample_that_fails(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = high_during([(1.0, 1.4), (1.45, 1.7)], base=5.0, high=12.0)
    out = wait(plant, timeout_s=2, hold_s=0.5)
    assert out["triggered"] is False      # runs of 0.4 s and 0.25 s: both under 0.5 s


def test_smoothing_delays_the_trigger_for_a_spike(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = high_during([(2.0, 2.01)], base=5.0, high=50.0)   # a single sample
    out = wait(plant, timeout_s=2, op=">", threshold=20.0, smoothing_s=0.5)
    assert out["triggered"] is False


def test_without_smoothing_the_same_spike_triggers(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = high_during([(2.0, 2.01)], base=5.0, high=50.0)
    out = wait(plant, timeout_s=2, op=">", threshold=20.0)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(2.0, abs=1e-9)


def test_smoothing_averages_the_new_samples(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = high_during([(3.0, 4.0)], base=5.0, high=30.0)
    out = wait(plant, timeout_s=5, op=">", threshold=20.0, smoothing_s=0.5)
    # The 0.5 s trailing mean reaches 20 V after about 0.3 s of 30 V (raw: at 3.00).
    assert out["triggered"] is True
    assert 3.25 <= out["t"] <= 3.35
    assert out["value"] > 20.0


def test_timeout_is_not_an_error(plant):
    out = wait(plant, timeout_s=0.5)
    assert out["triggered"] is False
    assert out["t"] is None and out["value"] is None
    assert out["waited_s"] == pytest.approx(0.5, abs=0.01)


def test_stop_during_the_wait_is_an_error(plant):
    plant.at(0.3, plant.stop)
    with pytest.raises(ApiError, match="acquisition stopped while waiting for the trigger"):
        wait(plant, timeout_s=5)


def test_clear_during_the_wait_is_an_error(plant):
    plant.at(0.3, plant.clear)
    with pytest.raises(ApiError, match="data was cleared while waiting"):
        wait(plant, timeout_s=5)


# ------------------------------------------------------------ capture_event

def test_capture_returns_window_markers_and_stats(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = step_at(2.0, 5.0, 12.0)
    out = capture(plant, timeout_s=5, pre_s=0.5, post_s=1.0, label="burst")
    assert out["triggered"] is True
    assert out["t_trigger"] == pytest.approx(2.0, abs=1e-9)
    assert out["window"] == {"t0": pytest.approx(1.5, abs=1e-9),
                             "t1": pytest.approx(3.0, abs=1e-9)}
    starts = {m.id: m for m in plant.ctrl.markers}
    start, end = starts[out["markers"]["start"]], starts[out["markers"]["end"]]
    assert (start.label, start.t) == ("burst start", pytest.approx(1.5, abs=1e-9))
    assert (end.label, end.t) == ("burst end", pytest.approx(3.0, abs=1e-9))
    # Samples 1.50 .. 3.00 inclusive: 151 of them, 5 V up to 2.00 and 12 V after.
    assert out["stats"]["count"] == 151
    assert out["stats"]["voltage_min"] == pytest.approx(5.0)
    assert out["stats"]["voltage_max"] == pytest.approx(12.0)


def test_capture_window_is_clipped_to_the_start_of_the_data(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = step_at(2.0, 5.0, 12.0)
    out = capture(plant, timeout_s=5, pre_s=10, post_s=0.5)
    assert out["window"]["t0"] == pytest.approx(0.0, abs=1e-9)
    assert out["stats"]["count"] == 251          # 0.00 .. 2.50 (trigger 2.00 + post 0.5)


def test_capture_uses_pre_trigger_data_recorded_before_the_call(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = lambda t: np.full(t.shape, 12.0)
    out = capture(plant, timeout_s=5, pre_s=0.5, post_s=0.2)
    assert out["t_trigger"] == pytest.approx(1.0, abs=1e-9)
    assert out["window"]["t0"] == pytest.approx(0.5, abs=1e-9)
    assert out["stats"]["voltage_min"] == pytest.approx(5.0)   # the pre-call part


def test_capture_not_triggered_records_nothing(plant):
    out = capture(plant, timeout_s=0.3)
    assert out == {"triggered": False, "t_trigger": None, "value": None,
                   "waited_s": pytest.approx(0.3, abs=0.01), "window": None,
                   "markers": None, "stats": None}
    assert len(plant.ctrl.markers) == 0


def test_capture_stop_during_the_post_window_is_an_error(plant):
    plant.volts = step_at(2.0, 5.0, 12.0)
    plant.prefill(1.0, 5.0)
    plant.at(1.5, plant.stop)                    # trigger at ~1.0 s on the clock, stop at 1.5
    with pytest.raises(ApiError, match="while recording after the trigger"):
        capture(plant, timeout_s=5, post_s=2)
    assert len(plant.ctrl.markers) == 0


def test_capture_clear_during_the_wait_is_an_error(plant):
    plant.at(0.3, plant.clear)
    with pytest.raises(ApiError, match="data was cleared while waiting"):
        capture(plant, timeout_s=5)


def test_capture_needs_two_samples_in_the_window(plant):
    plant.prefill(1.0, 5.0)
    plant.volts = step_at(2.0, 5.0, 12.0)
    with pytest.raises(ApiError, match="fewer than 2 samples"):
        capture(plant, timeout_s=5, pre_s=0, post_s=0)


def test_handlers_include_the_triggers_tools(plant):
    names = set(plant.svc._extension_handlers())
    assert {"wait_until", "capture_event"} <= names
    assert names <= set(schema.TOOLS_BY_NAME)


# ------------------------------------------------------ server stop (close)

def test_server_stop_ends_wait_until_with_an_error(plant):
    plant.at(0.5, plant.svc.close)                       # the server stops during the wait
    t0 = plant.clock.now
    with pytest.raises(ApiError, match="the API server was stopped while waiting"):
        wait(plant, timeout_s=60)
    assert plant.clock.now - t0 < 1.0                    # ended at once, not at the timeout


def test_server_stop_ends_capture_event_during_the_post_window(plant):
    plant.at(0.2, plant.svc.close)                       # triggers at once, stops while recording
    with pytest.raises(ApiError, match="the API server was stopped while recording"):
        capture(plant, threshold=1.0, post_s=5)


def test_waits_work_again_after_a_restart_and_a_later_stop_wakes_them(plant):
    plant.at(0.3, plant.svc.close)
    with pytest.raises(ApiError, match="the API server was stopped"):
        wait(plant, timeout_s=60)
    plant.svc.connect()                                  # the server starts again
    plant.volts = lambda t: np.full(t.shape, 12.0)
    out = wait(plant, timeout_s=5)                       # triggers on the first new sample
    assert out["triggered"] is True
    plant.volts = lambda t: np.full(t.shape, 5.0)
    plant.at(plant.clock.now + 0.3, plant.svc.close)
    with pytest.raises(ApiError, match="the API server was stopped"):
        wait(plant, timeout_s=60)


def test_closing_twice_and_without_a_wait_is_harmless(plant):
    plant.svc.close()
    plant.svc.close()
    plant.svc.connect()
    plant.volts = lambda t: np.full(t.shape, 12.0)
    assert wait(plant, timeout_s=5)["triggered"] is True


# ------------------------------------------------------------ hold across data gaps

def test_a_five_second_data_gap_breaks_the_hold_run(plant):
    # High from 1.0 to 1.6 (0.6 s), then 5 s with no samples, then high again from 6.6.
    # hold 1 s: without the gap rule the run that began at 1.0 would trigger at 6.6.
    plant.volts = high_during([(1.0, 1.6), (6.6, 9.0)], base=5.0, high=12.0)
    plant.at(1.6, lambda: plant.skip(5.0))
    out = wait(plant, timeout_s=20, hold_s=1.0)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(7.6, abs=0.02)


def test_a_short_gap_does_not_break_the_hold_run(plant):
    # Samples are 10 ms apart: a 40 ms interval is under the 50 ms floor, so the run survives.
    plant.volts = high_during([(1.0, 9.0)], base=5.0, high=12.0)   # high all the time
    plant.at(1.3, lambda: plant.skip(0.03))
    out = wait(plant, timeout_s=20, hold_s=0.5)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(1.5, abs=0.02)      # the run from 1.0 s is not broken


def test_gap_rule_uses_twice_the_median_interval_for_faster_data(plant):
    # 10 ms samples: a 0.1 s gap (10 intervals) is over the 0.05 s floor and breaks the run.
    plant.volts = high_during([(1.0, 9.0)], base=5.0, high=12.0)   # high all the time
    plant.at(1.3, lambda: plant.skip(0.1))
    out = wait(plant, timeout_s=20, hold_s=0.5)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(1.89, abs=0.02)     # the run restarts after the gap


# -------------------------------------------------------------- smoothing warm-up

def test_smoothing_warm_up_ignores_the_first_window_after_the_call(plant):
    # A spike at the very start (first 0.1 s) would pass a mean of only a few samples. With the
    # warm-up no sample is evaluated before a full 0.5 s window exists, so it cannot trigger.
    plant.volts = high_during([(0.0, 0.1)], base=5.0, high=50.0)
    out = wait(plant, timeout_s=1.5, op=">", threshold=20.0, smoothing_s=0.5)
    assert out["triggered"] is False


def test_smoothing_is_evaluated_from_the_first_full_window(plant):
    plant.volts = lambda t: np.full(t.shape, 12.0)
    out = wait(plant, timeout_s=5, op=">", threshold=10.0, smoothing_s=0.5)
    assert out["triggered"] is True
    assert out["t"] == pytest.approx(0.5, abs=0.011)     # the first full window, not 0.0
