"""Tests for the deterministic cleanup pipeline (FR-3..FR-7)."""
import cv2
import numpy as np
import pytest
from skimage.measure import label as cc_label

from app.core.cleanup import prepare, run_cleanup
from app.core.types import HOLE_PLATE, TRANSPARENT, TrimBox

PAL3 = ["#1a1a1a", "#e63946", "#f1faee"]
PAL2 = ["#000000", "#ffffff"]


def _noisy_stripes(h=80, w=90, seed=0):
    """3 clean vertical stripes (labels) with random single-pixel noise."""
    labels = np.zeros((h, w), np.int16)
    labels[:, :30] = 0
    labels[:, 30:60] = 1
    labels[:, 60:] = 2
    rng = np.random.default_rng(seed)
    ys = rng.integers(0, h, 400)
    xs = rng.integers(0, w, 400)
    vals = rng.integers(0, 3, 400).astype(np.int16)
    labels[ys, xs] = vals
    return labels


def test_fr2_labels_are_subset_of_valid_values():
    labels = _noisy_stripes()
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=4, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    uniq = set(np.unique(res.labels).tolist())
    assert uniq <= {TRANSPARENT, HOLE_PLATE, 0, 1, 2}


def test_island_removal_leaves_no_tiny_components():
    labels = _noisy_stripes()
    mm_per_px = 0.1
    min_feature_mm = 0.5
    res = run_cleanup(
        labels, PAL3, mm_per_px=mm_per_px, smoothness=0,
        min_feature_mm=min_feature_mm, clear_background=False,
        enclosed_background="plate",
    )
    min_area = (min_feature_mm / mm_per_px) ** 2
    bg = int(res.labels.min()) - 1
    cc = cc_label(res.labels, background=bg, connectivity=1)
    areas = np.bincount(cc.ravel())
    areas = areas[areas > 0]
    assert areas.min() >= min_area


def _o_shape(h=120, w=120):
    """A filled 'O': black ring on white background, white counter inside
    (not connected to any corner). Returns quantized-style label grid."""
    img = np.zeros((h, w), np.int16)  # 0 == white background... set below
    img[:] = 1  # white (index 1 in PAL2)
    cv2.circle(img, (w // 2, h // 2), 45, 0, -1)  # black disk (index 0)
    cv2.circle(img, (w // 2, h // 2), 22, 1, -1)  # white counter (index 1)
    return img


def test_background_clear_removes_corner_region_keeps_ring():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    # Corners (white bg) cleared to transparent.
    assert res.labels[0, 0] == TRANSPARENT
    # The black ring survives.
    assert (res.labels == 0).any()


def test_enclosed_through_hole():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="through_hole",
    )
    # No white (index 1) remains anywhere; counter cut through.
    assert not (res.labels == 1).any()
    assert not (res.labels == HOLE_PLATE).any()
    # Center is transparent.
    assert res.labels[60, 60] == TRANSPARENT


def test_enclosed_plate():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    assert res.labels[60, 60] == HOLE_PLATE
    assert not (res.labels == 1).any()  # white fully resolved


def test_enclosed_fill():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="fill",
    )
    # Counter keeps its palette label (white) AND is marked body_fill.
    assert res.labels[60, 60] == 1
    assert res.body_fill_mask[60, 60]
    assert not (res.labels == HOLE_PLATE).any()
    # The cleared outer background is transparent, not body_fill.
    assert not res.body_fill_mask[0, 0]


def test_thin_feature_flags_one_px_line():
    h = w = 60
    labels = np.full((h, w), 1, np.int16)  # white bg
    labels[10:50, 30] = 0  # 1px-wide black line, length 40
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.1, smoothness=0, min_feature_mm=0.5,
        clear_background=False, enclosed_background="plate",
    )
    assert res.thin_mask is not None
    assert res.thin_mask[30, 30]  # a line pixel is flagged


def test_no_thin_mask_for_chunky_shape():
    labels = np.zeros((60, 60), np.int16)
    labels[:, :30] = 0
    labels[:, 30:] = 1
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.1, smoothness=0, min_feature_mm=0.5,
        clear_background=False, enclosed_background="plate",
    )
    assert res.thin_mask is None


def test_coverage_sums_over_artwork():
    labels = np.zeros((10, 30), np.int16)
    labels[:, :10] = 0
    labels[:, 10:20] = 1
    labels[:, 20:] = 2
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
    )
    assert abs(sum(res.coverage.values()) - 1.0) < 1e-9
    assert abs(res.coverage["#1a1a1a"] - 1 / 3) < 1e-9


def test_smoothing_never_introduces_new_labels():
    labels = _noisy_stripes()
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=10, min_feature_mm=0.5,
        clear_background=False, enclosed_background="plate",
    )
    assert set(np.unique(res.labels).tolist()) <= {TRANSPARENT, 0, 1, 2}


def test_paint_fill_recolors_one_connected_component():
    # Two disconnected red (#e63946 = idx 1) blobs; paint only the left one white.
    labels = np.zeros((40, 60), np.int16)
    labels[5:15, 5:15] = 1
    labels[5:15, 40:50] = 1
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
        paint_fills=[(10, 10, "#f1faee")],
    )
    assert (res.labels[5:15, 5:15] == 2).all()
    assert (res.labels[5:15, 40:50] == 1).all()


