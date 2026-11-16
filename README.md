# Percia Bridge

Sends cameras on **your own network** to [Percia](https://percia.ai). Percia's servers cannot reach a camera at home or on a site network, so the Bridge runs there and pushes each camera out over WHIP (the same path phone cameras use). It only dials out: no port forwarding, no inbound ports.

Inputs: **ONVIF** cameras (auto-discovery), plain **RTSP** URLs, **Frigate** (via its go2rtc restream), **Home Assistant** (add-on). Video only, never audio. H.264 is passed through untouched; anything else is re-encoded to H.264 at a capped size and bitrate.

```
camera / Frigate / Home Assistant --RTSP--> percia-bridge --WHIP over HTTPS--> Percia
```

**Why push?** Percia's servers never connect to cameras on private networks (and you should not port-forward a camera). The Bridge pulls your camera once on the LAN and *pushes* one stream out over HTTPS/WebRTC. The camera serves a single RTSP session even when Frigate, an NVR and Percia all use it, and nothing is exposed to the internet.

Design and failure model: [docs/design.md](docs/design.md). Release history: [CHANGELOG.md](CHANGELOG.md).

## You need

- A Percia account and an **API key with scopes `cameras:read`, `cameras:write` and `watches:delete`** (Percia settings -> API keys). The key is used only to register, rotate and remove cameras; streaming uses per-camera tokens.
- Cameras enabled on your Percia server (`init` and `doctor` check it).
- Docker (recommended), or Python 3.11+ and ffmpeg >= 7.1 **with the `whip` muxer** (`percia-bridge version` checks).

## Quick start: Docker

```bash
mkdir -p percia-bridge/config percia-bridge/data && cd percia-bridge
# 1. config + API key (hidden prompt). Files are written 0600.
docker run --rm -it -v $PWD/config:/config -v $PWD/data:/data ghcr.io/velvee-ai/percia-bridge init
# 2. find ONVIF cameras (multicast needs host networking; Linux only)
docker run --rm -it --network host -v $PWD/config:/config ghcr.io/velvee-ai/percia-bridge discover
# 3. add cameras (registers the camera with Percia, safe to repeat, and stores its token)
docker run --rm -it -v $PWD/config:/config -v $PWD/data:/data ghcr.io/velvee-ai/percia-bridge add front-door --rtsp rtsp://viewer:PASSWORD@192.168.1.20/stream1
# 4. run it
docker run -d --name percia-bridge --restart unless-stopped -v $PWD/config:/config -v $PWD/data:/data ghcr.io/velvee-ai/percia-bridge
docker exec percia-bridge percia-bridge status
# anything wrong? one command checks ffmpeg, network, clock, key and every camera:
docker run --rm -it -v $PWD/config:/config -v $PWD/data:/data ghcr.io/velvee-ai/percia-bridge doctor
```

Then create a watch on the camera in Percia (source `percia-cam://<camera id>`).

Or with compose, in `docker-compose.yml`:

```yaml
services:
  percia-bridge:
    image: ghcr.io/velvee-ai/percia-bridge:latest
    restart: unless-stopped
    volumes: ["./config:/config", "./data:/data"]
```

Keep passwords out of your shell history: `add front-door --rtsp 'rtsp://viewer:${DOOR_PASSWORD}@192.168.1.20/stream1'` (single quotes) stores the reference, and the variable is read when the Bridge starts.

You can also put cameras straight into `config/config.yaml` (see [examples/config.example.yaml](examples/config.example.yaml)); `run` registers any that are missing. Adding the same camera twice (or after wiping `data/`) never creates a duplicate in Percia.

Many cameras at once: `percia-bridge register` (everything in the config) or `percia-bridge add-all --frigate http://frigate:5000 [--only a,b]`.

## ONVIF cameras

```bash
percia-bridge discover                               # asks for the camera login to list profiles; prints a ready `add` line for each camera
percia-bridge add garage --onvif 192.168.1.30 --user admin --password-env GARAGE_PASSWORD
```

Discovery sends a WS-Discovery multicast probe on the local network only (it never scans address ranges) and lists each camera's profiles and RTSP addresses **without passwords**. Pick a lower-resolution H.264 sub-stream when you can (`--profile subStream`): the Bridge copies it with almost no CPU. The camera's stream address is looked up again at every start.

## Frigate

The Bridge reads Frigate's **go2rtc restream** (`rtsp://frigate:8554/<stream>`), so Frigate keeps the only connection to each camera and the cameras see no extra load.

1. Make sure each Frigate camera has a go2rtc stream and uses it as its input (see the comment at the top of [examples/frigate-compose.yml](examples/frigate-compose.yml)).
2. Start the Bridge next to Frigate with that compose file.
3. `docker compose run --rm percia-bridge discover --frigate http://frigate:5000` lists the cameras and prints the `add` command for each:
   `percia-bridge add front-door --frigate http://frigate:5000 --camera front_door`. To take all of them: `percia-bridge add-all --frigate http://frigate:5000`.

No API access to Frigate? `discover --frigate-config /path/to/config.yml` reads the file instead.

## Home Assistant

1. **Settings -> Add-ons -> Add-on store -> ⋮ -> Repositories**, add `https://github.com/velvee-ai/percia-bridge` (the add-on is a prebuilt image, `ghcr.io/velvee-ai/percia-bridge-addon`; nothing is compiled on your Home Assistant).
2. Install **Percia Bridge**, paste your API key under **Configuration**, start it.
3. Open **Percia Bridge** in the sidebar, tick the cameras to send, press **Save**.

For each camera the add-on uses the RTSP address you paste for it, else Home Assistant's own stream (HLS), else the camera proxy (MJPEG, always re-encoded: use only as a fallback). Details: [homeassistant/DOCS.md](homeassistant/DOCS.md).

## Raspberry Pi / NAS (and always-on machines)

Pi 4 or 5 (64-bit OS) and ARM NAS boxes are supported by the same arm64 image. Install Docker and use the Docker quick start. A Pi 4 copies several H.264 cameras comfortably; **one re-encoded camera costs about a quarter to a half of one core**, so prefer H.264 sub-streams and keep `max_height: 480` for H.265 cameras.

Without Docker: `pipx install percia-bridge`, install an ffmpeg >= 7.1 with WHIP (the Debian/Raspberry Pi OS package does **not** include it; use a static build such as BtbN's `ffmpeg-n8.1-linuxarm64-gpl`), then use [examples/percia-bridge.service](examples/percia-bridge.service).

## Commands

| | |
|---|---|
| `init` | wizard: hidden key prompt, checks the key, its scopes and that cameras are enabled, prints next steps (`--api-key-env`, `--api-key-stdin`, `--yes`) |
| `discover` | ONVIF on the LAN; `--frigate URL`; `--frigate-config FILE`; `--home-assistant` (inside the add-on). Prints a ready `add` command for each camera |
| `add NAME ...` | `--rtsp URL`, `--onvif HOST --user U --password-env VAR [--profile P]`, `--frigate URL --camera C`, `--homeassistant ENTITY`; idempotent |
| `add-all --frigate URL` | add every Frigate camera (or `--only a,b`) and register them in one go (one `POST /api/cameras/bulk`, up to 10 per call; per-camera results) |
| `register` | register every configured camera that is not registered yet (bulk when there are two or more, one by one on older servers) |
| `rotate NAME` | new publish token (after a 401, or if one leaked); `--revoke` stops the camera publishing. A running bridge picks it up within seconds |
| `run` | stream every camera; reconnects with exponential backoff (1 s doubling to 30 s, with jitter) |
| `status` | per camera: bridge state, Percia's view (online / waiting / offline / revoked, with Percia's hint), last publish, video (codec, resolution, fps and bitrate only if the server reports them; today it does not); exit 1 if the local bridge is not fully streaming (`--no-remote` skips Percia) |
| `doctor` | checks ffmpeg + WHIP, DNS/HTTPS to Percia and the media servers, clock, key, every camera's reachability and codec |
| `check` | validates the config file |
| `remove NAME` | delete the camera in Percia and forget it here |
| `version` | versions, and whether ffmpeg can do WHIP |

Health and metrics (loopback only by default): `http://127.0.0.1:9477/healthz` and `/metrics` (Prometheus). Configuration reference: [examples/config.example.yaml](examples/config.example.yaml).

## What leaves your network, and what the credentials can do

- **Video only, never audio**, from the cameras you add, sent to Percia's media server (WebRTC/WHIP over TLS). Percia analyses it for the goals you set. Cameras you do not add are never touched.
- **HTTPS calls to Percia** to register cameras, rotate tokens and read status (camera name, a stable id derived from the camera identity, state). Camera passwords, RTSP URLs and LAN addresses are **not** sent.
- **Anonymous usage events are off by default.** If you switch them on (`telemetry: true`), only allow-listed values leave: Bridge version, CPU architecture, how it is run, source type, copy/transcode. Never names, addresses or error text (details in [docs/design.md](docs/design.md)).
- **Your API key** can create, list, rotate and delete cameras in your Percia account (scopes `cameras:read`, `cameras:write`, `watches:delete`). It cannot read video. Revoke it in Percia settings whenever you like.
- **A camera token** (`pc_...`) can only publish video for that one camera. `percia-bridge rotate NAME` replaces it; `--revoke` stops the camera.
- **Only outbound connections.** No port forwarding, nothing for the internet to reach. The only listener is health/metrics on `127.0.0.1`.

The API key lives in a `0600` file or `PERCIA_API_KEY`, never on a command line. Camera tokens are in `/data/cameras.json` (`0600`). Keys, tokens and camera passwords are masked in every log line. The container runs as a non-root user. One limitation: ffmpeg receives the camera URL (with its password) as an argument, visible to other users **of the same host** in `ps`. In Docker that is only the container; on a shared host run the Bridge as its own user.

Found a vulnerability? Please report it privately: see [SECURITY.md](SECURITY.md).

## Troubleshooting

**No video in Percia.**
`percia-bridge status` first. `streaming` means video is flowing: check the watch points at `percia-cam://<id>` (shown by `GET /api/cameras`). `backoff` with a reason:
- `source`: the Bridge cannot read the camera. From the same host try `ffprobe rtsp://...` (in Docker: `docker exec percia-bridge ffprobe -rtsp_transport tcp 'rtsp://...'`). Check the address, port 554, and that the camera allows the user to view RTSP. "username/password" in the error means the camera rejected the login.
- `network`: the stream was running and dropped; the Bridge retries by itself. Persistent drops: Wi-Fi camera, or try `rtsp_transport: tcp` (default) versus `udp`.
- `stall`: the camera stopped sending frames (a frozen camera); the Bridge restarts the connection.
- `auth`: see 401 below.
Nothing leaves the Bridge when the upload is blocked: allow outbound HTTPS to Percia and **UDP/TCP** to the media server (WebRTC). Some corporate firewalls block UDP; WebRTC then falls back to TCP.

**401 / `revoked`.**
Percia refused the camera token: it was rotated or revoked, or the camera was deleted. The Bridge pauses that camera (others keep running) and says so in `status` and the log. Fix: `percia-bridge rotate NAME`: a running bridge resumes by itself within seconds (if it runs in another container, share the `/data` volume). Camera deleted in Percia: `percia-bridge add NAME --force`. A `401` from `init`/`add` means the API key is wrong; `403` means it lacks a scope (`cameras:write`, and `watches:delete` for rotate/remove). A message "does not have cameras enabled" means the Percia server has phone cameras switched off.

**Duplicates / reinstall.** Cameras are registered under a stable id, so reinstalling or wiping `data/` re-attaches to the same Percia camera (and rotates its token) instead of creating another.

**Firewall.** Only outbound: HTTPS (443) to Percia and outbound WebRTC (UDP, TCP fallback) to `media.percia.ai`. No port forwarding. `percia-bridge doctor` tests all of it.

**`camera_limit`.** Percia allows 10 cameras per account; remove one with `percia-bridge remove`.

**High CPU.**
Look at the mode in `status`: `transcode` means the camera is being re-encoded (non-H.264, or H.264 with B-frames, or `mode: transcode`). Fixes, best first: pick the camera's H.264 sub-stream (`--profile`), lower `video.max_height` (480) and `max_fps` (10), or move the Bridge to a stronger machine. `mode: copy` costs almost nothing. With Frigate, use the restream of the detect (sub) stream.

**H.265 (HEVC) cameras.**
Percia needs H.264, so the Bridge re-encodes H.265 automatically (capped at 720p, 15 fps, 1.5 Mbit/s by default; see CPU above). Many cameras offer an H.264 profile or sub-stream; ONVIF discovery shows each profile's codec. Set the camera to H.264 in its own web page if it allows.

**ONVIF camera not found.**
Discovery is multicast: the Bridge must be on the same subnet, and in Docker it needs `--network host` (Linux). Some cameras have ONVIF switched off or need an ONVIF user created in their web page. Add the camera by RTSP URL instead.

**"this ffmpeg has no WHIP muxer".**
Your ffmpeg lacks WHIP support (common in distro packages). Use the Docker image, or a static build from BtbN or a Homebrew ffmpeg.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                 # unit tests + the simulated-camera end to end (skipped if ffmpeg-with-whip/mediamtx are missing)
.venv/bin/python scripts/e2e.py  # only the end-to-end tests
```

The end-to-end tests run a looping test clip as a simulated RTSP camera (MediaMTX), the Bridge, a second MediaMTX as Percia's WHIP endpoint that asks a fake Percia API to authorise every publish (as the real server does), and cover add, register, publish, restart without duplicates, rotation, revoke, outage and recovery, H.265 transcoding, the older pairing flow and bulk. They need no account, no key and nothing but 127.0.0.1.

## Contributing, security, license

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)

MIT for the Bridge ([LICENSE](LICENSE), Copyright Velvee, Inc.). The Docker image also contains a static **GPLv3** build of ffmpeg, run as a separate program; where to get its source, and the other third-party licenses, are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
