"""Starts the real add-on process against a fake Home Assistant and the fake Percia API (no ffmpeg stream needed)."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fakes import KEY

from percia_bridge import ha_addon  # noqa: F401  (import check)

STATES = (Path(__file__).parent / "fixtures" / "ha_states.json").read_bytes()


class FakeHA(BaseHTTPRequestHandler):
    seen_auth: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeHA.seen_auth.append(self.headers.get("authorization"))
        ok = self.path == "/api/states" and self.headers.get("authorization") == "Bearer SUPTOKEN"
        body = STATES if ok else b"{}"
        self.send_response(200 if ok else 401)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_addon_process_creates_selected_cameras_and_guards_ui(tmp_path, percia):
    ha = ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
    threading.Thread(target=ha.serve_forever, daemon=True).start()
    (tmp_path / "options.json").write_text(json.dumps({"api_key": KEY, "server": percia.url, "log_level": "info", "max_height": 480}))
    (tmp_path / "selection.json").write_text(json.dumps({"entities": {
        "camera.front_door": {"enabled": True, "rtsp_url": "rtsp://127.0.0.1:1/none"},   # unreachable on purpose
        "camera.backyard": {"enabled": False, "rtsp_url": ""}}}))
    port = free_port()
    env = {**os.environ, "SUPERVISOR_TOKEN": "SUPTOKEN", "PERCIA_ADDON_OPTIONS": str(tmp_path / "options.json"),
           "PERCIA_ADDON_SELECTION": str(tmp_path / "selection.json"), "PERCIA_BRIDGE_STATE": str(tmp_path / "data"),
           "PERCIA_HA_BASE": f"http://127.0.0.1:{ha.server_address[1]}", "PERCIA_ADDON_PORT": str(port),
           "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    log = (tmp_path / "addon.log").open("w")
    p = subprocess.Popen([sys.executable, "-m", "percia_bridge.ha_addon"], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        end = time.time() + 20
        while time.time() < end and percia.created != ["Front Door Reolink"]:
            time.sleep(0.2)
        assert percia.created == ["Front Door Reolink"], (percia.created, (tmp_path / "addon.log").read_text())
        assert percia.claimed == 1
        assert "Bearer SUPTOKEN" in FakeHA.seen_auth
        try:  # the UI answers only the ingress gateway; localhost gets 403
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cameras", timeout=3)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=20) == 0
        ha.shutdown()
        log.close()
    text = (tmp_path / "addon.log").read_text()
    assert KEY not in text and "SUPTOKEN" not in text
    assert "addon_started" in text
