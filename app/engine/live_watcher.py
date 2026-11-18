from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app import database as db
from app.config import get_settings
from app.sources import ytdlp_helper
from app.sources.kick_client import KickClient
from app.sources.twitch_client import TwitchChatSpikeMonitor, TwitchClient
from app.sources.youtube_source import YouTubeSourceClient

logger = logging.getLogger(__name__)


@dataclass
class ActiveStreamSession:
    pipeline_id: str
    platform: str
    username: str
    display_name: str
    channel_url: str
    stream_url: str
    title: str
    buffer_dir: Path
    process: Optional[asyncio.subprocess.Process] = None
    monitor_task: Optional[asyncio.Task[None]] = None
    pending_spike_reason: str = ""
    last_capture_at: float = 0.0
    baseline_rms: deque[float] = field(default_factory=lambda: deque(maxlen=12))


async def _measure_segment_rms_db(seg_path: Path) -> Optional[float]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(seg_path),
        "-af",
        "astats=metadata=1:reset=1",
        "-f",
        "null",
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=12.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    text = stderr.decode("utf-8", errors="replace")
    matches = re.findall(r"RMS level dB:\s*(-?[0-9.]+)", text)
    if not matches:
        return None
    vals = [float(x) for x in matches if x != "-inf"]
    return max(vals) if vals else None


