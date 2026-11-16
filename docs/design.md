# Percia Bridge: design

Status: implemented in this repo.

## 1. Problem and shape

Percia's pool runs in the cloud and cannot reach a camera on a home or site network (no inbound path, usually no public IP). Phone cameras already solve "get video into Percia": create a camera, claim a pairing token, publish video over WHIP, send a heartbeat. The Bridge is **a headless WHIP publisher that runs on the user's own network** and uses exactly that path for cameras the Percia servers cannot reach.

```
 LAN                                                        Internet (TLS only)
+-----------+  RTSP   +--------------------------+  WHIP (HTTPS, Bearer camera token)  +-----------------+
| IP camera |-------->|  percia-bridge           |------------------------------------>| Percia media    |
| (ONVIF)   |         |   supervisor (asyncio)   |  POST /api/cam/heartbeat every 20 s | (MediaMTX)      |
+-----------+         |    +- ffmpeg  cam A  ----+|------------------------------------>| Percia API      |
+-----------+  RTSP   |    +- ffmpeg  cam B      ||  POST /api/cameras, /api/cam/claim  +-----------------+
| Frigate   |-------->|    +- ...                 |  (setup only, API key)
| go2rtc    |         |  /healthz /metrics (local)|
+-----------+         +--------------------------+
+-----------+  HLS/MJPEG/RTSP  ^
| Home Asst.|------------------+  (add-on variant: same code, different input resolver + a small UI)
+-----------+
```

The Bridge only dials out. It opens no inbound port except an optional **loopback** health/metrics listener.

## 2. Stack decision

**Python 3.12 (asyncio) supervisor + ffmpeg subprocesses.** Requires ffmpeg >= 7.1 **built with the `whip` muxer** (7.1 is the first release with it; 8.x is fine; many distro builds omit it, see Distribution).

Why not Go: a Go single binary is attractive, but the Bridge's media work is entirely ffmpeg, and the only Go WHIP libraries (pion) would mean re-implementing RTSP depacketising, H.264 repacketising, ICE and DTLS-SRTP ourselves, plus a transcode path that still needs ffmpeg. Python's job is orchestration: processes, timers, a few HTTPS calls, SOAP. That is where it is shortest and easiest for the team to read and fix. Resource cost is dominated by ffmpeg (copy mode is ~1-3% of one core per camera on a Pi 4); the supervisor idles at ~30 MB RSS.

Why ffmpeg over a WebRTC library in-process: it already handles RTSP quirks of thousands of camera models (TCP interleaving, odd SDP, timestamps) and its `whip` muxer does ICE/DTLS/SRTP. One process per camera isolates crashes and memory growth: a bad camera takes down its own ffmpeg, never the other cameras.

Dependencies (kept small on purpose): `httpx` (HTTPS client), `pyyaml`, `websockets` (Home Assistant stream-source command only). ONVIF (WS-Discovery over UDP, SOAP over HTTP, WS-Security digest) is hand-written on the standard library: about 250 lines, no `zeep`/`onvif-zeep` (heavy, unmaintained forks, WSDL fetches at runtime). The tiny HTTP server for `/healthz`, `/metrics` and the add-on UI is also stdlib `asyncio`.

Distribution: one multi-arch Docker image (`linux/amd64`, `linux/arm64`, so Raspberry Pi 4/5 and ARM NAS work) on `python:3.12-slim-trixie`. **Finding while building this: Debian's `ffmpeg` (7.1.x in trixie) is compiled without the `whip` muxer** (WHIP needs OpenSSL for DTLS; Debian links GnuTLS), so `apt install ffmpeg` is not enough. The image therefore ships a **pinned static ffmpeg** (BtbN/FFmpeg-Builds `n8.1.3`, GPL build with OpenSSL + libx264), verified by SHA-256 per architecture, and the build **fails** unless `ffmpeg -muxers` lists `whip`, `libx264` exists and the version is >= 7.1. The bundled ffmpeg is GPL-3.0; the Bridge code is MIT and only runs ffmpeg as a separate process. Homebrew's ffmpeg (macOS) and recent distro-independent builds have whip; `percia-bridge version` tells a pip/pipx user whether their ffmpeg can publish.

