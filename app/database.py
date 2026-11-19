from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.config import CONFIG_DIR, get_settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> Path:
    return get_settings().data_dir / "clipper.db"


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize SQLite schema and seed TR + Global pipelines on first startup."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT NOT NULL,
                autopilot INTEGER NOT NULL DEFAULT 0,
                live_enabled INTEGER NOT NULL DEFAULT 1,
                clips_enabled INTEGER NOT NULL DEFAULT 1,
                game_keywords_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                username TEXT NOT NULL,
                display_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                channel_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                live_watch INTEGER NOT NULL DEFAULT 1,
                clip_scan INTEGER NOT NULL DEFAULT 1,
                is_live INTEGER NOT NULL DEFAULT 0,
                last_live_title TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(pipeline_id, platform, username)
            );

            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                streamer_username TEXT NOT NULL,
                streamer_display_name TEXT NOT NULL,
                source_platform TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_title TEXT NOT NULL DEFAULT '',
                duration_sec REAL NOT NULL DEFAULT 0.0,
                views_count INTEGER NOT NULL DEFAULT 0,
                raw_path TEXT NOT NULL DEFAULT '',
                rendered_path TEXT NOT NULL DEFAULT '',
                contact_sheet_path TEXT NOT NULL DEFAULT '',
                transcript TEXT NOT NULL DEFAULT '',
                segments_json TEXT NOT NULL DEFAULT '[]',
                word_count INTEGER NOT NULL DEFAULT 0,
                non_silent_ratio REAL NOT NULL DEFAULT 0.0,
                gameplay_motion_score REAL NOT NULL DEFAULT 0.0,
                faces_detected INTEGER NOT NULL DEFAULT 0,
                viral_score REAL NOT NULL DEFAULT 0.0,
                quality_reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                reject_reason TEXT NOT NULL DEFAULT '',
                youtube_title TEXT NOT NULL DEFAULT '',
                youtube_description TEXT NOT NULL DEFAULT '',
                youtube_tags_json TEXT NOT NULL DEFAULT '[]',
                instagram_caption TEXT NOT NULL DEFAULT '',
                youtube_video_id TEXT NOT NULL DEFAULT '',
                instagram_media_id TEXT NOT NULL DEFAULT '',
                telegram_message_id INTEGER NOT NULL DEFAULT 0,
                scheduled_for TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clips_dedup
                ON clips(pipeline_id, source_platform, source_id);

            CREATE INDEX IF NOT EXISTS idx_clips_status
                ON clips(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_id TEXT NOT NULL DEFAULT '',
                level TEXT NOT NULL DEFAULT 'info',
                source TEXT NOT NULL DEFAULT 'system',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
        if "scheduled_for" not in cols:
            conn.execute("ALTER TABLE clips ADD COLUMN scheduled_for TEXT NOT NULL DEFAULT ''")

        existing_count = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
        if existing_count == 0:
            _seed_default_pipelines(conn)


def _seed_default_pipelines(conn: sqlite3.Connection) -> None:
    seed_file = CONFIG_DIR / "default_pipelines.json"
    if not seed_file.exists():
        return
    payload = json.loads(seed_file.read_text(encoding="utf-8"))
    now = utc_now_iso()
    for p in payload.get("pipelines", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO pipelines
            (id, name, language, autopilot, live_enabled, clips_enabled, game_keywords_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p["id"],
                p["name"],
                p["language"],
                int(bool(p.get("autopilot", False))),
                int(bool(p.get("live_enabled", True))),
                int(bool(p.get("clips_enabled", True))),
                json.dumps(p.get("game_keywords", []), ensure_ascii=False),
                now,
            ),
        )
        for s in p.get("streamers", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO streamers
                (pipeline_id, username, display_name, platform, channel_url, enabled, live_watch, clip_scan, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["id"],
                    s["username"].strip().lower(),
                    s.get("display_name", s["username"]),
                    s["platform"].strip().lower(),
                    s["channel_url"].strip(),
                    int(bool(s.get("enabled", True))),
                    int(bool(s.get("live_watch", True))),
                    int(bool(s.get("clip_scan", True))),
                    now,
                ),
            )


def _pipeline_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "language": row["language"],
        "autopilot": bool(row["autopilot"]),
        "live_enabled": bool(row["live_enabled"]),
        "clips_enabled": bool(row["clips_enabled"]),
        "game_keywords": json.loads(row["game_keywords_json"] or "[]"),
        "updated_at": row["updated_at"],
    }


