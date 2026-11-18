from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from app import database as db
from app.config import get_settings

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Async Telegram Bot using long-polling with inline approval keyboards and phone commands."""

    API_ROOT = "https://api.telegram.org"

    def __init__(
        self,
        on_publish: Callable[[str, list[str]], Awaitable[dict[str, Any]]],
        on_reject: Callable[[str, str], Awaitable[Optional[dict[str, Any]]]],
        on_regen: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
        on_scan: Callable[[str], Awaitable[dict[str, Any]]],
        on_submit_url: Callable[[str, str], Awaitable[dict[str, Any]]],
    ) -> None:
        self._on_publish = on_publish
        self._on_reject = on_reject
        self._on_regen = on_regen
        self._on_scan = on_scan
        self._on_submit_url = on_submit_url
        self._offset = 0
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.telegram_bot_token.strip())

    def start(self) -> None:
        if not self._task or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _base_url(self) -> str:
        token = get_settings().telegram_bot_token.strip()
        return f"{self.API_ROOT}/bot{token}"

    async def send_text(self, text: str, chat_id: Optional[str] = None) -> None:
        s = get_settings()
        target = (chat_id or s.telegram_admin_chat_id).strip()
        if not s.telegram_bot_token.strip() or not target:
            return
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(
                f"{self._base_url()}/sendMessage",
                json={"chat_id": target, "text": text, "disable_web_page_preview": True},
            )

    def _format_clip_caption(self, clip: dict[str, Any]) -> str:
        flag = "🇹🇷 TÜRKÇE" if clip["pipeline_id"] == "tr" else "🌍 GLOBAL"
        src = "🔴 CANLI YAYIN" if clip["source_type"] == "live_spike" else "🔥 KLİP"
        tags = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in clip.get("youtube_tags", [])[:6])
        return (
            f"[{flag}] {src} | {clip['streamer_display_name']} ({clip['source_platform'].upper()})\n"
            f"Puan: {clip['viral_score']}/10 | Süre: {clip['duration_sec']:.0f}sn\n"
            f"Analiz: {clip['quality_reason']}\n\n"
            f"YT Başlık:\n{clip['youtube_title']}\n\n"
            f"IG Açıklama:\n{clip['instagram_caption'][:320]}\n\n"
            f"Etiketler: {tags}"
        )[:1010]

    async def send_clip_for_approval(self, clip: dict[str, Any], autopilot_published: bool = False) -> None:
        s = get_settings()
        chat_id = s.telegram_admin_chat_id.strip()
        if not s.telegram_bot_token.strip() or not chat_id:
            return

        rendered = Path(clip.get("rendered_path") or "")
        caption = self._format_clip_caption(clip)
        ig_configured = (
            bool(s.instagram_tr_user_id.strip() and s.instagram_tr_access_token.strip())
            if clip["pipeline_id"] == "tr"
            else bool(s.instagram_global_user_id.strip() and s.instagram_global_access_token.strip())
        )
        if autopilot_published:
            caption = (
                f"🚀 YOUTUBE SHORTS OTOPİLOTLA YAYINLANDI!\n"
                f"📱 Instagram için videoyu kaydedip aşağıdaki açıklamayı kopyalayabilirsin.\n\n{caption}"
            )[:1010]
            keyboard = {"inline_keyboard": []}
        else:
            cid = clip["id"]
            if clip.get("queue_rank"):
                sched_str = ""
                if clip.get("scheduled_for"):
                    try:
                        sched_str = " (" + datetime.fromisoformat(clip["scheduled_for"]).astimezone().strftime("%d.%m %H:%M") + ")"
                    except ValueError:
                        pass
                caption = (
                    f"📅 YOUTUBE YAYIN SIRASINA ALINDI! (#Sıra: {clip['queue_rank']}{sched_str})\n"
                    f"📱 Instagram için videoyu kaydedip açıklamayı kopyalayabilirsin.\n\n{caption}"
                )[:1010]
            if ig_configured:
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ YT + IG Paylaş", "callback_data": f"pub_both:{cid}"}],
                        [
                            {"text": "▶️ Sadece YT", "callback_data": f"pub_yt:{cid}"},
                            {"text": "📸 Sadece IG", "callback_data": f"pub_ig:{cid}"},
                        ],
                        [
                            {"text": "🔄 Başlık Yenile", "callback_data": f"regen:{cid}"},
                            {"text": "❌ Reddet", "callback_data": f"reject:{cid}"},
                        ],
                    ]
                }
            else:
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "▶️ YouTube'a Yayınla", "callback_data": f"pub_yt:{cid}"}],
                        [
                            {"text": "🔄 Başlık Yenile", "callback_data": f"regen:{cid}"},
                            {"text": "❌ Reddet", "callback_data": f"reject:{cid}"},
                        ],
                    ]
                }

        async with httpx.AsyncClient(timeout=90.0) as client:
            if rendered.exists():
                with rendered.open("rb") as fh:
                    resp = await client.post(
                        f"{self._base_url()}/sendVideo",
                        data={
                            "chat_id": chat_id,
                            "caption": caption,
                            "supports_streaming": "true",
                            "reply_markup": json.dumps(keyboard),
                        },
                        files={"video": (rendered.name, fh, "video/mp4")},
                    )
                if resp.status_code == 200:
                    msg_id = resp.json().get("result", {}).get("message_id", 0)
                    if msg_id:
                        db.update_clip(clip["id"], {"telegram_message_id": msg_id})
                    return

            await client.post(
                f"{self._base_url()}/sendMessage",
                json={"chat_id": chat_id, "text": caption, "reply_markup": keyboard},
            )

    async def _poll_loop(self) -> None:
        while self._running:
            s = get_settings()
            if not s.telegram_bot_token.strip():
                await asyncio.sleep(5.0)
                continue
            try:
                async with httpx.AsyncClient(timeout=40.0) as client:
                    resp = await client.get(
                        f"{self._base_url()}/getUpdates",
                        params={"offset": self._offset, "timeout": 25},
                    )
                    if resp.status_code != 200:
                        await asyncio.sleep(5.0)
                        continue
                    updates = resp.json().get("result", [])
                    for upd in updates:
                        self._offset = max(self._offset, upd["update_id"] + 1)
                        await self._handle_update(upd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling hatası: %s", exc)
                await asyncio.sleep(5.0)

    def _is_authorized_chat(self, chat_id: str) -> bool:
        configured = get_settings().telegram_admin_chat_id.strip()
        return not configured or configured == str(chat_id)

    async def _handle_update(self, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        message = upd.get("message")
        if not message or not message.get("text"):
            return
        chat_id = str(message["chat"]["id"])
        text = str(message["text"]).strip()

        if text == "/start" or text == "/yardim":
            help_msg = (
                "🎬 GTA 6 Auto-Clipper (TR + GLOBAL)\n"
                f"Chat ID'niz: {chat_id}\n\n"
                "Komutlar:\n"
                "/durum - Canlı yayınlar ve günlük istatistik\n"
                "/kuyruk - Sıradaki planlı YouTube yayın saatleri\n"
                "/tara tr | /tara global - Hemen klip taraması başlat\n"
                "/klip <tr|global> <url> - Linkten anında 9:16 klip üret\n"
                "/yayinci_ekle <tr|global> <kick|twitch|youtube> <isim>\n"
                "/autopilot <tr|global> <on|off> - Tam otomatik mod"
            )
            await self.send_text(help_msg, chat_id=chat_id)
            return

        if not self._is_authorized_chat(chat_id):
            await self.send_text(f"Yetkisiz erişim. Admin Chat ID ayarlanmalı (ID: {chat_id}).", chat_id=chat_id)
            return

        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/durum":
            stats = db.get_dashboard_stats()
            lines = ["📊 GTA 6 Clipper Durumu"]
            for p in stats["pipelines"]:
                st = stats["pipeline_stats"][p["id"]]
                mode = "OTOPİLOT AÇIK" if p["autopilot"] else "Onaylı Mod"
                lines.append(
                    f"\n[{p['id'].upper()}] {p['name']} ({mode})\n"
                    f"Canlı İzlenen: {st['live_now']} | Sırada: {st['pending_review']} | "
                    f"Yayınlanan: {st['published']} | Elenen: {st['rejected']}"
                )
            await self.send_text("\n".join(lines), chat_id=chat_id)

        elif cmd == "/kuyruk":
            s = get_settings()
            lines = [
                f"📅 YouTube Yayın Sırası (Günde {s.max_daily_youtube_per_pipeline} / "
                f"{s.min_publish_interval_minutes} dk ara)"
            ]
            for pid in ("tr", "global"):
                items = db.compute_pipeline_schedule(
                    pid,
                    s.max_daily_youtube_per_pipeline,
                    s.min_publish_interval_minutes,
                    limit=5,
                )
                lines.append(f"\n[{pid.upper()}] ({len(items)} sıradaki klip)")
                if not items:
                    lines.append("  Sırada bekleyen klip yok.")
                for item in items:
                    dt_str = datetime.fromisoformat(item["scheduled_for"]).astimezone().strftime("%d.%m %H:%M")
                    lines.append(
                        f"  #{item['queue_rank']} [{dt_str}] ({item['viral_score']}/10) {item['youtube_title'][:45]}"
                    )
            await self.send_text("\n".join(lines), chat_id=chat_id)

        elif cmd == "/tara" and len(parts) >= 2:
            pid = parts[1].lower()
            if pid not in {"tr", "global"}:
                await self.send_text("Kullanım: /tara tr veya /tara global", chat_id=chat_id)
                return
            await self.send_text(f"🔍 [{pid.upper()}] taraması başlatıldı...", chat_id=chat_id)
            asyncio.create_task(self._run_scan_command(pid, chat_id))

        elif cmd == "/klip" and len(parts) >= 3:
            pid = parts[1].lower()
            url = parts[2]
            if pid not in {"tr", "global"}:
                await self.send_text("Kullanım: /klip <tr|global> <url>", chat_id=chat_id)
                return
            await self.send_text(f"⏳ [{pid.upper()}] link indiriliyor ve kalite kontrolünden geçiyor...", chat_id=chat_id)
            asyncio.create_task(self._run_manual_clip(pid, url, chat_id))

        elif cmd == "/yayinci_ekle" and len(parts) >= 4:
            pid, platform, username = parts[1].lower(), parts[2].lower(), parts[3]
            if pid not in {"tr", "global"} or platform not in {"kick", "twitch", "youtube"}:
                await self.send_text("Kullanım: /yayinci_ekle <tr|global> <kick|twitch|youtube> <isim>", chat_id=chat_id)
                return
            item = db.add_streamer(pid, username, platform)
            await self.send_text(
                f"✅ Eklendi: [{pid.upper()}] {item['display_name']} ({item['platform']})",
                chat_id=chat_id,
            )

        elif cmd == "/autopilot" and len(parts) >= 3:
            pid = parts[1].lower()
            enabled = parts[2].lower() in {"on", "ac", "aç", "true", "1"}
            if pid not in {"tr", "global"}:
                await self.send_text("Kullanım: /autopilot <tr|global> <on|off>", chat_id=chat_id)
                return
            db.update_pipeline(pid, {"autopilot": enabled})
            durum = "AÇIK (otomatik paylaşım)" if enabled else "KAPALI (onay butonlu)"
            await self.send_text(f"⚙️ [{pid.upper()}] Otopilot: {durum}", chat_id=chat_id)

    async def _run_scan_command(self, pid: str, chat_id: str) -> None:
        try:
            res = await self._on_scan(pid)
            await self.send_text(
                f"✅ [{pid.upper()}] Tarama bitti: {res.get('processed', 0)} yeni klip işlendi, "
                f"{res.get('approved', 0)} onaylandı, {res.get('rejected', 0)} elendi.",
                chat_id=chat_id,
            )
        except Exception as exc:
            await self.send_text(f"❌ Tarama hatası: {exc}", chat_id=chat_id)

    async def _run_manual_clip(self, pid: str, url: str, chat_id: str) -> None:
        try:
            res = await self._on_submit_url(pid, url)
            if res.get("status") == "rejected":
                await self.send_text(f"🚫 Klip elendi: {res.get('reject_reason')}", chat_id=chat_id)
        except Exception as exc:
            await self.send_text(f"❌ Link işleme hatası: {exc}", chat_id=chat_id)

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        cq_id = cq["id"]
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        data = str(cq.get("data") or "")
        if not self._is_authorized_chat(chat_id) or ":" not in data:
            return
        action, clip_id = data.split(":", 1)

        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{self._base_url()}/answerCallbackQuery",
                json={"callback_query_id": cq_id, "text": "İşleniyor..."},
            )

        try:
            if action == "pub_both":
                res = await self._on_publish(clip_id, ["youtube", "instagram"])
                await self.send_text(f"✅ Yayınlandı ({clip_id}): {res.get('summary', 'Tamam')}", chat_id=chat_id)
            elif action == "pub_yt":
                res = await self._on_publish(clip_id, ["youtube"])
                await self.send_text(f"▶️ YouTube Shorts yayınlandı: {res.get('summary', 'Tamam')}", chat_id=chat_id)
            elif action == "pub_ig":
                res = await self._on_publish(clip_id, ["instagram"])
                await self.send_text(f"📸 Instagram Reels yayınlandı: {res.get('summary', 'Tamam')}", chat_id=chat_id)
            elif action == "regen":
                updated = await self._on_regen(clip_id)
                if updated:
                    await self.send_text(
                        f"🔄 Yeni Başlık ({clip_id}):\n{updated['youtube_title']}\n\n{updated['instagram_caption'][:280]}",
                        chat_id=chat_id,
                    )
            elif action == "reject":
                await self._on_reject(clip_id, "Telegram üzerinden manuel reddedildi")
                await self.send_text(f"❌ Klip reddedildi ({clip_id}).", chat_id=chat_id)
        except Exception as exc:
            await self.send_text(f"❌ İşlem hatası ({clip_id}): {exc}", chat_id=chat_id)
