from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import load_settings
from refresh import RefreshCoordinator
from store import Store


settings = load_settings()
store = Store(settings.database_path)
coordinator = RefreshCoordinator(settings, store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    refresh_task = asyncio.create_task(coordinator.run_periodic())
    try:
        yield
    finally:
        coordinator.stop()
        refresh_task.cancel()
        await asyncio.gather(refresh_task, return_exceptions=True)


app = FastAPI(
    title="Much ADO About Jira",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return JSONResponse(status_code=403, content={"detail": "Localhost access only"})
    origin = request.headers.get("origin")
    if request.method == "POST" and origin:
        allowed_origins = {
            f"http://127.0.0.1:{settings.app.port}",
            f"http://localhost:{settings.app.port}",
        }
        if origin.rstrip("/") not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Local origin required"})
    return await call_next(request)


class LocalStateRequest(BaseModel):
    entity_id: str
    action: Literal["seen", "dismiss"]


@app.get("/api/dashboard")
async def get_dashboard():
    return coordinator.dashboard()


@app.get("/api/health")
async def get_health():
    return coordinator.dashboard()["health"]


@app.post("/api/refresh")
async def refresh_dashboard():
    await coordinator.refresh()
    return coordinator.dashboard()


@app.post("/api/local-state")
async def update_local_state(request: LocalStateRequest):
    if not store.set_local_state(request.entity_id, request.action):
        raise HTTPException(status_code=404, detail="Dashboard item not found")
    return coordinator.dashboard()


@app.get("/")
async def index():
    return FileResponse(settings.project_root / "static" / "index.html")


app.mount("/static", StaticFiles(directory=settings.project_root / "static"), name="static")


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    threading.Timer(
        1.0, lambda: webbrowser.open(f"http://{settings.app.host}:{settings.app.port}")
    ).start()
    uvicorn.run(app, host=settings.app.host, port=settings.app.port, log_level="info")
