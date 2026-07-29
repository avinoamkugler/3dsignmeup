"""Deterministic label-grid cleanup (FR-3..FR-7).

Entry point:

prepare(labels, palette, *, target_width_mm, smoothness, min_feature_mm,
        clear_background, enclosed_background, paint_fills=()) -> PreparedArt

which trims empty margins (only when clear_background is on) and then calls:

run_cleanup(labels, palette, *, mm_per_px, smoothness, min_feature_mm,
            clear_background, enclosed_background, paint_fills=(),
            background_labels=None) -> CleanupResult

Trimming crops the quantized grid to the artwork's bounding box, so
target_width_mm sizes the artwork rather than the source image and the printed
sign no longer shrinks in proportion to whatever margin the upload happened to
have. It runs before cleanup because every step below scales by mm_per_px,
which the trimmed width defines.

Fixed processing order (from the design doc):
1. Smoothness: mode filtering on the label grid, kernel size scaled by
   smoothness x mm-scale (resolution independent); smoothness=0 is a no-op.
2. Island removal: connected components with area < min_feature_mm^2 are
   reassigned to their dominant surrounding label.
3. Background clear (if clear_background): sample the four corner labels;
   flood-fill every same-label region 4-connected to any corner -> TRANSPARENT.
   Originally-transparent pixels also count as corner-reachable background.
   On a trimmed grid the corners hold artwork, so `prepare` passes the
   background labels it detected pre-trim and the flood seeds from the border.
4. Enclosed background resolution (only when clear_background).
5. Paint-bucket fills (optional): each op flood-fills the single 4-connected
   component under a click to a chosen palette color, or erases it to
   TRANSPARENT (through-hole in art and plate) when to_hex is null.
6. Thin-feature scan (detection only).

Spatial parameters scale by mm_per_px so results are resolution independent.

Implementation choices documented inline:
- Smoothing is a true per-label majority (mode) vote via box-filter counts,
  argmax across categories (TRANSPARENT is its own category). Never produces a
  non-existing label.
- Island removal segments the grid into homogeneous connected components in one
  skimage.measure.label pass (4-connectivity), then reassigns every pixel of a
  sub-threshold component to the label of its nearest surviving pixel
  (Euclidean distance-transform fill). This is a vectorized equivalent of
  "dominant surrounding label" for tiny regions. Iterated up to 3 passes.
"""
from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np
from scipy import ndimage
from skimage.measure import label as cc_label

from app.core.types import (
    CleanupResult,
    HOLE_PLATE,
    Labels,
    PreparedArt,
    TRANSPARENT,
    TrimBox,
    hex_to_rgb,  # noqa: F401  (kept for parity; unused directly)
)

_MAX_ISLAND_PASSES = 3


def _smooth(labels: Labels, radius: int) -> Labels:
    """Categorical majority filter over a (2r+1) window; TRANSPARENT is a
    category. Ties resolved toward the lower label value (deterministic)."""
    if radius < 1:
        return labels
    k = 2 * radius + 1
    uniq = np.unique(labels)
    best_count = np.zeros(labels.shape, dtype=np.float32)
    best_label = np.full(labels.shape, uniq[0], dtype=labels.dtype)
    for v in uniq:
        m = (labels == v).astype(np.float32)
        c = cv2.boxFilter(
            m, ddepth=-1, ksize=(k, k),
            normalize=False, borderType=cv2.BORDER_REPLICATE,
        )
        upd = c > best_count
        best_label[upd] = v
        best_count[upd] = c[upd]
    return best_label


def _remove_islands(labels: Labels, min_area_px: float) -> Labels:
    """Reassign sub-threshold homogeneous components to their nearest
    surviving pixel's label. Iterates until stable or 3 passes."""
    if min_area_px <= 1:
        return labels
    out = labels.copy()
    bg = int(out.min()) - 1  # a value not present, so every pixel is labeled
    for _ in range(_MAX_ISLAND_PASSES):
        cc = cc_label(out, background=bg, connectivity=1)
        areas = np.bincount(cc.ravel())
        small_ids = np.nonzero(areas < min_area_px)[0]
        # id 0 only appears for pixels == bg (none here); guard anyway
        small_ids = small_ids[small_ids != 0]
        if small_ids.size == 0:
            break
        small_mask = np.isin(cc, small_ids)
        if small_mask.all():
            break
        # nearest surviving pixel for every small pixel
        _, (iy, ix) = ndimage.distance_transform_edt(
            small_mask, return_indices=True
        )
        out = out[iy, ix]
    return out


