"""Schema of the `events` extension: the `get_events` tool (stdlib only, see app/api/ext/__init__.py).

The event log is described in docs/API.md (Events).
"""

from __future__ import annotations

from typing import List

from ..schema import Tool, integer, number, tool

# Event kinds, in the order they appear in the docs.
EVENT_KINDS = (
    "snapshot",           # first event of a log: the state when the log started
    "state",              # controller state change (idle, connecting, streaming, stale, ...)
    "message",            # app message (level info, warning or error)
    "command_reply",      # device reply to a command (ok, cmd, msg)
    "device_info",        # !HELLO identity (name, firmware, protocol)
    "device_config",      # !CFG sensor configuration
    "data_reset",         # data cleared or a recording imported
    "lost_samples",       # lost samples since the last event (delta, total)
    "recording_started",  # a recording file was opened on disk (path)
    "recording_stopped",  # the recording file was closed (path)
)

RING_SIZE = 1000          # events kept; older ones are dropped (get_events reports the gap)
MAX_WAIT_S = 300.0
DEFAULT_MAX_EVENTS = 100
MAX_EVENTS_LIMIT = 500

_GET_EVENTS_DESCRIPTION = (
    "Events of the app since the event log started (the first extension call after the API "
    "started): controller state changes, app messages, device replies, device info and config, "
    "data resets, lost samples and recording file start/stop. Each event has id (increasing), "
    "time (epoch seconds), kind and data. Poll with since_id = the last_id of the previous "
    "call; with wait_s > 0 the call blocks until a newer event exists (up to wait_s seconds). "
    "dropped=true means events between since_id and the oldest kept event were lost (the log "
    "keeps the last 1000 events). Use kinds to filter, and max_events to page. When the API "
    "server is stopped the call returns at once with closed=true (wait_s is not waited); after "
    "a restart the ids continue. Events emitted between a restart and the next get_events call "
    "are not recorded."
)

TOOLS: List[Tool] = [
    tool("get_events", _GET_EVENTS_DESCRIPTION, {
        "since_id": integer("Return events with an id greater than this (0 = from the start "
                            "of the log).", default=0, minimum=0),
        "wait_s": number("Seconds to wait for a new event when none is newer than since_id "
                         "(0 = return at once).", default=0, minimum=0, maximum=MAX_WAIT_S),
        "kinds": {
            "type": "array",
            "description": "Only these event kinds (default: all).",
            "items": {"type": "string", "enum": list(EVENT_KINDS)},
            "minItems": 1,
        },
        "max_events": integer("Maximum events returned, oldest first (1 to 500).",
                              default=DEFAULT_MAX_EVENTS, minimum=1, maximum=MAX_EVENTS_LIMIT),
    }),
]

# A long-poll waits up to wait_s, so the timeout follows that argument.
TIMEOUTS = {"get_events": "param:wait_s"}
