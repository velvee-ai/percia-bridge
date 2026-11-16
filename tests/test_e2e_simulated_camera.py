"""End to end with a SIMULATED camera, no hardware and no network beyond 127.0.0.1.

  looping test clip --ffmpeg--> local MediaMTX "camera" (RTSP)  --percia-bridge-->  local MediaMTX "Percia media" (WHIP)
                                                                       |                      |
                                                                       +-- fake Percia API <--+  (MediaMTX asks it to authorise every publish,
                                                                                                  exactly like the real /api/internal/media/auth)

Covers add > register > publish, idempotent re-registration, token rotation and revoke while running, camera outage,
transcoding of an H.265 camera, the older claim flow, and that no secret reaches the logs.
Needs ffmpeg >= 7.1 (whip muxer), ffprobe and mediamtx on PATH; skipped otherwise. Run: pytest tests/test_e2e_simulated_camera.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fakes import KEY, FakePercia

from percia_bridge import cli

pytestmark = pytest.mark.e2e

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _ffmpeg_has_whip() -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    return " whip " in subprocess.run([exe, "-hide_banner", "-muxers"], capture_output=True, text=True).stdout


if not (shutil.which("mediamtx") and shutil.which("ffprobe") and _ffmpeg_has_whip()):
    pytest.skip("needs mediamtx, ffprobe and an ffmpeg with the whip muxer on PATH", allow_module_level=True)


def free_port(udp=False) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(what, cond, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except Exception:  # noqa: BLE001
            v = False
        if v:
            return v
        time.sleep(0.4)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what}")


class Rig:
    """Everything outside the bridge: the simulated camera, the fake Percia media server and API."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.procs: list[subprocess.Popen] = []
        self.cam_rtsp, self.media_rtsp = free_port(), free_port()
        self.whep_http, self.whep_udp = free_port(), free_port(udp=True)
        self.percia = FakePercia()
        self.percia.push = True
        self.percia.whip_base = f"http://127.0.0.1:{self.whep_http}"
        self.clip = tmp / "clip.mp4"
        self._clip("libx264", ["-profile:v", "baseline", "-bf", "0"])
        (tmp / "cam.yml").write_text(f"""logLevel: warn
rtspAddress: 127.0.0.1:{self.cam_rtsp}
rtspTransports: [tcp]
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
api: no
metrics: no
playback: no
paths:
  cam: {{}}
""")
        (tmp / "media.yml").write_text(f"""logLevel: warn
rtspAddress: 127.0.0.1:{self.media_rtsp}
rtspTransports: [tcp]
rtmp: no
hls: no
srt: no
moq: no
api: no
metrics: no
playback: no
webrtc: yes
webrtcAddress: 127.0.0.1:{self.whep_http}
webrtcLocalUDPAddress: 127.0.0.1:{self.whep_udp}
webrtcLocalTCPAddress: ""
webrtcIPsFromInterfaces: no
webrtcAdditionalHosts: [127.0.0.1]
authMethod: http
authHTTPAddress: {self.percia.url}/api/internal/media/auth
paths:
  all_others: {{}}
""")
        self.spawn(["mediamtx", str(tmp / "cam.yml")])
        self.spawn(["mediamtx", str(tmp / "media.yml")])
        time.sleep(1.5)
        self.camera = None
        self.start_camera()

    def _clip(self, codec, extra):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-t", "4", "-c:v", codec, *extra,
                        "-g", "30", "-pix_fmt", "yuv420p", str(self.clip)], check=True)

    def spawn(self, cmd, **kw):
        p = subprocess.Popen(cmd, stdout=kw.pop("stdout", subprocess.DEVNULL), stderr=kw.pop("stderr", subprocess.DEVNULL), **kw)
        self.procs.append(p)
        return p

    def start_camera(self):
        self.stop_camera()
        self.camera = self.spawn(["ffmpeg", "-v", "error", "-re", "-stream_loop", "-1", "-i", str(self.clip), "-c", "copy", "-f", "rtsp",
                                  "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{self.cam_rtsp}/cam"])
        time.sleep(1.5)

    def stop_camera(self):
        if self.camera and self.camera.poll() is None:
            self.camera.kill()
            self.camera.wait()

    def use_hevc_camera(self):
        self._clip("libx265", ["-x265-params", "log-level=error"])
        self.start_camera()

    @property
    def camera_url(self):
        return f"rtsp://user:camsecret@127.0.0.1:{self.cam_rtsp}/cam"

    def percia_side(self, cam_id):
        """codec,width,height of what arrived on the Percia media server (read back over RTSP)."""
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0", "-show_entries", "stream=codec_name,profile,width,height",
                            "-of", "csv=p=0", f"rtsp://127.0.0.1:{self.media_rtsp}/cam/{cam_id}"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.percia.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


