"""FastAPI app factory, static mount, router include."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.session import SessionStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _PROJECT_ROOT / "app" / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="3dSignMeUp")

    base_dir = Path(
        os.environ.get("SIGNMEUP_SESSIONS_DIR", str(_PROJECT_ROOT / ".sessions"))
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    app.state.store = SessionStore(base_dir)

    app.include_router(router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", port=8000, reload=False)
