import asyncio
import json
import os
from pathlib import Path
from typing import Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import df_manager
import settings_manager

app = FastAPI(title="Dwarf Forge")

STATIC_DIR = Path(__file__).parent / "static"

# Mount static files (CSS, JS assets if any)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- WebSocket connection manager ---

connected_clients: Set[WebSocket] = set()


async def broadcast(message: str):
    disconnected = set()
    for ws in connected_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    connected_clients.difference_update(disconnected)


df_manager.set_broadcast_callback(broadcast)


# --- Routes ---

@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health():
    df_dir = Path(os.environ.get("DF_DIR", "/opt/dwarf-fortress"))
    saves_dir = Path(os.environ.get("SAVES_DIR", "/saves"))
    df_bin = df_dir / "df"
    return {
        "status": "ok",
        "df_path": str(df_dir),
        "df_binary_exists": df_bin.exists(),
        "df_version": df_manager.get_df_version(),
        "saves_dir": str(saves_dir),
        "generation_running": df_manager.is_running(),
    }


@app.get("/api/worldgen/params")
async def worldgen_params():
    return df_manager.WORLDGEN_PARAM_RANGES


@app.post("/api/generate")
async def generate(config: dict):
    if df_manager.is_running():
        raise HTTPException(status_code=409, detail="A generation job is already running.")
    asyncio.create_task(df_manager.run_generation(config))
    return {"status": "started"}


@app.post("/api/cancel")
async def cancel():
    cancelled = await df_manager.cancel_generation()
    return {"status": "cancelled" if cancelled else "not_running"}


@app.get("/api/worlds")
async def list_worlds():
    return df_manager.list_worlds()


@app.get("/api/worlds/{name}/legends")
async def get_legends(name: str):
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid world name.")
    data = df_manager.parse_legends(name)
    if "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    return data


@app.delete("/api/worlds/{name}")
async def delete_world(name: str):
    # Sanitize: disallow path traversal
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid world name.")
    deleted = df_manager.delete_world(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="World not found.")
    return {"status": "deleted"}


@app.get("/api/settings")
async def get_settings():
    s = settings_manager.get_settings()
    if s.get("smb_password"):
        s["smb_password"] = "***"
    return s


@app.post("/api/settings")
async def post_settings(data: dict):
    valid_destinations = {"local", "network"}
    valid_share_types  = {"smb", "nfs", "local"}
    if "save_destination" in data and data["save_destination"] not in valid_destinations:
        raise HTTPException(status_code=400, detail="Invalid save_destination value.")
    if "share_type" in data and data["share_type"] not in valid_share_types:
        raise HTTPException(status_code=400, detail="Invalid share_type value.")
    # If the client echoed back the masked sentinel, preserve the real password
    if data.get("smb_password") == "***":
        data["smb_password"] = settings_manager.get_settings()["smb_password"]
    settings_manager.save_settings(data)
    return {"status": "ok"}


@app.websocket("/ws/log")
async def websocket_log(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        # Send current state
        await websocket.send_text(json.dumps({
            "type": "status",
            "state": "running" if df_manager.is_running() else "idle",
        }))
        # Keep alive — wait for disconnect
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send ping
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)
