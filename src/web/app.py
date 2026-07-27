"""FastAPI service: serves the pages and resolves a broadcast path to a manifest URL.

It deliberately does not proxy video (D10) — the player fetches segments straight
from object storage.
"""

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import ConfigError, TranscodeSettings, UploaderSettings, WebSettings
from web.resolver import InvalidSource, resolve_manifest_url
from web.streams import InvalidCamera, StreamManager, is_camera_url

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: WebSettings | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    app = FastAPI(title="bigbro-stream")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @lru_cache(maxsize=1)
    def manager() -> StreamManager:
        """Built on first use: the page must still serve without bucket credentials."""
        return StreamManager(
            settings=UploaderSettings.from_env(),
            spool_root=Path(os.environ.get("SPOOL_ROOT", "/var/spool/hls")),
            hls_time=int(os.environ.get("HLS_TIME", "4")),
            transcode=TranscodeSettings.from_env(),
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/default-stream")
    def default_stream() -> dict[str, str]:
        """What this deployment's ingest is writing, so the page can offer it."""
        return {"stream_id": settings.stream_id}

    @app.get("/api/resolve")
    def resolve(src: str = Query(default="")) -> dict[str, str]:
        if is_camera_url(src):
            # Resolving is a lookup; a camera has to be started before it has a
            # manifest to look up. Say so instead of "scheme not allowed".
            raise HTTPException(
                status_code=400,
                detail="это адрес камеры — её нужно запустить через POST /api/streams",
            )
        try:
            url = resolve_manifest_url(src, settings.public_base_url)
        except InvalidSource as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"src": src, "manifest_url": url}

    @app.post("/api/streams")
    def start_stream(src: str = Body(embed=True)) -> dict[str, str]:
        """Point this at a camera and it starts recording it. Idempotent per URL."""
        try:
            mgr = manager()
        except ConfigError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"хранилище не настроено, запуск камеры невозможен: {exc}",
            ) from exc
        try:
            stream = mgr.start(src)
        except InvalidCamera as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "stream_id": stream.stream_id,
            "source": stream.source,
            "manifest_url": resolve_manifest_url(stream.stream_id,
                                                 settings.public_base_url),
        }

    @app.get("/api/streams")
    def list_streams() -> dict[str, list[dict[str, str]]]:
        try:
            mgr = manager()
        except ConfigError:
            return {"streams": []}
        return {"streams": [
            {"stream_id": s.stream_id, "source": s.source} for s in mgr.list_streams()
        ]}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/watch")
    def watch() -> FileResponse:
        return FileResponse(STATIC_DIR / "watch.html")

    return app