class LiveStreamWatcher:
    """Watches live Kick/Twitch/YouTube streams with a rolling FFmpeg ring buffer & spike triggers."""

    def __init__(
        self,
        twitch: TwitchClient,
        kick: KickClient,
        youtube_src: YouTubeSourceClient,
        on_live_clip: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.twitch = twitch
        self.kick = kick
        self.youtube_src = youtube_src
        self.on_live_clip = on_live_clip
        self.chat_monitor = TwitchChatSpikeMonitor(on_spike=self.handle_chat_spike)
        self._sessions: dict[tuple[str, str], ActiveStreamSession] = {}
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._running = False

    def start(self) -> None:
        self.chat_monitor.start()
        if not self._loop_task or self._loop_task.done():
            self._running = True
            self._loop_task = asyncio.create_task(self._discovery_loop())

    async def stop(self) -> None:
        self._running = False
        await self.chat_monitor.stop()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for key in list(self._sessions.keys()):
            await self._stop_session(key)

    def get_active_sessions_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "pipeline_id": s.pipeline_id,
                "platform": s.platform,
                "username": s.username,
                "display_name": s.display_name,
                "title": s.title,
            }
            for s in self._sessions.values()
        ]

    async def handle_chat_spike(self, platform: str, username: str, reason: str) -> None:
        key = (platform.lower(), username.lower())
        session = self._sessions.get(key)
        if not session:
            return
        cooldown = float(get_settings().live_spike_cooldown_seconds)
        if time.time() - session.last_capture_at < cooldown:
            return
        session.pending_spike_reason = reason

    async def _discovery_loop(self) -> None:
        # Short initial delay so HTTP server binds immediately on startup
        await asyncio.sleep(4.0)
        while self._running:
            try:
                await self._check_live_channels()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Canlı yayın kontrol döngüsü hatası: %s", exc)
            await asyncio.sleep(max(20, get_settings().live_check_interval_seconds))

    async def _check_live_channels(self) -> None:
        settings = get_settings()
        pipelines = {p["id"]: p for p in db.list_pipelines()}
        streamers = [
            s
            for s in db.list_streamers(enabled_only=True)
            if s["live_watch"] and pipelines.get(s["pipeline_id"], {}).get("live_enabled", False)
        ]

        # Batch API queries where available
        twitch_users = [s["username"] for s in streamers if s["platform"] == "twitch"]
        kick_users = [s["username"] for s in streamers if s["platform"] == "kick"]

        twitch_live = await self.twitch.get_live_streams(twitch_users) if twitch_users else {}
        kick_live = await self.kick.get_channels_status(kick_users) if kick_users else {}

        live_twitch_channels: list[str] = []
        active_keys_now: set[tuple[str, str]] = set()
        now_iso = db.utc_now_iso()

        for st in streamers:
            platform = st["platform"]
            uname = st["username"]
            key = (platform, uname)
            is_live = False
            live_title = ""
            stream_url = ""

            if platform == "twitch" and self.twitch.is_configured():
                info = twitch_live.get(uname)
                if info and info.get("is_live"):
                    is_live = True
                    live_title = info.get("title", "")
            elif platform == "kick" and self.kick.is_configured():
                info = kick_live.get(uname)
                if info and info.get("is_live"):
                    is_live = True
                    live_title = info.get("title", "")
            else:
                # Direct yt-dlp probe for YouTube or unauthenticated Kick/Twitch channels
                if len(self._sessions) < settings.max_concurrent_live_streams or key in self._sessions:
                    resolved = await ytdlp_helper.resolve_live_stream_url(st["channel_url"])
                    if resolved and resolved.get("is_live"):
                        is_live = True
                        live_title = resolved.get("title", "")
                        stream_url = resolved.get("stream_url", "")

            db.update_streamer(
                st["id"],
                {
                    "is_live": is_live,
                    "last_live_title": live_title if is_live else "",
                    "last_checked_at": now_iso,
                },
            )

            if is_live:
                active_keys_now.add(key)
                if platform == "twitch":
                    live_twitch_channels.append(uname)
                if key not in self._sessions and len(self._sessions) < settings.max_concurrent_live_streams:
                    if not stream_url:
                        resolved = await ytdlp_helper.resolve_live_stream_url(st["channel_url"])
                        stream_url = (resolved or {}).get("stream_url", "")
                    if stream_url:
                        await self._start_session(st, stream_url, live_title)

        await self.chat_monitor.sync_channels(live_twitch_channels)

        # Clean up streams that went offline
        for existing_key in list(self._sessions.keys()):
            if existing_key not in active_keys_now:
                await self._stop_session(existing_key)

    async def _start_session(self, streamer: dict[str, Any], stream_url: str, title: str) -> None:
        settings = get_settings()
        key = (streamer["platform"], streamer["username"])
        buf_dir = settings.live_buffer_dir / f"{streamer['pipeline_id']}_{streamer['platform']}_{streamer['username']}"
        buf_dir.mkdir(parents=True, exist_ok=True)
        for old in buf_dir.glob("*.ts"):
            old.unlink(missing_ok=True)

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            stream_url,
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "5",
            "-segment_wrap",
            "24",
            "-reset_timestamps",
            "1",
            "seg_%03d.ts",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(buf_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session = ActiveStreamSession(
            pipeline_id=streamer["pipeline_id"],
            platform=streamer["platform"],
            username=streamer["username"],
            display_name=streamer["display_name"],
            channel_url=streamer["channel_url"],
            stream_url=stream_url,
            title=title,
            buffer_dir=buf_dir,
            process=proc,
        )
        session.monitor_task = asyncio.create_task(self._monitor_ring_buffer(key, session))
        self._sessions[key] = session
        db.add_log(
            f"Canlı yayın izlemeye alındı: {streamer['display_name']} ({streamer['platform']})",
            pipeline_id=streamer["pipeline_id"],
            source="live_watcher",
        )

    async def _stop_session(self, key: tuple[str, str]) -> None:
        session = self._sessions.pop(key, None)
        if not session:
            return
        if session.monitor_task:
            session.monitor_task.cancel()
            try:
                await session.monitor_task
            except asyncio.CancelledError:
                pass
        if session.process and session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                session.process.kill()
        logger.info("Canlı yayın oturumu sonlandırıldı: %s", key)

    async def _monitor_ring_buffer(self, key: tuple[str, str], session: ActiveStreamSession) -> None:
        last_seen_name = ""
        try:
            while self._running:
                if session.process and session.process.returncode is not None:
                    logger.warning("Canlı kayıt FFmpeg sonlandı: %s", key)
                    break

                segments = sorted(session.buffer_dir.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)
                # Leave the newest active file untouched; inspect the latest completed segment
                if len(segments) >= 3:
                    completed = segments[-2]
                    if completed.name != last_seen_name:
                        last_seen_name = completed.name
                        rms = await _measure_segment_rms_db(completed)
                        if rms is not None:
                            baseline = (
                                sum(session.baseline_rms) / len(session.baseline_rms)
                                if len(session.baseline_rms) >= 4
                                else -36.0
                            )
                            if rms > -42.0:
                                session.baseline_rms.append(rms)
                            cooldown = float(get_settings().live_spike_cooldown_seconds)
                            if (
                                len(segments) >= 8
                                and rms > -28.0
                                and (rms - baseline) >= 6.5
                                and (time.time() - session.last_capture_at) >= cooldown
                            ):
                                session.pending_spike_reason = (
                                    f"Ses/reaksiyon patlaması ({rms:.1f} dB vs taban {baseline:.1f} dB)"
                                )

                if session.pending_spike_reason:
                    reason = session.pending_spike_reason
                    session.pending_spike_reason = ""
                    session.last_capture_at = time.time()
                    # Wait 10s so the reaction tail finishes writing to disk
                    await asyncio.sleep(10.0)
                    await self._export_last_40s_clip(session, reason)

                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Ring buffer izleme hatası (%s): %s", key, exc)

    async def _export_last_40s_clip(self, session: ActiveStreamSession, reason: str) -> None:
        settings = get_settings()
        queued = db.list_clips(
            pipeline_id=session.pipeline_id,
            status="pending_review",
            limit=settings.max_queue_per_pipeline + 2,
        )
        if len(queued) >= settings.max_queue_per_pipeline:
            return

        segments = sorted(session.buffer_dir.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)
        # Exclude the very last segment currently being written
        usable = segments[:-1] if len(segments) > 2 else segments
        if len(usable) < 6:
            return

        end_mtime = usable[-1].stat().st_mtime
        picked: list[Path] = []
        for seg in reversed(usable):
            picked.append(seg)
            if (end_mtime - seg.stat().st_mtime) >= 36.0 or len(picked) >= 20:
                break
        picked.reverse()
        if len(picked) < 6:
            return

        clip_id = f"live_{uuid.uuid4().hex[:10]}"
        raw_out = get_settings().raw_dir / f"{clip_id}.mp4"
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        concat_file = session.buffer_dir / f"concat_{clip_id}.txt"
        concat_file.write_text(
            "\n".join(f"file '{p.name}'" for p in picked),
            encoding="utf-8",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file.name,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(raw_out),
                cwd=str(session.buffer_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=45.0)
            if proc.returncode == 0 and raw_out.exists():
                await self.on_live_clip(
                    {
                        "clip_id": clip_id,
                        "pipeline_id": session.pipeline_id,
                        "streamer_username": session.username,
                        "streamer_display_name": session.display_name,
                        "source_platform": session.platform,
                        "source_type": "live_spike",
                        "source_id": f"{session.username}_{int(time.time())}",
                        "source_url": session.channel_url,
                        "source_title": f"{session.title} [{reason}]".strip(),
                        "raw_path": raw_out,
                    }
                )
        finally:
            concat_file.unlink(missing_ok=True)
