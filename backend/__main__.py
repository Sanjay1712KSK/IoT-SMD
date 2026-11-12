"""Run with:  python -m backend          (start the server)
           python -m backend --check  (only test that face recognition is installed and working)
"""
import argparse
import logging
import sys

import uvicorn

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend")
    parser.add_argument("--check", action="store_true", help="check the face-recognition install and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    settings = Settings.from_env()

    if args.check:
        from .recognizer import FaceDB, make_recognizer
        rec = make_recognizer(settings.face_backend, settings.models_dir)
        db = FaceDB(rec, settings.faces_dir, settings.face_tolerance, settings.process_width)
        summary = db.reload()
        print(f"OK: backend={rec.name}, tolerance={db.tolerance}, people={summary}")
        sys.exit(0)

    from .app import create_app
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
