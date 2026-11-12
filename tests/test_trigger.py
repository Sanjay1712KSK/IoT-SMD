"""Trigger logic tests with a fake camera and fake recognizer (no hardware, no ML models needed)."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.camera import split_jpegs
from backend.config import Settings
from backend.core import DoorMonitor
from backend.events import EventStore
from backend.recognizer import Face

FRAME = np.zeros((240, 320, 3), np.uint8)
ALICE = Face((10, 10, 50, 50), "Alice", 0.3)
STRANGER = Face((100, 10, 50, 50), None, 0.9)


class FakeFrames:
    """Serves a fixed list of frames; wait_new() hands out the next one until they run out."""
    def __init__(self, count=1, online=True):
        self.count, self.online, self.i = count, online, 0
        self.last_error = ""

    def latest(self):
        return (FRAME, 1) if self.online else (None, 0)

    def wait_new(self, after_seq, timeout):
        if after_seq >= self.count:
            return None
        return FRAME, after_seq + 1


class FakeFaceDB:
    """Returns the scripted face lists in order, one per identify() call (last one repeats)."""
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def identify(self, frame):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, faces, frames=None, cooldown=15.0):
    alerts, clock = [], Clock()
    mon = DoorMonitor(frames or FakeFrames(), faces, EventStore(tmp_path / "ev.db"), tmp_path / "snaps",
                      cooldown_s=cooldown, check_window_s=1.0, alerter=lambda msg, img: alerts.append((msg, img)),
                      clock=clock)
    return mon, alerts, clock


def test_known_person(tmp_path):
    mon, alerts, _ = make(tmp_path, FakeFaceDB([ALICE]))
    assert mon.check(55) == "KNOWN:Alice"
    [ev] = mon.events.since()
    assert ev["kind"] == "known" and ev["message"] == "Alice is at the door" and ev["distance"] == 55
    assert ev["snapshot"] and (tmp_path / "snaps" / ev["snapshot"]).exists()
    assert alerts == []  # no alert for known people


def test_known_wins_over_unknown_in_same_frame(tmp_path):
    mon, _, _ = make(tmp_path, FakeFaceDB([STRANGER, ALICE]))
    assert mon.check(40) == "KNOWN:Alice"


def test_known_found_in_later_frame(tmp_path):
    # First frame: bad angle (looks unknown). Second frame: recognised -> must be KNOWN, not UNKNOWN.
    mon, alerts, _ = make(tmp_path, FakeFaceDB([STRANGER], [ALICE]), frames=FakeFrames(count=3))
    assert mon.check(40) == "KNOWN:Alice"
    assert alerts == []


def test_unknown_person(tmp_path):
    mon, alerts, _ = make(tmp_path, FakeFaceDB([STRANGER]), frames=FakeFrames(count=3))
    assert mon.check(60) == "UNKNOWN"
    [ev] = mon.events.since()
    assert ev["kind"] == "unknown"
    assert (tmp_path / "snaps" / ev["snapshot"]).exists()
    assert len(alerts) == 1 and "Unknown person" in alerts[0][0]


def test_no_face(tmp_path):
    mon, alerts, _ = make(tmp_path, FakeFaceDB([]))
    assert mon.check(80) == "NOFACE"
    [ev] = mon.events.since()
    assert ev["kind"] == "noface" and ev["snapshot"] is None
    assert alerts == []


def test_camera_offline_gives_err(tmp_path):
    mon, _, _ = make(tmp_path, FakeFaceDB([ALICE]), frames=FakeFrames(online=False))
    assert mon.check(50) == "ERR"
    assert mon.events.since()[0]["kind"] == "error"


def test_cooldown_suppresses_duplicate_alerts(tmp_path):
    mon, alerts, clock = make(tmp_path, FakeFaceDB([STRANGER]), cooldown=15)
    assert mon.check(50) == "UNKNOWN"
    clock.t += 5                      # within cooldown
    assert mon.check(50) == "UNKNOWN"  # Arduino still gets an answer...
    assert len(mon.events.since()) == 1 and len(alerts) == 1  # ...but no duplicate event/alert
    clock.t += 11                     # 16 s after the first -> cooldown over
    assert mon.check(50) == "UNKNOWN"
    assert len(mon.events.since()) == 2 and len(alerts) == 2


def test_cooldown_is_per_verdict(tmp_path):
    db = FakeFaceDB([STRANGER], [ALICE])
    mon, _, clock = make(tmp_path, db)
    assert mon.check(50) == "UNKNOWN"
    clock.t += 2
    assert mon.check(50) == "KNOWN:Alice"  # a different verdict is logged even inside the cooldown
    assert [e["kind"] for e in mon.events.since()] == ["unknown", "known"]


def test_recognizer_crash_returns_err_via_api(tmp_path):
    class Boom:
        def identify(self, frame):
            raise RuntimeError("model exploded")
    mon, _, _ = make(tmp_path, Boom())
    client = TestClient(create_app(Settings(snapshots_dir=tmp_path / "snaps"), monitor=mon))
    assert client.get("/trigger?d=40").text == "ERR"


def test_names_are_sanitised_for_serial(tmp_path):
    evil = Face((0, 0, 9, 9), "Bob\nUNKNOWN,with-a-very-long-name", 0.2)
    mon, _, _ = make(tmp_path, FakeFaceDB([evil]))
    reply = mon.check(40)
    assert "\n" not in reply and reply.startswith("KNOWN:") and len(reply) <= len("KNOWN:") + 16


# ---------------------------------------------------------------- HTTP API
@pytest.fixture
def client(tmp_path):
    mon, _, _ = make(tmp_path, FakeFaceDB([ALICE]))
    settings = Settings(api_token="s3cret", snapshots_dir=tmp_path / "snaps", faces_dir=tmp_path / "faces")
    return TestClient(create_app(settings, monitor=mon))


def test_trigger_requires_token(client):
    assert client.get("/trigger?d=40").status_code == 401
    assert client.get("/trigger?d=40&token=wrong").status_code == 401
    r = client.get("/trigger?d=40&token=s3cret")
    assert r.status_code == 200 and r.text == "KNOWN:Alice"


def test_events_and_snapshot_download(client):
    client.get("/trigger?d=40", headers={"X-Token": "s3cret"})
    evs = client.get("/events?after=0", headers={"X-Token": "s3cret"}).json()
    assert len(evs) == 1
    assert client.get(f"/events?after={evs[0]['id']}", headers={"X-Token": "s3cret"}).json() == []
    r = client.get(f"/snapshots/{evs[0]['snapshot']}?download=1&token=s3cret")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "attachment" in r.headers["content-disposition"]
    assert client.get("/snapshots/..%2F..%2F.env?token=s3cret").status_code in (400, 404)


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["auth_required"] is True


# ---------------------------------------------------------------- MJPEG parsing
def test_split_jpegs_handles_partial_chunks():
    jpg1, jpg2 = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBBB\xff\xd9"
    stream = b"--frame\r\nheaders\r\n\r\n" + jpg1 + b"\r\n--frame\r\n\r\n" + jpg2
    buf, out = bytearray(), []
    for i in range(0, len(stream), 5):  # feed in awkward 5-byte chunks
        buf += stream[i:i + 5]
        out += split_jpegs(buf)
    assert out == [jpg1, jpg2]


def test_unknown_decided_early_after_confirm_frames(tmp_path):
    db = FakeFaceDB([STRANGER])
    mon, _, _ = make(tmp_path, db, frames=FakeFrames(count=50))
    assert mon.check(50) == "UNKNOWN"
    assert db.calls == DoorMonitor.UNKNOWN_CONFIRM_FRAMES  # didn't keep looking for the whole window
