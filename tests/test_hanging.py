"""Unit tests for app.core.hanging.plan_hangs (in isolation, no geometry.py).

Fixtures are hand-built VectorizedArt with shapely boxes; the depth-availability
inputs (tops / regions / model_top) are passed directly, exactly as build_model
would compute them.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, box

from app.api.schemas import (
    ColorSpec,
    ExtrudeParams,
    HangParams,
    PlateParams,
)
from app.core import hanging
from app.core.hanging import plan_hangs
from app.core.types import VectorizedArt

R = "#ff0000"  # body color
G = "#00ff00"  # a non-body foreground color

DEEP = 30.0  # a generous model_top so depth never limits placement


def _mp(*polys) -> MultiPolygon:
    return MultiPolygon(list(polys))


def _art(w, h, regions=None, plate=None) -> VectorizedArt:
    if regions is None:
        regions = {R: _mp(box(0, 0, w, h))}
    if plate is None:
        plate = _mp(box(0, 0, w, h))
    return VectorizedArt(
        color_regions=regions,
        body_fill_region=None,
        plate_footprint=plate,
        width_mm=w,
        height_mm=h,
    )


def _extrude(mode="keyhole", angle=15.0, plate=True) -> ExtrudeParams:
    return ExtrudeParams(
        mode="swap",
        plate=PlateParams(enabled=plate, thickness_mm=1.2),
        main_body_color=R,
        colors=[ColorSpec(hex=R, height_mm=6.0)],
        hang=HangParams(mode=mode, fin_angle_deg=angle),
    )


def _plan(art, extrude, *, tops, regions=None, model_top=DEEP, plate_enabled=True):
    return plan_hangs(
        art,
        extrude,
        tops=tops,
        regions=regions if regions is not None else art.color_regions,
        body=R,
        plate_top=1.2,
        plate_enabled=plate_enabled,
        model_top=model_top,
    )


def _centroid_xs(cuts):
    return sorted({round(c.negative.centroid.x, 2) for c in cuts})


# --------------------------------------------------------------------------
# mode == none / plate disabled
# --------------------------------------------------------------------------
def test_mode_none_is_empty():
    art = _art(100, 100)
    plan = _plan(art, _extrude("none"), tops={R: DEEP})
    assert plan.cuts == []
    assert plan.extra_parts == []
    assert plan.warnings == []


def test_plate_disabled_warns_only():
    art = _art(100, 100)
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP}, plate_enabled=False)
    assert plan.cuts == []
    assert plan.extra_parts == []
    assert len(plan.warnings) == 1
    assert plan.warnings[0]["code"] == "hang_requires_plate"


def test_plate_disabled_fins_warns_only():
    art = _art(100, 100)
    plan = _plan(art, _extrude("fins"), tops={R: DEEP}, plate_enabled=False)
    assert [w["code"] for w in plan.warnings] == ["hang_requires_plate"]


# --------------------------------------------------------------------------
# keyhole: two-sided placement, orientation
# --------------------------------------------------------------------------
def test_keyhole_two_sided_placement():
    art = _art(100, 100)
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP})
    assert plan.warnings == []
    # Both anchors: 0.125*w and mirror; centered at 0.75*h.
    assert _centroid_xs(plan.cuts) == [12.5, 87.5]
    for c in plan.cuts:
        # every cut's footprint is centered vertically on 0.75 * height
        y0, y1 = c.negative.bounds[1], c.negative.bounds[3]
        assert 0.5 * (y0 + y1) == pytest.approx(75.0, abs=1.5)
    # cut stack spans the full total depth from the bed
    z0 = min(c.z0_mm for c in plan.cuts)
    z1 = max(c.z1_mm for c in plan.cuts)
    assert z0 == pytest.approx(0.0)
    assert z1 == pytest.approx(hanging.KEYHOLE_TOTAL_DEPTH_MM)


def test_keyhole_bulb_is_at_bottom():
    art = _art(100, 100)
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP})
    # The true layer-1 silhouette slice is [FILLET, LAYER1_DEPTH].
    layer1_cuts = [
        c
        for c in plan.cuts
        if math.isclose(c.z0_mm, hanging.KEYHOLE_FILLET_MM, abs_tol=1e-6)
        and math.isclose(c.z1_mm, hanging.KEYHOLE_LAYER1_DEPTH_MM, abs_tol=1e-6)
    ]
    assert layer1_cuts, "expected a true layer-1 slice"
    for c in layer1_cuts:
        prof = c.negative
        cy = prof.centroid.y
        x0, y0, x1, y1 = prof.bounds
        top = prof.intersection(box(x0, cy, x1, y1))
        bot = prof.intersection(box(x0, y0, x1, cy))
        # bulb (wide circle) sits in the lower half: more area AND wider there.
        assert bot.area > top.area
        top_w = top.bounds[2] - top.bounds[0]
        bot_w = bot.bounds[2] - bot.bounds[0]
        assert bot_w > top_w
        assert bot_w == pytest.approx(hanging.KEYHOLE_BULB_DIAM_MM, abs=0.1)


# --------------------------------------------------------------------------
# keyhole: fallback cascade
# --------------------------------------------------------------------------
def test_keyhole_fallback_single_narrow_sign():
    # Narrow: two footprints can't both fit horizontally, one centered can.
    art = _art(28, 100)
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP})
    assert [w["code"] for w in plan.warnings] == ["hang_fallback_single"]
    assert _centroid_xs(plan.cuts) == [14.0]  # centered


def test_keyhole_skipped_too_small():
    # Even one keyhole (10x17 + 3mm margins) can't fit.
    art = _art(12, 12)
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP})
    assert plan.cuts == []
    assert [w["code"] for w in plan.warnings] == ["hang_skipped"]
    assert "fit" in plan.warnings[0]["msg"]


def test_keyhole_skipped_insufficient_depth():
    # Plenty of room in XY, but thinner than the adaptive floor + safety.
    art = _art(100, 100)
    shallow = hanging.KEYHOLE_MIN_TOTAL_DEPTH_MM  # < min cavity + SAFETY_MARGIN
    plan = _plan(art, _extrude("keyhole"), tops={}, regions={}, model_top=shallow)
    assert plan.cuts == []
    assert [w["code"] for w in plan.warnings] == ["hang_skipped"]
    assert "depth" in plan.warnings[0]["msg"]


def test_keyhole_min_depth_matches_placement_need():
    assert hanging.keyhole_min_depth_mm() == pytest.approx(
        hanging.KEYHOLE_MIN_TOTAL_DEPTH_MM + hanging.SAFETY_MARGIN_MM
    )
    assert hanging.keyhole_nominal_depth_mm() == pytest.approx(
        hanging.KEYHOLE_TOTAL_DEPTH_MM + hanging.SAFETY_MARGIN_MM
    )
    assert hanging.fins_min_depth_mm() == pytest.approx(
        hanging.FIN_TAB_MIN_REACH_MM + hanging.FIN_TAB_CLEARANCE_MM + hanging.FIN_MARGIN_MM
    )
    assert hanging.fins_nominal_depth_mm() == pytest.approx(
        hanging.FIN_TAB_REACH_MM + hanging.FIN_TAB_CLEARANCE_MM + hanging.FIN_MARGIN_MM
    )
    assert hanging.hang_min_depth_mm("keyhole") == hanging.keyhole_min_depth_mm()
    assert hanging.hang_min_depth_mm("fins") == hanging.fins_min_depth_mm()
    assert hanging.hang_min_depth_mm("none") is None


def test_keyhole_adapts_depth_to_thin_model():
    art = _art(100, 100)
    # Ceiling between adaptive floor and nominal: usable = 3.0mm cavity.
    ceiling = hanging.KEYHOLE_MIN_TOTAL_DEPTH_MM + hanging.SAFETY_MARGIN_MM + 0.5
    plan = _plan(art, _extrude("keyhole"), tops={}, regions={}, model_top=ceiling)
    assert plan.cuts
    z1 = max(c.z1_mm for c in plan.cuts)
    assert z1 == pytest.approx(ceiling - hanging.SAFETY_MARGIN_MM)
    assert z1 < hanging.KEYHOLE_TOTAL_DEPTH_MM
    assert any(w["code"] == "hang_depth_adapted" for w in plan.warnings)


def test_keyhole_depth_excludes_overlapping_color_region():
    # A thin non-body color (top=3mm) sits over the LEFT footprint (~x=12.5,
    # y=75). Left fails the depth check -> fall back to a single centered
    # keyhole that clears the color region.
    green = _mp(box(5, 60, 20, 90))
    regions = {R: _mp(box(0, 0, 100, 100)), G: green}
    art = _art(100, 100, regions=regions, plate=_mp(box(0, 0, 100, 100)))
    plan = _plan(art, _extrude("keyhole"), tops={R: DEEP, G: 3.0}, regions=regions)
    assert [w["code"] for w in plan.warnings] == ["hang_fallback_single"]
    # centered keyhole (x=50) avoids the green region entirely
    assert _centroid_xs(plan.cuts) == [50.0]
    # sanity: a tall green region (top well above the cavity) would NOT exclude
    plan2 = _plan(art, _extrude("keyhole"), tops={R: DEEP, G: DEEP}, regions=regions)
    assert plan2.warnings == []
    assert _centroid_xs(plan2.cuts) == [12.5, 87.5]


# --------------------------------------------------------------------------
# fins: geometry of the printed part
# --------------------------------------------------------------------------
def test_fins_two_sided_parts_and_cuts():
    art = _art(100, 100)
    plan = _plan(art, _extrude("fins"), tops={R: DEEP})
    assert plan.warnings == []
    assert len(plan.extra_parts) == 2
    assert {p.name for p in plan.extra_parts} == {"fin_left", "fin_right"}
    # tab-pocket cut stacks (6 sub-slices per fin), all within the tab depth.
    assert len(plan.cuts) == 12
    tab_depth = hanging.FIN_TAB_REACH_MM + hanging.FIN_TAB_CLEARANCE_MM
    assert min(c.z0_mm for c in plan.cuts) == pytest.approx(0.0)
    assert max(c.z1_mm for c in plan.cuts) == pytest.approx(tab_depth)
    for p in plan.extra_parts:
        assert p.color == R
        assert p.mesh.is_watertight
        assert p.mesh.volume > 0.0


def _tab_growth_dirs(mesh, angle_deg):
    """Extract each tab's growth direction (tip vertex minus its flush base
    vertex) in the (Uf, Vf) = (mesh Y, mesh Z) plane.

    Each sub-prism only has vertices on its two extrusion caps, so selecting
    by the tab's narrow X band isolates exactly that one tab's 6 vertices
    (the wall/strut/collar caps live at X in {0, FIN_WIDTH_MM}).
    """
    a = math.radians(angle_deg)
    g = np.array([-math.cos(a), math.sin(a)])  # expected "into the panel" dir
    verts = mesh.vertices
    mid = hanging.FIN_WIDTH_MM / 2
    dirs = []
    for xc in (mid - hanging.FIN_TAB_SPACING_MM / 2, mid + hanging.FIN_TAB_SPACING_MM / 2):
        sel = verts[np.abs(verts[:, 0] - xc) < hanging.FIN_TAB_BASE_MM / 2 + 1e-6]
        proj = sel[:, 1] * g[0] + sel[:, 2] * g[1]
        tip = sel[np.argmax(proj)]  # furthest out along the growth direction
        flush = sel[proj < proj.min() + 1e-6]  # on the wall's outward face
        base = flush[np.argmin(np.linalg.norm(flush[:, [1, 2]] - tip[[1, 2]], axis=1))]
        d = tip[[1, 2]] - base[[1, 2]]
        dirs.append(d / np.linalg.norm(d))
    return dirs, g


def test_fin_tabs_are_separated_in_x_not_z():
    # THE v1 FIX (preserved): the two tabs must be spread across the sign's X
    # (width) axis, not stacked vertically. Their tip X differs by ~SPACING
    # while they share the same (Uf, Vf) band (identical Z), i.e. not stacked.
    angle = 15.0
    mesh, _ = hanging._build_fin_mesh(angle)
    a = math.radians(angle)
    g = np.array([-math.cos(a), math.sin(a)])
    verts = mesh.vertices
    proj = verts[:, 1] * g[0] + verts[:, 2] * g[1]
    tips = verts[proj > proj.max() - 1e-6]  # the -REACH tab tips, both tabs
    xs = sorted(tips[:, 0])
    left_x = np.mean([x for x in xs if x < hanging.FIN_WIDTH_MM / 2])
    right_x = np.mean([x for x in xs if x >= hanging.FIN_WIDTH_MM / 2])
    assert right_x - left_x == pytest.approx(hanging.FIN_TAB_SPACING_MM, abs=0.5)
    # both tab tips sit at the same height band -> separated in X, not Z.
    assert tips[:, 2].max() - tips[:, 2].min() == pytest.approx(0.0, abs=1e-6)


def test_fin_tab_direction_matches_back_normal():
    # The tabs must point "into the panel" == (-cos, sin)(angle) in (Uf, Vf).
    # Test two asymmetric angles so a rotation-sign error can't hide.
    for angle in (10.0, 40.0):
        mesh, _ = hanging._build_fin_mesh(angle)
        dirs, g = _tab_growth_dirs(mesh, angle)
        for d in dirs:
            err_deg = math.degrees(math.acos(np.clip(float(d @ g), -1.0, 1.0)))
            assert err_deg <= 1.0, f"angle={angle} dir={d} expected={g} err={err_deg}"


def test_fin_assembled_frame_consistency():
    # THE check that would have caught v1 and v2: the vertical strut's top
    # (where the rotated pad attaches) must land exactly where the sign's own
    # attach point sits when the sign leans back by angle_deg.
    for angle in (10.0, 20.0, 40.0):
        mesh, strut_len = hanging._build_fin_mesh(angle)
        a = math.radians(angle)
        # the foot is a single flat plane on the table (Vf == 0).
        assert mesh.bounds[0][2] == pytest.approx(0.0, abs=1e-9)
        # virtual sign-leg tip via plain trig (not the mesh).
        tip = np.array(
            [hanging.FIN_ANCHOR_HEIGHT_MM * math.sin(a),
             hanging.FIN_ANCHOR_HEIGHT_MM * math.cos(a)]
        )
        # the derived strut length must equal the tip's Vf.
        assert strut_len == pytest.approx(float(tip[1]), abs=1e-9)
        # the mesh must actually carry a vertex at the attach point (the pad's
        # base corner), i.e. the two pieces meet where they should.
        yz = mesh.vertices[:, [1, 2]]
        d = np.linalg.norm(yz - tip, axis=1)
        assert d.min() == pytest.approx(0.0, abs=1e-6)


def test_fin_foot_is_a_single_flat_face():
    mesh, _ = hanging._build_fin_mesh(15.0)
    # min Z is exactly the bed (table); the strut foot rests flush.
    assert mesh.bounds[0][2] == pytest.approx(0.0, abs=1e-9)
    # the entire Z=0 face == the plain strut cross-section (one flat plane).
    tris = mesh.vertices[mesh.faces]
    area = 0.0
    for t in tris:
        if np.all(np.abs(t[:, 2]) < 1e-6):
            a, b, c = t
            area += 0.5 * abs(np.cross(b - a, c - a)[2])
    assert area == pytest.approx(
        hanging.FIN_WIDTH_MM * hanging.FIN_STRUT_THICKNESS_MM, rel=1e-6
    )


def test_fin_tab_ramp_is_at_most_45_degrees():
    # reach <= height keeps every tab ramp at or under 45 degrees (support-free).
    assert hanging.FIN_TAB_REACH_MM <= hanging.FIN_TAB_HEIGHT_MM
    ramp_deg = math.degrees(math.atan2(hanging.FIN_TAB_REACH_MM, hanging.FIN_TAB_HEIGHT_MM))
    assert ramp_deg <= 45.0


def test_fin_strut_len_scales_with_cosine():
    # v3: the strut is a plain VERTICAL beam of length ANCHOR*cos(angle).
    m10, len10 = hanging._build_fin_mesh(10.0)
    m30, len30 = hanging._build_fin_mesh(30.0)
    assert len10 == pytest.approx(
        hanging.FIN_ANCHOR_HEIGHT_MM * math.cos(math.radians(10.0))
    )
    assert len30 == pytest.approx(
        hanging.FIN_ANCHOR_HEIGHT_MM * math.cos(math.radians(30.0))
    )
    # a larger lean angle -> a shorter strut.
    assert len30 < len10
    # width (Xf / mesh X) is fixed regardless of angle.
    for m in (m10, m30):
        assert m.bounds[1][0] - m.bounds[0][0] == pytest.approx(hanging.FIN_WIDTH_MM, abs=1e-6)


def test_fin_watertight_across_angles():
    for angle in (0.0, 10.0, 20.0, 40.0, 45.0):
        mesh, _ = hanging._build_fin_mesh(angle)
        assert mesh.is_watertight
        assert mesh.volume > 0.0


def test_fin_is_a_small_part():
    # v3 sanity: the whole part stays compact (strut ANCHOR*cos + a short pad).
    mesh, _ = hanging._build_fin_mesh(15.0)
    span = mesh.bounds[1] - mesh.bounds[0]
    assert max(span) < hanging.FIN_ANCHOR_HEIGHT_MM + hanging.FIN_TAB_HEIGHT_MM + 6.0


def test_fins_placed_outside_sign_footprint():
    art = _art(100, 100)
    plan = _plan(art, _extrude("fins"), tops={R: DEEP})
    sign_max_x = art.plate_footprint.bounds[2]
    for p in plan.extra_parts:
        assert p.mesh.bounds[0][0] >= sign_max_x - 1e-6


def test_fins_fallback_single_center():
    art = _art(28, 100)
    plan = _plan(art, _extrude("fins"), tops={R: DEEP})
    assert [w["code"] for w in plan.warnings] == ["hang_fallback_single"]
    assert len(plan.extra_parts) == 1
    assert plan.extra_parts[0].name == "fin_center"
    assert len(plan.cuts) == 6


def test_fins_skipped_insufficient_depth():
    art = _art(100, 100)
    # less than FIN_TAB_MIN_REACH + CLEARANCE + MARGIN available.
    shallow = hanging.FIN_TAB_MIN_REACH_MM + hanging.FIN_TAB_CLEARANCE_MM
    plan = _plan(art, _extrude("fins"), tops={}, regions={}, model_top=shallow)
    assert plan.extra_parts == []
    assert plan.cuts == []
    assert [w["code"] for w in plan.warnings] == ["hang_skipped"]


def test_fins_adapt_reach_to_thin_model():
    art = _art(100, 100)
    # Ceiling between adaptive floor and nominal reach.
    ceiling = (
        hanging.FIN_TAB_MIN_REACH_MM
        + hanging.FIN_TAB_CLEARANCE_MM
        + hanging.FIN_MARGIN_MM
        + 0.5
    )
    plan = _plan(art, _extrude("fins"), tops={}, regions={}, model_top=ceiling)
    assert len(plan.extra_parts) == 2
    z1 = max(c.z1_mm for c in plan.cuts)
    expected_reach = ceiling - hanging.FIN_MARGIN_MM - hanging.FIN_TAB_CLEARANCE_MM
    assert z1 == pytest.approx(expected_reach + hanging.FIN_TAB_CLEARANCE_MM)
    assert expected_reach < hanging.FIN_TAB_REACH_MM
    assert any(w["code"] == "hang_depth_adapted" for w in plan.warnings)
