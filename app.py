from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import load_settings
from refresh import RefreshCoordinator
from store import Store
from startup import StartupSettings, acquire_instance, listener_identity


settings = load_settings()
startup = StartupSettings(settings.project_root)
store = Store(settings.database_path)
coordinator = RefreshCoordinator(settings, store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    startup.initialize()
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
    response = await call_next(request)
    if request.url.path == '/' or request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return response


class StartupRequest(BaseModel):
    enabled: bool


@app.get('/api/settings/startup')
async def get_startup():
    return startup.status()


@app.post('/api/settings/startup')
async def set_startup(request: StartupRequest):
    if not startup.supported:
        raise HTTPException(status_code=400, detail='Launch at sign-in is available on Windows.')
    try:
        return startup.set_enabled(request.enabled)
    except OSError:
        raise HTTPException(status_code=500, detail='Windows startup setting could not be saved.')


class LocalStateRequest(BaseModel):
    entity_id: str
    action: Literal["seen", "unread", "dismiss", "dismiss_related", "restore"]


class BatchReadRequest(BaseModel):
    entity_ids: list[str] = Field(min_length=1, max_length=100000)
    action: Literal['seen', 'unread']


@app.post('/api/batch-read')
async def batch_read(request: BatchReadRequest):
    count = store.mark_batch(request.entity_ids, request.action)
    return {**coordinator.dashboard(), 'changed_count': count}


@app.get("/api/dashboard")
async def get_dashboard():
    return coordinator.dashboard()


@app.get("/api/identity")
async def identity():
    return {"application": "much-ado-about-jira"}


@app.get("/api/health")
async def get_health():
    return coordinator.dashboard()["health"]


@app.post("/api/refresh")
async def refresh_dashboard():
    await coordinator.refresh()
    return coordinator.dashboard()


@app.post("/api/local-state")
async def update_local_state(request: LocalStateRequest):
    if request.action in {'dismiss', 'dismiss_related'}:
        undo = store.dismiss_updates(request.entity_id, request.action == 'dismiss_related')
        if not undo:
            raise HTTPException(status_code=404, detail='Update no longer available for dismissal')
        return {**coordinator.dashboard(), 'undo_entries': undo}
    if not store.set_local_state(request.entity_id, request.action):
        raise HTTPException(status_code=404, detail="Dashboard item not found")
    return coordinator.dashboard()


class UndoEntry(BaseModel):
    entity_id: str
    version: str


class UndoRequest(BaseModel):
    entries: list[UndoEntry] = Field(min_length=1)


@app.post('/api/undo-dismiss')
async def undo_dismiss(request: UndoRequest):
    store.restore_dismissed([entry.model_dump() for entry in request.entries])
    return coordinator.dashboard()


@app.get("/")
async def index():
    static_root = settings.project_root / 'static'
    html = (static_root / 'index.html').read_text(encoding='utf-8')
    for name in ('app.js', 'theme.js', 'styles.css', 'favicon.svg'):
        digest = hashlib.sha256((static_root / name).read_bytes()).hexdigest()[:16]
        html = html.replace(f'/static/{name}', f'/static/{name}?v={digest}')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


app.mount("/static", StaticFiles(directory=settings.project_root / "static"), name="static")


def main():
    import threading
    import webbrowser

    import uvicorn

    import sys
    import socket
    background = '--background' in sys.argv
    instance_handle = acquire_instance(settings.app.port)
    listener = listener_identity(settings.app.host, settings.app.port)
    if listener == 'other':
        message = f'Port {settings.app.port} is already used by another application. Close that application or choose another port in settings.toml.'
        if not background:
            import tkinter.messagebox
            tkinter.messagebox.showerror('Dashboard could not start', message)
        else:
            import logging
            logging.error(message)
        sys.exit(1)
    if not instance_handle or listener == 'ours':
        if not background and listener == 'ours':
            webbrowser.open(f"http://{settings.app.host}:{settings.app.port}")
        sys.exit(0)
    if not background:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{settings.app.host}:{settings.app.port}")).start()
    uvicorn.run(app, host=settings.app.host, port=settings.app.port,
                log_level="info", **({'log_config': None} if background else {}))


if __name__ == "__main__":
    main()
