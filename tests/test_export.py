"""Tests for artifact writers: 3MF, GLB, PNGs."""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
from PIL import Image
from shapely.geometry import box

from app.core.export import (
    render_clean_png,
    render_thin_overlay,
    write_3mf,
    write_download_bundle,
    write_glb,
)
from app.core.types import Part, hex_to_rgb

R = "#ff0000"
G = "#00ff00"

_CORE_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


def _parts():
    a = trimesh.creation.extrude_polygon(box(0, 0, 10, 5), 2.0)
    b = trimesh.creation.extrude_polygon(box(0, 0, 6, 4), 1.0)
    b.apply_translation([0, 0, 2.0])
    return [
        Part(name="band_#ff0000_z0.00-2.00", color=R, mesh=a),
        Part(name="band_#00ff00_z2.00-3.00", color=G, mesh=b),
    ]


# --------------------------------------------------------------------------
# 3MF
# --------------------------------------------------------------------------
def test_3mf_is_valid_zip_with_required_files():
    data = write_3mf(_parts())
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "3D/3dmodel.model" in names


def test_3mf_model_parses_and_counts_match():
    parts = _parts()
    data = write_3mf(parts)
    zf = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(zf.read("3D/3dmodel.model"))
    assert root.attrib["unit"] == "millimeter"

    objects = root.findall(f".//{_CORE_NS}object")
    # one object per part + one assembly object
    assert len(objects) == len(parts) + 1

    # vertex/triangle counts on the mesh objects match the parts (sorted by name)
    by_name = {p.name: p for p in parts}
    mesh_objs = [o for o in objects if o.find(f"{_CORE_NS}mesh") is not None]
    for o in mesh_objs:
        part = by_name[o.attrib["name"]]
        verts = o.findall(f"{_CORE_NS}mesh/{_CORE_NS}vertices/{_CORE_NS}vertex")
        tris = o.findall(f"{_CORE_NS}mesh/{_CORE_NS}triangles/{_CORE_NS}triangle")
        assert len(verts) == len(part.mesh.vertices)
        assert len(tris) == len(part.mesh.faces)

    # base materials: one per part, colored
    bases = root.findall(f".//{_CORE_NS}basematerials/{_CORE_NS}base")
    assert len(bases) == len(parts)
    colors = {b.attrib["displaycolor"].upper() for b in bases}
    assert "#FF0000FF" in colors and "#00FF00FF" in colors


def test_3mf_roundtrips_through_trimesh_preserving_parts():
    parts = _parts()
    data = write_3mf(parts)
    try:
        loaded = trimesh.load(io.BytesIO(data), file_type="3mf")
    except ModuleNotFoundError:
        # trimesh's 3MF reader needs lxml; if unavailable verify via our own
        # parse that every part's triangles survived the write.
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
        tris = root.findall(f".//{_CORE_NS}mesh/{_CORE_NS}triangles/{_CORE_NS}triangle")
        assert len(tris) == sum(len(p.mesh.faces) for p in parts)
        return
    if isinstance(loaded, trimesh.Scene):
        assert len(loaded.geometry) == len(parts)
    else:
        # single mesh fallback should still carry all triangles
        assert len(loaded.faces) == sum(len(p.mesh.faces) for p in parts)


def test_3mf_deterministic():
    parts = _parts()
    assert write_3mf(parts) == write_3mf(parts)
    # order of input parts must not change bytes (sorted internally)
    assert write_3mf(parts) == write_3mf(list(reversed(parts)))


# --------------------------------------------------------------------------
# GLB
# --------------------------------------------------------------------------
def test_glb_roundtrip_and_colors():
    parts = _parts()
    data = write_glb(parts)
    scene = trimesh.load(io.BytesIO(data), file_type="glb")
    assert isinstance(scene, trimesh.Scene)
    assert len(scene.geometry) == len(parts)

    # verify each part color survives as a base color factor
    wanted = {p.name: hex_to_rgb(p.color) for p in parts}
    found = {}
    for name, geom in scene.geometry.items():
        mat = getattr(geom.visual, "material", None)
        assert mat is not None
        bcf = np.asarray(mat.baseColorFactor)[:3]
        found[name] = tuple(int(round(x)) for x in bcf)
    for name, rgb in wanted.items():
        assert name in found
        assert found[name] == rgb


def test_glb_deterministic():
    parts = _parts()
    assert write_glb(parts) == write_glb(parts)


# --------------------------------------------------------------------------
# clean PNG
# --------------------------------------------------------------------------
def test_clean_png_pixels():
    palette = [R, G, "#0000ff"]
    labels = np.array(
        [
            [0, 1, -1],
            [2, -2, 0],
        ],
        dtype=np.int16,
    )
    data = render_clean_png(labels, palette)
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    px = np.array(img)
    assert tuple(px[0, 0]) == (255, 0, 0, 255)  # palette 0
    assert tuple(px[0, 1]) == (0, 255, 0, 255)  # palette 1
    assert tuple(px[0, 2]) == (0, 0, 0, 0)  # TRANSPARENT
    assert tuple(px[1, 0]) == (0, 0, 255, 255)  # palette 2
    assert tuple(px[1, 1]) == (0, 0, 0, 0)  # HOLE_PLATE
    assert tuple(px[1, 2]) == (255, 0, 0, 255)  # palette 0


def test_clean_png_deterministic():
    palette = [R, G]
    labels = np.array([[0, 1], [1, -1]], dtype=np.int16)
    assert render_clean_png(labels, palette) == render_clean_png(labels, palette)


# --------------------------------------------------------------------------
# thin overlay
# --------------------------------------------------------------------------
def test_thin_overlay_pixels():
    mask = np.array([[True, False], [False, True]], dtype=bool)
    data = render_thin_overlay(mask)
    px = np.array(Image.open(io.BytesIO(data)).convert("RGBA"))
    assert tuple(px[0, 0]) == (255, 0, 255, 255)
    assert tuple(px[0, 1]) == (0, 0, 0, 0)
    assert tuple(px[1, 0]) == (0, 0, 0, 0)
    assert tuple(px[1, 1]) == (255, 0, 255, 255)


def test_thin_overlay_deterministic():
    mask = np.array([[True, False], [True, True]], dtype=bool)
    assert render_thin_overlay(mask) == render_thin_overlay(mask)


# --------------------------------------------------------------------------
# download bundle zip
# --------------------------------------------------------------------------
def test_download_bundle_contains_members():
    data = write_download_bundle([("sign.3mf", b"AAA"), ("clean.png", b"BBB")])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["sign.3mf", "clean.png"]
        assert zf.read("sign.3mf") == b"AAA"
        assert zf.read("clean.png") == b"BBB"


def test_download_bundle_deterministic():
    files = [("sign.3mf", b"AAA"), ("fins.3mf", b"CCC"), ("clean.png", b"BBB")]
    assert write_download_bundle(files) == write_download_bundle(files)
