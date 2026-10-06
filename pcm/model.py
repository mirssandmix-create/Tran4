"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels


@dataclass
class TextLine:
    text: str
    poly: list[Point]  # 4 corners (clockwise) in image pixels; rotated boxes allowed


@dataclass
class TextBlock:
    """One speech bubble / caption: what gets translated and typeset as a unit."""
    id: str
    text: str
    lines: list[TextLine]
    box: Box
    vertical: bool = False
    lang: str = ""
    translation: str = ""
    via: str = ""   # who wrote .translation: engine name or "google"; "" = original art kept on purpose

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass
class PageResult:
    ok: bool
    image: bytes = b""          # translated image (JPEG)
    mime: str = "image/jpeg"
    blocks: list[TextBlock] = field(default_factory=list)
    error: str = ""
    from_cache: bool = False


def union_box(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def poly_box(poly: list[Point]) -> Box:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))
