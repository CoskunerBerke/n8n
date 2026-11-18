from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.sources import ytdlp_helper

logger = logging.getLogger(__name__)


class YouTubeSourceClient:
    """Checks YouTube Live streams and recent Shorts/clips via Data API or yt-dlp."""

    API_BASE = "https://www.googleapis.com/youtube/v3"

    async def check_live(self, channel_url: str) -> Optional[dict[str, Any]]:
        """Check whether a YouTube channel is live and return stream info."""
        return await ytdlp_helper.resolve_live_stream_url(channel_url)

    async def fetch_recent_shorts(
        self,
        channel_url: str,
        channel_id: Optional[str] = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        s = get_settings()
        if s.youtube_data_api_key and channel_id:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"{self.API_BASE}/search",
                        params={
                            "key": s.youtube_data_api_key,
                            "channelId": channel_id,
                            "part": "snippet",
                            "order": "date",
                            "type": "video",
                            "maxResults": limit,
                        },
                    )
                    if resp.status_code == 200:
                        items = resp.json().get("items", [])
                        return [
                            {
                                "source_id": item["id"]["videoId"],
                                "source_url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                                "title": item["snippet"]["title"],
                                "duration": 0.0,
                                "views_count": 0,
                            }
                            for item in items
                            if item.get("id", {}).get("videoId")
                        ]
            except Exception as exc:
                logger.warning("YouTube Data API arama hatası: %s", exc)

        return await ytdlp_helper.fetch_channel_recent_clips(channel_url, "youtube", limit=limit)
