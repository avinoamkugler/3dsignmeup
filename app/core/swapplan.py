"""Layer snapping and swap-mode color planning (FR-10, FR-16, FR-17).

See module docstring history in the design docs. Public API:

snap_z(z_mm, *, layer_height_mm, first_layer_height_mm) -> (snapped_z, layers)
color_top(height_mm, *, plate_top, plate_enabled, layer_height_mm,
          first_layer_height_mm) -> (top_z, layers, snapped_rel)
compute_plan(extrude, visible_colors) -> Plan
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.api.schemas import ColorSpec, ExtrudeParams
from app.core.types import SwapEvent

_EPS = 1e-9


@dataclass
class Band:
    z0_mm: float
    z1_mm: float
    hex: str


@dataclass
class Plan:
    bands: list[Band] = field(default_factory=list)
    events: list[SwapEvent] = field(default_factory=list)
    snapped_heights: dict[str, dict] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    n_swaps: int = 0


def snap_z(z_mm: float, *, layer_height_mm: float, first_layer_height_mm: float) -> tuple[float, int]:
    """Snap ``z_mm`` to the nearest valid ``first_layer_height + n*layer_height``.

    Minimum 1 layer (never below the first layer). Ties round up. Returns
    ``(snapped_z, layers)`` where ``layers`` counts the first layer as 1.
    """
    raw = (z_mm - first_layer_height_mm) / layer_height_mm
    # round half up, with a tiny epsilon to absorb float error at exact ties.
    n = math.floor(raw + 0.5 + _EPS)
    if n < 0:
        n = 0
    snapped = round(first_layer_height_mm + n * layer_height_mm, 9)
    return snapped, n + 1


def color_top(
    height_mm: float,
    *,
    plate_top: float,
    plate_enabled: bool,
    layer_height_mm: float,
    first_layer_height_mm: float,
) -> tuple[float, int, float]:
    """Absolute snapped top Z of a color, its layer count, and its snapped
    height relative to the plate top.

    With a plate the total ``plate_thickness + height`` is snapped once (avoids
    double rounding); the reported relative height is ``top - plate_top`` and is
    clamped to at least one layer above the plate.
    """
    if plate_enabled:
        total, layers = snap_z(
            plate_top + height_mm,
            layer_height_mm=layer_height_mm,
            first_layer_height_mm=first_layer_height_mm,
        )
        if total <= plate_top + _EPS:
            total = round(plate_top + layer_height_mm, 9)
            _, layers = snap_z(
                total,
                layer_height_mm=layer_height_mm,
                first_layer_height_mm=first_layer_height_mm,
            )
        snapped_rel = round(total - plate_top, 9)
    else:
        total, layers = snap_z(
            height_mm,
            layer_height_mm=layer_height_mm,
            first_layer_height_mm=first_layer_height_mm,
        )
        snapped_rel = total
    return total, layers, snapped_rel


def compute_plan(extrude: ExtrudeParams, visible_colors: list[ColorSpec]) -> Plan:
    """Snap heights and (in swap mode) compute Z-bands + filament-change events.

    ``visible_colors`` must be pre-ordered so that, among colors that snap to the
    same top Z, the one with more perimeter presence comes first (it becomes the
    band's dominant filament). Order between distinct tops is irrelevant.
    """
    lh = extrude.layer_height_mm
    flh = extrude.first_layer_height_mm
    body = extrude.main_body_color
    plate_enabled = extrude.plate.enabled

    plan = Plan()

    plate_top = 0.0
    if plate_enabled:
        plate_top, _ = snap_z(
            extrude.plate.thickness_mm, layer_height_mm=lh, first_layer_height_mm=flh
        )

    # --- snapped heights + absolute tops for every visible color -----------
    tops: dict[str, float] = {}
    for c in visible_colors:
        top, layers, snapped_rel = color_top(
            c.height_mm,
            plate_top=plate_top,
            plate_enabled=plate_enabled,
            layer_height_mm=lh,
            first_layer_height_mm=flh,
        )
        plan.snapped_heights[c.hex] = {
            "requested": c.height_mm,
            "snapped": snapped_rel,
            "layers": layers,
        }
        if not c.first_layer_only:
            tops[c.hex] = top

    # --- AMS-slot warning (applies in both modes) --------------------------
    filaments = {c.hex for c in visible_colors} | {body}
    if len(filaments) > 4:
        plan.warnings.append(
            {
                "code": "ams_slots",
                "msg": f"{len(filaments)} colors incl. body exceeds AMS lite's 4 slots",
            }
        )

    if extrude.mode == "ams":
        return plan

    # ----------------------------- swap mode -------------------------------
    nonflo = [c for c in visible_colors if not c.first_layer_only]
    flo = [c for c in visible_colors if c.first_layer_only]

    # Sort by ascending top; equal tops keep input order (perimeter tiebreak).
    order = sorted(range(len(nonflo)), key=lambda i: (tops[nonflo[i].hex], i))
    sorted_nc = [nonflo[i] for i in order]

    # Group colors sharing a snapped top (siblings).
    groups: list[list[ColorSpec]] = []
    for c in sorted_nc:
        if groups and abs(tops[groups[-1][0].hex] - tops[c.hex]) < 1e-6:
            groups[-1].append(c)
        else:
            groups.append([c])

    bands: list[Band] = []
    if not groups:
        # Only first-layer-only colors (or none): everything is body.
        top_end = max([plate_top, flh])
        bands.append(Band(0.0, top_end, body))
    else:
        first_foreign = next(
            (gi for gi, g in enumerate(groups) if g[0].hex != body), None
        )
        prev = 0.0
        if first_foreign is None:
            # All non-flo colors print in body color: one body band.
            bands.append(Band(0.0, tops[groups[-1][0].hex], body))
        else:
            t_f = tops[groups[first_foreign][0].hex]
            lead = extrude.initial_color_switch_mm
            swap1, _ = snap_z(t_f - lead, layer_height_mm=lh, first_layer_height_mm=flh)
            clamped = False
            if t_f - lead < flh - _EPS or swap1 < flh - _EPS:
                swap1 = flh
                clamped = True
            if swap1 >= t_f - _EPS:
                swap1 = max(flh, round(t_f - lh, 9))
                clamped = True
            if clamped:
                plan.warnings.append(
                    {
                        "code": "lead_clamped",
                        "msg": "initial color-switch lead clamped to fit within the model",
                    }
                )
            bands.append(Band(0.0, swap1, body))
            prev = swap1
            for gi in range(first_foreign, len(groups)):
                dom = groups[gi][0].hex
                t = tops[groups[gi][0].hex]
                bands.append(Band(prev, t, dom))
                prev = t

    # Merge consecutive same-color bands (no swap between them).
    merged: list[Band] = []
    for b in bands:
        if merged and merged[-1].hex == b.hex:
            merged[-1].z1_mm = b.z1_mm
        else:
            merged.append(Band(b.z0_mm, b.z1_mm, b.hex))
    plan.bands = merged

    for i in range(1, len(merged)):
        z = merged[i].z0_mm
        _, layer = snap_z(z, layer_height_mm=lh, first_layer_height_mm=flh)
        plan.events.append(
            SwapEvent(
                z_mm=z,
                layer=layer,
                from_hex=merged[i - 1].hex,
                to_hex=merged[i].hex,
            )
        )

    # Extra swaps not expressible as a single from->to band boundary:
    sibling_extra = sum(len(g) - 1 for g in groups)
    flo_nonbody = {c.hex for c in flo if c.hex != body}
    flo_extra = (len(flo_nonbody) + 1) if flo_nonbody else 0

    plan.n_swaps = len(plan.events) + sibling_extra + flo_extra
    return plan
