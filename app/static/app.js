import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// ============================================================
// State — a single object. Server is stateless-by-params, so
// going back and re-approving just re-issues requests.
// ============================================================
const state = {
  step: 1,
  session: null,        // { session_id, width_px, height_px, suggested_palette }
  imageFile: null,
  imageObjectUrl: null,
  palette: [],          // current hexes shown in screen 2
  colorLock: true,      // "Keep original color mapping" toggle (default ON)
  quantizePalette: null,// frozen hex list for pixel->label assignment; snapshot of palette
  process: {            // ProcessParams (screen 2)
    target_width_mm: 120,
    palette: [],
    smoothness: 4,
    min_feature_mm: 0.5,
    clear_background: true,
    enclosed_background: "plate",
  },
  lastPreview: null,    // last successful PreviewResponse
  approvedProcess: null,// exact ProcessParams carried into screen 3
  coverage: {},         // color_coverage from last preview
  extrude: {            // ExtrudeParams (screen 3)
    mode: "swap",
    layer_height_mm: 0.2,
    first_layer_height_mm: 0.2,
    plate: { enabled: true, thickness_mm: 1.2 },
    main_body_color: null,
    colors: [],         // [{hex, height_mm, first_layer_only}]
    initial_color_switch_mm: 2.0,
    chamfer: { enabled: true, size_mm: 0.6 },
    hang: { mode: "none", fin_angle_deg: 15 }, // HangParams
  },
  model: null,          // ModelResponse
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ============================================================
// API helpers
// ============================================================
async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_) { /* non-json error */ }
    throw new Error(detail);
  }
  return res;
}

async function apiJson(url, opts = {}) {
  const res = await apiFetch(url, opts);
  return res.json();
}

function postJson(url, body, signal) {
  return apiJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

// ============================================================
// Toasts
// ============================================================
function toast(msg, title = "Something went wrong") {
  const stack = $("#toast-stack");
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<div class="t-msg"><div class="t-title"></div><div class="t-body"></div></div>
                  <button class="t-close" aria-label="Dismiss">×</button>`;
  el.querySelector(".t-title").textContent = title;
  el.querySelector(".t-body").textContent = msg;
  const close = () => el.remove();
  el.querySelector(".t-close").addEventListener("click", close);
  stack.appendChild(el);
  setTimeout(close, 7000);
}

// ============================================================
// Navigation
// ============================================================
function maxReachableStep() {
  if (!state.session) return 1;
  if (!state.approvedProcess) return 2;
  if (!state.model) return 3;
  return 4;
}

function goToStep(n) {
  if (n > maxReachableStep()) return;
  state.step = n;
  for (let i = 1; i <= 4; i++) {
    $(`#screen-${i}`).hidden = i !== n;
  }
  const reachable = maxReachableStep();
  $$("#stepper .step").forEach((btn) => {
    const s = Number(btn.dataset.step);
    btn.classList.toggle("active", s === n);
    btn.classList.toggle("done", s < n);
    btn.disabled = s > reachable;
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (n === 3) buildScreen3();
  if (n === 4) renderScreen4();
}

$$("#stepper .step").forEach((btn) => {
  btn.addEventListener("click", () => goToStep(Number(btn.dataset.step)));
});

// ============================================================
// SCREEN 1 — Upload
// ============================================================
const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const ACCEPT = ["image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif"];

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files && fileInput.files[0]) handleFile(fileInput.files[0]);
});

async function handleFile(file) {
  if (!ACCEPT.includes(file.type) && !/\.(png|jpe?g|webp|bmp|gif)$/i.test(file.name)) {
    toast(`Unsupported file type: ${file.type || file.name}`, "Invalid image");
    return;
  }
  state.imageFile = file;
  if (state.imageObjectUrl) URL.revokeObjectURL(state.imageObjectUrl);
  state.imageObjectUrl = URL.createObjectURL(file);

  $("#dropzone-empty").hidden = true;
  const prev = $("#dropzone-preview");
  prev.hidden = false;
  $("#upload-thumb").src = state.imageObjectUrl;
  $("#upload-meta").textContent = "Uploading…";
  $("#upload-next").disabled = true;

  try {
    const fd = new FormData();
    fd.append("image", file);
    const data = await apiJson("/api/sessions", { method: "POST", body: fd });
    state.session = data;
    state.palette = (data.suggested_palette || []).slice();
    state.process.palette = state.palette.slice();
    snapshotQuantizePalette(); // freeze pixel->label assignment against the freshly detected palette
    $("#upload-meta").textContent =
      `${data.width_px} × ${data.height_px}px · ${state.palette.length} colors detected`;
    $("#upload-next").disabled = false;
    buildScreen2();
  } catch (err) {
    $("#upload-meta").textContent = "Upload failed";
    toast(err.message, "Upload failed");
  }
}

$("#upload-next").addEventListener("click", () => {
  if (state.session) goToStep(2);
});

// ============================================================
// SCREEN 2 — Palette & cleanup
// ============================================================
let previewGen = 0;          // generation counter to drop stale responses
let previewAbort = null;
let previewTimer = null;
let inFlight = false;

