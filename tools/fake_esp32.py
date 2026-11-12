"""Pretend to be the ESP32-CAM, so the whole system can be tested without boards.

    python tools/fake_esp32.py                       # laptop webcam 0
    python tools/fake_esp32.py --source video.mp4    # loop a video file
    python tools/fake_esp32.py --source photos/      # show images from a folder (GET /next to switch)
    python tools/fake_esp32.py --source pattern      # moving test pattern (no camera at all)

It provides the same two things as the real ESP32-CAM firmware:
  1. MJPEG stream at http://127.0.0.1:8081/stream  (real board: port 81)
  2. The serial relay: lines "T,<cm>" in -> GET <backend>/trigger?d=<cm> -> reply line out.
     The "serial line" is a TCP socket on port 3333 (used by tools/fake_arduino.py), and/or a
     real serial port (--serial COM5 / /dev/tty.usbmodem...) for a real Arduino flashed with
     ESP_LINK_USB=1 (then the Arduino talks to this script over its USB cable).
"""
from __future__ import annotations

import argparse
import itertools
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import Settings  # noqa: E402

VALID_REPLIES = ("UNKNOWN", "NOFACE", "ERR")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class Camera:
    """Produces JPEG frames from a webcam, video file, image folder/file, or a test pattern."""

    def __init__(self, source: str, fps: float, cycle_s: float) -> None:
        self.source, self.fps, self.cycle_s = source, fps, cycle_s
        self.jpeg = b""
        self.images: list[Path] = []
        self.image_index = 0
        self.lock = threading.Lock()
        p = Path(source)
        if p.is_dir():
            self.images = sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTS)
            if not self.images:
                sys.exit(f"No images in {p}")
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            self.images = [p]
        threading.Thread(target=self._run, daemon=True).start()

    def next_image(self) -> str:
        if not self.images:
            return "not in image mode"
        self.image_index = (self.image_index + 1) % len(self.images)
        return self.images[self.image_index].name

    def _encode(self, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()

    def _run(self) -> None:
        delay = 1.0 / self.fps
        if self.images:
            started = time.monotonic()
            while True:
                if self.cycle_s > 0 and time.monotonic() - started > self.cycle_s:
                    started = time.monotonic()
                    self.next_image()
                img = cv2.imdecode(np.fromfile(str(self.images[self.image_index]), np.uint8), cv2.IMREAD_COLOR)
                self._encode(cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1]))))
                time.sleep(delay)
        elif self.source == "pattern":
            for i in itertools.count():
                img = np.full((480, 640, 3), 50, np.uint8)
                cv2.circle(img, (int(320 + 250 * np.sin(i / 15)), 240), 40, (0, 200, 255), -1)
                cv2.putText(img, "FAKE ESP32-CAM  " + time.strftime("%H:%M:%S"), (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                self._encode(img)
                time.sleep(delay)
        else:  # webcam index or video file
            src = int(self.source) if self.source.isdigit() else self.source
            while True:
                cap = cv2.VideoCapture(src)
                if not cap.isOpened():
                    print(f"[cam] cannot open {self.source}, retrying in 2 s")
                    time.sleep(2)
                    continue
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break  # end of video file -> reopen = loop
                    self._encode(frame)
                    time.sleep(delay if not isinstance(src, int) else 0)
                cap.release()


def make_http_handler(cam: Camera):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
                self.end_headers()
                try:
                    while True:
                        with cam.lock:
                            jpg = cam.jpeg
                        if jpg:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                        time.sleep(1.0 / cam.fps)
                except (ConnectionError, OSError):
                    return  # client went away
            elif self.path.startswith("/next"):
                self._text(f"now showing {cam.next_image()}\n")
            elif self.path.startswith("/capture"):
                with cam.lock:
                    jpg = cam.jpeg
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(jpg)
            else:
                self._text("fake ESP32-CAM: /stream /capture /next\n")

        def _text(self, s: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(s.encode())

    return Handler


def relay(line: str, backend: str, token: str) -> str | None:
    """Exactly what the real ESP32 does with one line from the Arduino. None = ignore the line."""
    line = line.strip()
    if not line.startswith("T,"):
        return None
    dist = line[2:]
    if not dist.replace(".", "", 1).isdigit():
        return None
    try:
        r = requests.get(f"{backend}/trigger", params={"d": dist, "token": token}, timeout=(3, 8))
        reply = r.text.strip() if r.status_code == 200 else "ERR"
    except requests.RequestException:
        reply = "ERR"
    if not (reply.startswith("KNOWN:") or reply in VALID_REPLIES):
        reply = "ERR"
    print(f"[relay] {line!r} -> {reply}")
    return reply


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # show relay lines immediately
    s = Settings.from_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0", help="webcam index, video file, image file/folder, or 'pattern'")
    ap.add_argument("--port", type=int, default=8081, help="MJPEG HTTP port (default 8081)")
    ap.add_argument("--relay-port", type=int, default=3333, help="TCP 'serial line' port (default 3333)")
    ap.add_argument("--backend", default=f"http://127.0.0.1:{s.port}")
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--cycle", type=float, default=0, help="image folder: switch image every N seconds")
    ap.add_argument("--serial", help="also relay for a real Arduino on this serial port (e.g. COM5)")
    args = ap.parse_args()

    cam = Camera(args.source, args.fps, args.cycle)
    http = ThreadingHTTPServer(("0.0.0.0", args.port), make_http_handler(cam))
    threading.Thread(target=http.serve_forever, daemon=True).start()

    class RelayHandler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(f"IP:{lan_ip()}\n".encode())
            for raw in self.rfile:
                reply = relay(raw.decode(errors="ignore"), args.backend, s.api_token)
                if reply:
                    self.wfile.write((reply + "\n").encode())

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    tcp = socketserver.ThreadingTCPServer(("127.0.0.1", args.relay_port), RelayHandler)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()

    print(f"Fake ESP32-CAM running. Source: {args.source}")
    print(f"  stream : http://127.0.0.1:{args.port}/stream   (set STREAM_URL to this)")
    print(f"  relay  : tcp://127.0.0.1:{args.relay_port}  -> {args.backend}/trigger")

    if args.serial:
        import serial  # pyserial
        port = serial.Serial(args.serial, 9600, timeout=0.2)
        print(f"  serial : {args.serial} @ 9600")
        time.sleep(2)  # opening the port resets an Uno; give it time to boot
        port.write(f"IP:{lan_ip()}\n".encode())
        buf = b""
        while True:
            buf += port.read(64)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode(errors="ignore").strip()
                reply = relay(text, args.backend, s.api_token)
                if reply:
                    port.write((reply + "\n").encode())
                elif text:
                    print(f"[arduino] {text}")
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
