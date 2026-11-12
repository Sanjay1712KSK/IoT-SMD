"""Face detection + recognition behind one small interface.

Two interchangeable backends:
  * DlibRecognizer   - the `face_recognition` library (dlib). Classic, but dlib needs CMake +
                       a C++ compiler to install, which often fails on Windows / new Pythons.
  * OpenCVRecognizer - OpenCV's YuNet (detector) + SFace (recogniser) ONNX models. Only needs
                       `opencv-python`, so it installs everywhere. Models auto-download (~40 MB).

Both turn a face into an "embedding" (a vector of numbers). Two photos of the same person give
embeddings that are close together. We compare a new face against every enrolled photo and take the
nearest one; if its distance is below the tolerance, it's a match, otherwise "unknown".

FaceDB is the only class the rest of the app uses; it doesn't care which backend is underneath.
"""
from __future__ import annotations

import logging
import shutil
import threading
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x, y, w, h in pixels
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class Face:
    box: Box
    name: str | None      # None = unknown
    distance: float       # distance to the closest enrolled face (lower = more similar)


class Recognizer(ABC):
    name = "base"
    default_tolerance = 0.5

    @abstractmethod
    def detect_and_embed(self, bgr: np.ndarray) -> list[tuple[Box, np.ndarray]]:
        """Find every face in a BGR image and return (box, embedding) for each."""

    @abstractmethod
    def distances(self, emb: np.ndarray, known: np.ndarray) -> np.ndarray:
        """Distance from one embedding to each row of `known` (lower = more similar)."""


class DlibRecognizer(Recognizer):
    name = "dlib (face_recognition)"
    default_tolerance = 0.6  # library default; euclidean distance between 128-d encodings

    def __init__(self) -> None:
        import face_recognition  # noqa: F401  (raises ImportError if dlib is not installed)

        self.fr = face_recognition

    def detect_and_embed(self, bgr):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        locations = self.fr.face_locations(rgb, model="hog")  # HOG = fast enough on a CPU
        encodings = self.fr.face_encodings(rgb, locations)
        return [((left, top, right - left, bottom - top), enc)
                for (top, right, bottom, left), enc in zip(locations, encodings)]

    def distances(self, emb, known):
        return np.linalg.norm(known - emb, axis=1)


MODEL_URLS = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def ensure_models(models_dir: Path) -> None:
    """Download the OpenCV ONNX models once if they are missing."""
    models_dir.mkdir(parents=True, exist_ok=True)
    for filename, url in MODEL_URLS.items():
        target = models_dir / filename
        if target.exists() and target.stat().st_size > 100_000:
            continue
        log.info("Downloading %s ...", filename)
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        tmp.replace(target)


class OpenCVRecognizer(Recognizer):
    name = "opencv (YuNet + SFace)"
    # We use distance = 1 - cosine similarity. OpenCV's suggested SFace match threshold is
    # cosine >= 0.363, i.e. distance <= 0.637. 0.60 is slightly stricter (fewer false "known").
    default_tolerance = 0.60

    def __init__(self, models_dir: Path) -> None:
        ensure_models(models_dir)
        self.detector = cv2.FaceDetectorYN.create(
            str(models_dir / "face_detection_yunet_2023mar.onnx"), "", (320, 320),
            0.8,   # score threshold: ignore weak detections
            0.3,   # NMS threshold
            5000)
        self.sface = cv2.FaceRecognizerSF.create(str(models_dir / "face_recognition_sface_2021dec.onnx"), "")

    def detect_and_embed(self, bgr):
        h, w = bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(bgr)
        results = []
        for f in (faces if faces is not None else []):
            aligned = self.sface.alignCrop(bgr, f)            # rotate/crop using the eye landmarks
            emb = self.sface.feature(aligned).flatten().astype(np.float32)
            emb /= np.linalg.norm(emb) + 1e-9                 # unit length -> dot product = cosine
            x, y, fw, fh = (int(v) for v in f[:4])
            results.append(((max(x, 0), max(y, 0), fw, fh), emb))
        return results

    def distances(self, emb, known):
        return 1.0 - known @ emb


