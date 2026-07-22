"""Wall-hanging hardware: parametric keyhole slots and kickstand fins. STUB.

Both hang modes cut a small cavity into the back (bed-facing, Z=0) surface
of the plate/body volume. Per the "no 3D booleans" constraint (see
geometry.py's module docstring), cavities are built as a short stack of 2D
cross-section cuts at specific Z-bands — exactly the representation
geometry.py already slices the whole model into — NOT as a mesh boolean
against an imported STL. `plan_hangs()` returns those cuts pre-positioned in
the model's global XY (mm); geometry.py subtracts them from whatever
cross-section it computes in the overlapping Z-range, and appends any extra
standalone parts (fins) to its part list. See "Integration into geometry.py"
at the bottom of this docstring for the exact splice points.

Single entry point
-------------------
plan_hangs(art, extrude, *, tops, regions, body, plate_top, plate_enabled,
           model_top) -> HangPlan

- `art`: VectorizedArt (for width_mm/height_mm and plate_footprint).
- `extrude`: ExtrudeParams (reads `extrude.hang`).
- `tops`, `regions`, `body`: the SAME dicts geometry.build_model already
  computed (nonflo color hex -> absolute snapped top Z; art.color_regions;
  main_body_color) — passed in so this module never recomputes swap-plan
  logic itself.
- `plate_top`, `plate_enabled`, `model_top`: as computed in build_model.

Both hang modes REQUIRE `plate_enabled`. If the user picked a hang mode
with the plate off, return a HangPlan with no cuts/parts and a single
warning (code "hang_requires_plate") — do not attempt no-plate placement.

Depth-availability rule (per product decision)
------------------------------------------------
For a candidate cavity footprint (a shapely polygon in global XY), define
its "ceiling":

    ceiling = min(tops[hexv] for hexv in tops
                  if regions[hexv].intersects(footprint))
              , defaulting to model_top if nothing intersects.

This is "how far up from the bed you can hollow before hitting a
different-colored visible region" — i.e. plate thickness PLUS whatever
contiguous main-body-colored volume sits directly above the plate at that
XY (exactly what the body/plate "remainder" cross-section in build_model's
assignment() covers). A cavity of total depth D is placeable at that
footprint iff `ceiling >= D + SAFETY_MARGIN_MM`.

Placement / fallback cascade (both keyhole and fins, same shape)
-------------------------------------------------------------------
1. Try the two-sided placement (left + right anchors). Build both exact
   footprint polygons; require (a) each fits within
   `[margin, width_mm-margin] x [margin, height_mm-margin]` of the art
   bounding box, (b) the two footprints don't overlap, (c) both pass the
   depth-availability check above. Use shapely for the fit checks —
   do not hand-derive numeric thresholds, compute against the actual
   art.width_mm/height_mm.
2. Else try one centered placement (same checks, single footprint at the
   horizontal center).
3. Else: no cavity/parts are added; emit a warning explaining which check
   failed ("too small" vs "not enough depth") — use codes
   "hang_fallback_single" (2->1) and "hang_skipped" (1->0), with a
   human-readable `msg` naming the specific reason.

Both features anchor their Y (vertical) position independently per the
constants below; X anchors reuse the same left/right fractions for both.

===========================================================================
2a/2b — Keyhole slot (parametric reproduction of a real screw-keyhole
hanger; measured from a reference STL rather than imported as a mesh)
===========================================================================

Real-world mechanism (why the numbers below are NOT arbitrary): you push a
screw's head through the round "bulb" opening at the back surface, into a
roomy relief pocket behind it; sliding the sign down moves the screw's
SHANK into the narrower "slot" above the bulb's position while the HEAD,
too wide for the slot, stays caught in the relief pocket below the now-
narrower ceiling. Gravity holds it there. This means, in LOCAL footprint
coordinates (+Y = up the sign), **the bulb must be at the TOP of the
footprint and the slot must taper away going DOWN** — get this backwards
and the hardware doesn't work (the sign falls off instead of hanging).

Two stacked layers, cut from the back surface (Z=0) upward:

    KEYHOLE_WIDTH_MM        = 10.0   # X extent of the whole footprint
    KEYHOLE_LENGTH_MM       = 17.0   # Y extent (vertical when placed)
    KEYHOLE_BULB_DIAM_MM    = 10.0   # circular bulb, at the TOP of the footprint
    KEYHOLE_SLOT_WIDTH_MM   = 4.0    # slot width, tapering to a point at the BOTTOM
    KEYHOLE_LAYER1_DEPTH_MM = 4.0    # Z 0->4mm: the keyhole profile itself (bulb+slot)
    KEYHOLE_LAYER2_DEPTH_MM = 4.0    # Z 4->8mm: wide oval relief pocket, full footprint
    KEYHOLE_TOTAL_DEPTH_MM  = 8.0
    SAFETY_MARGIN_MM        = 1.0    # required leftover material beyond the cavity
    KEYHOLE_FILLET_MM       = 1.0    # target rounding radius, both transitions below
    KEYHOLE_Y_FRAC          = 0.75   # anchor Y = 0.75 * art.height_mm (both placements)
    KEYHOLE_X_FRAC          = 0.125  # anchor X = 0.125 * width (left), mirror (right)
    EDGE_MARGIN_MM          = 3.0    # min clearance used in the fit checks above

Layer 1 (Z 0->4mm) cross-section: the keyhole silhouette itself — a circle
of diameter KEYHOLE_BULB_DIAM_MM centered `bulb_diam/2` below the
footprint's top edge, unioned with a KEYHOLE_SLOT_WIDTH_MM-wide channel
running down from the bulb's center to a point near the footprint's bottom
edge (a tapered polygon, e.g. a thin isoceles triangle/trapezoid — exact
taper shape is a free choice, "narrowing to a point" is the only
requirement).

Layer 2 (Z 4->8mm) cross-section: a single oval/rounded-rectangle spanning
the FULL footprint (KEYHOLE_WIDTH_MM x KEYHOLE_LENGTH_MM) — this is the
"less detailed, bigger" face that ends up embedded inside the material,
away from any external surface.

Rounding (2b — confirmed): replace the sharp step between layer 1 and
layer 2, and the sharp entry rim at Z=0, with graduated profiles across a
couple of extra thin Z-slices (e.g. Z in [0, 1mm] blending from a
KEYHOLE_FILLET_MM-larger entry profile down to the true layer-1 profile;
Z in [3mm, 5mm] blending layer-1's profile out to layer-2's wider oval) —
approximating a quarter-torus fillet within the existing per-Z-slice 2D
model. No literal torus primitive needed; a handful of intermediate
`shapely` `buffer()` amounts interpolated across 2-3 sub-slices is the
expected implementation and is sufficient — the goal is "no unbroken 90
degree internal step greater than ~1mm in a single slice," not a
mathematically exact torus.

===========================================================================
2c — Kickstand fins (v3 — corrected LEAN MECHANICS; v2's X-spread tab fix
is preserved unchanged, see below)
===========================================================================

REVISION HISTORY:
- v1 (broken): 2 round pins spaced along the vertical attach edge — i.e.
  stacked one above the other, both at the same horizontal position. Two
  pins collinear on a vertical line give zero resistance to the fin
  twisting sideways (rotating about that very line) — exactly the load a
  leaning sign puts on its prop.
- v2 (fixed the pin bug, introduced a NEW bug): spread the 2 tabs across a
  wedge's width (correct — this part is KEPT in v3 verbatim). But v2 built
  the wedge as a right triangle with a 90-degree angle between its "table
  leg" and "attach leg", attach leg flush with the sign's back. That right
  angle is itself wrong: if the table leg lies flat on the table
  (horizontal), the attach leg — being perpendicular to it — is FORCED to
  be exactly vertical. Since the attach leg is flush with the sign's back,
  this means the sign can never actually lean while the foot sits flush on
  the table; `fin_angle_deg` only changed how far the foot poked out, not
  the sign's tilt. v3 fixes this with a proper mechanical derivation.

v3 mechanics (confirmed stance: the sign's own bottom edge ALSO rests on
the table, classic picture-frame-easel-back style — NOT a fully cantilevered
mount off the tabs alone). Work in a 2D "world side view" plane (looking
along the sign's width axis): Uf = world-horizontal, Vf = world-vertical
(true up, i.e. gravity-vertical — NOT the sign's own local up). Put the
sign's bottom edge, which touches the table, at the origin (0, 0) in this
view; Uf=0 is directly below it.

- The sign's own "up" direction, tilted back by `angle_deg =
  extrude.hang.fin_angle_deg` from true vertical, is the unit vector
  `(sin(angle_deg), cos(angle_deg))` in (Uf, Vf).
- The fin attaches at a FIXED height above the sign's bottom edge:
  `FIN_ANCHOR_HEIGHT_MM = 25.0` (replaces v2's `FIN_Y_FRAC * height_mm`
  mid-height anchor — v3 always anchors near the bottom, like a real easel
  leg, and this also keeps the derived strut short). In world coords the
  attach point is at `FIN_ANCHOR_HEIGHT_MM * (sin(angle_deg),
  cos(angle_deg))`.
- The fin's own leg down to the table must be a PLAIN VERTICAL STRUT (i.e.
  parallel to true Vf, NOT perpendicular to the sign's back) — this is the
  only leg orientation whose foot lands on the table (Vf=0) directly below
  the attach point while ALSO sitting flush (a flat foot face, not just a
  corner). Strut length:

      FIN_STRUT_LEN_MM(angle_deg) = FIN_ANCHOR_HEIGHT_MM * cos(radians(angle_deg))

  (25.0mm at 0 degrees, down to ~17.7mm at 45 degrees — always shorter
  than v1, still a small fixed-ish part per the "keep fins small" request,
  now for a mechanically real reason instead of an arbitrary constant.)
- The strut's foot sits `FIN_ANCHOR_HEIGHT_MM * sin(angle_deg)` in the +Uf
  direction relative to the sign's own bottom edge — i.e. set back behind
  it, exactly like a real easel-back foot. (Informational — this offset is
  a consequence of the geometry below, not something you need to build
  directly; it's here so a sanity-check test can confirm the mechanism is
  self-consistent.)
- The 2 attachment tabs must point along the sign's back-NORMAL (not along
  its face) — i.e. perpendicular to `(sin(angle_deg), cos(angle_deg))`,
  specifically in direction `(-cos(angle_deg), sin(angle_deg))` (this is
  "straight into the panel" at angle_deg=0, tilting to match as the sign
  tilts). Get this direction backwards and the tabs point out of the sign
  instead of into it.

Constants (Xf = width axis, unchanged in spirit from v2 — this is still
the axis the 2 tabs spread across, becoming the sign's own X axis):

    FIN_ANCHOR_HEIGHT_MM  = 25.0  # fixed height above the sign's bottom
                                   #   edge where the fin attaches (NEW —
                                   #   replaces v2's FIN_Y_FRAC mid-height)
    FIN_WIDTH_MM          = 16.0  # unchanged from v2 — tab-spread axis
    FIN_STRUT_THICKNESS_MM = 6.0  # strut's own Uf-thickness (NEW)
    FIN_COLLAR_HEIGHT_MM  = 12.0  # short WIDER block at the strut's top,
                                   #   fixed size regardless of angle_deg,
                                   #   sized to fully contain the rotated
                                   #   mounting pad below at any angle up
                                   #   to 45 degrees (NEW — see below)
    FIN_COLLAR_EXTRA_MM   = 6.0   # how much wider the collar is than the
                                   #   plain strut, on the +Uf side only
    FIN_TAB_SPACING_MM    = 8.0   # UNCHANGED from v2
    FIN_TAB_BASE_MM       = 4.0   # UNCHANGED from v2
    FIN_TAB_HEIGHT_MM     = 4.0   # UNCHANGED from v2
    FIN_TAB_REACH_MM      = 3.5   # UNCHANGED from v2 (41-degree ramp)
    FIN_TAB_CLEARANCE_MM  = 0.2   # UNCHANGED from v2
    FIN_X_FRAC            = 0.125 # UNCHANGED — same left/right fractions
    FIN_MARGIN_MM         = 1.0   # UNCHANGED

Construction (modular — build these as 3 sub-shapes, concatenate, no
vertex welding, same as everywhere else in this codebase):

1. STRUT: a plain rectangular beam, Xf in [0, FIN_WIDTH_MM], Uf in
   [-FIN_STRUT_THICKNESS_MM/2, +FIN_STRUT_THICKNESS_MM/2], Vf in [0,
   FIN_STRUT_LEN_MM(angle_deg) - FIN_COLLAR_HEIGHT_MM]. Constant cross
   section — trivially support-free when printed with Vf as print-up
   (matches its own long axis exactly, no rotation needed for this piece).
   Vf=0 (the foot) is a single flat plane spanning the full Xf x Uf
   footprint — verify this in tests, it's what makes the part rest flush
   on the table.

2. COLLAR: same idea, but for the top FIN_COLLAR_HEIGHT_MM of the strut's
   total length, widen the Uf range to [-FIN_STRUT_THICKNESS_MM/2,
   +FIN_STRUT_THICKNESS_MM/2 + FIN_COLLAR_EXTRA_MM] (wider only on the
   +Uf side — this is the side the sign's material extends toward, per
   the tilt derivation above). Taper this widening in over the bottom
   ~3mm of the collar (a simple sloped transition, same "no unbroken step"
   philosophy as the keyhole fillets and v2's tab ramps — a linear taper
   from the strut's own width up to the full collar width is sufficient,
   exact interpolation shape is a free choice) so there's no sharp
   unsupported ledge. The collar's fixed size (independent of the actual
   `angle_deg` used) exists purely so the rotated mounting pad in step 3
   always lands fully within solid material below it, at any angle from 0
   to 45 degrees — this is a deliberate simplification to avoid deriving
   an angle-dependent taper; a small amount of excess material at low
   angles is an acceptable tradeoff for a small support part.

3. MOUNTING PAD + 2 TABS: build this exactly like v2's attach-wall-plus-
   tabs sub-assembly, in ITS OWN small local frame first (a short wall
   segment spanning Xf in [0, FIN_WIDTH_MM], some small height range in
   its own local "up" axis (say FIN_TAB_HEIGHT_MM + 2mm margin), with the
   2 tabs protruding from its outward local face exactly per v2's ramp
   construction: flush with the wall along the tab's whole local height,
   reaching full FIN_TAB_REACH_MM protrusion only at the top of its own
   small range — this sub-geometry is UNCHANGED from v2, just relocate it
   here). Then ROTATE this whole pad+tabs sub-assembly by `angle_deg`
   about the Xf (width) axis, and TRANSLATE it so its own local origin
   (the bottom-center of its wall, before rotation) lands at the top-
   center of the collar (Uf = FIN_STRUT_THICKNESS_MM/2 +
   FIN_COLLAR_EXTRA_MM/2-ish — pick the point on the collar's own tilt-
   facing side that keeps the pad's rotated footprint fully inside the
   collar's cross-section — Vf = FIN_STRUT_LEN_MM(angle_deg)). After this
   rotation, the tabs' growth direction (which was "straight out from the
   wall" in the pad's own local frame) becomes `(-cos(angle_deg),
   sin(angle_deg))` in the (Uf, Vf) frame — this MUST match the "into the
   panel" direction derived above; if your rotation sign convention gives
   the opposite direction, flip it.

Build each fin as one trimesh part (strut + collar + rotated pad+tabs,
concatenated). Translate it in XY to sit beside the sign's own footprint
exactly as v1/v2 did (offset past `art.plate_footprint.bounds`'s max-x by
a small gap, second fin offset further along). Name the parts
"fin_left"/"fin_right"/"fin_center"; color = extrude.main_body_color.

The matching cavity cut into the sign's back is UNCHANGED from v2 — it
only depends on the tab's own local shape (the shrinking-rectangle
approximation of the tapered pocket, per v2's `_fin_tab_cuts`), not on how
the rest of the fin's body is shaped. Only the ANCHOR position feeding
into it changes: use `ay = FIN_ANCHOR_HEIGHT_MM` (fixed) instead of v2's
`FIN_Y_FRAC * h`.

VERIFICATION THIS TIME MUST INCLUDE an assembled-frame consistency check —
this is the check that would have caught both v1 and v2's mechanical bugs,
and is more important than any single dimension: numerically place the
fin's own mesh at the derived world position/orientation (attach point at
`FIN_ANCHOR_HEIGHT_MM * (sin(angle_deg), cos(angle_deg))`, i.e. verify the
mesh's own Vf=0 foot face, when the WHOLE fin mesh is left exactly as
built (Uf, Vf already ARE the world side-view axes, no further transform
needed since this construction is done directly in assembled-frame terms)
sits at world Vf=0 (the table) — trivially true by construction, but
assert it — AND separately verify, via simple trig, that if you also stood
a same-length "virtual sign leg" (length FIN_ANCHOR_HEIGHT_MM, direction
(sin, cos)(angle_deg)) from the world origin, its tip lands exactly on the
fin's own top-of-collar attach point (Uf, Vf) — i.e. the two pieces
actually meet where they should. Also verify the tab growth direction
numerically (tip vertex minus base vertex of a tab, normalized) matches
`(-cos(angle_deg), sin(angle_deg))` within a small tolerance, for at least
2 different angle_deg values (e.g. 10 and 40 degrees) so a sign error in
the rotation direction can't hide at a symmetric test angle.

===========================================================================
Return contract
===========================================================================
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import trimesh
from shapely.affinity import scale
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from app.api.schemas import ExtrudeParams
from app.core.types import Part, VectorizedArt

_EPS = 1e-6

# --- keyhole (2a/2b) constants (measured from the reference STL) -----------
KEYHOLE_WIDTH_MM = 10.0
KEYHOLE_LENGTH_MM = 17.0
KEYHOLE_BULB_DIAM_MM = 10.0
KEYHOLE_SLOT_WIDTH_MM = 4.0
KEYHOLE_LAYER1_DEPTH_MM = 4.0
KEYHOLE_LAYER2_DEPTH_MM = 4.0
KEYHOLE_TOTAL_DEPTH_MM = 8.0
SAFETY_MARGIN_MM = 1.0
KEYHOLE_FILLET_MM = 1.0
KEYHOLE_Y_FRAC = 0.75
KEYHOLE_X_FRAC = 0.125
EDGE_MARGIN_MM = 3.0

# --- fin (2c, v3 — vertical strut + collar + rotated mounting pad) ---------
# Mesh-local axis convention (used directly as the assembled "world side view"
# frame — no separate print-transform this time):
#   mesh X = Xf  = width / tab-spread axis (== the sign's own X axis)
#   mesh Y = Uf  = world-horizontal in the side view
#   mesh Z = Vf  = world-vertical (true up); the foot rests at Vf == 0
# The tab growth direction and the lean derivation both live in the (Uf, Vf)
# = (mesh Y, mesh Z) plane.
FIN_ANCHOR_HEIGHT_MM = 25.0   # fixed height above the sign's bottom edge where
                              #   the fin attaches (v3 — replaces v2's FIN_Y_FRAC)
FIN_WIDTH_MM = 16.0           # tab-spread axis (Xf) — unchanged from v2
FIN_STRUT_THICKNESS_MM = 6.0  # strut's own Uf-thickness
FIN_COLLAR_HEIGHT_MM = 12.0   # short wider block at the strut's top
FIN_COLLAR_EXTRA_MM = 6.0     # how much wider the collar is than the strut (+Uf)
FIN_TAB_SPACING_MM = 8.0   # tab center-to-center across FIN_WIDTH_MM
FIN_TAB_BASE_MM = 4.0      # each tab's width along that same axis
FIN_TAB_HEIGHT_MM = 4.0    # tab height range on the mounting-pad wall
FIN_TAB_REACH_MM = 3.5     # max protrusion from the wall (< height -> <=45deg)
FIN_TAB_CLEARANCE_MM = 0.2  # gap for a press fit
FIN_X_FRAC = 0.125
FIN_MARGIN_MM = 1.0

_QUAD_SEGS = 64


@dataclass
class HangCut:
    """A 2D negative to subtract from the model's cross-section within
    [z0_mm, z1_mm), already positioned in the model's global XY (mm)."""

    z0_mm: float
    z1_mm: float
    negative: MultiPolygon


