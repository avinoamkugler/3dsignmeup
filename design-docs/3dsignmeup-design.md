# 3dSignMeUp — Module Layout & API Contract (v1)

Image → multicolor 3D-printable sign (3MF) for Bambu A1 mini, with and without AMS lite.

## Architecture

FastAPI backend + single-page vanilla JS frontend (three.js for 3D preview). One design principle drives the whole contract: **the server stores only the original upload; every result is a pure function of (original image, params).** The client re-sends the full param set on each call. This makes the approve/go-back loop trivial (no server-side undo state), makes results reproducible, and lets us cache by hashing params.

```
3dsignmeup/
├── app/
│   ├── main.py               # FastAPI app factory, static mount
│   ├── api/
│   │   ├── routes.py         # endpoints (thin: validate → call core → respond)
│   │   └── schemas.py        # Pydantic models == the API contract
│   ├── core/
│   │   ├── session.py        # session store: id → original image (disk), model cache
│   │   ├── palette.py        # k-means dominant colors; quantize image to palette
│   │   ├── cleanup.py        # mode/median smoothing, island removal (mm-scaled),
│   │   │                     #   corner flood-fill bg clear, enclosed-bg handling,
│   │   │                     #   thin-feature detection (morphological, mm-scaled)
│   │   ├── vectorize.py      # per-color mask → marching squares → simplify →
│   │   │                     #   shapely MultiPolygon with holes, px→mm transform
│   │   ├── geometry.py       # extrude polygons, backing plate, chamfer loft,
│   │   │                     #   boolean assembly per part
│   │   ├── swapplan.py       # layer snapping, AMS-off color ordering, swap-Z
│   │   │                     #   computation, slab splitting of solids
│   │   └── export.py         # 3MF assembly (parts + names + colors), GLB for
│   │                         #   preview, clean PNG
│   └── static/               # index.html, app.js, three.js
└── tests/                    # golden-image tests per core module
```

### Pipeline data flow

```
upload ─► palette.detect ─► [user edits palette]
      ─► quantize ─► cleanup ─► PREVIEW PNG + warnings ─► [approve / adjust, loop]
      ─► vectorize ─► geometry ─► swapplan (slab split) ─► export
      ─► GLB preview ─► [approve] ─► download 3MF + PNG
```

`quantize → cleanup` runs on every preview call (fast, sub-second at sign resolutions).
`vectorize → export` runs on model build. Both keyed by params hash for caching.

---

## Endpoints

### 1. `POST /api/sessions`
Upload the source image. `multipart/form-data`, field `image` (png/jpg/webp/bmp/gif-first-frame).

```json
201 → {
  "session_id": "a1b2c3",
  "width_px": 1024, "height_px": 768,
  "suggested_palette": ["#1a1a1a", "#e63946", "#f1faee"]   // quick k=3 for instant UI
}
```

### 2. `POST /api/sessions/{id}/palette`
Re-detect dominant colors when the user changes n.

```json
{ "n_colors": 4 }                       // 2..10
→ { "palette": ["#..", "#..", "#..", "#.."] }
```

Detection: k-means in Lab space (perceptual), ignoring fully transparent pixels;
centers snapped back to nearest actually-occurring pixel color so picks look "true".

### 3. `POST /api/sessions/{id}/preview`
The approval-loop workhorse. Full `ProcessParams`, returns processed image + warnings.

```json
{
  "target_width_mm": 120.0,             // sets px→mm scale for everything below
  "palette": ["#1a1a1a", "#e63946", "#f1faee"],   // user-edited hexes
  "smoothness": 4,                      // 0..10; 0 = off
  "min_feature_mm": 0.5,                // regions thinner than this get flagged/opened
  "clear_background": true,
  "enclosed_background": "plate"        // "through_hole" | "plate" | "fill"
}
→ {
  "preview_png": "/api/sessions/a1b2c3/artifacts/prev_9f31.png",
  "thin_overlay_png": "/api/sessions/a1b2c3/artifacts/thin_9f31.png",  // magenta mask of sub-min_feature regions, null if none
  "islands": 5,                         // disconnected components AFTER bg clear (info: plate will join them)
  "color_coverage": {"#1a1a1a": 0.41, "#e63946": 0.22, "#f1faee": 0.37},
  "params_hash": "9f31"
}
```

Processing order (fixed): quantize to palette (nearest in Lab) → smoothness
(median/mode filter, kernel scaled by smoothness × mm-scale) → island removal
(components smaller than min_feature_mm² reassigned to surrounding color) →
corner flood-fill background clear (marks pixels transparent) → enclosed-background
resolution → thin-feature scan. The PNG the user approves is byte-identical to the
downloadable "clean PNG": palette colors + transparency only, no other shades.

`enclosed_background` semantics (only meaningful when `clear_background` is on):
background-colored regions *not* reachable from corners are — `through_hole`: cut
through art *and* plate; `plate`: hole in the art, plate visible beneath; `fill`:
treated as artwork in main-body color.

### 4. `POST /api/sessions/{id}/model`
Build the mesh. Takes ProcessParams (verbatim from the approved preview) + ExtrudeParams.

