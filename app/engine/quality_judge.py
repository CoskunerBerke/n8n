from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import cv2
import httpx
import numpy as np
from PIL import Image, ImageDraw

from app.config import get_settings

logger = logging.getLogger(__name__)

_WHISPER_MODEL_CACHE: dict[str, Any] = {}


@dataclass
class QualityVerdict:
    approved: bool
    reject_reason: str = ""
    viral_score: float = 0.0
    quality_reason: str = ""
    duration_sec: float = 0.0
    non_silent_ratio: float = 0.0
    word_count: int = 0
    gameplay_motion_score: float = 0.0
    faces_detected: int = 0
    transcript: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    trim_start_sec: float = 0.0
    trim_end_sec: float = 0.0
    contact_sheet_path: str = ""
    youtube_title: str = ""
    youtube_description: str = ""
    youtube_tags: list[str] = field(default_factory=list)
    instagram_caption: str = ""


def _get_whisper_model(model_name: str) -> Any:
    if model_name not in _WHISPER_MODEL_CACHE:
        from faster_whisper import WhisperModel

        logger.info("Whisper modeli yükleniyor (%s)...", model_name)
        _WHISPER_MODEL_CACHE[model_name] = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
        )
    return _WHISPER_MODEL_CACHE[model_name]


async def _probe_media(video_path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_streams",
        "-of",
        "json",
        str(video_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0.0)
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": duration,
        "has_video": has_video,
        "has_audio": has_audio,
        "width": int(v_stream.get("width") or 0),
        "height": int(v_stream.get("height") or 0),
    }


async def _measure_non_silent_ratio(video_path: Path, duration: float) -> float:
    if duration <= 0:
        return 0.0
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video_path),
        "-af",
        "silencedetect=noise=-35dB:d=1.2",
        "-f",
        "null",
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=45.0)
    text = stderr.decode("utf-8", errors="replace")
    silence_durations = [float(x) for x in re.findall(r"silence_duration:\s*([0-9.]+)", text)]

    # Check if silence runs all the way to the end without closing silence_duration
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    silent_total = sum(silence_durations)
    if len(starts) > len(ends):
        silent_total += max(0.0, duration - starts[-1])

    non_silent = max(0.0, min(1.0, 1.0 - (silent_total / max(duration, 1.0))))
    return non_silent


def _transcribe_audio_sync(audio_path: Path, language: str, model_name: str) -> tuple[str, list[dict[str, Any]], int]:
    model = _get_whisper_model(model_name)
    lang_code = "tr" if language == "tr" else "en"
    raw_segments, _ = model.transcribe(
        str(audio_path),
        language=lang_code,
        vad_filter=True,
        beam_size=1,
    )
    segments: list[dict[str, Any]] = []
    texts: list[str] = []
    word_count = 0
    for seg in raw_segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        word_count += len(text.split())
        texts.append(text)
        segments.append(
            {
                "start": round(float(seg.start), 2),
                "end": round(float(seg.end), 2),
                "text": text,
            }
        )
    return " ".join(texts).strip(), segments, word_count


