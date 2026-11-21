"""The "runs" extension: named experiment results, stored as JSON, and their comparison.

The extension is in the real registry (app/api/ext/__init__.py). The service runs against a
real controller fed with synthetic samples, in a temporary recordings folder.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.api import ext, schema
from app.api.ext import runs as runs_mod
from app.api.ext import runs_schema
from app.api.service import ApiError, ApiService, DirectContext
from app.core import AppSettings, Samples
from app.ui.controller import AcquisitionController

RATE = 100.0
NAMES = [t.name for t in runs_schema.TOOLS]


# ----------------------------------------------------------------- helpers

def feed(ctrl: AcquisitionController, seconds: float, v: float = 5.0, i: float = 0.2) -> None:
    """Append `seconds` of constant readings through the controller's ingest path."""
    n = int(round(seconds * RATE))
    start = ctrl.store.last_t + 1 / RATE if len(ctrl.store) else 0.0
    t = start + np.arange(n) / RATE
    ctrl._ingest(Samples.from_columns(t, 1.79e9 + t, np.full(n, v), np.full(n, i),
                                      np.full(n, v * i)))


def synth(power: float, energy: float, duration: float, peak: float = None) -> dict:
    """The few fields compare_records reads, for pure comparison tests."""
    return {
        "statistics": {"power_avg": power, "power_max": peak if peak is not None else power,
                       "power_min": 0.0, "current_avg": 0.2, "voltage_avg": 5.0,
                       "duration_seconds": duration},
        "distribution": {"p95": power, "p99": power},
        "energy_j": energy,
    }


def run_file(rec: Path, name: str) -> Path:
    return rec / "runs" / (name + ".json")


def write_json(rec: Path, name: str, obj) -> Path:
    runs = rec / "runs"
    runs.mkdir(exist_ok=True)
    path = run_file(rec, name)
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return path


def set_epoch(rec: Path, name: str, epoch: float) -> None:
    path = run_file(rec, name)
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["created_epoch"] = epoch
    path.write_text(json.dumps(obj), encoding="utf-8")


