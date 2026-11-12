"""DoorMonitor: the part of the backend that answers "who is at the door?".

Two jobs:
 1. check(distance) - called by GET /trigger when the Arduino decides someone is close.
    Looks at the live frames for up to CHECK_WINDOW_S seconds and returns the reply line that
    goes back to the Arduino: "KNOWN:<name>", "UNKNOWN", "NOFACE" or "ERR".
 2. A background thread that draws boxes + names on the live feed for the web page.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import cv2
import numpy as np

from .events import EventStore, cleanup_snapshots
from .recognizer import Face

log = logging.getLogger(__name__)

GREEN, RED, WHITE, BLACK = (60, 200, 60), (40, 40, 230), (255, 255, 255), (0, 0, 0)


class Frames(Protocol):
    online: bool
    def latest(self) -> tuple[np.ndarray | None, int]: ...
    def wait_new(self, after_seq: int, timeout: float) -> tuple[np.ndarray, int] | None: ...


class Identifier(Protocol):
    def identify(self, bgr: np.ndarray) -> list[Face]: ...


def safe_name(name: str) -> str:
    """Names travel over a newline-terminated serial line and show on a 16-char LCD."""
    return re.sub(r"[^A-Za-z0-9 _.-]", "", name)[:16] or "?"


def draw_overlay(frame: np.ndarray, faces: list[Face]) -> np.ndarray:
    """CCTV-style overlay: timestamp bar + a box and label per face."""
    img = frame.copy()
    for f in faces:
        x, y, w, h = f.box
        color = GREEN if f.name else RED
        label = f.name or "UNKNOWN"
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        ty = max(y - 8, th + 8)
        cv2.rectangle(img, (x, ty - th - 6), (x + tw + 8, ty + 4), color, -1)
        cv2.putText(img, label, (x + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2, cv2.LINE_AA)
    stamp = "DOOR CAM  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cv2.rectangle(img, (0, 0), (img.shape[1], 26), BLACK, -1)
    cv2.circle(img, (12, 13), 6, RED, -1)
    cv2.putText(img, stamp, (24, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, WHITE, 1, cv2.LINE_AA)
    return img


def no_signal_jpeg() -> bytes:
    img = np.full((480, 640, 3), 30, np.uint8)
    cv2.putText(img, "NO SIGNAL", (190, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.4, WHITE, 3, cv2.LINE_AA)
    cv2.putText(img, "waiting for camera stream...", (170, 280), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 180), 1,
                cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


class DoorMonitor:
    UNKNOWN_CONFIRM_FRAMES = 3  # frames that must show only strangers before we say UNKNOWN

    def __init__(self, frames: Frames, facedb: Identifier, events: EventStore, snapshots_dir: Path,
                 cooldown_s: float = 15.0, check_window_s: float = 1.5, recognize_every_n: int = 3,
                 snapshot_max_age_days: float = 7.0,
                 alerter: Callable[[str, Path | None], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.frames = frames
        self.facedb = facedb
        self.events = events
        self.snapshots_dir = snapshots_dir
        self.cooldown_s = cooldown_s
        self.check_window_s = check_window_s
        self.recognize_every_n = recognize_every_n
        self.snapshot_max_age_days = snapshot_max_age_days
        self.alerter = alerter
        self.clock = clock
        self._check_lock = threading.Lock()          # one door check at a time
        self._last_logged: dict[str, float] = {}     # verdict key -> clock() of last logged event
        self._annotated = no_signal_jpeg()
        self._annotated_seq = 0
        self._stop = threading.Event()
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ door check
    def check(self, distance_cm: float | None = None) -> str:
        with self._check_lock:
            verdict, faces, frame = self._look()
            return self._record(verdict, faces, frame, distance_cm)

    def _look(self) -> tuple[str, list[Face], np.ndarray | None]:
        """Examine frames for up to check_window_s. Any known face -> KNOWN immediately.

        A single bad angle can make a known person look unknown, so we only conclude UNKNOWN
        after UNKNOWN_CONFIRM_FRAMES frames agree (or the window ends). NOFACE waits for the
        whole window, because the visitor may still be turning towards the camera.
        """
        frame, seq = self.frames.latest()
        if frame is None:
            return "ERR", [], None
        deadline = time.monotonic() + self.check_window_s
        unknown: tuple[list[Face], np.ndarray] | None = None
        unknown_frames = 0
        while True:
            faces = self.facedb.identify(frame)
            known = [f for f in faces if f.name]
            if known:
                best = min(known, key=lambda f: f.distance)
                return f"KNOWN:{safe_name(best.name)}", faces, frame
            if faces:
                unknown_frames += 1
                if unknown is None:
                    unknown = (faces, frame)
                if unknown_frames >= self.UNKNOWN_CONFIRM_FRAMES:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            nxt = self.frames.wait_new(seq, timeout=min(remaining, 0.5))
            if nxt is None:
                break
            frame, seq = nxt
        if unknown:
            return "UNKNOWN", unknown[0], unknown[1]
        return "NOFACE", [], frame

    def _record(self, verdict: str, faces: list[Face], frame: np.ndarray | None, distance: float | None) -> str:
        """Log the event (unless it's a duplicate within the cooldown) and fire alerts."""
        if verdict.startswith("KNOWN:"):
            name = verdict.split(":", 1)[1]
            kind, key, message = "known", verdict, f"{name} is at the door"
        elif verdict == "UNKNOWN":
            name, kind, key, message = None, "unknown", verdict, "Unknown person at your door"
        elif verdict == "NOFACE":
            name, kind, key, message = None, "noface", verdict, "Someone at the door, but no face visible"
        else:
            name, kind, key, message = None, "error", verdict, "Door check failed: camera stream unavailable"

        now = self.clock()
        last = self._last_logged.get(key)
        if last is not None and now - last < self.cooldown_s:
            log.info("Duplicate %s within cooldown - not logged again", verdict)
            return verdict  # the Arduino still gets the answer, but no duplicate alert
        self._last_logged[key] = now

        snapshot = None
        if frame is not None and kind in ("known", "unknown"):
            snapshot = self._save_snapshot(draw_overlay(frame, faces), kind)
        self.events.add(kind, message, name=name, distance=distance, snapshot=snapshot)
        log.info("Door check: %s (distance=%s)", verdict, distance)
        if kind == "unknown" and self.alerter:
            self.alerter(f"⚠️ {message} ({datetime.now():%H:%M:%S})",
                         self.snapshots_dir / snapshot if snapshot else None)
        return verdict

    def _save_snapshot(self, img: np.ndarray, kind: str) -> str:
        filename = f"{datetime.now():%Y%m%d_%H%M%S_%f}_{kind}.jpg"
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            (self.snapshots_dir / filename).write_bytes(buf.tobytes())
        return filename

    # ------------------------------------------------------------------ live annotated feed
    def annotated(self) -> tuple[bytes, int]:
        return self._annotated, self._annotated_seq

    def start(self) -> None:
        threading.Thread(target=self._annotate_loop, name="annotate", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _annotate_loop(self) -> None:
        seq, n, faces = 0, 0, []
        offline_img = no_signal_jpeg()
        last_cleanup = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_cleanup > 3600:  # hourly snapshot housekeeping
                last_cleanup = time.monotonic()
                cleanup_snapshots(self.snapshots_dir, self.snapshot_max_age_days)
            got = self.frames.wait_new(seq, timeout=1.0)
            if got is None:
                if not self.frames.online:
                    self._set_annotated(offline_img)
                continue
            frame, seq = got
            try:
                # Recognition is the expensive part, so only every Nth frame; boxes from the
                # previous run are reused in between (they lag by a fraction of a second).
                if n % self.recognize_every_n == 0:
                    faces = self.facedb.identify(frame)
                n += 1
                ok, buf = cv2.imencode(".jpg", draw_overlay(frame, faces), [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    self._set_annotated(buf.tobytes())
            except Exception:
                log.exception("Annotating frame failed")
                time.sleep(0.5)

    def _set_annotated(self, jpg: bytes) -> None:
        self._annotated = jpg
        self._annotated_seq += 1
