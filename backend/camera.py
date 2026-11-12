"""Background thread that keeps the latest camera frame in memory.

Source can be an MJPEG URL (the ESP32-CAM's http://<ip>:81/stream, or tools/fake_esp32.py)
or a webcam index such as "0". If the stream drops, we wait and reconnect automatically
(1 s, 2 s, 4 s ... up to 10 s between attempts).

Why parse MJPEG ourselves instead of cv2.VideoCapture(url)? VideoCapture on an HTTP stream can
block for a long time when the ESP32 disappears and its reconnect behaviour differs per OS.
An MJPEG stream is just JPEG images back to back, so we read bytes and cut out each JPEG between
its start marker (FF D8) and end marker (FF D9). Simple, and we control every timeout.
"""
from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG start / end markers
MAX_BUFFER = 4 * 1024 * 1024         # drop the buffer if garbage piles up (prevents memory growth)


def split_jpegs(buf: bytearray) -> list[bytes]:
    """Remove and return every complete JPEG in `buf`; leftover partial data stays in `buf`."""
    out = []
    while True:
        start = buf.find(SOI)
        if start < 0:
            del buf[:-1]  # keep the last byte: it might be the first half of a marker
            return out
        end = buf.find(EOI, start + 2)
        if end < 0:
            del buf[:start]
            return out
        out.append(bytes(buf[start:end + 2]))
        del buf[:end + 2]


class FrameSource:
    def __init__(self, source: str, offline_after_s: float = 3.0) -> None:
        self.source = source
        self.offline_after_s = offline_after_s
        self.last_error = ""
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    # ---- public API -------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def online(self) -> bool:
        return self._frame is not None and time.monotonic() - self._ts < self.offline_after_s

    def latest(self) -> tuple[np.ndarray | None, int]:
        """Most recent frame (or None if the camera is offline) and its sequence number."""
        with self._cond:
            if not self.online:
                return None, self._seq
            return self._frame, self._seq

    def wait_new(self, after_seq: int, timeout: float) -> tuple[np.ndarray, int] | None:
        """Block until a frame newer than `after_seq` arrives, or return None on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > after_seq, timeout=timeout):
                return None
            return self._frame, self._seq

    # ---- internals ---------------------------------------------------------
    def _publish(self, frame: np.ndarray) -> None:
        with self._cond:
            self._frame, self._ts = frame, time.monotonic()
            self._seq += 1
            self._cond.notify_all()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            got_frames = False
            try:
                if self.source.isdigit():
                    got_frames = self._read_webcam(int(self.source))
                else:
                    got_frames = self._read_mjpeg(self.source)
                self.last_error = "stream ended"
            except Exception as exc:  # network errors, timeouts, bad HTTP status...
                self.last_error = str(exc)
            if got_frames:
                backoff = 1.0
            log.warning("Camera %s unavailable (%s); retrying in %.0fs", self.source, self.last_error, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 10.0)

    def _read_mjpeg(self, url: str) -> bool:
        got = False
        # timeout=(connect, read): if no bytes arrive for 5 s the stream is considered dead.
        with requests.get(url, stream=True, timeout=(4, 5)) as resp:
            resp.raise_for_status()
            log.info("Connected to camera stream %s", url)
            self.last_error = ""
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=16384):
                if self._stop.is_set():
                    return got
                buf += chunk
                if len(buf) > MAX_BUFFER:
                    buf.clear()
                for jpg in split_jpegs(buf):
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:  # corrupt JPEGs (e.g. WiFi glitches) are skipped
                        self._publish(frame)
                        got = True
        return got

    def _read_webcam(self, index: int) -> bool:
        cap = cv2.VideoCapture(index)
        got = False
        try:
            if not cap.isOpened():
                raise RuntimeError(f"cannot open webcam {index}")
            log.info("Connected to webcam %d", index)
            self.last_error = ""
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    return got
                self._publish(frame)
                got = True
        finally:
            cap.release()
        return got
