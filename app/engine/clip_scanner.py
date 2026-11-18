from __future__ import annotations

import logging
from typing import Any

from app import database as db
from app.sources import ytdlp_helper
from app.sources.twitch_client import TwitchClient
from app.sources.youtube_source import YouTubeSourceClient

logger = logging.getLogger(__name__)


def _keyword_boost(title: str, keywords: list[str]) -> int:
    low = title.lower()
    for kw in keywords:
        if kw.lower() in low:
            return 1
    return 0


class ClipScanner:
    """Scans recent & trending clips across Kick, Twitch, and YouTube for a pipeline."""

    def __init__(self, twitch: TwitchClient, youtube_src: YouTubeSourceClient) -> None:
        self.twitch = twitch
        self.youtube_src = youtube_src

    async def scan_pipeline_candidates(self, pipeline_id: str, limit_per_source: int = 8) -> list[dict[str, Any]]:
        pipeline = db.get_pipeline(pipeline_id)
        if not pipeline or not pipeline["clips_enabled"]:
            return []

        streamers = [
            s
            for s in db.list_streamers(pipeline_id=pipeline_id, enabled_only=True)
            if s["clip_scan"]
        ]
        if not streamers:
            return []

        keywords = pipeline.get("game_keywords", [])
        raw_candidates: list[dict[str, Any]] = []
        streamer_map = {(s["platform"], s["username"]): s for s in streamers}

        # 1. Twitch official Helix clips (if configured)
        twitch_users = [s["username"] for s in streamers if s["platform"] == "twitch"]
        twitch_via_api = False
        if twitch_users and self.twitch.is_configured():
            try:
                api_clips = await self.twitch.get_trending_clips(
                    twitch_users, hours_back=24, max_per_streamer=limit_per_source, game_keywords=keywords
                )
                twitch_via_api = True
                for item in api_clips:
                    st = streamer_map.get(("twitch", item["username"]), {})
                    raw_candidates.append(
                        {
                            "pipeline_id": pipeline_id,
                            "streamer_username": item["username"],
                            "streamer_display_name": st.get("display_name", item["username"]),
                            "source_platform": "twitch",
                            "source_type": "trending_clip",
                            "source_id": item["source_id"],
                            "source_url": item["source_url"],
                            "title": item["title"],
                            "duration": item["duration"],
                            "views_count": item["views_count"],
                            "score_hint": item["velocity"] + (_keyword_boost(item["title"], keywords) * 50.0),
                        }
                    )
            except Exception as exc:
                logger.warning("[%s] Twitch Helix klip tarama hatası: %s", pipeline_id, exc)

        # 2. Fallback / Kick & YouTube clip scanning via yt-dlp
        for st in streamers:
            platform = st["platform"]
            if platform == "twitch" and twitch_via_api:
                continue
            try:
                if platform == "youtube":
                    items = await self.youtube_src.fetch_recent_shorts(st["channel_url"], limit=limit_per_source)
                else:
                    items = await ytdlp_helper.fetch_channel_recent_clips(
                        st["channel_url"], platform, limit=limit_per_source
                    )
                for item in items:
                    dur = float(item.get("duration") or 0.0)
                    if dur > 0 and (dur < 12.0 or dur > 180.0):
                        continue
                    views = int(item.get("views_count") or 0)
                    title = str(item.get("title") or "")
                    raw_candidates.append(
                        {
                            "pipeline_id": pipeline_id,
                            "streamer_username": st["username"],
                            "streamer_display_name": st["display_name"],
                            "source_platform": platform,
                            "source_type": "trending_clip",
                            "source_id": str(item["source_id"]),
                            "source_url": str(item["source_url"]),
                            "title": title,
                            "duration": dur,
                            "views_count": views,
                            "score_hint": float(views) + (_keyword_boost(title, keywords) * 500.0),
                        }
                    )
            except Exception as exc:
                logger.warning("[%s] %s (%s) tarama hatası: %s", pipeline_id, st["username"], platform, exc)

        # Deduplicate against SQLite and prioritize highest velocity/keyword match
        fresh: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        raw_candidates.sort(key=lambda x: x.get("score_hint", 0.0), reverse=True)
        for cand in raw_candidates:
            if cand["source_url"] in seen_urls:
                continue
            if db.clip_exists(
                pipeline_id,
                cand["source_platform"],
                cand["source_id"],
                cand["source_url"],
            ):
                continue
            seen_urls.add(cand["source_url"])
            fresh.append(cand)

        return fresh