def test_paint_fill_recolors_transparent_component():
    labels = np.full((20, 20), TRANSPARENT, np.int16)
    labels[5:15, 5:15] = 0
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
        paint_fills=[(0, 0, "#e63946")],
    )
    assert res.labels[0, 0] == 1
    assert (res.labels[5:15, 5:15] == 0).all()
    assert (res.labels[0:5, :] == 1).all()


def test_paint_fills_apply_in_order():
    labels = np.zeros((30, 30), np.int16)
    labels[5:25, 5:25] = 1
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
        paint_fills=[
            (15, 15, "#f1faee"),
            (15, 15, "#1a1a1a"),
        ],
    )
    assert (res.labels[5:25, 5:25] == 0).all()


def test_paint_erase_to_through_hole():
    labels = np.zeros((40, 60), np.int16)
    labels[5:15, 5:15] = 1
    labels[5:15, 40:50] = 1
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
        paint_fills=[(10, 10, None)],
    )
    assert (res.labels[5:15, 5:15] == TRANSPARENT).all()
    assert (res.labels[5:15, 40:50] == 1).all()


def test_paint_erase_cuts_plate_even_when_enclosed_is_plate():
    labels = np.zeros((30, 30), np.int16)
    labels[5:25, 5:25] = 1
    res = run_cleanup(
        labels, PAL3, mm_per_px=0.1, smoothness=0, min_feature_mm=0.1,
        clear_background=False, enclosed_background="plate",
        paint_fills=[(15, 15, None)],
    )
    assert (res.labels[5:25, 5:25] == TRANSPARENT).all()
    assert not (res.labels == HOLE_PLATE).any()


def test_paint_restores_hole_plate_to_palette_color():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
        paint_fills=[(60, 60, "#ffffff")],
    )
    assert res.labels[60, 60] == 1
    assert (res.labels[50:70, 50:70] == 1).any()
    assert not res.body_fill_mask[60, 60]


def test_paint_restores_through_hole_to_palette_color():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="through_hole",
        paint_fills=[(60, 60, "#000000")],
    )
    assert res.labels[60, 60] == 0
    assert (res.labels[55:65, 55:65] == 0).all()


def test_paint_same_color_promotes_body_fill_to_real_color():
    labels = _o_shape()
    res = run_cleanup(
        labels, PAL2, mm_per_px=0.2, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="fill",
        paint_fills=[(60, 60, "#ffffff")],
    )
    assert res.labels[60, 60] == 1
    assert not res.body_fill_mask[60, 60]



# --------------------------------------------------------------------------
# Margin trimming (prepare)
# --------------------------------------------------------------------------
def _offset_disk(h=120, w=200):
    """A black disk on a white background, deliberately off-centre so the
    trim box is asymmetric in both axes."""
    img = np.full((h, w), 1, np.int16)
    cv2.circle(img, (70, 40), 25, 0, -1)
    return img


def test_prepare_trims_margins_and_scales_target_width_to_the_art():
    labels = _offset_disk()
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    # Disk spans cols 45..95 and rows 15..65 -> a 51x51 box.
    assert prep.trim == TrimBox(top=15, left=45, bottom=66, right=96)
    assert prep.result.labels.shape == (51, 51)
    # target_width_mm now sizes the artwork, not the 200px-wide source image.
    assert prep.mm_per_px == pytest.approx(100.0 / 51)


def test_prepare_keeps_clearing_background_inside_the_trimmed_box():
    """The regression risk of trimming: art touches the trimmed corners, so
    corner sampling would mistake the art itself for background."""
    labels = _offset_disk()
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    out = prep.result.labels
    # The disk survives...
    assert (out == 0).any()
    assert out[25, 25] == 0  # centre of the trimmed box
    # ...and the white corners of the box are still cleared.
    assert out[0, 0] == TRANSPARENT


def test_prepare_does_not_trim_when_clear_background_is_off():
    labels = _offset_disk()
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=0.5,
        clear_background=False, enclosed_background="plate",
    )
    assert prep.trim is None
    assert prep.result.labels.shape == labels.shape
    assert prep.mm_per_px == pytest.approx(100.0 / 200)


def test_prepare_ignores_sub_min_feature_specks_when_trimming():
    """A speck in the margin must not inflate the box. min_feature_mm=1.5 at
    0.5mm/px puts the threshold at 9px, so the stray pixel is sub-threshold —
    at 0.5mm/px a single pixel is exactly 0.5mm and a legitimate feature."""
    labels = _offset_disk()
    labels[110, 190] = 0  # a single stray pixel far from the disk
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=1.5,
        clear_background=True, enclosed_background="plate",
    )
    assert prep.trim == TrimBox(top=15, left=45, bottom=66, right=96)


def test_prepare_leaves_full_bleed_art_untrimmed():
    """No background to clear -> nothing to trim, and the old scale stands."""
    labels = np.zeros((60, 80), np.int16)
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    assert prep.trim is None
    assert prep.mm_per_px == pytest.approx(100.0 / 80)


def test_prepare_preserves_enclosed_background_through_the_trim():
    labels = _o_shape()
    prep = prepare(
        labels, PAL2, target_width_mm=100.0, smoothness=0, min_feature_mm=0.5,
        clear_background=True, enclosed_background="plate",
    )
    out = prep.result.labels
    assert prep.trim == TrimBox(top=15, left=15, bottom=106, right=106)
    assert (out == HOLE_PLATE).any()  # the counter is still a plate-backed hole
    assert out[45, 45] == HOLE_PLATE  # disk centre, in trimmed coords
