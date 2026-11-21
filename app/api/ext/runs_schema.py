"""Schema of the "runs" extension: named experiment results and their comparison.

Stdlib only (the MCP bridge imports this module). The handlers are in runs.py.

Note for maintainers: the core argument validator (service._validate) keeps only the
keys declared in an object's "properties", so the free-form `metadata` argument has no
"type" keyword (its keys and values are checked in runs.py). The core validator enforces
`maxItems` (the tag count) too; runs.py repeats that check.
"""

from __future__ import annotations

from typing import List

from .. import schema
from ..schema import Tool

RUN_NAME_HELP = ("Run name: 1 to 64 characters, letters, digits, '.', '_' or '-', starting "
                 "with a letter or digit, not ending in .json or '.'. Windows device names "
                 "(CON, PRN, AUX, NUL, COM1-COM9, LPT1-LPT9, also with an extension) are refused.")

METRIC_NAMES = ("mean_power_w", "max_power_w", "min_power_w", "p95_power_w", "p99_power_w",
                "energy_j", "energy_per_second_j", "duration_s", "mean_current_a",
                "mean_voltage_v")

_name = schema.string(RUN_NAME_HELP, min_length=1, max_length=64)

_tags_prop = {
    "type": "array",
    "description": "Free-text labels, at most 20, each at most 40 characters "
                   "(for example 'idle', 'decode', 'rev-b').",
    "items": {"type": "string", "maxLength": 40},
    "maxItems": 20,
    "default": [],
}

_metadata_prop = {
    # No "type" on purpose: see the module note. Keys are 1 to 40 characters of letters,
    # digits, '_', '.' or '-'; at most 50 keys; values are strings (200 characters at most),
    # numbers or booleans.
    "description": "Extra facts about the run as a JSON object: at most 50 keys (letters, "
                   "digits, '_', '.', '-'; up to 40 characters), each value a string (up to "
                   "200 characters), a number or a boolean. For example {\"firmware_rev\": "
                   "\"2.0.1\", \"load\": \"video decode\", \"ambient_c\": 24}.",
    "additionalProperties": {"type": ["string", "number", "boolean"]},
    "maxProperties": 50,
}

_metrics_prop = {
    "type": "array",
    "description": "Which metrics to compare: any of " + ", ".join(METRIC_NAMES) + ". "
                   "Default: all of them.",
    "items": {"type": "string", "enum": list(METRIC_NAMES)},
    "minItems": 1,
}

TOOLS: List[Tool] = [
    schema.tool(
        "save_run",
        "Save a named experiment result: the statistics of a range (default: all data, "
        "the same figures as get_statistics), a power distribution summary (p5, p50, p95, "
        "p99), energy, connected time, sample rate, lost samples, device info and config, "
        "firmware, the calibration and host averaging settings, and your notes, tags and "
        "metadata. Stored as a JSON file in the recordings folder under runs/. Use it to "
        "keep a measurement under a name (for example 'idle-baseline' or 'decode-rev-b') "
        "so compare_runs can check a later change. Refuses to replace an existing run "
        "unless overwrite is true: overwrite replaces the whole record, including notes, tags "
        "and metadata (they are not merged). Returns replaced=true when an existing run was "
        "overwritten. Needs data: an acquisition or an imported CSV.",
        {
            "name": _name,
            **schema.range_props(),
            "notes": schema.string("Free-text notes about the run (at most 2000 characters).",
                                   default="", max_length=2000),
            "tags": _tags_prop,
            "metadata": _metadata_prop,
            "overwrite": schema.boolean("Replace an existing run with this name.",
                                        default=False),
        },
        required=["name"]),
    schema.tool(
        "list_runs",
        "List saved runs, newest first: name, created time, tags, duration, mean power and "
        "energy. Optionally only the runs that carry one tag. At most 2000 files are read (the "
        "most recently modified ones) and at most 64 MiB is parsed per call: truncated=true "
        "means some runs were not read. Unreadable or corrupted run "
        "files are skipped and their names are returned under 'unreadable'.",
        {
            "limit": schema.integer("How many runs to return (1 to 500).", default=50,
                                    minimum=1, maximum=500),
            "tag": schema.string("Only runs that have this tag.", max_length=40),
        }),
    schema.tool(
        "get_run",
        "Full stored record of one saved run: statistics, power distribution, energy, "
        "device info and config, firmware, settings, notes, tags and metadata.",
        {"name": _name},
        required=["name"]),
    schema.tool(
        "compare_runs",
        "Compare a candidate run with a baseline run. For each metric returns the baseline "
        "value, the candidate value, delta (candidate minus baseline) and delta_percent "
        "(delta divided by the absolute value of the baseline, in percent; null when the "
        "baseline is zero, a value is missing or the result is not finite), and a one-sentence "
        "summary (it says 'no percentage change available' when delta_percent is null). Energy per second is the energy "
        "divided by the run duration. Use it to check whether a change saved energy or "
        "moved the power peaks.",
        {
            "baseline": schema.string("Name of the reference run.", min_length=1, max_length=64),
            "candidate": schema.string("Name of the run to compare against the baseline.",
                                       min_length=1, max_length=64),
            "metrics": _metrics_prop,
        },
        required=["baseline", "candidate"]),
    schema.tool(
        "delete_run",
        "Delete one saved run file. Refuses symbolic links and anything that is not a "
        "regular file.",
        {"name": _name},
        required=["name"]),
]
