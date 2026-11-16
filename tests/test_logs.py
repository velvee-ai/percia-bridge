import io
import json
import logging

from percia_bridge import logs
from percia_bridge.logs import event, redact, register_secret, setup_logging, strip_credentials


def test_redacts_urls_tokens_and_registered_secrets():
    register_secret("hunter2hunter2")
    out = redact("open rtsp://admin:hunter2hunter2@10.0.0.5/s and Authorization: Bearer abc.def and pc_AbCdEfGhIjKlMnOp and password=zzz9")
    assert "hunter2" not in out and "abc.def" not in out and "AbCdEfGh" not in out and "zzz9" not in out
    assert "10.0.0.5" in out  # host is not secret


def test_user_only_url():
    assert "tok123" not in redact("rtsp://tok123@host/x")


def test_strip_credentials():
    assert strip_credentials("rtsp://u:p@h:554/a?b=c") == "rtsp://h:554/a?b=c"
    assert strip_credentials("rtsp://h/a") == "rtsp://h/a"


def test_json_log_is_redacted():
    buf = io.StringIO()
    setup_logging("info", True, buf)
    register_secret("SUPERSECRETKEY")
    event(logging.INFO, "camera_x", camera="a", error="failed with SUPERSECRETKEY at rtsp://u:p@h/s")
    line = json.loads(buf.getvalue())
    assert line["event"] == "camera_x" and line["camera"] == "a" and line["level"] == "info"
    assert "SUPERSECRETKEY" not in buf.getvalue() and ":p@" not in buf.getvalue()
    logs.log.handlers.clear()


def test_text_log_is_redacted():
    buf = io.StringIO()
    setup_logging("info", False, buf)
    event(logging.WARNING, "x", url="rtsp://u:pw@h/s")
    assert "pw@" not in buf.getvalue()
    logs.log.handlers.clear()
