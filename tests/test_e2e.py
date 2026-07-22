"""End-to-end smoke test: upload → palette → preview → model → downloads.

Uses only the HTTP contract (design-docs/3dsignmeup-design.md). Runs the real
pipeline — no mocks. A synthetic "sign": red background rectangle cleared as
background, white ring (letter O-like, exercises enclosed background) and a
blue square, on transparent canvas.
"""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pytest
from PIL import Image

RED = (230, 57, 70, 255)
WHITE = (241, 250, 238, 255)
BLUE = (69, 123, 157, 255)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    os.environ["SIGNMEUP_SESSIONS_DIR"] = str(tmp_path_factory.mktemp("sessions"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture(scope="module")
def sign_png() -> bytes:
    """400x300: red bg rect, white ring with red counter, blue square."""
    img = np.zeros((300, 400, 4), dtype=np.uint8)
    img[:, :] = RED  # background fills to the corners
    # white ring (outer 60..140 sq, inner 85..115 red -> enclosed background)
    img[60:200, 60:200] = WHITE
    img[100:160, 100:160] = RED
    # blue square
    img[80:220, 240:360] = BLUE

    buf = io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def session(client, sign_png):
    r = client.post("/api/sessions", files={"image": ("sign.png", sign_png, "image/png")})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["width_px"] == 400 and data["height_px"] == 300
    assert len(data["suggested_palette"]) == 3
    return data


PROCESS = {
    "target_width_mm": 120.0,
    "palette": ["#e63946", "#f1faee", "#457b9d"],
    "smoothness": 2,
    "min_feature_mm": 0.5,
    "clear_background": True,
    "enclosed_background": "plate",
}


def test_palette_redetect(client, session):
    r = client.post(f"/api/sessions/{session['session_id']}/palette", json={"n_colors": 3})
    assert r.status_code == 200, r.text
    palette = r.json()["palette"]
    assert len(palette) == 3
    # detected colors should be (near-)exact since the image is flat-colored
    assert set(palette) == {"#e63946", "#f1faee", "#457b9d"}


def test_preview(client, session):
    sid = session["session_id"]
    r = client.post(f"/api/sessions/{sid}/preview", json=PROCESS)
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["params_hash"]

    png = client.get(prev["preview_png"])
    assert png.status_code == 200
    img = np.array(Image.open(io.BytesIO(png.content)).convert("RGBA"))

    # FR-2/FR-8: only palette colors + transparency, background cleared
    opaque = img[img[..., 3] > 0]
    shown = {f"#{r_:02x}{g:02x}{b:02x}" for r_, g, b in map(tuple, opaque[:, :3])}
    assert shown <= set(PROCESS["palette"])
    assert img[0, 0, 3] == 0  # corner background cleared
    assert img[130, 130, 3] == 0  # enclosed counter -> hole (plate mode renders transparent)

    # coverage sums to ~1 over remaining art
    assert abs(sum(prev["color_coverage"].values()) - 1.0) < 0.05


def _model_request(mode: str) -> dict:
    return {
        "process": PROCESS,
        "extrude": {
            "mode": mode,
            "layer_height_mm": 0.2,
            "first_layer_height_mm": 0.2,
            "plate": {"enabled": True, "thickness_mm": 1.2},
            "main_body_color": "#e63946",
            "colors": [
                {"hex": "#e63946", "height_mm": 2.0, "first_layer_only": False},
                {"hex": "#f1faee", "height_mm": 3.0, "first_layer_only": False},
                {"hex": "#457b9d", "height_mm": 4.0, "first_layer_only": False},
            ],
            "initial_color_switch_mm": 2.0,
            "chamfer": {"enabled": True, "size_mm": 0.6},
        },
    }


@pytest.mark.parametrize("mode", ["swap", "ams"])
def test_model_and_downloads(client, session, mode):
    sid = session["session_id"]
    r = client.post(f"/api/sessions/{sid}/model", json=_model_request(mode))
    assert r.status_code == 200, r.text
    model = r.json()

    assert model["parts"], "no parts built"
    part_colors = {p["color"] for p in model["parts"]}
    assert part_colors <= {"#e63946", "#f1faee", "#457b9d"}
    assert all(p["triangles"] > 0 for p in model["parts"])

    if mode == "swap":
        assert model["n_swaps"] == len(model["swap_plan"]) > 0
        for ev in model["swap_plan"]:
            assert ev["from"] != ev["to"]
            assert ev["layer"] >= 1
        # ascending Z
        zs = [ev["z_mm"] for ev in model["swap_plan"]]
        assert zs == sorted(zs)
    else:
        assert model["swap_plan"] == [] and model["n_swaps"] == 0

    # snapped heights present for every non-body visible color at least
    assert model["snapped_heights"]
    for info in model["snapped_heights"].values():
        assert info["layers"] >= 1

    # downloads
    threemf = client.get(model["downloads"]["threemf"])
    assert threemf.status_code == 200
    with zipfile.ZipFile(io.BytesIO(threemf.content)) as z:
        names = z.namelist()
        assert "[Content_Types].xml" in names
        assert any(n.endswith(".model") for n in names)
        assert z.testzip() is None

    # 3MF loads as valid geometry
    import trimesh

    scene = trimesh.load(io.BytesIO(threemf.content), file_type="3mf")
    tm_meshes = (
        list(scene.geometry.values()) if isinstance(scene, trimesh.Scene) else [scene]
    )
    assert tm_meshes and all(m.faces.shape[0] > 0 for m in tm_meshes)

    glb = client.get(model["preview_glb"])
    assert glb.status_code == 200
    gscene = trimesh.load(io.BytesIO(glb.content), file_type="glb")
    assert len(gscene.geometry) >= 1

    png = client.get(model["downloads"]["png"])
    assert png.status_code == 200
    # FR-8: downloadable clean PNG byte-identical to the approved preview
    prev = client.post(f"/api/sessions/{sid}/preview", json=PROCESS).json()
    prev_bytes = client.get(prev["preview_png"]).content
    assert png.content == prev_bytes


def test_meshes_watertight(client, session):
    """Every part in the built 3MF must be manifold (NFR: slice w/o repair)."""
    import trimesh

    sid = session["session_id"]
    model = client.post(f"/api/sessions/{sid}/model", json=_model_request("swap")).json()
    raw = client.get(model["downloads"]["threemf"]).content
    # process=False: parts are unions of individually-closed prisms (no 3D
    # booleans by design); trimesh's default vertex welding would fuse their
    # coincident interfaces into non-manifold edges that don't exist on disk.
    scene = trimesh.load(io.BytesIO(raw), file_type="3mf", process=False)
    meshes = list(scene.geometry.values()) if isinstance(scene, trimesh.Scene) else [scene]
    for name, m in zip(scene.geometry.keys(), meshes):
        assert m.is_watertight, f"part {name} is not watertight"
        assert m.volume > 0, f"part {name} has non-positive volume"
