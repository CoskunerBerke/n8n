from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _ass_timestamp(seconds: float) -> str:
    sec = max(0.0, seconds)
    hours = int(sec // 3600)
    minutes = int((sec % 3600) // 60)
    secs = sec % 60.0
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape_ass(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", "\\N")
    )


def _chunk_segments_for_shorts(
    segments: list[dict[str, Any]],
    trim_start: float,
    trim_end: float,
) -> list[tuple[float, float, str]]:
    """Split Whisper segments into punchy 4-5 word subtitle cues relative to trim_start."""
    cues: list[tuple[float, float, str]] = []
    for seg in segments:
        s0 = float(seg.get("start", 0.0))
        s1 = float(seg.get("end", 0.0))
        text = str(seg.get("text", "")).strip()
        if not text or s1 <= trim_start or s0 >= trim_end:
            continue

        clamped_start = max(s0, trim_start) - trim_start
        clamped_end = min(s1, trim_end) - trim_start
        words = text.split()
        if not words or clamped_end <= clamped_start:
            continue

        chunk_size = 4
        chunks = [words[i : i + chunk_size] for i in range(0, len(words), chunk_size)]
        total_dur = clamped_end - clamped_start
        slice_dur = total_dur / len(chunks)
        for idx, chunk in enumerate(chunks):
            c_start = clamped_start + idx * slice_dur
            c_end = clamped_start + (idx + 1) * slice_dur
            cues.append((c_start, c_end, " ".join(chunk).upper()))
    return cues


def build_ass_subtitles(
    segments: list[dict[str, Any]],
    trim_start: float,
    trim_end: float,
    streamer_name: str,
) -> str:
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,DejaVu Sans,68,&H00FFFFFF,&H0000FFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,410,1\n"
        "Style: Watermark,DejaVu Sans,34,&HCCFFFFFF,&H00FFFFFF,&H00000000,&H66000000,-1,0,0,0,100,100,1,0,1,3,1,8,40,40,210,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    clip_len = max(1.0, trim_end - trim_start)
    events.append(
        f"Dialogue: 0,0:00:00.00,{_ass_timestamp(clip_len)},Watermark,,0,0,0,,@{_escape_ass(streamer_name)}"
    )

    for c_start, c_end, line_text in _chunk_segments_for_shorts(segments, trim_start, trim_end):
        events.append(
            f"Dialogue: 1,{_ass_timestamp(c_start)},{_ass_timestamp(c_end)},Default,,0,0,0,,{_escape_ass(line_text)}"
        )

    return header + "\n".join(events) + "\n"


async def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    streams = data.get("streams") or [{}]
    return int(streams[0].get("width") or 1920), int(streams[0].get("height") or 1080)


class VideoRenderer:
    """Renders 1080x1920 Shorts/Reels with blurred background, normalized audio, and burned ASS captions."""

    async def render_vertical_short(
        self,
        raw_path: Path,
        clip_id: str,
        segments: list[dict[str, Any]],
        streamer_name: str,
        trim_start: float = 0.0,
        trim_end: float = 0.0,
    ) -> Path:
        settings = get_settings()
        work_dir = settings.work_dir / clip_id
        work_dir.mkdir(parents=True, exist_ok=True)
        rendered_out = settings.rendered_dir / f"{clip_id}.mp4"
        rendered_out.parent.mkdir(parents=True, exist_ok=True)

        try:
            local_in = work_dir / "input.mp4"
            shutil.copy2(raw_path, local_in)

            width, height = await _probe_dimensions(local_in)
            ass_content = build_ass_subtitles(
                segments=segments,
                trim_start=trim_start,
                trim_end=trim_end if trim_end > trim_start else 60.0,
                streamer_name=streamer_name,
            )
            (work_dir / "captions.ass").write_text(ass_content, encoding="utf-8")

            is_already_vertical = height > width
            if is_already_vertical:
                base_filter = (
                    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,setsar=1,ass=captions.ass[vout]"
                )
            else:
                # Blurred darkened backdrop + full-width gameplay/webcam frame slightly above center
                base_filter = (
                    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,boxblur=20:4,eq=brightness=-0.16[bg];"
                    "[0:v]scale=1080:-2[fg];"
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2-70,setsar=1,ass=captions.ass[vout]"
                )

            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            if trim_start > 0.0:
                cmd.extend(["-ss", f"{trim_start:.2f}"])
            if trim_end > trim_start:
                cmd.extend(["-to", f"{trim_end:.2f}"])

            # IMPORTANT: Execute inside work_dir using bare filenames so Windows drive letters
            # (C:\...) and non-ASCII directory names (Masaüstü) never break FFmpeg's ass= filter.
            cmd.extend(
                [
                    "-i",
                    "input.mp4",
                    "-filter_complex",
                    base_filter,
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "21",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    "30",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-af",
                    "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-movflags",
                    "+faststart",
                    "output.mp4",
                ]
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=240.0)
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"FFmpeg render hatası: {err_msg[:300]}")

            local_out = work_dir / "output.mp4"
            if not local_out.exists():
                raise FileNotFoundError("FFmpeg çıktı dosyası oluşturulamadı: output.mp4")
            shutil.move(str(local_out), str(rendered_out))
            return rendered_out
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
