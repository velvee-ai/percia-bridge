import asyncio

from percia_bridge import telemetry as t


def test_unknown_event_dropped():
    assert t.sanitize("user_clicked", {}) is None


def test_hostile_values_are_dropped():
    name, props = t.sanitize("bridge_camera_failed", {
        "source_type": "rtsp://admin:pw@192.168.1.5/s", "reason": "Connection refused to 10.0.0.1", "mode": "copy",
        "version": "1.2.3-evil", "arch": "arm64", "camera_name": "Front door", "ip": "10.0.0.1", "count": 5, "variant": "docker"})
    assert props == {"mode": "copy", "arch": "arm64", "variant": "docker"}


def test_valid_values_pass():
    _, props = t.sanitize("bridge_started", {"source_type": "onvif", "reason": "stall", "version": "0.1.0", "camera_bucket": "2-5"})
    assert props == {"source_type": "onvif", "reason": "stall", "version": "0.1.0", "camera_bucket": "2-5"}


def test_bucket():
    assert [t.bucket(n) for n in (0, 1, 2, 5, 6, 10)] == ["1", "1", "2-5", "2-5", "6-10", "6-10"]


def test_disabled_by_default_and_without_key():
    assert not t.Telemetry(False, "phc_x", "id", "docker", "0.1.0").enabled
    assert not t.Telemetry(True, "", "id", "docker", "0.1.0").enabled


def test_sent_payload_has_only_allowed_fields(monkeypatch):
    sent = []

    async def fake_send(self, name, props):
        sent.append((name, props))
    monkeypatch.setattr(t.Telemetry, "_send", fake_send)

    async def go():
        tel = t.Telemetry(True, "phc_x", "install-uuid", "ha_addon", "0.1.0")
        tel.emit("bridge_camera_failed", {"source_type": "frigate", "reason": "tls error for host.example", "url": "http://x"})
        tel.emit("not_allowed", {})
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert len(sent) == 1
    name, props = sent[0]
    assert name == "bridge_camera_failed"
    assert set(props) <= {"source_type", "mode", "reason", "arch", "variant", "version", "camera_bucket"}
    assert "reason" not in props and props["variant"] == "ha_addon" and props["source_type"] == "frigate"
