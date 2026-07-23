"""API endpoints. Thin layer: validate -> call core -> build response."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.api.schemas import (
    Downloads,
    ModelRequest,
    ModelResponse,
    PaletteRequest,
    PaletteResponse,
    PartInfo,
    PreviewResponse,
    ProcessParams,
    SessionCreated,
    SnappedHeight,
    SwapEventOut,
    WarningOut,
)
from app.core import cleanup, export, geometry, palette, session, vectorize
from app.core.session import Session, SessionStore

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_MEDIA_TYPES = {
    ".png": "image/png",
    ".glb": "model/gltf-binary",
    ".3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+3mf",
    ".zip": "application/zip",
}


def _store(request: Request) -> SessionStore:
    return request.app.state.store


def _get_session(request: Request, session_id: str) -> Session:
    try:
        return _store(request).get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session")


def _serve(base_dir: Path, session_dir: Path, name: str) -> FileResponse:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="not found")
    target = (base_dir / name).resolve()
    session_root = session_dir.resolve()
    if session_root not in target.parents:
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type, filename=name)


# --------------------------------------------------------------------------
# 1. Upload
# --------------------------------------------------------------------------
@router.post("/api/sessions", response_model=SessionCreated, status_code=201)
async def create_session(request: Request, image: UploadFile = File(...)) -> SessionCreated:
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        sess = _store(request).create(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="not a decodable image")
    rgba = sess.rgba()
    height_px, width_px = int(rgba.shape[0]), int(rgba.shape[1])
    suggested = palette.detect_palette(rgba, 3)
    return SessionCreated(
        session_id=sess.id,
        width_px=width_px,
        height_px=height_px,
        suggested_palette=suggested,
    )


# --------------------------------------------------------------------------
# 2. Palette re-detection
# --------------------------------------------------------------------------
@router.post("/api/sessions/{session_id}/palette", response_model=PaletteResponse)
def detect_palette(request: Request, session_id: str, body: PaletteRequest) -> PaletteResponse:
    sess = _get_session(request, session_id)
    colors = palette.detect_palette(sess.rgba(), body.n_colors)
    return PaletteResponse(palette=colors)


# --------------------------------------------------------------------------
# 3. Preview
# --------------------------------------------------------------------------
@router.post("/api/sessions/{session_id}/preview", response_model=PreviewResponse)
def preview(request: Request, session_id: str, params: ProcessParams) -> PreviewResponse:
    sess = _get_session(request, session_id)
    rgba = sess.rgba()
    width_px = int(rgba.shape[1])
    mm_per_px = params.target_width_mm / width_px

    h = session.params_hash(params.model_dump())
    meta_path = sess.artifact_path(f"meta_{h}.json")
    if meta_path.exists():
        return PreviewResponse.model_validate_json(meta_path.read_text())

    labels = palette.quantize(rgba, params.quantize_palette or params.palette)
    result = cleanup.run_cleanup(
        labels,
        params.palette,
        mm_per_px=mm_per_px,
        smoothness=params.smoothness,
        min_feature_mm=params.min_feature_mm,
        clear_background=params.clear_background,
        enclosed_background=params.enclosed_background,
        paint_fills=[(f.x, f.y, f.to_hex) for f in params.paint_fills],
    )

    png = export.render_clean_png(result.labels, params.palette)
    sess.artifact_path(f"prev_{h}.png").write_bytes(png)

    thin_url: str | None = None
    if result.thin_mask is not None:
        thin_png = export.render_thin_overlay(result.thin_mask)
        sess.artifact_path(f"thin_{h}.png").write_bytes(thin_png)
        thin_url = f"/api/sessions/{sess.id}/artifacts/thin_{h}.png"

    resp = PreviewResponse(
        preview_png=f"/api/sessions/{sess.id}/artifacts/prev_{h}.png",
        thin_overlay_png=thin_url,
        islands=result.islands,
        color_coverage=result.coverage,
        color_perimeter=cleanup.color_perimeters(result.labels, params.palette),
        params_hash=h,
    )
    meta_path.write_text(resp.model_dump_json())
    return resp


# --------------------------------------------------------------------------
# 4. Model build
# --------------------------------------------------------------------------
@router.post("/api/sessions/{session_id}/model", response_model=ModelResponse)
def build_model(request: Request, session_id: str, body: ModelRequest) -> ModelResponse:
    sess = _get_session(request, session_id)
    process = body.process
    extrude = body.extrude

    color_hexes = {c.hex for c in extrude.colors}
    missing = [hexv for hexv in process.palette if hexv not in color_hexes]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"extrude.colors is missing palette hex(es): {missing}",
        )

    model_id = "m_" + session.params_hash(
        {"process": process.model_dump(), "extrude": extrude.model_dump()}
    )
    mdir = sess.model_dir(model_id)
    meta_path = mdir / "meta.json"
    if (
        meta_path.exists()
        and (mdir / "sign.3mf").exists()
        and (mdir / "preview.glb").exists()
        and (mdir / "clean.png").exists()
        and (mdir / "bundle.zip").exists()
    ):
        return ModelResponse.model_validate_json(meta_path.read_text())

    rgba = sess.rgba()
    width_px = int(rgba.shape[1])
    mm_per_px = process.target_width_mm / width_px

    labels = palette.quantize(rgba, process.quantize_palette or process.palette)
    result = cleanup.run_cleanup(
        labels,
        process.palette,
        mm_per_px=mm_per_px,
        smoothness=process.smoothness,
        min_feature_mm=process.min_feature_mm,
        clear_background=process.clear_background,
        enclosed_background=process.enclosed_background,
        paint_fills=[(f.x, f.y, f.to_hex) for f in process.paint_fills],
    )
    art = vectorize.vectorize(
        result, process.palette, mm_per_px=mm_per_px, smoothness=process.smoothness
    )
    built = geometry.build_model(art, extrude)

    sign_parts = [p for p in built.parts if not p.name.startswith("fin_")]
    fin_parts = [p for p in built.parts if p.name.startswith("fin_")]

    sign_3mf = export.write_3mf(sign_parts)
    clean_png = export.render_clean_png(result.labels, process.palette)
    (mdir / "sign.3mf").write_bytes(sign_3mf)
    (mdir / "preview.glb").write_bytes(export.write_glb(sign_parts))
    (mdir / "clean.png").write_bytes(clean_png)

    bundle_files = [("sign.3mf", sign_3mf), ("clean.png", clean_png)]
    fins_url = None
    if fin_parts:
        fins_3mf = export.write_3mf(fin_parts)
        (mdir / "fins.3mf").write_bytes(fins_3mf)
        bundle_files.append(("fins.3mf", fins_3mf))
        fins_url = f"/api/sessions/{sess.id}/models/{model_id}/fins.3mf"

    (mdir / "bundle.zip").write_bytes(export.write_download_bundle(bundle_files))

    parts = [
        PartInfo(name=p.name, color=p.color, triangles=len(p.mesh.faces))
        for p in built.parts
    ]
    swap_plan = [
        SwapEventOut(z_mm=e.z_mm, layer=e.layer, from_hex=e.from_hex, to_hex=e.to_hex)
        for e in built.swap_plan
    ]
    snapped_heights = {
        hexv: SnappedHeight(**vals) for hexv, vals in built.snapped_heights.items()
    }
    warnings = [WarningOut(**w) for w in built.warnings]

    resp = ModelResponse(
        model_id=model_id,
        parts=parts,
        swap_plan=swap_plan,
        n_swaps=built.n_swaps,
        snapped_heights=snapped_heights,
        warnings=warnings,
        preview_glb=f"/api/sessions/{sess.id}/models/{model_id}/preview.glb",
        downloads=Downloads(
            threemf=f"/api/sessions/{sess.id}/models/{model_id}/sign.3mf",
            png=f"/api/sessions/{sess.id}/models/{model_id}/clean.png",
            zip=f"/api/sessions/{sess.id}/models/{model_id}/bundle.zip",
            fins_threemf=fins_url,
        ),
    )
    meta_path.write_text(resp.model_dump_json(by_alias=True))
    return resp


# --------------------------------------------------------------------------
# 5. File serving
# --------------------------------------------------------------------------
@router.get("/api/sessions/{session_id}/artifacts/{name}")
def get_artifact(request: Request, session_id: str, name: str) -> FileResponse:
    sess = _get_session(request, session_id)
    return _serve(sess.dir / "artifacts", sess.dir, name)


@router.get("/api/sessions/{session_id}/models/{model_id}/{filename}")
def get_model_file(
    request: Request, session_id: str, model_id: str, filename: str
) -> FileResponse:
    sess = _get_session(request, session_id)
    if not _NAME_RE.match(model_id):
        raise HTTPException(status_code=404, detail="not found")
    return _serve(sess.dir / "models" / model_id, sess.dir, filename)


# --------------------------------------------------------------------------
# 6. Root
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        return PlainTextResponse("frontend not built", status_code=503)
    return HTMLResponse(index_file.read_text())
