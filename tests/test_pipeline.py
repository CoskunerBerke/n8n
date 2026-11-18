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


def test_youtube_publish_pacing(isolated_env: Path) -> None:
    initial = db.get_publish_pacing("tr")
    assert initial["published_last_24h"] == 0
    assert initial["minutes_since_last_publish"] is None

    db.insert_clip(
        {
            "id": "pace_test_1",
            "pipeline_id": "tr",
            "streamer_username": "rraenee",
            "streamer_display_name": "RRaenee",
            "source_platform": "kick",
            "source_type": "trending_clip",
            "source_id": "pace_src_1",
            "source_url": "https://kick.com/rraenee/clips/pace_src_1",
            "status": "published",
            "youtube_video_id": "yt_abc123",
        }
    )
    after = db.get_publish_pacing("tr")
    assert after["published_last_24h"] == 1
    assert after["minutes_since_last_publish"] is not None
    assert after["minutes_since_last_publish"] < 1.0


def test_queue_cap_and_schedule(isolated_env: Path) -> None:
    rendered_dir = isolated_env / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    for idx, score in enumerate((7.5, 9.6, 8.9, 6.2), start=1):
        mp4 = rendered_dir / f"q_{idx}.mp4"
        mp4.write_bytes(b"fake_mp4")
        db.insert_clip(
            {
                "id": f"q_{idx}",
                "pipeline_id": "global",
                "streamer_username": "kaicenat",
                "streamer_display_name": "Kai Cenat",
                "source_platform": "twitch",
                "source_type": "live_spike",
                "source_id": f"src_q_{idx}",
                "source_url": f"https://twitch.tv/kaicenat/q_{idx}",
                "rendered_path": str(mp4),
                "viral_score": score,
                "duration_sec": 35.0,
                "status": "pending_review",
                "youtube_title": f"Clip {idx}",
            }
        )

    evicted = db.enforce_queue_cap("global", 2)
    assert evicted == 2
    # Highest-scoring two (9.6 and 8.9) survive; lowest two are evicted and files deleted
    assert (rendered_dir / "q_2.mp4").exists()
    assert (rendered_dir / "q_3.mp4").exists()
    assert not (rendered_dir / "q_1.mp4").exists()
    assert not (rendered_dir / "q_4.mp4").exists()

    sched = db.compute_pipeline_schedule("global", daily_cap=4, interval_minutes=210)
    assert [c["id"] for c in sched] == ["q_2", "q_3"]
    assert sched[0]["queue_rank"] == 1
    assert sched[1]["queue_rank"] == 2
    assert sched[1]["scheduled_for"] > sched[0]["scheduled_for"]

    # Also test rolling-24h cap spillover when daily_cap=1
    sched_capped = db.compute_pipeline_schedule("global", daily_cap=1, interval_minutes=210)
    assert len(sched_capped) == 2
    assert sched_capped[1]["scheduled_for"] > sched_capped[0]["scheduled_for"]

    # Confirm future scheduled_for on a published clip advances the next queued clip by >= 210 minutes
    from datetime import datetime, timedelta, timezone

    future_slot = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    db.update_clip("q_2", {"status": "published", "youtube_video_id": "yt_sched_1", "scheduled_for": future_slot})
    sched_after = db.compute_pipeline_schedule("global", daily_cap=4, interval_minutes=210)
    assert [c["id"] for c in sched_after] == ["q_3"]
    next_dt = datetime.fromisoformat(sched_after[0]["scheduled_for"])
    prev_dt = datetime.fromisoformat(future_slot)
    assert (next_dt - prev_dt).total_seconds() >= 209 * 60


def test_strict_gta6_game_filter() -> None:
    from app.engine.game_filter import is_gta6_match

    # Non-GTA 6 categories and titles must be rejected
    assert is_gta6_match("Just Chatting", "insane reaction") is False
    assert is_gta6_match("Counter-Strike 2", "ace clutch") is False
    assert is_gta6_match("Grand Theft Auto V", "roleplay moments") is False
    assert is_gta6_match("Grand Theft Auto VI", "FiveM RP server") is False
    assert is_gta6_match("", "Playing GTA 5 Online tonight") is False

    # Genuine GTA 6 categories/titles must pass
    assert is_gta6_match("Grand Theft Auto VI", "OHA BU NE") is True
    assert is_gta6_match("GTA VI", "First look") is True
    assert is_gta6_match("Just Chatting", "Reacting to GTA 6 trailer") is True
    assert is_gta6_match("", "GTA 6 Vice City first mission") is True


def test_template_metadata_sanitization() -> None:
    from app.engine.quality_judge import _build_template_metadata

    meta = _build_template_metadata(
        language="tr",
        streamer_name="Elraenn",
        source_title="GTA 6 Başladı !insta !kick [Ses/reaksiyon patlaması (-18.2 dB vs taban -36.0 dB)]",
        transcript="Beyler bu araba ne böyle",
        viral_score=8.7,
    )
    assert "[" not in meta["youtube_title"]
    assert "Ses/reaksiyon" not in meta["youtube_title"]
    assert "!insta" not in meta["youtube_title"]
    assert "!kick" not in meta["youtube_title"]
    assert "GTA 6 Başladı" in meta["youtube_title"]
    assert "Ses/reaksiyon" not in meta["youtube_description"]


def test_cross_pipeline_dedup_and_disabled_seed(isolated_env: Path) -> None:
    # rraenee should be seeded disabled by default while active streamers stay enabled
    tr_streamers = db.list_streamers("tr")
    rraenee_rows = [s for s in tr_streamers if s["username"] == "rraenee"]
    assert rraenee_rows and all(not s["enabled"] for s in rraenee_rows)

    db.insert_clip(
        {
            "id": "dedup_clip_1",
            "pipeline_id": "global",
            "streamer_username": "xqc",
            "streamer_display_name": "xQc",
            "source_platform": "kick",
            "source_type": "trending_clip",
            "source_id": "shared_clip_999",
            "source_url": "https://kick.com/xqc/clips/shared_clip_999",
            "status": "pending_review",
        }
    )
    # Cross-pipeline duplicate by source_platform+source_id or non-live_spike source_url must be blocked
    assert db.clip_exists("tr", "kick", "shared_clip_999", "https://kick.com/other") is True
    assert db.clip_exists("tr", "twitch", "other_id", "https://kick.com/xqc/clips/shared_clip_999") is True

    # Live spike captures share the bare channel URL, so a second live spike with a new source_id must still pass
    db.insert_clip(
        {
            "id": "dedup_live_1",
            "pipeline_id": "global",
            "streamer_username": "xqc",
            "streamer_display_name": "xQc",
            "source_platform": "kick",
            "source_type": "live_spike",
            "source_id": "xqc_1795000001",
            "source_url": "https://kick.com/xqc",
            "status": "pending_review",
        }
    )
    assert db.clip_exists("global", "kick", "xqc_1795000999", "https://kick.com/xqc") is False
    assert db.clip_exists("tr", "kick", "xqc_1795000999", "https://kick.com/xqc") is False