def expect_error(svc, name, params, match):
    with pytest.raises(ApiError, match=match):
        svc.call(name, params)


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def rec(tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    folder.mkdir()
    monkeypatch.delenv(schema.EXPORT_DIRS_ENV, raising=False)
    return folder


@pytest.fixture
def ctrl(qapp, rec):
    c = AcquisitionController(AppSettings())
    c.settings.recordings_dir = str(rec)
    yield c
    c.reader = None
    c.shutdown()


@pytest.fixture
def svc(ctrl):
    """An ApiService with the runs tools from the real registry."""
    return ApiService(ctrl, ctrl.settings, DirectContext())


@pytest.fixture
def data(ctrl):
    """Ten seconds at 1 W (5 V, 0.2 A): energy 9.99 J over the 9.99 s of intervals."""
    feed(ctrl, 10.0)
    return ctrl


# ----------------------------------------------------------------- registration

def test_tools_are_declared_with_llm_descriptions():
    assert NAMES == ["save_run", "list_runs", "get_run", "compare_runs", "delete_run"]
    for t in runs_schema.TOOLS:
        assert len(t.description) > 60, t.name


def test_extension_is_in_the_real_registry():
    assert "runs" in ext.EXTENSIONS


def test_handler_map_has_exactly_the_declared_tools(svc):
    from app.api.ext import runs as runs_module
    assert set(runs_module.handlers(svc)) == set(NAMES)
    assert set(NAMES) <= set(svc._build_extension_handlers())


# ----------------------------------------------------------------- name rule

@pytest.mark.parametrize("bad", [
    "../x", "../../etc/passwd", "/abs", "a/b", "a\\b", ".", "..", ".hidden", "-x", "_x",
    "a.json", "x.JSON", "a b", "a\n", "a" * 65, "ünï", "naïve", "a\x00b", "",
])
def test_name_rule_rejects_unsafe_names(bad):
    with pytest.raises(ApiError, match="run name must be"):
        runs_mod.check_name(bad)


@pytest.mark.parametrize("good", ["a", "A1", "idle-baseline", "decode.rev_b", "x" * 64,
                                  "v2.0.1"])
def test_name_rule_accepts_safe_names(good):
    assert runs_mod.check_name(good) == good


def test_name_rule_applies_through_the_tools(svc, data):
    expect_error(svc, "save_run", {"name": "../x"}, "run name must be")
    expect_error(svc, "get_run", {"name": "a.json"}, "run name must be")
    expect_error(svc, "delete_run", {"name": "ünï"}, "run name must be")
    expect_error(svc, "compare_runs", {"baseline": "ok", "candidate": "../x"},
                 "run name must be")


# ----------------------------------------------------------------- save and read

def test_save_stores_statistics_distribution_and_context(data, svc, rec):
    summary = svc.call("save_run", {"name": "idle", "notes": "bench A",
                                    "tags": ["idle", "rev-b"],
                                    "metadata": {"load": "none", "ambient_c": 24,
                                                 "warm": True}})
    assert summary["name"] == "idle"
    assert summary["energy_j"] == pytest.approx(9.99, rel=1e-6)
    assert summary["mean_power_w"] == pytest.approx(1.0)
    assert "statistics" not in summary            # the summary is small

    full = svc.call("get_run", {"name": "idle"})
    assert full["schema_version"] == 1
    assert full["app_version"]
    assert full["created"] and isinstance(full["created_epoch"], float)
    assert full["range"]["t0"] == pytest.approx(0.0)
    assert full["range"]["t1"] == pytest.approx(9.99)
    stats = full["statistics"]
    assert stats["count"] == 1000
    assert stats["energy_j"] == pytest.approx(9.99, rel=1e-6)
    assert stats["power_avg"] == pytest.approx(1.0)
    assert set(full["distribution"]) == {"p5", "p50", "p95", "p99"}
    assert full["distribution"]["p50"] == pytest.approx(1.0)
    assert full["connected_seconds"] == pytest.approx(9.99)
    assert full["sample_rate_hz"] == pytest.approx(RATE, rel=1e-3)
    assert full["lost_samples"] == 0
    assert set(full["settings"]) == set(runs_mod.SETTINGS_KEYS)
    assert full["settings"]["shunt_ohm"] == pytest.approx(AppSettings().shunt_ohm)
    assert full["notes"] == "bench A"
    assert full["tags"] == ["idle", "rev-b"]
    assert full["metadata"] == {"load": "none", "ambient_c": 24, "warm": True}
    # Stored as strict JSON in the runs folder, with no temporary files left behind.
    text = run_file(rec, "idle").read_text(encoding="utf-8")
    json.loads(text, parse_constant=lambda c: pytest.fail(c))
    assert sorted(p.name for p in (rec / "runs").iterdir()) == ["idle.json"]


def test_save_range_arguments_select_part_of_the_data(data, svc):
    summary = svc.call("save_run", {"name": "half", "t0": 0.0, "t1": 4.99})
    assert summary["energy_j"] == pytest.approx(4.99, rel=1e-6)
    full = svc.call("get_run", {"name": "half"})
    assert full["range"]["t1"] == pytest.approx(4.99)


def test_save_needs_data(svc):
    expect_error(svc, "save_run", {"name": "empty"}, "no data")


def test_overwrite_rules(data, svc):
    svc.call("save_run", {"name": "x"})
    expect_error(svc, "save_run", {"name": "x"}, "already exists")
    expect_error(svc, "save_run", {"name": "x", "overwrite": False}, "already exists")
    summary = svc.call("save_run", {"name": "x", "overwrite": True, "notes": "second"})
    assert summary["name"] == "x"
    assert svc.call("get_run", {"name": "x"})["notes"] == "second"


def test_recordings_folder_is_created_when_missing(ctrl, svc, tmp_path, data):
    folder = tmp_path / "not" / "there" / "yet"
    ctrl.settings.recordings_dir = str(folder)
    svc.call("save_run", {"name": "fresh"})
    runs = folder / "runs"
    assert runs.is_dir()
    assert stat.S_IMODE(os.stat(runs).st_mode) & 0o077 == 0     # owner only
    assert (runs / "fresh.json").is_file()


def test_list_and_get_do_not_create_the_folder(ctrl, svc, tmp_path):
    folder = tmp_path / "missing"
    ctrl.settings.recordings_dir = str(folder)
    assert svc.call("list_runs", {})["runs"] == []
    assert not folder.exists()
    expect_error(svc, "get_run", {"name": "nothing"}, "no run named 'nothing'")


def test_notes_metadata_and_tags_are_limited(data, svc):
    expect_error(svc, "save_run", {"name": "n", "notes": "x" * 2001}, "at most 2000")
    expect_error(svc, "save_run", {"name": "t", "tags": ["x" * 41]}, "at most 40")
    expect_error(svc, "save_run", {"name": "t", "tags": [str(i) for i in range(21)]},
                 "at most 20 items")
    expect_error(svc, "save_run", {"name": "t", "tags": [""]}, "non-empty")
    expect_error(svc, "save_run", {"name": "m", "metadata": {"bad key": 1}}, "is invalid")
    expect_error(svc, "save_run", {"name": "m", "metadata": {"k": [1]}}, "text, a number")
    expect_error(svc, "save_run", {"name": "m", "metadata": {"k": "x" * 201}},
                 "at most 200")
    expect_error(svc, "save_run", {"name": "m", "metadata": {f"k{i}": i for i in range(51)}},
                 "at most 50 keys")
    expect_error(svc, "save_run", {"name": "m", "metadata": {"k" * 41: 1}}, "is invalid")
    expect_error(svc, "save_run", {"name": "m", "metadata": ["not", "a", "dict"]},
                 "JSON object")


def test_metadata_boundaries_are_accepted(data, svc):
    meta = {f"k{i}": i for i in range(47)}
    meta.update({"k" * 40: "y" * 200, "flag": False, "rate.hz-1_x": 2.5})
    summary = svc.call("save_run", {"name": "edge", "metadata": meta, "tags": ["a", "a"]})
    full = svc.call("get_run", {"name": "edge"})
    assert full["metadata"] == meta
    assert full["tags"] == ["a"]           # duplicates removed
    assert summary["tags"] == ["a"]


def test_metadata_null_and_absent_mean_empty(data, svc):
    svc.call("save_run", {"name": "a1"})
    svc.call("save_run", {"name": "a2", "metadata": None, "tags": None})
    assert svc.call("get_run", {"name": "a2"})["metadata"] == {}
    assert svc.call("get_run", {"name": "a2"})["tags"] == []


def test_save_refused_at_the_file_limit(data, svc, rec, monkeypatch):
    monkeypatch.setattr(runs_mod, "MAX_RUN_FILES", 2)
    svc.call("save_run", {"name": "one"})
    svc.call("save_run", {"name": "two"})
    expect_error(svc, "save_run", {"name": "three"}, "too many runs")
    svc.call("save_run", {"name": "two", "overwrite": True})     # replacing still works


# ----------------------------------------------------------------- symlinks and files

def test_symlinked_runs_folder_is_refused(data, svc, rec, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, rec / "runs")
    expect_error(svc, "save_run", {"name": "x"}, "symbolic link")
    expect_error(svc, "list_runs", {}, "symbolic link")
    expect_error(svc, "get_run", {"name": "x"}, "symbolic link")
    assert list(elsewhere.iterdir()) == []


def test_runs_path_that_is_a_file_is_refused(data, svc, rec):
    (rec / "runs").write_text("not a folder")
    expect_error(svc, "save_run", {"name": "x"}, "is not a folder")


def test_symlinked_run_file_is_refused_everywhere(data, svc, rec, tmp_path):
    svc.call("save_run", {"name": "real"})
    target = tmp_path / "victim.json"
    target.write_text("{}")
    os.symlink(target, run_file(rec, "link"))
    expect_error(svc, "save_run", {"name": "link"}, "symbolic link")
    expect_error(svc, "save_run", {"name": "link", "overwrite": True}, "symbolic link")
    expect_error(svc, "get_run", {"name": "link"}, "symbolic link")
    expect_error(svc, "compare_runs", {"baseline": "real", "candidate": "link"},
                 "symbolic link")
    expect_error(svc, "delete_run", {"name": "link"}, "symbolic link")
    assert target.read_text() == "{}"
    assert os.path.islink(run_file(rec, "link"))
    # The listing reports the link as unreadable and does not follow it.
    out = svc.call("list_runs", {})
    assert out["unreadable"] == ["link"]
    assert [r["name"] for r in out["runs"]] == ["real"]


def test_run_path_that_is_a_folder_is_refused(data, svc, rec):
    (rec / "runs" / "dir.json").mkdir(parents=True)
    expect_error(svc, "save_run", {"name": "dir"}, "not a regular file")
    expect_error(svc, "delete_run", {"name": "dir"}, "not a regular file")
    assert svc.call("list_runs", {})["unreadable"] == ["dir"]


def test_atomic_write_leaves_the_old_file_when_replace_fails(data, svc, rec, monkeypatch):
    svc.call("save_run", {"name": "keep", "notes": "original"})
    before = run_file(rec, "keep").read_bytes()

    def boom(src, dst, **kwargs):
        raise OSError(28, "No space left on device")

    with mock.patch.object(runs_mod.os, "replace", boom):
        expect_error(svc, "save_run", {"name": "keep", "overwrite": True, "notes": "new"},
                     "cannot write the run")
    assert run_file(rec, "keep").read_bytes() == before
    assert sorted(p.name for p in (rec / "runs").iterdir()) == ["keep.json"]


def test_atomic_write_removes_its_temporary_file_on_error(data, svc, rec, monkeypatch):
    def fail_fsync(fd):
        raise OSError(5, "I/O error")

    with mock.patch.object(runs_mod.os, "fsync", fail_fsync):
        expect_error(svc, "save_run", {"name": "gone"}, "cannot write the run")
    assert not run_file(rec, "gone").exists()
    assert list((rec / "runs").iterdir()) == []


# ----------------------------------------------------------------- corrupted files

def test_invalid_json_gives_a_clean_error(svc, rec):
    write_json(rec, "bad", "{not json")
    expect_error(svc, "get_run", {"name": "bad"}, "unreadable: not valid JSON")


def test_nan_and_deep_nesting_are_clean_errors(svc, rec):
    write_json(rec, "nan", '{"a": NaN}')
    expect_error(svc, "get_run", {"name": "nan"}, "unreadable: not valid JSON")
    write_json(rec, "deep", "[" * 100000)
    expect_error(svc, "get_run", {"name": "deep"}, "unreadable: not valid JSON")


def test_non_utf8_file_is_a_clean_error(svc, rec):
    runs = rec / "runs"
    runs.mkdir()
    (runs / "latin.json").write_bytes(b'{"name": "\xff\xfe"}')
    expect_error(svc, "get_run", {"name": "latin"}, "unreadable")


def test_oversized_file_is_refused_before_it_is_read(svc, rec):
    big = '{"pad": "' + "x" * (1024 * 1024) + '"}'
    write_json(rec, "big", big)
    expect_error(svc, "get_run", {"name": "big"}, "larger than 1 MiB")
    out = svc.call("list_runs", {})
    assert out["unreadable"] == ["big"]


def _valid_record(data, svc, rec, name="base") -> dict:
    svc.call("save_run", {"name": name})
    return json.loads(run_file(rec, name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path, value, message", [
    (["statistics", "power_avg"], "high", "'power_avg' must be a number or null"),
    (["statistics", "count"], 1.5, "'count' must be an integer"),
    (["energy_j"], [1], "'energy_j' must be a number or null"),
    (["tags"], "idle", "'tags' must be a list of text"),
    (["tags"], [1, 2], "'tags' must be a list of text"),
    (["metadata"], {"x": [1]}, "'metadata' must be an object"),
    (["range"], "0-10", "'range' must be an object"),
    (["distribution", "p95"], True, "'p95' must be a number or null"),
    (["settings"], None, "'settings' must be an object"),
    (["lost_samples"], "many", "'lost_samples' must be an integer or null"),
    (["schema_version"], 2, "unsupported schema_version"),
    (["schema_version"], True, "unsupported schema_version"),
    (["name"], "other", "does not match the file name"),
])
def test_wrong_types_are_clean_errors(data, svc, rec, path, value, message):
    obj = _valid_record(data, svc, rec, name="typed")
    node = obj
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    write_json(rec, "typed", obj)
    expect_error(svc, "get_run", {"name": "typed"}, message)


def test_top_level_array_is_a_clean_error(svc, rec):
    write_json(rec, "arr", "[1, 2, 3]")
    expect_error(svc, "get_run", {"name": "arr"}, "the top level is not a JSON object")


def test_list_skips_corrupted_files_and_reports_their_names(data, svc, rec):
    svc.call("save_run", {"name": "good1"})
    svc.call("save_run", {"name": "good2"})
    write_json(rec, "broken", "{")
    write_json(rec, "wrong", '{"schema_version": 1}')
    (rec / "runs" / "notes.txt").write_text("ignored: not a run file")
    (rec / "runs" / ".tmp-leftover.tmp").write_text("ignored: hidden temporary file")
    out = svc.call("list_runs", {})
    assert sorted(r["name"] for r in out["runs"]) == ["good1", "good2"]
    assert out["unreadable"] == ["broken", "wrong"]
    assert out["matched"] == 2
    assert out["truncated"] is False


def test_invalid_stem_in_runs_folder_is_unreadable_not_a_crash(svc, rec):
    write_json(rec, "bad.json", "{}")        # file name "bad.json.json": stem not a valid name
    out = svc.call("list_runs", {})
    assert out["unreadable"] == ["bad.json"]


# ----------------------------------------------------------------- list

def test_list_is_newest_first_with_limit_and_tag_filter(data, svc, rec):
    for name, epoch, tags in [("old", 1000.0, ["a"]), ("mid", 2000.0, ["a", "b"]),
                              ("new", 3000.0, ["b"])]:
        svc.call("save_run", {"name": name, "tags": tags})
        set_epoch(rec, name, epoch)
    out = svc.call("list_runs", {})
    assert [r["name"] for r in out["runs"]] == ["new", "mid", "old"]
    first = out["runs"][0]
    assert set(first) == {"name", "created", "tags", "duration_s", "mean_power_w", "energy_j"}
    assert first["duration_s"] == pytest.approx(9.99)
    assert first["mean_power_w"] == pytest.approx(1.0)
    assert svc.call("list_runs", {"limit": 2})["runs"][0]["name"] == "new"
    assert [r["name"] for r in svc.call("list_runs", {"limit": 2})["runs"]] == ["new", "mid"]
    assert [r["name"] for r in svc.call("list_runs", {"tag": "a"})["runs"]] == ["mid", "old"]
    assert svc.call("list_runs", {"tag": "zzz"})["runs"] == []


def test_list_limits_are_checked(svc):
    expect_error(svc, "list_runs", {"limit": 0}, "at least 1")
    expect_error(svc, "list_runs", {"limit": 501}, "at most 500")


def test_list_truncates_the_scan_at_the_file_cap(svc, rec, monkeypatch):
    monkeypatch.setattr(runs_mod, "MAX_RUN_FILES", 3)
    for k in range(5):
        write_json(rec, f"r{k}", "{}")
    out = svc.call("list_runs", {})
    assert out["truncated"] is True
    assert out["matched"] == 0
    assert len(out["unreadable"]) == 3


# ----------------------------------------------------------------- compare

def test_compare_math_for_each_metric():
    base = synth(power=10.0, energy=100.0, duration=10.0, peak=12.0)
    cand = synth(power=8.0, energy=80.0, duration=10.0, peak=9.0)
    out = runs_mod.compare_records(base, cand, list(runs_schema.METRIC_NAMES))
    m = out["metrics"]
    assert m["mean_power_w"] == {"baseline": 10.0, "candidate": 8.0, "delta": -2.0,
                                 "delta_percent": pytest.approx(-20.0)}
    assert m["max_power_w"]["delta_percent"] == pytest.approx((9.0 - 12.0) / 12.0 * 100)
    assert m["energy_j"]["delta"] == pytest.approx(-20.0)
    assert m["energy_per_second_j"]["baseline"] == pytest.approx(10.0)
    assert m["energy_per_second_j"]["candidate"] == pytest.approx(8.0)
    assert m["duration_s"]["delta_percent"] == pytest.approx(0.0)
    assert m["mean_current_a"]["delta"] == pytest.approx(0.0)
    assert list(m) == list(runs_schema.METRIC_NAMES)


def test_compare_zero_baseline_gives_null_percentage():
    base = synth(power=0.0, energy=0.0, duration=10.0)
    cand = synth(power=2.0, energy=20.0, duration=10.0)
    m = runs_mod.compare_records(base, cand, ["mean_power_w", "energy_j"])["metrics"]
    assert m["mean_power_w"] == {"baseline": 0.0, "candidate": 2.0, "delta": 2.0,
                                 "delta_percent": None}
    assert m["energy_j"]["delta_percent"] is None


def test_compare_zero_duration_has_no_energy_per_second():
    base = synth(power=1.0, energy=5.0, duration=0.0)
    cand = synth(power=1.0, energy=5.0, duration=10.0)
    m = runs_mod.compare_records(base, cand, ["energy_per_second_j"])["metrics"]
    assert m["energy_per_second_j"]["baseline"] is None
    assert m["energy_per_second_j"]["delta"] is None


def test_compare_summary_sentence():
    base = synth(power=10.0, energy=100.0, duration=10.0)
    cand = synth(power=8.77, energy=87.7, duration=10.0)
    summary = runs_mod.compare_records(base, cand, list(runs_schema.METRIC_NAMES))["summary"]
    assert summary == "candidate uses 12.3% less energy per second than baseline"
    more = runs_mod.compare_records(cand, base, ["energy_j"])["summary"]
    assert more == "candidate uses 14.0% more energy than baseline"
    same = runs_mod.compare_records(base, base, ["mean_power_w"])["summary"]
    assert same == "candidate and baseline have the same mean power"
    zero = runs_mod.compare_records(synth(0.0, 0.0, 10.0), cand,
                                    ["energy_per_second_j"])["summary"]
    assert "against a baseline of zero" in zero


def test_compare_through_the_tools(data, svc, rec):
    svc.call("save_run", {"name": "base"})
    svc.call("save_run", {"name": "cand", "t0": 0.0, "t1": 4.99})
    out = svc.call("compare_runs", {"baseline": "base", "candidate": "cand",
                                    "metrics": ["energy_j", "mean_power_w", "energy_j"]})
    assert out["baseline"] == "base" and out["candidate"] == "cand"
    assert list(out["metrics"]) == ["energy_j", "mean_power_w"]      # subset, deduplicated
    assert out["metrics"]["energy_j"]["candidate"] == pytest.approx(4.99, rel=1e-6)
    assert out["metrics"]["energy_j"]["delta_percent"] == pytest.approx(-50.05, rel=1e-2)
    assert out["summary"].startswith("candidate uses")
    default = svc.call("compare_runs", {"baseline": "base", "candidate": "base"})
    assert list(default["metrics"]) == list(runs_schema.METRIC_NAMES)
    expect_error(svc, "compare_runs", {"baseline": "base", "candidate": "base",
                                       "metrics": ["bogus"]}, "must be one of")
    expect_error(svc, "compare_runs", {"baseline": "base", "candidate": "missing"},
                 "no run named 'missing'")


# ----------------------------------------------------------------- delete

def test_delete_removes_the_file(data, svc, rec):
    svc.call("save_run", {"name": "gone"})
    assert svc.call("delete_run", {"name": "gone"}) == {"deleted": "gone"}
    assert not run_file(rec, "gone").exists()
    expect_error(svc, "delete_run", {"name": "gone"}, "no run named 'gone'")


# ----------------------------------------------------------------- metadata helpers

def test_check_record_accepts_what_save_writes(data, svc, rec):
    obj = _valid_record(data, svc, rec, name="ok")
    runs_mod.check_record(obj, "ok")       # does not raise
    obj["statistics"]["power_avg"] = None  # null is allowed for uncomputable values
    runs_mod.check_record(obj, "ok")


def test_save_of_nonfinite_values_writes_nulls(data, svc, rec, monkeypatch):
    # Force one statistic to NaN: it must be stored as null, and the file must stay valid JSON.
    real = runs_mod.Statistics.from_samples

    def with_nan(samples):
        stats = real(samples)
        stats.power_std = float("nan")
        return stats

    monkeypatch.setattr(runs_mod.Statistics, "from_samples", staticmethod(with_nan))
    svc.call("save_run", {"name": "nan-stats"})
    text = run_file(rec, "nan-stats").read_text(encoding="utf-8")
    json.loads(text, parse_constant=lambda c: pytest.fail(c))
    assert svc.call("get_run", {"name": "nan-stats"})["statistics"]["power_std"] is None


# ----------------------------------------------------------------- adversarial fixes

def test_is_num_rejects_bool_and_integers_too_large_for_a_float():
    assert runs_mod._is_num(10 ** 400) is False
    assert runs_mod._is_num(True) is False
    assert runs_mod._is_num(1.5) is True
    assert runs_mod._is_num(3) is True


def test_huge_integer_in_a_file_is_unreadable_not_a_crash(data, svc, rec):
    obj = _valid_record(data, svc, rec, name="huge")
    write_json(rec, "huge", obj)
    text = run_file(rec, "huge").read_text(encoding="utf-8")
    text = text.replace('"energy_j": ', '"energy_j": ' + "1" * 400 + ", \"_x\": ", 1)
    run_file(rec, "huge").write_text(text, encoding="utf-8")
    expect_error(svc, "get_run", {"name": "huge"}, "run 'huge' is unreadable")
    out = svc.call("list_runs", {})
    assert out["unreadable"] == ["huge"]
    assert "huge" not in [r["name"] for r in out["runs"]]


def test_huge_integer_in_a_stored_number_field_is_unreadable(data, svc, rec):
    obj = _valid_record(data, svc, rec, name="bigpower")
    obj["statistics"]["power_avg"] = 10 ** 400
    write_json(rec, "bigpower", obj)
    expect_error(svc, "get_run", {"name": "bigpower"}, "unreadable")
    assert svc.call("list_runs", {})["unreadable"] == ["bigpower"]


def test_unexpected_error_while_checking_becomes_unreadable(data, svc, rec, monkeypatch):
    svc.call("save_run", {"name": "odd"})

    def explode(obj, name):
        raise RuntimeError("boom")

    monkeypatch.setattr(runs_mod, "check_record", explode)
    expect_error(svc, "get_run", {"name": "odd"}, "run 'odd' is unreadable: RuntimeError")
    assert svc.call("list_runs", {})["unreadable"] == ["odd"]


def test_compare_percentage_uses_the_absolute_baseline_and_a_negative_baseline_direction():
    base = synth(power=-10.0, energy=-100.0, duration=10.0)
    cand = synth(power=-5.0, energy=50.0, duration=10.0)
    out = runs_mod.compare_records(base, cand, ["energy_j", "mean_power_w"])
    assert out["metrics"]["energy_j"]["delta"] == pytest.approx(150.0)
    assert out["metrics"]["energy_j"]["delta_percent"] == pytest.approx(150.0)
    assert out["metrics"]["mean_power_w"]["delta_percent"] == pytest.approx(50.0)
    assert out["summary"] == "candidate uses 150.0% more energy than baseline"
    back = runs_mod.compare_records(cand, base, ["energy_j"])
    # Baseline 50 J, candidate -100 J: delta -150 is 300 % of the (positive) baseline.
    assert back["metrics"]["energy_j"]["delta_percent"] == pytest.approx(-300.0)
    assert back["summary"] == "candidate uses 300.0% less energy than baseline"


def test_compare_negative_baseline_with_less_energy_is_less():
    base = synth(power=-10.0, energy=-100.0, duration=10.0)
    cand = synth(power=-20.0, energy=-150.0, duration=10.0)
    # Baseline -100 J, candidate -150 J: the candidate is 50 % lower (more negative).
    out = runs_mod.compare_records(base, cand, ["energy_j"])
    assert out["metrics"]["energy_j"]["delta"] == pytest.approx(-50.0)
    assert out["metrics"]["energy_j"]["delta_percent"] == pytest.approx(-50.0)
    assert out["summary"] == "candidate uses 50.0% less energy than baseline"


def test_compare_overflow_gives_null_and_never_inf():
    base = synth(power=1e-300, energy=1e-300, duration=10.0)
    cand = synth(power=1e300, energy=1e300, duration=10.0)
    out = runs_mod.compare_records(base, cand, ["energy_j"])
    row = out["metrics"]["energy_j"]
    assert row["delta_percent"] is None
    assert row["delta"] is None or row["delta"] != float("inf")
    assert "no percentage change available" in out["summary"]
    assert "inf" not in out["summary"].lower()
    both = runs_mod.compare_records(synth(-1e308, -1e308, 10.0), synth(1e308, 1e308, 10.0),
                                    ["energy_j"])
    assert both["metrics"]["energy_j"]["delta_percent"] is None
    assert "inf" not in both["summary"].lower()
    assert "no percentage change available" in both["summary"]


def test_compare_zero_baseline_sentence_says_no_percentage_change_available():
    out = runs_mod.compare_records(synth(0.0, 0.0, 10.0), synth(2.0, 20.0, 10.0),
                                   ["energy_j"])
    assert out["summary"] == ("candidate energy is 20 against a baseline of zero: "
                              "no percentage change available")


@pytest.mark.parametrize("name", ["CON", "con.report", "Nul", "aux.test", "COM1", "com9.x",
                                  "LPT1", "lpt9.a.b", "PRN"])
def test_windows_device_names_are_refused(svc, name):
    expect_error(svc, "save_run", {"name": name}, "reserved Windows device name")


@pytest.mark.parametrize("name", ["COM10", "CON-x", "nul_1", "LPT0", "console", "prn2x"])
def test_names_close_to_device_names_are_allowed(name):
    assert runs_mod.check_name(name) == name


@pytest.mark.parametrize("name", ["idle.", "a.b.", "x.."])
def test_names_ending_in_a_dot_are_refused(name):
    with pytest.raises(ApiError, match="run name must be"):
        runs_mod.check_name(name)


def test_overwrite_reports_replaced_and_replaces_the_whole_record(data, svc, rec):
    first = svc.call("save_run", {"name": "rep", "notes": "old", "tags": ["a"],
                                  "metadata": {"k": 1}})
    assert first["replaced"] is False
    second = svc.call("save_run", {"name": "rep", "overwrite": True, "notes": "new"})
    assert second["replaced"] is True
    stored = svc.call("get_run", {"name": "rep"})
    assert stored["notes"] == "new"
    assert stored["tags"] == [] and stored["metadata"] == {}


def test_list_takes_the_newest_files_when_the_cap_applies(svc, rec, monkeypatch):
    monkeypatch.setattr(runs_mod, "MAX_RUN_FILES", 2)
    runs = rec / "runs"
    runs.mkdir()
    for k, name in enumerate(["aaa-old", "bbb-mid", "zzz-new"]):
        path = write_json(rec, name, "{}")
        stamp = 1_000_000 + k * 100
        os.utime(path, (stamp, stamp))
    out = svc.call("list_runs", {})
    assert out["truncated"] is True
    # The two newest by modification time are read; the oldest ('aaa-old') is skipped.
    assert out["unreadable"] == ["bbb-mid", "zzz-new"]


def test_list_stops_at_the_byte_budget_and_reports_truncated(data, svc, rec, monkeypatch):
    for name in ("b1", "b2", "b3"):
        svc.call("save_run", {"name": name})
    sizes = [run_file(rec, n).stat().st_size for n in ("b1", "b2", "b3")]
    monkeypatch.setattr(runs_mod, "MAX_SCAN_BYTES", sum(sizes[:2]) + 1)
    out = svc.call("list_runs", {})
    assert out["truncated"] is True
    assert out["matched"] == 2
    assert sorted(r["name"] for r in out["runs"]) == ["b2", "b3"]


def test_atomic_write_without_directory_descriptors_still_works(data, svc, rec, monkeypatch):
    monkeypatch.setattr(runs_mod, "_DIR_FD_OK", False)
    svc.call("save_run", {"name": "fallback", "notes": "path based"})
    assert svc.call("get_run", {"name": "fallback"})["notes"] == "path based"
    assert sorted(p.name for p in (rec / "runs").iterdir()) == ["fallback.json"]


def test_atomic_write_in_directory_leaves_no_temporary_file(data, svc, rec):
    if not runs_mod._DIR_FD_OK:
        pytest.skip("directory file descriptors are not supported here")
    svc.call("save_run", {"name": "clean"})
    svc.call("save_run", {"name": "clean", "overwrite": True})
    assert sorted(p.name for p in (rec / "runs").iterdir()) == ["clean.json"]


def test_directory_descriptor_write_refuses_a_symlinked_folder(data, svc, rec, tmp_path):
    if not runs_mod._DIR_FD_OK:
        pytest.skip("directory file descriptors are not supported here")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    runs_dir = rec / "runs"
    runs_dir.mkdir()
    real = runs_mod._runs_dir
    calls = []

    def swap_before_the_write(service, create):
        # save_run looks the folder up twice: the second lookup (just before the write) finds
        # it replaced by a link. The O_NOFOLLOW open of the folder must refuse it.
        calls.append(1)
        if len(calls) == 1:
            return real(service, create)
        _link_to(runs_dir, elsewhere)
        return runs_dir

    with mock.patch.object(runs_mod, "_runs_dir", swap_before_the_write):
        expect_error(svc, "save_run", {"name": "x"}, "cannot write the run")
    assert len(calls) == 2
    assert list(elsewhere.iterdir()) == []


def _link_to(link: Path, target: Path) -> None:
    """Replace the (empty) folder `link` by a symbolic link to `target`."""
    if link.is_dir() and not link.is_symlink():
        link.rmdir()
    os.symlink(target, link)