@dataclass
class HangPlan:
    cuts: list[HangCut] = field(default_factory=list)
    extra_parts: list[Part] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# shapely helpers
# ---------------------------------------------------------------------------
def _as_mp(geom) -> MultiPolygon:
    """Coerce any polygonal geometry into a (possibly empty) MultiPolygon."""
    if geom is None or geom.is_empty:
        return MultiPolygon([])
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    return MultiPolygon(polys)


def _fits(footprint, width_mm: float, height_mm: float, margin: float) -> bool:
    """True when footprint's bounds sit within the margin-inset art rectangle."""
    x0, y0, x1, y1 = footprint.bounds
    return (
        x0 >= margin - _EPS
        and y0 >= margin - _EPS
        and x1 <= width_mm - margin + _EPS
        and y1 <= height_mm - margin + _EPS
    )


def _ceiling(
    footprint, tops: dict[str, float], regions: dict[str, MultiPolygon], model_top: float
) -> float:
    """Max Z (from the bed) hollowable before breaking through a visible color.

    The lowest top among visible color regions overlapping the footprint caps
    the available material column; nothing overlapping -> full model height.
    """
    ceiling = model_top
    for hexv, top in tops.items():
        reg = regions.get(hexv)
        if reg is not None and not reg.is_empty and reg.intersects(footprint):
            ceiling = min(ceiling, top)
    return ceiling


