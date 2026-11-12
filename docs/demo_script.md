# 3-minute demo script

## Before the examiner arrives (10 min)
- [ ] Laptop and ESP32-CAM on the same 2.4 GHz WiFi (phone hotspot is the safest choice at college).
- [ ] `python -m backend` running; `http://localhost:8000/?token=...` open, **🔔 Enable alerts** clicked, Notification permission allowed.
- [ ] Status badge says **camera online**. Live feed visible.
- [ ] You are enrolled (Faces tab shows your name with 5+ photos). A friend who is *not* enrolled is ready to be the "stranger".
- [ ] Arduino powered, LCD shows `Smart Door / Dist: ...`. The ESP32 IP was shown on the LCD at boot.
- [ ] Laptop volume up. Phone with the web app open as a second screen (optional).
- [ ] Backup plan: if a board fails, run `python tools/fake_esp32.py` + `python tools/fake_arduino.py 45` (same backend, same web app).

## The demo

**0:00 – 0:30 · Architecture (point at the board, then the README diagram)**
> "The Arduino is the brain: it measures distance, debounces and decides *when* to check. The ESP32-CAM only provides the camera and WiFi. Face recognition runs on the laptop because it needs real compute. The web app is the owner's view."

**0:30 – 1:15 · Known person**
1. Walk up to the door to about 50 cm.
2. Point out the LCD: `Checking face...` → `Welcome, <your name>`, short chirp.
3. On the laptop: green box with your name on the live feed; event log: **"<name> is at the door"** with a thumbnail.
> "Three readings under 100 cm in a row trigger it, so a hand waving past does not."

**1:15 – 2:15 · Unknown person**
1. Step away. Your friend walks up.
2. LCD: `UNKNOWN PERSON / Owner alerted`, buzzer alarm.
3. Laptop: red banner **"Unknown person at your door"**, alarm sound, desktop notification, red entry in the log.
4. Click **Download image** to show the saved snapshot.
5. (If Telegram is set up) show the phone receiving the photo.

**2:15 – 2:40 · Robustness**
- Friend stays in front: no new alert → *"15 s cooldown on the Arduino and duplicate suppression on the server."*
- Optional: unplug the ESP32 → badge turns **camera offline**, feed shows NO SIGNAL; plug it back in → it reconnects by itself. A check meanwhile gives `No server reply` on the LCD.

**2:40 – 3:00 · Enrolment + wrap-up**
- Faces tab: type the friend's name, click **Capture from door camera** 3–4 times → friend is now recognised ("no restart needed").
> "History is stored in SQLite, snapshots are deleted after 7 days, and access needs a token."

---

## Likely viva questions

**Why is the Arduino the brain and not the ESP32?**
The spec separates concerns: the Arduino does real-time sensing (ultrasonic timing, debounce, cooldown) and the user feedback (LCD, buzzer). The ESP32-CAM is busy with the camera and WiFi; keeping it a "dumb relay" makes it simpler and means the decision logic can be tested and changed without reflashing the camera. It also shows a classic IoT pattern: microcontroller + connectivity module.

**Why does recognition run on the laptop?**
Face recognition needs a neural network for detection and one for embeddings (millions of operations per frame). The Uno has 2 KB RAM; the ESP32 has 520 KB + 4 MB PSRAM and could only do tiny, inaccurate models. The laptop does it in ~15 ms per frame. This is "edge sensing, local server compute" and keeps data on the home network.

**How does the recognition work?**
1) A detector (YuNet, or dlib HOG) finds faces. 2) Each face is aligned using eye positions and turned into a 128-number "embedding" by a neural network (SFace, or dlib ResNet). 3) We compare it to the embeddings of the enrolled photos. Nearest one closer than the tolerance → known; else unknown.

**What is the tolerance and how did you choose it?**
The maximum distance between two embeddings that still counts as the same person. For OpenCV SFace we use 1 − cosine similarity ≤ 0.60 (OpenCV's recommended limit is 0.637; ours is slightly stricter). For dlib, 0.6 Euclidean is the library default. Lower = fewer strangers accepted (false accept) but more family members rejected (false reject). For a security product we prefer false *rejects*: the cost is a harmless alert.

**How accurate is it?**
The published models reach ~99% on the LFW benchmark, but real-world accuracy depends on light, angle, and camera quality. We improve it by: several enrolment photos per person, checking several frames per trigger (one match = known; UNKNOWN only after 3 frames agree), and downscaling consistently.

**How do you avoid false triggers and duplicate alerts?**
Median of 5 ultrasonic readings removes spikes; 3 consecutive close readings are required; 15 s cooldown on the Arduino; and the server logs/alerts the same verdict at most once per 15 s.

**Why the 1k/2k resistor divider?**
The Uno's TX is 5 V, the ESP32's RX is 3.3 V max. The divider gives 5 × 2/3 = 3.33 V. The other direction (3.3 V into the Uno) is read as HIGH, so no shifter needed.

**What happens when something fails?**
Camera stream drops → backend reconnects with back-off, UI shows "camera offline", checks return ERR. Laptop unreachable → ESP32 answers ERR; if the ESP32 is dead, the Arduino times out after 10 s; both show "No server reply". No face visible → NOFACE, "Look at camera".

**How fast is a check?**
Known: ~50 ms of server time; unknown: ~0.1–0.3 s; no face: 1 s (we wait in case the person turns). Plus WiFi round-trip. The live feed only recognises every 3rd frame to save CPU.

**Privacy considerations?**
Face images and history are biometric personal data. Everything stays on the local laptop (no cloud) unless Telegram is enabled. Snapshots auto-delete after 7 days; enrolled photos can be deleted from the Faces page; access needs a token; the repo's .gitignore excludes photos, snapshots and secrets. Visitors should be informed (a "CCTV in operation" sign), and in many countries (e.g. GDPR) only the owner's own doorway may be recorded.

**Security weaknesses?**
Plain HTTP and a shared token (could be sniffed on the LAN); a printed photo could fool 2D recognition (no liveness check); the ESP32 stream itself is unauthenticated on the LAN.

**Limitations and future work?**
Liveness detection (blink / depth), IR night vision, HTTPS + user accounts, a smart lock actuator for known people, running a small model on the ESP32-S3 for offline fallback, mobile app with push via FCM, multi-camera support.

**Why MJPEG and not RTSP/H.264?**
The ESP32-CAM's sensor outputs JPEG in hardware, so MJPEG costs the ESP32 almost nothing, and browsers display it natively in an `<img>` tag. H.264 would need encoding the ESP32 can't do.

**Why SQLite?**
Zero setup, a single file, survives restarts, more than fast enough for a few events per minute.
