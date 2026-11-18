from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class InstagramPublisher:
    """Publishes Reels to separate Global & Turkish Instagram accounts via Meta Graph API v21.0."""

    GRAPH_BASE = "https://graph.facebook.com/v21.0"

    def _credentials_for(self, pipeline_id: str) -> tuple[str, str]:
        s = get_settings()
        if pipeline_id == "tr":
            return s.instagram_tr_user_id.strip(), s.instagram_tr_access_token.strip()
        return s.instagram_global_user_id.strip(), s.instagram_global_access_token.strip()

    def is_configured(self, pipeline_id: str) -> bool:
        uid, token = self._credentials_for(pipeline_id)
        return bool(uid and token)

    async def _resolve_public_video_url(self, video_path: Path) -> str:
        s = get_settings()
        if s.public_base_url.strip():
            base = s.public_base_url.strip().rstrip("/")
            return f"{base}/media/rendered/{video_path.name}"

        if s.temp_public_upload_enabled:
            logger.info("PUBLIC_BASE_URL boş, Instagram için geçici dosya bağlantısı hazırlanıyor...")
            async with httpx.AsyncClient(timeout=120.0) as client:
                with video_path.open("rb") as fh:
                    resp = await client.post(
                        "https://litterbox.catbox.moe/resources/internals/api.php",
                        data={"reqtype": "fileupload", "time": "1h"},
                        files={"fileToUpload": (video_path.name, fh, "video/mp4")},
                    )
                    resp.raise_for_status()
                    url = resp.text.strip()
                    if url.startswith("http"):
                        return url
            raise RuntimeError("Geçici medya yükleme servisi geçerli URL döndürmedi.")

        raise RuntimeError(
            "Instagram Reels API herkese açık bir MP4 URL'si gerektirir. "
            "Ayarlardan PUBLIC_BASE_URL (ör. Cloudflare Tunnel) girin veya TEMP_PUBLIC_UPLOAD_ENABLED=true yapın."
        )

    async def publish_reel(
        self,
        pipeline_id: str,
        video_path: Path,
        caption: str,
    ) -> str:
        ig_user_id, access_token = self._credentials_for(pipeline_id)
        if not ig_user_id or not access_token:
            raise RuntimeError(
                f"{pipeline_id.upper()} Instagram Graph API ayarları eksik (User ID / Access Token)."
            )
        if not video_path.exists():
            raise FileNotFoundError(f"Reels videosu bulunamadı: {video_path}")

        video_url = await self._resolve_public_video_url(video_path)

        async with httpx.AsyncClient(timeout=45.0) as client:
            # 1. Create Reels media container
            create_resp = await client.post(
                f"{self.GRAPH_BASE}/{ig_user_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption[:2200],
                    "share_to_feed": "true",
                    "access_token": access_token,
                },
            )
            create_data = create_resp.json()
            if create_resp.status_code >= 400 or "id" not in create_data:
                raise RuntimeError(f"Instagram container hatası: {create_data}")
            container_id = str(create_data["id"])

            # 2. Poll processing status until FINISHED
            for _ in range(30):
                await asyncio.sleep(5.0)
                status_resp = await client.get(
                    f"{self.GRAPH_BASE}/{container_id}",
                    params={"fields": "status_code,status", "access_token": access_token},
                )
                status_data = status_resp.json()
                code = status_data.get("status_code")
                if code == "FINISHED":
                    break
                if code in {"ERROR", "EXPIRED"}:
                    raise RuntimeError(f"Instagram video işleme hatası: {status_data}")
            else:
                raise TimeoutError("Instagram Reels video işleme zaman aşımına uğradı.")

            # 3. Publish Reel
            pub_resp = await client.post(
                f"{self.GRAPH_BASE}/{ig_user_id}/media_publish",
                data={"creation_id": container_id, "access_token": access_token},
            )
            pub_data = pub_resp.json()
            if pub_resp.status_code >= 400 or "id" not in pub_data:
                raise RuntimeError(f"Instagram yayınlama hatası: {pub_data}")
            media_id = str(pub_data["id"])

        logger.info("[%s] Instagram Reels yayınlandı: media_id=%s", pipeline_id, media_id)
        return media_id
