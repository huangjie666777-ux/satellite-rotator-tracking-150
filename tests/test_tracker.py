import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.passes import PassInterval
from app.rotctl import RotctlClient, RotctlError
from app.rotor_sim import RotorState, _Handler
from app.player import controller

client = TestClient(app)


def track_request():
    return json.loads(Path("examples/track_request.json").read_text())


def test_plan_ok_north_crossing():
    r = client.post("/api/track/plan", json=track_request())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target_count"] == len(d["targets"]) > 0
    track = [t for t in d["targets"] if t["phase"] == "track"]
    azs = [t["az_deg"] for t in track]
    # azimuth unwraps across north without a flip: continuous, exceeds 360
    assert max(azs) > 360.0
    for a, b in zip(azs, azs[1:]):
        assert abs(b - a) < 10.0
    # relative time: preset starts at 0, phases are ordered
    assert d["targets"][0]["t_rel_s"] == 0.0
    assert track[0]["t_rel_s"] == pytest.approx(30.0)
    phases = [t["phase"] for t in d["targets"]]
    assert phases == sorted(phases, key={"preset": 0, "track": 1,
                                         "park": 2}.get)
    # 1 s grid on the track phase (except the fractional tail)
    dts = [b["t_rel_s"] - a["t_rel_s"] for a, b in zip(track, track[1:])]
    assert all(dt == pytest.approx(1.0) for dt in dts[:-1])
    assert 0.0 < dts[-1] <= 1.0


def test_plan_interval_index_out_of_range():
    req = track_request()
    req["interval_index"] = 99
    r = client.post("/api/track/plan", json=req)
    assert r.status_code == 422
    assert "out of range" in r.json()["detail"]


def test_plan_infeasible_rate_rejected():
    req = track_request()
    req["mechanics"]["max_az_rate_dps"] = 0.01  # far below track rate
    r = client.post("/api/track/plan", json=req)
    assert r.status_code == 422
    assert "rate" in r.json()["detail"]


def test_plan_el_limit_rejected():
    req = track_request()
    req["mechanics"]["el_min_deg"] = 50.0  # pass only reaches ~34 deg
    req["mechanics"]["current_el_deg"] = 50.0
    req["mechanics"]["park_el_deg"] = 50.0
    r = client.post("/api/track/plan", json=req)
    assert r.status_code == 422
    assert "elevation" in r.json()["detail"]


def test_mechanics_validation():
    req = track_request()
    req["mechanics"]["az_max_deg"] = 800.0  # span > 720
    assert client.post("/api/track/plan", json=req).status_code == 422
    req = track_request()
    req["mechanics"]["el_max_deg"] = 120.0  # elevation beyond [0, 90]
    assert client.post("/api/track/plan", json=req).status_code == 422
    req = track_request()
    req["mechanics"]["preset_s"] = 0.0  # seconds must be positive
    assert client.post("/api/track/plan", json=req).status_code == 422
    req = track_request()
    req["mechanics"]["max_az_rate_dps"] = "inf"
    assert client.post("/api/track/plan", json=req).status_code == 422
    req = track_request()
    req["mechanics"]["current_az_deg"] = 999.0  # outside limits
    assert client.post("/api/track/plan", json=req).status_code == 422


def test_segment_over_30min_rejected():
    from datetime import timedelta
    from app.passes import HorizonMask, Site
    from app.tle import parse_tle
    from app.tracker import MechLimits, PlanError, plan_track
    req = json.loads(Path("examples/request.json").read_text())
    sat = req["satellites"][0]
    tle = parse_tle(sat["tle_line1"], sat["tle_line2"])
    st = req["stations"][1]
    site = Site(st["id"], st["lat_deg"], st["lon_deg"], st["alt_m"],
                HorizonMask(None))
    iv = PassInterval(start=tle.epoch, end=tle.epoch + timedelta(minutes=31),
                      duration_s=31 * 60.0, truncated_at_start=False,
                      truncated_at_end=False, max_el_deg=10.0,
                      max_el_time=tle.epoch)
    lim = MechLimits(-90.0, 450.0, 0.0, 90.0, 10.0, 10.0)
    with pytest.raises(PlanError, match="exceeds"):
        plan_track(tle.satrec, site, iv, lim, (180.0, 10.0), (180.0, 10.0),
                   30.0, 30.0)


def test_csv_fractional_tail():
    req = json.loads(Path("examples/request.json").read_text())
    r = client.post("/api/passes/download", json=req)
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    summary = json.loads(zf.read("summary.json"))
    name = next(n for n in zf.namelist() if n.endswith("_000.csv"))
    first = zf.read(name).decode().splitlines()
    last_stamp = first[-1].split(",")[0]
    # interval end has a fractional second; the tail row must be kept
    end = summary["intervals"][0]["end"]
    assert "." in end
    assert last_stamp.rstrip("Z").startswith(end.rstrip("Z")[:19])
    assert "." in last_stamp


