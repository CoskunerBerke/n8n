from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _ytdlp_cmd() -> list[str]:
    """Prefer yt-dlp inside the active Python environment, fallback to PATH."""
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        binary = shutil.which("yt-dlp") or "yt-dlp"
        return [binary]


async def _run_ytdlp_json(args: list[str], timeout_sec: float = 45.0) -> tuple[Optional[dict[str, Any]], str]:
    cmd = [*_ytdlp_cmd(), "--no-warnings", "--ignore-errors", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "yt-dlp zaman aşımına uğradı"

    out_text = stdout.decode("utf-8", errors="replace").strip()
    err_text = stderr.decode("utf-8", errors="replace").strip()
    if not out_text:
        return None, err_text or f"yt-dlp çıkış kodu: {proc.returncode}"

    for line in reversed(out_text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line), err_text
            except json.JSONDecodeError:
                continue
    return None, err_text or "yt-dlp JSON çıktısı ayrıştırılamadı"


def _pick_stream_url(info: dict[str, Any]) -> str:
    """Select an HLS/HTTP stream URL up to 1080p for live capture."""
    formats = info.get("formats") or []
    best_url = ""
    best_height = -1
    for fmt in formats:
        url = fmt.get("url") or ""
        if not url:
            continue
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        height = fmt.get("height") or 0
        if vcodec == "none":
            continue
        # Prefer combined audio+video HLS formats <= 1080p
        if height <= 1080 and acodec != "none" and height >= best_height:
            best_height = height
            best_url = url
    if best_url:
        return best_url
    return str(info.get("url") or info.get("manifest_url") or "")


async def resolve_live_stream_url(channel_url: str) -> Optional[dict[str, Any]]:
    """Check whether a channel URL is currently live and return its stream URL."""
    info, err = await _run_ytdlp_json(
        ["--dump-single-json", "--no-playlist", channel_url],
        timeout_sec=35.0,
    )
    if not info:
        if "403" in err or "Cloudflare" in err:
            logger.warning("Canlı yayın kontrolü erişim engeli (%s): %s", channel_url, err[:160])
        return None

    is_live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
    if not is_live:
        return None

    stream_url = _pick_stream_url(info)
    if not stream_url:
        return None

    return {
        "is_live": True,
        "stream_url": stream_url,
        "title": str(info.get("title") or info.get("fulltitle") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "category": str(info.get("categories", [""])[0] if isinstance(info.get("categories"), list) and info.get("categories") else ""),
    }


async def inspect_url_metadata(url: str) -> Optional[dict[str, Any]]:
    """Fetch metadata for a clip or video URL without downloading."""
    info, err = await _run_ytdlp_json(
        ["--dump-single-json", "--no-playlist", url],
        timeout_sec=35.0,
    )
    if not info:
        logger.warning("URL bilgisi alınamadı (%s): %s", url, err[:180])
        return None
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or "streamer"),
        "duration": float(info.get("duration") or 0.0),
        "view_count": int(info.get("view_count") or 0),
        "webpage_url": str(info.get("webpage_url") or url),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or "").lower(),
    }


def _build_clips_tab_url(channel_url: str, platform: str) -> str:
    clean = channel_url.rstrip("/")
    if platform == "kick":
        if clean.endswith("/clips"):
            return clean
        return f"{clean}/clips"
    if platform == "twitch":
        if "/clips" in clean:
            return clean
        return f"{clean}/clips?filter=clips&range=24hr"
    if platform == "youtube":
        if clean.endswith("/live"):
            clean = clean[:-5]
        if clean.endswith("/shorts") or clean.endswith("/videos"):
            return clean
        return f"{clean}/shorts"
    return clean


async def fetch_channel_recent_clips(
    channel_url: str,
    platform: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Fetch recent clip listings using yt-dlp flat-playlist when supported."""
    target_url = _build_clips_tab_url(channel_url, platform)
    info, err = await _run_ytdlp_json(
        ["--flat-playlist", "--playlist-end", str(limit), "--dump-single-json", target_url],
        timeout_sec=45.0,
    )
    if not info:
        if "403" in err or "Cloudflare" in err:
            logger.warning("Klip taraması erişim engeli (%s): %s", target_url, err[:160])
        return []

    entries = info.get("entries") or []
    results: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        clip_url = str(item.get("webpage_url") or item.get("url") or "")
        clip_id = str(item.get("id") or "")
        if not clip_url:
            continue
        if platform == "youtube" and not clip_url.startswith("http"):
            clip_url = f"https://www.youtube.com/watch?v={clip_url}"
        results.append(
            {
                "source_id": clip_id or clip_url,
                "source_url": clip_url,
                "title": str(item.get("title") or ""),
                "duration": float(item.get("duration") or 0.0),
                "views_count": int(item.get("view_count") or 0),
                "uploader": str(item.get("uploader") or item.get("channel") or ""),
            }
        )
    return results


async def download_clip_media(
    url: str,
    output_path: Path,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
) -> Path:
    """Download a clip or bounded VOD segment as MP4 using yt-dlp."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stem_template = str(output_path.with_suffix("")) + ".%(ext)s"

    cmd = [
        *_ytdlp_cmd(),
        "--no-warnings",
        "--no-playlist",
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format",
        "mp4",
        "-o",
        stem_template,
    ]
    if start_sec is not None and end_sec is not None and end_sec > start_sec:
        cmd.extend(["--download-sections", f"*{start_sec:.2f}-{end_sec:.2f}"])
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=240.0)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Video indirme zaman aşımı: {url}") from exc

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"yt-dlp indirme hatası: {err_msg[:260]}")

    if output_path.exists():
        return output_path

    # Handle case where yt-dlp kept another extension before merge fallback
    for candidate in output_path.parent.glob(f"{output_path.stem}.*"):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            if candidate != output_path:
                candidate.replace(output_path)
            return output_path

    raise FileNotFoundError(f"İndirilen video dosyası bulunamadı: {output_path}")