def _background_clear(
    labels: Labels, background_labels: set[int] | None = None
) -> tuple[Labels, set[int]]:
    """Flood TRANSPARENT from the four corners over cells that are either
    already TRANSPARENT or hold a corner label. Returns cleared grid and the
    set of palette labels sampled at the corners (the "background" colors).

    ``background_labels`` overrides corner sampling. Trimmed grids have artwork
    touching their corners, so the caller passes the set detected before the
    trim rather than letting this re-sample and clear the art itself.
    """
    h, w = labels.shape
    corners = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    if background_labels is None:
        corner_labels = {int(labels[r, c]) for (r, c) in corners}
        palette_bg = {v for v in corner_labels if v >= 0}
    else:
        palette_bg = {v for v in background_labels if v >= 0}

    passable = (labels == TRANSPARENT)
    for v in palette_bg:
        passable |= (labels == v)

    comp = cc_label(passable, background=0, connectivity=1)
    if background_labels is None:
        seed_ids = {int(comp[r, c]) for (r, c) in corners if passable[r, c]}
    else:
        # A trimmed grid's corners are artwork, so seed from the whole border
        # ring. Only background-colored cells are passable, so widening the
        # seeds cannot reach the art.
        ring = np.zeros(labels.shape, dtype=bool)
        ring[0, :] = ring[-1, :] = True
        ring[:, 0] = ring[:, -1] = True
        seed_ids = set(np.unique(comp[ring & passable]).tolist())
    seed_ids.discard(0)
    if seed_ids:
        reached = np.isin(comp, list(seed_ids))
        out = labels.copy()
        out[reached] = TRANSPARENT
    else:
        out = labels.copy()
    return out, palette_bg


def _thin_mask(labels: Labels, n_colors: int, radius: int) -> np.ndarray | None:
    """Morphological opening residual per palette color -> thin mask."""
    if radius < 1:
        return None
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)
    )
    thin = np.zeros(labels.shape, dtype=bool)
    for i in range(n_colors):
        m = (labels == i).astype(np.uint8)
        if not m.any():
            continue
        opened = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
        thin |= (m > 0) & (opened == 0)
    return thin if thin.any() else None


def apply_paint_fills(
    labels: Labels,
    body_fill_mask: np.ndarray,
    palette: list[str],
    paint_fills: Sequence[tuple[int, int, str | None]],
) -> tuple[Labels, np.ndarray]:
    """Apply ordered paint-bucket ops: each floods one connected component.

    Each fill is ``(x, y, to_hex)`` in image space (row 0 = top). ``to_hex``
    null means erase to TRANSPARENT (through-hole in art and plate). Palette
    colors, HOLE_PLATE, and TRANSPARENT are all fillable so enclosed holes
    (e.g. white letterforms that matched the cleared background) can be
    restored. Painting a body-fill region with its own display color clears
    the body-fill flag, promoting it to a real palette color. Other painted
    body-fill pixels also lose the flag.
    """
    if not paint_fills:
        return labels, body_fill_mask
    out = labels.copy()
    bf = body_fill_mask.copy()
    hex_to_idx = {hx: i for i, hx in enumerate(palette)}
    h, w = out.shape
    for x, y, to_hex in paint_fills:
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        src = int(out[y, x])
        if to_hex is None:
            if src == TRANSPARENT:
                continue
            to_idx = None
        else:
            to_idx = hex_to_idx.get(to_hex)
            if to_idx is None:
                continue
        cc = cc_label(out == src, connectivity=1)
        cid = int(cc[y, x])
        if cid == 0:
            continue
        region = cc == cid
        if to_idx is not None and src == to_idx:
            if not bf[region].any():
                continue
            bf[region] = False
            continue
        if to_hex is None:
            out[region] = TRANSPARENT
        else:
            out[region] = to_idx
        bf[region] = False
    return out, bf


def run_cleanup(
    labels: Labels,
    palette: list[str],
    *,
    mm_per_px: float,
    smoothness: int,
    min_feature_mm: float,
    clear_background: bool,
    enclosed_background: str,
    paint_fills: Sequence[tuple[int, int, str | None]] | None = None,
    background_labels: set[int] | None = None,
) -> CleanupResult:
    n = len(palette)
    labels = np.asarray(labels, dtype=np.int16).copy()

    # 1. Smoothing (kernel radius ~ smoothness * 0.05mm, in px).
    radius = int(round(smoothness * 0.05 / mm_per_px)) if smoothness > 0 else 0
    labels = _smooth(labels, radius)

    # 2. Island removal (area threshold in px^2).
    min_area_px = (min_feature_mm / mm_per_px) ** 2
    labels = _remove_islands(labels, min_area_px)

    body_fill_mask = np.zeros(labels.shape, dtype=bool)

    if clear_background:
        # 3. Corner-connected background clear.
        labels, palette_bg = _background_clear(labels, background_labels)

        # 4. Enclosed background: remaining pixels still holding a corner
        #    (background) palette label are enclosed regions.
        if palette_bg:
            enclosed = np.isin(labels, list(palette_bg))
            if enclosed.any():
                if enclosed_background == "through_hole":
                    labels[enclosed] = TRANSPARENT
                elif enclosed_background == "plate":
                    labels[enclosed] = HOLE_PLATE
                elif enclosed_background == "fill":
                    body_fill_mask = enclosed
                else:
                    raise ValueError(
                        f"unknown enclosed_background: {enclosed_background!r}"
                    )

    # 5. Paint-bucket fills (after cleanup so clicks match the preview).
    if paint_fills:
        labels, body_fill_mask = apply_paint_fills(
            labels, body_fill_mask, palette, paint_fills
        )

    # 6. Thin-feature scan (detection only, never mutates labels).
    thin_radius = int(round(min_feature_mm / 2 / mm_per_px))
    thin = _thin_mask(labels, n, thin_radius)

    # islands: 8-connected components of artwork (label >= 0) after clearing.
    art = labels >= 0
    islands = int(cc_label(art, background=0, connectivity=2).max()) if art.any() else 0

    # coverage: fraction of artwork (label >= 0) pixels per palette hex.
    denom = int(art.sum())
    coverage: dict[str, float] = {}
    for i, hx in enumerate(palette):
        cnt = int((labels == i).sum())
        coverage[hx] = (cnt / denom) if denom > 0 else 0.0

    return CleanupResult(
        labels=labels,
        body_fill_mask=body_fill_mask,
        thin_mask=thin,
        islands=islands,
        coverage=coverage,
    )