def test_epoch_window_end_beyond_7_days_rejected():
    req = json.loads(Path("examples/request.json").read_text())
    # start within 7 days of epoch, end beyond: must be rejected
    req["window"] = {"start": "2024-01-07T13:00:00Z",
                     "end": "2024-01-08T13:00:00Z"}
    r = client.post("/api/passes", json=req)
    assert r.status_code == 422
    assert "7 days" in r.json()["detail"]


def test_bisect_falling_direction():
    # falling-edge crossing must converge from the visible side
    from datetime import datetime, timedelta, timezone
    from app.passes import HorizonMask, Site, _bisect_crossing, _margin
    from app.tle import parse_tle
    req = json.loads(Path("examples/request.json").read_text())
    sat = req["satellites"][0]
    tle = parse_tle(sat["tle_line1"], sat["tle_line2"])
    st = req["stations"][1]
    site = Site(st["id"], st["lat_deg"], st["lon_deg"], st["alt_m"],
                HorizonMask(None))
    # known pass end 2024-01-01T02:33:03.375Z; bracket the falling edge
    t_vis = datetime(2024, 1, 1, 2, 33, 3, tzinfo=timezone.utc)
    t_inv = t_vis + timedelta(seconds=1)
    assert _margin(tle.satrec, site, t_vis) > 0
    assert _margin(tle.satrec, site, t_inv) <= 0
    cross = _bisect_crossing(tle.satrec, site, t_vis, t_inv)
    assert t_vis < cross <= t_inv
    assert abs(_margin(tle.satrec, site, cross)) < 0.05


# ---- rotctld client / playback against the local simulator ----

class SimServer:
    def __init__(self, az=180.0, el=10.0, rate=1000.0):
        import socketserver

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), _Handler)
        self.server.rotor = RotorState(az, el, rate_dps=rate)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sim():
    srv = SimServer()
    yield srv
    srv.stop()


def test_rotctl_client(sim):
    c = RotctlClient("127.0.0.1", sim.port)
    assert c.get_position() == (180.0, 10.0)
    c.set_position(200.0, 20.0)
    c.stop()
    c.close()


def test_rotctl_rprt_error(sim):
    c = RotctlClient("127.0.0.1", sim.port)
    c._send("bogus")
    with pytest.raises(RotctlError, match="RPRT -1"):
        c._check_rprt()
    c.close()


def _wait_state(timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = controller.status()
        if st.state != "running":
            return st
        time.sleep(0.05)
    return controller.status()


def test_playback_done(sim):
    from app.tracker import TrackTarget
    targets = [TrackTarget(t_rel_s=0.0, az_deg=180.0, el_deg=10.0,
                           phase="preset"),
               TrackTarget(t_rel_s=0.2, az_deg=181.0, el_deg=10.5,
                           phase="track"),
               TrackTarget(t_rel_s=0.4, az_deg=182.0, el_deg=11.0,
                           phase="park")]
    controller.start(targets, "127.0.0.1", sim.port)
    st = _wait_state()
    assert st.state == "done", st.detail
    assert st.targets_sent == 3
    assert st.final_position is not None


def test_playback_start_position_mismatch(sim):
    from app.tracker import TrackTarget
    targets = [TrackTarget(t_rel_s=0.0, az_deg=0.0, el_deg=80.0,
                           phase="preset")]
    controller.start(targets, "127.0.0.1", sim.port)
    st = _wait_state()
    assert st.state == "error"
    assert "differs from plan start" in st.detail


def test_playback_cancel(sim):
    from app.tracker import TrackTarget
    targets = [TrackTarget(t_rel_s=float(i), az_deg=180.0, el_deg=10.0,
                           phase="track") for i in range(30)]
    controller.start(targets, "127.0.0.1", sim.port)
    time.sleep(0.3)
    assert controller.cancel()
    st = _wait_state()
    assert st.state == "cancelled"
    assert st.targets_sent < 30


def test_playback_exclusive(sim):
    from app.tracker import TrackTarget
    targets = [TrackTarget(t_rel_s=float(i), az_deg=180.0, el_deg=10.0,
                           phase="track") for i in range(30)]
    controller.start(targets, "127.0.0.1", sim.port)
    with pytest.raises(RuntimeError):
        controller.start(targets, "127.0.0.1", sim.port)
    controller.cancel()
    _wait_state()