# ---------------------------------------------------------------------------
# keyhole geometry
# ---------------------------------------------------------------------------
def _keyhole_footprint(cx: float, cy: float) -> Polygon:
    """Nominal WIDTH x LENGTH footprint box, centered on the anchor."""
    return box(
        cx - KEYHOLE_WIDTH_MM / 2,
        cy - KEYHOLE_LENGTH_MM / 2,
        cx + KEYHOLE_WIDTH_MM / 2,
        cy + KEYHOLE_LENGTH_MM / 2,
    )


def _keyhole_layer1(cx: float, cy: float) -> Polygon:
    """The keyhole silhouette: bulb at the TOP, slot tapering to the bottom."""
    y_top = cy + KEYHOLE_LENGTH_MM / 2
    y_bot = cy - KEYHOLE_LENGTH_MM / 2
    bulb_cy = y_top - KEYHOLE_BULB_DIAM_MM / 2
    bulb = Point(cx, bulb_cy).buffer(KEYHOLE_BULB_DIAM_MM / 2, quad_segs=_QUAD_SEGS)
    # Slot: full width at the bulb center, narrowing to a point near the bottom.
    hw = KEYHOLE_SLOT_WIDTH_MM / 2
    slot = Polygon(
        [
            (cx - hw, bulb_cy),
            (cx + hw, bulb_cy),
            (cx, y_bot + 0.5),
        ]
    )
    prof = unary_union([bulb, slot])
    if isinstance(prof, MultiPolygon):
        prof = prof.buffer(0)
    return prof