function buildScreen2() {
  $("#target-width").value = state.process.target_width_mm;
  $("#ncolors-value").textContent = state.palette.length;
  $("#smoothness").value = state.process.smoothness;
  $("#smoothness-val").textContent = state.process.smoothness;
  $("#min-feature").value = state.process.min_feature_mm;
  $("#clear-bg").checked = state.process.clear_background;
  $("#enclosed-bg").value = state.process.enclosed_background;
  $("#color-lock").checked = state.colorLock;
  renderPaletteSwatches();
  updateEnclosedEnabled();
  schedulePreview();
}

// Freeze the CURRENT palette as the pixel->label quantization reference.
// Called whenever cardinality changes (upload / n-colors / reset) or when
// the lock is turned on mid-session ("lock in whatever I have now").
function snapshotQuantizePalette() {
  state.quantizePalette = state.palette.slice();
}

$("#color-lock").addEventListener("change", () => {
  state.colorLock = $("#color-lock").checked;
  // Turning the lock ON snapshots the palette as it is right now, not the
  // original detection — so subsequent swatch edits keep this mapping frozen.
  if (state.colorLock) snapshotQuantizePalette();
  schedulePreview();
});

$("#reset-colors").addEventListener("click", () => {
  const original = (state.session && state.session.suggested_palette) || [];
  if (!original.length) return;
  state.palette = original.slice();
  state.process.palette = state.palette.slice();
  snapshotQuantizePalette(); // re-freeze to match the restored palette
  $("#ncolors-value").textContent = state.palette.length;
  renderPaletteSwatches();
  updateNColorsButtons();
  schedulePreview();
});

function renderPaletteSwatches() {
  const wrap = $("#palette-swatches");
  wrap.innerHTML = "";
  state.palette.forEach((hex, i) => {
    const input = document.createElement("input");
    input.type = "color";
    input.value = hex;
    input.title = hex;
    input.addEventListener("input", () => {
      state.palette[i] = input.value.toLowerCase();
      input.title = input.value;
      state.process.palette = state.palette.slice();
      schedulePreview();
    });
    wrap.appendChild(input);
  });
}

function updateNColorsButtons() {
  $("#ncolors-dec").disabled = state.palette.length <= 2;
  $("#ncolors-inc").disabled = state.palette.length >= 10;
}

async function changeNColors(delta) {
  const target = state.palette.length + delta;
  if (target < 2 || target > 10) return;
  try {
    const data = await apiJson(`/api/sessions/${state.session.session_id}/palette`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ n_colors: target }),
    });
    state.palette = data.palette.slice();
    state.process.palette = state.palette.slice();
    snapshotQuantizePalette(); // different cardinality can't reuse the old frozen labels
    $("#ncolors-value").textContent = state.palette.length;
    renderPaletteSwatches();
    updateNColorsButtons();
    schedulePreview();
  } catch (err) {
    toast(err.message, "Palette detection failed");
  }
}

$("#ncolors-dec").addEventListener("click", () => changeNColors(-1));
$("#ncolors-inc").addEventListener("click", () => changeNColors(1));

$("#target-width").addEventListener("input", () => {
  const v = parseFloat($("#target-width").value);
  if (!Number.isNaN(v) && v > 0) { state.process.target_width_mm = v; schedulePreview(); }
});
$("#smoothness").addEventListener("input", () => {
  state.process.smoothness = parseInt($("#smoothness").value, 10);
  $("#smoothness-val").textContent = state.process.smoothness;
  schedulePreview();
});
$("#min-feature").addEventListener("input", () => {
  const v = parseFloat($("#min-feature").value);
  if (!Number.isNaN(v) && v > 0) { state.process.min_feature_mm = v; schedulePreview(); }
});
$("#clear-bg").addEventListener("change", () => {
  state.process.clear_background = $("#clear-bg").checked;
  updateEnclosedEnabled();
  schedulePreview();
});
$("#enclosed-bg").addEventListener("change", () => {
  state.process.enclosed_background = $("#enclosed-bg").value;
  schedulePreview();
});
$("#thin-toggle").addEventListener("change", applyThinOverlay);

function updateEnclosedEnabled() {
  $("#enclosed-bg").disabled = !state.process.clear_background;
}

function processParamsBody() {
  return {
    target_width_mm: state.process.target_width_mm,
    palette: state.process.palette.slice(),
    smoothness: state.process.smoothness,
    min_feature_mm: state.process.min_feature_mm,
    clear_background: state.process.clear_background,
    enclosed_background: state.process.enclosed_background,
    // Locked: freeze pixel->label assignment against the snapshot so editing a
    // swatch only recolors its region. Unlocked: null re-quantizes every edit.
    quantize_palette:
      state.colorLock && state.quantizePalette ? state.quantizePalette.slice() : null,
  };
}

function schedulePreview() {
  updateNColorsButtons();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 300);
}

async function runPreview() {
  if (!state.session) return;
  const gen = ++previewGen;
  if (previewAbort) previewAbort.abort();
  previewAbort = new AbortController();
  const body = processParamsBody();

  $("#preview-spinner").hidden = false;
  inFlight = true;
  try {
    const data = await postJson(
      `/api/sessions/${state.session.session_id}/preview`,
      body,
      previewAbort.signal,
    );
    if (gen !== previewGen) return; // stale, a newer request superseded us
    state.lastPreview = data;
    state.coverage = data.color_coverage || {};
    state.approvedProcess = body; // exact params of last successful preview
    renderPreview(data);
    $("#clean-approve").disabled = false;
  } catch (err) {
    if (err.name === "AbortError") return;
    if (gen !== previewGen) return;
    toast(err.message, "Preview failed");
  } finally {
    if (gen === previewGen) { $("#preview-spinner").hidden = true; inFlight = false; }
  }
}

