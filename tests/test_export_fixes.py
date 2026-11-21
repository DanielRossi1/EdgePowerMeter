"""Regression tests for the export layer: recorder file handling and CSV import/export edge cases."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from app.core import Samples
from app.core.markers import Marker, MarkerList
from app.export.csv_io import CsvFormat, detect_separator, export_csv, import_csv, import_recording
from app.export.recorder import RecordingWriter

REL_HEADER = "Timestamp,RelativeTime[s],Voltage[V],Current[A],Power[W]"


def _batch(n: int, t0: float = 0.0, rate: float = 100.0) -> Samples:
    t = t0 + np.arange(n) / rate
    v = np.full(n, 5.0)
    i = np.full(n, 0.1)
    return Samples.from_columns(t, 1.79e9 + t, v, i, v * i)


# ------------------------------------------------------------ recorder

def test_flush_reports_recording_deleted_while_open(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(100))
    w.flush()
    w.path.unlink()
    w.append(_batch(100, t0=1.0))
    w.flush()
    assert w.error and "deleted" in w.error
    assert not w.path.exists()
    w.close()
    assert not w.path.exists()           # nothing written to the orphaned file either


def test_marker_after_close_does_not_recreate_deleted_recording(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(500))
    assert w.close()
    w.path.unlink()
    w.marker_changed(Marker(1, 1.0, "late"))
    w.flush()
    assert not w.path.exists()           # no header-less "#MARKER only" file
    assert w.error


def test_same_second_recordings_do_not_overwrite(tmp_path: Path, monkeypatch):
    when = datetime(2026, 10, 8, 12, 0, 0)
    other = tmp_path / "recording_20261008_120000.csv"
    other.write_text("written by another instance\n")
    # Race window: the other instance creates its file after our existence check.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    w = RecordingWriter(tmp_path, CsvFormat(), started=when)
    assert w.path.name == "recording_20261008_120000_2.csv"
    assert other.read_text() == "written by another instance\n"
    w.discard()


# ------------------------------------------------------------ import

def test_truncated_row_with_relative_time_is_skipped_not_shifted(tmp_path: Path):
    p = tmp_path / "crash.csv"
    p.write_text(REL_HEADER + "\n"
                 "2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                 "2026-10-08 12:00:00.100,0.1,5.1,0.2,1.02\n"
                 "2026-10-08 12:00:00.200,0.2,5.2,0.3")      # cut off after 4 fields
    r = import_csv(p)
    assert len(r) == 2
    assert r.v.tolist() == pytest.approx([5.0, 5.1])
    assert r.i.tolist() == pytest.approx([0.1, 0.2])


@pytest.fixture
def rome_tz():
    if os.name == "nt" or not hasattr(time, "tzset"):
        pytest.skip("TZ switching needs POSIX tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Rome"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_dst_fall_back_hour_imports_on_the_right_wall_clock(tmp_path: Path, rome_tz):
    # 2026-10-25: 03:00 CEST goes back to 02:00 CET. Recording starts at 02:30 CEST.
    p = tmp_path / "dst.csv"
    p.write_text(REL_HEADER + "\n"
                 "2026-10-25 02:30:00.000,0.0,5.0,0.1,0.5\n"
                 "2026-10-25 02:59:00.000,1740.0,5.0,0.1,0.5\n"
                 "2026-10-25 02:00:00.000,1800.0,5.0,0.1,0.5\n"   # 02:00 CET, 30 min later
                 "2026-10-25 02:30:00.000,3600.0,5.0,0.1,0.5\n"   # 02:30 CET, 1 h later
                 "#MARKER\t1\t1800.500000\tafter fall-back\n")
    r, markers = import_recording(p)
    rel = np.array([0.0, 1740.0, 1800.0, 3600.0])
    start = datetime(2026, 10, 25, 2, 30).timestamp()    # fold=0: CEST
    assert r.t.tolist() == pytest.approx(rel.tolist())
    assert r.wall.tolist() == pytest.approx((start + rel).tolist(), abs=1e-3)
    assert markers.get(1).t == pytest.approx(1800.5)


def test_clock_sync_back_step_keeps_parsed_wall_clock(tmp_path: Path, rome_tz):
    # The device clock was synced back by 300 s at row 3: that correction must survive import.
    p = tmp_path / "sync.csv"
    p.write_text(REL_HEADER + "\n"
                 "2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                 "2026-10-08 12:00:01.000,1.0,5.0,0.1,0.5\n"
                 "2026-10-08 12:00:02.000,2.0,5.0,0.1,0.5\n"
                 "2026-10-08 11:55:03.000,3.0,5.0,0.1,0.5\n"   # sync: -300 s
                 "2026-10-08 11:55:04.000,4.0,5.0,0.1,0.5\n")
    r, _ = import_recording(p)
    parsed = [datetime(2026, 10, 8, 12, 0, 0), datetime(2026, 10, 8, 12, 0, 1),
              datetime(2026, 10, 8, 12, 0, 2), datetime(2026, 10, 8, 11, 55, 3),
              datetime(2026, 10, 8, 11, 55, 4)]
    assert r.wall.tolist() == pytest.approx([d.timestamp() for d in parsed], abs=1e-3)


def test_import_skips_comment_after_byte_order_mark(tmp_path: Path):
    p = tmp_path / "bom.csv"
    p.write_text("﻿# exported by EdgePowerMeter\n"
                 "Timestamp,Voltage[V],Current[A],Power[W]\n"
                 "2026-10-08 12:00:00.000,5.0,0.1,0.5\n"
                 "2026-10-08 12:00:00.010,5.0,0.1,0.5\n", encoding="utf-8")
    assert len(import_csv(p)) == 2


def test_detect_separator_ignores_truncated_last_line():
    lines = [REL_HEADER,
             "2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5",
             "2026-10-08 12:00:00.010,0.01,5.0"]
    assert detect_separator(lines) == ","


def test_failed_export_keeps_previous_file(tmp_path: Path):
    p = tmp_path / "out.csv"
    p.write_text("previous export\n")

    def cancelled(_fraction):
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        export_csv(p, _batch(4000), progress=cancelled)
    assert p.read_text() == "previous export\n"
    assert [x.name for x in tmp_path.iterdir()] == ["out.csv"]   # temp file removed


# ------------------------------------------------------ hardening (file handling)

import logging  # noqa: E402

from app.core.markers import MarkerList  # noqa: E402
from app.export import csv_io  # noqa: E402
from app.export.pdf_report import ReportOptions, export_pdf  # noqa: E402

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and symlinks")


def _no_tmp_left(directory: Path) -> bool:
    return not [p for p in directory.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]


# ------------------------------------------------------------ CSV export

@posix_only
def test_export_ignores_planted_symlink_at_old_temp_name(tmp_path: Path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n")
    (tmp_path / ".out.csv.tmp").symlink_to(victim)        # the old predictable name
    export_csv(tmp_path / "out.csv", _batch(50))
    assert victim.read_text() == "do not touch\n"
    assert (tmp_path / "out.csv").read_text().startswith("Timestamp,")
    # Only the symlink we planted remains: the export's own temp file was renamed away.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".out.csv.tmp", "out.csv", "victim.txt"]


def test_export_failure_keeps_original_error_when_cleanup_fails(tmp_path: Path, monkeypatch):
    def cancelled(_fraction):
        raise RuntimeError("cancelled")

    def no_unlink(self, *a, **k):
        raise OSError("unlink refused")

    monkeypatch.setattr(Path, "unlink", no_unlink)
    with pytest.raises(RuntimeError, match="cancelled"):
        export_csv(tmp_path / "out.csv", _batch(4000), progress=cancelled)


def test_marker_formula_labels_are_escaped_in_comma_export(tmp_path: Path):
    p = tmp_path / "m.csv"
    ml = MarkerList()
    ml.add(0.1, '=HYPERLINK("http://evil.example")')
    ml.add(0.2, "+1+1")
    ml.add(0.3, "-2+3")
    ml.add(0.4, "@SUM(1,1)")
    export_csv(p, _batch(50), markers=ml)
    text = p.read_text(encoding="utf-8")
    assert "\t'=HYPERLINK" in text
    assert "\t'+1+1" in text and "\t'-2+3" in text and "\t'@SUM" in text
    assert "\t=HYPERLINK" not in text
    samples, back = import_recording(p)
    assert [m.label for m in back.sorted()] == [m.label for m in ml.sorted()]


def test_tab_separated_export_escapes_formulas_and_round_trips(tmp_path: Path):
    p = tmp_path / "tsv.csv"
    ml = MarkerList()
    ml.add(0.2, "=cmd|' /C calc'!A0")
    ml.add(0.5, "plain label")
    ml.add(0.7, "'=already quoted")          # apostrophe-led labels must survive too
    export_csv(p, _batch(100), separator="\t", markers=ml)
    lines = p.read_text(encoding="utf-8").splitlines()
    marker_lines = [ln for ln in lines if ln.startswith("#MARKER")]
    assert len(marker_lines) == 3
    assert marker_lines[0].split("\t")[3] == "'=cmd|' /C calc'!A0"
    assert marker_lines[1].split("\t")[3] == "plain label"
    # Data rows stay tab separated with five cells.
    assert lines[1].count("\t") == 4
    samples, back = import_recording(p)
    assert len(samples) == 100
    assert [m.label for m in back.sorted()] == [m.label for m in ml.sorted()]


def test_format_datetimes_falls_back_to_epoch_for_unrepresentable_wall_time():
    wall = np.array([1.79e9, 1e15, 1.79e9 + 1.5])
    ts = csv_io._format_datetimes(wall)
    assert len(ts) == 3
    assert ts[0].startswith("20")                       # a normal local date-time
    assert ts[1] == "%.6f" % 1e15                       # epoch seconds for that row
    assert ts[2].startswith("20") and ts[2].endswith(".500")


def test_export_with_unrepresentable_wall_time_still_writes(tmp_path: Path):
    s = Samples.from_columns(np.arange(3, dtype=float), np.array([1.79e9, 1e15, 1.79e9 + 2]),
                             np.full(3, 5.0), np.full(3, 0.1), np.full(3, 0.5))
    p = tmp_path / "wide.csv"
    export_csv(p, s)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and lines[2].startswith("1000000000000000.000000,")


# ------------------------------------------------------------ CSV import

def test_import_rejects_files_over_size_cap(tmp_path: Path, monkeypatch):
    assert csv_io.IMPORT_MAX_BYTES == 256 * 1024 ** 2
    p = tmp_path / "big.csv"
    p.write_text(REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n" * 3)
    monkeypatch.setattr(csv_io, "IMPORT_MAX_BYTES", 40)
    with pytest.raises(ValueError, match=r"file too large to import \(max 256 MiB\)"):
        import_recording(p)


def test_import_rejects_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="not a regular file"):
        import_recording(tmp_path)


@posix_only
def test_import_rejects_fifo_without_blocking(tmp_path: Path):
    fifo = tmp_path / "pipe.csv"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        import_recording(fifo)


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_import_rejects_character_device():
    with pytest.raises(ValueError, match="not a regular file"):
        import_recording(Path("/dev/zero"))


@posix_only
def test_import_follows_symlink_to_regular_file(tmp_path: Path):
    target = tmp_path / "real.csv"
    target.write_text(REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                                   "2026-10-08 12:00:00.100,0.1,5.0,0.1,0.5\n")
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    assert len(import_csv(link)) == 2


def test_import_counts_unparseable_rows(tmp_path: Path, caplog):
    p = tmp_path / "bad.csv"
    good = "2026-10-08 12:00:00.{ms:03d},{t},5.0,0.1,0.5\n"
    lines = [REL_HEADER + "\n",
             good.format(ms=0, t="0.0"), good.format(ms=100, t="0.1"),
             good.format(ms=200, t="0.2"), good.format(ms=300, t="0.3"),
             good.format(ms=400, t="0.4"),                                   # 5th line: separator sample
             "not-a-time,0.5,5.0,0.1,0.5\n",                                  # bad timestamp
             "2026-10-08 12:00:00.600,0.6,5.0,0.1\n",                         # truncated
             "2026-10-08 12:00:00.700,0.7,nan,0.1,0.5\n",                     # non-finite
             good.format(ms=800, t="0.8")]
    p.write_text("".join(lines))
    with caplog.at_level(logging.WARNING, logger="app.export.csv_io"):
        samples, markers = import_recording(p)
    assert len(samples) == 6
    assert any("3 unreadable row" in r.getMessage() for r in caplog.records)
    _, _, skipped = csv_io.import_recording_ex(p)
    assert skipped == 3


def test_import_clean_file_reports_no_skips(tmp_path: Path, caplog):
    p = tmp_path / "clean.csv"
    p.write_text(REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                 "2026-10-08 12:00:00.100,0.1,5.0,0.1,0.5\n")
    with caplog.at_level(logging.WARNING, logger="app.export.csv_io"):
        import_recording(p)
    assert not caplog.records
    assert csv_io.import_recording_ex(p)[2] == 0


def test_import_utf16_little_endian_bom(tmp_path: Path):
    p = tmp_path / "excel.csv"
    p.write_bytes((REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                   "2026-10-08 12:00:00.100,0.1,5.0,0.1,0.5\n").encode("utf-16"))
    assert p.read_bytes()[:2] == b"\xff\xfe"
    assert len(import_csv(p)) == 2


def test_import_utf16_big_endian_bom_with_marker(tmp_path: Path):
    text = (REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
            "2026-10-08 12:00:00.100,0.1,5.0,0.1,0.5\n#MARKER\t1\t0.050000\tbe marker\n")
    p = tmp_path / "be.csv"
    p.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))
    samples, markers = import_recording(p)
    assert len(samples) == 2
    assert markers.get(1).label == "be marker"


def test_import_utf8_bom_still_works(tmp_path: Path):
    p = tmp_path / "u8bom.csv"
    p.write_bytes(b"\xef\xbb\xbf" + (REL_HEADER + "\n2026-10-08 12:00:00.000,0.0,5.0,0.1,0.5\n"
                                     "2026-10-08 12:00:00.100,0.1,5.0,0.1,0.5\n").encode("utf-8"))
    assert len(import_csv(p)) == 2


# ------------------------------------------------------------ recorder

def test_flush_with_surrogate_marker_label_writes_samples_and_marker(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(100))
    w.marker_changed(Marker(1, 0.5, "BAD\ud800LABEL"))     # bypasses clean_label
    w.flush()
    assert w.error is None
    assert w.samples_written == 100
    w.close()
    text = w.path.read_text(encoding="utf-8")
    assert "#MARKER\t1\t0.500000\tBAD�LABEL" in text


def test_flush_turns_value_error_into_recorder_error(tmp_path: Path, monkeypatch):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(10))

    def bad_rows(self, part):
        raise ValueError("cannot encode")

    monkeypatch.setattr(CsvFormat, "rows", bad_rows)
    w.flush()                                  # must not raise
    assert w.error and "cannot encode" in w.error


@posix_only
def test_marker_after_close_does_not_follow_symlink(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(500))
    assert w.close()
    victim = tmp_path / "victim.csv"
    victim.write_text("VICTIM\n")
    w.path.unlink()
    w.path.symlink_to(victim)
    w.marker_changed(Marker(1, 1.0, "injected"))
    w.flush()
    assert victim.read_text() == "VICTIM\n"
    assert w.error and "replaced" in w.error


def test_marker_after_close_refuses_replaced_file(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(500))
    assert w.close()
    # Move the original away instead of unlinking it: a freed inode can be reused at once
    # on some filesystems (CI), which would give the new file the same identity.
    w.path.rename(tmp_path / "moved.csv")
    w.path.write_text("someone else's file\n")          # regular file, different inode
    w.marker_changed(Marker(1, 1.0, "late"))
    w.flush()
    assert w.path.read_text() == "someone else's file\n"
    assert w.error


def test_marker_after_close_appends_to_same_file(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_batch(500))
    assert w.close()
    w.marker_changed(Marker(1, 1.0, "late"))
    w.flush()
    assert w.error is None
    assert w.path.read_text(encoding="utf-8").endswith("#MARKER\t1\t1.000000\tlate\n")


# ------------------------------------------------------------ PDF report

def _spy_paragraphs(monkeypatch):
    import reportlab.platypus as rp
    seen = []
    original = rp.Paragraph

    def spy(text, *args, **kwargs):
        seen.append(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(rp, "Paragraph", spy)
    return seen


def test_pdf_failed_build_leaves_no_file_and_keeps_previous_report(tmp_path: Path, monkeypatch):
    from reportlab.platypus import SimpleDocTemplate
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4 previous good report\n")

    def boom(self, *args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SimpleDocTemplate, "build", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        export_pdf(target, _batch(400), ReportOptions())
    assert target.read_bytes() == b"%PDF-1.4 previous good report\n"
    assert _no_tmp_left(tmp_path)


def test_pdf_failed_first_export_leaves_no_zero_byte_file(tmp_path: Path, monkeypatch):
    from reportlab.platypus import SimpleDocTemplate
    target = tmp_path / "new.pdf"
    def boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(SimpleDocTemplate, "build", boom)
    with pytest.raises(RuntimeError):
        export_pdf(target, _batch(400), ReportOptions())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_pdf_success_is_complete_and_leaves_no_temp(tmp_path: Path):
    target = tmp_path / "ok.pdf"
    export_pdf(target, _batch(400), ReportOptions(title="Bench run", notes="line one\nline two"))
    assert target.read_bytes().startswith(b"%PDF-")
    assert target.stat().st_size > 1000
    assert _no_tmp_left(tmp_path)


def test_pdf_title_and_notes_are_capped_regardless_of_caller(tmp_path: Path, monkeypatch):
    seen = _spy_paragraphs(monkeypatch)
    export_pdf(tmp_path / "caps.pdf", _batch(400),
               ReportOptions(title="W" * 300_000, notes="N" * 60_000))
    assert "W" * 200 in seen and "W" * 201 not in seen
    assert "N" * 5000 in seen and "N" * 5001 not in seen


def test_pdf_title_with_lone_surrogate_is_valid_text(tmp_path: Path, monkeypatch):
    seen = _spy_paragraphs(monkeypatch)
    export_pdf(tmp_path / "surr.pdf", _batch(400),
               ReportOptions(title="SURR\ud800OGATE", notes="NOTE\udc80"))
    texts = [t for t in seen if isinstance(t, str)]
    assert any("SURR�OGATE" in t for t in texts)
    assert all("\ud800" not in t and "\udc80" not in t for t in texts)
    assert (tmp_path / "surr.pdf").read_bytes().startswith(b"%PDF-")
