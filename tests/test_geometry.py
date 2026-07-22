"""Tests for solid construction (build_model)."""
from __future__ import annotations

import pytest
from shapely.geometry import MultiPolygon, box

from app.api.schemas import (
    ChamferParams,
    ColorSpec,
    ExtrudeParams,
    HangParams,
    PlateParams,
)
from app.core.geometry import build_model
from app.core.types import VectorizedArt

R = "#ff0000"
G = "#00ff00"
B = "#0000ff"
W = "#ffffff"


def _mp(*polys) -> MultiPolygon:
    return MultiPolygon([p for p in polys])


def _art(color_regions, *, body_fill=None, plate=None, w=20.0, h=30.0):
    if plate is None:
        # union of everything as the plate footprint
        geoms = list(color_regions.values())
        if body_fill is not None:
            geoms.append(body_fill)
        from shapely.ops import unary_union

        plate = unary_union(geoms)
        if not isinstance(plate, MultiPolygon):
            plate = MultiPolygon([plate])
    return VectorizedArt(
        color_regions=color_regions,
        body_fill_region=body_fill,
        plate_footprint=plate,
        width_mm=w,
        height_mm=h,
    )


def _extrude(mode="swap", colors=None, body=R, lead=2.0, lh=0.2, flh=0.2,
             plate=False, plate_th=1.2, chamfer=False, chamfer_size=0.6,
             hang=None):
    return ExtrudeParams(
        mode=mode,
        layer_height_mm=lh,
        first_layer_height_mm=flh,
        plate=PlateParams(enabled=plate, thickness_mm=plate_th),
        main_body_color=body,
        colors=colors or [],
        initial_color_switch_mm=lead,
        chamfer=ChamferParams(enabled=chamfer, size_mm=chamfer_size),
        hang=hang or HangParams(),
    )


def _all_watertight(model):
    assert model.parts, "expected at least one part"
    for p in model.parts:
        assert p.mesh.is_watertight, f"{p.name} not watertight"


# --------------------------------------------------------------------------
# simple single-color box: volume must match analytic
# --------------------------------------------------------------------------
def test_single_box_volume_and_watertight():
    region = box(0, 0, 20, 30)  # area 600
    art = _art({R: _mp(region)})
    ex = _extrude(mode="swap", colors=[ColorSpec(hex=R, height_mm=6.0)], body=R)
    model = build_model(art, ex)
    _all_watertight(model)
    total_vol = sum(p.mesh.volume for p in model.parts)
    assert total_vol == pytest.approx(600 * 6.0, rel=1e-3)
    assert model.n_swaps == 0