```json
{
  "process": { ...exact ProcessParams from step 3... },
  "extrude": {
    "mode": "swap",                     // "ams" | "swap"
    "layer_height_mm": 0.2,
    "first_layer_height_mm": 0.2,
    "plate": { "enabled": true, "thickness_mm": 1.2 },   // enabled=false is the advanced path
    "main_body_color": "#1a1a1a",       // any hex; usually one of the palette
    "colors": [                          // one entry per palette color
      { "hex": "#1a1a1a", "height_mm": 2.0, "first_layer_only": false },
      { "hex": "#e63946", "height_mm": 3.0, "first_layer_only": false },
      { "hex": "#f1faee", "height_mm": 3.0, "first_layer_only": true }
    ],
    "initial_color_switch_mm": 2.0,      // swap mode only; purge lead before first visible color
    "chamfer": { "enabled": true, "size_mm": 0.6 }
  }
}
→ {
  "model_id": "m_5d2e",
  "parts": [
    { "name": "body_#1a1a1a_z0.0-3.2", "color": "#1a1a1a", "triangles": 1204 },
    { "name": "cap_#e63946_z3.2-4.2",  "color": "#e63946", "triangles": 820 }
  ],
  "swap_plan": [                         // empty in AMS mode
    { "z_mm": 1.0, "layer": 5, "from": "#1a1a1a", "to": "#e63946" },
    { "z_mm": 3.0, "layer": 15, "from": "#e63946", "to": "#1a1a1a" }
  ],
  "n_swaps": 2,
  "snapped_heights": { "#e63946": {"requested": 3.0, "snapped": 3.0, "layers": 15} },
  "warnings": [
    { "code": "ams_slots", "msg": "5 colors incl. body exceeds AMS lite's 4 slots" }
  ],
  "preview_glb": "/api/sessions/a1b2c3/models/m_5d2e/preview.glb",
  "downloads": {
    "threemf": "/api/sessions/a1b2c3/models/m_5d2e/sign.3mf",
    "png": "/api/sessions/a1b2c3/models/m_5d2e/clean.png"
  }
}
```

### 5. `GET` artifact/download routes
Plain file responses for `preview_png`, `thin_overlay_png`, `preview.glb`
(colored per part — three.js `GLTFLoader`, colors come for free), `sign.3mf`,
`clean.png`. All content-hashed, cacheable.

---

## Contract rules the core modules enforce

**Layer snapping (swapplan.py).** Valid Z values are `first_layer_height + n × layer_height`.
Every color height, swap Z, and chamfer top snaps to the nearest valid Z (min 1 layer);
response always reports requested vs. snapped so the UI can display "3.0mm → 15 layers".
Two colors snapping to the same top Z is legal: they become sibling caps in the same
Z band (one extra swap on those layers); the body slab beneath them prints in whichever
of the two has more perimeter pixels (your tiebreak heuristic).

**Slab model (swapplan.py + geometry.py).** In swap mode the solid is cut at every
swap Z; each slab is a separate 3MF part colored by the filament active in that band —
the slicer generates the change events (your pause macro handles the physical swap),
no layer-slider work in Bambu Studio. Ordering: plate + everything below the first
visible non-body top face prints in body color, minus `initial_color_switch_mm` of
purge lead before the first foreign color; after each color's top face completes,
switch directly to the next color (ascending height), never back to body. Your
R10/G5/B15 example ⇒ R 3mm, G 2mm, R 5mm, B 10mm... note: B's band is z5→15 = 10mm
of B *printing*, matching your plan's intent.

**AMS mode.** Body = one part in `main_body_color` up to (color_height − cap); each
color region gets a cap part (top 0.6mm / 3 layers, constant for v1). ≤4 filaments
validated → warning, not error.

**`first_layer_only`.** That color's region is emitted as a first-layer-height slab
part at z0 in its own color; above it, the region belongs to the body/plate. Works in
both modes (in swap mode it adds same-band sibling parts on layer 1 — your pause
macro makes this printable).

**Chamfer (geometry.py).** 45° loft from `outline.buffer(-size)` at z0 to the full
outline at z=size (snapped to whole layers), applied to the outer perimeter of the
bottom-most solid (plate if enabled, else each island — with a warning that
no-plate + chamfer on multi-island art can create tiny feet).

**Vectorize guarantees.** Per-color masks are traced with marching squares at
half-pixel precision, simplified with tolerance ≈ 0.3 × smoothness × mm_per_px,
holes preserved via even-odd nesting into shapely polygons. Adjacent color regions
share identical boundary vertices (traced from the same label grid) so extruded
parts butt perfectly with no z-fighting or gaps.

---

## Frontend screens (maps 1:1 to endpoints)

1. **Upload** → drop zone → `POST /sessions`.
2. **Palette & cleanup** → n-colors stepper, per-color pickers, smoothness slider,
   physical width, bg-clear toggle + enclosed-bg 3-way, live preview image with
   thin-region overlay toggle → loops on `POST /preview` → Approve.
3. **Extrusion** → mode toggle, per-color height inputs showing snapped layer counts,
   first-layer-only toggles, body color, plate/chamfer/lead params → `POST /model`.
4. **3D preview & download** → GLB in three.js, swap-plan table, warnings,
   download buttons.

## Suggested build order

session/upload → palette → preview loop (quantize+cleanup only) → vectorize →
single-color extrude + GLB preview → plate/chamfer → swap plan + slab split →
3MF export → AMS caps → first-layer-only → polish.
