"""Enroll a known person by capturing face photos into faces/<name>/.

    python enroll.py Alice                 # grab frames via the running backend (default), else STREAM_URL
    python enroll.py Alice --webcam        # use the laptop webcam instead (index 0)
    python enroll.py Alice --count 15

Stand in front of the camera and slowly turn your head a little left/right/up/down.
Only frames with exactly ONE face are saved. Afterwards the running backend is told to reload
its faces (no restart needed). You can also enroll from the web app's "Faces" tab.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import requests

from backend.camera import FrameSource
from backend.config import Settings
from backend.recognizer import FaceDB, make_recognizer


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name")
    p.add_argument("--count", type=int, default=10, help="photos to save (default 10)")
    p.add_argument("--interval", type=float, default=0.7, help="seconds between photos")
    p.add_argument("--webcam", nargs="?", const="0", help="use a local webcam (optional index, default 0)")
    p.add_argument("--stream", help="read this MJPEG URL directly (instead of via the backend)")
    args = p.parse_args()

    s = Settings.from_env()
    name = args.name.strip()
    if not name or any(c in name for c in r'\/:*?"<>|'):
        sys.exit("Invalid name")
    server = f"http://127.0.0.1:{s.port}"
    headers = {"X-Token": s.api_token} if s.api_token else {}

    # Where do frames come from? The ESP32 only serves ONE stream client well, and the backend
    # is normally that client, so by default we ask the backend for its latest frame.
    grab = None
    if not args.webcam and not args.stream:
        try:
            requests.get(f"{server}/health", timeout=2).raise_for_status()

            def grab():
                r = requests.get(f"{server}/frame.jpg", headers=headers, timeout=5)
                if r.status_code != 200:
                    return None
                return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            print(f"Using frames from the running backend at {server}")
        except requests.RequestException:
            print("Backend not running -> reading the camera directly")
    if grab is None:
        source = args.webcam or args.stream or s.stream_url
        cam = FrameSource(source)
        cam.start()
        print(f"Opening camera {source} ...")

        def grab():
            got = cam.wait_new(0, timeout=10)
            return cam.latest()[0] if got else None

    rec = make_recognizer(s.face_backend, s.models_dir)
    db = FaceDB(rec, s.faces_dir, s.face_tolerance, s.process_width)
    folder = s.faces_dir / name
    folder.mkdir(parents=True, exist_ok=True)

    print(f"Look at the camera, {name}. Capturing {args.count} photos...")
    saved, attempts = 0, 0
    while saved < args.count and attempts < args.count * 10:
        attempts += 1
        frame = grab()
        if frame is None:
            print("  no frame (camera offline?)")
            time.sleep(1)
            continue
        n = db.count_faces(frame)
        if n != 1:
            print(f"  {n} faces in view - need exactly 1")
            time.sleep(0.3)
            continue
        path = folder / f"{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        cv2.imencode(".jpg", frame)[1].tofile(str(path))
        saved += 1
        print(f"  saved {saved}/{args.count}: {path.name}")
        time.sleep(args.interval)

    print(f"Saved {saved} photos to {folder}")
    try:
        r = requests.post(f"{server}/api/faces/reload", headers=headers, timeout=60)
        print("Backend reloaded faces:", r.json().get("loaded") if r.ok else f"HTTP {r.status_code}")
    except requests.RequestException:
        print("Backend not running; faces will be loaded when it starts.")


if __name__ == "__main__":
    main()
