"""API endpoint tests.

Core pipeline functions are monkeypatched with tiny fakes so these tests pass
even while the real core modules still raise NotImplementedError. Fakes are
installed as attributes on the core *modules* exactly as routes.py references
them (module.attr style), which is what makes monkeypatch.setattr work.
"""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from app.core import cleanup, export, geometry, palette, vectorize
from app.core.types import BuiltModel, CleanupResult, Part, SwapEvent

_PALETTE = ["#111111", "#222222", "#333333"]

_PROCESS = {
    "target_width_mm": 120.0,
    "palette": _PALETTE,
    "smoothness": 4,
    "min_feature_mm": 0.5,
    "clear_background": True,
    "enclosed_background": "plate",
}

_EXTRUDE = {
    "mode": "swap",
    "layer_height_mm": 0.2,
    "plate": {"enabled": True, "thickness_mm": 1.2},
    "main_body_color": "#111111",
    "colors": [
        {"hex": "#111111", "height_mm": 2.0, "first_layer_only": False},
        {"hex": "#222222", "height_mm": 3.0, "first_layer_only": False},
        {"hex": "#333333", "height_mm": 3.0, "first_layer_only": True},
    ],
    "initial_color_switch_mm": 2.0,
    "chamfer": {"enabled": True, "size_mm": 0.6},
}


def _png_bytes(w: int = 8, h: int = 6) -> bytes:
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_cleanup_result(thin: bool = False) -> CleanupResult:
    labels = np.zeros((4, 4), dtype=np.int16)
    return CleanupResult(
        labels=labels,
        body_fill_mask=np.zeros((4, 4), dtype=bool),
        thin_mask=(np.ones((4, 4), dtype=bool) if thin else None),
        islands=3,
        coverage={"#111111": 0.5, "#222222": 0.5},
    )


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SIGNMEUP_SESSIONS_DIR", str(tmp_path / "sessions"))
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def calls() -> dict:
    return {k: 0 for k in ("detect", "quantize", "cleanup", "vectorize", "build")}


@pytest.fixture(autouse=True)
def patch_core(monkeypatch, calls):
    def fake_detect(rgba, n_colors):
        calls["detect"] += 1
        base = [f"#{i:02x}{i:02x}{i:02x}" for i in range(1, 11)]
        return base[:n_colors]

    def fake_quantize(rgba, pal):
        calls["quantize"] += 1
        return np.zeros((4, 4), dtype=np.int16)

    def fake_cleanup(labels, pal, **kw):
        calls["cleanup"] += 1
        return _make_cleanup_result(thin=False)

    def fake_vectorize(result, pal, **kw):
        calls["vectorize"] += 1
        return object()

    def fake_build(art, extrude):
        calls["build"] += 1
        return BuiltModel(
            parts=[Part(name="body_#111111_z0.0-1.0", color="#111111", mesh=trimesh.creation.box())],
            swap_plan=[SwapEvent(z_mm=1.0, layer=5, from_hex="#111111", to_hex="#222222")],
            n_swaps=1,
            snapped_heights={"#222222": {"requested": 3.0, "snapped": 3.0, "layers": 15}},
            warnings=[{"code": "ams_slots", "msg": "too many colors"}],
        )

    monkeypatch.setattr(palette, "detect_palette", fake_detect)
    monkeypatch.setattr(palette, "quantize", fake_quantize)
    monkeypatch.setattr(cleanup, "run_cleanup", fake_cleanup)
    monkeypatch.setattr(vectorize, "vectorize", fake_vectorize)
    monkeypatch.setattr(geometry, "build_model", fake_build)
    monkeypatch.setattr(export, "render_clean_png", lambda labels, pal: b"\x89PNG_clean")
    monkeypatch.setattr(export, "render_thin_overlay", lambda mask: b"\x89PNG_thin")
    monkeypatch.setattr(export, "write_3mf", lambda parts: b"3MF_DATA")
    monkeypatch.setattr(export, "write_glb", lambda parts: b"GLB_DATA")


def _new_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", files={"image": ("in.png", _png_bytes(), "image/png")})
    assert resp.status_code == 201
    return resp.json()["session_id"]


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def test_upload_happy(client):
    resp = client.post("/api/sessions", files={"image": ("in.png", _png_bytes(8, 6), "image/png")})
    assert resp.status_code == 201
    body = resp.json()
    assert body["width_px"] == 8
    assert body["height_px"] == 6
    assert body["suggested_palette"] == ["#010101", "#020202", "#030303"]
    assert len(body["session_id"]) == 6  # secrets.token_hex(3)


def test_upload_bad_file(client):
    resp = client.post("/api/sessions", files={"image": ("bad.txt", b"not an image", "text/plain")})
    assert resp.status_code == 400


def test_upload_empty(client):
    resp = client.post("/api/sessions", files={"image": ("empty.png", b"", "image/png")})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Unknown session -> 404
# --------------------------------------------------------------------------
def test_unknown_session_404(client):
    assert client.post("/api/sessions/nope/palette", json={"n_colors": 3}).status_code == 404
    assert client.post("/api/sessions/nope/preview", json=_PROCESS).status_code == 404
    assert client.post(
        "/api/sessions/nope/model", json={"process": _PROCESS, "extrude": _EXTRUDE}
    ).status_code == 404
    assert client.get("/api/sessions/nope/artifacts/prev_x.png").status_code == 404


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
def test_palette(client):
    sid = _new_session(client)
    resp = client.post(f"/api/sessions/{sid}/palette", json={"n_colors": 4})
    assert resp.status_code == 200
    assert resp.json()["palette"] == ["#010101", "#020202", "#030303", "#040404"]

    assert client.post(f"/api/sessions/{sid}/palette", json={"n_colors": 1}).status_code == 422


