# Releasing (maintainers)

Releases are made by pushing a version tag. Nothing is published by merging to `main`.

1. Bump the version in **both** `pyproject.toml` and `homeassistant/config.yaml`, and add a `## X.Y.Z` section to `CHANGELOG.md` (its text becomes the GitHub release notes). Also update `homeassistant/CHANGELOG.md`.
2. Run the suite: `pytest` (unit tests + the simulated-camera end to end, which needs ffmpeg with the `whip` muxer and `mediamtx`).
3. Merge to `main`, wait for CI to be green, then tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. `.github/workflows/release.yml` then checks that the tag, `pyproject.toml`, `homeassistant/config.yaml` and `CHANGELOG.md` agree, and builds and pushes
   - `ghcr.io/velvee-ai/percia-bridge:X.Y.Z`, `:X.Y`, `:latest` (linux/amd64 + linux/arm64),
   - `ghcr.io/velvee-ai/percia-bridge-addon:X.Y.Z`, `:latest` (the Home Assistant add-on),
   and creates the GitHub release.
5. Verify from a machine that is not logged in to GitHub: `docker pull ghcr.io/velvee-ai/percia-bridge:X.Y.Z && docker run --rm ghcr.io/velvee-ai/percia-bridge:X.Y.Z version`. The first time, both packages must be set to **public** in GitHub (Packages -> package settings).
6. If the Home Assistant add-on lives in a separate repository (`percia-ha-addon`), copy `homeassistant/*` there and commit with the same version.
7. Optional PyPI: `python -m build && twine upload dist/*` (no workflow; needs a PyPI token).

## Bumping ffmpeg
The Dockerfile pins a static BtbN build by release tag and SHA-256 per architecture (instructions at the top of the Dockerfile). Update `THIRD_PARTY_NOTICES.md` if the license changes.

## Local builds
```bash
docker buildx build --platform linux/amd64 --load -t percia-bridge:dev .
docker buildx build --platform linux/arm64 --load -t percia-bridge:dev-arm64 .
```