function renderPreview(data) {
  const img = $("#preview-img");
  img.src = data.preview_png;
  img.hidden = false;
  $("#preview-placeholder").hidden = true;
  applyThinOverlay();

  $("#info-islands").innerHTML = `Islands: <b>${data.islands}</b> <span style="color:var(--text-faint)">(the plate joins them into one printable object)</span>`;

  const cov = $("#coverage");
  cov.innerHTML = "";
  const entries = Object.entries(data.color_coverage || {}).sort((a, b) => b[1] - a[1]);
  for (const [hex, frac] of entries) {
    const pct = (frac * 100);
    const row = document.createElement("div");
    row.className = "cov-item";
    row.innerHTML = `
      <span class="cov-swatch" style="background:${hex}"></span>
      <span class="cov-bar"><span style="width:${pct.toFixed(1)}%"></span></span>
      <span class="cov-pct">${pct.toFixed(1)}%</span>`;
    cov.appendChild(row);
  }
}

function applyThinOverlay() {
  const overlay = $("#thin-overlay");
  const note = $("#thin-note");
  const data = state.lastPreview;
  const on = $("#thin-toggle").checked;
  if (!data) { overlay.hidden = true; note.hidden = true; return; }

  if (data.thin_overlay_png) {
    note.hidden = true;
    if (on) { overlay.src = data.thin_overlay_png; overlay.hidden = false; }
    else { overlay.hidden = true; }
  } else {
    overlay.hidden = true;
    if (on) {
      note.textContent = `No features below ${state.process.min_feature_mm} mm.`;
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }
}

$("#clean-back").addEventListener("click", () => goToStep(1));
$("#clean-approve").addEventListener("click", () => {
  if (!state.approvedProcess) { toast("Wait for a successful preview first.", "Not ready"); return; }
  goToStep(3);
});

// ============================================================
// SCREEN 3 — Extrusion
// ============================================================
function coverageMaxColor() {
  const entries = Object.entries(state.coverage);
  if (!entries.length) return state.process.palette[0];
  entries.sort((a, b) => b[1] - a[1]);
  return entries[0][0];
}

function buildScreen3() {
  const palette = state.approvedProcess.palette;
  // (Re)build per-color list, keeping prior values by hex where possible.
  const prior = new Map(state.extrude.colors.map((c) => [c.hex, c]));
  state.extrude.colors = palette.map((hex) => {
    const p = prior.get(hex);
    return {
      hex,
      height_mm: p ? p.height_mm : 3.0,
      first_layer_only: p ? p.first_layer_only : false,
    };
  });
  if (!palette.includes(state.extrude.main_body_color)) {
    state.extrude.main_body_color = coverageMaxColor();
  }

  // Reflect mode
  $$("#mode-toggle .toggle-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.extrude.mode));
  $("#layer-height").value = state.extrude.layer_height_mm;
  $("#first-layer-height").value = state.extrude.first_layer_height_mm;
  $("#plate-enabled").checked = state.extrude.plate.enabled;
  $("#plate-thickness").value = state.extrude.plate.thickness_mm;
  $("#chamfer-enabled").checked = state.extrude.chamfer.enabled;
  $("#chamfer-size").value = state.extrude.chamfer.size_mm;
  $("#initial-lead").value = state.extrude.initial_color_switch_mm;

  $$("#hang-toggle .toggle-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.hang === state.extrude.hang.mode));
  $("#fin-angle").value = state.extrude.hang.fin_angle_deg;
  $("#fin-angle-val").textContent = state.extrude.hang.fin_angle_deg;

  renderColorHeights();
  renderBodyColor();
  updateModeVisibility();
  updateHangVisibility();
  renderFilamentPanel();
}

function layerInfo(height) {
  const layer = state.extrude.layer_height_mm;
  const first = state.extrude.first_layer_height_mm;
  const layers = Math.max(1, Math.round((height - first) / layer) + 1);
  const snapped = first + (layers - 1) * layer;
  return { layers, snapped };
}

function renderColorHeights() {
  const wrap = $("#color-heights");
  wrap.innerHTML = "";
  state.extrude.colors.forEach((c, i) => {
    const { layers, snapped } = layerInfo(c.height_mm);
    const row = document.createElement("div");
    row.className = "ch-row";
    row.innerHTML = `
      <span class="ch-swatch" style="background:${c.hex}"></span>
      <input type="number" min="0.01" step="0.1" value="${c.height_mm}" />
      <span class="ch-layers">→ ${layers} layer${layers === 1 ? "" : "s"} (${snapped.toFixed(1)} mm)</span>
      <label class="ch-flo checkbox"><input type="checkbox" ${c.first_layer_only ? "checked" : ""} /> 1st layer only</label>`;
    const numInput = row.querySelector('input[type="number"]');
    const chk = row.querySelector('input[type="checkbox"]');
    numInput.addEventListener("input", () => {
      const v = parseFloat(numInput.value);
      if (!Number.isNaN(v) && v > 0) {
        c.height_mm = v;
        const info = layerInfo(v);
        row.querySelector(".ch-layers").textContent =
          `→ ${info.layers} layer${info.layers === 1 ? "" : "s"} (${info.snapped.toFixed(1)} mm)`;
        renderFilamentPanel();
      }
    });
    chk.addEventListener("change", () => { c.first_layer_only = chk.checked; renderFilamentPanel(); });
    wrap.appendChild(row);
  });
}

function refreshColorLayerLabels() {
  const rows = $$("#color-heights .ch-row");
  state.extrude.colors.forEach((c, i) => {
    const info = layerInfo(c.height_mm);
    const label = rows[i] && rows[i].querySelector(".ch-layers");
    if (label) label.textContent =
      `→ ${info.layers} layer${info.layers === 1 ? "" : "s"} (${info.snapped.toFixed(1)} mm)`;
  });
}

function renderBodyColor() {
  const wrap = $("#main-body-color");
  wrap.innerHTML = "";
  const palette = state.approvedProcess.palette;
  palette.forEach((hex) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "body-swatch";
    chip.setAttribute("role", "radio");
    const selected = hex === state.extrude.main_body_color;
    chip.setAttribute("aria-checked", selected ? "true" : "false");
    chip.classList.toggle("selected", selected);
    chip.tabIndex = selected ? 0 : -1;
    chip.dataset.hex = hex;
    chip.innerHTML = `<span class="body-swatch-color" style="background:${hex}"></span><span class="body-swatch-hex">${hex}</span>`;
    chip.addEventListener("click", () => selectBodyColor(hex));
    wrap.appendChild(chip);
  });
}

function selectBodyColor(hex, focus = false) {
  state.extrude.main_body_color = hex;
  const chips = $$("#main-body-color .body-swatch");
  chips.forEach((chip) => {
    const on = chip.dataset.hex === hex;
    chip.setAttribute("aria-checked", on ? "true" : "false");
    chip.classList.toggle("selected", on);
    chip.tabIndex = on ? 0 : -1;
    if (on && focus) chip.focus();
  });
  renderFilamentPanel();
}

// Arrow-key navigation for the body-color radiogroup.
$("#main-body-color").addEventListener("keydown", (e) => {
  const palette = state.approvedProcess ? state.approvedProcess.palette : [];
  if (!palette.length) return;
  let idx = palette.indexOf(state.extrude.main_body_color);
  if (idx < 0) idx = 0;
  if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); idx = (idx + 1) % palette.length; }
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); idx = (idx - 1 + palette.length) % palette.length; }
  else return;
  selectBodyColor(palette[idx], true);
});

