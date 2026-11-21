"""Automation API: tool registry and shared helpers (Qt-free).

The same registry drives the HTTP API (`server.py`), the implementation
(`service.py`) and the MCP bridge (`mcp_bridge.py`), so the three never
disagree about tool names or parameters.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

DEFAULT_PORT = 8765
API_VERSION = 1
DISCOVERY_ENV = "EPM_API_DISCOVERY"
# os.pathsep-separated absolute folders the export/import tools may also use.
EXPORT_DIRS_ENV = "EPM_API_EXPORT_DIRS"
TEXT_MAX_CHARS = 2000    # report title / notes and other free text
MARKER_LABEL_MAX_CHARS = 60
# Tools that block for a long time on the server (waiting for samples, a timed window or an
# event). At most a few run at once, so they cannot starve the short tools and health.
# Extension tools that wait must be listed here by name.
LONG_WAIT_TOOLS = frozenset({"wait", "measure", "wait_until", "capture_event", "get_events"})
SEND_COMMAND_PER_SECOND = 20   # send_command calls per second (429 beyond that)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str      # written for an LLM: what it does, units, when to use it
    input_schema: dict    # JSON Schema of the arguments (type "object")


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "EdgePowerMeter"


def api_discovery_path() -> Path:
    """File where a running app publishes its API URL and token for local clients."""
    override = os.environ.get(DISCOVERY_ENV)
    return Path(override).expanduser() if override else config_dir() / "api.json"


# ----------------------------------------------------------- schema helpers
# Literal copies of app/serial/protocol.py constants (this module must stay
# stdlib-only); tests/test_api_service.py checks they match.
INA226_AVG_VALUES = (1, 4, 16, 64, 128, 256, 512, 1024)
INA226_CT_VALUES_US = (140, 204, 332, 588, 1100, 2116, 4156, 8244)
CSV_SEPARATORS = (",", ";", "\t")
CSV_DECIMALS = (".", ",")
CSV_TIME_FORMATS = ("datetime", "epoch")
QUANTITIES = ("v", "i", "p")
# Literal copy of app/core/settings.OFFSET_LIMIT (calibration offsets in V or A);
# tests/test_api_service.py checks they match.
OFFSET_LIMIT = 100.0


def _schema(properties: Dict[str, dict], required: Sequence[str] = ()) -> dict:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _prop(kind: str, description: str, default: Any = None, **constraints: Any) -> dict:
    out: Dict[str, Any] = {"type": kind, "description": description}
    if default is not None:
        out["default"] = default
    out.update({k: v for k, v in constraints.items() if v is not None})
    return out


def _number(description: str, default: Any = None, minimum: Any = None, maximum: Any = None,
            exclusive_minimum: Any = None) -> dict:
    return _prop("number", description, default, minimum=minimum, maximum=maximum,
                 exclusiveMinimum=exclusive_minimum)


def _integer(description: str, default: Any = None, minimum: Any = None,
             maximum: Any = None) -> dict:
    return _prop("integer", description, default, minimum=minimum, maximum=maximum)


def _boolean(description: str, default: Any = None) -> dict:
    return _prop("boolean", description, default)


def _string(description: str, default: Any = None, min_length: Any = None,
            max_length: Any = None) -> dict:
    return _prop("string", description, default, minLength=min_length, maxLength=max_length)


def _enum(description: str, values: Sequence[Any], default: Any = None) -> dict:
    kind = "string" if all(isinstance(v, str) for v in values) else "integer"
    out = _prop(kind, description, default)
    out["enum"] = list(values)
    return out


def _quantities() -> dict:
    out = _prop("array", "Which quantities to return: any of v (voltage), i (current), "
                "p (power). Default: all three.")
    out["items"] = {"type": "string", "enum": list(QUANTITIES)}
    out["minItems"] = 1
    return out


RANGE_PROPERTIES: Dict[str, dict] = {
    "t0": _number("Start of the range in recording seconds (time axis of get_samples and "
                  "get_statistics). Default: start of the data."),
    "t1": _number("End of the range in recording seconds. Default: end of the data."),
    "last_s": _number("Use only the last N seconds of data. Overrides t0/t1.",
                      exclusive_minimum=0),
    "segment": _integer("Index (0-based) of a segment from get_segments. Overrides the other "
                        "range options.", minimum=0),
    "marker_from": _integer("Marker id: the range starts at this marker. Requires marker_to. "
                            "Overrides last_s and t0/t1. If marker_from is after marker_to, "
                            "the two are swapped (the range runs from the earlier marker to "
                            "the later one).", minimum=1),
    "marker_to": _integer("Marker id: the range ends at this marker. Requires marker_from.",
                          minimum=1),
}

RANGE_NOTE = (" Range: segment > marker_from/marker_to > last_s > t0/t1; the default is "
              "all data. Needs at least 2 samples in the range.")


def _range(properties: Dict[str, dict] = None) -> Dict[str, dict]:
    """Shared range arguments merged with a tool's own properties."""
    props = dict(RANGE_PROPERTIES)
    props.update(properties or {})
    return props


