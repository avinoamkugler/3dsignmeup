"""Palette detection and quantization (FR-1, FR-2).

detect_palette(rgba, n_colors) -> list[str]
    k-means in Lab space on non-transparent pixels (alpha > 0 counts as
    opaque; alpha == 0 ignored). Downsample large images for speed before
    clustering. Each returned center is snapped back to the nearest
    actually-occurring pixel color so picks look "true". Returns lowercase
    hex, ordered by cluster population (most common first). Deterministic
    (fixed random_state).

quantize(rgba, palette) -> Labels
    Map every non-transparent pixel to the index of the nearest palette
    color by perceptual (Lab) distance; fully transparent pixels (alpha==0)
    become TRANSPARENT. Semi-transparent pixels are treated as opaque.
    Output dtype int16, shape (H, W).

Lab conversion uses skimage.color.rgb2lab (float RGB in [0, 1]) consistently
in both functions. The clustering sample is capped at 200k opaque pixels
(deterministic RNG, seed 0); cluster centers are snapped against the full
set of actually-occurring opaque colors so picks are real pixel colors.
"""
from __future__ import annotations

import numpy as np
from skimage.color import rgb2lab
from sklearn.cluster import KMeans

from app.core.types import Labels, TRANSPARENT, rgb_to_hex, hex_to_rgb

_MAX_FIT_PIXELS = 200_000


def _opaque_rgb(rgba: np.ndarray) -> np.ndarray:
    """Return (M, 3) uint8 array of opaque pixel RGB values."""
    if rgba.ndim != 3 or rgba.shape[2] not in (3, 4):
        raise ValueError("expected an (H, W, 3|4) image array")
    rgb = rgba[..., :3]
    if rgba.shape[2] == 4:
        opaque = rgba[..., 3] > 0
        return rgb[opaque].reshape(-1, 3)
    return rgb.reshape(-1, 3)


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(N, 3) uint8/float RGB -> (N, 3) Lab."""
    arr = np.asarray(rgb, dtype=np.float64) / 255.0
    return rgb2lab(arr.reshape(-1, 1, 3)).reshape(-1, 3)


def detect_palette(rgba: np.ndarray, n_colors: int) -> list[str]:
    rgb = _opaque_rgb(rgba)
    if rgb.shape[0] == 0:
        return []

    unique_rgb = np.unique(rgb, axis=0)
    unique_lab = _rgb_to_lab(unique_rgb)

    n_eff = int(min(n_colors, unique_rgb.shape[0]))

    lab = _rgb_to_lab(rgb)
    if lab.shape[0] > _MAX_FIT_PIXELS:
        rng = np.random.default_rng(0)
        idx = rng.choice(lab.shape[0], _MAX_FIT_PIXELS, replace=False)
        sample = lab[idx]
    else:
        sample = lab

    km = KMeans(n_clusters=n_eff, random_state=0, n_init=10)
    assignments = km.fit_predict(sample)
    counts = np.bincount(assignments, minlength=n_eff)
    centers = km.cluster_centers_

    # Snap each center to the nearest actually-occurring opaque color.
    order = np.argsort(counts)[::-1]  # most populous first
    seen: list[str] = []
    for ci in order:
        d = ((unique_lab - centers[ci]) ** 2).sum(axis=1)
        snapped = unique_rgb[int(np.argmin(d))]
        hexc = rgb_to_hex(snapped)
        if hexc not in seen:
            seen.append(hexc)
    return seen


def quantize(rgba: np.ndarray, palette: list[str]) -> Labels:
    if rgba.ndim != 3 or rgba.shape[2] not in (3, 4):
        raise ValueError("expected an (H, W, 3|4) image array")
    h, w = rgba.shape[:2]
    lab = _rgb_to_lab(rgba[..., :3].reshape(-1, 3))  # (H*W, 3)

    palette_rgb = np.array([hex_to_rgb(hx) for hx in palette], dtype=np.float64)
    palette_lab = _rgb_to_lab(palette_rgb)

    best_idx = np.zeros(lab.shape[0], dtype=np.int32)
    best_dist = np.full(lab.shape[0], np.inf, dtype=np.float64)
    for i, pl in enumerate(palette_lab):
        d = ((lab - pl) ** 2).sum(axis=1)
        upd = d < best_dist
        best_idx[upd] = i
        best_dist[upd] = d[upd]

    labels = best_idx.reshape(h, w).astype(np.int16)
    if rgba.shape[2] == 4:
        labels[rgba[..., 3] == 0] = TRANSPARENT
    return labels