$$("#mode-toggle .toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.extrude.mode = btn.dataset.mode;
    $$("#mode-toggle .toggle-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    updateModeVisibility();
    renderFilamentPanel();
  });
});

function updateModeVisibility() {
  $("#lead-field").hidden = state.extrude.mode !== "swap";
}

$$("#hang-toggle .toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.extrude.hang.mode = btn.dataset.hang;
    $$("#hang-toggle .toggle-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    updateHangVisibility();
  });
});

$("#fin-angle").addEventListener("input", () => {
  const v = parseFloat($("#fin-angle").value);
  if (!Number.isNaN(v)) {
    state.extrude.hang.fin_angle_deg = v;
    $("#fin-angle-val").textContent = v;
  }
});

function updateHangVisibility() {
  $("#fin-angle-field").hidden = state.extrude.hang.mode !== "fins";
}

$("#layer-height").addEventListener("input", () => {
  const v = parseFloat($("#layer-height").value);
  if (!Number.isNaN(v) && v > 0) { state.extrude.layer_height_mm = v; refreshColorLayerLabels(); renderFilamentPanel(); }
});
$("#first-layer-height").addEventListener("input", () => {
  const v = parseFloat($("#first-layer-height").value);
  if (!Number.isNaN(v) && v > 0) { state.extrude.first_layer_height_mm = v; refreshColorLayerLabels(); renderFilamentPanel(); }
});
$("#plate-enabled").addEventListener("change", () => {
  state.extrude.plate.enabled = $("#plate-enabled").checked;
  renderFilamentPanel();
});
$("#plate-thickness").addEventListener("input", () => {
  const v = parseFloat($("#plate-thickness").value);
  if (!Number.isNaN(v) && v > 0) { state.extrude.plate.thickness_mm = v; renderFilamentPanel(); }
});
$("#chamfer-enabled").addEventListener("change", () => {
  state.extrude.chamfer.enabled = $("#chamfer-enabled").checked;
});
$("#chamfer-size").addEventListener("input", () => {
  const v = parseFloat($("#chamfer-size").value);
  if (!Number.isNaN(v) && v > 0) state.extrude.chamfer.size_mm = v;
});
$("#initial-lead").addEventListener("input", () => {
  const v = parseFloat($("#initial-lead").value);
  if (!Number.isNaN(v) && v >= 0) { state.extrude.initial_color_switch_mm = v; renderFilamentPanel(); }
});

// ------------------------------------------------------------
// Client-side swap-plan preview. Mirrors app/core/swapplan.py
// compute_plan(). This is only a PREVIEW; the authoritative plan
// comes from the server on Build. Deviation: the backend orders
// sibling tiebreaks by perimeter presence; here we use coverage
// (the only per-color signal available client-side).
// ------------------------------------------------------------
const PLAN_EPS = 1e-9;
const round9 = (x) => Math.round(x * 1e9) / 1e9;