def _streamer_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pipeline_id": row["pipeline_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "platform": row["platform"],
        "channel_url": row["channel_url"],
        "enabled": bool(row["enabled"]),
        "live_watch": bool(row["live_watch"]),
        "clip_scan": bool(row["clip_scan"]),
        "is_live": bool(row["is_live"]),
        "last_live_title": row["last_live_title"],
        "last_checked_at": row["last_checked_at"],
        "created_at": row["created_at"],
    }


def _clip_from_row(row: sqlite3.Row) -> dict[str, Any]:
    rendered_name = Path(row["rendered_path"]).name if row["rendered_path"] else ""
    contact_name = Path(row["contact_sheet_path"]).name if row["contact_sheet_path"] else ""
    return {
        "id": row["id"],
        "pipeline_id": row["pipeline_id"],
        "streamer_username": row["streamer_username"],
        "streamer_display_name": row["streamer_display_name"],
        "source_platform": row["source_platform"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "source_url": row["source_url"],
        "source_title": row["source_title"],
        "duration_sec": row["duration_sec"],
        "views_count": row["views_count"],
        "raw_path": row["raw_path"],
        "rendered_path": row["rendered_path"],
        "rendered_url": f"/media/rendered/{rendered_name}" if rendered_name else "",
        "contact_sheet_path": row["contact_sheet_path"],
        "contact_sheet_url": f"/media/rendered/{contact_name}" if contact_name else "",
        "transcript": row["transcript"],
        "segments": json.loads(row["segments_json"] or "[]"),
        "word_count": row["word_count"],
        "non_silent_ratio": row["non_silent_ratio"],
        "gameplay_motion_score": row["gameplay_motion_score"],
        "faces_detected": row["faces_detected"],
        "viral_score": row["viral_score"],
        "quality_reason": row["quality_reason"],
        "status": row["status"],
        "reject_reason": row["reject_reason"],
        "youtube_title": row["youtube_title"],
        "youtube_description": row["youtube_description"],
        "youtube_tags": json.loads(row["youtube_tags_json"] or "[]"),
        "instagram_caption": row["instagram_caption"],
        "youtube_video_id": row["youtube_video_id"],
        "instagram_media_id": row["instagram_media_id"],
        "telegram_message_id": row["telegram_message_id"],
        "scheduled_for": row["scheduled_for"] if "scheduled_for" in row.keys() else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_pipelines() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pipelines ORDER BY id DESC").fetchall()
        return [_pipeline_from_row(r) for r in rows]


def get_pipeline(pipeline_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone()
        return _pipeline_from_row(row) if row else None


def update_pipeline(pipeline_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
    current = get_pipeline(pipeline_id)
    if not current:
        return None
    autopilot = int(bool(patch["autopilot"])) if "autopilot" in patch else int(current["autopilot"])
    live_enabled = int(bool(patch["live_enabled"])) if "live_enabled" in patch else int(current["live_enabled"])
    clips_enabled = int(bool(patch["clips_enabled"])) if "clips_enabled" in patch else int(current["clips_enabled"])
    keywords = (
        json.dumps(patch["game_keywords"], ensure_ascii=False)
        if "game_keywords" in patch
        else json.dumps(current["game_keywords"], ensure_ascii=False)
    )
    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE pipelines
            SET autopilot = ?, live_enabled = ?, clips_enabled = ?, game_keywords_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (autopilot, live_enabled, clips_enabled, keywords, now, pipeline_id),
        )
    return get_pipeline(pipeline_id)


def list_streamers(pipeline_id: Optional[str] = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM streamers WHERE 1=1"
    params: list[Any] = []
    if pipeline_id:
        query += " AND pipeline_id = ?"
        params.append(pipeline_id)
    if enabled_only:
        query += " AND enabled = 1"
    query += " ORDER BY pipeline_id ASC, is_live DESC, display_name ASC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_streamer_from_row(r) for r in rows]


def add_streamer(
    pipeline_id: str,
    username: str,
    platform: str,
    display_name: Optional[str] = None,
    channel_url: Optional[str] = None,
    live_watch: bool = True,
    clip_scan: bool = True,
) -> dict[str, Any]:
    clean_user = username.strip().lstrip("@").lower()
    clean_platform = platform.strip().lower()
    if not channel_url:
        if clean_platform == "kick":
            channel_url = f"https://kick.com/{clean_user}"
        elif clean_platform == "twitch":
            channel_url = f"https://www.twitch.tv/{clean_user}"
        else:
            channel_url = f"https://www.youtube.com/@{clean_user}/live"

    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO streamers
            (pipeline_id, username, display_name, platform, channel_url, enabled, live_watch, clip_scan, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(pipeline_id, platform, username) DO UPDATE SET
                display_name = excluded.display_name,
                channel_url = excluded.channel_url,
                enabled = 1,
                live_watch = excluded.live_watch,
                clip_scan = excluded.clip_scan
            """,
            (
                pipeline_id,
                clean_user,
                (display_name or username).strip(),
                clean_platform,
                channel_url.strip(),
                int(live_watch),
                int(clip_scan),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM streamers WHERE pipeline_id = ? AND platform = ? AND username = ?",
            (pipeline_id, clean_platform, clean_user),
        ).fetchone()
        return _streamer_from_row(row)


def update_streamer(streamer_id: int, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
        if not row:
            return None
        enabled = int(bool(patch["enabled"])) if "enabled" in patch else row["enabled"]
        live_watch = int(bool(patch["live_watch"])) if "live_watch" in patch else row["live_watch"]
        clip_scan = int(bool(patch["clip_scan"])) if "clip_scan" in patch else row["clip_scan"]
        is_live = int(bool(patch["is_live"])) if "is_live" in patch else row["is_live"]
        last_live_title = patch.get("last_live_title", row["last_live_title"])
        last_checked_at = patch.get("last_checked_at", row["last_checked_at"])
        conn.execute(
            """
            UPDATE streamers
            SET enabled = ?, live_watch = ?, clip_scan = ?, is_live = ?, last_live_title = ?, last_checked_at = ?
            WHERE id = ?
            """,
            (enabled, live_watch, clip_scan, is_live, last_live_title, last_checked_at, streamer_id),
        )
        updated = conn.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
        return _streamer_from_row(updated)


def delete_streamer(streamer_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
        return cur.rowcount > 0


def clip_exists(pipeline_id: str, source_platform: str, source_id: str, source_url: str = "") -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM clips
            WHERE (source_platform = ? AND source_id = ?)
               OR (source_type != 'live_spike' AND source_url != '' AND source_url = ?)
            LIMIT 1
            """,
            (source_platform, source_id, source_url),
        ).fetchone()
        return row is not None


def insert_clip(data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO clips (
                id, pipeline_id, streamer_username, streamer_display_name,
                source_platform, source_type, source_id, source_url, source_title,
                duration_sec, views_count, raw_path, rendered_path, contact_sheet_path,
                transcript, segments_json, word_count, non_silent_ratio,
                gameplay_motion_score, faces_detected, viral_score, quality_reason,
                status, reject_reason, youtube_title, youtube_description,
                youtube_tags_json, instagram_caption, youtube_video_id,
                instagram_media_id, telegram_message_id, scheduled_for, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                data["id"],
                data["pipeline_id"],
                data["streamer_username"],
                data.get("streamer_display_name", data["streamer_username"]),
                data["source_platform"],
                data["source_type"],
                data["source_id"],
                data["source_url"],
                data.get("source_title", ""),
                float(data.get("duration_sec", 0.0)),
                int(data.get("views_count", 0)),
                str(data.get("raw_path", "")),
                str(data.get("rendered_path", "")),
                str(data.get("contact_sheet_path", "")),
                data.get("transcript", ""),
                json.dumps(data.get("segments", []), ensure_ascii=False),
                int(data.get("word_count", 0)),
                float(data.get("non_silent_ratio", 0.0)),
                float(data.get("gameplay_motion_score", 0.0)),
                int(data.get("faces_detected", 0)),
                float(data.get("viral_score", 0.0)),
                data.get("quality_reason", ""),
                data.get("status", "pending_review"),
                data.get("reject_reason", ""),
                data.get("youtube_title", ""),
                data.get("youtube_description", ""),
                json.dumps(data.get("youtube_tags", []), ensure_ascii=False),
                data.get("instagram_caption", ""),
                data.get("youtube_video_id", ""),
                data.get("instagram_media_id", ""),
                int(data.get("telegram_message_id", 0)),
                data.get("scheduled_for", ""),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (data["id"],)).fetchone()
        return _clip_from_row(row)


def get_clip(clip_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return _clip_from_row(row) if row else None


def update_clip(clip_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
    allowed = {
        "raw_path",
        "rendered_path",
        "contact_sheet_path",
        "transcript",
        "word_count",
        "non_silent_ratio",
        "gameplay_motion_score",
        "faces_detected",
        "viral_score",
        "quality_reason",
        "status",
        "reject_reason",
        "youtube_title",
        "youtube_description",
        "instagram_caption",
        "youtube_video_id",
        "instagram_media_id",
        "telegram_message_id",
        "scheduled_for",
        "duration_sec",
    }
    sets: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k == "youtube_tags":
            sets.append("youtube_tags_json = ?")
            params.append(json.dumps(v, ensure_ascii=False))
        elif k == "segments":
            sets.append("segments_json = ?")
            params.append(json.dumps(v, ensure_ascii=False))
        elif k in allowed:
            sets.append(f"{k} = ?")
            params.append(v)

    if not sets:
        return get_clip(clip_id)

    sets.append("updated_at = ?")
    params.append(utc_now_iso())
    params.append(clip_id)

    with get_conn() as conn:
        conn.execute(f"UPDATE clips SET {', '.join(sets)} WHERE id = ?", params)
    return get_clip(clip_id)


def list_clips(
    pipeline_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM clips WHERE 1=1"
    params: list[Any] = []
    if pipeline_id:
        query += " AND pipeline_id = ?"
        params.append(pipeline_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if status == "pending_review":
        query += " ORDER BY viral_score DESC, duration_sec DESC, created_at ASC LIMIT ?"
    else:
        query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_clip_from_row(r) for r in rows]


def add_log(message: str, pipeline_id: str = "", level: str = "info", source: str = "system") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO activity_logs (pipeline_id, level, source, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pipeline_id, level, source, message, utc_now_iso()),
        )


def list_logs(limit: int = 40) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_dashboard_stats() -> dict[str, Any]:
    with get_conn() as conn:
        pipelines = [_pipeline_from_row(r) for r in conn.execute("SELECT * FROM pipelines ORDER BY id DESC").fetchall()]
        counts_rows = conn.execute(
            """
            SELECT pipeline_id, status, COUNT(*) AS cnt
            FROM clips
            GROUP BY pipeline_id, status
            """
        ).fetchall()
        live_rows = conn.execute(
            "SELECT pipeline_id, COUNT(*) AS cnt FROM streamers WHERE enabled = 1 AND is_live = 1 GROUP BY pipeline_id"
        ).fetchall()
        total_streamers = conn.execute("SELECT COUNT(*) FROM streamers WHERE enabled = 1").fetchone()[0]

    stats: dict[str, dict[str, int]] = {}
    for p in pipelines:
        stats[p["id"]] = {
            "pending_review": 0,
            "published": 0,
            "rejected": 0,
            "failed": 0,
            "live_now": 0,
        }
    for row in counts_rows:
        pid = row["pipeline_id"]
        if pid in stats and row["status"] in stats[pid]:
            stats[pid][row["status"]] = row["cnt"]
    for row in live_rows:
        pid = row["pipeline_id"]
        if pid in stats:
            stats[pid]["live_now"] = row["cnt"]

    return {
        "pipelines": pipelines,
        "pipeline_stats": stats,
        "enabled_streamers": total_streamers,
    }


def get_publish_pacing(pipeline_id: str) -> dict[str, Any]:
    """Return 24h YouTube publish count and minutes elapsed since last publish."""
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT updated_at FROM clips
            WHERE pipeline_id = ?
              AND status = 'published'
              AND youtube_video_id != ''
              AND updated_at >= ?
            ORDER BY updated_at DESC
            """,
            (pipeline_id, cutoff_iso),
        ).fetchall()

    minutes_since_last: Optional[float] = None
    if rows:
        try:
            last_dt = datetime.fromisoformat(str(rows[0]["updated_at"]).replace("Z", "+00:00"))
            minutes_since_last = max(0.0, (now - last_dt).total_seconds() / 60.0)
        except ValueError:
            minutes_since_last = None

    return {
        "published_last_24h": len(rows),
        "minutes_since_last_publish": minutes_since_last,
    }


def get_next_queued_clip(pipeline_id: str) -> Optional[dict[str, Any]]:
    """Return highest-scoring queued clip ready for scheduled YouTube upload."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM clips
            WHERE pipeline_id = ?
              AND status = 'pending_review'
              AND rendered_path != ''
            ORDER BY viral_score DESC, duration_sec DESC, created_at ASC
            LIMIT 1
            """,
            (pipeline_id,),
        ).fetchone()
        return _clip_from_row(row) if row else None


def enforce_queue_cap(pipeline_id: str, max_keep: int) -> int:
    """Keep only top max_keep queued clips per pipeline and free disk for excess clips."""
    if max_keep < 1:
        return 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, rendered_path, contact_sheet_path FROM clips
            WHERE pipeline_id = ? AND status = 'pending_review'
            ORDER BY viral_score DESC, duration_sec DESC, created_at ASC
            """,
            (pipeline_id,),
        ).fetchall()
        if len(rows) <= max_keep:
            return 0
        overflow = rows[max_keep:]
        now = utc_now_iso()
        for r in overflow:
            for p_col in ("rendered_path", "contact_sheet_path"):
                raw = str(r[p_col] or "").strip()
                if raw:
                    try:
                        Path(raw).unlink(missing_ok=True)
                    except OSError:
                        pass
            conn.execute(
                """
                UPDATE clips
                SET status = 'rejected',
                    reject_reason = 'Kuyruk dolu (daha yüksek puanlı klipler korundu)',
                    rendered_path = '',
                    contact_sheet_path = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, r["id"]),
            )
        return len(overflow)


def compute_pipeline_schedule(
    pipeline_id: str,
    daily_cap: int,
    interval_minutes: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return queued clips enriched with queue_rank and estimated scheduled_for ISO timestamp."""
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        pub_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(scheduled_for, ''), updated_at) AS slot_ts
            FROM clips
            WHERE pipeline_id = ?
              AND status = 'published'
              AND youtube_video_id != ''
              AND COALESCE(NULLIF(scheduled_for, ''), updated_at) >= ?
            ORDER BY slot_ts ASC
            """,
            (pipeline_id, cutoff_iso),
        ).fetchall()
        queued_rows = conn.execute(
            """
            SELECT * FROM clips
            WHERE pipeline_id = ?
              AND status = 'pending_review'
              AND rendered_path != ''
            ORDER BY viral_score DESC, duration_sec DESC, created_at ASC
            LIMIT ?
            """,
            (pipeline_id, limit),
        ).fetchall()

    recent_publishes: list[datetime] = []
    for r in pub_rows:
        try:
            recent_publishes.append(datetime.fromisoformat(str(r["slot_ts"]).replace("Z", "+00:00")))
        except ValueError:
            continue
    recent_publishes.sort()

    cursor = now
    if recent_publishes:
        cursor = max(cursor, recent_publishes[-1] + timedelta(minutes=interval_minutes))

    cap = max(1, daily_cap)

    def _align_prime_window(dt: datetime) -> datetime:
        """Keep scheduled slots inside active audience hours (avoid 00:00-11:59 TR / dead-night UTC)."""
        if pipeline_id == "tr":
            # Turkey UTC+3: active prime window 13:00-23:30 TR => 10:00-20:30 UTC
            if dt.hour < 10:
                return dt.replace(hour=10, minute=0, second=0, microsecond=0)
            if dt.hour >= 21:
                nxt = dt + timedelta(days=1)
                return nxt.replace(hour=10, minute=0, second=0, microsecond=0)
        else:
            # Global US/EU audience: active prime window 15:00-02:30 UTC (18:00-05:30 TR / 10:00-21:30 EST)
            if 3 <= dt.hour < 15:
                return dt.replace(hour=15, minute=0, second=0, microsecond=0)
        return dt

    scheduled_clips: list[dict[str, Any]] = []
    for idx, row in enumerate(queued_rows, start=1):
        clip = _clip_from_row(row)
        while True:
            cursor = _align_prime_window(cursor)
            window_start = cursor - timedelta(hours=24)
            active_in_24h = [t for t in recent_publishes if window_start < t <= cursor]
            if len(active_in_24h) < cap:
                break
            oldest_in_window = min(active_in_24h)
            cursor = max(cursor, oldest_in_window + timedelta(hours=24, minutes=1))

        clip["queue_rank"] = idx
        clip["scheduled_for"] = cursor.isoformat()
        scheduled_clips.append(clip)
        recent_publishes.append(cursor)
        cursor = cursor + timedelta(minutes=interval_minutes)

    return scheduled_clips
