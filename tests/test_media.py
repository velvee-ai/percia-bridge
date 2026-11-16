import json

import pytest

from percia_bridge import media
from percia_bridge.config import Video

PROBE = json.dumps({"streams": [{"codec_name": "h264", "profile": "Main", "width": 1920, "height": 1080, "has_b_frames": 0}]})


def test_parse_probe():
    p = media.parse_probe(PROBE)
    assert (p.codec, p.width, p.height, p.has_b_frames) == ("h264", 1920, 1080, 0)
    with pytest.raises(ValueError):
        media.parse_probe('{"streams": []}')


@pytest.mark.parametrize("probe,video,mode,reason", [
    (media.Probe("h264", "Main", 1920, 1080, 0), Video(), "copy", "h264"),
    (media.Probe("h264", "Main", 1920, 1080, 2), Video(), "transcode", "b_frames"),
    (media.Probe("hevc", "Main", 1920, 1080, 0), Video(), "transcode", "codec_hevc"),
    (media.Probe("mjpeg", "", 640, 480, 0), Video(), "transcode", "codec_mjpeg"),
    (media.Probe("h264", "High", 1920, 1080, 0), Video(max_height=720), "transcode", "too_tall"),
    (media.Probe("h264", "High", 1280, 720, 0), Video(max_height=720), "copy", "h264"),
    (None, Video(), "transcode", "probe_failed"),
    (media.Probe("hevc", "", 1, 1, 0), Video(mode="copy"), "copy", "forced"),
    (media.Probe("h264", "", 1, 1, 0), Video(mode="transcode"), "transcode", "forced"),
])
def test_plan(probe, video, mode, reason):
    p = media.plan(probe, video)
    assert (p.mode, p.reason) == (mode, reason)


def test_copy_command():
    cmd = media.build_command("ffmpeg", "rtsp://h/s", "https://m/cam/1/whip", "pc_tok", media.Plan("copy", "h264"), Video())
    assert cmd[0] == "ffmpeg" and "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy" and "libx264" not in cmd
    assert "-an" in cmd and ("-rtsp_transport", "tcp") == (cmd[cmd.index("-rtsp_transport")], cmd[cmd.index("-rtsp_transport") + 1])
    assert cmd[-5:] == ["-f", "whip", "-authorization", "pc_tok", "https://m/cam/1/whip"]
    assert "nice" not in cmd


def test_transcode_command_caps():
    v = Video(max_height=480, max_fps=10, max_bitrate_kbps=800)
    cmd = media.build_command("ffmpeg", "rtsp://h/s", "https://m/w", "t", media.Plan("transcode", "x"), v)
    assert cmd[:3] == ["nice", "-n", "10"]
    j = " ".join(cmd)
    assert "libx264" in cmd and "baseline" in cmd and "-bf 0" in j and "-maxrate 800k" in j and "-bufsize 1600k" in j
    assert "min(ih,480)" in j and "fps=10" in j and "-g 20" in j


def test_default_transcode_height_is_720():
    assert "min(ih,720)" in " ".join(media.video_args(media.Plan("transcode", "x"), Video()))


def test_non_rtsp_input_gets_headers_not_rtsp_flags():
    a = media.input_args("http://supervisor/core/api/hls/x/master.m3u8", "tcp", {"Authorization": "Bearer T"})
    assert "-rtsp_transport" not in a and "-headers" in a and "Authorization: Bearer T" in a[a.index("-headers") + 1]


def test_probe_command_for_rtsp_uses_transport():
    c = media.probe_command("ffprobe", "rtsp://h/s", "udp")
    assert "udp" in c and c[-1] == "rtsp://h/s"


@pytest.mark.parametrize("line,expected", [
    ("[out#0/whip @ 0x85b008840] Could not write header (incorrect codec parameters ?): Server returned 401 Unauthorized (authorization failed)", "percia_auth"),
    ("[out#0/whip @ 0x1] Server returned 403 Forbidden", "percia_auth"),
    ("[in#0 @ 0x8c8c14000] Error opening input: Server returned 401 Unauthorized (authorization failed)", "source_auth"),
    ("[rtsp @ 0x7] method DESCRIBE failed: 401 Unauthorized", "source_auth"),
    ("frame=401 fps=15", None),
    ("Connection refused", None),
    ("[out#0/whip @ 0x1] Server returned 500 Internal Server Error", None),
])
def test_classify_error(line, expected):
    assert media.classify_error(line) == expected
