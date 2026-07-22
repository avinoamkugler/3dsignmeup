"""Solid construction: extrusion, plate, chamfer, part assembly (FR-9..16).

Single entry point ``build_model(art, extrude) -> BuiltModel``.

Implementation strategy (no 3D booleans): the model is sliced along Z into
intervals at every breakpoint (plate top, color tops, cap bottoms / band
boundaries, first-layer height, chamfer layers). Within each interval the 2D
cross-section is a constant set of disjoint colored polygons, computed by a
painter-style assignment (highest-priority claims first, lower ones get the
remainder). Each colored polygon is extruded as a vertical prism and the prisms
are concatenated per part WITHOUT welding vertices, so every part is a union of
individually-watertight closed volumes and stays watertight overall.
"""
from __future__ import annotations

from collections import defaultdict

import trimesh
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
)
from shapely.ops import unary_union

from app.api.schemas import ExtrudeParams
from app.core import hanging
from app.core.swapplan import Band, color_top, compute_plan, snap_z
from app.core.types import BuiltModel, Part, VectorizedArt

_EPS = 1e-6


# --------------------------------------------------------------------------
# shapely helpers
# --------------------------------------------------------------------------
def _iter_polys(geom):
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        yield from geom.geoms
    elif isinstance(geom, GeometryCollection):
        for g in geom.geoms:
            if isinstance(g, (Polygon, MultiPolygon)):
                yield from _iter_polys(g)


def _clean(geom):
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _num_geoms(geom) -> int:
    return sum(1 for p in _iter_polys(geom) if p.area > _EPS)


def _fz(z: float) -> str:
    return f"{z:.2f}"