def test_single_box_with_plate_volume():
    region = box(0, 0, 20, 30)
    art = _art({R: _mp(region)})
    ex = _extrude(
        mode="swap", colors=[ColorSpec(hex=R, height_mm=6.0)], body=R,
        plate=True, plate_th=1.2,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    # plate top snaps to 1.2; color top = 1.2 + 6.0 = 7.2. Body==color -> one solid.
    total_vol = sum(p.mesh.volume for p in model.parts)
    assert total_vol == pytest.approx(600 * 7.2, rel=1e-3)


# --------------------------------------------------------------------------
# swap-mode band z-extents
# --------------------------------------------------------------------------
def test_swap_band_z_extents():
    # Three stacked colors covering the same footprint; body = R.
    region = box(0, 0, 20, 30)
    art = _art({R: _mp(region), G: _mp(region), B: _mp(region)})
    ex = _extrude(
        mode="swap",
        colors=[
            ColorSpec(hex=R, height_mm=10.0),
            ColorSpec(hex=G, height_mm=5.0),
            ColorSpec(hex=B, height_mm=15.0),
        ],
        body=R,
        lead=2.0,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    # Expected bands: R[0,3) G[3,5) R[5,10) B[10,15]
    events = [(e.z_mm, e.from_hex, e.to_hex) for e in model.swap_plan]
    assert events == [(3.0, R, G), (5.0, G, R), (10.0, R, B)]
    assert model.n_swaps == 3

    # Each band is one part; check z-extents from mesh bounds.
    by_name = {p.name: p for p in model.parts}
    # G band spans [3,5)
    g_parts = [p for p in model.parts if p.color == G]
    assert len(g_parts) == 1
    gb = g_parts[0].mesh.bounds
    assert gb[0][2] == pytest.approx(3.0, abs=1e-6)
    assert gb[1][2] == pytest.approx(5.0, abs=1e-6)
    # B band spans [10,15)
    b_parts = [p for p in model.parts if p.color == B]
    assert b_parts[0].mesh.bounds[0][2] == pytest.approx(10.0, abs=1e-6)
    assert b_parts[0].mesh.bounds[1][2] == pytest.approx(15.0, abs=1e-6)

    # Total volume = footprint area * tallest top (contiguous solid column).
    total_vol = sum(p.mesh.volume for p in model.parts)
    assert total_vol == pytest.approx(600 * 15.0, rel=1e-3)


# --------------------------------------------------------------------------
# AMS caps exactly 3 layers thick
# --------------------------------------------------------------------------
def test_ams_cap_thickness():
    region = box(0, 0, 20, 30)
    art = _art({R: _mp(region), G: _mp(region)})
    ex = _extrude(
        mode="ams",
        colors=[
            ColorSpec(hex=R, height_mm=6.0),
            ColorSpec(hex=G, height_mm=6.0),
        ],
        body=R,
        lh=0.2,
        flh=0.2,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    caps = [p for p in model.parts if p.name.startswith("cap_")]
    # Only G gets a cap (R == body).
    assert len(caps) == 1
    cap = caps[0]
    assert cap.color == G
    zext = cap.mesh.bounds[1][2] - cap.mesh.bounds[0][2]
    assert zext == pytest.approx(3 * 0.2, abs=1e-6)
    # cap top at G's top (6.0), bottom at 6.0 - 0.6 = 5.4.
    assert cap.mesh.bounds[1][2] == pytest.approx(6.0, abs=1e-6)
    assert cap.mesh.bounds[0][2] == pytest.approx(5.4, abs=1e-6)


def test_ams_body_and_cap_volume():
    region = box(0, 0, 20, 30)  # area 600
    art = _art({R: _mp(region), G: _mp(region)})
    ex = _extrude(
        mode="ams",
        colors=[ColorSpec(hex=R, height_mm=6.0), ColorSpec(hex=G, height_mm=6.0)],
        body=R,
    )
    model = build_model(art, ex)
    total_vol = sum(p.mesh.volume for p in model.parts)
    assert total_vol == pytest.approx(600 * 6.0, rel=1e-3)


# --------------------------------------------------------------------------
# chamfer reduces bottom face area
# --------------------------------------------------------------------------
def _bottom_area(mesh, z0=0.0, tol=1e-4):
    import numpy as np

    tris = mesh.vertices[mesh.faces]
    area = 0.0
    for t in tris:
        if np.all(np.abs(t[:, 2] - z0) < tol):
            a, b, c = t
            area += 0.5 * abs(np.cross(b - a, c - a)[2])
    return area


def test_chamfer_reduces_bottom_area():
    region = box(0, 0, 20, 30)
    art = _art({R: _mp(region)})
    base = _extrude(
        mode="swap", colors=[ColorSpec(hex=R, height_mm=6.0)], body=R,
        plate=True, plate_th=1.2, chamfer=False,
    )
    cham = _extrude(
        mode="swap", colors=[ColorSpec(hex=R, height_mm=6.0)], body=R,
        plate=True, plate_th=1.2, chamfer=True, chamfer_size=0.6,
    )
    m0 = build_model(art, base)
    m1 = build_model(art, cham)
    _all_watertight(m0)
    _all_watertight(m1)
    a0 = sum(_bottom_area(p.mesh) for p in m0.parts)
    a1 = sum(_bottom_area(p.mesh) for p in m1.parts)
    assert a1 < a0
    # chamfered bottom ~ buffer(-0.6) of a 20x30 box = 18.8x28.8 = 541.44
    assert a1 == pytest.approx((20 - 1.2) * (30 - 1.2), rel=1e-2)


# --------------------------------------------------------------------------
# first-layer-only region
# --------------------------------------------------------------------------
def test_first_layer_only_slab_swap():
    region = box(0, 0, 20, 30)
    inner = box(5, 5, 15, 25)  # FLO region sits inside the body footprint
    art = _art({R: _mp(region), W: _mp(inner)})
    ex = _extrude(
        mode="swap",
        colors=[
            ColorSpec(hex=R, height_mm=6.0),
            ColorSpec(hex=W, height_mm=1.0, first_layer_only=True),
        ],
        body=R,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    first_parts = [p for p in model.parts if p.name.startswith("first_")]
    assert len(first_parts) == 1
    fp = first_parts[0]
    assert fp.color == W
    # slab only in the first layer [0, 0.2).
    assert fp.mesh.bounds[1][2] == pytest.approx(0.2, abs=1e-6)


def test_first_layer_only_ams():
    region = box(0, 0, 20, 30)
    inner = box(5, 5, 15, 25)
    art = _art({R: _mp(region), W: _mp(inner)})
    ex = _extrude(
        mode="ams",
        colors=[
            ColorSpec(hex=R, height_mm=6.0),
            ColorSpec(hex=W, height_mm=1.0, first_layer_only=True),
        ],
        body=R,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    first_parts = [p for p in model.parts if p.name.startswith("first_")]
    assert len(first_parts) == 1
    assert first_parts[0].mesh.bounds[1][2] == pytest.approx(0.2, abs=1e-6)


# --------------------------------------------------------------------------
# equal-height sibling parts
# --------------------------------------------------------------------------
def test_equal_height_sibling_parts():
    # G and B share the same footprint region but different areas; both top 5.
    left = box(0, 0, 8, 30)
    right = box(8, 0, 20, 30)
    art = _art({R: _mp(box(0, 0, 20, 30)), G: _mp(left), B: _mp(right)})
    ex = _extrude(
        mode="swap",
        colors=[
            ColorSpec(hex=G, height_mm=5.0),
            ColorSpec(hex=B, height_mm=5.0),
            ColorSpec(hex=R, height_mm=10.0),
        ],
        body=R,
    )
    model = build_model(art, ex)
    _all_watertight(model)
    # Both G and B appear as parts.
    colors = {p.color for p in model.parts}
    assert G in colors and B in colors
    assert model.n_swaps == len(model.swap_plan) + 1


# --------------------------------------------------------------------------
# no-plate multi-island warning
# --------------------------------------------------------------------------
def test_no_plate_islands_warning():
    left = box(0, 0, 8, 30)
    right = box(12, 0, 20, 30)  # disconnected
    art = _art({R: _mp(left, right)})
    ex = _extrude(mode="swap", colors=[ColorSpec(hex=R, height_mm=6.0)], body=R, plate=False)
    model = build_model(art, ex)
    assert any(w["code"] == "no_plate_islands" for w in model.warnings)


# --------------------------------------------------------------------------
# wall-hanging hardware end-to-end (keyhole / fins)
# --------------------------------------------------------------------------
def _hang_art():
    # Big plate with a small foreground color island in the center; the
    # keyhole/fin anchors (~0.125w / 0.875w) fall over pure body/plate.
    outer = box(0, 0, 100, 100)
    inner = box(45, 45, 55, 55)
    return _art({R: _mp(outer), G: _mp(inner)}, plate=_mp(outer), w=100.0, h=100.0)


def _hang_extrude(mode):
    return _extrude(
        mode="swap",
        colors=[ColorSpec(hex=R, height_mm=10.0), ColorSpec(hex=G, height_mm=5.0)],
        body=R,
        plate=True,
        plate_th=1.2,
        hang=HangParams(mode=mode, fin_angle_deg=15.0),
    )


def test_build_model_keyhole_carves_without_new_parts():
    art = _hang_art()
    base = build_model(art, _hang_extrude("none"))
    key = build_model(art, _hang_extrude("keyhole"))
    _all_watertight(base)
    _all_watertight(key)
    # keyhole cuts material from existing band parts; part count is unchanged.
    assert len(key.parts) == len(base.parts)
    assert {p.name for p in key.parts} == {p.name for p in base.parts}
    # total volume strictly decreases (two 8mm-deep cavities removed).
    assert sum(p.mesh.volume for p in key.parts) < sum(p.mesh.volume for p in base.parts)
    # swap-mode plan is untouched by hanging.
    assert key.n_swaps == base.n_swaps


def test_build_model_fins_add_parts_outside_bbox():
    art = _hang_art()
    base = build_model(art, _hang_extrude("none"))
    fins = build_model(art, _hang_extrude("fins"))
    _all_watertight(base)
    _all_watertight(fins)
    # two separate fin objects on top of the usual swap-mode band parts.
    fin_parts = [p for p in fins.parts if p.name.startswith("fin_")]
    assert len(fin_parts) == 2
    assert len(fins.parts) == len(base.parts) + 2
    # the pin-holes also carve the sign body -> sign volume drops a little.
    sign_base = sum(p.mesh.volume for p in base.parts)
    sign_fins = sum(
        p.mesh.volume for p in fins.parts if not p.name.startswith("fin_")
    )
    assert sign_fins < sign_base
    # every fin sits entirely outside the sign's XY bounding box.
    sign_max_x = max(
        p.mesh.bounds[1][0] for p in fins.parts if not p.name.startswith("fin_")
    )
    for p in fin_parts:
        assert p.mesh.bounds[0][0] >= sign_max_x - 1e-6
        assert p.color == R


def test_perf_under_budget():
    import time

    # a moderately detailed footprint (a many-vertex approx circle)
    from shapely.geometry import Point

    region = Point(10, 15).buffer(10, quad_segs=64)
    art = _art({R: MultiPolygon([region])})
    ex = _extrude(
        mode="swap",
        colors=[ColorSpec(hex=R, height_mm=10.0), ColorSpec(hex=G, height_mm=5.0)],
        body=R,
        plate=True,
        chamfer=True,
    )
    art = _art({R: MultiPolygon([region]), G: MultiPolygon([region.buffer(-3)])})
    t = time.time()
    model = build_model(art, ex)
    assert time.time() - t < 10.0
    _all_watertight(model)