class Install:
    """A bridge install: config dir, CLI calls (in process) and `run` as a real subprocess."""

    def __init__(self, rig: Rig, name="install"):
        self.rig, self.dir = rig, rig.tmp / name
        self.health_port = free_port()
        self.cfg = self.dir / "config.yaml"
        self.env = {**os.environ, "PERCIA_BRIDGE_CONFIG": str(self.cfg), "PERCIA_BRIDGE_HEARTBEAT_S": "1", "PERCIA_BRIDGE_REVOKED_POLL_S": "1",
                    "PYTHONPATH": SRC, "TESTKEY": KEY}
        self.env.pop("PERCIA_API_KEY", None)
        self.proc = None
        self.log = None

    def cli(self, *argv):
        old = {k: os.environ.get(k) for k in ("PERCIA_BRIDGE_CONFIG", "PERCIA_API_KEY", "PERCIA_BRIDGE_STATE")}
        os.environ["PERCIA_BRIDGE_CONFIG"] = str(self.cfg)
        os.environ.pop("PERCIA_API_KEY", None)
        os.environ.pop("PERCIA_BRIDGE_STATE", None)
        try:
            return cli.main(list(argv))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def init(self):
        os.environ["TESTKEY"] = KEY
        assert self.cli("init", "--server", self.rig.percia.url, "--api-key-env", "TESTKEY") == 0
        raw = self.cfg.read_text().replace("127.0.0.1:9477", f"127.0.0.1:{self.health_port}")
        self.cfg.write_text(raw)

    def run(self):
        self.log = (self.dir / f"bridge-{len(list(self.dir.glob('bridge-*.log')))}.log").open("w")
        self.proc = self.rig.spawn([sys.executable, "-m", "percia_bridge", "run", "--log-format", "json"], env=self.env, stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            assert self.proc.wait(timeout=25) == 0
        self.log and self.log.close()

    def health(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.health_port}/healthz", timeout=2) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)

    def cam(self, i=0):
        return self.health()["cameras"][i]

    def state(self, i=0):
        return self.cam(i)["state"]

    def logs(self):
        return "".join(p.read_text() for p in sorted(self.dir.glob("bridge-*.log")))


@pytest.fixture
def install(rig):
    i = Install(rig)
    i.init()
    yield i
    i.stop()


def streaming(inst):
    wait_for("camera streaming", lambda: inst.state() == "streaming", 45)


def test_add_register_publish_to_the_percia_media_server(rig, install, capsys):
    assert install.cli("add", "sim", "--rtsp", rig.camera_url) == 0
    p = rig.percia
    assert p.created == ["sim"] and p.claimed == 0 and len(p.push_cams) == 1  # the push API, not the pairing flow
    cam_id = next(iter(p.push_cams))
    install.run()
    streaming(install)
    wait_for("H.264 640x360 arrives on the Percia media server", lambda: rig.percia_side(cam_id).startswith("h264") and ",640,360" in rig.percia_side(cam_id))
    wait_for("heartbeats with the camera token", lambda: p.heartbeats >= 2)
    assert install.cam()["mode"] == "copy"  # H.264 baseline source: no re-encode
    assert p.publish_denied == [] and p.publish_ok  # MediaMTX asked Percia, Percia accepted the camera token
    capsys.readouterr()
    p.cameras_status_route = True  # the final contract: state/lastSeenAt/hint, no codec or bitrate
    install.cli("--config", str(install.cfg), "status", "--listen", f"127.0.0.1:{install.health_port}")
    out = capsys.readouterr().out
    assert "sim" in out and "streaming" in out and "yes" in out and p.status_calls == 1
    install.stop()
    text = install.logs()
    for secret in (p.push_cams[cam_id]["token"], KEY, "camsecret"):
        assert secret not in text, "a secret reached the logs"
    assert "camera_streaming" in text


def test_restart_and_wiped_state_never_duplicate_the_camera(rig, install):
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    install.run()
    streaming(install)
    install.stop()
    install.run()  # plain restart
    streaming(install)
    install.stop()
    (install.dir / "state" / "cameras.json").unlink()  # reinstall: tokens lost, config kept
    install.run()
    streaming(install)
    assert len(rig.percia.push_cams) == 1 and rig.percia.created == ["sim"] and rig.percia.rotated == ["cam_push1"]
    assert rig.percia.publish_denied == []


def test_rotate_while_running_swaps_the_token_without_restarting_the_bridge(rig, install):
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    install.run()
    streaming(install)
    old = next(iter(rig.percia.push_cams.values()))["token"]
    assert install.cli("rotate", "sim") == 0
    new = next(iter(rig.percia.push_cams.values()))["token"]
    assert new != old
    # the heartbeat with the old token now gets 401: the camera goes revoked, then picks up the new token from the state dir
    wait_for("revoked or already recovered", lambda: install.cam()["restarts"] >= 0 and (install.state() == "revoked" or new in rig.percia.publish_ok), 30)
    wait_for("streaming again with the new token", lambda: install.state() == "streaming" and new in rig.percia.publish_ok, 60)
    assert old not in install.logs() and new not in install.logs()


def test_revoke_then_rotate_shows_a_clear_error_and_recovers(rig, install, capsys):
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    install.run()
    streaming(install)
    assert install.cli("rotate", "sim", "--revoke") == 0
    wait_for("camera revoked", lambda: install.state() == "revoked", 30)
    assert 'percia-bridge rotate "sim"' in install.cam()["last_error"]
    assert install.cli("rotate", "sim") == 0
    streaming(install)


def test_camera_outage_backoff_and_recovery(rig, install):
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    install.run()
    streaming(install)
    rig.stop_camera()
    wait_for("bridge notices the outage", lambda: install.state() in ("backoff", "starting"), 60)
    rig.start_camera()
    wait_for("streams again", lambda: install.state() == "streaming", 90)
    assert install.cam()["restarts"] >= 1


def test_h265_camera_is_transcoded_to_h264_baseline(rig, install):
    if " libx265 " not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout:
        pytest.skip("no libx265 in this ffmpeg")
    rig.use_hevc_camera()
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    cam_id = next(iter(rig.percia.push_cams))
    install.run()
    streaming(install)
    assert install.cam()["mode"] == "transcode"
    wait_for("H.264 Baseline on the Percia media server", lambda: rig.percia_side(cam_id).startswith("h264,Constrained Baseline") or rig.percia_side(cam_id).startswith("h264,Baseline"))


def test_older_server_without_the_push_api_uses_the_claim_flow_end_to_end(rig, install):
    rig.percia.push = False
    rig.percia.whip_base = None
    install.cli("add", "sim", "--rtsp", rig.camera_url)
    assert rig.percia.claimed == 1 and rig.percia.push_cams == {}
    state = json.loads((install.dir / "state" / "cameras.json").read_text())["cameras"]["sim"]
    assert state["kind"] == "phone"  # still registered, still usable; rotate is not available for it
    assert install.cli("check") == 0


def test_bulk_register_many_cameras_one_command(rig, install):
    raw = install.cfg.read_text().replace("cameras: []", "cameras:\n" + "".join(
        f"  - name: sim{i}\n    source: {{type: rtsp, url: \"{rig.camera_url}?n={i}\"}}\n" for i in range(1, 6)))
    install.cfg.write_text(raw)
    assert install.cli("register") == 0
    assert len(rig.percia.push_cams) == 5 and len(rig.percia.bulk_calls) == 1 and len(rig.percia.bulk_calls[0]) == 5  # one POST /api/cameras/bulk
    assert install.cli("register") == 0 and len(rig.percia.push_cams) == 5 and len(rig.percia.bulk_calls) == 1  # all registered: nothing to send
