"""Schema of the `triggers` extension: wait for a live condition, capture an event.

Stdlib only (through app.api.schema): the MCP bridge imports this module. The handlers
are in app/api/ext/triggers.py. Import app.api.schema first (see app/api/ext/__init__.py).
"""

from __future__ import annotations

from typing import Dict, List

from ..schema import Tool, enum, number, string, tool

QUANTITY_VALUES = ("voltage", "current", "power")
OP_VALUES = (">", ">=", "<", "<=")

# Bridge timeouts: wait_until and capture_event may wait timeout_s for the trigger, and
# capture_event then records post_s more (at most 50 s, see POST_S_MAX). The bridge
# allows timeout_s + 60 s, which covers the post phase plus its own 5 s grace.
POST_S_MAX = 50.0

_QUANTITY = enum("Quantity to watch: voltage (V), current (A) or power (W) of each sample.",
                 QUANTITY_VALUES)
_OP = enum("Comparison: '>' '>=' '<' or '<=' between the (smoothed) value and threshold.",
           OP_VALUES)
_THRESHOLD = number("Threshold in the unit of the quantity (V, A or W).")
_HOLD = number("Seconds the condition must hold without a break, measured on the recording "
               "time axis (0 = the first sample that meets it triggers). A gap in the data longer "
               "than max(2 x the median sample interval, 0.05 s) also breaks the run.",
               default=0, minimum=0, maximum=60)
_SMOOTHING = number("Moving-average window in seconds applied before the comparison (0 = raw "
                    "samples). Only samples recorded after the call are averaged, and no sample "
                    "is evaluated until a full window of post-call samples exists: the first "
                    "smoothing_s seconds after the call cannot trigger.", default=0,
                    minimum=0, maximum=10)
_TIMEOUT = number("Seconds to wait for the trigger before giving up (not an error: "
                  "triggered=false is returned).", default=60, minimum=0.1, maximum=86400)

_WAIT_DESCRIPTION = (
    "Block until a live condition holds, then return. Requires a running acquisition. "
    "Watches only samples recorded after the call starts, so data already present cannot "
    "trigger it. Use it to wait for a load to switch on, a voltage to drop below a limit, "
    "or a power to exceed a level before the next step. Returns triggered=true with the "
    "recording time t and the value at that time, or triggered=false on timeout. "
    "Errors if the acquisition stops, the data is cleared or the API server is stopped while "
    "waiting.")

_CAPTURE_DESCRIPTION = (
    "Wait for a trigger like wait_until, then keep recording for post_s seconds and return "
    "the statistics of the window [t_trigger - pre_s, t_trigger + post_s] (clipped to the "
    "start of the data). Two markers '<label> start' and '<label> end' are added at the window "
    "edges. Only samples recorded after the call can trigger it; pre_s may reach back into "
    "data recorded before the call. If not triggered within timeout_s, nothing is recorded "
    "and no markers are added. Use it to capture one event (a surge, a dropout, a load step) "
    "with its statistics. Errors if the acquisition stops, the data is cleared or the API "
    "server is stopped while waiting or recording.")


def _trigger_props(extra: Dict[str, dict]) -> Dict[str, dict]:
    props = {
        "quantity": _QUANTITY,
        "op": _OP,
        "threshold": _THRESHOLD,
        "hold_s": _HOLD,
        "smoothing_s": _SMOOTHING,
    }
    props.update(extra)
    return props


TOOLS: List[Tool] = [
    tool("wait_until", _WAIT_DESCRIPTION,
         _trigger_props({"timeout_s": _TIMEOUT}),
         required=["quantity", "op", "threshold"]),
    tool("capture_event", _CAPTURE_DESCRIPTION,
         _trigger_props({
             "pre_s": number("Seconds of the window before the trigger time (0 to 60).",
                             default=1, minimum=0, maximum=60),
             "post_s": number("Seconds recorded after the trigger time (0 to 50). The window "
                              "ends at t_trigger + post_s.", default=2, minimum=0,
                              maximum=POST_S_MAX),
             "timeout_s": _TIMEOUT,
             "label": string("Marker label prefix: the markers are '<label> start' and "
                             "'<label> end'.", default="event", min_length=1, max_length=40),
         }),
         required=["quantity", "op", "threshold"]),
]

# Bridge timeouts (see app/api/mcp_bridge.py tool_timeout_s): timeout_s + 60 s.
TIMEOUTS: Dict[str, str] = {
    "wait_until": "param:timeout_s",
    "capture_event": "param:timeout_s",
}
