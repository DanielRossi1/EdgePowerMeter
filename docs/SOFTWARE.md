# EdgePowerMeter Software Documentation

Desktop application (Python, PySide6/Qt 6, pyqtgraph) for the EdgePowerMeter
hardware. This document describes the architecture and the main modules.
The serial protocol is specified in [PROTOCOL.md](PROTOCOL.md), the firmware
and hardware in [HARDWARE.md](HARDWARE.md), packaging in [BUILD.md](BUILD.md).

## Table of Contents

- [Architecture](#architecture)
- [Data path](#data-path)
- [Modules](#modules)
- [User interface](#user-interface)
- [Translations](#translations)
- [Settings](#settings)
- [Export formats](#export-formats)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
            USB-CDC                    reader thread                      GUI thread
┌──────────┐  bytes  ┌───────────────────────────────────┐  Samples   ┌──────────────────────────┐
│ ESP32-C3 │ ──────► │ SerialPortHandler (select/os.read)│  batches   │ AcquisitionController     │
│ firmware │ ◄────── │ StreamDecoder  (RAW / legacy CSV) │ ─────────► │  SampleStore (numpy)      │
└──────────┘ commands│ SerialReader   (handshake, watchdog,│  ≤60/s    │  RunningStats             │
                     │                 commands, batching)│           │  reconnect logic          │
                     └───────────────────────────────────┘           └────────────┬─────────────┘
                                                                                  │ signals
                                          ┌───────────────┬───────────────┬───────┴──────┬──────────────┐
                                          │ LivePage      │ AnalysisPage  │ DevicePage   │ SettingsPage │
                                          │ plots, tiles  │ selection,    │ INA226 cfg,  │ all options  │
                                          │               │ PSU, spectrum │ clock, calib │              │
                                          └───────────────┴───────────────┴──────────────┴──────────────┘
```

Design rules:

- **No per-sample work on the GUI thread.** The reader decodes in bulk and
  emits one `Samples` batch per frame; plots redraw at a fixed rate (default
  30 FPS) and only the visible slice is handed to pyqtgraph, which
  peak-downsamples it to the pixel width.
- **Columnar storage.** Samples live in growable numpy columns
  (`SampleStore`, 28 bytes/sample), so hours of 1 kHz data stay in the
  hundreds of MB and every statistic is vectorized.
- **Device time.** With firmware 2.x the time axis comes from the device's µs
  clock, not from when data reached the PC, so USB and GUI latency do not
  distort sample spacing, energy integration or the spectrum.
- **The controller has no widgets.** Connection, reconnection and
  data-retention rules live in `AcquisitionController` and are tested headless.

---

## Data path

1. `SerialPortHandler` opens the port (raw termios file descriptor on Linux,
   pyserial elsewhere) and returns whatever bytes are available.
2. `SerialReader` sends `HELLO` until firmware 2.x answers, then `GET`,
   `MODE RAW` and (optionally) `SYNC` at the next second boundary. If only
   legacy CSV lines arrive for 2.5 s it falls back to firmware-1.x mode.
3. `StreamDecoder` splits lines, converts raw INA226 registers with the
   host-side `Calibration` (shunt, gain, offset), maps device time to wall
   clock using the `T` anchors, counts lost samples (sequence gaps) and missed
   conversions (time gaps), and optionally block-averages N samples.
4. `AcquisitionController` appends batches to the `SampleStore`, updates
   `RunningStats` (energy/charge integration skips disconnection gaps), and
   handles state changes.

Connection rules (see `app/ui/controller.py`):

| Event | Behaviour |
|-------|-----------|
| Start | New recording; the UI asks before discarding unsaved data. |
| Stop | Final. Nothing restarts the acquisition automatically. |
| USB unplugged / port hung up | Data kept. With auto-reconnect, the port is polled every second; on return the recording continues and the time axis is bridged with the real outage duration. |
| No data for the configured timeout | Acquisition stops with an explanatory error. |

---

## Modules

| Module | Purpose |
|--------|---------|
| `app/core/samples.py` | `Samples` (immutable columnar view) and `SampleStore` (append-only growable store). |
| `app/core/statistics.py` | `Statistics.from_samples()` (vectorized), `RunningStats` (incremental), trapezoidal `integrate_energy()` with gap skipping. |
| `app/core/power_supply_quality.py` | Ripple, RMS noise, stability rating, load regulation and settling time (time-based, sample-rate independent). |
| `app/core/markers.py` | `MarkerList` (ids, segments between markers, append-only CSV lines). |
| `app/core/benchmark.py` | Energy per inference, inferences per joule, throughput per watt (gross / net of idle power). |
| `app/export/recorder.py` | `RecordingWriter`: continuous, crash-safe CSV recording. |
| `app/ui/marker_actions.py` | Shared marker gestures (key, button, context menu, rename, drag). |
| `app/core/spectrum.py` | FFT of the load variation on a uniform resampled grid, Hann window, amplitude-correct peaks. |
| `app/core/settings.py` | `AppSettings` dataclass, QSettings persistence, validation, migration from 1.x. |
| `app/serial/protocol.py` | `StreamDecoder`, `Calibration`, `DeviceConfig`, `DeviceInfo`. Qt-free and unit-tested. |
| `app/serial/serial_reader.py` | `SerialReader` QThread: handshake, commands, clock sync, watchdog, batching. |
| `app/serial/handler.py` | Low-level port I/O; raises `DeviceDisconnected` on hang-up. |
| `app/export/csv_io.py` | CSV export (separator, decimal comma, date/time or Unix time) and import (all layouts written by any app version or by the firmware). |
| `app/export/pdf_report.py` | PDF report with vector charts drawn by reportlab (no matplotlib). |
| `app/export/units.py` | Engineering-prefix formatting shared by UI and reports. |
| `app/i18n/` | Runtime translation lookup (`tr()`, `N_()`, `set_language()`). |
| `app/ui/controller.py` | `AcquisitionController` (session, data, reconnection). |
| `app/ui/main_window.py` | Window shell: navigation rail, connection bar, pages, status bar, shortcuts. |
| `app/ui/pages/*.py` | Live, Analysis, Device, Settings and About pages. |
| `app/ui/widgets/plot_widget.py` | Stacked V/I/P plots: live (follow) and overview (selection region) modes. |
| `app/ui/file_actions.py` | Import/export dialogs running in a background thread with progress. |
| `app/core/gpu_*.py` | Pre-start OpenGL probe and fallback (default / NVIDIA offload / software). |

---

## User interface

- **Live** – stacked voltage/current/power plots with selectable series,
  time-window presets, follow-live toggle (drag to look back, double-click to
  return), crosshair readout in the status bar, live tiles (value, min/max,
  moving or whole-recording average power) and energy, charge, sample rate and
  lost samples.
- **Analysis** – overview of the whole recording with a draggable selection;
  statistics per quantity, energy/charge/RMS, power-supply quality with
  recommendations, frequency spectrum (linear/log), **Markers** (segments
  between markers with duration, average/peak power, energy, charge; click a
  row to select it) and **Benchmark** (energy per inference, inferences per
  joule, throughput per watt, gross and net of the idle power). Export the
  selection or the whole recording as CSV or PDF; import CSV.
- **Device** – connection and firmware info, INA226 averaging and conversion
  times (presets and custom, with the resulting sample rate), OLED on/off,
  save as device default, clock sync with the PC and device–PC offset,
  host-side calibration (shunt, offset with "zero now", gains), device log and
  a raw command line.
- **Settings** – language, theme (dark/light/system), refresh rate, time
  window, line width, antialiasing, grid, crosshair, units (auto prefix, base,
  milli), significant digits, average-power mode, CPU indicator, OpenGL,
  baud rate, auto-reconnect, no-data timeout, host averaging, CSV format and
  PDF contents. Every change applies immediately and is saved.

Keyboard: `Ctrl+R` start/stop, `M` add a marker, `Ctrl+O` import,
`Ctrl+E` export CSV, `Ctrl+P` export PDF, `Ctrl+1…5` switch page.

### Markers

Markers are added with the `M` key or the *Marker* button while recording
(placed at the best estimate of "now": newest sample time plus the time
elapsed since it arrived), or with a right-click on any plot at the exact
point. Double-click a marker to rename it; drag it on the Analysis plot to
move it; right-click it to delete it. Markers are stored in CSV files as
trailing lines:

```
#MARKER<TAB>id<TAB>time_s<TAB>label        (add or replace)
#MARKER<TAB>id<TAB>DELETE                  (remove)
```

Later lines for the same id win, so a recording file is only ever appended
to. Tools such as pandas can skip them with `comment="#"`.

### Continuous recording

With *Save recordings automatically* (default on) every live acquisition is
written, while it runs, to `Documents/EdgePowerMeter/recording_YYYYMMDD_HHMMSS.csv`
(folder configurable). It is the same CSV format as Export CSV, flushed about
once per second, so the file is valid even after a crash. No hidden or
temporary files are created; recordings shorter than one second (accidental
Start/Stop) are deleted; existing files are never overwritten. Exporting the
whole recording as CSV offers the existing file instead of creating a
duplicate. The status bar shows the file and its size; click it to open the
folder.

---

## Translations

All UI text is written in English and wrapped in `tr()`. Catalogs for Italian,
Spanish, French and German are generated **offline on the developer machine**
by a local machine-translation model (Argos Translate models run through
CTranslate2; no cloud service), then corrected through
`app/i18n/overrides/<lang>.json`, which always wins. The tool refuses
translations that lose placeholders or brackets. See
[tools/i18n/README.md](../tools/i18n/README.md):

```bash
tools/i18n/.venv/bin/python tools/i18n/translate.py extract
tools/i18n/.venv/bin/python tools/i18n/translate.py translate --prune
tools/i18n/.venv/bin/python tools/i18n/translate.py check
```

The language defaults to the operating-system language and can be changed in
Settings; the window is rebuilt in place without losing data.

---

## Settings

`AppSettings` is persisted with `QSettings` (group `settings_v2`):

- Linux: `~/.config/EdgePowerMeter/EdgePowerMeter.conf`
- Windows: registry `HKCU\Software\EdgePowerMeter`
- macOS: `~/Library/Preferences/com.EdgePowerMeter.plist`

Values are type-checked and clamped on load, so a corrupted entry falls back
to its default. The few options that existed in 1.x are migrated.

---

## Export formats

### CSV

```
Timestamp,RelativeTime[s],Voltage[V],Current[A],Power[W]
2026-10-08 12:34:56.123,0.000000,5.012500,0.250000,1.253125
```

- `RelativeTime` is the precise (device) time axis; `Timestamp` is local
  wall-clock time with millisecond resolution, or Unix seconds if chosen.
- With *decimal comma* the separator becomes `;` for spreadsheet locales such
  as Italian or German.
- Import auto-detects separator, decimal comma, timestamp style and the
  4-column layout of older files and firmware logs.

### PDF

Key-figure tiles, recording metadata, per-quantity table (min, max, average,
std dev, RMS, peak-to-peak), energy and derived quantities (charge, power
variability, current crest factor, voltage ripple, load resistance), markers
and segment table, benchmark results, vector charts of V/I/P with min/max
envelopes and markers, power distribution histogram with percentiles,
power-supply quality with pass/fail table and recommendations, frequency
spectrum (linear and dB) with peaks, and the instrument settings
(sensor configuration and calibration) for traceability.

---

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Tests run headless (`QT_QPA_PLATFORM=offscreen`), use temporary QSettings and
a scripted fake device (`tests/fakes.py`) that emulates firmware 1.x and 2.x,
including unplugging, silence, slow boot and dropped lines.

---

## Troubleshooting

**Permission denied on the serial port (Linux)** – add the user to the
`dialout` group and log in again: `sudo usermod -a -G dialout $USER`.

**"Firmware 1.x detected"** – the app works, but device time, lost-sample
detection, sensor configuration and clock sync need firmware 2.0
(`firmware/` folder, see HARDWARE.md).

**Lost samples increase** – the PC is not reading fast enough (very slow
machine or heavy load) or the INA226 period is below ~0.5 ms. Increase the
averaging on the Device page.

**Blank or slow plots** – disable *Hardware acceleration (OpenGL)* in
Settings, or use *Re-detect graphics on next launch* on the About page.