def _keyhole_layer2(cx: float, cy: float) -> Polygon:
    """Full-footprint oval relief pocket (the wider, embedded face)."""
    circle = Point(cx, cy).buffer(1.0, quad_segs=_QUAD_SEGS)
    return scale(circle, KEYHOLE_WIDTH_MM / 2, KEYHOLE_LENGTH_MM / 2)


def _keyhole_cuts(cx: float, cy: float) -> list[HangCut]:
    """Stacked back-surface cuts with graduated fillet transitions.

    Z layout (from the bed up):
      [0, fillet]         entry countersink: layer1 buffered +FILLET -> layer1
      [fillet, 3]         the true keyhole silhouette (layer1)
      [3, 5]              blend layer1 out to the layer2 oval pocket
      [5, TOTAL_DEPTH]    the layer2 oval pocket
    Each blend is a handful of thin slices with interpolated shapes so no
    single slice has an unbroken ~90-degree internal step > ~1mm.
    """
    layer1 = _keyhole_layer1(cx, cy)
    layer2 = _keyhole_layer2(cx, cy)
    cuts: list[HangCut] = []

    fillet = KEYHOLE_FILLET_MM
    # Entry fillet [0, fillet]: widest at the bed, shrinking to layer1.
    n_entry = 2
    dz = fillet / n_entry
    for i in range(n_entry):
        z0 = round(i * dz, 9)
        z1 = round((i + 1) * dz, 9)
        amt = fillet * (1.0 - z0 / fillet)  # buffer at the slice bottom (widest)
        prof = layer1.buffer(amt) if amt > _EPS else layer1
        cuts.append(HangCut(z0, z1, _as_mp(prof)))

    # True layer1 [fillet, 3].
    cuts.append(HangCut(round(fillet, 9), 3.0, _as_mp(layer1)))

    # Blend [3, 5]: layer1 unioned with a growing erosion of the oval pocket.
    n_blend = 10
    z_lo, z_hi = 3.0, 5.0
    shrink = KEYHOLE_LENGTH_MM / 2  # enough to empty the oval at t=0
    dzb = (z_hi - z_lo) / n_blend
    for i in range(n_blend):
        z0 = round(z_lo + i * dzb, 9)
        z1 = round(z_lo + (i + 1) * dzb, 9)
        t = (z1 - z_lo) / (z_hi - z_lo)  # slice top: conservatively wider
        pocket = layer2.buffer(-(1.0 - t) * shrink)
        prof = unary_union([layer1, pocket]) if not pocket.is_empty else layer1
        cuts.append(HangCut(z0, z1, _as_mp(prof)))

    # Layer2 pocket [5, TOTAL_DEPTH].
    cuts.append(HangCut(5.0, KEYHOLE_TOTAL_DEPTH_MM, _as_mp(layer2)))
    return cuts