# --------------------------------------------------------------------------
# extrusion
# --------------------------------------------------------------------------
def _extrude(geom, z0: float, z1: float) -> list:
    h = z1 - z0
    meshes = []
    if h <= _EPS:
        return meshes
    for poly in _iter_polys(_clean(geom)):
        if poly.area <= _EPS:
            continue
        poly = _clean(poly)
        if poly.is_empty or poly.area <= _EPS:
            continue
        try:
            m = trimesh.creation.extrude_polygon(poly, h)
        except Exception:
            continue
        m.apply_translation([0.0, 0.0, z0])
        meshes.append(m)
    return meshes


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------
def build_model(art: VectorizedArt, extrude: ExtrudeParams) -> BuiltModel:
    lh = extrude.layer_height_mm
    flh = extrude.first_layer_height_mm
    body = extrude.main_body_color
    plate_enabled = extrude.plate.enabled
    mode = extrude.mode

    regions = art.color_regions
    bodyfill = _clean(art.body_fill_region)

    # Colors present in the art, in extrude order.
    visible = [c for c in extrude.colors if c.hex in regions and not regions[c.hex].is_empty]

    # Order for sibling tiebreak: more perimeter first, then hex.
    def perim(hexv: str) -> float:
        return regions[hexv].length

    visible_ordered = sorted(visible, key=lambda c: (-perim(c.hex), c.hex))

    plan = compute_plan(extrude, visible_ordered)

    plate_top = 0.0
    if plate_enabled:
        plate_top, _ = snap_z(extrude.plate.thickness_mm, layer_height_mm=lh, first_layer_height_mm=flh)
    base_z = plate_top

    nonflo = [c for c in visible if not c.first_layer_only]
    flo = [c for c in visible if c.first_layer_only]
    flo_hexes = [c.hex for c in flo]

    tops: dict[str, float] = {}
    for c in nonflo:
        top, _, _ = color_top(
            c.height_mm,
            plate_top=plate_top,
            plate_enabled=plate_enabled,
            layer_height_mm=lh,
            first_layer_height_mm=flh,
        )
        tops[c.hex] = top

    # Sibling groups: colors sharing a snapped top. Dominant = first in
    # visible_ordered (highest perimeter).
    grp_members: dict[float, list[str]] = defaultdict(list)
    for c in visible_ordered:
        if c.first_layer_only:
            continue
        grp_members[round(tops[c.hex], 6)].append(c.hex)
    sibling_rank: dict[str, int] = {}
    for _, hexes in grp_members.items():
        for i, hexv in enumerate(hexes):
            sibling_rank[hexv] = i

    # Body top for body-fill and above-first-layer FLO material.
    if plate_enabled:
        body_top = plate_top
    elif body in tops:
        body_top = tops[body]
    else:
        body_top = flh

    # Cap thickness for AMS.
    cap = 3 * lh

    warnings: list[dict] = list(plan.warnings)
    if not plate_enabled and _num_geoms(art.plate_footprint) > 1:
        warnings.append(
            {"code": "no_plate_islands", "msg": "art is multiple islands and no plate joins them"}
        )

    # --- model top (hoisted: hanging needs it) ----------------------------
    band_tops = [b.z1_mm for b in plan.bands] if mode == "swap" else []
    model_top = max([plate_top, flh, body_top] + list(tops.values()) + band_tops + [0.0])

    # --- wall-hanging hardware (keyhole / fins) ---------------------------
    hang_plan = hanging.plan_hangs(
        art,
        extrude,
        tops=tops,
        regions=regions,
        body=body,
        plate_top=plate_top,
        plate_enabled=plate_enabled,
        model_top=model_top,
    )
    warnings.extend(hang_plan.warnings)

    # --- chamfer setup -----------------------------------------------------
    chamfer_on = extrude.chamfer.enabled
    size = 0.0
    size_layers = 0
    chamfer_outline = None
    if chamfer_on:
        size, size_layers = snap_z(extrude.chamfer.size_mm, layer_height_mm=lh, first_layer_height_mm=flh)
        # snap_z reports layers incl. the first layer; chamfer spans size_layers
        # actual layers of height lh starting at bed.
        size_layers = max(1, round(size / lh))
        if plate_enabled:
            chamfer_outline = _clean(art.plate_footprint)
        else:
            outline_parts = [regions[c.hex] for c in visible]
            if bodyfill is not None and not bodyfill.is_empty:
                outline_parts.append(bodyfill)
            chamfer_outline = _clean(unary_union(outline_parts)) if outline_parts else None
        if chamfer_outline is not None and not chamfer_outline.is_empty:
            shrunk = _clean(chamfer_outline.buffer(-size))
            if shrunk.is_empty or _num_geoms(shrunk) < _num_geoms(chamfer_outline):
                warnings.append(
                    {"code": "chamfer_islands", "msg": "chamfer removes small islands from the bottom face"}
                )
        else:
            chamfer_on = False

    # --- Z breakpoints -----------------------------------------------------
    bps = {0.0, flh, plate_top, body_top, model_top}
    bps |= set(tops.values())
    if mode == "ams":
        for c in nonflo:
            if c.hex == body:
                continue
            bps.add(max(base_z, tops[c.hex] - cap))
    else:
        for b in plan.bands:
            bps.add(b.z0_mm)
            bps.add(b.z1_mm)
    if chamfer_on:
        bps.add(size)
        for i in range(size_layers + 1):
            bps.add(round(i * lh, 9))
    for hc in hang_plan.cuts:
        bps.add(hc.z0_mm)
        bps.add(hc.z1_mm)
    bps = sorted(z for z in bps if -_EPS <= z <= model_top + _EPS)

    # --- band lookup (swap) -----------------------------------------------
    def band_of(z: float) -> Band:
        for b in plan.bands:
            if b.z0_mm - _EPS <= z < b.z1_mm - _EPS:
                return b
        if plan.bands:
            last = plan.bands[-1]
            if z >= last.z1_mm - _EPS:
                return last
        return Band(0.0, model_top, body)

    # --- assignment: disjoint (poly, color, part_key, part_name) at z ------
    def assignment(z: float):
        # Total material footprint at height z.
        solids = []
        if plate_enabled and z < plate_top - _EPS:
            solids.append(art.plate_footprint)
        if z >= base_z - _EPS:
            for c in nonflo:
                if tops[c.hex] > z + _EPS:
                    solids.append(regions[c.hex])
            if bodyfill is not None and not bodyfill.is_empty and z < body_top - _EPS:
                solids.append(bodyfill)
            for hexv in flo_hexes:
                if flh - _EPS <= z < body_top - _EPS:
                    solids.append(regions[hexv])
        if z < flh - _EPS:
            for hexv in flo_hexes:
                solids.append(regions[hexv])

        if not solids:
            return []
        S = _clean(unary_union(solids))
        if S.is_empty:
            return []

        out = []
        claimed = None

        def take(reg, color, key, name):
            nonlocal claimed
            reg = _clean(reg)
            if reg is None or reg.is_empty:
                return
            eff = reg if claimed is None else _clean(reg.difference(claimed))
            claimed = reg if claimed is None else _clean(claimed.union(reg))
            if eff is None or eff.is_empty or eff.area <= _EPS:
                return
            out.append((eff, color, key, name))

        # 1. first-layer-only colors show their own color in layer 1.
        if z < flh - _EPS:
            for c in flo:
                take(regions[c.hex], c.hex, ("first", c.hex), f"first_{c.hex}")

        # 2. swap-mode sibling colors (non-dominant) print their own color
        #    across their whole band.
        if mode == "swap" and z >= base_z - _EPS:
            for c in nonflo:
                if sibling_rank.get(c.hex, 0) == 0:
                    continue
                t = tops[c.hex]
                b = next(
                    (bb for bb in plan.bands if abs(bb.z1_mm - t) < _EPS),
                    band_of(0.5 * (base_z + t)),
                )
                if b.z0_mm - _EPS <= z < t - _EPS:
                    take(
                        regions[c.hex],
                        c.hex,
                        ("band", c.hex, b.z0_mm, b.z1_mm),
                        f"band_{c.hex}_z{_fz(b.z0_mm)}-{_fz(b.z1_mm)}",
                    )

        # 3. AMS caps.
        if mode == "ams" and z >= base_z - _EPS:
            for c in nonflo:
                if c.hex == body:
                    continue
                t = tops[c.hex]
                cb = max(base_z, t - cap)
                if cb - _EPS <= z < t - _EPS:
                    take(
                        regions[c.hex],
                        c.hex,
                        ("cap", c.hex, cb, t),
                        f"cap_{c.hex}_z{_fz(cb)}-{_fz(t)}",
                    )

        # 4. remainder -> body (AMS) / band filament (swap).
        rem = S if claimed is None else _clean(S.difference(claimed))
        if rem is not None and not rem.is_empty and rem.area > _EPS:
            if mode == "swap":
                b = band_of(z)
                out.append(
                    (
                        rem,
                        b.hex,
                        ("band", b.hex, b.z0_mm, b.z1_mm),
                        f"band_{b.hex}_z{_fz(b.z0_mm)}-{_fz(b.z1_mm)}",
                    )
                )
            else:
                out.append(
                    (rem, body, ("body", body), f"body_{body}_z{_fz(0.0)}-{_fz(model_top)}")
                )
        return out

    # --- iterate intervals, extrude, accumulate ---------------------------
    part_meshes: dict[tuple, list] = defaultdict(list)
    part_color: dict[tuple, str] = {}
    part_name: dict[tuple, str] = {}

    for za, zb in zip(bps, bps[1:]):
        if zb - za <= _EPS:
            continue
        zmid = 0.5 * (za + zb)
        in_chamfer = chamfer_on and zb <= size + _EPS
        clip = None
        if in_chamfer:
            off = za - size  # bottom of layer; z=0 -> buffer(-size) bed face
            clip = _clean(chamfer_outline.buffer(off)) if off < -_EPS else chamfer_outline

        # Union of every hang cut whose [z0, z1) overlaps this slice.
        hang_neg = None
        for hc in hang_plan.cuts:
            if hc.z1_mm > za + _EPS and hc.z0_mm < zb - _EPS:
                hang_neg = hc.negative if hang_neg is None else _clean(hang_neg.union(hc.negative))

        for poly, color, key, name in assignment(zmid):
            if clip is not None:
                poly = _clean(poly.intersection(clip))
                if poly is None or poly.is_empty or poly.area <= _EPS:
                    continue
            if hang_neg is not None:
                poly = _clean(poly.difference(hang_neg))
                if poly is None or poly.is_empty or poly.area <= _EPS:
                    continue
            meshes = _extrude(poly, za, zb)
            if meshes:
                part_meshes[key].extend(meshes)
                part_color[key] = color
                part_name[key] = name

    parts: list[Part] = []
    for key in part_meshes:
        meshes = part_meshes[key]
        if not meshes:
            continue
        mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        parts.append(Part(name=part_name[key], color=part_color[key], mesh=mesh))
    parts.extend(hang_plan.extra_parts)
    parts.sort(key=lambda p: p.name)

    return BuiltModel(
        parts=parts,
        swap_plan=list(plan.events),
        n_swaps=plan.n_swaps,
        snapped_heights=plan.snapped_heights,
        warnings=warnings,
    )
