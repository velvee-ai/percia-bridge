# Thin add-on image: the published multi-arch Percia Bridge image plus the add-on entry point.
# Built and pushed by .github/workflows/release.yml as ghcr.io/velvee-ai/percia-bridge-addon (config.yaml `image:`),
# so Home Assistant pulls it instead of building on the device. The bridge image of the same version is built first.
ARG BRIDGE_VERSION=0.2.0
FROM ghcr.io/velvee-ai/percia-bridge:${BRIDGE_VERSION}

USER root
ENV PERCIA_BRIDGE_VARIANT=ha_addon
# The Supervisor mounts /data (options.json, our state) as root.
ENTRYPOINT ["tini", "--", "python", "-m", "percia_bridge.ha_addon"]
CMD []
