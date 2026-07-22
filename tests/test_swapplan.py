"""Tests for layer snapping and swap-mode planning."""
from __future__ import annotations

import pytest

from app.api.schemas import (
    ChamferParams,
    ColorSpec,
    ExtrudeParams,
    PlateParams,
)
from app.core.swapplan import compute_plan, snap_z


def _extrude(mode="swap", colors=None, body="#ff0000", lead=2.0,
             lh=0.2, flh=0.2, plate=False, plate_th=1.2):
    return ExtrudeParams(
        mode=mode,
        layer_height_mm=lh,
        first_layer_height_mm=flh,
        plate=PlateParams(enabled=plate, thickness_mm=plate_th),
        main_body_color=body,
        colors=colors or [],
        initial_color_switch_mm=lead,
        chamfer=ChamferParams(enabled=False, size_mm=0.6),
    )


# --------------------------------------------------------------------------
# snap_z
# --------------------------------------------------------------------------
def test_snap_z_exact():
    assert snap_z(1.0, layer_height_mm=0.2, first_layer_height_mm=0.2) == (1.0, 5)


def test_snap_z_min_one_layer():
    # Anything below/at the first layer snaps to exactly one layer.
    assert snap_z(0.05, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.2, 1)
    assert snap_z(0.0, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.2, 1)
    assert snap_z(-5.0, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.2, 1)


def test_snap_z_tie_rounds_up():
    # 0.3 is exactly halfway between 0.2 and 0.4 -> rounds up to 0.4.
    assert snap_z(0.3, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.4, 2)
    # 0.5 halfway between 0.4 and 0.6 -> 0.6.
    assert snap_z(0.5, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.6, 3)


def test_snap_z_nearest():
    assert snap_z(0.28, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.2, 1)
    assert snap_z(0.33, layer_height_mm=0.2, first_layer_height_mm=0.2) == (0.4, 2)


def test_snap_z_different_first_layer():
    # first layer 0.3, then 0.2 each.
    assert snap_z(0.3, layer_height_mm=0.2, first_layer_height_mm=0.3) == (0.3, 1)
    assert snap_z(0.5, layer_height_mm=0.2, first_layer_height_mm=0.3) == (0.5, 2)


# --------------------------------------------------------------------------
# reference R/G/B plan (PRD FR-16)
# --------------------------------------------------------------------------
def test_reference_rgb_plan():
    R, G, B = "#ff0000", "#00ff00", "#0000ff"
    colors = [
        ColorSpec(hex=R, height_mm=10.0),
        ColorSpec(hex=G, height_mm=5.0),
        ColorSpec(hex=B, height_mm=15.0),
    ]
    plan = compute_plan(_extrude(colors=colors, body=R, lead=2.0), colors)

    bands = [(b.z0_mm, b.z1_mm, b.hex) for b in plan.bands]
    assert bands == [
        (0.0, 3.0, R),
        (3.0, 5.0, G),
        (5.0, 10.0, R),
        (10.0, 15.0, B),
    ]

    events = [(e.z_mm, e.from_hex, e.to_hex) for e in plan.events]
    assert events == [
        (3.0, R, G),
        (5.0, G, R),
        (10.0, R, B),
    ]
    assert plan.n_swaps == 3


def test_first_color_equals_body_no_initial_swap():
    # Body color is the shortest visible color -> no lead / initial swap for it.
    R, G = "#ff0000", "#00ff00"
    colors = [
        ColorSpec(hex=R, height_mm=3.0),
        ColorSpec(hex=G, height_mm=6.0),
    ]
    plan = compute_plan(_extrude(colors=colors, body=R, lead=2.0), colors)
    bands = [(b.z0_mm, b.z1_mm, b.hex) for b in plan.bands]
    # body up to G's lead point, then G. R's own top is absorbed in the body band.
    assert bands[0][2] == R
    assert bands[0][0] == 0.0
    assert bands[-1] == (bands[-1][0], 6.0, G)
    # Only one swap (body -> G); no swap for the body color itself.
    assert len(plan.events) == 1
    assert plan.events[0].from_hex == R and plan.events[0].to_hex == G
    assert plan.n_swaps == 1


