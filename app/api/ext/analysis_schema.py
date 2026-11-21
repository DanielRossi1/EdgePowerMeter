"""Tool schemas of the `analysis` extension (stdlib only: the MCP bridge imports this).

Spectrum, power-supply quality, distribution and peak (event) analysis over a range of
the recording. All tools accept the standard range arguments (see docs/API.md, Analysis).
"""
from __future__ import annotations

from typing import Dict, List

from app.api import schema

_SIGNALS = ("voltage", "current", "power")
_RANGE_NOTE = " Time values are seconds on the recording time axis."

_QUANTITY = ("Quantity to analyse: voltage (V), current (A) or power (W).")

# percentiles: an array of numbers in 0..100. The service checks minItems and maxItems;
# the handler checks the length again.
PERCENTILES_DEFAULT: List[float] = [1, 5, 25, 50, 75, 95, 99]
PERCENTILES_MAX_ITEMS = 20


def _percentiles() -> dict:
    out = {
        "type": "array",
        "description": "Percentiles to compute, each 0 to 100 (for example 50 is the median). "
                       "Keys in the result are 'p<value>', e.g. 'p95' (values that would share "
                       "a key, such as 99.99 and 99.9999, are refused). At most 20 items.",
        "default": list(PERCENTILES_DEFAULT),
        "items": schema.number("A percentile from 0 to 100.", minimum=0, maximum=100),
        "minItems": 1,
        "maxItems": PERCENTILES_MAX_ITEMS,
    }
    return out


TOOLS: List[schema.Tool] = [
    schema.tool(
        "analyze_spectrum",
        "Frequency spectrum of the load variation in a range (FFT with a Hann window): shows "
        "where the variation energy sits, e.g. periodic inference bursts, switching-regulator "
        "ripple, or thermal throttling cycles. Returns the DC level (mean), the variation RMS "
        "(std), modulation_percent (std / mean * 100), the sample_rate and nyquist_hz (Hz), the "
        "frequency resolution (resolution_hz), the n_peaks strongest peaks "
        "[{frequency_hz, amplitude}] in signal units, and the amplitude spectrum reduced to "
        "points_out points (spectrum.frequency_hz and spectrum.amplitude, max-pooled per bin: "
        "each bin keeps its largest amplitude and that bin's frequency). The signal is resampled "
        "onto a uniform grid first. Long ranges are decimated with an anti-alias filter to "
        "max_points: the result then has decimated=true, effective_sample_rate_hz (the rate "
        "of the samples the FFT used) and a note saying which frequencies were filtered out; "
        "otherwise decimated=false. If the range is too short (under 64 samples) or has no "
        "variation, the result is {available: false, reason} instead of an error." + _RANGE_NOTE,
        schema.range_props({
            "signal": schema.enum("Signal to analyse: voltage (V), current (A) or power (W).",
                                  _SIGNALS, default="current"),
            "n_peaks": schema.integer("Number of strongest peaks to return.", default=5,
                                      minimum=1, maximum=20),
            "max_points": schema.integer("Maximum samples used for the FFT (longer ranges are "
                                         "decimated with an anti-alias filter).",
                                         default=1048576, minimum=1024, maximum=4194304),
            "points_out": schema.integer("Number of spectrum points returned (max-pooled). Use a "
                                         "smaller value for a compact answer.", default=256,
                                         minimum=16, maximum=2048),
        })),
    schema.tool(
        "power_quality",
        "Power supply quality of a DC rail over a range: voltage min/max/mean-based ripple "
        "(voltage_ripple_percent of the nominal voltage, voltage_ripple_mv peak-to-peak), "
        "rms_noise and std_deviation (V), load_regulation_percent and settling_time_ms around "
        "the first significant load step (null when there is no step), a stability_rating "
        "(excellent, good, fair or poor) and whether the ripple meets the 1 %, 0.1 % and 0.05 % "
        "specs. nominal_voltage sets the reference for the percentages; by default the mean "
        "voltage of the range is used. Needs at least 10 samples in the range." + _RANGE_NOTE,
        schema.range_props({
            "nominal_voltage": schema.number("Nominal rail voltage in V for the percentage "
                                             "figures. Default: the mean voltage of the range.",
                                             minimum=0, maximum=1000),
        })),
    schema.tool(
        "distribution",
        "Distribution of a quantity over a range: count, mean, std, min, max, percentiles "
        "(keys 'p<value>', e.g. p95) and a histogram {edges, counts} with equal-width bins. "
        "Use it to see the spread of power or current, e.g. the typical versus peak load. "
        "Each sample counts once (no time weighting)." + _RANGE_NOTE,
        schema.range_props({
            "quantity": schema.enum(_QUANTITY, _SIGNALS, default="power"),
            "percentiles": _percentiles(),
            "bins": schema.integer("Number of equal-width histogram bins.", default=40,
                                   minimum=2, maximum=200),
        })),
    schema.tool(
        "detect_peaks",
        "Find events where a quantity stays above a threshold: each event is a run of "
        "consecutive samples above the threshold (no merging of nearby runs). Use it to count "
        "load bursts or inference spikes. The threshold is either absolute (threshold, in the "
        "quantity's unit) or threshold_sigma standard deviations above the range mean. Returns "
        "threshold_used, the baseline mean and std, the total count (even when the list is "
        "cut to max_events; then truncated is true), rate_per_s, mean_duration_s and the events "
        "in time order: t_start, t_end, duration_s, peak, mean and, for power, energy_j (joules, "
        "trapezoid integral over the event's samples). Events shorter than min_duration_s are "
        "dropped." + _RANGE_NOTE,
        schema.range_props({
            "quantity": schema.enum(_QUANTITY, _SIGNALS, default="power"),
            "threshold": schema.number("Absolute threshold in the quantity's unit (V, A or W). "
                                       "Samples strictly above it count. Default: use "
                                       "threshold_sigma."),
            "threshold_sigma": schema.number("Used when threshold is absent: threshold = mean + "
                                             "k * std of the range, with k given here.",
                                             default=3, minimum=0.5, maximum=20),
            "min_duration_s": schema.number("Drop events shorter than this (seconds).",
                                            default=0, minimum=0, maximum=60),
            "max_events": schema.integer("Maximum number of events listed (the count is "
                                         "always the total).", default=100, minimum=1,
                                         maximum=1000),
        })),
]

# Every tool may take a while on long recordings: the 600 s long-tool timeout.
TIMEOUTS: Dict[str, str] = {t.name: "long" for t in TOOLS}