function snapZ(z, lh, flh) {
  const raw = (z - flh) / lh;
  let n = Math.floor(raw + 0.5 + PLAN_EPS);
  if (n < 0) n = 0;
  return { snapped: round9(flh + n * lh), layers: n + 1 };
}

function colorTop(height, plateTop, plateEnabled, lh, flh) {
  if (plateEnabled) {
    let { snapped: total, layers } = snapZ(plateTop + height, lh, flh);
    if (total <= plateTop + PLAN_EPS) {
      total = round9(plateTop + lh);
      layers = snapZ(total, lh, flh).layers;
    }
    return { top: total, layers, snappedRel: round9(total - plateTop) };
  }
  const { snapped: total, layers } = snapZ(height, lh, flh);
  return { top: total, layers, snappedRel: total };
}

// Colors present in the art (coverage > 0), ordered so that within a
// shared snapped top the higher-coverage color is dominant.
function visibleColorsForPlan() {
  const cov = {};
  for (const [hex, frac] of Object.entries(state.coverage || {})) cov[hex.toLowerCase()] = frac;
  // Sibling ("more perimeter presence") ordering must match the server,
  // which sorts by (-perimeter, hex). The preview response supplies the
  // per-color perimeter; coverage is only a fallback.
  const perim = {};
  for (const [hex, px] of Object.entries(state.lastPreview?.color_perimeter || {}))
    perim[hex.toLowerCase()] = px;
  const present = state.extrude.colors.filter((c) => (cov[c.hex.toLowerCase()] || 0) > 0);
  const list = present.length ? present : state.extrude.colors.slice();
  const rank = (c) => perim[c.hex.toLowerCase()] ?? (cov[c.hex.toLowerCase()] || 0);
  return list.slice().sort((a, b) => (rank(b) - rank(a)) || a.hex.localeCompare(b.hex));
}

function computePlan(extrude, visibleColors) {
  const lh = extrude.layer_height_mm;
  const flh = extrude.first_layer_height_mm;
  const body = extrude.main_body_color;
  const plateEnabled = extrude.plate.enabled;
  const plan = { bands: [], events: [], snappedHeights: {}, warnings: [], nSwaps: 0, plateTop: 0, tops: {}, flh };

  let plateTop = 0;
  if (plateEnabled) plateTop = snapZ(extrude.plate.thickness_mm, lh, flh).snapped;
  plan.plateTop = plateTop;

  const tops = {};
  for (const c of visibleColors) {
    const { top, layers, snappedRel } = colorTop(c.height_mm, plateTop, plateEnabled, lh, flh);
    plan.snappedHeights[c.hex] = { requested: c.height_mm, snapped: snappedRel, layers };
    if (!c.first_layer_only) tops[c.hex] = top;
  }
  plan.tops = tops;

  const filaments = new Set(visibleColors.map((c) => c.hex));
  filaments.add(body);
  plan.filamentCount = filaments.size;
  if (filaments.size > 4)
    plan.warnings.push({ code: "ams_slots", msg: `${filaments.size} colors incl. body exceeds AMS lite's 4 slots` });

  if (extrude.mode === "ams") return plan;

  const nonflo = visibleColors.filter((c) => !c.first_layer_only);
  const flo = visibleColors.filter((c) => c.first_layer_only);

  const order = nonflo.map((_, i) => i)
    .sort((a, b) => (tops[nonflo[a].hex] - tops[nonflo[b].hex]) || (a - b));
  const sortedNc = order.map((i) => nonflo[i]);

  const groups = [];
  for (const c of sortedNc) {
    const last = groups[groups.length - 1];
    if (last && Math.abs(tops[last[0].hex] - tops[c.hex]) < 1e-6) last.push(c);
    else groups.push([c]);
  }

  let bands = [];
  if (!groups.length) {
    bands.push({ z0: 0, z1: Math.max(plateTop, flh), hexes: [body] });
  } else {
    let firstForeign = null;
    for (let gi = 0; gi < groups.length; gi++) {
      if (groups[gi][0].hex !== body) { firstForeign = gi; break; }
    }
    if (firstForeign === null) {
      bands.push({ z0: 0, z1: tops[groups[groups.length - 1][0].hex], hexes: [body] });
    } else {
      const tF = tops[groups[firstForeign][0].hex];
      const lead = extrude.initial_color_switch_mm;
      let swap1 = snapZ(tF - lead, lh, flh).snapped;
      let clamped = false;
      if (tF - lead < flh - PLAN_EPS || swap1 < flh - PLAN_EPS) { swap1 = flh; clamped = true; }
      if (swap1 >= tF - PLAN_EPS) { swap1 = Math.max(flh, round9(tF - lh)); clamped = true; }
      if (clamped) plan.warnings.push({ code: "lead_clamped", msg: "initial color-switch lead clamped to fit within the model" });
      bands.push({ z0: 0, z1: swap1, hexes: [body] });
      let prev = swap1;
      for (let gi = firstForeign; gi < groups.length; gi++) {
        bands.push({ z0: prev, z1: tops[groups[gi][0].hex], hexes: groups[gi].map((c) => c.hex) });
        prev = tops[groups[gi][0].hex];
      }
    }
  }

  const merged = [];
  for (const b of bands) {
    const last = merged[merged.length - 1];
    if (last && last.hexes[0] === b.hexes[0]) {
      last.z1 = b.z1;
      for (const h of b.hexes) if (!last.hexes.includes(h)) last.hexes.push(h);
    } else merged.push({ z0: b.z0, z1: b.z1, hexes: b.hexes.slice() });
  }
  plan.bands = merged;

  for (let i = 1; i < merged.length; i++) {
    const z = merged[i].z0;
    plan.events.push({ z_mm: z, layer: snapZ(z, lh, flh).layers, from: merged[i - 1].hexes[0], to: merged[i].hexes[0] });
  }

  const siblingExtra = groups.reduce((s, g) => s + g.length - 1, 0);
  const floNonbody = [...new Set(flo.filter((c) => c.hex !== body).map((c) => c.hex))];
  plan.floNonbody = floNonbody;
  const floExtra = floNonbody.length ? floNonbody.length + 1 : 0;
  plan.nSwaps = plan.events.length + siblingExtra + floExtra;
  return plan;
}