# --------------------------------------------------------------------------
# Preview (+ caching, URL shapes, null thin overlay)
# --------------------------------------------------------------------------
def test_preview_and_cache(client, calls):
    sid = _new_session(client)
    resp = client.post(f"/api/sessions/{sid}/preview", json=_PROCESS)
    assert resp.status_code == 200
    body = resp.json()
    h = body["params_hash"]
    assert body["preview_png"] == f"/api/sessions/{sid}/artifacts/prev_{h}.png"
    assert body["thin_overlay_png"] is None  # fake cleanup has no thin mask
    assert body["islands"] == 3
    assert body["color_coverage"] == {"#111111": 0.5, "#222222": 0.5}
    assert calls["cleanup"] == 1

    # Second identical call is served from the cached meta json.
    resp2 = client.post(f"/api/sessions/{sid}/preview", json=_PROCESS)
    assert resp2.status_code == 200
    assert resp2.json() == body
    assert calls["cleanup"] == 1  # not recomputed

    # The preview PNG is downloadable with the right content type.
    png = client.get(body["preview_png"])
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content == b"\x89PNG_clean"


def test_preview_thin_overlay(client, monkeypatch):
    monkeypatch.setattr(cleanup, "run_cleanup", lambda labels, pal, **kw: _make_cleanup_result(thin=True))
    sid = _new_session(client)
    resp = client.post(f"/api/sessions/{sid}/preview", json=_PROCESS)
    body = resp.json()
    h = body["params_hash"]
    assert body["thin_overlay_png"] == f"/api/sessions/{sid}/artifacts/thin_{h}.png"
    thin = client.get(body["thin_overlay_png"])
    assert thin.status_code == 200
    assert thin.headers["content-type"] == "image/png"
    assert thin.content == b"\x89PNG_thin"


# --------------------------------------------------------------------------
# Model (+ from/to alias, downloads, caching, validation)
# --------------------------------------------------------------------------
def test_model_happy(client, calls):
    sid = _new_session(client)
    resp = client.post(f"/api/sessions/{sid}/model", json={"process": _PROCESS, "extrude": _EXTRUDE})
    assert resp.status_code == 200
    body = resp.json()
    mid = body["model_id"]
    assert mid.startswith("m_")

    # Parts triangle count derived from mesh faces (box == 12).
    assert body["parts"][0]["triangles"] == 12
    assert body["parts"][0]["color"] == "#111111"

    # Swap events serialize with the "from"/"to" aliases.
    ev = body["swap_plan"][0]
    assert ev["from"] == "#111111"
    assert ev["to"] == "#222222"
    assert "from_hex" not in ev
    assert body["n_swaps"] == 1

    assert body["snapped_heights"]["#222222"] == {"requested": 3.0, "snapped": 3.0, "layers": 15}
    assert body["warnings"][0]["code"] == "ams_slots"

    # URL shapes.
    assert body["preview_glb"] == f"/api/sessions/{sid}/models/{mid}/preview.glb"
    assert body["downloads"]["threemf"] == f"/api/sessions/{sid}/models/{mid}/sign.3mf"
    assert body["downloads"]["png"] == f"/api/sessions/{sid}/models/{mid}/clean.png"
    assert body["downloads"]["zip"] == f"/api/sessions/{sid}/models/{mid}/bundle.zip"
    assert body["downloads"]["fins_threemf"] is None

    # Second identical call is cached (no recompute).
    assert calls["build"] == 1
    resp2 = client.post(f"/api/sessions/{sid}/model", json={"process": _PROCESS, "extrude": _EXTRUDE})
    assert resp2.json() == body
    assert calls["build"] == 1


def test_model_downloads_served(client):
    sid = _new_session(client)
    body = client.post(f"/api/sessions/{sid}/model", json={"process": _PROCESS, "extrude": _EXTRUDE}).json()

    glb = client.get(body["preview_glb"])
    assert glb.status_code == 200
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert glb.content == b"GLB_DATA"

    tmf = client.get(body["downloads"]["threemf"])
    assert tmf.status_code == 200
    assert tmf.headers["content-type"] == "application/vnd.ms-package.3dmanufacturing-3dmodel+3mf"
    assert tmf.content == b"3MF_DATA"

    png = client.get(body["downloads"]["png"])
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"

    zipped = client.get(body["downloads"]["zip"])
    assert zipped.status_code == 200
    assert zipped.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
        assert set(zf.namelist()) == {"sign.3mf", "clean.png"}
        assert zf.read("sign.3mf") == b"3MF_DATA"
        assert zf.read("clean.png") == b"\x89PNG_clean"


def test_model_missing_palette_color_400(client):
    extrude = {**_EXTRUDE, "colors": [{"hex": "#111111", "height_mm": 2.0}]}  # drops #222222/#333333
    sid = _new_session(client)
    resp = client.post(f"/api/sessions/{sid}/model", json={"process": _PROCESS, "extrude": extrude})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# File serving safety
# --------------------------------------------------------------------------
def test_path_traversal_rejected(client):
    sid = _new_session(client)
    # dotdot passes the charset regex but the resolve-check keeps it inside the dir.
    assert client.get(f"/api/sessions/{sid}/artifacts/..").status_code == 404
    # encoded slash traversal never resolves inside the session dir.
    assert client.get(f"/api/sessions/{sid}/artifacts/..%2Foriginal.bin").status_code == 404
    # unknown artifact.
    assert client.get(f"/api/sessions/{sid}/artifacts/prev_dead.png").status_code == 404


def test_root_route(client):
    resp = client.get("/")
    # Frontend is written by another agent; guarded route must not crash.
    assert resp.status_code in (200, 503)