## 3. Commands

| Command | What it does |
|---|---|
| `percia-bridge init` | Creates the config dir, writes `config.yaml` (0600) and the API key file (0600). Asks for the key with a hidden prompt (or `--api-key-env`, `--api-key-stdin`). Verifies the key with `GET /api/cameras` (unless `--offline`). |
| `percia-bridge discover` | ONVIF WS-Discovery on the local LAN; for each device asks for profiles and RTSP URIs (`GetStreamUri`) and prints name, codec, resolution, URI with **credentials stripped**. `--frigate URL` lists Frigate cameras; `--home-assistant` lists camera entities (inside the add-on). `--no-probe` only lists addresses. |
| `percia-bridge add NAME ...` | Adds the camera to the config (source flags below), creates the Percia camera (`POST /api/cameras`), claims it (`POST /api/cam/claim`) and stores the token in the state dir. Idempotent: an already-added name is skipped. |
| `percia-bridge run` | Supervises one ffmpeg per camera (RTSP -> WHIP), heartbeats, reconnects, graceful shutdown on SIGTERM/SIGINT. Cameras in the config that were never added are added automatically (`auto_add: true`, default). |
| `percia-bridge status` | Reads the local `/healthz` of a running bridge (or the state dir if none runs) and prints one line per camera: state, uptime, mode, last error. Exit code 1 if any camera is not streaming. |
| `percia-bridge remove NAME` | Deletes the camera on Percia (`DELETE /api/cameras/:id`, reports `409 camera_in_use` plainly), deletes its token from the state dir and removes it from the config. `--keep-remote` only forgets it locally. |
| `percia-bridge version` | Prints bridge and ffmpeg versions and whether the whip muxer is present. |

`add` sources: `--rtsp URL`, `--onvif HOST --user U` (password prompt; or `--password-env`), `--frigate URL --camera NAME`.

## 4. Percia API contract used

From Percia's public camera API (phone-camera integrator docs):

1. `POST /api/cameras` `{name}` (API key with scope `cameras:write`; name 1-60 chars; max 10 cameras per account, `429 camera_limit`) -> `201 {camera, pairUrl, pairExpiresAt}`. The pairing token is the last path segment of `pairUrl` (valid 10 minutes, single use).
2. `POST /api/cam/claim` `{pairing}` (public, rate limited) -> `{cameraId, name, token, whipUrl}`. The camera token (`pc_...`) is returned **once**. `400 pairing_invalid` = used/expired.
3. WHIP publish: `POST whipUrl` with `Authorization: Bearer <camera token>` (done by ffmpeg via `-authorization`), video only, no audio.
4. `POST /api/cam/heartbeat` with the camera bearer token every 20 s while streaming. `401` means the camera was deleted or re-paired.
5. `DELETE /api/cameras/:id` (API key; `409 camera_in_use` while a watch uses it).
6. If phone cameras are switched off on the server, all of the above answer `404 not_found`; the bridge says "this Percia server does not have cameras enabled" and exits non-zero from `add`.

`add` creates and claims in one go, so the 10-minute pairing window is never a concern and the API key is used only at setup time, not while streaming.

## 5. Configuration (YAML)

Default path: `$PERCIA_BRIDGE_CONFIG`, else `/config/config.yaml` in Docker, else `~/.config/percia-bridge/config.yaml`.