// ------------------------------------------------------------
// Filament-by-height panel (image + vertical bar).
// ------------------------------------------------------------
function bandFill(hexes) {
  if (hexes.length === 1) return hexes[0];
  // Equal-top siblings: split the segment vertically side-by-side.
  const step = 100 / hexes.length;
  const stops = hexes.map((h, i) => `${h} ${(i * step).toFixed(2)}%, ${h} ${((i + 1) * step).toFixed(2)}%`);
  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

function renderFilamentPanel() {
  if (!state.approvedProcess) return;
  const img = $("#extrude-preview-img");
  if (state.lastPreview && state.lastPreview.preview_png) {
    img.src = state.lastPreview.preview_png;
    img.hidden = false;
  } else {
    img.hidden = true;
  }

  const bar = $("#fbar");
  const scale = $("#fbar-scale");
  const headline = $("#fbar-headline");
  const note = $("#fbar-note");
  bar.innerHTML = "";
  scale.innerHTML = "";
  note.hidden = true;

  const plan = computePlan(extrudeStateForPlan(), visibleColorsForPlan());

  if (state.extrude.mode === "ams") {
    renderAmsBar(plan, bar, scale, headline, note);
    return;
  }

  const bands = plan.bands;
  headline.textContent = `${plan.nSwaps} color switch${plan.nSwaps === 1 ? "" : "es"}`;
  if (!bands.length) { bar.innerHTML = `<div class="fbar-empty">No plan yet</div>`; return; }

  const total = bands[bands.length - 1].z1 || plan.flh;
  const boundaries = new Set([0]);
  for (const b of bands) { boundaries.add(round9(b.z0)); boundaries.add(round9(b.z1)); }

  for (const b of bands) {
    const seg = document.createElement("div");
    seg.className = "fseg";
    seg.style.bottom = `${(b.z0 / total) * 100}%`;
    seg.style.height = `${((b.z1 - b.z0) / total) * 100}%`;
    seg.style.background = bandFill(b.hexes);
    seg.title = `${b.hexes.join(" + ")}  ·  ${b.z0.toFixed(2)}–${b.z1.toFixed(2)} mm`;
    bar.appendChild(seg);
  }

  // First-layer-only stripe at the very bottom.
  if (plan.floNonbody && plan.floNonbody.length) {
    const flo = document.createElement("div");
    flo.className = "fseg fseg-flo";
    flo.style.bottom = "0%";
    flo.style.height = `${(plan.flh / total) * 100}%`;
    flo.style.background = bandFill(plan.floNonbody);
    flo.title = `first layer only: ${plan.floNonbody.join(" + ")}  ·  0–${plan.flh.toFixed(2)} mm`;
    bar.appendChild(flo);
  }

  // Plate zone marker.
  if (plan.plateTop > 0) {
    const pz = document.createElement("div");
    pz.className = "fbar-plate";
    pz.style.bottom = "0%";
    pz.style.height = `${(plan.plateTop / total) * 100}%`;
    pz.title = `backing plate  ·  0–${plan.plateTop.toFixed(2)} mm`;
    pz.innerHTML = `<span>plate</span>`;
    bar.appendChild(pz);
  }

  renderScaleLabels(scale, [...boundaries].sort((a, b) => a - b), total);
}

function renderAmsBar(plan, bar, scale, headline, note) {
  const m = plan.filamentCount;
  headline.textContent = `${m} filament${m === 1 ? "" : "s"} (incl. body)`;
  if (m > 4) {
    note.textContent = `${m} filaments exceed AMS lite's 4 slots.`;
    note.hidden = false;
  }
  const tops = plan.tops;
  const topVals = Object.values(tops);
  const total = topVals.length ? Math.max(...topVals) : plan.flh;
  const body = state.extrude.main_body_color;

  // Full body bar, then a cap marker (top ~3 layers) at each color's top.
  const base = document.createElement("div");
  base.className = "fseg";
  base.style.bottom = "0%";
  base.style.height = "100%";
  base.style.background = body;
  base.title = `body ${body}  ·  0–${total.toFixed(2)} mm`;
  bar.appendChild(base);

  const cap = 3 * state.extrude.layer_height_mm;
  // Group colors sharing a top so overlapping caps stripe together.
  const byTop = new Map();
  for (const [hex, t] of Object.entries(tops)) {
    const key = round9(t);
    if (!byTop.has(key)) byTop.set(key, []);
    byTop.get(key).push(hex);
  }
  const boundaries = new Set([0, round9(total)]);
  for (const [t, hexes] of byTop.entries()) {
    boundaries.add(round9(t));
    const seg = document.createElement("div");
    seg.className = "fseg";
    const h = Math.min(cap, t);
    seg.style.bottom = `${((t - h) / total) * 100}%`;
    seg.style.height = `${(h / total) * 100}%`;
    seg.style.background = bandFill(hexes);
    seg.title = `${hexes.join(" + ")} cap  ·  ${t.toFixed(2)} mm`;
    bar.appendChild(seg);
  }

  if (plan.plateTop > 0) {
    const pz = document.createElement("div");
    pz.className = "fbar-plate";
    pz.style.bottom = "0%";
    pz.style.height = `${(plan.plateTop / total) * 100}%`;
    pz.title = `backing plate  ·  0–${plan.plateTop.toFixed(2)} mm`;
    pz.innerHTML = `<span>plate</span>`;
    bar.appendChild(pz);
  }

  renderScaleLabels(scale, [...boundaries].sort((a, b) => a - b), total);
}

function renderScaleLabels(scale, zs, total) {
  if (total <= 0) return;
  for (const z of zs) {
    const lab = document.createElement("div");
    lab.className = "fscale-tick";
    lab.style.bottom = `${(z / total) * 100}%`;
    lab.textContent = `${z.toFixed(1)}`;
    scale.appendChild(lab);
  }
}

// The plan mirror wants an extrude-shaped object; reuse the live state
// directly rather than the wire body so it always reflects edits.
function extrudeStateForPlan() {
  return {
    mode: state.extrude.mode,
    layer_height_mm: state.extrude.layer_height_mm,
    first_layer_height_mm: state.extrude.first_layer_height_mm,
    plate: { enabled: state.extrude.plate.enabled, thickness_mm: state.extrude.plate.thickness_mm },
    main_body_color: state.extrude.main_body_color,
    initial_color_switch_mm: state.extrude.initial_color_switch_mm,
  };
}

function extrudeParamsBody() {
  return {
    mode: state.extrude.mode,
    layer_height_mm: state.extrude.layer_height_mm,
    first_layer_height_mm: state.extrude.first_layer_height_mm,
    plate: {
      enabled: state.extrude.plate.enabled,
      thickness_mm: state.extrude.plate.thickness_mm,
    },
    main_body_color: state.extrude.main_body_color,
    colors: state.extrude.colors.map((c) => ({
      hex: c.hex,
      height_mm: c.height_mm,
      first_layer_only: c.first_layer_only,
    })),
    initial_color_switch_mm: state.extrude.initial_color_switch_mm,
    chamfer: {
      enabled: state.extrude.chamfer.enabled,
      size_mm: state.extrude.chamfer.size_mm,
    },
    hang: {
      mode: state.extrude.hang.mode,
      fin_angle_deg: state.extrude.hang.fin_angle_deg,
    },
  };
}

$("#extrude-back").addEventListener("click", () => goToStep(2));
$("#extrude-build").addEventListener("click", buildModel);

async function buildModel() {
  const btn = $("#extrude-build");
  btn.disabled = true;
  btn.textContent = "Building…";
  $("#model-warnings").innerHTML = "";
  try {
    const data = await postJson(`/api/sessions/${state.session.session_id}/model`, {
      process: state.approvedProcess,
      extrude: extrudeParamsBody(),
    });
    state.model = data;
    renderWarnings($("#model-warnings"), data.warnings);
    goToStep(4);
  } catch (err) {
    toast(err.message, "Build failed");
  } finally {
    btn.disabled = false;
    btn.textContent = "Build model →";
  }
}

function renderWarnings(container, warnings) {
  container.innerHTML = "";
  (warnings || []).forEach((w) => {
    const el = document.createElement("div");
    el.className = "warning-banner";
    el.innerHTML = `<span>⚠</span>
      <div class="wb-msg"><span class="wb-code">${w.code}</span><div></div></div>
      <button class="wb-close" aria-label="Dismiss">×</button>`;
    el.querySelector(".wb-msg div").textContent = w.msg;
    el.querySelector(".wb-close").addEventListener("click", () => el.remove());
    container.appendChild(el);
  });
}

// ============================================================
// SCREEN 4 — Review & download
// ============================================================
let viewer = null; // { renderer, scene, camera, controls, raf }

function renderScreen4() {
  const m = state.model;
  if (!m) return;

  renderWarnings($("#review-warnings"), m.warnings);

  // Swap plan
  $("#nswaps-pill").textContent = `${m.n_swaps} swap${m.n_swaps === 1 ? "" : "s"}`;
  const swapWrap = $("#swap-plan-wrap");
  if (state.extrude.mode === "ams") {
    swapWrap.innerHTML = `<p class="empty-note">AMS mode: map filaments to parts on import.</p>`;
  } else if (!m.swap_plan || !m.swap_plan.length) {
    swapWrap.innerHTML = `<p class="empty-note">No swaps needed.</p>`;
  } else {
    let rows = "";
    for (const s of m.swap_plan) {
      rows += `<tr>
        <td>${s.z_mm.toFixed(2)}</td>
        <td>${s.layer}</td>
        <td>${colorChip(s.from)} <span class="chip-arrow">→</span> ${colorChip(s.to)}</td>
      </tr>`;
    }
    swapWrap.innerHTML = `<div class="table-scroll"><table class="data-table">
      <thead><tr><th>Z (mm)</th><th>Layer</th><th>From → To</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  // Snapped heights
  const snapWrap = $("#snapped-wrap");
  const entries = Object.entries(m.snapped_heights || {});
  if (!entries.length) {
    snapWrap.innerHTML = `<p class="empty-note">No height data.</p>`;
  } else {
    let rows = "";
    for (const [hex, h] of entries) {
      rows += `<tr>
        <td>${colorChip(hex)}</td>
        <td>${h.requested.toFixed(2)}</td>
        <td>${h.snapped.toFixed(2)}</td>
        <td>${h.layers}</td>
      </tr>`;
    }
    snapWrap.innerHTML = `<div class="table-scroll"><table class="data-table">
      <thead><tr><th>Color</th><th>Requested</th><th>Snapped</th><th>Layers</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  // Downloads
  $("#dl-3mf").href = m.downloads.threemf;
  $("#dl-png").href = m.downloads.png;

  loadViewer(m.preview_glb);
}

function colorChip(hex) {
  return `<span class="chip"><span class="chip-swatch" style="background:${hex}"></span>${hex}</span>`;
}

// ---------- three.js viewer ----------
function disposeViewer() {
  if (!viewer) return;
  cancelAnimationFrame(viewer.raf);
  viewer.controls.dispose();
  viewer.renderer.dispose();
  if (viewer.renderer.domElement && viewer.renderer.domElement.parentNode) {
    viewer.renderer.domElement.parentNode.removeChild(viewer.renderer.domElement);
  }
  if (viewer.onResize) window.removeEventListener("resize", viewer.onResize);
  viewer = null;
}

function loadViewer(glbUrl) {
  disposeViewer();
  const container = $("#viewer");
  const loading = $("#viewer-loading");
  loading.hidden = false;

  const width = container.clientWidth || 640;
  const height = container.clientHeight || 480;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0e14);

  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 5000);
  camera.position.set(0, 0, 200);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height);
  container.appendChild(renderer.domElement);

  // Neutral studio lighting
  const hemi = new THREE.HemisphereLight(0xffffff, 0x444455, 1.0);
  scene.add(hemi);
  const dir = new THREE.DirectionalLight(0xffffff, 1.6);
  dir.position.set(60, 120, 100);
  scene.add(dir);
  const dir2 = new THREE.DirectionalLight(0xffffff, 0.5);
  dir2.position.set(-80, -40, -60);
  scene.add(dir2);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  const grid = new THREE.GridHelper(400, 40, 0x2a323d, 0x1b222c);
  scene.add(grid);

  viewer = { renderer, scene, camera, controls, raf: 0 };

  const loader = new GLTFLoader();
  loader.load(
    glbUrl,
    (gltf) => {
      const model = gltf.scene;
      scene.add(model);

      // Frame the model via bounding box
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z) || 100;

      // Sit the model on the grid.
      model.position.y -= box.min.y;
      center.y -= box.min.y;

      grid.scale.setScalar(Math.max(1, maxDim / 100));

      const dist = maxDim * 2.2;
      camera.position.set(center.x + dist * 0.6, center.y + dist * 0.5, center.z + dist);
      camera.near = maxDim / 100;
      camera.far = maxDim * 100;
      camera.updateProjectionMatrix();
      controls.target.copy(center);
      controls.update();

      loading.hidden = true;
    },
    undefined,
    (err) => {
      loading.hidden = true;
      toast("Could not load 3D preview.", "Viewer error");
      console.warn(err);
    },
  );

  const onResize = () => {
    if (!viewer) return;
    const w = container.clientWidth || width;
    const h = container.clientHeight || height;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  };
  viewer.onResize = onResize;
  window.addEventListener("resize", onResize);

  const animate = () => {
    if (!viewer) return;
    viewer.raf = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  };
  animate();
}

$("#review-back").addEventListener("click", () => { disposeViewer(); goToStep(3); });
$("#review-restart").addEventListener("click", restart);

function restart() {
  disposeViewer();
  if (state.imageObjectUrl) { URL.revokeObjectURL(state.imageObjectUrl); }
  state.session = null;
  state.imageFile = null;
  state.imageObjectUrl = null;
  state.palette = [];
  state.process.palette = [];
  state.quantizePalette = null;
  state.lastPreview = null;
  state.approvedProcess = null;
  state.coverage = {};
  state.extrude.colors = [];
  state.extrude.main_body_color = null;
  state.model = null;

  $("#dropzone-empty").hidden = false;
  $("#dropzone-preview").hidden = true;
  $("#upload-thumb").src = "";
  $("#upload-meta").textContent = "";
  $("#upload-next").disabled = true;
  fileInput.value = "";
  $("#preview-img").hidden = true;
  $("#preview-placeholder").hidden = false;
  $("#clean-approve").disabled = true;

  goToStep(1);
}

// ============================================================
// Boot
// ============================================================
goToStep(1);
