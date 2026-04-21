"""FastAPI entry point for the ScreenView CMS backend.

Launch with:

    uvicorn server.main:app --reload

or simply:

    python -m server
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .routers import auth, devices, media, schedules, websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    summary="Lightweight digital signage CMS (Xibo alternative).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(media.router)
app.include_router(schedules.router)
app.include_router(websocket.router)

app.mount("/uploads", StaticFiles(directory=str(settings.upload_dir)), name="uploads")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "cms-frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="cms")


@app.get("/api/health", tags=["meta"])
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}
