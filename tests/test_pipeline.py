from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.config import BASE_DIR, reload_settings
from app.engine.quality_judge import ClipQualityJudge
from app.engine.video_renderer import VideoRenderer


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLIPPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLIPPER_DISABLE_BG", "1")
    monkeypatch.setenv("WHISPER_ENABLED", "false")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    reload_settings()
    db.init_db()
    return tmp_path


def test_dual_pipeline_and_streamer_seed(isolated_env: Path) -> None:
    pipelines = {p["id"]: p for p in db.list_pipelines()}
    assert "global" in pipelines
    assert "tr" in pipelines
    assert pipelines["global"]["language"] == "en"
    assert pipelines["tr"]["language"] == "tr"

    global_users = {s["username"] for s in db.list_streamers("global")}
    tr_users = {s["username"] for s in db.list_streamers("tr")}
    assert {"ishowspeed", "kaicenat", "xqc", "adinross"}.issubset(global_users)
    assert {"rraenee", "elraenn", "jahrein", "wtcn", "mithrain"}.issubset(tr_users)


@pytest.mark.asyncio
async def test_quality_judge_rejects_silent_and_static_clips(isolated_env: Path) -> None:
    judge = ClipQualityJudge()

    # 1. Silent black 32s clip -> rejected at Layer 1 (silence gate)
    silent_mp4 = isolated_env / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=30:d=32",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "32",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(silent_mp4),
        ],
        check=True,
    )
    verdict_silent = await judge.evaluate(
        video_path=silent_mp4,
        clip_id="test_silent",
        language="tr",
        streamer_name="RRaenee",
    )
    assert verdict_silent.approved is False
    assert "sessiz" in verdict_silent.reject_reason.lower() or "konuşma" in verdict_silent.reject_reason.lower()

    # 2. Static screen with audible sine tone -> rejected at Layer 2 (static/AFK gate)
    static_mp4 = isolated_env / "static_audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1280x720:r=30:d=32",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=32",
            "-t",
            "32",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(static_mp4),
        ],
        check=True,
    )
    verdict_static = await judge.evaluate(
        video_path=static_mp4,
        clip_id="test_static",
        language="tr",
        streamer_name="Elraenn",
    )
    assert verdict_static.approved is False
    assert "sabit" in verdict_static.reject_reason.lower() or "afk" in verdict_static.reject_reason.lower()


@pytest.mark.asyncio
async def test_vertical_9x16_render_with_burned_subtitles(isolated_env: Path) -> None:
    raw_mp4 = isolated_env / "dynamic_input.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=30:duration=34",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=520:duration=34",
            "-t",
            "34",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(raw_mp4),
        ],
        check=True,
    )

    segments = [
        {"start": 1.0, "end": 4.5, "text": "Oha GTA 6 açıldı inanılmaz görünüyor!"},
        {"start": 5.0, "end": 9.0, "text": "Şu grafiklere ve fiziklere bakın arkadaşlar!"},
    ]
    renderer = VideoRenderer()
    out_path = await renderer.render_vertical_short(
        raw_path=raw_mp4,
        clip_id="test_render_tr",
        segments=segments,
        streamer_name="RRaenee",
        trim_start=0.0,
        trim_end=34.0,
    )
    assert out_path.exists()

    probe_proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    probe = json.loads(probe_proc.stdout)
    streams = probe["streams"]
    video_stream = next(s for s in streams if s["codec_type"] == "video")
    audio_stream = next(s for s in streams if s["codec_type"] == "audio")
    duration = float(probe["format"]["duration"])

    assert video_stream["width"] == 1080
    assert video_stream["height"] == 1920
    assert video_stream["codec_name"] == "h264"
    assert audio_stream["codec_name"] == "aac"
    assert 32.5 <= duration <= 35.5


def test_fastapi_endpoints_and_pwa(isolated_env: Path) -> None:
    from app.main import app

    with TestClient(app) as client:
        for route in ("/api/health", "/api/dashboard", "/api/pipelines", "/api/streamers", "/", "/manifest.json"):
            resp = client.get(route)
            assert resp.status_code == 200, f"Route failed: {route} -> {resp.status_code}"

        health = client.get("/api/health").json()
        assert health["status"] == "ok"


def test_n8n_workflows_valid_json() -> None:
    wf_dir = BASE_DIR / "n8n" / "workflows"
    files = list(wf_dir.glob("*.json"))
    assert len(files) >= 3
    for wf_file in files:
        data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert isinstance(data.get("nodes"), list) and len(data["nodes"]) > 0
        assert isinstance(data.get("connections"), dict)
