"""Pretend to be the Arduino: send "T,<cm>" and print the verdict.

    python tools/fake_arduino.py                 # one trigger at 45 cm via fake_esp32 (tcp://127.0.0.1:3333)
    python tools/fake_arduino.py 70              # distance 70 cm
    python tools/fake_arduino.py --repeat 5 --every 3
    python tools/fake_arduino.py --serial COM6   # talk to a REAL ESP32-CAM through a USB-serial adapter

What the real Arduino would show on its LCD is printed as well.
"""
from __future__ import annotations

import argparse
import socket
import time

REPLY_TIMEOUT_S = 10  # same as REPLY_TIMEOUT_MS in the Arduino sketch


def lcd_for(reply: str | None) -> tuple[str, str]:
    if reply is None:
        return "No server reply", "Laptop/WiFi down"
    if reply.startswith("KNOWN:"):
        return "Welcome,", reply[6:][:16]
    return {"UNKNOWN": ("UNKNOWN PERSON", "Owner alerted"),
            "NOFACE": ("No face seen", "Look at camera"),
            "ERR": ("No server reply", "Server/cam error")}.get(reply, ("?", reply[:16]))


class TcpLine:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=REPLY_TIMEOUT_S)
        self.buf = b""

    def send(self, line: str):
        self.sock.sendall((line + "\n").encode())

    def readline(self, timeout: float) -> str | None:
        self.sock.settimeout(timeout)
        try:
            while b"\n" not in self.buf:
                chunk = self.sock.recv(256)
                if not chunk:
                    return None
                self.buf += chunk
        except socket.timeout:
            return None
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="ignore").strip()


class SerialLine:
    def __init__(self, port: str):
        import serial
        self.port = serial.Serial(port, 9600, timeout=0.2)

    def send(self, line: str):
        self.port.reset_input_buffer()
        self.port.write((line + "\n").encode())

    def readline(self, timeout: float) -> str | None:
        self.port.timeout = timeout
        raw = self.port.readline()
        return raw.decode(errors="ignore").strip() if raw.endswith(b"\n") else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("distance", nargs="?", type=int, default=45)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--serial", help="serial port of a real ESP32-CAM instead of fake_esp32")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--every", type=float, default=3.0, help="seconds between repeats")
    args = ap.parse_args()

    link = SerialLine(args.serial) if args.serial else TcpLine(args.host, args.port)
    for i in range(args.repeat):
        if i:
            time.sleep(args.every)
        msg = f"T,{args.distance}"
        t0 = time.monotonic()
        link.send(msg)
        print(f"-> {msg}")
        deadline = t0 + REPLY_TIMEOUT_S
        reply = None
        while time.monotonic() < deadline:  # skip info lines like "IP:..." just like the sketch does
            line = link.readline(max(0.1, deadline - time.monotonic()))
            if line is None:
                break
            if line.startswith("IP:"):
                print(f"   (camera says {line})")
                continue
            reply = line
            break
        dt = time.monotonic() - t0
        top, bottom = lcd_for(reply)
        print(f"<- {reply or '(timeout)'}   [{dt:.2f}s]")
        print(f"   LCD: |{top:<16}|\n        |{bottom:<16}|")


if __name__ == "__main__":
    main()
