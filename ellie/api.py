"""HTTP API and dashboard host (FastAPI)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .config import Settings, get_settings
from .ingest import run_ingest
from .service import NoData, Platform, UnknownSymbol

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    platform = Platform(conn)
    app = FastAPI(title="Ellie — S&P 500 Equity Intelligence", version=__version__)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.platform = platform

    def guarded(fn, *args):
        try:
            return fn(*args)
        except UnknownSymbol as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except NoData as exc:
            raise HTTPException(503, detail=str(exc)) from exc

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status():
        return guarded(platform.status)

    @app.get("/api/overview")
    def overview():
        return guarded(platform.overview)

    @app.get("/api/screener")
    def screener():
        return guarded(platform.screener)

    @app.get("/api/stocks/{symbol}")
    def stock(symbol: str):
        return guarded(platform.stock, symbol)

    @app.get("/api/stocks/{symbol}/forecast")
    def stock_forecast(symbol: str, horizon: int = Query(21, ge=5, le=63)):
        return guarded(platform.forecast, symbol, horizon)

    @app.get("/api/sectors")
    def sectors():
        return guarded(platform.sectors)

    @app.get("/api/risk")
    def risk():
        return guarded(platform.risk)

    @app.get("/api/macro")
    def macro():
        return guarded(platform.macro)

    @app.get("/api/alerts")
    def alerts():
        return guarded(platform.alerts)

    @app.get("/api/quality")
    def quality():
        return platform.quality()

    @app.post("/api/ingest", status_code=202)
    def trigger_ingest(mode: str | None = None, x_admin_token: str | None = Header(default=None)):
        token = os.environ.get("ELLIE_ADMIN_TOKEN")
        if token and x_admin_token != token:
            raise HTTPException(401, "invalid admin token")
        if mode not in (None, "auto", "live", "synthetic"):
            raise HTTPException(400, "mode must be auto, live or synthetic")
        if platform.ingest_running:
            raise HTTPException(409, "an ingestion run is already in progress")

        def work():
            platform.ingest_running = True
            try:
                # A separate connection: SQLite connections are not shared across writers.
                run_ingest(db.connect(settings.db_path), settings, mode)
            except Exception:
                log.exception("ingestion failed")
            finally:
                platform.ingest_running = False

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app