# Settings the automation API may read and change (see service.update_settings).
# Value schemas mirror the AppSettings field types and validate() limits.
SETTINGS_SCHEMA: Dict[str, dict] = {
    "auto_reconnect": _boolean("Reconnect automatically after an unexpected disconnect."),
    "no_data_timeout_s": _number("Seconds without data before the connection is reported "
                                 "as stuck.", minimum=3, maximum=600),
    "host_averaging": _integer("Host-side averaging of N samples (1 = off).",
                               minimum=1, maximum=1000),
    "sync_clock_on_connect": _boolean("Set the device clock when a session starts."),
    "shunt_ohm": _number("Shunt resistance in ohm (0.00001 to 100).", minimum=1e-5, maximum=100),
    "voltage_gain": _number("Host voltage gain factor (0.5 to 2).", minimum=0.5, maximum=2),
    "voltage_offset_v": _number("Host voltage offset in V (|offset| <= 100).",
                                minimum=-OFFSET_LIMIT, maximum=OFFSET_LIMIT),
    "current_gain": _number("Host current gain factor (0.5 to 2).", minimum=0.5, maximum=2),
    "current_offset_a": _number("Host current offset in A (|offset| <= 100).",
                                minimum=-OFFSET_LIMIT, maximum=OFFSET_LIMIT),
    "autosave": _boolean("Save live recordings to disk continuously."),
    "csv_separator": _enum("CSV column separator.", CSV_SEPARATORS),
    "csv_decimal": _enum("CSV decimal character.", CSV_DECIMALS),
    "csv_time_format": _enum("CSV timestamp format: datetime or epoch seconds.",
                             CSV_TIME_FORMATS),
    "pdf_include_graphs": _boolean("Include the time-series charts in PDF reports."),
    "pdf_include_psu": _boolean("Include the power-supply quality section in PDF reports."),
    "pdf_include_spectrum": _boolean("Include the spectrum section in PDF reports."),
    "report_title": _string("Default PDF report title.", max_length=TEXT_MAX_CHARS),
    "report_notes": _string("Default PDF report notes.", max_length=TEXT_MAX_CHARS),
}


ROOTS_NOTE = ("Paths are checked against the allowed folders: the recordings folder and the "
              "folders in the EPM_API_EXPORT_DIRS environment variable.")


def _tool(name: str, description: str, properties: Dict[str, dict] = None,
          required: Sequence[str] = (), schema: dict = None) -> Tool:
    return Tool(name, description, schema if schema is not None
                else _schema(properties or {}, required))


# Public names for extension schema modules (app/api/ext/<name>_schema.py). Same
# functions as the underscored ones above; extensions should use these.
tool = _tool
object_schema = _schema
number = _number
integer = _integer
boolean = _boolean
string = _string
enum = _enum
quantities = _quantities
range_props = _range


