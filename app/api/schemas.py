"""Pydantic models — the API contract (see design-docs/3dsignmeup-design.md).

These mirror the endpoint payloads verbatim. Routes must accept/return these
models exactly; core modules receive them (or plain values derived from them).
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _normalize_hex(v: str) -> str:
    if not _HEX_RE.match(v):
        raise ValueError(f"not a #rrggbb hex color: {v!r}")
    return v.lower()


class PaintFill(BaseModel):
    """One paint-bucket click: flood-fill the connected label region at (x, y)
    to ``to_hex``, or erase it to a through-hole when ``to_hex`` is null.
    Image-space coords (row 0 = top)."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    to_hex: str | None = None

    @field_validator("to_hex")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_hex(v)


class ProcessParams(BaseModel):
    """Body of POST /api/sessions/{id}/preview; also half of /model."""

    target_width_mm: float = Field(gt=0, le=1000)
    palette: list[str] = Field(min_length=2, max_length=10)
    smoothness: int = Field(default=1, ge=0, le=10)
    min_feature_mm: float = Field(default=0.5, gt=0)
    clear_background: bool = True
    enclosed_background: Literal["through_hole", "plate", "fill"] = "plate"
    quantize_palette: list[str] | None = None
    """When set, pixel->label assignment quantizes against THIS palette
    (nearest-Lab-match, same length/order as `palette`) while every other
    step (cleanup, rendering, coverage, vectorize) uses `palette` for color.
    This decouples "which pixel belongs to which region" (frozen once
    quantized) from "what color does that region display as" (freely
    editable) — the "keep original color distribution" toggle. None
    (default) quantizes against `palette` directly, i.e. today's behavior."""
    paint_fills: list[PaintFill] = Field(default_factory=list)
    """Ordered paint-bucket ops applied after cleanup: each flood-fills the
    single connected component under the click to ``to_hex``, or erases it to
    a through-hole when ``to_hex`` is null."""

    @field_validator("palette", "quantize_palette")
    @classmethod
    def _hexes(cls, v: list[str] | None) -> list[str] | None:
        return [_normalize_hex(h) for h in v] if v is not None else None


class PlateParams(BaseModel):
    enabled: bool = True
    thickness_mm: float = Field(default=2.0, gt=0)


class ChamferParams(BaseModel):
    enabled: bool = True
    size_mm: float = Field(default=0.6, gt=0)


class ColorSpec(BaseModel):
    hex: str
    height_mm: float = Field(gt=0)
    first_layer_only: bool = False

    @field_validator("hex")
    @classmethod
    def _hex(cls, v: str) -> str:
        return _normalize_hex(v)


class HangParams(BaseModel):
    """Wall-hanging hardware, molded into the backing plate/body volume.

    Mutually exclusive modes (never both): "keyhole" cuts one or two
    screw-keyhole slots into the back; "fins" cuts pin-holes for separately
    printed kickstand-leg parts. Both require ``plate.enabled``; both fall
    back from 2 placements to 1 centered, then to skipped, per fit/depth —
    see app/core/hanging.py.
    """

    mode: Literal["none", "keyhole", "fins"] = "none"
    fin_angle_deg: float = Field(default=15.0, ge=0, le=45)
    """Fin lean angle from vertical (0 = fin folded flat against the back).
    Only meaningful when mode == "fins"."""


class ExtrudeParams(BaseModel):
    mode: Literal["ams", "swap"]
    layer_height_mm: float = Field(default=0.2, gt=0)
    plate: PlateParams = PlateParams()
    main_body_color: str
    colors: list[ColorSpec] = Field(min_length=1)
    initial_color_switch_mm: float = Field(default=0.4, ge=0)
    chamfer: ChamferParams = ChamferParams()
    hang: HangParams = HangParams()

    @field_validator("main_body_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        return _normalize_hex(v)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

class SessionCreated(BaseModel):
    session_id: str
    width_px: int
    height_px: int
    suggested_palette: list[str]


class PaletteRequest(BaseModel):
    n_colors: int = Field(ge=2, le=10)


class PaletteResponse(BaseModel):
    palette: list[str]


class PreviewResponse(BaseModel):
    preview_png: str
    thin_overlay_png: str | None
    islands: int
    color_coverage: dict[str, float]
    color_perimeter: dict[str, float] = {}
    """hex -> boundary-pixel count. Ranks 'perimeter presence' for the
    equal-height sibling tiebreak so the client preview matches the server
    plan (geometry uses region perimeter; this is its raster equivalent)."""
    params_hash: str


class ModelRequest(BaseModel):
    process: ProcessParams
    extrude: ExtrudeParams


class PartInfo(BaseModel):
    name: str
    color: str
    triangles: int


class SwapEventOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    z_mm: float
    layer: int
    from_hex: str = Field(alias="from", serialization_alias="from")
    to_hex: str = Field(alias="to", serialization_alias="to")


class SnappedHeight(BaseModel):
    requested: float
    snapped: float
    layers: int


class WarningOut(BaseModel):
    code: str
    msg: str


class Downloads(BaseModel):
    threemf: str
    png: str
    zip: str
    fins_threemf: str | None = None


class ModelResponse(BaseModel):
    model_id: str
    parts: list[PartInfo]
    swap_plan: list[SwapEventOut]
    n_swaps: int
    snapped_heights: dict[str, SnappedHeight]
    warnings: list[WarningOut]
    preview_glb: str
    downloads: Downloads
