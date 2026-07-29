"""FastAPI app factory, static mount, router include."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.api.routes import router
from app.core.session import SessionStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _PROJECT_ROOT / "app" / "static"


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that forces revalidation.

    Starlette sends only etag/last-modified. With no Cache-Control a browser
    falls back to *heuristic* freshness and will serve a cached styles.css or
    app.js without asking the server — so an edit silently doesn't show up, and
    a half-updated page (new markup, old CSS) looks like a rendering bug.
    "no-cache" still caches, it just revalidates first, so hits stay cheap 304s.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="3dSignMeUp")

    base_dir = Path(
        os.environ.get("SIGNMEUP_SESSIONS_DIR", str(_PROJECT_ROOT / ".sessions"))
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    app.state.store = SessionStore(base_dir)

    app.include_router(router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", _RevalidatingStatic(directory=str(_STATIC_DIR)), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", port=8000, reload=False)
