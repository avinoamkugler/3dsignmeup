"""Artifact writers: 3MF, GLB, PNGs (FR-18..20).

- write_3mf: hand-rolled OPC zip (stdlib zipfile + xml.etree). One assembly
  object built from per-part component objects; each part object carries a base
  material so its color survives import into Bambu Studio. Deterministic bytes
  (fixed zip timestamps, sorted parts, fixed float formatting).
- write_glb: trimesh.Scene, one PBR-colored geometry per part, rotated so
  print-space Z-up becomes glTF Y-up.
- render_clean_png / render_thin_overlay: PIL RGBA PNGs with fixed settings.
"""
from __future__ import annotations

import io
import math
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import trimesh
from PIL import Image

from app.core.types import Labels, Part, hex_to_rgb

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _sorted_parts(parts: list[Part]) -> list[Part]:
    return sorted(parts, key=lambda p: p.name)


# --------------------------------------------------------------------------
# 3MF
# --------------------------------------------------------------------------
def _model_xml(parts: list[Part]) -> bytes:
    model = ET.Element("model", {"unit": "millimeter", "xmlns": _CORE_NS})
    resources = ET.SubElement(model, "resources")

    basematerials = ET.SubElement(resources, "basematerials", {"id": "1"})
    for p in parts:
        color = p.color.lstrip("#").upper()
        ET.SubElement(
            basematerials,
            "base",
            {"name": p.name, "displaycolor": f"#{color}FF"},
        )

    obj_ids: list[int] = []
    next_id = 2
    for idx, p in enumerate(parts):
        obj = ET.SubElement(
            resources,
            "object",
            {
                "id": str(next_id),
                "type": "model",
                "name": p.name,
                "pid": "1",
                "pindex": str(idx),
            },
        )
        mesh_el = ET.SubElement(obj, "mesh")
        verts_el = ET.SubElement(mesh_el, "vertices")
        for v in p.mesh.vertices:
            ET.SubElement(
                verts_el,
                "vertex",
                {"x": f"{v[0]:.6f}", "y": f"{v[1]:.6f}", "z": f"{v[2]:.6f}"},
            )
        tris_el = ET.SubElement(mesh_el, "triangles")
        for f in p.mesh.faces:
            ET.SubElement(
                tris_el,
                "triangle",
                {"v1": str(int(f[0])), "v2": str(int(f[1])), "v3": str(int(f[2]))},
            )
        obj_ids.append(next_id)
        next_id += 1

    assembly = ET.SubElement(
        resources, "object", {"id": str(next_id), "type": "model", "name": "sign"}
    )
    components = ET.SubElement(assembly, "components")
    for oid in obj_ids:
        ET.SubElement(components, "component", {"objectid": str(oid)})

    build = ET.SubElement(model, "build")
    ET.SubElement(build, "item", {"objectid": str(next_id)})

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(model, encoding="utf-8")


def _content_types_xml() -> bytes:
    types = ET.Element("Types", {"xmlns": _CT_NS})
    ET.SubElement(
        types,
        "Default",
        {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"},
    )
    ET.SubElement(
        types,
        "Default",
        {
            "Extension": "model",
            "ContentType": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        },
    )
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(types, encoding="utf-8")


def _rels_xml() -> bytes:
    rels = ET.Element("Relationships", {"xmlns": _REL_NS})
    ET.SubElement(
        rels,
        "Relationship",
        {
            "Id": "rel0",
            "Target": "/3D/3dmodel.model",
            "Type": "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
        },
    )
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rels, encoding="utf-8")


def write_3mf(parts: list[Part]) -> bytes:
    parts = _sorted_parts(parts)
    files = [
        ("[Content_Types].xml", _content_types_xml()),
        ("_rels/.rels", _rels_xml()),
        ("3D/3dmodel.model", _model_xml(parts)),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


# --------------------------------------------------------------------------
# GLB
# --------------------------------------------------------------------------
def write_glb(parts: list[Part]) -> bytes:
    scene = trimesh.Scene()
    for p in _sorted_parts(parts):
        r, g, b = hex_to_rgb(p.color)
        mesh = p.mesh.copy()
        mat = trimesh.visual.material.PBRMaterial(
            name=p.name, baseColorFactor=(r, g, b, 255)
        )
        mesh.visual = trimesh.visual.TextureVisuals(material=mat)
        scene.add_geometry(mesh, geom_name=p.name)
    # print-space Z-up -> glTF Y-up
    rot = trimesh.transformations.rotation_matrix(-math.pi / 2.0, [1.0, 0.0, 0.0])
    scene.apply_transform(rot)
    return scene.export(file_type="glb")


# --------------------------------------------------------------------------
# PNGs
# --------------------------------------------------------------------------
def _png_bytes(rgba: np.ndarray) -> bytes:
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def render_clean_png(labels: Labels, palette: list[str]) -> bytes:
    h, w = labels.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    if palette:
        pal = np.array([hex_to_rgb(c) for c in palette], dtype=np.uint8)
        mask = labels >= 0
        if mask.any():
            idx = labels[mask].astype(np.int64)
            rgba[mask, :3] = pal[idx]
            rgba[mask, 3] = 255
    return _png_bytes(rgba)


def render_thin_overlay(thin_mask: np.ndarray) -> bytes:
    h, w = thin_mask.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    m = thin_mask.astype(bool)
    rgba[m, 0] = 255
    rgba[m, 1] = 0
    rgba[m, 2] = 255
    rgba[m, 3] = 255
    return _png_bytes(rgba)


def write_download_bundle(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()
