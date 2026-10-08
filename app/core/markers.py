"""Event markers on the recording time axis and the segments between them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .samples import Samples
from .statistics import Statistics

# Line format used inside CSV files (recordings and exports). Append-only
# friendly: a later line with the same id replaces the earlier one, and a
# DELETE line removes it, so an autosaved recording never has to be rewritten.
MARKER_PREFIX = "#MARKER"


@dataclass
class Marker:
    id: int
    t: float
    label: str


@dataclass
class Segment:
    start: float
    end: float
    name: str
    stats: Optional[Statistics]


def clean_label(label: str) -> str:
    return " ".join(str(label).replace("\t", " ").split())[:60]


class MarkerList:
    """Sorted list of markers with stable ids."""

    def __init__(self) -> None:
        self._items: Dict[int, Marker] = {}
        self._next_id = 1

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self.sorted())

    def sorted(self) -> List[Marker]:
        return sorted(self._items.values(), key=lambda m: (m.t, m.id))

    def get(self, marker_id: int) -> Optional[Marker]:
        return self._items.get(marker_id)

    def add(self, t: float, label: str = "") -> Marker:
        mid = self._next_id
        self._next_id += 1
        m = Marker(mid, float(t), clean_label(label) or f"M{mid}")
        self._items[mid] = m
        return m

    def put(self, marker_id: int, t: float, label: str) -> Marker:
        """Insert or replace with an explicit id (used when loading files)."""
        m = Marker(int(marker_id), float(t), clean_label(label) or f"M{marker_id}")
        self._items[m.id] = m
        self._next_id = max(self._next_id, m.id + 1)
        return m

    def remove(self, marker_id: int) -> bool:
        return self._items.pop(marker_id, None) is not None

    def rename(self, marker_id: int, label: str) -> Optional[Marker]:
        m = self._items.get(marker_id)
        if m is not None:
            m.label = clean_label(label) or m.label
        return m

    def move(self, marker_id: int, t: float) -> Optional[Marker]:
        m = self._items.get(marker_id)
        if m is not None:
            m.t = float(t)
        return m

    def clear(self) -> None:
        self._items.clear()
        self._next_id = 1

    def nearest(self, t: float, tolerance: float) -> Optional[Marker]:
        best = None
        for m in self._items.values():
            d = abs(m.t - t)
            if d <= tolerance and (best is None or d < abs(best.t - t)):
                best = m
        return best

    # ------------------------------------------------------------ segments

    def boundaries(self, data: Samples) -> List[Tuple[float, str]]:
        if len(data) < 2:
            return []
        t0, t1 = float(data.t[0]), float(data.t[-1])
        inner = [(m.t, m.label) for m in self.sorted() if t0 < m.t < t1]
        return [(t0, "")] + inner + [(t1, "")]

    def segments(self, data: Samples, start_label: str = "Start",
                 end_label: str = "End") -> List[Segment]:
        """Segments between consecutive markers (and the recording ends)."""
        b = self.boundaries(data)
        out: List[Segment] = []
        for (a, la), (z, lz) in zip(b, b[1:]):
            name = f"{la or start_label} → {lz or end_label}"
            out.append(Segment(a, z, name, Statistics.from_samples(data.between(a, z))))
        return out

    # --------------------------------------------------------- persistence

    @staticmethod
    def line(m: Marker) -> str:
        return f"{MARKER_PREFIX}\t{m.id}\t{m.t:.6f}\t{m.label}"

    @staticmethod
    def delete_line(marker_id: int) -> str:
        return f"{MARKER_PREFIX}\t{marker_id}\tDELETE"

    def lines(self) -> List[str]:
        return [self.line(m) for m in self.sorted()]

    def apply_line(self, text: str) -> bool:
        """Apply one '#MARKER' line; returns False if it is not a marker line."""
        if not text.startswith(MARKER_PREFIX):
            return False
        parts = text.rstrip("\r\n").split("\t")
        try:
            mid = int(parts[1])
            if len(parts) >= 3 and parts[2] == "DELETE":
                self.remove(mid)
            else:
                self.put(mid, float(parts[2]), parts[3] if len(parts) > 3 else "")
        except (IndexError, ValueError):
            return False
        return True
