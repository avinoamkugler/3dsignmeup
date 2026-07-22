"""Tests for palette detection and quantization (FR-1, FR-2)."""
import numpy as np

from app.core.palette import detect_palette, quantize
from app.core.types import TRANSPARENT, hex_to_rgb


def _stripes_rgba(widths, colors, height=40):
    """Vertical stripes of given pixel widths / rgb colors -> (H,W,4) uint8."""
    w = sum(widths)
    img = np.zeros((height, w, 4), dtype=np.uint8)
    img[..., 3] = 255
    x = 0
    for width, col in zip(widths, colors):
        img[:, x : x + width, :3] = col
        x += width
    return img


BLACK = (26, 26, 26)
RED = (230, 57, 70)
WHITE = (241, 250, 238)


def test_detect_palette_returns_true_colors_ordered_by_population():
    img = _stripes_rgba([30, 20, 10], [BLACK, RED, WHITE])
    pal = detect_palette(img, 3)
    assert pal == ["#1a1a1a", "#e63946", "#f1faee"]  # widest first


def test_detect_palette_deterministic():
    img = _stripes_rgba([30, 20, 10], [BLACK, RED, WHITE])
    assert detect_palette(img, 3) == detect_palette(img, 3)


def test_detect_palette_ignores_transparent_pixels():
    img = _stripes_rgba([30, 20, 10], [BLACK, RED, WHITE])
    # Paint a huge transparent block of a 4th color that must be ignored.
    img[:, :15, :3] = (0, 200, 0)
    img[:, :15, 3] = 0
    pal = detect_palette(img, 3)
    assert "#00c800" not in pal
    assert set(pal) == {"#1a1a1a", "#e63946", "#f1faee"}


def test_detect_palette_snaps_to_real_pixel_color():
    img = _stripes_rgba([30, 20, 10], [BLACK, RED, WHITE])
    pal = detect_palette(img, 3)
    present = {tuple(c) for c in [BLACK, RED, WHITE]}
    for hx in pal:
        assert hex_to_rgb(hx) in present


def test_quantize_only_palette_indices_and_transparent():
    img = _stripes_rgba([30, 20, 10], [BLACK, RED, WHITE])
    img[:, :5, 3] = 0  # some fully transparent pixels
    pal = ["#1a1a1a", "#e63946", "#f1faee"]
    labels = quantize(img, pal)
    assert labels.dtype == np.int16
    uniq = set(np.unique(labels).tolist())
    assert uniq <= {TRANSPARENT, 0, 1, 2}
    assert TRANSPARENT in uniq
    # Transparent region is exactly where alpha == 0.
    assert np.all(labels[:, :5] == TRANSPARENT)


def test_quantize_maps_offpalette_to_nearest():
    # Slightly off-palette reds must snap to the red index.
    img = np.zeros((10, 10, 4), np.uint8)
    img[..., 3] = 255
    img[..., :3] = (225, 60, 66)  # near RED
    pal = ["#1a1a1a", "#e63946", "#f1faee"]
    labels = quantize(img, pal)
    assert np.all(labels == 1)
