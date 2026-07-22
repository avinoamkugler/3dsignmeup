"""Label grid -> shapely geometry in mm (FR-9 prerequisites).

vectorize(result, palette, *, mm_per_px, smoothness) -> VectorizedArt

Tracing strategy (guarantees adjacent regions share boundary vertices):
- Every surface pixel is assigned a region key: a palette index for visible
  color, a distinct key for body-fill pixels; TRANSPARENT and HOLE_PLATE are
  void. Each key's mask is contour-traced with skimage.measure.find_contours
  at the 0.5 level on a zero-padded mask, i.e. exactly along the half-pixel
  boundary between filled and empty. Because every shared edge is the same
  0.5 level crossing computed from the same grid, adjacent regions' vertices
  are numerically identical -> perfect butting, zero overlap.
- When smoothness > 0 the shared boundary NETWORK is simplified once: the
  union of all region boundaries is noded into arcs (shared edges become a
  single arc), each arc simplified with tolerance 0.3 * smoothness * mm_per_px
  (preserve_topology), then re-polygonized and faces re-assigned to keys by
  their interior point. Simplifying each shared arc exactly once keeps
  neighbours sharing vertices, so the sliver-free guarantee survives.
- Holes are recovered via even-odd nesting of the traced rings.
- Coordinates are flipped to CAD space (y up), scaled by mm_per_px, and the
  whole scene is translated so the artwork bbox bottom-left is (0, 0).
- plate_footprint traces the non-transparent mask (through-holes kept as
  polygon holes) and is simplified independently (it is a base slab).
"""
from __future__ import annotations

import numpy as np
from shapely import make_valid
from shapely.affinity import translate as shp_translate
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union
from skimage.measure import find_contours

from app.core.types import (
    CleanupResult,
    HOLE_PLATE,
    TRANSPARENT,
    VectorizedArt,
)

_BODY_FILL_KEY = -100  # sentinel region key for body-fill pixels


def _mask_rings(mask: np.ndarray, h: int, mm_per_px: float) -> list[np.ndarray]:
    """Trace a binary mask -> list of (N,2) ring coordinate arrays in mm
    (y-flipped CAD space, no translation yet)."""
    if not mask.any():
        return []
    padded = np.zeros((mask.shape[0] + 2, mask.shape[1] + 2), dtype=np.float32)
    padded[1:-1, 1:-1] = mask.astype(np.float32)
    rings: list[np.ndarray] = []
    for c in find_contours(padded, 0.5):
        # c: (row, col) in padded coords; undo the 1px pad.
        rr = c[:, 0] - 1.0
        cc = c[:, 1] - 1.0
        x = cc * mm_per_px
        y = (h - rr) * mm_per_px
        ring = np.column_stack([x, y])
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _rings_to_polygons(rings: list[np.ndarray]) -> list[Polygon]:
    """Assemble traced rings into polygons with holes via even-odd nesting."""
    if not rings:
        return []
    polys = [Polygon(r).buffer(0) for r in rings]
    areas = [p.area for p in polys]
    reps = [Polygon(r).representative_point() for r in rings]
    n = len(rings)
    depth = [0] * n
    for i in range(n):
        for j in range(n):
            if i != j and areas[j] > areas[i] and polys[j].contains(reps[i]):
                depth[i] += 1
    out: list[Polygon] = []
    for i in range(n):
        if depth[i] % 2 != 0:
            continue  # this ring is a hole, handled by its parent
        holes = []
        for j in range(n):
            if depth[j] == depth[i] + 1 and areas[i] > areas[j] and polys[i].contains(reps[j]):
                holes.append(rings[j])
        out.append(Polygon(rings[i], holes))
    return out


def _clean_multipolygon(polys: list[Polygon], min_area: float) -> MultiPolygon:
    """Validate, drop degenerate parts, return a MultiPolygon (possibly empty)."""
    good: list[Polygon] = []
    for p in polys:
        if p.is_empty:
            continue
        g = make_valid(p) if not p.is_valid else p
        geoms = getattr(g, "geoms", [g])
        for gg in geoms:
            if isinstance(gg, Polygon) and gg.area >= min_area:
                good.append(gg)
    return MultiPolygon(good) if good else MultiPolygon()


def _key_grid(result: CleanupResult, n_colors: int) -> np.ndarray:
    """Per-pixel surface region key: palette index, _BODY_FILL_KEY, or
    TRANSPARENT for void (transparent + hole-plate)."""
    labels = result.labels
    key = np.full(labels.shape, TRANSPARENT, dtype=np.int32)
    for i in range(n_colors):
        key[labels == i] = i
    key[result.body_fill_mask] = _BODY_FILL_KEY
    # HOLE_PLATE / TRANSPARENT stay void.
    return key


