from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from app import database as db
from app.bot.telegram_bot import TelegramBotService
from app.config import get_settings
from app.engine.clip_scanner import ClipScanner
from app.engine.live_watcher import LiveStreamWatcher
from app.engine.quality_judge import ClipQualityJudge
from app.engine.video_renderer import VideoRenderer
from app.publishers.instagram_publisher import InstagramPublisher
from app.publishers.youtube_publisher import YouTubePublisher
from app.sources import ytdlp_helper
from app.sources.kick_client import KickClient
from app.sources.twitch_client import TwitchClient
from app.sources.youtube_source import YouTubeSourceClient

logger = logging.getLogger(__name__)


def _infer_platform(url: str) -> str:
    low = url.lower()
    if "kick.com" in low:
        return "kick"
    if "twitch.tv" in low:
        return "twitch"
    return "youtube"


def _slug_from_url(url: str) -> str:
    m = re.search(r"(?:kick\.com|twitch\.tv)/([^/?#]+)", url, re.I)
    if m:
        return m.group(1).lower()
    return "streamer"


class PipelineOrchestrator:
    """Coordinates TR & GLOBAL discovery, 3-layer quality gating, rendering, Telegram & publishers."""

    def __init__(self) -> None:
        self.twitch = TwitchClient()
        self.kick = KickClient()
        self.youtube_src = YouTubeSourceClient()
        self.judge = ClipQualityJudge()
        self.renderer = VideoRenderer()
        self.youtube_pub = YouTubePublisher()
        self.instagram_pub = InstagramPublisher()
        self.scanner = ClipScanner(self.twitch, self.youtube_src)
        self.live_watcher = LiveStreamWatcher(
            twitch=self.twitch,
            kick=self.kick,
            youtube_src=self.youtube_src,
            on_live_clip=self.process_live_capture,
        )
        self.telegram = TelegramBotService(
            on_publish=self.publish_clip,
            on_reject=self.reject_clip,
            on_regen=self.regenerate_clip_metadata,
            on_scan=self.run_pipeline_cycle,
            on_submit_url=lambda pid, url: self.submit_manual_url(pid, url),
            on_schedule_batch=self.schedule_queued_to_youtube,
            on_vault_telegram=self.push_queue_to_telegram_vault,
        )
        self._locks = {"global": asyncio.Lock(), "tr": asyncio.Lock()}
        self._youtube_backoff_until: dict[str, float] = {}
        self._scheduler_task: Optional[asyncio.Task[None]] = None
        self._queue_task: Optional[asyncio.Task[None]] = None
        self._running = False

    def start_background_services(self) -> None:
        self._running = True
        self.telegram.start()
        self.live_watcher.start()
        if get_settings().internal_scheduler_enabled and (
            not self._scheduler_task or self._scheduler_task.done()
        ):
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        if not self._queue_task or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._queue_publisher_loop())

    async def stop_background_services(self) -> None:
        self._running = False
        for task in (self._scheduler_task, self._queue_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.live_watcher.stop()
        await self.telegram.stop()

    async def _queue_publisher_loop(self) -> None:
        """Drip-publishes or pre-schedules queued clips on YouTube Studio (4/day, 3.5h spacing)."""
        await asyncio.sleep(20.0)
        while self._running:
            settings = get_settings()
            for pid in ("tr", "global"):
                try:
                    pipeline = db.get_pipeline(pid)
                    if not pipeline or not pipeline.get("autopilot"):
                        continue
                    if time.time() < self._youtube_backoff_until.get(pid, 0.0):
                        continue

                    if settings.youtube_schedule_ahead:
                        scheduled = db.compute_pipeline_schedule(
                            pid,
                            settings.max_daily_youtube_per_pipeline,
                            settings.min_publish_interval_minutes,
                            limit=1,
                        )
                        if scheduled:
                            next_clip = scheduled[0]
                            if next_clip.get("rendered_path") and Path(next_clip["rendered_path"]).exists():
                                await self._try_autopublish_clip(
                                    next_clip,
                                    notify_telegram=True,
                                    publish_at_iso=next_clip.get("scheduled_for"),
                                )
                        continue

                    pacing = db.get_publish_pacing(pid)
                    within_cap = pacing["published_last_24h"] < settings.max_daily_youtube_per_pipeline
                    last_min = pacing["minutes_since_last_publish"]
                    within_spacing = last_min is None or last_min >= settings.min_publish_interval_minutes
                    if not (within_cap and within_spacing):
                        continue
                    next_clip = db.get_next_queued_clip(pid)
                    if next_clip and next_clip.get("rendered_path") and Path(next_clip["rendered_path"]).exists():
                        await self._try_autopublish_clip(next_clip, notify_telegram=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Planlı kuyruk yayın döngüsü hatası (%s): %s", pid, exc)
            await asyncio.sleep(60.0)

    async def _try_autopublish_clip(
        self,
        clip: dict[str, Any],
        notify_telegram: bool = False,
        publish_at_iso: Optional[str] = None,
    ) -> bool:
        pid = clip["pipeline_id"]
        auto_targets = ["youtube"]
        if self.instagram_pub.is_configured(pid):
            auto_targets.append("instagram")
        try:
            res = await self.publish_clip(clip["id"], auto_targets, publish_at_iso=publish_at_iso)
            if notify_telegram:
                yt_id = res.get("youtube") or ""
                link = f"https://youtube.com/shorts/{yt_id}" if yt_id else (res.get("summary") or "")
                sched_label = ""
                if publish_at_iso:
                    try:
                        local_dt = datetime.fromisoformat(publish_at_iso.replace("Z", "+00:00")).astimezone()
                        sched_label = f" (Planlandı: {local_dt.strftime('%d.%m %H:%M')})"
                    except ValueError:
                        pass
                await self.telegram.send_text(
                    f"📅 YOUTUBE STUDIO'YA PLANLANDI!{sched_label}\n[{pid.upper()}] {clip['youtube_title']}\n{link}"
                )
            return True
        except Exception as exc:
            err_text = str(exc)
            if "uploadLimitExceeded" in err_text or "quotaExceeded" in err_text:
                # Keep clip in queue instead of dropping to failed; wait 4h before retry
                db.update_clip(clip["id"], {"status": "pending_review", "reject_reason": ""})
                self._youtube_backoff_until[pid] = time.time() + 4 * 3600
                db.add_log(
                    "YouTube günlük kanal yükleme sınırı dolu; klip sırada korundu, 4 saat sonra otomatik devam edecek",
                    pipeline_id=pid,
                    level="warning",
                    source="autopilot",
                )
            else:
                logger.warning("Otopilot yayınlama hatası (%s): %s", clip["id"], exc)
                db.add_log(f"Otopilot yayın hatası: {exc}", pipeline_id=pid, level="warning")
            return False

    async def schedule_queued_to_youtube(self, pipeline_id: str, max_uploads: int = 6) -> dict[str, Any]:
        """Pre-upload queued clips to YouTube Studio with native publishAt slots for offline PC days."""
        settings = get_settings()
        scheduled = db.compute_pipeline_schedule(
            pipeline_id,
            settings.max_daily_youtube_per_pipeline,
            settings.min_publish_interval_minutes,
            limit=max_uploads,
        )
        uploaded: list[dict[str, Any]] = []
        stopped_reason = ""
        for item in scheduled:
            if not item.get("rendered_path") or not Path(item["rendered_path"]).exists():
                continue
            ok = await self._try_autopublish_clip(
                item,
                notify_telegram=False,
                publish_at_iso=item.get("scheduled_for"),
            )
            if ok:
                uploaded.append(
                    {
                        "id": item["id"],
                        "title": item["youtube_title"],
                        "scheduled_for": item.get("scheduled_for"),
                    }
                )
            else:
                if time.time() < self._youtube_backoff_until.get(pipeline_id, 0.0):
                    stopped_reason = "YouTube günlük kanal yükleme sınırına ulaşıldı; kalanlar sırada korundu."
                    break
        return {
            "pipeline_id": pipeline_id,
            "scheduled_count": len(uploaded),
            "items": uploaded,
            "stopped_reason": stopped_reason,
        }

    async def push_queue_to_telegram_vault(self, pipeline_id: Optional[str] = None) -> int:
        """Send all unsent queued 9:16 videos and captions to Telegram Cloud for phone access."""
        sent = 0
        s = get_settings()
        pids = [pipeline_id] if pipeline_id in {"tr", "global"} else ["tr", "global"]
        for pid in pids:
            items = db.compute_pipeline_schedule(
                pid,
                s.max_daily_youtube_per_pipeline,
                s.min_publish_interval_minutes,
                limit=s.max_queue_per_pipeline,
            )
            for clip in items:
                if int(clip.get("telegram_message_id") or 0) > 0:
                    continue
                if not clip.get("rendered_path") or not Path(clip["rendered_path"]).exists():
                    continue
                await self.telegram.send_clip_for_approval(clip, autopilot_published=False)
                sent += 1
                await asyncio.sleep(1.0)
        return sent

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(15.0)
        while self._running:
            s = get_settings()
            if s.internal_scheduler_enabled:
                for pid in ("global", "tr"):
                    try:
                        await self.run_pipeline_cycle(pid, max_new=2)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Zamanlayıcı (%s) hatası: %s", pid, exc)
            await asyncio.sleep(max(2, s.scan_interval_minutes) * 60)

    async def run_pipeline_cycle(self, pipeline_id: str, max_new: int = 3) -> dict[str, Any]:
        lock = self._locks.setdefault(pipeline_id, asyncio.Lock())
        if lock.locked():
            return {
                "pipeline_id": pipeline_id,
                "skipped": True,
                "reason": "Tarama zaten çalışıyor",
                "processed": 0,
                "approved": 0,
                "rejected": 0,
                "clips": [],
            }

        async with lock:
            candidates = await self.scanner.scan_pipeline_candidates(pipeline_id)
            processed_clips: list[dict[str, Any]] = []
            approved_count = 0
            rejected_count = 0

            for cand in candidates[:max_new]:
                clip_id = f"{pipeline_id}_{uuid.uuid4().hex[:10]}"
                raw_path = get_settings().raw_dir / f"{clip_id}.mp4"
                try:
                    await ytdlp_helper.download_clip_media(cand["source_url"], raw_path)
                    clip = await self.process_candidate_video(
                        clip_id=clip_id,
                        pipeline_id=pipeline_id,
                        streamer_username=cand["streamer_username"],
                        streamer_display_name=cand["streamer_display_name"],
                        source_platform=cand["source_platform"],
                        source_type=cand["source_type"],
                        source_id=cand["source_id"],
                        source_url=cand["source_url"],
                        source_title=cand.get("title", ""),
                        views_count=int(cand.get("views_count", 0)),
                        raw_path=raw_path,
                    )
                    processed_clips.append(clip)
                    if clip["status"] == "rejected":
                        rejected_count += 1
                    else:
                        approved_count += 1
                except Exception as exc:
                    logger.warning("[%s] Klip işleme hatası (%s): %s", pipeline_id, cand["source_url"], exc)
                    db.add_log(
                        f"Klip indirilemedi ({cand['streamer_display_name']}): {exc}",
                        pipeline_id=pipeline_id,
                        level="warning",
                        source="scanner",
                    )
                    raw_path.unlink(missing_ok=True)

            db.add_log(
                f"Tarama tamamlandı: {len(processed_clips)} işlendi ({approved_count} onay, {rejected_count} elendi).",
                pipeline_id=pipeline_id,
                source="scanner",
            )
            return {
                "pipeline_id": pipeline_id,
                "skipped": False,
                "candidates_found": len(candidates),
                "processed": len(processed_clips),
                "approved": approved_count,
                "rejected": rejected_count,
                "clips": processed_clips,
            }

    async def process_live_capture(self, payload: dict[str, Any]) -> None:
        try:
            await self.process_candidate_video(
                clip_id=payload["clip_id"],
                pipeline_id=payload["pipeline_id"],
                streamer_username=payload["streamer_username"],
                streamer_display_name=payload["streamer_display_name"],
                source_platform=payload["source_platform"],
                source_type="live_spike",
                source_id=payload["source_id"],
                source_url=payload["source_url"],
                source_title=payload.get("source_title", ""),
                views_count=0,
                raw_path=Path(payload["raw_path"]),
            )
        except Exception as exc:
            logger.warning("Canlı klip işleme hatası: %s", exc)
            db.add_log(f"Canlı kesit hatası: {exc}", pipeline_id=payload.get("pipeline_id", ""), level="error")

    async def submit_manual_url(
        self,
        pipeline_id: str,
        url: str,
        start_sec: Optional[float] = None,
        end_sec: Optional[float] = None,
    ) -> dict[str, Any]:
        meta = await ytdlp_helper.inspect_url_metadata(url)
        platform = _infer_platform(url)
        uploader = (meta or {}).get("uploader") or _slug_from_url(url)
        source_id = (meta or {}).get("id") or uuid.uuid4().hex[:10]
        source_title = (meta or {}).get("title") or ""
        views_count = int((meta or {}).get("view_count") or 0)

        clip_id = f"man_{uuid.uuid4().hex[:10]}"
        raw_path = get_settings().raw_dir / f"{clip_id}.mp4"
        await ytdlp_helper.download_clip_media(url, raw_path, start_sec=start_sec, end_sec=end_sec)

        return await self.process_candidate_video(
            clip_id=clip_id,
            pipeline_id=pipeline_id,
            streamer_username=uploader.lower().replace(" ", ""),
            streamer_display_name=uploader,
            source_platform=platform,
            source_type="manual_url",
            source_id=source_id,
            source_url=url,
            source_title=source_title,
            views_count=views_count,
            raw_path=raw_path,
        )

    async def process_candidate_video(
        self,
        clip_id: str,
        pipeline_id: str,
        streamer_username: str,
        streamer_display_name: str,
        source_platform: str,
        source_type: str,
        source_id: str,
        source_url: str,
        source_title: str,
        views_count: int,
        raw_path: Path,
    ) -> dict[str, Any]:
        pipeline = db.get_pipeline(pipeline_id) or {"language": "tr" if pipeline_id == "tr" else "en", "autopilot": False}
        language = pipeline["language"]

        verdict = await self.judge.evaluate(
            video_path=raw_path,
            clip_id=clip_id,
            language=language,
            streamer_name=streamer_display_name,
            source_title=source_title,
        )

        if not verdict.approved:
            raw_path.unlink(missing_ok=True)
            rejected = db.insert_clip(
                {
                    "id": clip_id,
                    "pipeline_id": pipeline_id,
                    "streamer_username": streamer_username,
                    "streamer_display_name": streamer_display_name,
                    "source_platform": source_platform,
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_url": source_url,
                    "source_title": source_title,
                    "duration_sec": verdict.duration_sec,
                    "views_count": views_count,
                    "contact_sheet_path": verdict.contact_sheet_path,
                    "transcript": verdict.transcript,
                    "segments": verdict.segments,
                    "word_count": verdict.word_count,
                    "non_silent_ratio": verdict.non_silent_ratio,
                    "gameplay_motion_score": verdict.gameplay_motion_score,
                    "faces_detected": verdict.faces_detected,
                    "viral_score": verdict.viral_score,
                    "status": "rejected",
                    "reject_reason": verdict.reject_reason,
                }
            )
            db.add_log(
                f"Klip elendi ({streamer_display_name}): {verdict.reject_reason}",
                pipeline_id=pipeline_id,
                level="info",
                source="quality_judge",
            )
            return rejected

        rendered_path = await self.renderer.render_vertical_short(
            raw_path=raw_path,
            clip_id=clip_id,
            segments=verdict.segments,
            streamer_name=streamer_display_name,
            trim_start=verdict.trim_start_sec,
            trim_end=verdict.trim_end_sec,
        )
        raw_path.unlink(missing_ok=True)

        clip = db.insert_clip(
            {
                "id": clip_id,
                "pipeline_id": pipeline_id,
                "streamer_username": streamer_username,
                "streamer_display_name": streamer_display_name,
                "source_platform": source_platform,
                "source_type": source_type,
                "source_id": source_id,
                "source_url": source_url,
                "source_title": source_title,
                "duration_sec": verdict.duration_sec,
                "views_count": views_count,
                "rendered_path": str(rendered_path),
                "contact_sheet_path": verdict.contact_sheet_path,
                "transcript": verdict.transcript,
                "segments": verdict.segments,
                "word_count": verdict.word_count,
                "non_silent_ratio": verdict.non_silent_ratio,
                "gameplay_motion_score": verdict.gameplay_motion_score,
                "faces_detected": verdict.faces_detected,
                "viral_score": verdict.viral_score,
                "quality_reason": verdict.quality_reason,
                "status": "pending_review",
                "youtube_title": verdict.youtube_title,
                "youtube_description": verdict.youtube_description,
                "youtube_tags": verdict.youtube_tags,
                "instagram_caption": verdict.instagram_caption,
            }
        )

        settings = get_settings()
        db.enforce_queue_cap(pipeline_id, settings.max_queue_per_pipeline)
        clip = db.get_clip(clip_id) or clip
        if clip.get("status") == "rejected":
            return clip

        scheduled = db.compute_pipeline_schedule(
            pipeline_id,
            settings.max_daily_youtube_per_pipeline,
            settings.min_publish_interval_minutes,
            limit=settings.max_queue_per_pipeline + 2,
        )
        match = next((c for c in scheduled if c["id"] == clip_id), None)
        if match:
            clip["queue_rank"] = match["queue_rank"]
            clip["scheduled_for"] = match["scheduled_for"]

        autopilot_done = False
        if pipeline.get("autopilot") and verdict.viral_score >= settings.min_viral_score:
            not_backed_off = time.time() >= self._youtube_backoff_until.get(pipeline_id, 0.0)
            if settings.youtube_schedule_ahead and not_backed_off and match:
                autopilot_done = await self._try_autopublish_clip(
                    clip,
                    notify_telegram=False,
                    publish_at_iso=match["scheduled_for"],
                )
                clip = db.get_clip(clip_id) or clip
            else:
                pacing = db.get_publish_pacing(pipeline_id)
                within_daily_cap = pacing["published_last_24h"] < settings.max_daily_youtube_per_pipeline
                last_min = pacing["minutes_since_last_publish"]
                within_spacing = last_min is None or last_min >= settings.min_publish_interval_minutes

                if within_daily_cap and within_spacing and not_backed_off:
                    autopilot_done = await self._try_autopublish_clip(clip, notify_telegram=False)
                    clip = db.get_clip(clip_id) or clip
                else:
                    db.add_log(
                        f"Klip yayın sırasına eklendi ({pacing['published_last_24h']}/{settings.max_daily_youtube_per_pipeline} günlük)",
                        pipeline_id=pipeline_id,
                        source="autopilot",
                    )

        await self.telegram.send_clip_for_approval(clip, autopilot_published=autopilot_done)
        await self._notify_n8n(clip)
        return clip

    async def _notify_n8n(self, clip: dict[str, Any]) -> None:
        webhook_url = get_settings().n8n_webhook_url.strip()
        if not webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(webhook_url, json={"event": "clip_ready", "clip": clip})
        except Exception as exc:
            logger.warning("n8n webhook bildirim hatası: %s", exc)

    async def publish_clip(
        self,
        clip_id: str,
        targets: list[str],
        override_title: Optional[str] = None,
        override_description: Optional[str] = None,
        override_caption: Optional[str] = None,
        publish_at_iso: Optional[str] = None,
    ) -> dict[str, Any]:
        clip = db.get_clip(clip_id)
        if not clip:
            raise ValueError(f"Klip bulunamadı: {clip_id}")

        patch: dict[str, Any] = {}
        if override_title is not None:
            patch["youtube_title"] = override_title
        if override_description is not None:
            patch["youtube_description"] = override_description
        if override_caption is not None:
            patch["instagram_caption"] = override_caption
        if patch:
            clip = db.update_clip(clip_id, patch) or clip

        video_path = Path(clip["rendered_path"])
        results: dict[str, Any] = {"clip_id": clip_id, "youtube": None, "instagram": None, "errors": []}

        if "youtube" in targets:
            try:
                yt_id = await self.youtube_pub.upload_short(
                    pipeline_id=clip["pipeline_id"],
                    video_path=video_path,
                    title=clip["youtube_title"],
                    description=clip["youtube_description"],
                    tags=clip["youtube_tags"],
                    publish_at_iso=publish_at_iso,
                )
                results["youtube"] = yt_id
                db.update_clip(clip_id, {"youtube_video_id": yt_id})
            except Exception as exc:
                results["errors"].append(f"YouTube: {exc}")

        if "instagram" in targets:
            try:
                ig_id = await self.instagram_pub.publish_reel(
                    pipeline_id=clip["pipeline_id"],
                    video_path=video_path,
                    caption=clip["instagram_caption"],
                )
                results["instagram"] = ig_id
                db.update_clip(clip_id, {"instagram_media_id": ig_id})
            except Exception as exc:
                results["errors"].append(f"Instagram: {exc}")

        if results["youtube"] or results["instagram"]:
            update_fields: dict[str, Any] = {"status": "published"}
            log_prefix = "Yayınlandı"
            if publish_at_iso and results["youtube"]:
                try:
                    local_dt = datetime.fromisoformat(publish_at_iso.replace("Z", "+00:00")).astimezone()
                    log_prefix = f"YouTube Studio'da Planlandı ({local_dt.strftime('%d.%m %H:%M')})"
                    update_fields["quality_reason"] = f"📅 {log_prefix} · {clip.get('quality_reason', '')}"
                except ValueError:
                    pass
            db.update_clip(clip_id, update_fields)
            db.add_log(
                f"{log_prefix} ({clip['streamer_display_name']}): YT={results['youtube'] or '-'} | IG={results['instagram'] or '-'}",
                pipeline_id=clip["pipeline_id"],
                source="publisher",
            )
        elif results["errors"]:
            db.update_clip(clip_id, {"status": "failed", "reject_reason": " | ".join(results["errors"])})
            raise RuntimeError(" | ".join(results["errors"]))

        parts = []
        if results["youtube"]:
            parts.append(f"YT: https://youtube.com/shorts/{results['youtube']}")
        if results["instagram"]:
            parts.append(f"IG Media: {results['instagram']}")
        if results["errors"]:
            parts.append("Hatalar: " + "; ".join(results["errors"]))
        results["summary"] = " | ".join(parts)
        results["clip"] = db.get_clip(clip_id)
        return results

    async def reject_clip(self, clip_id: str, reason: str = "Manuel reddedildi") -> Optional[dict[str, Any]]:
        updated = db.update_clip(clip_id, {"status": "rejected", "reject_reason": reason})
        if updated:
            db.add_log(
                f"Klip reddedildi ({updated['streamer_display_name']}): {reason}",
                pipeline_id=updated["pipeline_id"],
                source="review",
            )
        return updated

    async def regenerate_clip_metadata(self, clip_id: str) -> Optional[dict[str, Any]]:
        clip = db.get_clip(clip_id)
        if not clip:
            return None
        pipeline = db.get_pipeline(clip["pipeline_id"]) or {"language": "tr"}
        new_meta = await self.judge.regenerate_metadata(clip, pipeline["language"])
        return db.update_clip(clip_id, new_meta)

    async def handle_kick_webhook(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.kick.handle_webhook_event(
            event_type=event_type,
            payload=payload,
            on_spike=self.live_watcher.handle_chat_spike,
        )
