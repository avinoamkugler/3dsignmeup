"""Shared types and conventions for the core pipeline.

This file is the contract between core modules. Do not change signatures or
semantics here without updating every consumer.

Label grid convention
---------------------
The pipeline's working representation of the image is a **label grid**:
an ``np.ndarray`` of shape (H, W), dtype int16, where each cell is either a
palette index (0..n-1) or one of the sentinels below.

Sentinels:
- ``TRANSPARENT`` (-1): no material. Originally-transparent pixels, corner-
  connected background cleared by flood fill, and enclosed background in
  ``through_hole`` mode (cuts through art AND plate).
- ``HOLE_PLATE`` (-2): enclosed background in ``plate`` mode — a hole in the
  artwork, but the backing plate remains solid beneath it. Rendered as
  transparency in the clean PNG.

Enclosed background in ``fill`` mode is NOT a sentinel: those pixels keep
their palette index in the label grid (so the clean PNG shows only palette
colors), and are additionally marked in ``CleanupResult.body_fill_mask``.
At geometry time the masked region belongs to the main body color, not to
the palette color it displays as.

Coordinate convention
---------------------
Label grids are image-space: row 0 = top, y grows downward.
Vector/mm space is CAD-space: y grows upward, origin at the artwork's
bottom-left. ``vectorize`` performs the flip; everything downstream of it
(geometry, swapplan, export) works purely in mm with z=0 at the print bed.

Colors are lowercase hex strings ``"#rrggbb"`` everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import MultiPolygon

TRANSPARENT = -1
HOLE_PLATE = -2

# Type alias: (H, W) int16 grid of palette indices / sentinels.
Labels = np.ndarray


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """'#e63946' -> (230, 57, 70)."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int] | np.ndarray) -> str:
    """(230, 57, 70) -> '#e63946' (lowercase)."""
    r, g, b = (int(v) for v in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass(frozen=True)
class TrimBox:
    """Half-open image-space crop rectangle: rows [top, bottom), cols
    [left, right)."""

    top: int
    left: int
    bottom: int
    right: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def shape(self) -> tuple[int, int]:
        """(H, W) of the cropped grid, comparable to ``labels.shape``."""
        return (self.height, self.width)


@dataclass
class CleanupResult:
    """Output of the quantize+cleanup stage (one preview round-trip)."""

    labels: Labels
    """Cleaned label grid: palette indices + TRANSPARENT / HOLE_PLATE."""

    body_fill_mask: np.ndarray
    """(H, W) bool. Enclosed-background pixels resolved as 'fill': they keep
    their palette index in `labels` for PNG rendering, but belong to the main
    body color at geometry time."""

    thin_mask: np.ndarray | None
    """(H, W) bool mask of regions thinner than min_feature_mm, or None if
    nothing is thin. Used only for the preview overlay."""

    islands: int
    """Number of disconnected artwork components after background clearing."""

    coverage: dict[str, float]
    """hex -> fraction of non-transparent pixels holding that palette color."""


@dataclass
class PreparedArt:
    """Output of cleanup.prepare: a cleaned grid plus the scale it was cleaned
    at. Downstream stages must use this ``mm_per_px``, not one derived from the
    source image width, because trimming changes it."""

    result: CleanupResult

    mm_per_px: float
    """Scale of ``result.labels`` — target_width_mm divided by the *trimmed*
    width when a trim happened, else by the source width."""

    trim: TrimBox | None
    """The crop applied to the quantized grid, or None when nothing was
    trimmed. Image-space coords in the source image."""


@dataclass
class VectorizedArt:
    """Output of vectorize: everything downstream is pure mm geometry.

    All polygons are valid shapely MultiPolygons in mm, y-up, origin at the
    artwork bounding box's bottom-left. Adjacent color regions share boundary
    vertices exactly (traced from the same label grid) so extrusions butt
    perfectly.
    """

    color_regions: dict[str, MultiPolygon]
    """hex -> the visible region of that palette color. Excludes body-fill
    areas (those are in body_fill_region even though the PNG shows them in a
    palette color). Colors with empty regions are omitted."""

    body_fill_region: MultiPolygon | None
    """Enclosed-background areas resolved as 'fill' — printed in the main
    body color at main-body height. None when empty."""

    plate_footprint: MultiPolygon
    """Solid footprint for the backing plate: union of all artwork,
    body-fill and HOLE_PLATE areas. Through-holes (enclosed TRANSPARENT
    regions) are preserved as polygon holes. When the artwork is multiple
    disconnected islands, the footprint is the convex hull of the art
    (minus through-holes) so the plate joins the islands into one printable
    object (FR-11)."""

    width_mm: float
    height_mm: float


@dataclass
class Part:
    """One named, colored mesh in the exported model."""

    name: str
    color: str  # "#rrggbb"
    mesh: "object"  # trimesh.Trimesh (kept untyped to avoid import cost here)


@dataclass
class SwapEvent:
    z_mm: float
    layer: int
    from_hex: str
    to_hex: str


@dataclass
class BuiltModel:
    """Output of geometry.build_model — maps 1:1 onto the /model response."""

    parts: list[Part]
    swap_plan: list[SwapEvent] = field(default_factory=list)  # empty in AMS mode
    n_swaps: int = 0
    snapped_heights: dict[str, dict] = field(default_factory=dict)
    """hex -> {"requested": float, "snapped": float, "layers": int}"""
    warnings: list[dict] = field(default_factory=list)
    """Each: {"code": str, "msg": str} — e.g. code="ams_slots"."""