def vectorize(
    result: CleanupResult,
    palette: list[str],
    *,
    mm_per_px: float,
    smoothness: int,
) -> VectorizedArt:
    labels = result.labels
    h = labels.shape[0]
    n = len(palette)
    tol = 0.3 * smoothness * mm_per_px
    min_area = mm_per_px ** 2

    key = _key_grid(result, n)
    surface_keys = [k for k in np.unique(key) if k != TRANSPARENT]

    # Exact (tiled) polygons per surface key.
    exact: dict[int, MultiPolygon] = {}
    for k in surface_keys:
        polys = _rings_to_polygons(_mask_rings(key == k, h, mm_per_px))
        mp = _clean_multipolygon(polys, min_area)
        if not mp.is_empty:
            exact[int(k)] = mp

    if tol > 0 and exact:
        region_by_key = _simplify_partition(exact, tol, min_area)
    else:
        region_by_key = exact

    # Map keys back to output structures.
    color_regions: dict[str, MultiPolygon] = {}
    for i in range(n):
        mp = region_by_key.get(i)
        if mp is not None and not mp.is_empty:
            color_regions[palette[i]] = mp
    body_fill_region = region_by_key.get(_BODY_FILL_KEY)
    if body_fill_region is not None and body_fill_region.is_empty:
        body_fill_region = None

    # Plate footprint: everything not transparent, through-holes kept as holes.
    plate_mask = labels != TRANSPARENT
    plate_polys = _rings_to_polygons(_mask_rings(plate_mask, h, mm_per_px))
    plate_footprint = _clean_multipolygon(plate_polys, min_area)
    if tol > 0 and not plate_footprint.is_empty:
        plate_footprint = _clean_multipolygon(
            [plate_footprint.simplify(tol, preserve_topology=True)], min_area
        )

    # FR-11: the plate must keep disconnected artwork (separate letters) as
    # ONE printable object. When the footprint is multiple islands, span them
    # with the convex hull of the art, re-cutting the through-holes.
    if len(plate_footprint.geoms) > 1:
        solid = unary_union([Polygon(p.exterior) for p in plate_footprint.geoms])
        hull = solid.convex_hull
        holes = [
            Polygon(r) for p in plate_footprint.geoms for r in p.interiors
        ]
        if holes:
            hull = hull.difference(unary_union(holes))
        plate_footprint = _as_multipolygon(make_valid(hull))

    # Translate the whole scene so the artwork bbox bottom-left is (0, 0).
    scene = list(color_regions.values()) + [plate_footprint]
    if body_fill_region is not None:
        scene.append(body_fill_region)
    nonempty = [g for g in scene if not g.is_empty]
    if nonempty:
        minx = min(g.bounds[0] for g in nonempty)
        miny = min(g.bounds[1] for g in nonempty)
    else:
        minx = miny = 0.0

    def shift(g: MultiPolygon) -> MultiPolygon:
        return _as_multipolygon(shp_translate(g, xoff=-minx, yoff=-miny))

    color_regions = {hx: shift(mp) for hx, mp in color_regions.items()}
    if body_fill_region is not None:
        body_fill_region = shift(body_fill_region)
    plate_footprint = shift(plate_footprint)

    if not plate_footprint.is_empty:
        b = plate_footprint.bounds
        width_mm = float(b[2] - b[0])
        height_mm = float(b[3] - b[1])
    else:
        width_mm = height_mm = 0.0

    return VectorizedArt(
        color_regions=color_regions,
        body_fill_region=body_fill_region,
        plate_footprint=plate_footprint,
        width_mm=width_mm,
        height_mm=height_mm,
    )


def _as_multipolygon(geom) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom]) if not geom.is_empty else MultiPolygon()
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(polys) if polys else MultiPolygon()


def _simplify_partition(
    exact: dict[int, MultiPolygon], tol: float, min_area: float
) -> dict[int, MultiPolygon]:
    """Simplify the shared boundary network once, re-polygonize, and reassign
    faces to region keys by their interior point. Preserves shared vertices."""
    boundaries = unary_union([mp.boundary for mp in exact.values()])
    simp = boundaries.simplify(tol, preserve_topology=True)
    faces = list(polygonize(simp))
    if not faces:
        return exact

    keys = list(exact.keys())
    grouped: dict[int, list[Polygon]] = {k: [] for k in keys}
    for face in faces:
        if face.is_empty or face.area < min_area:
            continue
        rp = face.representative_point()
        assigned = None
        for k in keys:
            if exact[k].contains(rp):
                assigned = k
                break
        if assigned is None:
            # Fallback: greatest-overlap key (skip void faces).
            best_k, best_ov = None, 0.0
            for k in keys:
                ov = face.intersection(exact[k]).area
                if ov > best_ov:
                    best_ov, best_k = ov, k
            if best_k is not None and best_ov > 0.5 * face.area:
                assigned = best_k
        if assigned is not None:
            grouped[assigned].append(face)

    out: dict[int, MultiPolygon] = {}
    for k, polys in grouped.items():
        mp = _clean_multipolygon(polys, min_area)
        if not mp.is_empty:
            out[k] = mp
    # Any key that lost all faces to simplification falls back to its exact form.
    for k in keys:
        if k not in out:
            out[k] = exact[k]
    return out
