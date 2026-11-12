"""FastAPI web server: web app, live video, Arduino trigger endpoint, events, face management."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse

from .alerts import make_alerter
from .camera import FrameSource
from .config import ROOT, Settings
from .core import DoorMonitor
from .events import EventStore, cleanup_snapshots
from .recognizer import IMAGE_EXTS, FaceDB, downscale, make_recognizer

log = logging.getLogger(__name__)

WEB_DIR = ROOT / "web"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,30}$")
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9_.-]+\.jpg$")


def build_monitor(settings: Settings) -> tuple[DoorMonitor, FrameSource, FaceDB]:
    recognizer = make_recognizer(settings.face_backend, settings.models_dir)
    facedb = FaceDB(recognizer, settings.faces_dir, settings.face_tolerance, settings.process_width)
    facedb.reload()
    frames = FrameSource(settings.stream_url)
    events = EventStore(settings.db_path)
    cleanup_snapshots(settings.snapshots_dir, settings.snapshot_max_age_days)
    monitor = DoorMonitor(
        frames, facedb, events, settings.snapshots_dir,
        cooldown_s=settings.cooldown_s, check_window_s=settings.check_window_s,
        recognize_every_n=settings.recognize_every_n, snapshot_max_age_days=settings.snapshot_max_age_days,
        alerter=make_alerter(settings.alert_webhook, settings.telegram_bot_token, settings.telegram_chat_id))
    return monitor, frames, facedb


def create_app(settings: Settings | None = None, monitor: DoorMonitor | None = None,
               facedb: FaceDB | None = None) -> FastAPI:
    """Build the app. Tests pass in a monitor with fake parts; normally everything is built here."""
    settings = settings or Settings.from_env()
    state: dict = {"monitor": monitor, "facedb": facedb}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["monitor"] is None:
            if not settings.api_token:
                log.warning("API_TOKEN is empty: the web app and /trigger are NOT password protected!")
            mon, frames, fdb = build_monitor(settings)
            frames.start()
            mon.start()
            state.update(monitor=mon, facedb=fdb, frames=frames)
            log.info("Camera source: %s", settings.stream_url)
            log.info("Open http://localhost:%d/?token=<API_TOKEN> in a browser", settings.port)
        yield
        if "frames" in state:
            state["frames"].stop()
            state["monitor"].stop()

    app = FastAPI(title="Smart Door Monitor", lifespan=lifespan)

    def require_token(request: Request) -> None:
        """Shared-secret check: header X-Token or ?token=. Disabled if API_TOKEN is empty."""
        if not settings.api_token:
            return
        given = request.headers.get("x-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(given.encode(), settings.api_token.encode()):
            raise HTTPException(status_code=401, detail="Missing or wrong token")

    def mon() -> DoorMonitor:
        return state["monitor"]

    auth = [Depends(require_token)]

    # ---------------------------------------------------------------- pages / status
    @app.get("/", response_class=HTMLResponse)
    def index():
        # The page itself contains no data; it asks for the token and sends it on every request.
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    def health():
        m, fdb = mon(), state["facedb"]
        return {
            "status": "ok",
            "camera_online": bool(m.frames.online),
            "camera_error": getattr(m.frames, "last_error", ""),
            "face_backend": fdb.rec.name if fdb else None,
            "known_people": fdb.people if fdb else [],
            "auth_required": bool(settings.api_token),
        }

    # ---------------------------------------------------------------- Arduino / ESP32
    @app.get("/trigger", response_class=PlainTextResponse, dependencies=auth)
    def trigger(d: float | None = None):
        # Plain `def` (not async): FastAPI runs it in a worker thread, so the ~1 s recognition
        # doesn't block the video stream or other requests.
        try:
            return mon().check(d)
        except Exception:
            log.exception("Door check crashed")
            return "ERR"

    # ---------------------------------------------------------------- video
    @app.get("/video", dependencies=auth)
    async def video(request: Request):
        boundary = "frame"

        async def gen():
            last_seq = -1
            while not await request.is_disconnected():
                jpg, seq = mon().annotated()
                if seq != last_seq:
                    last_seq = seq
                    yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n"
                           .encode() + jpg + b"\r\n")
                await asyncio.sleep(0.05)  # max ~20 fps to the browser

        return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")

    @app.get("/frame.jpg", dependencies=auth)
    def frame_jpg(raw: bool = True):
        """Latest frame (raw by default). Used by enroll.py so it doesn't fight over the ESP32 stream."""
        if raw:
            frame, _ = mon().frames.latest()
            if frame is None:
                raise HTTPException(503, "Camera offline")
            return Response(cv2.imencode(".jpg", frame)[1].tobytes(), media_type="image/jpeg")
        return Response(mon().annotated()[0], media_type="image/jpeg")

    # ---------------------------------------------------------------- events & snapshots
    @app.get("/events", dependencies=auth)
    def events(after: int = 0, limit: int = 50):
        return mon().events.since(after, limit)

    @app.get("/snapshots/{filename}", dependencies=auth)
    def snapshot(filename: str, download: bool = False):
        if not SNAPSHOT_RE.match(filename):  # blocks ../ path traversal
            raise HTTPException(400, "Bad file name")
        path = settings.snapshots_dir / filename
        if not path.is_file():
            raise HTTPException(404, "Snapshot not found (maybe deleted after the retention period)")
        return FileResponse(path, media_type="image/jpeg",
                            filename=filename if download else None,
                            content_disposition_type="attachment" if download else "inline")

    # ---------------------------------------------------------------- faces
    def person_dir(name: str) -> Path:
        name = name.strip()
        if not NAME_RE.match(name):
            raise HTTPException(400, "Name must be 1-31 letters/digits/space/_ . - and start with a letter or digit")
        return settings.faces_dir / name

    def fdb() -> FaceDB:
        if state["facedb"] is None:
            raise HTTPException(503, "Face database not loaded")
        return state["facedb"]

    def list_faces():
        out = []
        if settings.faces_dir.exists():
            for p in sorted(settings.faces_dir.iterdir()):
                if p.is_dir():
                    n = sum(1 for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTS)
                    out.append({"name": p.name, "photos": n})
        return out

    @app.get("/api/faces", dependencies=auth)
    def get_faces():
        return {"people": list_faces(), "loaded": fdb().people}

    @app.post("/api/faces/reload", dependencies=auth)
    def reload_faces():
        return {"loaded": fdb().reload()}

    @app.post("/api/faces/{name}", dependencies=auth)
    def upload_faces(name: str, files: list[UploadFile] = File(...)):
        folder = person_dir(name)
        saved, rejected = 0, []
        for up in files:
            img = cv2.imdecode(np.frombuffer(up.file.read(), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                rejected.append(f"{up.filename}: not an image")
                continue
            img, _ = downscale(img, 1024)
            if fdb().count_faces(img) != 1:
                rejected.append(f"{up.filename}: needs exactly one face")
                continue
            folder.mkdir(parents=True, exist_ok=True)
            cv2.imencode(".jpg", img)[1].tofile(str(folder / f"{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"))
            saved += 1
        loaded = fdb().reload() if saved else None
        return {"saved": saved, "rejected": rejected, "loaded": loaded}

    @app.post("/api/faces/{name}/capture", dependencies=auth)
    def capture_face(name: str):
        """Save the current camera frame as a training photo (person stands in front of the camera)."""
        folder = person_dir(name)
        frame, _ = mon().frames.latest()
        if frame is None:
            raise HTTPException(503, "Camera offline")
        n = fdb().count_faces(frame)
        if n != 1:
            raise HTTPException(422, f"Need exactly one face in view, found {n}")
        folder.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".jpg", frame)[1].tofile(str(folder / f"{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"))
        return {"saved": 1, "loaded": fdb().reload()}

    @app.delete("/api/faces/{name}", dependencies=auth)
    def delete_face(name: str):
        folder = person_dir(name)
        if not folder.is_dir():
            raise HTTPException(404, "No such person")
        shutil.rmtree(folder)
        return {"deleted": name, "loaded": fdb().reload()}

    return app
