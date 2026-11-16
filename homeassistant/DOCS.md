# Percia Bridge

Sends the Home Assistant cameras you pick to [Percia](https://percia.ai) so it can watch them for a plain-language goal. Only video leaves your home, never audio. The add-on only connects out; nothing is opened on your network.

## Set up

1. Create a Percia API key with the scope `cameras:write` and paste it into **Configuration → API key**.
2. Start the add-on and open **Percia Bridge** in the sidebar.
3. Tick the cameras to send and press **Save**. Each one is created in your Percia account and starts streaming.
4. In Percia, create a watch on the camera (its source is `percia-cam://<id>`).

At most 10 cameras per Percia account.

## Which stream is used

For each camera the add-on tries, in order:

1. **The stream URL you typed** in the page (an `rtsp://` address; for example the camera's own RTSP URL). Best: no re-encoding if the camera sends H.264.
2. **Home Assistant's stream** (HLS) for cameras that have a stream source (needs the `stream` integration, which most RTSP/ONVIF/UniFi/Reolink cameras use).
3. **The camera proxy image stream** (MJPEG). Always re-encoded, uses more CPU, lower quality. A fallback only.

## Options

| Option | Meaning |
|---|---|
| `api_key` | Percia API key (scope `cameras:write`). Stored only in this add-on's configuration. |
| `server` | Percia address. Leave the default. |
| `max_height`, `max_fps`, `max_bitrate_kbps` | Caps used only when a camera must be re-encoded (non-H.264 or H.264 with B-frames). H.264 streams are passed through untouched. |
| `log_level` | `info` is fine. Logs never contain keys or camera passwords. |

## Troubleshooting

- **A camera shows `backoff`**: Home Assistant cannot give a stream. Paste the camera's RTSP URL into the Stream URL box.
- **`revoked`**: the camera was deleted in Percia or paired again elsewhere. Untick it, Save, tick it, Save.
- **High CPU**: the camera is being re-encoded (see the mode in the add-on log). Use a camera's H.264 sub-stream or lower `max_height`.
