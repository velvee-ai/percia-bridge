# Changelog

## 0.2.0
Uses Percia's **camera-push API** and makes first-time setup much easier. Older Percia servers keep working through fallbacks.

- **Bulk registration.** `register` and `add-all` send two or more cameras to `POST /api/cameras/bulk` in chunks of 10 (idempotent by `externalId`; cameras Percia already has come back without a token and get theirs from the state dir or a rotation). The server is all-or-nothing against the camera and plan limits: every camera of that call is reported with the server's message and nothing is half-created, so retrying is safe. A rejected chunk is re-done one by one to name the bad camera. A 404 falls back to the old one-by-one loop.
- **`status` uses `GET /api/cameras/:id/status`.** Shows Percia's state (online, waiting, offline, revoked) with its hint, and last publish. Codec, resolution, fps and bitrate appear only if the server returns them; today it does not (needs a media-server change on Percia's side). A 404 (older server) falls back to `GET /api/cameras`.

- **Push API with automatic fallback.** `POST /api/cameras {kind:"push", externalId}` returns the WHIP URL and a publish token directly (no pairing link). Against an older Percia server the bridge falls back to the previous create + claim flow, so one release works with both.
- **Idempotent registration.** Every camera is registered under a stable `externalId` (a hash of its identity: ONVIF serial, Frigate camera name, Home Assistant entity id, or the RTSP host/path without credentials). Restarts, a wiped state directory, re-adding, or a reinstall never create a duplicate camera. Override with `external_id:` per camera.
- **Token rotation.** New `percia-bridge rotate NAME` (and `--revoke`). On a rejected token the bridge says exactly what to run, and a running bridge picks up the new token from the state directory within seconds, no restart.
- **Bulk.** `percia-bridge register` (all configured cameras) and `percia-bridge add-all --frigate URL [--only a,b]`; one failure never stops the rest.
- **`status`** now shows per camera: bridge state, online, last publish time and video (codec, resolution, fps when the server reports them).
- **`doctor`** checks ffmpeg and the WHIP muxer, DNS and HTTPS to Percia, media.percia.ai and playground.percia.ai, clock skew, API key scopes, that cameras are enabled, each camera's reachability and codec (with an H.264 Baseline hint), and states plainly that only outbound connections are used.
- **`init`** wizard: hidden key prompt with retries, validates the key, its scopes (`cameras:read`, `cameras:write`, `watches:delete`) and that cameras are enabled, then prints next steps.
- **`discover`** prints a ready `add` command for every ONVIF, Frigate and Home Assistant camera found; `add --homeassistant ENTITY`.
- **`check`** validates the config; unknown keys get "did you mean" hints.
- Clear human network errors (DNS, TLS, timeouts); reconnects use exponential backoff with jitter.
- Tests: a simulated camera end to end (looping clip -> local RTSP server -> bridge -> local WHIP server that authorises publishes through a fake Percia API).

## 0.1.0
First release: ONVIF, RTSP, Frigate and Home Assistant cameras to Percia over WHIP.