```yaml
server: https://percia.ai          # base URL; https only (http allowed only for localhost, for tests)
api_key_file: /config/api_key      # 0600 file; or env PERCIA_API_KEY (env wins)
state_dir: /data                   # camera tokens (0600 file cameras.json)
auto_add: true                     # `run` creates+claims cameras missing in the state dir
health:
  listen: 127.0.0.1:9477           # empty string disables /healthz and /metrics
video:                             # defaults, overridable per camera
  mode: auto                       # auto | copy | transcode
  max_height: 720                  # only used when transcoding
  max_fps: 15
  max_bitrate_kbps: 1500
  preset: veryfast
rtsp_transport: tcp
telemetry: false                   # anonymous, opt-in, see section 9
cameras:
  - name: front-door
    source: {type: rtsp, url: "rtsp://user:${DOOR_PASS}@192.168.1.20/stream1"}
  - name: garage
    source: {type: onvif, host: 192.168.1.30, port: 80, username: admin, password: "${GARAGE_PASS}", profile: Profile_1}
    video: {mode: transcode, max_height: 480}
  - name: driveway
    source: {type: frigate, url: "http://frigate:5000", camera: driveway, rtsp_port: 8554}
```

Rules: `${VAR}` expands from the environment (so secrets can live in the environment or `.env`); unknown variable is a config error, not an empty string. Camera names are 1-60 chars, unique, `[A-Za-z0-9 _.-]`. Unknown keys are errors (typos are caught at start). The loader warns if the config or API key file is group/world readable and `init` always creates them 0600. Credentials in config are plaintext (0600): the Bridge must be able to log in to the camera unattended, so encrypting at rest with a key beside it would be theatre. Prefer `${ENV}` references and the platform's secret store where there is one.

## 6. Security

- **API key**: read from env `PERCIA_API_KEY` or a file (0600, checked). Never accepted on the command line (it would land in shell history and `ps`). Used only for `init`, `add`, `remove`, auto-add. Streaming needs only per-camera tokens, so a leaked running bridge cannot create or delete other cameras without the key file.
- **Camera tokens**: `state_dir/cameras.json`, mode 0600, directory 0700, written atomically (temp file + rename). A token is only ever sent as `Authorization: Bearer` to the WHIP URL and heartbeat, over HTTPS.
- **Never logged**: a central `redact()` is applied to every log record and every captured ffmpeg stderr line: bearer tokens, `pc_...` tokens, the API key, pairing tokens, `user:pass@` in URLs, ONVIF passwords. `discover` strips credentials from RTSP URIs before printing. Unit tests assert known secrets never appear in log output.
- **Process listing**: ffmpeg needs the RTSP URL (with camera credentials) and the Percia bearer token as arguments, which are visible to other users of the host (`ps`). Mitigation: in Docker the container has its own PID namespace; on a shared host run the Bridge as its own user and set `hidepid`. Documented in the README. (ffmpeg has no portable "read argument from file/env" for these.)
- **No inbound ports**: the Bridge only dials out (RTSP to the camera/Frigate/HA, HTTPS to Percia, UDP/TCP for WebRTC media which ICE opens outbound). The optional health listener binds `127.0.0.1` by default; binding elsewhere logs a warning. It exposes no secrets and no control endpoints.
- **TLS only**: `server` must be `https://` (except `localhost`/`127.0.0.1` for tests). Certificates are verified; there is no `--insecure`. WHIP URLs returned by the server that are not https are refused (same localhost exception).
- **Camera side**: ONVIF credentials travel in WS-Security digest form (nonce + timestamp + SHA-1 digest, not plaintext); RTSP basic/digest as the camera supports. LAN discovery sends multicast probes only from the machine's own interfaces; it does not scan address ranges.
- **Container**: runs as a non-root user (`uid 10001`), read-only root filesystem compatible (writes only to `/config` and `/data`), no capabilities required. Host networking is only needed for `discover` (multicast), not for `run`.
- **Supply chain**: the ffmpeg download is pinned to a release tag and verified by SHA-256 in the Dockerfile (a mismatch fails the build); Python deps are version-bounded (hash pinning is a later hardening step); GitHub Actions use minimal `permissions`. BtbN prunes old autobuild tags, so a 404 on the pin means: bump `FFMPEG_TAG`/`FFMPEG_NAME` and the sums (documented in the Dockerfile).

