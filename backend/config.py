"""All configuration comes from environment variables (or a .env file in the repo root).

See .env.example for the full list with explanations.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _float_or_none(value: str) -> float | None:
    return float(value) if value else None


@dataclass
class Settings:
    # Camera source: an MJPEG URL (ESP32-CAM: http://<ip>:81/stream) or a webcam index like "0".
    stream_url: str = "http://127.0.0.1:8081/stream"
    host: str = "0.0.0.0"
    port: int = 8000
    # Shared secret for the web app and /trigger. Empty = auth disabled (a warning is logged).
    api_token: str = ""

    # Face recognition
    face_backend: str = "auto"            # auto | dlib | opencv
    face_tolerance: float | None = None   # None = backend default (lower = stricter)
    process_width: int = 480              # frames are downscaled to this width before recognition
    recognize_every_n: int = 3            # live feed: run recognition on every Nth frame only
    check_window_s: float = 1.0           # /trigger: look at frames for up to this long

    # Duplicate-alert suppression: the same verdict (e.g. UNKNOWN) is only logged/alerted once per window.
    cooldown_s: float = 15.0

    # Storage
    faces_dir: Path = field(default_factory=lambda: ROOT / "faces")
    snapshots_dir: Path = field(default_factory=lambda: ROOT / "snapshots")
    models_dir: Path = field(default_factory=lambda: ROOT / "models")
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "events.db")
    snapshot_max_age_days: float = 7.0

    # Optional push alerts
    alert_webhook: str = ""               # "" (off) or "telegram"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or ROOT / ".env", override=False)
        d = cls()
        return cls(
            stream_url=_get("STREAM_URL", d.stream_url),
            host=_get("HOST", d.host),
            port=int(_get("PORT", str(d.port))),
            api_token=_get("API_TOKEN", ""),
            face_backend=_get("FACE_BACKEND", d.face_backend).lower(),
            face_tolerance=_float_or_none(_get("FACE_TOLERANCE", "")),
            process_width=int(_get("PROCESS_WIDTH", str(d.process_width))),
            recognize_every_n=max(1, int(_get("RECOGNIZE_EVERY_N", str(d.recognize_every_n)))),
            check_window_s=float(_get("CHECK_WINDOW_S", str(d.check_window_s))),
            cooldown_s=float(_get("COOLDOWN_S", str(d.cooldown_s))),
            faces_dir=Path(_get("FACES_DIR", str(d.faces_dir))),
            snapshots_dir=Path(_get("SNAPSHOTS_DIR", str(d.snapshots_dir))),
            models_dir=Path(_get("MODELS_DIR", str(d.models_dir))),
            db_path=Path(_get("DB_PATH", str(d.db_path))),
            snapshot_max_age_days=float(_get("SNAPSHOT_MAX_AGE_DAYS", str(d.snapshot_max_age_days))),
            alert_webhook=_get("ALERT_WEBHOOK", "").lower(),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
        )
