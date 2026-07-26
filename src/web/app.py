"""FastAPI service: serves the pages and resolves a broadcast path to a manifest URL.

It deliberately does not proxy video (D10) — the player fetches segments straight
from object storage.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import WebSettings
from web.resolver import InvalidSource, resolve_manifest_url

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: WebSettings | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    app = FastAPI(title="bigbro-stream")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/default-stream")
    def default_stream() -> dict[str, str]:
        """What this deployment's ingest is writing, so the page can offer it."""
        return {"stream_id": settings.stream_id}

    @app.get("/api/resolve")
    def resolve(src: str = Query(default="")) -> dict[str, str]:
        try:
            url = resolve_manifest_url(src, settings.public_base_url)
        except InvalidSource as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"src": src, "manifest_url": url}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/watch")
    def watch() -> FileResponse:
        return FileResponse(STATIC_DIR / "watch.html")

    return app
