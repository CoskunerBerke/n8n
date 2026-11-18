from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.config import CREDENTIALS_DIR, get_settings, resolve_path

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class YouTubePublisher:
    """Manages separate OAuth2 channels and Shorts uploads for 'global' and 'tr'."""

    def __init__(self) -> None:
        self._pending_flows: dict[str, tuple[str, Flow]] = {}
        self._channel_cache: dict[str, tuple[float, str]] = {}

    def _token_path(self, pipeline_id: str) -> Path:
        filename = "youtube_tr_token.json" if pipeline_id == "tr" else "youtube_global_token.json"
        return CREDENTIALS_DIR / filename

    def _client_secrets_path(self, pipeline_id: str) -> Path:
        settings = get_settings()
        raw = (
            settings.youtube_tr_client_secrets
            if pipeline_id == "tr"
            else settings.youtube_global_client_secrets
        )
        return resolve_path(raw)

    def is_connected(self, pipeline_id: str) -> bool:
        return self._token_path(pipeline_id).exists()

    def get_channel_title(self, pipeline_id: str) -> str:
        """Return the authenticated YouTube channel title (cached per token mtime)."""
        token_path = self._token_path(pipeline_id)
        if not token_path.exists():
            self._channel_cache.pop(pipeline_id, None)
            return ""
        try:
            mtime = token_path.stat().st_mtime
            cached = self._channel_cache.get(pipeline_id)
            if cached and cached[0] == mtime:
                return cached[1]
            creds = self._load_credentials(pipeline_id)
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
            title = str(items[0]["snippet"]["title"]) if items else ""
            self._channel_cache[pipeline_id] = (mtime, title)
            return title
        except Exception as exc:
            logger.debug("YouTube kanal adı alınamadı (%s): %s", pipeline_id, exc)
            return ""

    def build_auth_url(self, pipeline_id: str, redirect_uri: str) -> str:
        secrets_path = self._client_secrets_path(pipeline_id)
        if not secrets_path.exists():
            raise FileNotFoundError(
                f"Google OAuth istemci dosyası bulunamadı: {secrets_path}. "
                "Google Cloud Console'dan indirdiğiniz client_secret JSON dosyasını credentials/ klasörüne koyun."
            )
        flow = Flow.from_client_secrets_file(
            str(secrets_path),
            scopes=YOUTUBE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self._pending_flows[state] = (pipeline_id, flow)
        return auth_url

    def complete_auth(self, state: str, authorization_response_url: str) -> str:
        if state not in self._pending_flows:
            raise ValueError("OAuth oturumu bulunamadı veya süresi doldu. Lütfen tekrar bağlayın.")
        pipeline_id, flow = self._pending_flows.pop(state)
        flow.fetch_token(authorization_response=authorization_response_url)
        creds = flow.credentials
        token_path = self._token_path(pipeline_id)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return pipeline_id

    def _load_credentials(self, pipeline_id: str) -> Credentials:
        token_path = self._token_path(pipeline_id)
        if not token_path.exists():
            raise RuntimeError(
                f"{pipeline_id.upper()} YouTube hesabı bağlı değil. Paneldeki Ayarlar > YouTube Bağla butonunu kullanın."
            )
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    async def upload_short(
        self,
        pipeline_id: str,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str = "public",
        publish_at_iso: Optional[str] = None,
    ) -> str:
        if not video_path.exists():
            raise FileNotFoundError(f"Yüklenecek video bulunamadı: {video_path}")

        safe_title = title.strip()[:98] or "GTA 6 Short"
        safe_desc = description.strip()
        if "#shorts" not in safe_title.lower() and "#shorts" not in safe_desc.lower():
            safe_desc = f"{safe_desc}\n\n#Shorts".strip()

        status_obj: dict[str, Any] = {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        }
        if publish_at_iso:
            try:
                pub_dt = datetime.fromisoformat(publish_at_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
                if (pub_dt - datetime.now(timezone.utc)).total_seconds() >= 900:
                    status_obj = {
                        "privacyStatus": "private",
                        "publishAt": pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "selfDeclaredMadeForKids": False,
                    }
            except ValueError:
                pass

        def _upload_sync() -> str:
            creds = self._load_credentials(pipeline_id)
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            body: dict[str, Any] = {
                "snippet": {
                    "title": safe_title,
                    "description": safe_desc,
                    "tags": [t.lstrip("#") for t in tags[:15]],
                    "categoryId": "20",  # Gaming
                },
                "status": status_obj,
            }
            media = MediaFileUpload(str(video_path), chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                _, response = request.next_chunk()
            return str(response["id"])

        video_id = await asyncio.to_thread(_upload_sync)
        logger.info("[%s] YouTube Shorts yüklendi: https://youtube.com/shorts/%s", pipeline_id, video_id)
        return video_id
