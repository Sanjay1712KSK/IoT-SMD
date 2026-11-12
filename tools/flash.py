"""Compile + upload the firmware with arduino-cli (no Arduino IDE needed).

    python tools/flash.py ports                       # list serial ports / detected boards
    python tools/flash.py arduino --port COM5         # build + upload door_arduino to an Uno
    python tools/flash.py arduino --port COM5 --usb-link   # variant that talks to fake_esp32 over USB
    python tools/flash.py esp32 --port COM6           # build + upload esp32_cam (IO0 to GND + RST first!)
    python tools/flash.py esp32 --build-only          # just check it compiles
    python tools/flash.py monitor --port COM6         # print serial output (9600) - shows "IP:..." from the ESP32

arduino-cli location: env ARDUINO_CLI, else ~/arduino-cli/arduino-cli(.exe) with arduino-cli.yaml next to it,
else `arduino-cli` on PATH. Install it from https://arduino.github.io/arduino-cli/ and run once:
    arduino-cli core install arduino:avr esp32:esp32 && arduino-cli lib install "LiquidCrystal I2C"
(ESP32 core needs the board URL https://espressif.github.io/arduino-esp32/package_esp32_index.json)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARDUINO_SKETCH = ROOT / "firmware" / "arduino" / "door_arduino"
ESP32_SKETCH = ROOT / "firmware" / "esp32_cam" / "esp32_cam"
# ESP32-CAM clones often boot-loop with the default QIO/80 MHz flash settings -> use DIO/40 MHz (always safe).
# DebugLevel=error: only real errors are printed (at boot, before our code switches the UART to 9600).
FQBN = {"arduino": "arduino:avr:uno",
        "esp32": "esp32:esp32:esp32cam:FlashMode=dio,FlashFreq=40,DebugLevel=error"}


def cli() -> list[str]:
    exe = "arduino-cli.exe" if os.name == "nt" else "arduino-cli"
    if os.environ.get("ARDUINO_CLI"):
        return [os.environ["ARDUINO_CLI"]]
    home = Path.home() / "arduino-cli"
    if (home / exe).exists():
        cmd = [str(home / exe)]
        if (home / "arduino-cli.yaml").exists():
            cmd += ["--config-file", str(home / "arduino-cli.yaml")]
        return cmd
    if shutil.which("arduino-cli"):
        return ["arduino-cli"]
    sys.exit("arduino-cli not found (see the top of tools/flash.py)")


def run(args: list[str]) -> None:
    print(">", " ".join(args[-4:]))
    if subprocess.call(cli() + args) != 0:
        sys.exit("FAILED")


def short_build_dir() -> str:
    # Windows' 260-char path limit breaks the ESP32 toolchain when build paths get long.
    d = Path(tempfile.gettempdir()).anchor + "doorbuild" if os.name == "nt" else tempfile.gettempdir() + "/doorbuild"
    return d


def flash(target: str, port: str | None, build_only: bool, usb_link: bool, debug: bool = False) -> None:
    sketch = ARDUINO_SKETCH if target == "arduino" else ESP32_SKETCH
    if target == "esp32" and not (sketch / "secrets.h").exists():
        sys.exit(f"Missing {sketch / 'secrets.h'}: copy secrets.h.example and fill it in.")
    tmp = None
    if usb_link:  # build a copy with ESP_LINK_USB 1
        tmp = Path(tempfile.mkdtemp()) / "door_arduino"
        tmp.mkdir()
        src = (sketch / "door_arduino.ino").read_text(encoding="utf-8")
        (tmp / "door_arduino.ino").write_text(src.replace("#define ESP_LINK_USB 0", "#define ESP_LINK_USB 1"),
                                              encoding="utf-8")
        sketch = tmp
    if debug and target == "esp32":  # copy with DEBUG_SERIAL 1 (115200 baud step-by-step messages)
        tmp = Path(tempfile.mkdtemp()) / "esp32_cam"
        shutil.copytree(sketch, tmp)
        ino = tmp / "esp32_cam.ino"
        ino.write_text("#define DEBUG_SERIAL 1\n" + ino.read_text(encoding="utf-8"), encoding="utf-8")
        sketch = tmp
    build = short_build_dir() + "_" + target + ("_debug" if debug else "")
    run(["compile", "-b", FQBN[target], "--build-path", build, str(sketch)])
    if build_only:
        return
    if not port:
        sys.exit("--port is required to upload (see: python tools/flash.py ports)")
    if target == "esp32":
        print("\nESP32-CAM: make sure it is in flash mode (IO0 to GND, then press RST; MB board: hold IO0, tap RST).")
    extra = ["--upload-property", "upload.speed=115200"] if target == "esp32" else []  # slow = reliable via the Uno
    run(["upload", "-b", FQBN[target], "-p", port, "--input-dir", build, *extra, str(sketch)])
    if target == "esp32":
        print("\nDone. Now remove the IO0-GND link (if any) and press RST. Then: python tools/flash.py monitor --port", port)


def monitor(port: str, baud: int) -> None:
    import serial
    with serial.Serial(port, baud, timeout=0.5) as s:
        print(f"Listening on {port} @ {baud} (Ctrl+C to stop). Press RST on the board to see boot output.")
        try:
            while True:
                line = s.readline()
                if line:
                    print(time.strftime("%H:%M:%S"), line.decode(errors="replace").rstrip())
        except KeyboardInterrupt:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=["arduino", "esp32", "ports", "monitor"])
    ap.add_argument("--port")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--usb-link", action="store_true", help="Arduino: ESP_LINK_USB=1 (for fake_esp32 --serial)")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--debug", action="store_true", help="ESP32: troubleshooting build, messages at 115200")
    a = ap.parse_args()
    if a.target == "ports":
        run(["board", "list"])
    elif a.target == "monitor":
        if not a.port:
            sys.exit("--port required")
        monitor(a.port, a.baud)
    else:
        flash(a.target, a.port, a.build_only, a.usb_link, a.debug)


if __name__ == "__main__":
    main()
