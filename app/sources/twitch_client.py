from __future__ import annotations

import asyncio
import logging
import random
import re
import ssl
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

HYPE_TERMS = {
    "clip",
    "clip it",
    "oha",
    "wtf",
    "omg",
    "lmfao",
    "lmao",
    "lan",
    "bro",
    "yuh",
    "kekw",
    "lul",
    "crazy",
    "insane",
    "no way",
    "nasil",
    "nasıl",
}


class TwitchClient:
    """Official Twitch Helix API client with OAuth2 client-credentials flow."""

    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    API_BASE = "https://api.twitch.tv/helix"

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.twitch_client_id and s.twitch_client_secret)

    async def get_app_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        s = get_settings()
        if not s.twitch_client_id or not s.twitch_client_secret:
            return ""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.TOKEN_URL,
                params={
                    "client_id": s.twitch_client_id,
                    "client_secret": s.twitch_client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    async def _headers(self) -> dict[str, str]:
        s = get_settings()
        token = await self.get_app_token()
        return {
            "Client-Id": s.twitch_client_id,
            "Authorization": f"Bearer {token}",
        }

    async def get_live_streams(self, usernames: list[str]) -> dict[str, dict[str, Any]]:
        if not usernames or not self.is_configured():
            return {}
        headers = await self._headers()
        results: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i in range(0, len(usernames), 100):
                batch = usernames[i : i + 100]
                params = [("user_login", u.lower()) for u in batch]
                resp = await client.get(f"{self.API_BASE}/streams", headers=headers, params=params)
                if resp.status_code != 200:
                    logger.warning("Twitch streams API hatası (%s): %s", resp.status_code, resp.text[:160])
                    continue
                for stream in resp.json().get("data", []):
                    login = str(stream.get("user_login", "")).lower()
                    results[login] = {
                        "is_live": True,
                        "title": str(stream.get("title") or ""),
                        "game_name": str(stream.get("game_name") or ""),
                        "viewer_count": int(stream.get("viewer_count") or 0),
                        "started_at": str(stream.get("started_at") or ""),
                    }
        return results

    async def get_user_ids(self, usernames: list[str]) -> dict[str, str]:
        if not usernames or not self.is_configured():
            return {}
        headers = await self._headers()
        mapping: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i in range(0, len(usernames), 100):
                batch = usernames[i : i + 100]
                params = [("login", u.lower()) for u in batch]
                resp = await client.get(f"{self.API_BASE}/users", headers=headers, params=params)
                if resp.status_code != 200:
                    continue
                for user in resp.json().get("data", []):
                    mapping[str(user["login"]).lower()] = str(user["id"])
        return mapping

    async def get_trending_clips(
        self,
        usernames: list[str],
        hours_back: int = 24,
        max_per_streamer: int = 8,
        game_keywords: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent Twitch clips ordered by viral velocity (views / age_minutes)."""
        if not usernames or not self.is_configured():
            return []
        user_ids = await self.get_user_ids(usernames)
        if not user_ids:
            return []

        headers = await self._headers()
        started_at = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=20.0) as client:
            for login, uid in user_ids.items():
                resp = await client.get(
                    f"{self.API_BASE}/clips",
                    headers=headers,
                    params={
                        "broadcaster_id": uid,
                        "started_at": started_at,
                        "first": max_per_streamer,
                    },
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("data", []):
                    duration = float(item.get("duration") or 0.0)
                    if duration < 15.0 or duration > 120.0:
                        continue
                    title = str(item.get("title") or "")
                    created_str = str(item.get("created_at") or "")
                    try:
                        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        age_minutes = max((now - created_dt).total_seconds() / 60.0, 5.0)
                    except ValueError:
                        age_minutes = 60.0

                    views = int(item.get("view_count") or 0)
                    velocity = views / age_minutes
                    candidates.append(
                        {
                            "username": login,
                            "source_platform": "twitch",
                            "source_id": str(item["id"]),
                            "source_url": str(item["url"]),
                            "title": title,
                            "duration": duration,
                            "views_count": views,
                            "velocity": velocity,
                            "game_id": str(item.get("game_id") or ""),
                        }
                    )

        candidates.sort(key=lambda c: c["velocity"], reverse=True)
        return candidates


class TwitchChatSpikeMonitor:
    """Read-only anonymous Twitch IRC monitor detecting sudden chat hype spikes."""

    IRC_HOST = "irc.chat.twitch.tv"
    IRC_PORT = 6697
    PRIVMSG_RE = re.compile(r"^:[^!]+![^@]+@[^\s]+\s+PRIVMSG\s+#([^\s]+)\s+:(.*)$")

    def __init__(
        self,
        on_spike: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        self._on_spike = on_spike
        self._channels: set[str] = set()
        self._desired_channels: set[str] = set()
        self._msg_times: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=600))
        self._last_spike: dict[str, float] = {}
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    def start(self) -> None:
        if not self._task or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._writer:
            self._writer.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def sync_channels(self, usernames: list[str]) -> None:
        desired = {u.strip().lower() for u in usernames if u.strip()}
        self._desired_channels = desired
        if not self._writer:
            return
        to_join = desired - self._channels
        to_part = self._channels - desired
        for ch in to_join:
            self._writer.write(f"JOIN #{ch}\r\n".encode("utf-8"))
            self._channels.add(ch)
        for ch in to_part:
            self._writer.write(f"PART #{ch}\r\n".encode("utf-8"))
            self._channels.discard(ch)
        if to_join or to_part:
            await self._writer.drain()

    async def _run_loop(self) -> None:
        ssl_ctx = ssl.create_default_context()
        while self._running:
            if not self._desired_channels:
                await asyncio.sleep(5.0)
                continue
            try:
                reader, writer = await asyncio.open_connection(self.IRC_HOST, self.IRC_PORT, ssl=ssl_ctx)
                self._writer = writer
                self._channels.clear()
                nick = f"justinfan{random.randint(10000, 99999)}"
                writer.write(b"PASS SCHMOOPIIE\r\n")
                writer.write(f"NICK {nick}\r\n".encode("utf-8"))
                await writer.drain()
                await self.sync_channels(list(self._desired_channels))
                logger.info("Twitch IRC canlı chat dinleyicisi bağlandı (%d kanal).", len(self._channels))

                while self._running:
                    raw = await asyncio.wait_for(reader.readline(), timeout=120.0)
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("PING"):
                        writer.write(b"PONG :tmi.twitch.tv\r\n")
                        await writer.drain()
                        continue
                    match = self.PRIVMSG_RE.match(line)
                    if match:
                        channel = match.group(1).lower()
                        text = match.group(2)
                        await self._record_message(channel, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Twitch IRC bağlantısı yenileniyor: %s", exc)
                self._writer = None
                self._channels.clear()
                await asyncio.sleep(8.0)

    async def _record_message(self, channel: str, text: str) -> None:
        now = time.time()
        lower = text.lower()
        weight = 1.8 if any(term in lower for term in HYPE_TERMS) or "😂" in text or "💀" in text else 1.0
        history = self._msg_times[channel]
        history.append((now, weight))

        # Prune older than 180 seconds
        while history and now - history[0][0] > 180.0:
            history.popleft()

        recent_score = sum(w for ts, w in history if now - ts <= 10.0)
        recent_count = sum(1 for ts, _ in history if now - ts <= 10.0)
        baseline_items = [w for ts, w in history if 15.0 < (now - ts) <= 180.0]
        baseline_per_10s = (sum(baseline_items) / 16.5) if baseline_items else 1.5

        last_ts = self._last_spike.get(channel, 0.0)
        if recent_count >= 8 and recent_score >= max(10.0, baseline_per_10s * 3.5) and (now - last_ts) >= 90.0:
            self._last_spike[channel] = now
            reason = f"Twitch chat patlaması ({recent_count} mesaj/10sn, taban={baseline_per_10s:.1f})"
            logger.info("[%s] %s", channel, reason)
            asyncio.create_task(self._on_spike("twitch", channel, reason))