async def _extract_audio_wav(video_path: Path, wav_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(wav_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=45.0)


def _load_haar_cascade(xml_path: Path) -> Optional[Any]:
    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    if not cascade_cls or not xml_path.exists():
        return None
    try:
        # Load XML text in Python and parse via FileStorage memory mode so
        # non-ASCII Windows paths (e.g. Masaüstü) do not break OpenCV C++ fopen.
        xml_text = xml_path.read_text(encoding="utf-8")
        fs = cv2.FileStorage(xml_text, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        try:
            cascade = cascade_cls()
            if cascade.read(fs.getFirstTopLevelNode()) and not cascade.empty():
                return cascade
        finally:
            fs.release()
    except Exception as exc:
        logger.warning("Haar cascade yüklenemedi (%s): %s", xml_path.name, exc)
    return None


def _analyze_visuals_sync(
    video_path: Path,
    duration: float,
    contact_sheet_out: Path,
) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return {
            "gameplay_motion_score": 0.0,
            "faces_detected": 0,
            "empty_chair_suspected": True,
            "contact_sheet_path": "",
        }

    cascades_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
    frontal_cascade = _load_haar_cascade(Path(cascades_dir) / "haarcascade_frontalface_default.xml") if cascades_dir else None
    profile_cascade = _load_haar_cascade(Path(cascades_dir) / "haarcascade_profileface.xml") if cascades_dir else None

    sample_count = 10
    timestamps = np.linspace(0.5, max(duration - 0.5, 1.0), num=sample_count)
    frames: list[tuple[float, np.ndarray]] = []
    grays_small: list[np.ndarray] = []
    faces_detected = 0
    ad_slate_frames = 0

    for ts in timestamps:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(ts) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frames.append((float(ts), frame))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_small = cv2.resize(gray, (320, 180))
        grays_small.append(gray_small)

        small_bgr = cv2.resize(frame, (320, 180))
        hsv_small = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2HSV)
        purple_cyan_ratio = float(
            np.mean((hsv_small[:, :, 0] >= 80) & (hsv_small[:, :, 0] <= 168) & (hsv_small[:, :, 1] >= 70))
        )
        # Real Twitch commercial slates have smooth blur corners with zero HUD/webcam texture
        corners_lap = [
            float(cv2.Laplacian(gray_small[:40, :64], cv2.CV_64F).var()),
            float(cv2.Laplacian(gray_small[:40, -64:], cv2.CV_64F).var()),
            float(cv2.Laplacian(gray_small[-40:, :64], cv2.CV_64F).var()),
            float(cv2.Laplacian(gray_small[-40:, -64:], cv2.CV_64F).var()),
        ]
        if purple_cyan_ratio >= 0.94 and float(np.mean(corners_lap)) < 25.0:
            ad_slate_frames += 1

        if frontal_cascade is not None or profile_cascade is not None:
            detect_gray = cv2.resize(gray, (640, 360))
            f_rects = (
                frontal_cascade.detectMultiScale(
                    detect_gray, scaleFactor=1.15, minNeighbors=4, minSize=(28, 28)
                )
                if frontal_cascade is not None and not frontal_cascade.empty()
                else ()
            )
            p_rects = (
                profile_cascade.detectMultiScale(
                    detect_gray, scaleFactor=1.15, minNeighbors=4, minSize=(28, 28)
                )
                if profile_cascade is not None and not profile_cascade.empty() and len(f_rects) == 0
                else ()
            )
            if len(f_rects) > 0 or len(p_rects) > 0:
                faces_detected += 1

    cap.release()

    if len(frames) < 2:
        return {
            "gameplay_motion_score": 0.0,
            "faces_detected": 0,
            "empty_chair_suspected": True,
            "contact_sheet_path": "",
        }

    # Global frame-to-frame motion
    diffs = [
        float(np.mean(cv2.absdiff(grays_small[i], grays_small[i - 1])))
        for i in range(1, len(grays_small))
    ]
    gameplay_motion = float(np.mean(diffs)) if diffs else 0.0

    # Corner webcam ROI static check (detects streamer leaving chair/webcam empty)
    h, w = grays_small[0].shape
    cw, ch = int(w * 0.28), int(h * 0.32)
    corners = [
        (0, 0, cw, ch),
        (w - cw, 0, w, ch),
        (0, h - ch, cw, h),
        (w - cw, h - ch, w, h),
    ]
    static_corner_detected = False
    for x1, y1, x2, y2 in corners:
        c_diffs = [
            float(np.mean(cv2.absdiff(grays_small[i][y1:y2, x1:x2], grays_small[i - 1][y1:y2, x1:x2])))
            for i in range(1, len(grays_small))
        ]
        # Detect non-blank corner (webcam room) that has near-zero internal movement
        c_std = float(np.std(grays_small[0][y1:y2, x1:x2]))
        if c_std > 12.0 and c_diffs and float(np.mean(c_diffs)) < 1.25:
            static_corner_detected = True

    empty_chair_suspected = (faces_detected == 0 and static_corner_detected and gameplay_motion < 6.0)

    # Build 3x2 annotated contact sheet (960x540) for multimodal AI & UI review
    contact_sheet_out.parent.mkdir(parents=True, exist_ok=True)
    sheet = Image.new("RGB", (960, 540), (14, 14, 18))
    draw = ImageDraw.Draw(sheet)
    pick_indices = np.linspace(0, len(frames) - 1, num=min(6, len(frames)), dtype=int)
    for slot, idx in enumerate(pick_indices):
        ts, bgr = frames[int(idx)]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        thumb = Image.fromarray(rgb).resize((320, 180))
        gx = (slot % 3) * 320
        gy = (slot // 3) * 180
        sheet.paste(thumb, (gx, gy))
        label = f"#{slot + 1} {int(ts // 60):02d}:{int(ts % 60):02d}"
        draw.rectangle([gx + 8, gy + 8, gx + 104, gy + 28], fill=(0, 0, 0))
        draw.text((gx + 14, gy + 12), label, fill=(255, 255, 255))

    sheet.save(str(contact_sheet_out), quality=88)

    return {
        "gameplay_motion_score": round(gameplay_motion, 2),
        "faces_detected": faces_detected,
        "empty_chair_suspected": empty_chair_suspected,
        "ad_break_detected": (ad_slate_frames >= 4 and faces_detected <= 1 and gameplay_motion < 12.0),
        "contact_sheet_path": str(contact_sheet_out),
    }


def _select_best_trim_window(
    duration: float,
    segments: list[dict[str, Any]],
    min_sec: int,
    max_sec: int,
) -> tuple[float, float]:
    target = float(min(max_sec, max(min_sec, 38)))
    if duration <= max_sec:
        return 0.0, round(duration, 2)
    if not segments:
        start = max(0.0, (duration - target) / 2.0)
        return round(start, 2), round(min(duration, start + target), 2)

    best_start = 0.0
    best_words = -1
    step = 2.0
    cursor = 0.0
    while cursor + min_sec <= duration:
        win_end = min(duration, cursor + target)
        words = sum(
            len(s["text"].split())
            for s in segments
            if s["end"] >= cursor and s["start"] <= win_end
        )
        if words > best_words:
            best_words = words
            best_start = cursor
        cursor += step

    return round(best_start, 2), round(min(duration, best_start + target), 2)


def _clean_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _sanitize_source_title(raw: str) -> str:
    """Remove internal spike notes, chat !commands, URLs, and extra punctuation."""
    text = str(raw or "")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?:^|\s)![A-Za-z0-9_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" |•-:\t\r\n")
    return text


def _build_template_metadata(
    language: str,
    streamer_name: str,
    source_title: str,
    transcript: str,
    viral_score: float,
) -> dict[str, Any]:
    snippet = ""
    if transcript:
        words = transcript.split()[:7]
        snippet = " ".join(words).strip(".,!?")
    clean_title = _sanitize_source_title(source_title) or snippet

    if language == "tr":
        yt_title = (
            f"{streamer_name} GTA 6: {clean_title}"
            if clean_title
            else f"{streamer_name} GTA 6 Yayınında Efsane An!"
        )[:70]
        yt_desc = (
            f"{streamer_name} GTA 6 yayınından öne çıkan kesit.\n\n"
            f"Orijinal başlık: {clean_title or 'Canlı yayın kesiti'}\n"
            f"Daha fazla Türkçe GTA 6 kesiti için abone olmayı unutma!\n\n"
            f"#gta6 #gta6türkçe #shorts #{streamer_name.lower().replace(' ', '')}"
        )
        tags = [
            "gta 6",
            "gta 6 türkçe",
            "gta 6 clips",
            streamer_name.lower(),
            "kick türkiye",
            "twitch türkiye",
            "gta 6 oynanış",
            "shorts",
        ]
        ig_caption = (
            f"{yt_title}\n\n"
            f"Sence bu sahne 10 üzerinden kaç? Yorumlara yaz 👇\n\n"
            f"#gta6 #gta6turkiye #{streamer_name.lower().replace(' ', '')} #kickturkiye #reels #gaming"
        )
    else:
        yt_title = (
            f"{streamer_name} GTA 6: {clean_title}"
            if clean_title
            else f"{streamer_name} Insane GTA 6 Moment!"
        )[:70]
        yt_desc = (
            f"Highlight clip from {streamer_name} playing GTA 6.\n\n"
            f"Subscribe for daily GTA 6 viral moments & live reactions!\n\n"
            f"#gta6 #gta6clips #shorts #{streamer_name.lower().replace(' ', '')}"
        )
        tags = [
            "gta 6",
            "gta vi",
            "gta 6 clips",
            streamer_name.lower(),
            "gta 6 livestream",
            "kick clips",
            "twitch clips",
            "shorts",
        ]
        ig_caption = (
            f"{yt_title}\n\n"
            f"Rate this GTA 6 moment 1-10 👇\n\n"
            f"#gta6 #gtavi #gta6clips #{streamer_name.lower().replace(' ', '')} #gaming #reels"
        )

    return {
        "youtube_title": yt_title,
        "youtube_description": yt_desc,
        "youtube_tags": tags,
        "instagram_caption": ig_caption,
        "viral_score": viral_score,
    }


def _build_ai_prompt(
    language: str,
    streamer_name: str,
    source_title: str,
    duration: float,
    transcript: str,
    min_sec: int,
    max_sec: int,
) -> str:
    target_lang = "Turkish (natural Turkish creator/viewer slang, engaging, concise)" if language == "tr" else "English (punchy Shorts/Reels hook, engaging)"
    strict_rule = (
        "0. STRICT GAME CHECK: This channel is ONLY for Grand Theft Auto VI (GTA 6). "
        "Inspect the 6 frames, HUD, minimap, graphics, title, and transcript. "
        "If this is ANY OTHER GAME (including GTA 5 / GTA Online / FiveM, RDR2, CS2, Valorant, Minecraft, indie games) "
        "or Just Chatting/IRL where GTA 6 gameplay/trailer is NOT on screen, you MUST set approved=false "
        "and reject_reason='GTA 6 dışı oyun/içerik elendi'.\n"
        if get_settings().strict_gta6_only
        else ""
    )
    clean_title = _sanitize_source_title(source_title)
    return (
        f"You are an expert GTA 6 Shorts/Reels video editor and viral clipper.\n"
        f"Inspect the 3x2 contact sheet frames (chronological #1 to #6) and the transcript below.\n"
        f"Streamer: {streamer_name}\n"
        f"Original Clip Title: {clean_title or 'N/A'}\n"
        f"Duration: {duration:.1f}s\n"
        f"Transcript:\n{transcript[:1600] or '[No speech text]'}\n\n"
        f"STRICT REJECTION RULES (set approved=false if ANY applies):\n"
        f"{strict_rule}"
        f"1. Streamer went AFK / bathroom break / empty chair with webcam showing nobody.\n"
        f"2. Boring silent menu/loading/pause/BRB screen with no action or funny/hype reaction.\n"
        f"3. Completely pointless filler where nothing interesting happens.\n"
        f"4. Stream shows 'Commercial break in progress' or ad-break placeholder screen.\n\n"
        f"OUTPUT LANGUAGE FOR METADATA: {target_lang}\n"
        f"Return ONLY valid JSON with exact keys:\n"
        f"{{\n"
        f'  "approved": true/false,\n'
        f'  "reject_reason": "short reason in Turkish if rejected, else empty",\n'
        f'  "viral_score": 0.0 to 10.0,\n'
        f'  "trim_start_sec": number (>= 0),\n'
        f'  "trim_end_sec": number (aim for {min_sec}-{max_sec}s window if clip is longer),\n'
        f'  "youtube_title": "catchy <= 68 chars title (no clickbait spam)",\n'
        f'  "youtube_description": "2-3 clean lines + 3-5 hashtags",\n'
        f'  "youtube_tags": ["tag1", "tag2", "... up to 12 tags"],\n'
        f'  "instagram_caption": "engaging 1-2 line hook + CTA + 6-10 hashtags"\n'
        f"}}"
    )


async def _evaluate_gemini(prompt: str, contact_sheet: Path, model_name: str, api_key: str) -> dict[str, Any]:
    candidates: list[str] = []
    for candidate in (
        model_name,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    ):
        clean = (candidate or "").strip()
        if clean and clean not in candidates:
            candidates.append(clean)

    parts: list[dict[str, Any]] = [{"text": prompt}]
    if contact_sheet.exists():
        b64 = base64.b64encode(contact_sheet.read_bytes()).decode("ascii")
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})

    last_err = ""
    async with httpx.AsyncClient(timeout=18.0) as client:
        for model in candidates:
            gen_cfg: dict[str, Any] = {
                "responseMimeType": "application/json",
                "temperature": 0.4,
            }
            if model.startswith("gemini-3.") and "lite" not in model:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            try:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json={"contents": [{"parts": parts}], "generationConfig": gen_cfg},
                )
                if resp.status_code != 200:
                    last_err = f"{model} HTTP {resp.status_code}: {resp.text[:160]}"
                    logger.warning("Gemini model denemesi başarısız (%s)", last_err)
                    continue
                payload = resp.json()
                cands = payload.get("candidates") or []
                resp_parts = cands[0].get("content", {}).get("parts", []) if cands else []
                raw = "".join(str(p.get("text") or "") for p in resp_parts).strip()
                if not raw:
                    last_err = f"{model} boş yanıt döndürdü"
                    continue
                return _clean_json_object(raw)
            except Exception as exc:
                last_err = f"{model}: {exc}"
                logger.warning("Gemini model isteği başarısız (%s)", last_err)
                continue

    raise RuntimeError(f"Tüm Gemini modelleri başarısız oldu: {last_err}")