def art_trim_box(
    labels: Labels, *, min_area_px: float
) -> tuple[TrimBox, set[int]] | None:
    """Tightest box around the artwork that would survive a background clear.

    Runs the background clear on the *quantized* grid — before smoothing and
    island removal — because the caller needs the trimmed width to derive
    mm_per_px, which those steps already consume. Components smaller than
    ``min_area_px`` are ignored so a stray speck in the margin cannot inflate
    the box; island removal would have deleted them anyway.

    Returns the box and the palette labels detected as background, or None when
    no artwork survives (e.g. a full-bleed image whose corners are art).
    """
    cleared, palette_bg = _background_clear(labels)
    art = cleared != TRANSPARENT
    if not art.any():
        return None

    if min_area_px > 1:
        cc = cc_label(art, background=0, connectivity=2)
        areas = np.bincount(cc.ravel())
        small = np.nonzero(areas < min_area_px)[0]
        art &= ~np.isin(cc, small[small != 0])
        if not art.any():
            return None

    rows = np.nonzero(art.any(axis=1))[0]
    cols = np.nonzero(art.any(axis=0))[0]
    box = TrimBox(
        top=int(rows[0]),
        left=int(cols[0]),
        bottom=int(rows[-1]) + 1,
        right=int(cols[-1]) + 1,
    )
    return box, palette_bg


def prepare(
    labels: Labels,
    palette: list[str],
    *,
    target_width_mm: float,
    smoothness: int,
    min_feature_mm: float,
    clear_background: bool,
    enclosed_background: str,
    paint_fills: Sequence[tuple[int, int, str | None]] | None = None,
) -> PreparedArt:
    """Trim empty margins, then clean up — the whole quantized-grid stage.

    When ``clear_background`` is on, the artwork is trimmed to its bounding box
    and ``target_width_mm`` becomes the width of the *artwork* rather than of
    the source image, so margins no longer shrink the printed sign. The trim
    runs before cleanup so every mm-scaled parameter (smoothing radius, min
    feature, thin scan) and the preview PNG share one scale.
    """
    mm_per_px = target_width_mm / int(labels.shape[1])
    trim: TrimBox | None = None
    background_labels: set[int] | None = None

    if clear_background:
        found = art_trim_box(labels, min_area_px=(min_feature_mm / mm_per_px) ** 2)
        if found is not None:
            box, background_labels = found
            if box.shape != labels.shape:
                trim = box
                labels = labels[box.top : box.bottom, box.left : box.right]
                mm_per_px = target_width_mm / box.width
            else:
                background_labels = None  # nothing trimmed; corner sampling is fine

    result = run_cleanup(
        labels,
        palette,
        mm_per_px=mm_per_px,
        smoothness=smoothness,
        min_feature_mm=min_feature_mm,
        clear_background=clear_background,
        enclosed_background=enclosed_background,
        paint_fills=paint_fills,
        background_labels=background_labels,
    )
    return PreparedArt(result=result, mm_per_px=mm_per_px, trim=trim)


def color_perimeters(labels: Labels, palette: list[str]) -> dict[str, float]:
    """Boundary-pixel count per palette color: pixels of that color with at
    least one 4-neighbor holding a different value (image edge counts). The
    raster equivalent of geometry's per-region perimeter — used only to rank
    "perimeter presence" for the equal-height sibling tiebreak, so the client
    sidebar and the server swap plan agree on sibling ordering."""
    padded = np.pad(labels, 1, constant_values=TRANSPARENT - 1)
    diff = (
        (padded[1:-1, 1:-1] != padded[:-2, 1:-1])
        | (padded[1:-1, 1:-1] != padded[2:, 1:-1])
        | (padded[1:-1, 1:-1] != padded[1:-1, :-2])
        | (padded[1:-1, 1:-1] != padded[1:-1, 2:])
    )
    out: dict[str, float] = {}
    for i, hx in enumerate(palette):
        out[hx] = float(((labels == i) & diff).sum())
    return out