## 7. Failure model

State machine per camera: `starting -> streaming -> backoff -> starting ...`, plus terminal `revoked` (401) and `stopped` (shutdown / removed).

- **Reconnects**: whenever ffmpeg exits (camera dropped, network down, WHIP closed), wait with exponential backoff **1 s, 2 s, 4 s ... capped at 30 s**, with +/-20% jitter so many cameras do not stampede. The delay resets to 1 s once a session has been streaming for at least 60 s. There is no retry limit: networks come back.
- **401**: if ffmpeg's WHIP request reports HTTP 401/403, or a heartbeat returns 401, that camera **stops** (no retry), state `revoked`, with the log line `camera <name>: Percia rejected the camera token (removed or re-paired). Run: percia-bridge add <name> --force`. Other cameras keep running. `--force` removes the stale state and pairs again. The exit code of `run` is unaffected (other cameras still work); `status` shows it and exits 1.
- **Camera offline**: if the source cannot be opened (ffmpeg exits before producing a frame, or the ffprobe preflight times out), the camera is `backoff` with reason `source` and `percia_bridge_camera_up 0`. No heartbeat is sent while not streaming, so Percia shows the camera `offline` after 60 s (its normal rule) and pauses a watch after 60 minutes without video, as for phones. The Bridge keeps trying.
- **Stalls**: a frozen connection (ffmpeg alive, no new frames) is killed after 30 s without progress (`-progress` output is the watchdog) and treated as a normal drop.
- **Heartbeat failures** other than 401 (network, 5xx) are logged once per failure streak and retried on the next tick; they never stop video.
- **Graceful shutdown**: SIGTERM/SIGINT stops heartbeats, sends `q` to every ffmpeg so it closes the WHIP session (DELETE on the resource), waits up to 5 s, then kills.
- **Clock/ID safety**: each ffmpeg is a child with `start_new_session` so a supervisor crash under Docker/systemd leaves no orphan holding the camera (`--init`/`tini` in the image reaps).

## 8. Resource limits and transcoding

Goal: do the cheapest thing that produces video Percia's pool can decode (WebRTC/WHIP, H.264, no B-frames).

1. **Probe** the source once per (re)start with `ffprobe` (timeout 10 s): codec, profile, size, B-frames.
2. `mode: auto` (default):
   - H.264 without B-frames at or under the height cap -> `-c:v copy` (no decode, nearly zero CPU, no quality loss).
   - H.264 with B-frames, or taller than `max_height` (only when the user lowered it from the default of "unset"), or anything else (H.265/HEVC, MJPEG, MPEG-4) -> transcode.
3. Transcode: `libx264 -profile:v baseline -preset veryfast -tune zerolatency -pix_fmt yuv420p -g <2*fps> -bf 0 -vf scale=-2:min(ih\,max_height),fps=<max_fps> -maxrate <b>k -bufsize <2b>k`. Capped at 720p / 15 fps / 1500 kbps by default (enough for scene understanding; about 25-60% of one Pi 4 core). Users lower it per camera (`video.max_height: 480`).
4. `mode: copy` forces copy (fails visibly if the codec cannot be sent); `mode: transcode` forces re-encode.
5. Video only (`-an`), no audio ever leaves the network. No two ffmpegs per camera. For Frigate, the Bridge reads Frigate's go2rtc restream so the camera sees no extra connection.
6. Limits: `max_cameras` guard at 10 (the server limit); a `nice`-able subprocess (`nice -n 10` when transcoding) so a Pi stays responsive; ffmpeg stderr is read through a bounded line buffer (no unbounded memory growth); log lines per camera are rate-limited.

