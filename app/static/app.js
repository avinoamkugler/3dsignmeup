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
    target_width_mm: 160,   // width of the trimmed artwork, not of the upload
    palette: [],
    smoothness: 1,
    min_feature_mm: 0.5,
    clear_background: true,
    enclosed_background: "plate",
    paint_fills: [],    // [{x, y, to_hex}] paint-bucket ops; to_hex null = erase
  },
  paintHex: null,       // selected fill-with palette hex
  paintErase: false,    // when true, clicks erase to hole instead of recoloring
  lastPreview: null,    // last successful PreviewResponse
  approvedProcess: null,// exact ProcessParams carried into screen 3
  coverage: {},         // color_coverage from last preview
  extrude: {            // ExtrudeParams (screen 3)
    mode: "swap",
    layer_height_mm: 0.2,
    plate: { enabled: true, thickness_mm: 2.0 },
    main_body_color: null,
    colors: [],         // [{hex, height_mm, first_layer_only}]
    initial_color_switch_mm: 0.4,
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
    state.process.paint_fills = [];
    state.paintHex = state.palette[0] || null;
    state.paintErase = false;
    snapshotQuantizePalette(); // freeze pixel->label assignment against the freshly detected palette
    $("#upload-meta").textContent =
      `${data.width_px} × ${data.height_px}px · ${state.palette.length} colors detected`;
    updatePaintUI();
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

// Full-resolution copy, not the 500px display logo: this feeds the real
// pipeline, and vectorized curves get visibly faceted at low mm-per-px.
const DEMO_URL = "/static/logo-full.png";
const demoBtn = $("#demo-load");
demoBtn.addEventListener("click", async () => {
  demoBtn.disabled = true;
  try {
    const blob = await (await apiFetch(DEMO_URL)).blob();
    await handleFile(new File([blob], "3dsignmeup-logo.png", { type: "image/png" }));
  } catch (err) {
    toast(err.message, "Could not load the demo image");
  } finally {
    demoBtn.disabled = false;
  }
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
  if (!state.paintHex || !state.palette.includes(state.paintHex)) {
    state.paintHex = state.palette[0] || null;
  }
  renderPaletteSwatches();
  updatePaintUI();
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
  state.process.paint_fills = [];
  state.paintHex = state.palette[0] || null;
  state.paintErase = false;
  snapshotQuantizePalette(); // re-freeze to match the restored palette
  $("#ncolors-value").textContent = state.palette.length;
  renderPaletteSwatches();
  updatePaintUI();
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
    state.process.paint_fills = [];
    state.paintHex = state.palette[0] || null;
    state.paintErase = false;
    snapshotQuantizePalette(); // different cardinality can't reuse the old frozen labels
    $("#ncolors-value").textContent = state.palette.length;
    renderPaletteSwatches();
    updatePaintUI();
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

function updatePaintUI() {
  $("#preview-stage").classList.add("paint-cursor");
  if (!state.paintErase && (!state.paintHex || !state.palette.includes(state.paintHex))) {
    state.paintHex = state.palette[0] || null;
  }
  const wrap = $("#paint-swatches");
  wrap.innerHTML = "";
  state.palette.forEach((hex) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "paint-chip" + (!state.paintErase && hex === state.paintHex ? " selected" : "");
    btn.style.background = hex;
    btn.title = hex;
    btn.addEventListener("click", () => {
      state.paintErase = false;
      state.paintHex = hex;
      updatePaintUI();
    });
    wrap.appendChild(btn);
  });
  const eraseBtn = $("#paint-erase");
  eraseBtn.classList.toggle("selected", state.paintErase);
  const n = state.process.paint_fills.length;
  $("#paint-undo").disabled = n === 0;
  $("#paint-clear").disabled = n === 0;
  $("#paint-count").textContent = n ? `${n} paint${n === 1 ? "" : "s"}` : "";
}

$("#paint-erase").addEventListener("click", () => {
  state.paintErase = true;
  updatePaintUI();
});

$("#paint-undo").addEventListener("click", () => {
  if (!state.process.paint_fills.length) return;
  state.process.paint_fills.pop();
  updatePaintUI();
  schedulePreview();
});

$("#paint-clear").addEventListener("click", () => {
  if (!state.process.paint_fills.length) return;
  state.process.paint_fills = [];
  updatePaintUI();
  schedulePreview();
});

function imagePixelFromClick(img, clientX, clientY) {
  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth;
  const nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const scale = Math.min(rect.width / nw, rect.height / nh);
  const dispW = nw * scale;
  const dispH = nh * scale;
  const offX = (rect.width - dispW) / 2;
  const offY = (rect.height - dispH) / 2;
  const x = Math.floor((clientX - rect.left - offX) / scale);
  const y = Math.floor((clientY - rect.top - offY) / scale);
  if (x < 0 || y < 0 || x >= nw || y >= nh) return null;
  return { x, y };
}

$("#preview-stage").addEventListener("click", (e) => {
  if (!state.paintErase && !state.paintHex) return;
  const img = $("#preview-img");
  if (!img || img.hidden || !img.naturalWidth) return;
  const pix = imagePixelFromClick(img, e.clientX, e.clientY);
  if (!pix) return;
  state.process.paint_fills.push({
    x: pix.x,
    y: pix.y,
    to_hex: state.paintErase ? null : state.paintHex,
  });
  updatePaintUI();
  schedulePreview();
});

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
    paint_fills: state.process.paint_fills.map((f) => ({
      x: f.x, y: f.y, to_hex: f.to_hex,
    })),
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
  // Heights are totals from the bed, so the default has to clear the plate to
  // give the same 3 mm of relief a plate-less sign gets.
  const defaultHeight = snapToLayerMultiple(
    plateTopMm() + 3.0, state.extrude.layer_height_mm,
  );
  state.extrude.colors = palette.map((hex) => {
    const p = prior.get(hex);
    return {
      hex,
      height_mm: p ? p.height_mm : defaultHeight,
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
  $("#plate-enabled").checked = state.extrude.plate.enabled;
  $("#plate-thickness").value = state.extrude.plate.thickness_mm;
  $("#chamfer-enabled").checked = state.extrude.chamfer.enabled;
  $("#chamfer-size").value = state.extrude.chamfer.size_mm;
  $("#initial-lead").value = state.extrude.initial_color_switch_mm;

  $$("#hang-toggle .toggle-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.hang === state.extrude.hang.mode));
  $("#fin-angle").value = state.extrude.hang.fin_angle_deg;
  $("#fin-angle-val").textContent = state.extrude.hang.fin_angle_deg;

  syncHeightInputSteps();
  renderColorHeights();
  renderBodyColor();
  updateModeVisibility();
  updateHangVisibility();
  renderFilamentPanel();
}

function snapToLayerMultiple(value, lh, { allowZero = false } = {}) {
  if (allowZero && (!(value > 0) || value < lh * 0.5)) return 0;
  const n = Math.max(1, Math.round(value / lh));
  return Math.round(n * lh * 1e9) / 1e9;
}

function syncHeightInputSteps() {
  const lh = state.extrude.layer_height_mm;
  const plate = $("#plate-thickness");
  plate.step = lh;
  plate.min = lh;
  const lead = $("#initial-lead");
  lead.step = lh;
}

function plateTopMm() {
  if (!state.extrude.plate.enabled) return 0;
  return snapZ(state.extrude.plate.thickness_mm, state.extrude.layer_height_mm).snapped;
}

// A height is the color's TOTAL thickness from the bed. With a plate the second
// number people actually need is the relief left above it.
function heightInfo(height) {
  const lh = state.extrude.layer_height_mm;
  const enabled = state.extrude.plate.enabled;
  const { top, layers, relief } = colorTop(height, plateTopMm(), enabled, lh);
  const text =
    `→ ${layers} layer${layers === 1 ? "" : "s"} (${top.toFixed(1)} mm` +
    (enabled ? ` · ${relief.toFixed(1)} mm above plate)` : ")");
  return { top, layers, relief, text };
}

function renderColorHeights() {
  const wrap = $("#color-heights");
  wrap.innerHTML = "";
  const lh = state.extrude.layer_height_mm;
  const min = round9(plateTopMm() + lh);
  state.extrude.colors.forEach((c) => {
    const row = document.createElement("div");
    row.className = "ch-row";
    row.innerHTML = `
      <span class="ch-swatch" style="background:${c.hex}"></span>
      <input type="number" min="${min}" step="${lh}" value="${c.height_mm}" />
      <span class="ch-layers">${heightInfo(c.height_mm).text}</span>
      <label class="ch-flo checkbox"><input type="checkbox" ${c.first_layer_only ? "checked" : ""} /> 1st layer only</label>`;
    const numInput = row.querySelector('input[type="number"]');
    const chk = row.querySelector('input[type="checkbox"]');
    numInput.addEventListener("input", () => {
      const v = parseFloat(numInput.value);
      if (!Number.isNaN(v) && v > 0) {
        c.height_mm = v;
        row.querySelector(".ch-layers").textContent = heightInfo(v).text;
        renderFilamentPanel();
      }
    });
    chk.addEventListener("change", () => { c.first_layer_only = chk.checked; renderFilamentPanel(); });
    wrap.appendChild(row);
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
  const mode = state.extrude.hang.mode;
  $("#fin-angle-field").hidden = mode !== "fins";
  const hint = $("#hang-depth-hint");
  const depths = hangDepthMm(mode);
  if (depths == null) {
    hint.hidden = true;
    hint.textContent = "";
    return;
  }
  hint.hidden = false;
  const forWhat = mode === "keyhole" ? " (#6 pan-head / small nail)" : "";
  hint.textContent =
    `Needs ≥ ${depths.min.toFixed(1)} mm at hang points` +
    ` (adapts up to ${depths.nominal.toFixed(1)} mm)` +
    ` — plate + contiguous body above${forWhat}.`;
}

// Mirrors app.core.hanging keyhole/fins min + nominal depth helpers.
function hangDepthMm(mode) {
  if (mode === "keyhole") return { min: 2.5 + 1.0, nominal: 3.7 + 1.0 };
  if (mode === "fins") return { min: 2.0 + 0.2 + 1.0, nominal: 3.5 + 0.2 + 1.0 };
  return null;
}

$("#layer-height").addEventListener("input", () => {
  const v = parseFloat($("#layer-height").value);
  if (Number.isNaN(v) || v <= 0) return;
  state.extrude.layer_height_mm = v;
  const lh = v;
  state.extrude.plate.thickness_mm = snapToLayerMultiple(state.extrude.plate.thickness_mm, lh);
  state.extrude.initial_color_switch_mm = snapToLayerMultiple(
    state.extrude.initial_color_switch_mm, lh, { allowZero: true }
  );
  state.extrude.colors.forEach((c) => {
    c.height_mm = snapToLayerMultiple(c.height_mm, lh);
  });
  $("#plate-thickness").value = state.extrude.plate.thickness_mm;
  $("#initial-lead").value = state.extrude.initial_color_switch_mm;
  syncHeightInputSteps();
  renderColorHeights();
  renderFilamentPanel();
});
// The plate is part of every height, so its controls also change what each
// per-color row reports.
$("#plate-enabled").addEventListener("change", () => {
  state.extrude.plate.enabled = $("#plate-enabled").checked;
  renderColorHeights();
  renderFilamentPanel();
});
$("#plate-thickness").addEventListener("input", () => {
  const v = parseFloat($("#plate-thickness").value);
  if (!Number.isNaN(v) && v > 0) {
    state.extrude.plate.thickness_mm = v;
    renderColorHeights();
    renderFilamentPanel();
  }
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

function snapZ(z, lh) {
  const raw = z / lh;
  let n = Math.floor(raw + 0.5 + PLAN_EPS);
  if (n < 1) n = 1;
  return { snapped: round9(n * lh), layers: n };
}

// Heights are TOTAL thickness from the bed: the plate is part of a color's
// height, not an addition to it. Mirrors app/core/swapplan.py color_top().
function colorTop(height, plateTop, plateEnabled, lh) {
  let { snapped: total, layers } = snapZ(height, lh);
  if (plateEnabled && total <= plateTop + PLAN_EPS) {
    total = round9(plateTop + lh);
    layers = snapZ(total, lh).layers;
  }
  return {
    top: total,
    layers,
    relief: plateEnabled ? round9(total - plateTop) : total,
  };
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
  const body = extrude.main_body_color;
  const plateEnabled = extrude.plate.enabled;
  const plan = { bands: [], events: [], snappedHeights: {}, warnings: [], nSwaps: 0, plateTop: 0, tops: {}, flh: lh };

  let plateTop = 0;
  if (plateEnabled) plateTop = snapZ(extrude.plate.thickness_mm, lh).snapped;
  plan.plateTop = plateTop;

  const tops = {};
  for (const c of visibleColors) {
    const { top, layers, relief } = colorTop(c.height_mm, plateTop, plateEnabled, lh);
    plan.snappedHeights[c.hex] = { requested: c.height_mm, snapped: top, layers, relief };
    if (!c.first_layer_only) tops[c.hex] = top;
  }
  plan.tops = tops;

  const filaments = new Set(visibleColors.map((c) => c.hex));
  filaments.add(body);
  plan.filamentCount = filaments.size;
  if (extrude.mode === "ams") {
    if (filaments.size > 4)
      plan.warnings.push({ code: "ams_slots", msg: `${filaments.size} colors incl. body exceeds AMS lite's 4 slots` });
    return plan;
  }

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
  const groupZ0 = groups.map(() => null);
  if (!groups.length) {
    bands.push({ z0: 0, z1: Math.max(plateTop, lh), hexes: [body] });
  } else {
    let firstForeign = null;
    for (let gi = 0; gi < groups.length; gi++) {
      if (groups[gi].some((c) => c.hex !== body)) { firstForeign = gi; break; }
    }
    if (firstForeign === null) {
      bands.push({ z0: 0, z1: tops[groups[groups.length - 1][0].hex], hexes: [body] });
      for (let gi = 0; gi < groups.length; gi++)
        groupZ0[gi] = gi === 0 ? 0 : tops[groups[gi - 1][0].hex];
    } else {
      const tF = tops[groups[firstForeign][0].hex];
      const lead = extrude.initial_color_switch_mm;
      let swap1 = snapZ(tF - lead, lh).snapped;
      let clamped = false;
      if (tF - lead < lh - PLAN_EPS || swap1 < lh - PLAN_EPS) { swap1 = lh; clamped = true; }
      if (swap1 >= tF - PLAN_EPS) { swap1 = Math.max(lh, round9(tF - lh)); clamped = true; }
      if (clamped) plan.warnings.push({ code: "lead_clamped", msg: "initial color-switch lead clamped to fit within the model" });
      bands.push({ z0: 0, z1: swap1, hexes: [body] });
      let prev = swap1;
      for (let gi = firstForeign; gi < groups.length; gi++) {
        groupZ0[gi] = prev;
        bands.push({ z0: prev, z1: tops[groups[gi][0].hex], hexes: groups[gi].map((c) => c.hex) });
        prev = tops[groups[gi][0].hex];
      }
      for (let gi = 0; gi < firstForeign; gi++)
        groupZ0[gi] = gi === 0 ? 0 : tops[groups[gi - 1][0].hex];
    }
  }

  // Merge only pure single-color runs so sibling stripes keep their true z0.
  const merged = [];
  for (const b of bands) {
    const last = merged[merged.length - 1];
    if (
      last &&
      last.hexes.length === 1 &&
      b.hexes.length === 1 &&
      last.hexes[0] === b.hexes[0]
    ) {
      last.z1 = b.z1;
    } else {
      merged.push({ z0: b.z0, z1: b.z1, hexes: b.hexes.slice() });
    }
  }
  plan.bands = merged;

  for (let i = 1; i < merged.length; i++) {
    const z = merged[i].z0;
    // Band-boundary events use the dominant (first) hex of each segment.
    if (merged[i - 1].hexes[0] !== merged[i].hexes[0]) {
      plan.events.push({
        z_mm: z,
        layer: snapZ(z, lh).layers,
        from: merged[i - 1].hexes[0],
        to: merged[i].hexes[0],
      });
    }
  }

  plan.siblingSpans = [];
  for (let gi = 0; gi < groups.length; gi++) {
    const g = groups[gi];
    if (g.length < 2 || groupZ0[gi] == null) continue;
    const z0 = groupZ0[gi];
    const z1 = tops[g[0].hex];
    const hexes = g.map((c) => c.hex);
    plan.siblingSpans.push({ z0, z1, hexes });
    const layer = snapZ(z0, lh).layers;
    for (let i = 1; i < hexes.length; i++)
      plan.events.push({ z_mm: z0, layer, from: hexes[0], to: hexes[i] });
  }

  plan.events.sort((a, b) => (a.z_mm - b.z_mm) || a.from.localeCompare(b.from) || a.to.localeCompare(b.to));
  const floNonbody = [...new Set(flo.filter((c) => c.hex !== body).map((c) => c.hex))];
  plan.floNonbody = floNonbody;
  plan.nSwaps = plan.events.length;
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

  if (plan.siblingSpans && plan.siblingSpans.length) {
    const n = plan.siblingSpans.reduce((s, sp) => s + sp.hexes.length - 1, 0);
    note.textContent =
      `${n} mid-layer swap${n === 1 ? "" : "s"} for equal-height colors ` +
      `(striped segments on the bar).`;
    note.hidden = false;
  }

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

  // A color whose top is not a band edge (the body color's own top, or any
  // color sharing the band's filament) changes no filament, so it would leave
  // no trace on the bar at all. Mark those tops: without them, editing the body
  // color's height looks like it does nothing.
  const marks = [];
  for (const [hex, t] of Object.entries(plan.tops)) {
    if ([...boundaries].some((b) => Math.abs(b - t) < 1e-6)) continue;
    marks.push(hex);
    boundaries.add(round9(t));
    const mark = document.createElement("div");
    mark.className = "fmark";
    mark.style.bottom = `${(t / total) * 100}%`;
    mark.title = `${hex} top  ·  ${t.toFixed(2)} mm  (no filament change here)`;
    bar.appendChild(mark);
  }

  // Plate zone marker.
  if (plan.plateTop > 0) {
    boundaries.add(round9(plan.plateTop));
    const pz = document.createElement("div");
    pz.className = "fbar-plate";
    pz.style.bottom = "0%";
    pz.style.height = `${(plan.plateTop / total) * 100}%`;
    pz.title = `backing plate  ·  0–${plan.plateTop.toFixed(2)} mm`;
    pz.innerHTML = `<span>plate</span>`;
    bar.appendChild(pz);
  }

  if (marks.length) {
    const extra =
      `Dashed: top of ${marks.join(", ")} — same filament as the band, so no swap.`;
    note.textContent = note.hidden ? extra : `${note.textContent} ${extra}`;
    note.hidden = false;
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
    boundaries.add(round9(plan.plateTop));
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
    plate: { enabled: state.extrude.plate.enabled, thickness_mm: state.extrude.plate.thickness_mm },
    main_body_color: state.extrude.main_body_color,
    initial_color_switch_mm: state.extrude.initial_color_switch_mm,
  };
}

function extrudeParamsBody() {
  return {
    mode: state.extrude.mode,
    layer_height_mm: state.extrude.layer_height_mm,
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
  const list = (warnings || []).filter((w) => {
    if (w.code === "ams_slots" && state.extrude.mode !== "ams") return false;
    return true;
  });
  list.forEach((w) => {
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
    // Heights are totals from the bed; with a plate, show what is left above it.
    const plateTop = plateTopMm();
    let rows = "";
    for (const [hex, h] of entries) {
      rows += `<tr>
        <td>${colorChip(hex)}</td>
        <td>${h.requested.toFixed(2)}</td>
        <td>${h.snapped.toFixed(2)}</td>
        ${plateTop > 0 ? `<td>${(h.snapped - plateTop).toFixed(2)}</td>` : ""}
        <td>${h.layers}</td>
      </tr>`;
    }
    snapWrap.innerHTML = `<div class="table-scroll"><table class="data-table">
      <thead><tr><th>Color</th><th>Requested</th><th>Snapped total</th>${
        plateTop > 0 ? "<th>Above plate</th>" : ""
      }<th>Layers</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  // Downloads
  $("#dl-3mf").href = m.downloads.threemf;
  $("#dl-png").href = m.downloads.png;
  $("#dl-zip").href = m.downloads.zip;
  const dlFins = $("#dl-fins");
  if (m.downloads.fins_threemf) {
    dlFins.href = m.downloads.fins_threemf;
    dlFins.hidden = false;
  } else {
    dlFins.removeAttribute("href");
    dlFins.hidden = true;
  }

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
  if (viewer.onMiddleDown) {
    viewer.renderer.domElement.removeEventListener("mousedown", viewer.onMiddleDown);
  }
  viewer.renderer.dispose();
  if (viewer.renderer.domElement && viewer.renderer.domElement.parentNode) {
    viewer.renderer.domElement.parentNode.removeChild(viewer.renderer.domElement);
  }
  if (viewer.onResize) window.removeEventListener("resize", viewer.onResize);
  exitViewerFullscreen();
  viewer = null;
}

// ---------- fullscreen ----------
function fullscreenEl() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}

function toggleViewerFullscreen() {
  const el = $("#viewer");
  if (fullscreenEl()) {
    exitViewerFullscreen();
    return;
  }
  const req = el.requestFullscreen || el.webkitRequestFullscreen;
  if (!req) { toast("This browser has no fullscreen API.", "Fullscreen unavailable"); return; }
  Promise.resolve(req.call(el)).catch((err) => toast(err.message, "Fullscreen failed"));
}

function exitViewerFullscreen() {
  if (!fullscreenEl()) return;
  const exit = document.exitFullscreen || document.webkitExitFullscreen;
  if (exit) Promise.resolve(exit.call(document)).catch(() => {});
}

function onFullscreenChange() {
  const on = fullscreenEl() === $("#viewer");
  const btn = $("#viewer-fullscreen");
  btn.textContent = on ? "⤡" : "⛶";
  btn.title = on ? "Exit fullscreen (Esc)" : "Fullscreen";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  // The element's box updates after the event fires, so resize on the next frame.
  if (viewer && viewer.onResize) requestAnimationFrame(viewer.onResize);
}

$("#viewer-fullscreen").addEventListener("click", toggleViewerFullscreen);
document.addEventListener("fullscreenchange", onFullscreenChange);
document.addEventListener("webkitfullscreenchange", onFullscreenChange);

function loadViewer(glbUrl) {
  disposeViewer();
  const container = $("#viewer");
  const loading = $("#viewer-loading");
  loading.hidden = false;

  const width = container.clientWidth || 640;
  const height = container.clientHeight || 480;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x120d0b);

  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 5000);
  camera.position.set(0, 0, 200);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height);
  container.appendChild(renderer.domElement);

  // Neutral studio lighting
  const hemi = new THREE.HemisphereLight(0xffffff, 0x554438, 1.0);
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
  // Slicer/CAD convention (Bambu Studio, Fusion): left orbits, the scroll
  // wheel *pressed* pans, the wheel itself zooms.
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.PAN,
  };
  // Chrome/Firefox start autoscroll on a middle press unless it is consumed.
  const onMiddleDown = (e) => { if (e.button === 1) e.preventDefault(); };
  renderer.domElement.addEventListener("mousedown", onMiddleDown);

  const grid = new THREE.GridHelper(400, 40, 0x3e2f28, 0x251c18);
  scene.add(grid);

  viewer = { renderer, scene, camera, controls, raf: 0, onMiddleDown };

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

      viewer.fitDistance = camera.position.distanceTo(controls.target);
      controls.minDistance = viewer.fitDistance / 8;
      controls.maxDistance = viewer.fitDistance * 4;
      viewerZoomControls.update(100);
      controls.addEventListener("change", () => {
        viewerZoomControls.update(viewer.fitDistance / camera.position.distanceTo(controls.target) * 100);
      });
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
let viewerZoomControls;
initImageZoom($("#preview-stage"), $("#preview-img"));
initImageZoom($("#extrude-preview-img").parentElement, $("#extrude-preview-img"));
initViewerZoom();
goToStep(1);


function createZoomControls(label, min, max, onChange) {
  const bar = document.createElement("div");
  bar.className = "zoom-controls";
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", `${label} zoom controls`);
  bar.innerHTML = `
    <button type="button" aria-label="Zoom out" title="Zoom out">−</button>
    <input type="range" min="${min}" max="${max}" value="100" step="1" aria-label="${label} zoom" />
    <button type="button" aria-label="Zoom in" title="Zoom in">+</button>
    <output>100%</output>
    <button type="button" title="Reset zoom">Reset</button>`;
  const slider = bar.querySelector("input");
  const output = bar.querySelector("output");
  const buttons = bar.querySelectorAll("button");
  const update = (value) => {
    slider.value = String(Math.max(min, Math.min(max, value)));
    output.value = `${Math.round(Number(slider.value))}%`;
    buttons[0].disabled = Number(slider.value) <= min;
    buttons[1].disabled = Number(slider.value) >= max;
  };
  const change = (value) => { update(value); onChange(Number(slider.value)); };
  slider.addEventListener("input", () => change(Number(slider.value)));
  buttons[0].addEventListener("click", () => change(Number(slider.value) / 1.25));
  buttons[1].addEventListener("click", () => change(Number(slider.value) * 1.25));
  buttons[2].addEventListener("click", () => change(100));
  return { bar, update };
}

function initImageZoom(stage, img) {
  let zoom = 100;
  const content = document.createElement("div");
  content.className = "zoom-image-content";
  content.appendChild(img);
  const overlay = stage.querySelector(".overlay");
  if (overlay) content.appendChild(overlay);
  stage.appendChild(content);
  stage.classList.add("zoom-image-stage");
  const layout = () => {
    if (!img.naturalWidth || img.hidden || !stage.clientWidth) return;
    const scale = Math.min(stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
    content.style.width = `${img.naturalWidth * scale * zoom / 100}px`;
    content.style.height = `${img.naturalHeight * scale * zoom / 100}px`;
  };
  const controls = createZoomControls(img.alt, 50, 800, (value) => {
    const centerX = (stage.scrollLeft + stage.clientWidth / 2) / (content.offsetWidth || 1);
    const centerY = (stage.scrollTop + stage.clientHeight / 2) / (content.offsetHeight || 1);
    zoom = value;
    layout();
    stage.scrollLeft = centerX * content.offsetWidth - stage.clientWidth / 2;
    stage.scrollTop = centerY * content.offsetHeight - stage.clientHeight / 2;
  });
  stage.before(controls.bar);
  img.addEventListener("load", layout);
  new ResizeObserver(layout).observe(stage);
}

function initViewerZoom() {
  const controls = createZoomControls("3D view", 25, 800, (value) => {
    if (!viewer?.fitDistance) return;
    const offset = viewer.camera.position.clone().sub(viewer.controls.target);
    offset.setLength(viewer.fitDistance * 100 / value);
    viewer.camera.position.copy(viewer.controls.target).add(offset);
    viewer.controls.update();
  });
  $("#viewer").appendChild(controls.bar);
  viewerZoomControls = controls;
}
