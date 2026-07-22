# 3dSignMeUp

Turn a picture into a clean, multicolor, 3D-printable sign (3MF) for a
Bambu Lab A1 mini — with or without AMS lite. See `design-docs/` for the
PRD and module design.

## Setup

```sh
uv sync        # Python 3.11 venv + backend deps
npm install    # vendors three.js into app/static/vendor (postinstall)
```

## Run

```sh
uv run uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000 — upload → clean → extrude → download
`sign.3mf` + `clean.png`. Import the 3MF into Bambu Studio, map filaments
to parts, slice, print.

Session data (uploads + cached artifacts) lives in `.sessions/`
(override with `SIGNMEUP_SESSIONS_DIR`); it is disposable.

## Tests

```sh
uv run pytest
```

`tests/test_e2e.py` runs the full upload→preview→model→download pipeline
against the real core (no mocks) and validates the 3MF (zip structure,
watertight shells, part colors).

## Layout

```
app/
├── main.py            # FastAPI app factory, static mount
├── api/               # routes (thin) + Pydantic schemas (the API contract)
├── core/              # palette → cleanup → vectorize → geometry/swapplan → export
│   └── types.py       # shared contracts: label-grid convention, dataclasses
└── static/            # vanilla JS SPA (three.js vendored, no build step)
```

Design principle: the server stores only the original upload; every
result is a pure function of (image, params), cached by params hash.

Note for mesh tooling: exported parts are unions of individually closed
prisms (no 3D booleans by design). Load 3MFs with vertex welding disabled
(e.g. `trimesh.load(..., process=False)`) or shells will be fused into
non-manifold geometry that doesn't exist in the file.