def make_recognizer(backend: str, models_dir: Path) -> Recognizer:
    """Pick a backend. 'auto' prefers dlib and falls back to OpenCV if dlib can't be imported."""
    if backend in ("auto", "dlib"):
        try:
            rec = DlibRecognizer()
            log.info("Face backend: %s", rec.name)
            return rec
        except Exception as exc:  # ImportError, or dlib's own errors when models are missing
            if backend == "dlib":
                raise RuntimeError(
                    "FACE_BACKEND=dlib but face_recognition/dlib is not usable: "
                    f"{exc}. Install it (see README) or set FACE_BACKEND=opencv.") from exc
            log.warning("face_recognition/dlib not available (%s) -> using OpenCV YuNet + SFace.", exc)
    rec = OpenCVRecognizer(models_dir)
    log.info("Face backend: %s", rec.name)
    return rec


def downscale(img: np.ndarray, max_width: int) -> tuple[np.ndarray, float]:
    """Shrink an image to at most max_width (keeps aspect). Returns (image, scale_to_original)."""
    h, w = img.shape[:2]
    if w <= max_width:
        return img, 1.0
    scale = max_width / w
    return cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA), 1.0 / scale


def read_image(path: Path) -> np.ndarray | None:
    """cv2.imread can't open non-ASCII paths on Windows; decoding from bytes works everywhere."""
    try:
        return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, cv2.error):
        return None


def largest(found: list[tuple[Box, np.ndarray]]) -> tuple[Box, np.ndarray]:
    return max(found, key=lambda item: item[0][2] * item[0][3])


class FaceDB:
    """Enrolled faces (from faces/<name>/*.jpg) + identification of faces in new frames.

    Thread safety: OpenCV DNN nets and dlib are not safe to call from two threads at once,
    so every call into the recognizer goes through self._lock.
    """

    def __init__(self, recognizer: Recognizer, faces_dir: Path,
                 tolerance: float | None = None, process_width: int = 480) -> None:
        self.rec = recognizer
        self.faces_dir = faces_dir
        self.tolerance = tolerance if tolerance is not None else recognizer.default_tolerance
        self.process_width = process_width
        self._lock = threading.Lock()
        self._names: list[str] = []
        self._embs = np.zeros((0, 1), np.float32)

    def embed_largest(self, bgr: np.ndarray) -> tuple[Box, np.ndarray] | None:
        with self._lock:
            found = self.rec.detect_and_embed(bgr)
        return largest(found) if found else None

    def count_faces(self, bgr: np.ndarray) -> int:
        small, _ = downscale(bgr, self.process_width)
        with self._lock:
            return len(self.rec.detect_and_embed(small))

    def reload(self) -> dict[str, int]:
        """(Re)build the database from disk. Safe to call while the app is running."""
        names, embs, summary = [], [], {}
        self.faces_dir.mkdir(parents=True, exist_ok=True)
        for person in sorted(p for p in self.faces_dir.iterdir() if p.is_dir()):
            summary[person.name] = 0
            for img_path in sorted(person.iterdir()):
                if img_path.suffix.lower() not in IMAGE_EXTS:
                    continue
                img = read_image(img_path)
                if img is None:
                    log.warning("Could not read %s", img_path)
                    continue
                hit = self.embed_largest(downscale(img, 800)[0])
                if hit is None:
                    log.warning("No face found in %s (skipped)", img_path)
                    continue
                names.append(person.name)
                embs.append(hit[1])
                summary[person.name] += 1
        with self._lock:  # swap in the new data atomically
            self._names = names
            self._embs = np.array(embs, np.float32) if embs else np.zeros((0, 1), np.float32)
        log.info("Loaded %d face photos for %d people: %s", len(names), len(summary), summary)
        return summary

    @property
    def people(self) -> list[str]:
        return sorted(set(self._names))

    def identify(self, bgr: np.ndarray) -> list[Face]:
        """Detect + recognise all faces in a frame. Boxes are returned in original-frame pixels."""
        small, scale = downscale(bgr, self.process_width)
        with self._lock:
            found = self.rec.detect_and_embed(small)
            names, embs = self._names, self._embs
        faces = []
        for (x, y, w, h), emb in found:
            name, dist = None, float("inf")
            if len(names):
                d = self.rec.distances(emb, embs)
                i = int(np.argmin(d))
                dist = float(d[i])
                if dist <= self.tolerance:
                    name = names[i]
            box = (int(x * scale), int(y * scale), int(w * scale), int(h * scale))
            faces.append(Face(box, name, dist))
        return faces
