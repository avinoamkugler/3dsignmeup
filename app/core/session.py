"""Disk-backed session store.

The server stores ONLY the original upload per session; every other artifact
is a pure function of (original image, params) and is cached under the session
directory keyed by a short params hash.

Layout on disk::

    <base_dir>/<session_id>/original.bin          # raw uploaded bytes
    <base_dir>/<session_id>/artifacts/<name>       # preview / thin / meta caches
    <base_dir>/<session_id>/models/<model_id>/...  # per-model build outputs
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

_RGBA_CACHE_CAP = 16


def params_hash(payload: dict) -> str:
    """Stable short hash of a params dict.

    Canonical JSON (sorted keys, compact separators) -> sha256 -> first 8 hex.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _decode_rgba(image_bytes: bytes) -> np.ndarray:
    """Decode arbitrary image bytes to an (H, W, 4) uint8 RGBA array.

    GIF/animated inputs yield the first frame. Raises ValueError on anything
    PIL cannot decode.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.seek(0)  # first frame for animated formats
            rgba = img.convert("RGBA")
            arr = np.asarray(rgba, dtype=np.uint8)
    except Exception as exc:  # PIL raises a variety of errors
        raise ValueError(f"undecodable image: {exc}") from exc
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError("decoded image is not RGBA")
    return arr


class Session:
    """A single upload session and its on-disk artifact tree."""

    def __init__(self, store: "SessionStore", session_id: str) -> None:
        self._store = store
        self.id = session_id
        self.dir = store.base_dir / session_id

    def rgba(self) -> np.ndarray:
        """Decoded (H, W, 4) uint8 RGBA array for the original upload (cached)."""
        return self._store._rgba(self)

    def artifact_path(self, name: str) -> Path:
        d = self.dir / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def model_dir(self, model_id: str) -> Path:
        d = self.dir / "models" / model_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _original_path(self) -> Path:
        return self.dir / "original.bin"


class SessionStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._rgba_cache: "OrderedDict[str, np.ndarray]" = OrderedDict()

    def create(self, image_bytes: bytes) -> Session:
        """Decode/validate the upload, assign an id, persist the raw bytes."""
        arr = _decode_rgba(image_bytes)  # validates; ValueError on bad input
        session_id = secrets.token_hex(3)  # e.g. "a1b2c3"
        while (self.base_dir / session_id).exists():
            session_id = secrets.token_hex(3)
        sess = Session(self, session_id)
        sess.dir.mkdir(parents=True, exist_ok=True)
        sess._original_path.write_bytes(image_bytes)
        self._sessions[session_id] = sess
        self._cache_put(session_id, arr)
        return sess

    def get(self, session_id: str) -> Session:
        """Fetch a session by id. Raises KeyError if unknown."""
        sess = self._sessions.get(session_id)
        if sess is not None:
            return sess
        candidate = Session(self, session_id)
        if candidate._original_path.exists():
            self._sessions[session_id] = candidate
            return candidate
        raise KeyError(session_id)

    # -- rgba decode cache ---------------------------------------------------
    def _rgba(self, sess: Session) -> np.ndarray:
        cached = self._rgba_cache.get(sess.id)
        if cached is not None:
            self._rgba_cache.move_to_end(sess.id)
            return cached
        arr = _decode_rgba(sess._original_path.read_bytes())
        self._cache_put(sess.id, arr)
        return arr

    def _cache_put(self, session_id: str, arr: np.ndarray) -> None:
        self._rgba_cache[session_id] = arr
        self._rgba_cache.move_to_end(session_id)
        while len(self._rgba_cache) > _RGBA_CACHE_CAP:
            self._rgba_cache.popitem(last=False)