# ---------------------------------------------------------------------------
# fin geometry
# ---------------------------------------------------------------------------
# Permutation that maps a Z-extruded prism's local axes (u, v, w) to the fin's
# own mesh frame (X, Y, Z) = (w, u, v): a 2D profile drawn in the (Uf, Vf)
# side-view plane (u=Uf, v=Vf) and extruded along w becomes width=mesh X,
# Uf=mesh Y, Vf=mesh Z.
_WEDGE_XFORM = np.array(
    [
        [0.0, 0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
)

# Local up-range of the mounting pad's own wall (before it is rotated).
_FIN_PAD_HEIGHT_MM = FIN_TAB_HEIGHT_MM + 2.0
# Wall thickness of the mounting pad (Uf, on the +Uf side of the tab face).
_FIN_PAD_WALL_MM = 3.0


def _fin_strut_len_mm(angle_deg: float) -> float:
    """Vertical strut length: the height the fin must rise so its top reaches
    the sign's attach point when the sign leans back by angle_deg."""
    return FIN_ANCHOR_HEIGHT_MM * math.cos(math.radians(angle_deg))


def _fin_setback_mm(angle_deg: float) -> float:
    """How far behind the sign's bottom edge (+Uf) the vertical strut's foot
    sits — a consequence of the easel-back geometry (see docstring 2c)."""
    return FIN_ANCHOR_HEIGHT_MM * math.sin(math.radians(angle_deg))


def _fin_tab_local_xs() -> list[float]:
    """The two tab center positions across FIN_WIDTH_MM (width/Xf coords)."""
    mid = FIN_WIDTH_MM / 2
    return [mid - FIN_TAB_SPACING_MM / 2, mid + FIN_TAB_SPACING_MM / 2]


def _extrude_profile(profile: Polygon, width: float) -> trimesh.Trimesh:
    """Extrude a 2D (Uf, Vf) profile across `width`, mapped to the fin's mesh
    frame (mesh X=width, mesh Y=Uf, mesh Z=Vf)."""
    prism = trimesh.creation.extrude_polygon(profile, width)
    prism.apply_transform(_WEDGE_XFORM)
    return prism


def _build_fin_mesh(angle_deg: float):
    """Build one v3 fin in its own mesh frame (which IS the assembled world
    side-view frame): mesh X = width (Xf), mesh Y = Uf (world-horizontal),
    mesh Z = Vf (world-vertical, foot at Z=0).

    Three concatenated sub-shapes (no vertex welding, per the codebase's
    watertight-union-of-closed-prisms approach):

    1. STRUT: a plain vertical rectangular beam, constant cross-section,
       centered at Uf = setback, spanning Vf in [0, strut_len - collar]. Its
       Vf=0 foot is a single flat plane -> rests flush on the table.
    2. COLLAR: the top FIN_COLLAR_HEIGHT_MM of the strut, widened on the +Uf
       side by FIN_COLLAR_EXTRA_MM, tapered in over the bottom ~3mm so there
       is no unsupported ledge.
    3. MOUNTING PAD + 2 TABS: v2's attach-wall-plus-ramp-tabs sub-assembly,
       built in its own local frame, rotated rigidly by angle_deg about the
       width (mesh X) axis, and translated so the pad's local origin lands on
       the strut's top-center (the attach point). After rotation the tabs grow
       along (-cos, sin)(angle_deg) in (Uf, Vf) — "into the panel".

    Returns (mesh, strut_len_mm).
    """
    strut_len = _fin_strut_len_mm(angle_deg)
    setback = _fin_setback_mm(angle_deg)
    half_t = FIN_STRUT_THICKNESS_MM / 2
    collar_bot = strut_len - FIN_COLLAR_HEIGHT_MM
    taper_h = min(3.0, FIN_COLLAR_HEIGHT_MM)

    # 1. STRUT — profile in (Uf, Vf), centered at Uf=0 for now.
    strut2d = box(-half_t, 0.0, half_t, collar_bot)
    meshes = [_extrude_profile(strut2d, FIN_WIDTH_MM)]

    # 2. COLLAR — same beam over the top FIN_COLLAR_HEIGHT_MM, widened +Uf.
    collar2d = Polygon(
        [
            (-half_t, collar_bot),
            (half_t, collar_bot),
            (half_t + FIN_COLLAR_EXTRA_MM, collar_bot + taper_h),
            (half_t + FIN_COLLAR_EXTRA_MM, strut_len),
            (-half_t, strut_len),
        ]
    )
    meshes.append(_extrude_profile(collar2d, FIN_WIDTH_MM))

    # 3. MOUNTING PAD + 2 TABS — built in its own local (Uf, Vf) frame with
    # origin at the bottom-center of the wall's outward (tab) face (Uf=0).
    # The wall body sits on the +Uf side; tabs protrude toward -Uf, reaching
    # full FIN_TAB_REACH_MM only at the top of their height range (v2 ramp).
    pad_meshes = []
    wall2d = box(0.0, 0.0, _FIN_PAD_WALL_MM, _FIN_PAD_HEIGHT_MM)
    pad_meshes.append(_extrude_profile(wall2d, FIN_WIDTH_MM))

    tab_bot = (_FIN_PAD_HEIGHT_MM - FIN_TAB_HEIGHT_MM) / 2
    tab_top = tab_bot + FIN_TAB_HEIGHT_MM
    for tx in _fin_tab_local_xs():
        tab2d = Polygon(
            [
                (0.0, tab_bot),
                (0.0, tab_top),
                (-FIN_TAB_REACH_MM, tab_top),
            ]
        )
        prism = _extrude_profile(tab2d, FIN_TAB_BASE_MM)
        prism.apply_translation([tx - FIN_TAB_BASE_MM / 2, 0.0, 0.0])
        pad_meshes.append(prism)

    pad = trimesh.util.concatenate(pad_meshes)
    # Rotate the whole rigid pad about the width (mesh X) axis. In the (Uf, Vf)
    # = (mesh Y, mesh Z) plane trimesh's rotation by alpha acts as
    # [[cos a, -sin a], [sin a, cos a]]; we need [[cos, sin], [-sin, cos]] so
    # that the tab growth (-1, 0) maps to (-cos, sin) -> use alpha = -angle.
    rot = trimesh.transformations.rotation_matrix(
        -math.radians(angle_deg), [1.0, 0.0, 0.0]
    )
    pad.apply_transform(rot)
    # Land the pad's local origin (Uf=0, Vf=0) on the strut's top-center.
    pad.apply_translation([0.0, 0.0, strut_len])
    meshes.append(pad)

    mesh = trimesh.util.concatenate(meshes)
    # Shift the whole fin so the strut/attach point sits at the derived
    # world Uf = setback (foot set back behind the sign's bottom edge). The
    # attach point (mesh Y, Z) then equals FIN_ANCHOR_HEIGHT_MM*(sin, cos).
    mesh.apply_translation([0.0, setback, 0.0])
    return mesh, strut_len


def _fin_footprint(ax: float, ay: float) -> Polygon:
    """Bounding-box footprint for the fit/depth checks: the full wedge width
    by the tab height range, centered on the anchor."""
    return box(
        ax - FIN_WIDTH_MM / 2,
        ay - FIN_TAB_HEIGHT_MM / 2,
        ax + FIN_WIDTH_MM / 2,
        ay + FIN_TAB_HEIGHT_MM / 2,
    )


def _fin_tab_cuts(ax: float, ay: float) -> list[HangCut]:
    """Tapered tab pockets for a fin anchored at (ax, ay), as a stack of
    shrinking rectangles (the dual of the tab wedge).

    Sign depth Z runs into the material from the back surface (Z=0). At depth
    Z the tab occupies only the sign-Y range whose reach has already grown to
    Z, i.e. Y in [ay - H/2 + Z*H/REACH, ay + H/2]; so the affected Y range
    shrinks from the tab's full height (near the entrance) down to a sliver at
    the top (at the deepest point). Approximated with 6 Z-sub-slices, each a
    rectangle FIN_TAB_BASE_MM+clearance wide in X, using the shallow (wider)
    edge of each slice so the pocket enclosingly contains the tab. Active in
    Z in [0, FIN_TAB_REACH_MM + FIN_TAB_CLEARANCE_MM).
    """
    depth = FIN_TAB_REACH_MM + FIN_TAB_CLEARANCE_MM
    n = 6
    dz = depth / n
    x_hw = (FIN_TAB_BASE_MM + FIN_TAB_CLEARANCE_MM) / 2
    y_top = ay + FIN_TAB_HEIGHT_MM / 2
    tab_xs = [ax + (lx - FIN_WIDTH_MM / 2) for lx in _fin_tab_local_xs()]
    cuts: list[HangCut] = []
    for i in range(n):
        z0 = round(i * dz, 9)
        z1 = round((i + 1) * dz, 9)
        # Shallow (widest) edge of the slice -> lowest Y threshold in it.
        frac = min(z0 / FIN_TAB_REACH_MM, 1.0)
        y_lo = ay - FIN_TAB_HEIGHT_MM / 2 + frac * FIN_TAB_HEIGHT_MM
        rects = [box(tx - x_hw, y_lo, tx + x_hw, y_top) for tx in tab_xs]
        cuts.append(HangCut(z0, z1, _as_mp(unary_union(rects))))
    return cuts


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def plan_hangs(
    art: VectorizedArt,
    extrude: ExtrudeParams,
    *,
    tops: dict[str, float],
    regions: dict[str, MultiPolygon],
    body: str,
    plate_top: float,
    plate_enabled: bool,
    model_top: float,
) -> HangPlan:
    plan = HangPlan()
    mode = extrude.hang.mode
    if mode == "none":
        return plan
    if not plate_enabled:
        plan.warnings.append(
            {
                "code": "hang_requires_plate",
                "msg": "wall-hanging hardware requires the backing plate to be enabled",
            }
        )
        return plan

    if mode == "keyhole":
        _plan_keyhole(plan, art, tops, regions, model_top)
    elif mode == "fins":
        _plan_fins(plan, art, extrude, tops, regions, model_top)
    return plan


def _plan_keyhole(
    plan: HangPlan,
    art: VectorizedArt,
    tops: dict[str, float],
    regions: dict[str, MultiPolygon],
    model_top: float,
) -> None:
    w, h = art.width_mm, art.height_mm
    depth = KEYHOLE_TOTAL_DEPTH_MM
    need = depth + SAFETY_MARGIN_MM
    cy = KEYHOLE_Y_FRAC * h
    lx = KEYHOLE_X_FRAC * w
    rx = w - lx

    left_fp = _keyhole_footprint(lx, cy)
    right_fp = _keyhole_footprint(rx, cy)

    def ok(fp) -> tuple[bool, bool]:
        return _fits(fp, w, h, EDGE_MARGIN_MM), _ceiling(fp, tops, regions, model_top) >= need - _EPS

    lf, ld = ok(left_fp)
    rf, rd = ok(right_fp)
    disjoint = not left_fp.intersects(right_fp)

    if lf and rf and ld and rd and disjoint:
        plan.cuts.extend(_keyhole_cuts(lx, cy))
        plan.cuts.extend(_keyhole_cuts(rx, cy))
        return

    cx = w / 2
    center_fp = _keyhole_footprint(cx, cy)
    cf, cd = ok(center_fp)
    if cf and cd:
        plan.cuts.extend(_keyhole_cuts(cx, cy))
        reason = "too small for two keyholes" if not (lf and rf and disjoint) else "not enough depth for two keyholes"
        plan.warnings.append(
            {"code": "hang_fallback_single", "msg": f"placed one centered keyhole ({reason})"}
        )
        return

    reason = "keyhole does not fit within the sign" if not cf else "not enough material depth for a keyhole"
    plan.warnings.append({"code": "hang_skipped", "msg": f"wall-hanging skipped: {reason}"})


def _plan_fins(
    plan: HangPlan,
    art: VectorizedArt,
    extrude: ExtrudeParams,
    tops: dict[str, float],
    regions: dict[str, MultiPolygon],
    model_top: float,
) -> None:
    w, h = art.width_mm, art.height_mm
    depth = FIN_TAB_REACH_MM + FIN_TAB_CLEARANCE_MM
    need = depth + FIN_MARGIN_MM
    ay = FIN_ANCHOR_HEIGHT_MM
    lx = FIN_X_FRAC * w
    rx = w - lx

    left_fp = _fin_footprint(lx, ay)
    right_fp = _fin_footprint(rx, ay)

    def ok(fp) -> tuple[bool, bool]:
        return _fits(fp, w, h, EDGE_MARGIN_MM), _ceiling(fp, tops, regions, model_top) >= need - _EPS

    lf, ld = ok(left_fp)
    rf, rd = ok(right_fp)
    disjoint = not left_fp.intersects(right_fp)

    angle = extrude.hang.fin_angle_deg
    bx1 = art.plate_footprint.bounds[2]
    by0 = art.plate_footprint.bounds[1]
    gap = 5.0
    color = extrude.main_body_color

    def place(anchor_x: float, name: str, part_index: int) -> None:
        plan.cuts.extend(_fin_tab_cuts(anchor_x, ay))
        mesh, leg = _build_fin_mesh(angle)
        ox = bx1 + gap + part_index * (FIN_WIDTH_MM + gap)
        mesh.apply_translation([ox, by0, 0.0])
        plan.extra_parts.append(Part(name=name, color=color, mesh=mesh))

    if lf and rf and ld and rd and disjoint:
        place(lx, "fin_left", 0)
        place(rx, "fin_right", 1)
        return

    cx = w / 2
    center_fp = _fin_footprint(cx, ay)
    cf, cd = ok(center_fp)
    if cf and cd:
        place(cx, "fin_center", 0)
        reason = "too small for two fins" if not (lf and rf and disjoint) else "not enough depth for two fins"
        plan.warnings.append(
            {"code": "hang_fallback_single", "msg": f"placed one centered fin ({reason})"}
        )
        return

    reason = "fin tabs do not fit within the sign" if not cf else "not enough material depth for fin tabs"
    plan.warnings.append({"code": "hang_skipped", "msg": f"wall-hanging skipped: {reason}"})


# ---------------------------------------------------------------------------
# Integration into geometry.py (do this as part of this task — it's the only
# other file this feature touches):
#
# 1. In `build_model`, after `tops` is fully computed and before the
#    `bps`/breakpoints block, call:
#        hang_plan = hanging.plan_hangs(
#            art, extrude, tops=tops, regions=regions, body=body,
#            plate_top=plate_top, plate_enabled=plate_enabled,
#            model_top=model_top,   # note: model_top is computed a few lines
#                                    # below tops today — reorder so it's
#                                    # available here, or compute a
#                                    # provisional model_top from the same
#                                    # inputs before this call and reuse it.
#        )
#        warnings.extend(hang_plan.warnings)
#    (`model_top` currently depends on `plan.bands`, which doesn't depend on
#    hanging — safe to hoist that computation earlier if needed.)
#
# 2. Add every cut's z0_mm/z1_mm into the `bps` breakpoint set, exactly like
#    the chamfer breakpoints are added, so no cut spans a slice boundary.
#
# 3. In the main `for za, zb in zip(bps, bps[1:])` loop, after computing
#    `clip` (the existing chamfer intersection), compute the union of every
#    HangCut whose [z0_mm, z1_mm) overlaps [za, zb), and — for every
#    (poly, color, key, name) yielded by `assignment(zmid)` — subtract it:
#    `poly = poly.difference(hang_cut)` (after the existing chamfer
#    `.intersection(clip)` step, same guard pattern: skip if the result goes
#    empty). This is safe unconditionally because `plan_hangs` only ever
#    places a cut where the depth-availability check already proved that
#    Z-range is body/plate material only (never a different visible color),
#    so there's no risk of eating into a color region that should stay
#    solid.
#
# 4. After the main loop builds `parts`, extend it with `hang_plan.extra_parts`
#    before the final `parts.sort(...)`.
#
# No other files need to change — `extrude.hang` already reaches
# `build_model` unmodified through the existing `ModelRequest` -> routes.py
# -> `build_model(art, extrude)` call path.