def test_equal_heights_are_siblings_extra_swap():
    R, G, B = "#ff0000", "#00ff00", "#0000ff"
    colors = [
        ColorSpec(hex=G, height_mm=5.0),
        ColorSpec(hex=B, height_mm=5.0),  # equal to G
        ColorSpec(hex=R, height_mm=10.0),
    ]
    # Pass G first -> dominant of the equal-top group.
    plan = compute_plan(_extrude(colors=colors, body=R, lead=2.0), colors)
    # One band ends at the shared top (5.0) with the dominant color.
    tops = [b.z1_mm for b in plan.bands]
    assert 5.0 in tops
    # sibling adds one extra swap beyond the band events.
    assert plan.n_swaps == len(plan.events) + 1


def test_first_layer_only_swap_count():
    R, G, W = "#ff0000", "#00ff00", "#ffffff"
    colors = [
        ColorSpec(hex=R, height_mm=6.0),
        ColorSpec(hex=G, height_mm=3.0),
        ColorSpec(hex=W, height_mm=1.0, first_layer_only=True),
    ]
    plan = compute_plan(_extrude(colors=colors, body=R, lead=2.0), colors)
    # FLO color excluded from ascending-top ordering (no band ends at its top).
    # It adds (k+1) swaps for k=1 non-body FLO color -> +2.
    assert plan.n_swaps == len(plan.events) + 2


# --------------------------------------------------------------------------
# snapped heights & plate
# --------------------------------------------------------------------------
def test_snapped_heights_reported():
    G = "#00ff00"
    colors = [ColorSpec(hex=G, height_mm=3.0)]
    plan = compute_plan(_extrude(colors=colors, body="#ff0000"), colors)
    sh = plan.snapped_heights[G]
    assert sh["requested"] == 3.0
    assert sh["snapped"] == 3.0
    assert sh["layers"] == 15


def test_plate_snaps_total_once():
    G = "#00ff00"
    colors = [ColorSpec(hex=G, height_mm=3.0)]
    plan = compute_plan(
        _extrude(colors=colors, body="#ff0000", plate=True, plate_th=1.2), colors
    )
    # plate top snaps to 1.2 (6 layers); color top = snap(1.2+3.0)=4.2; rel=3.0.
    assert plan.snapped_heights[G]["snapped"] == pytest.approx(3.0)


# --------------------------------------------------------------------------
# AMS mode
# --------------------------------------------------------------------------
def test_ams_no_bands_slot_warning():
    colors = [
        ColorSpec(hex="#111111", height_mm=2.0),
        ColorSpec(hex="#222222", height_mm=2.0),
        ColorSpec(hex="#333333", height_mm=2.0),
        ColorSpec(hex="#444444", height_mm=2.0),
    ]
    plan = compute_plan(
        _extrude(mode="ams", colors=colors, body="#ff0000"), colors
    )
    assert plan.bands == []
    assert plan.events == []
    # 4 colors + distinct body = 5 filaments > 4 slots.
    assert any(w["code"] == "ams_slots" for w in plan.warnings)


def test_ams_within_slots_no_warning():
    colors = [
        ColorSpec(hex="#111111", height_mm=2.0),
        ColorSpec(hex="#222222", height_mm=2.0),
    ]
    plan = compute_plan(
        _extrude(mode="ams", colors=colors, body="#111111"), colors
    )
    assert not any(w["code"] == "ams_slots" for w in plan.warnings)


def test_lead_clamped_warning():
    # Lead larger than the color's height forces a clamp.
    G = "#00ff00"
    colors = [ColorSpec(hex=G, height_mm=1.0)]
    plan = compute_plan(
        _extrude(colors=colors, body="#ff0000", lead=5.0), colors
    )
    assert any(w["code"] == "lead_clamped" for w in plan.warnings)