_RANGE_DOC = " Time values are seconds on the recording time axis."

_RANGE_HELP = (" Range: segment > marker_from/marker_to > last_s > t0/t1; the default is all data."
               " Needs at least 2 samples in the range.")

TOOLS: List[Tool] = [
    _tool("get_status",
          "Connection and recording state: whether acquisition is running, the port, the device "
          "protocol, sample count, duration, sample rate (Hz), lost samples, recording file, "
          "unsaved-data flag, marker count, device info and config, the last 10 app messages "
          "and command replies, and the app and API versions. Call this first to see what is "
          "going on.", {}),
    _tool("list_ports",
          "List serial ports with a description and suggest one to use (an ESP32-like USB device "
          "when found). Use before start_acquisition when the port is unknown.",
          {"show_all": _boolean("Include non-USB serial ports too.", default=False)}),
    _tool("start_acquisition",
          "Connect to the power meter on a serial port and start a new recording. The port must "
          "be one of the ports listed by list_ports (show_all ports included); any other value is "
          "refused. Existing data "
          "is discarded, so this is refused while there is unsaved data unless discard_unsaved "
          "is true. Waits until samples flow (up to wait_s) and returns the status. A start that "
          "fails (port missing or busy, permission denied, device stopped) is an error carrying "
          "the app's message; a start still connecting when wait_s runs out returns a 'warning'. "
          "Omit port to use the suggested port.",
          {
              "port": _string("Serial port from list_ports, e.g. /dev/ttyACM0 or COM3. "
                              "Default: the suggested port from list_ports.", min_length=1),
              "discard_unsaved": _boolean("Discard unsaved data of the previous recording.",
                                          default=False),
              "wait_s": _number("Seconds to wait for the first samples (0 = do not wait).",
                                default=10, minimum=0, maximum=600),
          }),
    _tool("stop_acquisition",
          "Stop the acquisition and disconnect from the device. The recording is kept and can "
          "still be analysed and exported. Returns the status.", {}),
    _tool("clear_data",
          "Delete the data and markers of the current recording, keeping the connection. "
          "Refused while there is unsaved data unless discard_unsaved is true. Returns the "
          "status.",
          {"discard_unsaved": _boolean("Discard unsaved data.", default=False)}),
    _tool("get_live",
          "Latest readings: voltage (V), current (A) and power (W) of the newest sample, the "
          "averages over the last window_s seconds, the sample rate (Hz), and the energy (Wh) "
          "and charge (Ah) since the recording started. Use it to watch a running experiment. "
          "Errors when there is no data yet.",
          {"window_s": _number("Averaging window in seconds (up to 3600).", default=1.0,
                               exclusive_minimum=0, maximum=3600)}),
    _tool("add_marker",
          "Add an event marker (for example 'load on' or 'phase 2') at a time on the recording "
          "axis, by default at the current live time. Markers split the recording into segments "
          "and are used to name ranges in the other tools.",
          {
              "label": _string("Marker label (up to 60 characters).", default="",
                               max_length=MARKER_LABEL_MAX_CHARS),
              "t": _number("Recording time in seconds. Default: the current live time."),
          }),
    _tool("list_markers",
          "All event markers sorted by time, each with id, t (seconds) and label. The ids serve "
          "as marker_from / marker_to in range arguments.", {}),
    _tool("update_marker",
          "Rename a marker and/or move it to another time. Errors on an unknown marker id.",
          {
              "id": _integer("Marker id from list_markers.", minimum=1),
              "label": _string("New label.", min_length=1, max_length=MARKER_LABEL_MAX_CHARS),
              "t": _number("New recording time in seconds."),
          }, required=["id"]),
    _tool("remove_marker",
          "Delete a marker. Returns removed=false when the id does not exist.",
          {"id": _integer("Marker id.", minimum=1)}, required=["id"]),
    _tool("get_segments",
          "Split the recording at its markers into segments (start to first marker, between "
          "markers, last marker to end), each with statistics. Segments are numbered from 0; "
          "pass the index as 'segment' in other tools to analyse one phase." + _RANGE_DOC, {}),
    _tool("get_statistics",
          "Statistics over a range: sample count, duration (s), min/avg/max/std of voltage (V), "
          "current (A) and power (W), RMS current (A), and energy (J, Wh) and charge (C, Ah) "
          "integrated over time. Pick the window with the range arguments, e.g. one segment or "
          "the last 30 seconds." + _RANGE_DOC + _RANGE_HELP,
          _range()),
    _tool("measure",
          "Main experiment primitive. Requires a running acquisition. Optionally waits settle_s, "
          "adds a start marker, records for duration_s, adds an end marker, and returns the "
          "statistics (energy in J/Wh, average power in W, ...) of exactly that window. Use it "
          "for one load step or phase; the markers stay in the recording. Fails if the "
          "acquisition stops during the measurement.",
          {
              "duration_s": _number("Measurement length in seconds (0 < d <= 86400).",
                                    exclusive_minimum=0, maximum=86400),
              "label": _string("Label for the start marker; the end marker is '<label> end'.",
                               default="measurement", min_length=1, max_length=50),
              "settle_s": _number("Seconds to wait before the window starts (e.g. for a load to "
                                  "settle).", default=0, minimum=0, maximum=3600),
          }, required=["duration_s"]),
    _tool("benchmark",
          "Energy efficiency of a workload over a range: energy per unit (J), units per joule, "
          "and throughput per watt (e.g. FPS/W). Give either units completed in the range or a "
          "rate in units per second. Idle power (W) can be given directly or as the mean power "
          "of a segment (idle_segment); net figures subtract it." + _RANGE_DOC + _RANGE_HELP,
          _range({
              "units": _number("Work units completed in the range (inferences, frames, ...).",
                               exclusive_minimum=0),
              "rate": _number("Throughput in units per second, if the count is unknown.",
                              exclusive_minimum=0),
              "idle_power_w": _number("Idle power in W to subtract for net figures.", minimum=0),
              "idle_segment": _integer("Segment index whose mean power is the idle power.",
                                       minimum=0),
          })),
    _tool("get_samples",
          "Raw time series over a range as arrays t (s), v (V), i (A), p (W). Long ranges are "
          "thinned to max_points by uniform stride decimation (evenly spaced samples, first and "
          "last included; peaks may be missed). Use it to plot or inspect the waveform."
          + _RANGE_DOC + _RANGE_HELP,
          _range({
              "max_points": _integer("Maximum points returned (10 to 100000).", default=2000,
                                     minimum=10, maximum=100000),
              "quantities": _quantities(),
          })),
    _tool("export_csv",
          "Write the samples of a range to a CSV file, with the markers inside the range. "
          "The file must end in .csv. Its folder must exist. Refuses to overwrite an existing "
          "file unless overwrite is true. overwrite replaces the whole file even if the export "
          "covers only part of the data. The file the current data was loaded from and the live "
          "recording file can never be overwritten. Uses the CSV format from the settings. "
          + ROOTS_NOTE + _RANGE_HELP,
          _range({
              "path": _string("Path of the .csv file to write: absolute, or relative to the "
                              "recordings folder.", min_length=1, max_length=4096),
              "overwrite": _boolean("Replace the file if it exists.", default=False),
          }), required=["path"]),
    _tool("export_pdf",
          "Write a PDF report for a range: key figures, charts, markers, benchmark and device "
          "calibration. The file must end in .pdf. Its folder must exist. Refuses to overwrite "
          "an existing file unless overwrite is true. overwrite replaces the whole file even if "
          "the export covers only part of the data. The file the current data was loaded from and "
          "the live recording file can never be overwritten. Title and notes default to the "
          "settings. " + ROOTS_NOTE + _RANGE_HELP,
          _range({
              "path": _string("Path of the .pdf file to write: absolute, or relative to the "
                              "recordings folder.", min_length=1, max_length=4096),
              "title": _string("Report title (default: the title from settings).",
                               max_length=TEXT_MAX_CHARS),
              "notes": _string("Report notes (default: the notes from settings).",
                               max_length=TEXT_MAX_CHARS),
              "overwrite": _boolean("Replace the file if it exists.", default=False),
          }), required=["path"]),
    _tool("import_csv",
          "Load a CSV recording (written by this app or the firmware log) as the current data, "
          "including its markers. Refused while acquisition runs, and while there is unsaved "
          "data unless discard_unsaved is true. Returns the status. The file must end in .csv "
          "and lie in the allowed folders (see export_csv); any other path reports 'file not "
          "found or not allowed'.",
          {
              "path": _string("Path of the CSV file: absolute, or relative to the recordings "
                              "folder.", min_length=1, max_length=4096),
              "discard_unsaved": _boolean("Discard unsaved data of the current recording.",
                                          default=False),
          }, required=["path"]),
    _tool("get_device",
          "Device identity (name, firmware, protocol), the current INA226 sensor configuration "
          "(averaging, conversion times, sample rate in Hz, OLED) and the host calibration "
          "(shunt and gains/offsets) that is applied to the samples.", {}),
    _tool("configure_device",
          "Change the device's INA226 sensor settings: averaging and conversion times set the "
          "sample rate (rate = 1 / ((vct + ict) * avg)); the OLED can be switched off to reduce "
          "noise. Requires a connected device with firmware 2.x. Set save_default to store the "
          "settings in the device flash. Waits briefly for the device to confirm and returns "
          "get_device. A command the device rejects is an error with the firmware's reason.",
          {
              "avg": _enum("INA226 averaging count.", INA226_AVG_VALUES),
              "vct_us": _enum("Bus voltage conversion time in microseconds.",
                              INA226_CT_VALUES_US),
              "ict_us": _enum("Shunt current conversion time in microseconds.",
                              INA226_CT_VALUES_US),
              "oled": _boolean("Enable the device display."),
              "save_default": _boolean("Store the settings as the device default (flash).",
                                       default=False),
          }),
    _tool("send_command",
          "Send a raw firmware command line (see docs/PROTOCOL.md, e.g. 'GET' or 'SET STREAM 0'). "
          "Only HELLO, GET, SET and SYNC are allowed: SAVE is refused (use configure_device with "
          "save_default=true) and MODE is managed by the app. The command must be ASCII and at "
          "most 20 calls per second are accepted (429 beyond that). "
          "Returns sent=false if no device is connected; otherwise waits up to 1 s for the "
          "firmware reply and returns it as 'reply'. A !OK/!ERR reply is returned as "
          "{ok, cmd, msg}. GET and SET return the device's !CFG as {type: 'config', config: {...}}; "
          "HELLO returns {type: 'hello', name, firmware, protocol}. Use configure_device for "
          "sensor settings.",
          {"command": _string("One command line without line breaks.", min_length=1,
                              max_length=200)}, required=["command"]),
    _tool("sync_clock",
          "Ask the device to set its real-time clock from this computer at the next second "
          "boundary. Requires firmware 2.x. Returns requested=false otherwise.", {}),
    _tool("get_settings",
          "Current values of the settings the API may read and change: host calibration, "
          "recording folder and autosave, CSV format, PDF report options and reconnect "
          "behaviour. The result also lists the export_roots (folders the export and import "
          "tools may use) and the recordings_folder. The recording folder itself can only be "
          "changed in the app.", {}),
    _tool("update_settings",
          "Change application settings. Only the listed keys are accepted (unknown keys are "
          "rejected), values are range-checked, and text must not contain control or bidi "
          "override characters. Settings that change measurements: the calibration (shunt_ohm, "
          "voltage_gain, voltage_offset_v, current_gain, current_offset_a; applied to the next "
          "samples) and autosave (whether live data is written to disk). Changes are saved and "
          "shown in the settings page. The result lists the keys that changed and any value the "
          "app adjusted after validation ('adjusted'). Report every measurement-affecting "
          "change to the user.",
          {"settings": _schema(SETTINGS_SCHEMA, required=())},
          required=["settings"]),
    _tool("wait",
          "Pause for a number of seconds without touching the device, e.g. to let a load settle "
          "or to pace an experiment. Returns waited_s.",
          {"seconds": _number("Seconds to wait (0 to 3600).", minimum=0, maximum=3600)},
          required=["seconds"]),
]

