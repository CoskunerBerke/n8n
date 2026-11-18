from __future__ import annotations

import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import database as db
from app.config import (
    CREDENTIALS_DIR,
    get_public_settings_dict,
    get_settings,
    reload_settings,
    resolve_path,
    update_runtime_settings,
)
from app.engine.pipeline_orchestrator import PipelineOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("clipper.api")

WEB_DIR = Path(__file__).resolve().parent / "web"

orchestrator: Optional[PipelineOrchestrator] = None


def get_orchestrator() -> PipelineOrchestrator:
    global orchestrator
    if orchestrator is None:
        orchestrator = PipelineOrchestrator()
    return orchestrator


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()
    db.init_db()
    orch = get_orchestrator()
    if os.environ.get("CLIPPER_DISABLE_BG") != "1":
        orch.start_background_services()
    try:
        yield
    finally:
        if os.environ.get("CLIPPER_DISABLE_BG") != "1":
            await orch.stop_background_services()


app = FastAPI(
    title="GTA 6 Dual-Pipeline Auto-Clipper",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount rendered media and static web UI assets
get_settings()
app.mount("/media/rendered", StaticFiles(directory=str(get_settings().rendered_dir)), name="rendered_media")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


class PipelinePatch(BaseModel):
    autopilot: Optional[bool] = None
    live_enabled: Optional[bool] = None
    clips_enabled: Optional[bool] = None
    game_keywords: Optional[list[str]] = None


class StreamerCreate(BaseModel):
    pipeline_id: str = Field(..., pattern="^(global|tr)$")
    username: str
    display_name: Optional[str] = None
    platform: str = Field(..., pattern="^(kick|twitch|youtube)$")
    channel_url: Optional[str] = None
    live_watch: bool = True
    clip_scan: bool = True


class StreamerPatch(BaseModel):
    enabled: Optional[bool] = None
    live_watch: Optional[bool] = None
    clip_scan: Optional[bool] = None


class SubmitUrlRequest(BaseModel):
    pipeline_id: str = Field(..., pattern="^(global|tr)$")
    url: str
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


class PublishClipRequest(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["youtube", "instagram"])
    youtube_title: Optional[str] = None
    youtube_description: Optional[str] = None
    instagram_caption: Optional[str] = None


class RejectClipRequest(BaseModel):
    reason: str = "Manuel reddedildi"


def _detect_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    return FileResponse(str(WEB_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    s = get_settings()
    orch = get_orchestrator()
    return {
        "status": "ok",
        "scheduler_enabled": s.internal_scheduler_enabled,
        "telegram_configured": orch.telegram.is_configured(),
        "youtube_global_connected": orch.youtube_pub.is_connected("global"),
        "youtube_tr_connected": orch.youtube_pub.is_connected("tr"),
        "instagram_global_configured": orch.instagram_pub.is_configured("global"),
        "instagram_tr_configured": orch.instagram_pub.is_configured("tr"),
        "live_sessions": orch.live_watcher.get_active_sessions_summary(),
    }


@app.get("/api/dashboard")
async def get_dashboard() -> dict[str, Any]:
    orch = get_orchestrator()
    stats = db.get_dashboard_stats()
    lan_ip = _detect_lan_ip()
    port = get_settings().port
    return {
        **stats,
        "live_sessions": orch.live_watcher.get_active_sessions_summary(),
        "recent_logs": db.list_logs(limit=25),
        "network": {
            "lan_ip": lan_ip,
            "mobile_url": f"http://{lan_ip}:{port}",
            "n8n_url": "http://localhost:5678",
        },
        "accounts": {
            "youtube_global": orch.youtube_pub.is_connected("global"),
            "youtube_tr": orch.youtube_pub.is_connected("tr"),
            "youtube_global_channel": orch.youtube_pub.get_channel_title("global"),
            "youtube_tr_channel": orch.youtube_pub.get_channel_title("tr"),
            "instagram_global": orch.instagram_pub.is_configured("global"),
            "instagram_tr": orch.instagram_pub.is_configured("tr"),
            "telegram": orch.telegram.is_configured(),
        },
    }


@app.get("/api/pipelines")
async def get_pipelines() -> list[dict[str, Any]]:
    return db.list_pipelines()


@app.patch("/api/pipelines/{pipeline_id}")
async def patch_pipeline(pipeline_id: str, body: PipelinePatch) -> dict[str, Any]:
    updated = db.update_pipeline(pipeline_id, body.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Pipeline bulunamadı")
    return updated


@app.post("/api/pipelines/{pipeline_id}/run-cycle")
async def run_pipeline_cycle(pipeline_id: str, max_new: int = 3) -> dict[str, Any]:
    if pipeline_id not in {"global", "tr"}:
        raise HTTPException(status_code=404, detail="Geçersiz pipeline ID (tr veya global olmalı)")
    return await get_orchestrator().run_pipeline_cycle(pipeline_id, max_new=max_new)


@app.post("/api/pipelines/{pipeline_id}/schedule-youtube-batch")
async def schedule_youtube_batch(pipeline_id: str, max_uploads: int = 4) -> dict[str, Any]:
    if pipeline_id not in {"global", "tr"}:
        raise HTTPException(status_code=404, detail="Geçersiz pipeline ID (tr veya global olmalı)")
    return await get_orchestrator().schedule_queued_to_youtube(pipeline_id, max_uploads=max_uploads)


@app.post("/api/clips/send-vault-telegram")
async def send_vault_telegram(pipeline_id: Optional[str] = None) -> dict[str, int]:
    sent = await get_orchestrator().push_queue_to_telegram_vault(pipeline_id=pipeline_id)
    return {"sent": sent}


@app.get("/api/streamers")
async def get_streamers(pipeline_id: Optional[str] = None) -> list[dict[str, Any]]:
    return db.list_streamers(pipeline_id=pipeline_id)


@app.post("/api/streamers")
async def create_streamer(body: StreamerCreate) -> dict[str, Any]:
    return db.add_streamer(
        pipeline_id=body.pipeline_id,
        username=body.username,
        platform=body.platform,
        display_name=body.display_name,
        channel_url=body.channel_url,
        live_watch=body.live_watch,
        clip_scan=body.clip_scan,
    )


@app.patch("/api/streamers/{streamer_id}")
async def patch_streamer(streamer_id: int, body: StreamerPatch) -> dict[str, Any]:
    updated = db.update_streamer(streamer_id, body.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Yayıncı bulunamadı")
    return updated


@app.delete("/api/streamers/{streamer_id}")
async def remove_streamer(streamer_id: int) -> dict[str, bool]:
    ok = db.delete_streamer(streamer_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Yayıncı bulunamadı")
    return {"deleted": True}


@app.get("/api/clips")
async def get_clips(
    pipeline_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if status == "pending_review":
        s = get_settings()
        pids = [pipeline_id] if pipeline_id in {"tr", "global"} else ["tr", "global"]
        combined: list[dict[str, Any]] = []
        for pid in pids:
            combined.extend(
                db.compute_pipeline_schedule(
                    pid,
                    s.max_daily_youtube_per_pipeline,
                    s.min_publish_interval_minutes,
                    limit=limit,
                )
            )
        combined.sort(key=lambda c: c.get("scheduled_for") or "")
        return combined[:limit]
    return db.list_clips(pipeline_id=pipeline_id, status=status, limit=limit)


@app.post("/api/clips/submit-url")
async def submit_clip_url(body: SubmitUrlRequest) -> dict[str, Any]:
    try:
        return await get_orchestrator().submit_manual_url(
            pipeline_id=body.pipeline_id,
            url=body.url.strip(),
            start_sec=body.start_sec,
            end_sec=body.end_sec,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clips/{clip_id}/publish")
async def publish_clip(clip_id: str, body: PublishClipRequest) -> dict[str, Any]:
    try:
        return await get_orchestrator().publish_clip(
            clip_id=clip_id,
            targets=body.targets,
            override_title=body.youtube_title,
            override_description=body.youtube_description,
            override_caption=body.instagram_caption,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clips/{clip_id}/reject")
async def reject_clip(clip_id: str, body: RejectClipRequest) -> dict[str, Any]:
    updated = await get_orchestrator().reject_clip(clip_id, body.reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Klip bulunamadı")
    return updated


@app.post("/api/clips/{clip_id}/regenerate-metadata")
async def regenerate_metadata(clip_id: str) -> dict[str, Any]:
    updated = await get_orchestrator().regenerate_clip_metadata(clip_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Klip bulunamadı")
    return updated


@app.get("/api/settings")
async def get_settings_endpoint() -> dict[str, Any]:
    data = get_public_settings_dict()
    orch = get_orchestrator()
    data["youtube_global_channel"] = orch.youtube_pub.get_channel_title("global")
    data["youtube_tr_channel"] = orch.youtube_pub.get_channel_title("tr")
    return data


@app.post("/api/settings")
async def save_settings_endpoint(patch: dict[str, Any]) -> dict[str, Any]:
    updated = update_runtime_settings(patch)
    reload_settings()
    orch = get_orchestrator()
    updated["youtube_global_channel"] = orch.youtube_pub.get_channel_title("global")
    updated["youtube_tr_channel"] = orch.youtube_pub.get_channel_title("tr")
    return updated


@app.post("/api/settings/upload-youtube-secret")
async def upload_youtube_secret(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read()
    target = CREDENTIALS_DIR / "youtube_client_secret.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {"uploaded": True, "path": "credentials/youtube_client_secret.json"}


@app.get("/api/auth/youtube/{pipeline_id}/start")
async def youtube_auth_start(pipeline_id: str, request: Request) -> dict[str, str]:
    if pipeline_id not in {"global", "tr"}:
        raise HTTPException(status_code=400, detail="Geçersiz pipeline ID")
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/auth/youtube/callback"
    try:
        auth_url = get_orchestrator().youtube_pub.build_auth_url(pipeline_id, redirect_uri)
        return {"auth_url": auth_url, "redirect_uri": redirect_uri}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/youtube/callback", response_class=HTMLResponse)
async def youtube_auth_callback(request: Request, state: str = "", code: str = "") -> HTMLResponse:
    if not state or not code:
        return HTMLResponse("<h3>Eksik OAuth parametresi. Pencereyi kapatıp tekrar deneyin.</h3>", status_code=400)
    try:
        # Allow local http callback during OAuth2 development
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        pid = get_orchestrator().youtube_pub.complete_auth(state, str(request.url))
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;background:#09090b;color:#fff;padding:40px;'>"
            f"<h2>✅ {pid.upper()} YouTube hesabı başarıyla bağlandı!</h2>"
            f"<p>Bu sekmeyi kapatıp kontrol paneline dönebilirsiniz.</p>"
            f"<script>setTimeout(()=>window.location.href='/',1500);</script></body></html>"
        )
    except Exception as exc:
        return HTMLResponse(f"<h3>Bağlantı hatası: {exc}</h3>", status_code=400)


@app.post("/api/webhooks/kick")
async def kick_webhook_endpoint(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    event_type = request.headers.get("Kick-Event-Type", "")
    payload = await request.json()
    res = await get_orchestrator().handle_kick_webhook(event_type, payload)
    return JSONResponse(res)


@app.get("/api/logs")
async def get_logs(limit: int = 40) -> list[dict[str, Any]]:
    return db.list_logs(limit=limit)
