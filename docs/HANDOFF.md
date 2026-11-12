# Handoff: current state (2026-11-13)

Read this first when continuing on another computer or in a new chat session.

## Status
| Part | State |
|---|---|
| Backend + web app | Done and tested with simulators (15 pytest tests pass, end-to-end with fake ESP32/Arduino). |
| Arduino Uno | Door sketch flashed and working: HC-SR04 on D6 (Trig) / D5 (Echo) auto-detected, debounce and 15 s cooldown verified. |
| ESP32-CAM | Flashed and working: camera OK, PSRAM OK, joins WiFi **`Sid `** (router name ends with a space!), stream at `http://192.168.0.153:81/stream` (IP may change). |
| Not done yet | Backend never reached the real ESP32 (the desktop PC was on a different router), Arduino not yet in final wiring, no full real-hardware door check yet. |

## What we learned (hardware quirks)
- ESP32-CAM clone needs **Flash Mode DIO, 40 MHz** (QIO/80 MHz = reboot loop). `tools/flash.py` already uses this.
- It browned out when powered via breadboard rails: ESP32 5V/GND go **directly to the Uno's 5V/GND pins**; firmware also disables the brownout detector.
- Flashing through the Uno: Uno RESET→GND, ESP U0R→D0, U0T→D1, IO0→GND, press ESP RST. The IO0 jumper is often loose: if `boot:0x13` shows up it is NOT grounded (flash mode = `boot:0x3`).
- ESP32 prints `LOG:` status lines and `IP:<addr>` at 9600 baud (the Arduino ignores them): `python tools/flash.py monitor --port COMx`.
- No resistors: Arduino D11 → ESP U0R uses open-drain signalling (direct wire).
- The ESP32 sends `/trigger` to whichever computer watches its `/stream`, so the backend laptop's IP does not need to be in `secrets.h`.
- WiFi on the ESP32: `"Sid "` (main) and `esports` (phone hotspot, backup). Edit `firmware/esp32_cam/esp32_cam/secrets.h`.

## Next steps
1. Laptop joins WiFi **`Sid `** or `Sid_5G` (same router, so same network as the ESP32).
2. Set `STREAM_URL=http://<esp32-ip>:81/stream` in `.env` (IP shows in the `LOG:` lines or on the router's client list).
3. `python -m backend`, open `http://localhost:8000/?token=<API_TOKEN from .env>`; check the live video.
4. Allow Python through the laptop firewall (Windows: run `tools\open_firewall.bat` as administrator, or click Allow on the prompt; macOS: allow incoming connections for Python).
5. Re-wire the Uno to the final layout: remove Uno RESET→GND and ESP IO0 wires; ESP U0T → Uno D10, ESP U0R → Uno D11; Uno D0/D1 empty. Press ESP RST.
6. Enrol a face (web app → Faces → Capture from door camera), then walk up to the sensor: expect `KNOWN:<name>` / `UNKNOWN` in the web app and on the Uno serial monitor (115200).
7. Later: add LCD (SDA→A4, SCL→A5) and buzzer (D8).

Wiring diagram: https://claude.ai/artifact/WJ99P8ZjTXY4A3Yk5qa398 (also docs/wiring.md).