TOOLS_BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}


# ------------------------------------------------------------ extensions
# Extension tools live in app/api/ext/<name>_schema.py (see app/api/ext/__init__.py).
TIMEOUT_LONG = "long"                 # TIMEOUTS value: the 600 s long-tool timeout
TIMEOUT_PARAM_PREFIX = "param:"       # TIMEOUTS value prefix: timeout = that argument + 60 s
EXTENSION_TIMEOUTS: Dict[str, str] = {}


def install_extensions(modules: Sequence[Any], tools: List[Tool], tools_by_name: Dict[str, Tool],
                       timeouts: Dict[str, str]) -> None:
    """Add the tools and timeouts of extension schema modules to the given registry.

    Checks everything first, so a bad module leaves the registry untouched. Raises
    ValueError for a duplicate tool name or a bad TIMEOUTS entry, and RuntimeError for a
    module without a TOOLS list (usually: app.api.schema was not imported first).
    """
    owner: Dict[str, str] = {name: "the built-in tools" for name in tools_by_name}
    new_tools: List[Tool] = []
    new_timeouts: Dict[str, str] = {}
    for module in modules:
        mod_name = getattr(module, "__name__", repr(module))
        declared = getattr(module, "TOOLS", None)
        if not isinstance(declared, list):
            raise RuntimeError(f"{mod_name} defines no TOOLS list (import app.api.schema "
                               "before the extension's schema module)")
        own: Dict[str, Tool] = {}
        for t in declared:
            if not isinstance(t, Tool):
                raise ValueError(f"{mod_name}: TOOLS must hold schema.Tool objects, got {t!r}")
            if t.name in owner:
                raise ValueError(f"duplicate tool name '{t.name}' in {mod_name}: already "
                                 f"defined by {owner[t.name]}")
            owner[t.name] = mod_name
            own[t.name] = t
            new_tools.append(t)
        for name, spec in dict(getattr(module, "TIMEOUTS", {}) or {}).items():
            if name not in own:
                raise ValueError(f"{mod_name}: TIMEOUTS names '{name}', which is not a tool "
                                 "of that module")
            if spec == TIMEOUT_LONG:
                pass
            elif isinstance(spec, str) and spec.startswith(TIMEOUT_PARAM_PREFIX):
                arg = spec[len(TIMEOUT_PARAM_PREFIX):]
                prop = own[name].input_schema.get("properties", {}).get(arg)
                if prop is None or prop.get("type") not in ("number", "integer"):
                    raise ValueError(f"{mod_name}: TIMEOUTS for '{name}' needs a numeric "
                                     f"argument '{arg}'")
            else:
                raise ValueError(f"{mod_name}: TIMEOUTS for '{name}' must be 'long' or "
                                 f"'param:<arg>', got {spec!r}")
            new_timeouts[name] = spec
    tools.extend(new_tools)
    for t in new_tools:
        tools_by_name[t.name] = t
    timeouts.update(new_timeouts)


def _install_builtin_extensions() -> None:
    # Imported here, not at the top: the extension schema modules import helpers above.
    from . import ext
    install_extensions(ext.schema_modules(), TOOLS, TOOLS_BY_NAME, EXTENSION_TIMEOUTS)


_install_builtin_extensions()