async def _evaluate_openai(prompt: str, contact_sheet: Path, model_name: str, api_key: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if contact_sheet.exists():
        b64 = base64.b64encode(contact_sheet.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
            }
        )
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"},
                "temperature": 0.4,
            },
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    return _clean_json_object(text)


class ClipQualityJudge:
    """3-Layer Smart Quality Gate: Audio/Speech -> Visual Anti-AFK -> Multimodal AI Director."""

    async def evaluate(
        self,
        video_path: Path,
        clip_id: str,
        language: str,
        streamer_name: str,
        source_title: str = "",
    ) -> QualityVerdict:
        settings = get_settings()

        # --- Layer 0: Probe Media ---
        probe = await _probe_media(video_path)
        duration = probe["duration"]
        if not probe["has_video"]:
            return QualityVerdict(approved=False, reject_reason="Video akışı bozuk veya bulunamadı")
        if duration < 10.0:
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                reject_reason=f"Klip çok kısa ({duration:.1f}sn < 10sn)",
            )
        if not probe["has_audio"]:
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                reject_reason="Klipte ses kanalı bulunamadı",
            )

        # --- Layer 1: Audio & Silence Detection ---
        non_silent_ratio = await _measure_non_silent_ratio(video_path, duration)
        if non_silent_ratio < settings.min_non_silent_ratio:
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                non_silent_ratio=round(non_silent_ratio, 3),
                reject_reason=f"Sessiz kesit elendi (aktif ses oranı %{int(non_silent_ratio * 100)})",
            )

        # --- Layer 1B: Speech Transcription via faster-whisper ---
        transcript = ""
        segments: list[dict[str, Any]] = []
        word_count = 0
        if settings.whisper_enabled:
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    wav_path = Path(tmp_dir) / "audio.wav"
                    await _extract_audio_wav(video_path, wav_path)
                    transcript, segments, word_count = await asyncio.to_thread(
                        _transcribe_audio_sync,
                        wav_path,
                        language,
                        settings.whisper_model,
                    )
            except Exception as exc:
                logger.warning("Whisper çözümleme atlandı (%s): %s", clip_id, exc)
                # Fallback estimate from non-silent ratio when offline/model unavailable
                word_count = int(non_silent_ratio * duration * 1.5)
        else:
            word_count = int(non_silent_ratio * duration * 1.5)

        if word_count < settings.min_speech_words:
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                non_silent_ratio=round(non_silent_ratio, 3),
                word_count=word_count,
                transcript=transcript,
                segments=segments,
                reject_reason=f"Yayıncı konuşmuyor ({word_count} kelime < {settings.min_speech_words})",
            )

        # --- Layer 2: Visual Motion & Empty-Chair Anti-AFK Gate ---
        contact_sheet_path = settings.rendered_dir / f"{clip_id}_sheet.jpg"
        visual = await asyncio.to_thread(
            _analyze_visuals_sync,
            video_path,
            duration,
            contact_sheet_path,
        )
        motion = visual["gameplay_motion_score"]
        faces = visual["faces_detected"]

        if motion < 2.0:
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                non_silent_ratio=round(non_silent_ratio, 3),
                word_count=word_count,
                gameplay_motion_score=motion,
                faces_detected=faces,
                transcript=transcript,
                segments=segments,
                contact_sheet_path=visual["contact_sheet_path"],
                reject_reason=f"Sabit ekran / BRB / Duraklatılmış yayın (hareket={motion})",
            )

        if visual.get("ad_break_detected"):
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                non_silent_ratio=round(non_silent_ratio, 3),
                word_count=word_count,
                gameplay_motion_score=motion,
                faces_detected=faces,
                transcript=transcript,
                segments=segments,
                contact_sheet_path=visual["contact_sheet_path"],
                reject_reason="Twitch reklam arası ekranı (Commercial break) elendi",
            )

        if visual["empty_chair_suspected"] or (motion < 4.5 and faces == 0 and word_count < 14):
            return QualityVerdict(
                approved=False,
                duration_sec=duration,
                non_silent_ratio=round(non_silent_ratio, 3),
                word_count=word_count,
                gameplay_motion_score=motion,
                faces_detected=faces,
                transcript=transcript,
                segments=segments,
                contact_sheet_path=visual["contact_sheet_path"],
                reject_reason="Yayıncı ekranda yok / Koltuk boş veya AFK tespit edildi",
            )

        # --- Optimal 28-45s Trim Window ---
        trim_start, trim_end = _select_best_trim_window(
            duration,
            segments,
            settings.clip_min_seconds,
            settings.clip_max_seconds,
        )

        # --- Layer 3: Multimodal AI Director & Localized Metadata ---
        heuristic_score = round(
            min(
                9.8,
                4.8
                + min(2.2, non_silent_ratio * 2.5)
                + min(1.5, word_count / 35.0)
                + (0.8 if faces > 0 else 0.2)
                + min(1.0, motion / 18.0),
            ),
            1,
        )
        ai_data: Optional[dict[str, Any]] = None
        prompt = _build_ai_prompt(
            language=language,
            streamer_name=streamer_name,
            source_title=source_title,
            duration=duration,
            transcript=transcript,
            min_sec=settings.clip_min_seconds,
            max_sec=settings.clip_max_seconds,
        )

        try:
            if settings.ai_provider == "openai" and settings.openai_api_key:
                ai_data = await _evaluate_openai(
                    prompt, contact_sheet_path, settings.openai_model, settings.openai_api_key
                )
            elif settings.gemini_api_key:
                ai_data = await _evaluate_gemini(
                    prompt, contact_sheet_path, settings.gemini_model, settings.gemini_api_key
                )
            elif settings.openai_api_key:
                ai_data = await _evaluate_openai(
                    prompt, contact_sheet_path, settings.openai_model, settings.openai_api_key
                )
        except Exception as exc:
            logger.warning("AI değerlendirmesi hatası (%s), sezgisel moda geçiliyor: %s", clip_id, exc)
            ai_data = None

        if ai_data is not None:
            approved = bool(ai_data.get("approved", True))
            viral_score = float(ai_data.get("viral_score", heuristic_score))
            reject_reason = str(ai_data.get("reject_reason") or "").strip()
            if not approved:
                return QualityVerdict(
                    approved=False,
                    reject_reason=reject_reason or "AI editör klibi eğlenceli/uygun bulmadı",
                    viral_score=viral_score,
                    duration_sec=duration,
                    non_silent_ratio=round(non_silent_ratio, 3),
                    word_count=word_count,
                    gameplay_motion_score=motion,
                    faces_detected=faces,
                    transcript=transcript,
                    segments=segments,
                    contact_sheet_path=visual["contact_sheet_path"],
                )
            if viral_score < settings.min_viral_score:
                return QualityVerdict(
                    approved=False,
                    reject_reason=f"Viral puanı yetersiz ({viral_score:.1f} < {settings.min_viral_score:.1f})",
                    viral_score=viral_score,
                    duration_sec=duration,
                    non_silent_ratio=round(non_silent_ratio, 3),
                    word_count=word_count,
                    gameplay_motion_score=motion,
                    faces_detected=faces,
                    transcript=transcript,
                    segments=segments,
                    contact_sheet_path=visual["contact_sheet_path"],
                )

            ai_start = float(ai_data.get("trim_start_sec", trim_start))
            ai_end = float(ai_data.get("trim_end_sec", trim_end))
            if 0.0 <= ai_start < ai_end <= duration and (ai_end - ai_start) >= 15.0:
                trim_start, trim_end = round(ai_start, 2), round(ai_end, 2)

            fallback_meta = _build_template_metadata(
                language, streamer_name, source_title, transcript, viral_score
            )
            return QualityVerdict(
                approved=True,
                viral_score=round(viral_score, 1),
                quality_reason=f"Konuşma: {word_count} kelime | Ses: %{int(non_silent_ratio*100)} | Yüz: {faces} | Hareket: {motion}",
                duration_sec=round(trim_end - trim_start, 2),
                non_silent_ratio=round(non_silent_ratio, 3),
                word_count=word_count,
                gameplay_motion_score=motion,
                faces_detected=faces,
                transcript=transcript,
                segments=segments,
                trim_start_sec=trim_start,
                trim_end_sec=trim_end,
                contact_sheet_path=visual["contact_sheet_path"],
                youtube_title=str(ai_data.get("youtube_title") or fallback_meta["youtube_title"])[:95],
                youtube_description=str(ai_data.get("youtube_description") or fallback_meta["youtube_description"]),
                youtube_tags=list(ai_data.get("youtube_tags") or fallback_meta["youtube_tags"])[:15],
                instagram_caption=str(ai_data.get("instagram_caption") or fallback_meta["instagram_caption"]),
            )

        meta = _build_template_metadata(
            language, streamer_name, source_title, transcript, heuristic_score
        )
        return QualityVerdict(
            approved=True,
            viral_score=heuristic_score,
            quality_reason=f"Sezgisel Onay | Konuşma: {word_count} kelime | Ses: %{int(non_silent_ratio*100)} | Hareket: {motion}",
            duration_sec=round(trim_end - trim_start, 2),
            non_silent_ratio=round(non_silent_ratio, 3),
            word_count=word_count,
            gameplay_motion_score=motion,
            faces_detected=faces,
            transcript=transcript,
            segments=segments,
            trim_start_sec=trim_start,
            trim_end_sec=trim_end,
            contact_sheet_path=visual["contact_sheet_path"],
            youtube_title=meta["youtube_title"],
            youtube_description=meta["youtube_description"],
            youtube_tags=meta["youtube_tags"],
            instagram_caption=meta["instagram_caption"],
        )

    async def regenerate_metadata(self, clip: dict[str, Any], language: str) -> dict[str, Any]:
        settings = get_settings()
        contact_sheet = Path(clip.get("contact_sheet_path") or "")
        prompt = _build_ai_prompt(
            language=language,
            streamer_name=clip["streamer_display_name"],
            source_title=clip.get("source_title", ""),
            duration=float(clip.get("duration_sec") or 35.0),
            transcript=clip.get("transcript", ""),
            min_sec=settings.clip_min_seconds,
            max_sec=settings.clip_max_seconds,
        )
        try:
            if settings.ai_provider == "openai" and settings.openai_api_key:
                data = await _evaluate_openai(
                    prompt, contact_sheet, settings.openai_model, settings.openai_api_key
                )
            elif settings.gemini_api_key:
                data = await _evaluate_gemini(
                    prompt, contact_sheet, settings.gemini_model, settings.gemini_api_key
                )
            elif settings.openai_api_key:
                data = await _evaluate_openai(
                    prompt, contact_sheet, settings.openai_model, settings.openai_api_key
                )
            else:
                data = _build_template_metadata(
                    language,
                    clip["streamer_display_name"],
                    clip.get("source_title", ""),
                    clip.get("transcript", ""),
                    float(clip.get("viral_score") or 7.5),
                )
            return {
                "youtube_title": str(data.get("youtube_title") or clip["youtube_title"])[:95],
                "youtube_description": str(data.get("youtube_description") or clip["youtube_description"]),
                "youtube_tags": list(data.get("youtube_tags") or clip["youtube_tags"])[:15],
                "instagram_caption": str(data.get("instagram_caption") or clip["instagram_caption"]),
            }
        except Exception as exc:
            logger.warning("Başlık yenileme hatası: %s", exc)
            return _build_template_metadata(
                language,
                clip["streamer_display_name"],
                clip.get("source_title", ""),
                clip.get("transcript", ""),
                float(clip.get("viral_score") or 7.5),
            )