Hardware encoders (VAAPI, v4l2m2m, NVENC) are a possible later option (`video.encoder`), left out of v1 to keep one tested path.

## 9. Observability

- **Structured logs** to stdout: JSON lines by default in Docker (`{"ts","level","event","camera","state",...}`), human text on a TTY; `--log-level`. Events are stable names (`camera_streaming`, `camera_backoff`, `camera_revoked`, `heartbeat_failed`, `ffmpeg_exit`). All lines pass `redact()`.
- **`GET /healthz`** (loopback): JSON `{"status":"ok|degraded|down","version","uptime_s","cameras":[{name,state,mode,uptime_s,restarts,last_error,fps}]}`; HTTP 200 when at least one camera streams or none are configured, 503 when every camera is down. The Docker `HEALTHCHECK` calls `percia-bridge status --quiet`.
- **`GET /metrics`**: Prometheus text: `percia_bridge_up`, `percia_bridge_camera_up{camera}`, `..._camera_restarts_total`, `..._camera_fps`, `..._camera_streaming_seconds`, `..._heartbeat_failures_total`, `..._ffmpeg_speed`. Camera names are the labels (local only, never sent anywhere).
- **Anonymous product events (opt-in, off by default)**: `telemetry: true` plus a PostHog project key in `telemetry_key` (the Bridge ships without one). An event is a closed set of names and every property value must be one of a closed set; anything else is dropped before sending. Allowed: events `bridge_started`, `bridge_camera_streaming`, `bridge_camera_failed`, `bridge_camera_revoked`; properties `source_type` (`onvif|rtsp|frigate|homeassistant`), `mode` (`copy|transcode`), `reason` (`source|network|auth|ffmpeg|stall|other`), `arch` (`amd64|arm64|other`), `variant` (`docker|ha_addon|pip`), `version` (matches `^\d+\.\d+\.\d+$`). Never: names, URLs, IPs, camera counts above bucket `1|2-5|6-10`, error text. The distinct id is a random per-install UUID in the state dir, not linked to a Percia account. Tested: a test feeds hostile values and asserts they are dropped.
- Telemetry sending is fire-and-forget with a 2 s timeout and can never affect streaming.

## 10. Inputs

- **ONVIF** (Profile S and T): WS-Discovery probe (UDP multicast `239.255.255.250:3702`, type `NetworkVideoTransmitter`) from the machine's own interfaces; then per device `GetSystemDateAndTime` (unauthenticated, to correct clock skew in the digest), `GetCapabilities` (media XAddr) or `GetServices` (Media2 `ver20` XAddr), `GetProfiles`, `GetStreamUri` (RTP-Unicast over RTSP). Media2 is preferred when the device has it (it reports H.265 correctly), else Media1. Video only; events/PTZ/audio not touched. The resolved URI is re-queried at each (re)start so DHCP/port changes in the camera's own config do not break a camera.
- **Plain RTSP**: any `rtsp://` or `rtsps://` URL reachable from the Bridge.
- **Frigate**: `GET {url}/api/config` (or `--frigate-config /path/config.yml`) lists cameras; the stream used is Frigate's go2rtc restream `rtsp://<frigate-host>:8554/<stream>` where `<stream>` comes from the camera's `ffmpeg.inputs[].path` if it already points at the restream (`rtsp://127.0.0.1:8554/<stream>`), preferring the input with the `detect` role (lowest resolution that Frigate itself analyses), else the camera name. Frigate keeps the only connection to the real camera. Example compose file in `examples/frigate-compose.yml`.
- **Home Assistant**: an add-on (`homeassistant/`, with `repository.yaml` at the repo root). It lists `camera.*` entities through the Supervisor proxy (`http://supervisor/core/api/states`) and resolves the best available stream per entity, in order: (1) an explicit `rtsp_url` the user pasted for that entity in the add-on UI, (2) the entity's HLS stream through the websocket command `camera/stream` (needs HA's `stream` integration, usual for cameras with a stream source), (3) the HA camera proxy `/api/camera_proxy_stream/<entity>` (MJPEG, always transcoded) as the fallback. A small ingress web UI lets the user tick which cameras to send and shows state. The add-on runs the very same `percia-bridge run` core with a different config source.

