# Security policy

## Reporting a vulnerability

Please report it **privately**, and please do not open a public issue:

- GitHub: **Security -> Report a vulnerability** on this repository (private advisory), or
- email **support@velvee.ai** with a subject starting **[security]**.

Include the Bridge version, how you run it (Docker, Home Assistant add-on, pip) and the steps to reproduce. **Do not include real API keys, camera tokens or camera passwords.** We aim to acknowledge a report within 3 working days and to tell you what we plan to do within 10. We will credit you in the release notes unless you prefer not.

Supported versions: the latest release of Percia Bridge.

## What the Bridge can and cannot do (security model)

- **Outbound only.** It opens no listening port for the internet. Its only listener is the health/metrics page on `127.0.0.1:9477` (loopback) and, in the Home Assistant add-on, the ingress page that Home Assistant itself fronts. Percia never connects to your network.
- **What leaves your network:** video only (never audio) from the cameras you add, over WebRTC/WHIP to Percia's media server; HTTPS API calls to Percia to register cameras and read their status (camera names, an id derived from the camera identity, status). Optional anonymous product events are **off by default** and carry only allow-listed values (version, architecture, source type); see `docs/design.md`.
- **API key** (`cameras:read`, `cameras:write`, `watches:delete`): can create, list, rotate and delete cameras in your Percia account. It cannot read video. It is stored in a `0600` file or the `PERCIA_API_KEY` variable, never on a command line. Revoke it any time in Percia settings.
- **Camera token** (`pc_...`, one per camera): can only publish video for that camera. Stored in `/data/cameras.json` (`0600`). Rotate with `percia-bridge rotate NAME`; `--revoke` stops the camera.
- **Camera passwords** are used only on your LAN. Keys, tokens and passwords are masked in all log lines. TLS is always used towards Percia.
- **Known limitation:** ffmpeg receives the camera RTSP URL (with its password) as an argument, visible in `ps` to other users of the same host. In Docker that is only the container; on a shared machine run the Bridge as its own user.
- The container runs as a non-root user. Images are built by GitHub Actions from tagged commits; the bundled ffmpeg is pinned and verified by SHA-256 at build time.
