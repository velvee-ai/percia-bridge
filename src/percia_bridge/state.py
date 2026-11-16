"""Per-camera tokens in the state dir (cameras.json, 0600). Tokens are registered for log redaction on load."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import write_private
from .logs import register_secret

STATE_VERSION = 1


class StateError(Exception):
    pass


@dataclass
class CameraState:
    camera_id: str
    token: str
    whip_url: str
    external_id: str = ""  # what we registered with Percia (idempotency key); "" for old claim-flow cameras
    kind: str = "phone"  # "push" (token is rotatable) | "phone" (old pair+claim flow)


class State:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.file = state_dir / "cameras.json"
        self.cameras: dict[str, CameraState] = {}
        self.install_id: str = ""
        self.load()

    def load(self) -> None:
        if not self.file.exists():
            self.cameras = {}
            return
        try:
            raw = json.loads(self.file.read_text())
        except json.JSONDecodeError as e:
            raise StateError(f"{self.file} is not valid JSON ({e}); restore it or remove it and re-add the cameras") from None
        if raw.get("state_version", 1) > STATE_VERSION:
            raise StateError(f"{self.file} was written by a newer percia-bridge (state_version {raw['state_version']}); upgrade before running")
        self.install_id = raw.get("install_id", "")
        self.cameras = {k: CameraState(v["camera_id"], v["token"], v["whip_url"], v.get("external_id", ""), v.get("kind", "phone")) for k, v in raw.get("cameras", {}).items()}
        for c in self.cameras.values():
            register_secret(c.token)

    def save(self) -> None:
        if not self.install_id:
            self.install_id = str(uuid.uuid4())
        data = {"state_version": STATE_VERSION, "install_id": self.install_id,
                "cameras": {k: vars(v) for k, v in self.cameras.items()}}
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        write_private(self.file, json.dumps(data, indent=2))

    def get(self, name: str) -> CameraState | None:
        return self.cameras.get(name)

    def put(self, name: str, cam: CameraState) -> None:
        register_secret(cam.token)
        self.cameras[name] = cam
        self.save()

    def drop(self, name: str) -> CameraState | None:
        cam = self.cameras.pop(name, None)
        if cam:
            self.save()
        return cam