## 11. Upgrade path

- Docker: tag `latest` follows `main`, `X.Y.Z` is pinned; `docker compose pull && up -d` upgrades. State (`/data`) and config (`/config`) are volumes and survive. A `state_version` field in `cameras.json` and `config_version` (implicit 1) allow migrations; unknown newer versions make the bridge refuse to start rather than corrupt state.
- pip/pipx: `pipx upgrade percia-bridge`.
- HA add-on: bump `version` in `homeassistant/config.yaml`; HA shows the update.
- `percia-bridge version` and the startup log line print the bridge and ffmpeg versions; the image labels carry the git SHA.
- Compatibility rule: the Bridge depends only on the camera API in section 4 (also in Percia's OpenAPI document). If that contract changes, the server keeps the old routes for at least one minor release; the Bridge's CI e2e uses a fake of this contract.

## 12. Testing strategy

Unit: config, redaction, ONVIF XML parsing against recorded fixtures, Frigate config parsing, HA entity/stream resolution, ffmpeg command building, supervisor backoff/401/stall logic with a fake ffmpeg and a fake Percia API (real local HTTP server). End to end (`scripts/e2e.py`): two local MediaMTX instances plus ffmpeg: one plays an "RTSP camera" looping a generated test clip, the other acts as the WHIP endpoint; a fake Percia API hands the Bridge the local WHIP URL; the script asserts the stream appears on the WHIP side and that a camera 401 stops it. No production Percia, no real keys, nothing outside localhost.

## 13. 0.2.0: camera-push API (supersedes parts of sections 3, 4 and 7)

Percia has a push camera kind. The Bridge uses it when the server has it and falls back to sections 4's create + claim flow when not (feature-detected from the create response: a `whipUrl` means push; a `pairUrl` or a 400/404 means the older flow).

- **Register** `POST /api/cameras {name, kind:"push", externalId}` returns `whipUrl` + token (once). `externalId` = `pb-` + SHA-256 of the camera's identity (ONVIF serial/model, Frigate camera name, HA entity id, or RTSP host:port/path without credentials; per-camera `external_id:` overrides). The same id again returns the same camera with `token: null`: if the state dir still has the token it is reused, else the Bridge rotates it. So restarts, wiped state and re-adds never duplicate.
- **401 handling**: a rejected token puts the camera in `revoked` with the hint `percia-bridge rotate "NAME"`. `rotate` calls `POST /api/cameras/:id/token` and writes the new token to the state dir; the revoked worker polls the state file (5 s) and restarts itself with the new token. Cameras made with the old flow have no rotatable token (`add NAME --force`).
- **Bulk**: `register` / `add-all` send two or more cameras to `POST /api/cameras/bulk` (chunks of 10, unique `externalId`s, idempotent; existing cameras come back with `token: null` and get their token from the state dir or a rotation). The server is all-or-nothing against the camera/plan limit (429, nothing created): every camera of that chunk is reported with the server's message and a retry just works. A 400 on a chunk re-does that chunk one by one to name the bad camera; a 404 (older server) falls back to one-by-one registration on one connection. Per-camera results; a bad key stops early.
- **Status**: local `/healthz` plus Percia's view from `GET /api/cameras` and `GET /api/cameras/:id/status` (`state` waiting/online/offline/revoked, `lastSeenAt`, `hint`; codec/resolution/fps/bitrate shown only if the server ever returns them). A 404 from the status route (older server) is remembered and the list data is used alone.
- **doctor / init / check**: see CHANGELOG. `init` and `doctor` use `GET /api/whoami` (scopes) and `GET /api/config` (`phoneCameraEnabled`).
