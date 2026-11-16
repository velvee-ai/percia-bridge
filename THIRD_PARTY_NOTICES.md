# Third-party software

The Percia Bridge source code is MIT licensed (see [LICENSE](LICENSE)). The Docker image contains other software under its own licenses.

## ffmpeg (GPL)

The image includes a static **ffmpeg** and **ffprobe** from the BtbN FFmpeg-Builds project (`gpl` variant, includes libx264 and OpenSSL), pinned in the [Dockerfile](Dockerfile) by release tag and SHA-256. It is licensed under the **GNU General Public License version 3** (built with `--enable-gpl --enable-version3`; see the notice printed by `ffmpeg -L`). The Bridge runs it as a separate program (it only starts the `ffmpeg` command); the Bridge's own code is not linked with it.

**Source code offer.** The complete corresponding source of that ffmpeg build, including the build scripts and patches, is public:
- Binaries and build scripts: https://github.com/BtbN/FFmpeg-Builds (release tag in the Dockerfile `FFMPEG_TAG`)
- FFmpeg source: https://ffmpeg.org/download.html and https://github.com/FFmpeg/FFmpeg (the exact commit is in the build name, `FFMPEG_NAME`)
- libx264 source: https://code.videolan.org/videolan/x264

If you received this image and cannot get the source from those places, write to **support@velvee.ai** and we will provide it for at least three years after the image was published.

## Other components

- Python packages installed by `pip`: httpx (BSD-3-Clause), PyYAML (MIT), websockets (BSD-3-Clause), and their dependencies. `pip show NAME` inside the image lists each license.
- Base image: `python:3.12-slim-trixie` (Debian); Debian packages keep their licenses under `/usr/share/doc/*/copyright` (includes `tini`, MIT).
