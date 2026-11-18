from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

KICK_HYPE_TERMS = {
    "clip",
    "oha",
    "wtf",
    "omg",
    "lan",
    "yuh",
    "nasil",
    "nasıl",
    "gg",
    "insane",
    "crazy",
    "patladi",
    "patladı",
}


class KickClient:
    """Official Kick Public API v1 client (api.kick.com/public/v1) + webhook processor."""

    TOKEN_URL = "https://id.kick.com/oauth/token"
    API_BASE = "https://api.kick.com/public/v1"

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0
        self._chat_history: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=600))
        self._last_spike: dict[str, float] = {}

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.kick_client_id and s.kick_client_secret)

    async def get_app_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        s = get_settings()
        if not s.kick_client_id or not s.kick_client_secret:
            return ""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": s.kick_client_id,
                    "client_secret": s.kick_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    async def get_channels_status(self, slugs: list[str]) -> dict[str, dict[str, Any]]:
        """Query Kick's official Public v1 channels endpoint for live status."""
        if not slugs or not self.is_configured():
            return {}
        try:
            token = await self.get_app_token()
        except Exception as exc:
            logger.warning("Kick OAuth token alınamadı: %s", exc)
            return {}
        if not token:
            return {}

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        results: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Query in small batches to keep URL clean
            for i in range(0, len(slugs), 20):
                batch = slugs[i : i + 20]
                params = [("slug", s.lower()) for s in batch]
                resp = await client.get(f"{self.API_BASE}/channels", headers=headers, params=params)
                if resp.status_code != 200:
                    logger.warning("Kick API durum kodu %s: %s", resp.status_code, resp.text[:160])
                    continue
                for ch in resp.json().get("data", []):
                    slug = str(ch.get("slug") or "").lower()
                    stream = ch.get("stream") or {}
                    is_live = bool(stream.get("is_live"))
                    category = ch.get("category") or {}
                    results[slug] = {
                        "is_live": is_live,
                        "title": str(ch.get("stream_title") or stream.get("title") or ""),
                        "game_name": str(category.get("name") or ""),
                        "viewer_count": int(stream.get("viewer_count") or 0),
                    }
        return results

    async def handle_webhook_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        on_spike: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ) -> dict[str, Any]:
        """Process official Kick webhook payloads (chat.message.sent / livestream.status.updated)."""
        if event_type == "chat.message.sent":
            broadcaster = payload.get("broadcaster") or {}
            slug = str(broadcaster.get("channel_slug") or broadcaster.get("username") or "").lower()
            content = str(payload.get("content") or "")
            if not slug:
                return {"handled": False, "reason": "broadcaster missing"}

            now = time.time()
            lower = content.lower()
            weight = 1.8 if any(t in lower for t in KICK_HYPE_TERMS) or "😂" in content else 1.0
            history = self._chat_history[slug]
            history.append((now, weight))
            while history and now - history[0][0] > 180.0:
                history.popleft()

            recent_score = sum(w for ts, w in history if now - ts <= 10.0)
            recent_count = sum(1 for ts, _ in history if now - ts <= 10.0)
            baseline_items = [w for ts, w in history if 15.0 < (now - ts) <= 180.0]
            baseline_per_10s = (sum(baseline_items) / 16.5) if baseline_items else 1.5
            last_ts = self._last_spike.get(slug, 0.0)

            if (
                on_spike is not None
                and recent_count >= 8
                and recent_score >= max(10.0, baseline_per_10s * 3.5)
                and (now - last_ts) >= 90.0
            ):
                self._last_spike[slug] = now
                reason = f"Kick chat patlaması ({recent_count} mesaj/10sn)"
                asyncio.create_task(on_spike("kick", slug, reason))
                return {"handled": True, "spike_triggered": True, "channel": slug}
            return {"handled": True, "spike_triggered": False, "channel": slug}

        if event_type in {"livestream.status.updated", "livestream.metadata.updated"}:
            return {"handled": True, "event_type": event_type}

        return {"handled": False, "event_type": event_type}
