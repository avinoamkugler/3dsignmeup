# 3dSignMeUp — Product Requirements Document (v1)

## 1. Overview

3dSignMeUp turns a picture into a clean, multicolor, 3D-printable sign in one tool.
The user uploads an image (typically AI-generated vector-style art), interactively
cleans it down to a small exact color palette, configures per-color extrusion
heights and a color-change strategy, and downloads a print-ready **3MF** (plus the
cleaned **PNG**) tuned for a **Bambu Lab A1 mini**, with or without **AMS lite**.

### Problem
Today the workflow spans four tools: an AI image generator, manual pixel cleanup,
an online raster→DXF converter, and manual extrusion in Onshape. The AI output is
noisy (anti-aliased edges, off-palette pixels, messy backgrounds), the DXF
conversion is lossy, and multicolor setup in the slicer is manual and error-prone.
A single sign takes hours, most of it mechanical fixing.

### Goal
Upload → clean → configure → download a slicer-ready 3MF in minutes, where the
only manual print step is swapping filament when the printer pauses (non-AMS mode)
or loading the AMS (AMS mode).

### Target user / scale
Single hobbyist, self-hosted, local use. No auth, no multi-tenancy, no persistence
beyond the working session. Simplicity beats robustness at scale.

## 2. Scope

### In scope (v1)
- Common raster inputs: PNG, JPG, WEBP, BMP (GIF: first frame).
- Interactive palette detection and editing (2–10 colors, default 3).
- Deterministic cleanup: quantization, smoothing, island removal, background
  clearing, enclosed-background handling, thin-feature detection.
- Preview/approve loop for the cleaned image; user can adjust and re-run freely.
- Vector tracing of color regions (no raster-staircase extrusions).
- Extrusion with per-color heights, backing plate, chamfer, first-layer-only colors.
- Two color strategies: AMS mode (multi-part caps) and swap mode (layer-band slabs
  with auto-generated filament-change events; user's printer runs a pause G-code
  macro on color change enabling manual swaps, including multiple colors within a
  single layer).
- 3D preview (colored) and downloads: 3MF + cleaned PNG.

### Out of scope (v1)
- Hooks / keychain loops (explicitly deferred to a later iteration).
- AI image generation itself; DXF/SVG import or export; STL export.
- G-code generation (slicing stays in Bambu Studio).
- Multi-plate layout, text tools, image editing beyond the cleanup pipeline.

## 3. User flow

1. **Upload** an image.
2. **Clean**: set physical sign width (mm); pick number of colors (auto-detected
   palette, each color editable via color picker); set smoothness; toggle
   background clearing; choose enclosed-background behavior. Live preview with an
   optional overlay highlighting features thinner than the printable minimum.
   Iterate until satisfied → **Approve**.
3. **Configure extrusion**: choose AMS or swap mode; per color set height in mm
   (UI shows the snapped layer count) and an optional "show in 1st layer only"
   toggle; pick main body color; backing plate on/off + thickness; chamfer on/off;
   in swap mode set the initial color-switch lead (mm); layer height and first
   layer height.
4. **Review**: colored 3D preview, swap-plan table (swap Z, layer number, from→to
   color, total swap count), and any warnings.
5. **Download** `sign.3mf` and `clean.png`. Import the 3MF into Bambu Studio,
   map filaments to parts, slice, print.

## 4. Functional requirements

### 4.1 Image processing
- **FR-1 Palette detection.** Detect n dominant colors (k-means in Lab space,
  transparent pixels ignored), n ∈ [2,10], default 3. Detected colors are
  starting points; the user can replace any via color picker.
- **FR-2 Quantization.** On processing, every pixel maps to the nearest palette
  color (perceptual/Lab distance). The output image contains **only** palette
  colors and transparency — no other sampleable shades, ever.
- **FR-3 Smoothness.** A 0–10 parameter controlling (a) raster mode/median
  filtering and (b) vector simplification tolerance, both scaled to physical mm
  so results are resolution-independent. 0 = off.
- **FR-4 Island removal.** Connected components smaller than the minimum feature
  area are reassigned to their surrounding color, eliminating stray
  single-pixel/tiny-region extrusions.
- **FR-5 Background clearing (toggle).** Sample the four corner pixels after
  quantization; flood-fill-delete (→ transparent) all same-color regions
  connected to any corner.
- **FR-6 Enclosed background (3-way option).** Background-colored regions *not*
  connected to a corner (e.g., the counter of an "O") are, per user choice:
  `through_hole` (cut through art and plate), `plate` (hole in art, plate visible
  beneath — default), or `fill` (kept as solid in main body color).
- **FR-7 Thin-feature detection.** Regions narrower than a configurable minimum
  feature width (default 0.5mm at the chosen physical size; A1 mini nozzle is
  0.4mm) are highlighted in a preview overlay so the user can fix or accept them.
- **FR-8 Approval loop.** The user sees the processed result before any geometry
  is built and can revise parameters and re-preview indefinitely. The approved
  preview is byte-identical to the downloadable clean PNG.

