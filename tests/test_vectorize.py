"""Tests for vectorization: px->mm tracing with shared boundaries (FR-9)."""
import cv2
import numpy as np

from app.core.cleanup import run_cleanup
from app.core.vectorize import vectorize

PAL3 = ["#1a1a1a", "#e63946", "#f1faee"]
PAL2 = ["#000000", "#ffffff"]


def _stripe_labels(h=40, w=90):
    labels = np.zeros((h, w), np.int16)
    labels[:, :30] = 0
    labels[:, 30:60] = 1
    labels[:, 60:] = 2
    return labels


def test_roundtrip_area_matches_pixel_counts():
    mm_per_px = 0.1
    labels = _stripe_labels()
    res = run_cleanup(
        labels, PAL3, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    va = vectorize(res, PAL3, mm_per_px=mm_per_px, smoothness=0)
    px_area = mm_per_px ** 2
    for i, hx in enumerate(PAL3):
        want = int((res.labels == i).sum()) * px_area
        got = va.color_regions[hx].area
        assert abs(got - want) / want < 0.01


def test_scene_translated_to_origin():
    mm_per_px = 0.1
    labels = _stripe_labels()
    res = run_cleanup(
        labels, PAL3, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    va = vectorize(res, PAL3, mm_per_px=mm_per_px, smoothness=0)
    b = va.plate_footprint.bounds
    assert abs(b[0]) < 1e-9 and abs(b[1]) < 1e-9
    assert abs(va.width_mm - 90 * mm_per_px) < 1e-6
    assert abs(va.height_mm - 40 * mm_per_px) < 1e-6


def test_adjacent_regions_do_not_overlap_and_leave_no_gaps():
    mm_per_px = 0.1
    labels = _stripe_labels()
    res = run_cleanup(
        labels, PAL3, mm_per_px=mm_per_px, smoothness=6, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    va = vectorize(res, PAL3, mm_per_px=mm_per_px, smoothness=6)
    regions = list(va.color_regions.values())
    # No pairwise overlap.
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            assert regions[i].intersection(regions[j]).area < 1e-9
    # Union area == sum of areas (no slivers, no gaps between neighbours).
    from shapely.ops import unary_union
    union = unary_union(regions)
    total = sum(r.area for r in regions)
    assert abs(union.area - total) < 1e-6


def _o_labels(h=120, w=120):
    img = np.full((h, w), 1, np.int16)  # white bg
    cv2.circle(img, (w // 2, h // 2), 45, 0, -1)  # black disk
    cv2.circle(img, (w // 2, h // 2), 22, 1, -1)  # white counter
    return img


def test_through_hole_creates_polygon_hole_in_plate():
    mm_per_px = 0.2
    labels = _o_labels()
    res = run_cleanup(
        labels, PAL2, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="through_hole",
    )
    va = vectorize(res, PAL2, mm_per_px=mm_per_px, smoothness=0)
    # Plate has a hole where the counter was cut through.
    interiors = sum(len(p.interiors) for p in va.plate_footprint.geoms)
    assert interiors >= 1
    assert va.body_fill_region is None


def test_plate_mode_keeps_solid_plate_under_counter():
    mm_per_px = 0.2
    labels = _o_labels()
    res = run_cleanup(
        labels, PAL2, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    va = vectorize(res, PAL2, mm_per_px=mm_per_px, smoothness=0)
    # HOLE_PLATE counts toward the plate, so the plate has NO hole here
    # (counter is solid beneath), unlike the through_hole case.
    interiors = sum(len(p.interiors) for p in va.plate_footprint.geoms)
    assert interiors == 0
    assert va.plate_footprint.area > 0


def test_fill_mode_populates_body_fill_region():
    mm_per_px = 0.2
    labels = _o_labels()
    res = run_cleanup(
        labels, PAL2, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="fill",
    )
    va = vectorize(res, PAL2, mm_per_px=mm_per_px, smoothness=0)
    assert va.body_fill_region is not None
    assert va.body_fill_region.area > 0
    # Body-fill area is excluded from the white color region.
    white = va.color_regions.get("#ffffff")
    if white is not None:
        assert white.intersection(va.body_fill_region).area < 1e-6


def test_all_outputs_valid_multipolygons():
    mm_per_px = 0.1
    labels = _stripe_labels()
    res = run_cleanup(
        labels, PAL3, mm_per_px=mm_per_px, smoothness=4, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    va = vectorize(res, PAL3, mm_per_px=mm_per_px, smoothness=4)
    from shapely.geometry import MultiPolygon
    assert isinstance(va.plate_footprint, MultiPolygon)
    assert va.plate_footprint.is_valid
    for mp in va.color_regions.values():
        assert isinstance(mp, MultiPolygon)
        assert mp.is_valid


def test_plate_joins_disconnected_islands():
    """FR-11: with multi-island art the plate footprint spans the gap
    (convex hull minus through-holes) so everything prints as one object."""
    mm_per_px = 0.1
    labels = np.full((40, 100), -1, np.int16)  # transparent canvas
    labels[10:30, 5:25] = 0     # island A
    labels[10:30, 75:95] = 1    # island B, 5mm gap between them
    res = run_cleanup(
        labels, PAL2, mm_per_px=mm_per_px, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    va = vectorize(res, PAL2, mm_per_px=mm_per_px, smoothness=0)
    assert len(va.plate_footprint.geoms) == 1  # single joined slab
    # the plate must cover the gap between the two islands
    from shapely.geometry import Point
    assert va.plate_footprint.contains(Point(4.5, 1.0))  # mid-gap, mid-height
    # art regions themselves stay disconnected
    assert len(va.color_regions[PAL2[0]].geoms) == 1
    assert len(va.color_regions[PAL2[1]].geoms) == 1
