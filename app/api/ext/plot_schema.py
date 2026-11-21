"""Schema of the `plot` extension: render_plot (stdlib only, imported by the MCP bridge)."""

from __future__ import annotations

from typing import Dict

from app.api import schema

PLOT_QUANTITIES = ("voltage", "current", "power")

_QUANTITIES = {
    "type": "array",
    "description": "Quantities to draw, one stacked panel each: any of voltage (V), current "
                   "(A), power (W); 1 to 3 different items. Default: [\"power\"].",
    "items": {"type": "string", "enum": list(PLOT_QUANTITIES)},
    "minItems": 1,
    "maxItems": 3,
    "default": ["power"],
}

TOOLS = [
    schema.tool(
        "render_plot",
        "Draw the chosen range as a PNG picture so you can look at the waveform (for example "
        "bursts, spikes or drop-outs). One stacked panel per quantity; each pixel column shows "
        "the min to max envelope of its samples (so spikes are never lost) and the mean line. "
        "Vertical dashed lines mark the markers inside the range, labelled with their ids. The "
        "panels use the same time axis (seconds on the recording time axis). Returns the image "
        "and the min, max and mean of each quantity over the range. Use get_samples or "
        "get_statistics for exact numbers.",
        schema.range_props({
            "quantities": _QUANTITIES,
            "width": schema.integer("Image width in pixels (200 to 2000).", default=900,
                                    minimum=200, maximum=2000),
            "height": schema.integer("Height of each panel in pixels (150 to 1200). The image "
                                     "is this tall times the number of quantities.",
                                     default=360, minimum=150, maximum=1200),
            "show_markers": schema.boolean("Draw the markers inside the range.", default=True),
        }),
    ),
]

TIMEOUTS: Dict[str, str] = {"render_plot": "long"}
