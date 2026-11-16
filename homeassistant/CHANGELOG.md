# Changelog

## 0.2.0
- Uses Percia's camera-push API: cameras are registered idempotently (restarts and re-adds never duplicate), tokens can be rotated.
- Clearer errors, exponential reconnect with jitter.

## 0.1.0
- First release: pick Home Assistant cameras to send to Percia.