### 4.2 Geometry
- **FR-9 Vector extrusion.** Color regions are contour-traced (marching squares),
  simplified, and extruded as polygons with holes — not per-pixel voxels.
  Adjacent color regions share boundary vertices exactly (traced from one label
  grid) so parts meet with no gaps or overlaps.
- **FR-10 Per-color height.** Each palette color has an extrusion height in mm,
  measured as TOTAL thickness from the bed — a backing plate is part of that
  height, never an addition to it (otherwise a plate printed in the body color
  silently doubles the body color's thickness). A height that would leave nothing
  above the plate is lifted to one layer above it, with a warning.
  All heights snap to whole layers (`first_layer_height + n × layer_height`);
  the UI shows requested vs. snapped values, layer counts, and the relief left
  above the plate.
- **FR-11 Backing plate.** Default ON: a base slab in main body color under the
  full artwork outline, configurable thickness, keeping disconnected artwork
  (e.g., separate letters) as one printable object. "No plate" is the advanced
  choice, with a warning when the art is multiple disconnected islands.
- **FR-12 Chamfer (toggle).** A 45° chamfer on the bed-facing outer perimeter of
  the bottom solid to improve adhesion/edge quality. Warn when combined with
  no-plate multi-island art.
- **FR-13 First-layer-only colors.** Any color can be toggled to appear only in
  the first layer (region emitted as a first-layer slab in that color; body/plate
  above). Supported in both modes; in swap mode this relies on the user's
  pause-on-color-change macro to allow two colors within one layer.
- **FR-14 Main body color.** A designated filament color used for the plate and
  all non-visible interior volume.

### 4.3 Color strategies
- **FR-15 AMS mode.** Body printed in main body color; each color region gets a
  thin top cap (0.6mm / 3 layers in v1) as a separate colored part — forcing the
  "double color swap" so faces show true colors with minimal AMS purging. Warn
  (not block) when total filaments exceed AMS lite's 4 slots.
- **FR-16 Swap mode (AMS off).** Color is a function of Z. The solid is cut into
  horizontal slabs at computed swap heights; each slab is a separate colored 3MF
  part so the slicer generates filament-change events automatically (no manual
  layer-slider edits). Swap plan rules:
  - Plate and body volume below the first visible color print in main body color.
  - Switch to the first non-body color `initial_color_switch_mm` **before** its
    visible top face (purge lead), i.e., only the first swap gets a lead.
  - After a color's top face completes, switch directly to the next color in
    ascending height order; **never switch back to body color** — swaps are
    minimized.
  - Reference example: colors R 10mm / G 5mm / B 15mm, body R, lead 2mm ⇒
    R for 3mm → G for 2mm → R for 5mm → B for the remainder. 3 swaps.
  - If the first visible color equals the body color, no initial swap.
  - **Equal heights** are allowed: both colors become sibling parts in the same
    Z band (one extra swap on those layers); the shared body slab beneath them
    prints in whichever color has more perimeter presence (fewer visible side
    stripes).
- **FR-17 Swap plan visibility.** The UI shows the full plan (Z, layer, from→to)
  and total swap count before download.

### 4.4 Output
- **FR-18 3MF export.** Single 3MF containing named, colored parts (one object,
  multiple parts) that Bambu Studio imports with per-part filament mapping.
- **FR-19 Clean PNG export.** The approved processed image (palette colors +
  transparency only).
- **FR-20 3D preview.** Colored in-browser preview (GLB via three.js) before
  download.

## 5. Non-functional requirements
- Python backend (FastAPI); OpenCV/Pillow/scikit-image/scikit-learn for raster,
  shapely + trimesh for geometry; vanilla JS + three.js frontend.
- Stateless-by-params: server stores only the original upload per session;
  all outputs are pure functions of (image, params), cached by params hash.
- Preview round-trip < 1s and model build < 10s for typical sign inputs
  (≤ ~2000px source, ≤ 200mm sign) on a desktop machine.
- Meshes must be watertight/manifold and slice cleanly in Bambu Studio without
  repair prompts.
- Local single-user deployment; no authentication; sessions are disposable.

## 6. Known accepted limitations
- **Striped sides in swap mode**: at any layer the full cross-section (including
  outer walls of taller regions) prints in the currently loaded filament, so
  side walls show color bands. Accepted; mitigations (tall color as body, thin
  caps) are usage guidance, not features.
- STL cannot carry color; 3MF is the only 3D output format in v1.
- AMS cap thickness fixed at 3 layers in v1.

## 7. Future iterations (parking lot)
- Hooks / hanging loops (torus unioned pre-extrusion) — first candidate.
- Cap-thickness parameter; "suggest body color" heuristic; SVG input;
  per-color min-feature overrides; saved presets per filament set.

## 8. Success criteria
- A typical AI-generated sign image goes from upload to slicer-ready 3MF in
  under 5 minutes with no external tools.
- The printed result requires zero manual slicer surgery: filament mapping on
  import (AMS) or macro-driven pauses (swap mode) only.
- The clean PNG contains exactly the palette colors — verifiable by eyedropper.
