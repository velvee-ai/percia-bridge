"""Percia camera API client (the same path phone cameras use)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__
from .logs import register_secret
from .state import CameraState


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Unauthorized(ApiError):
    """401: the key or camera token is no longer valid."""


class FeatureOff(ApiError):
    """404 on the camera routes: this server has phone cameras switched off."""


class WrongKind(ApiError):
    """409 wrong_kind: a phone camera has no rotatable token (pair it again instead)."""


class Forbidden(ApiError):
    """403: the API key is valid but lacks a scope."""


@dataclass
class Registration:
    state: CameraState
    existing: bool = False  # Percia already had a camera with this externalId (nothing was created)
    rotated: bool = False  # it existed, we had no token, so the token was rotated to get one
    flow: str = "push"  # "push" (camera-push API) or "claim" (older servers: create + pairing claim)


def describe_exception(e: BaseException) -> str:
    """One human sentence for a failed call, never a traceback or a raw exception name."""
    if isinstance(e, ApiError):
        return e.message
    if isinstance(e, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.PoolTimeout):
        return "Percia did not answer in time (timeout). Check the internet connection; the bridge keeps retrying."
    if isinstance(e, httpx.ConnectError):
        txt = str(e).lower()
        if "name or service not known" in txt or "nodename nor servname" in txt or "temporary failure in name resolution" in txt:
            return "cannot resolve the Percia host name (DNS). Check the network or DNS settings."
        if "certificate" in txt or "ssl" in txt:
            return "TLS certificate problem talking to Percia. Check the system clock and any HTTPS-inspecting proxy."
        return "cannot connect to Percia (network down or HTTPS blocked outbound). Check the internet connection."
    if isinstance(e, httpx.HTTPError):
        return f"network error talking to Percia ({type(e).__name__})."
    return f"{type(e).__name__}: {e}"


def read_api_key(env_name: str = "PERCIA_API_KEY", file: Path | None = None) -> str:
    key = os.environ.get(env_name, "").strip()
    if not key and file:
        try:
            mode = file.stat().st_mode
        except OSError:
            raise ApiError(0, "no_api_key", f"API key file not found: {file}") from None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ApiError(0, "key_file_open", f"{file} is readable by other users; run: chmod 600 {file}")
        key = file.read_text().strip()
    if not key:
        raise ApiError(0, "no_api_key", f"No API key: set {env_name} or run `percia-bridge init`.")
    register_secret(key)
    return key


def pairing_token(pair_url_or_token: str) -> str:
    """POST /api/cameras returns pairUrl .../cam/<token>; the claim wants the token."""
    return pair_url_or_token.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]


def _check_https(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ApiError(0, "insecure_url", "refusing a non-https URL from the server")


def _err(r: httpx.Response, off_hint: bool = False) -> ApiError:
    try:
        body = r.json()
        err = body.get("error", body)
        code, msg = (err.get("code") or "error"), (err.get("message") or r.text[:200])
    except Exception:
        code, msg = "error", r.text[:200]
    if r.status_code == 401:
        return Unauthorized(401, code, msg or "Unauthorized")
    if r.status_code == 403:
        return Forbidden(403, code, msg or "This API key does not have the needed scope. Create one with scopes cameras:read, cameras:write and watches:delete.")
    if r.status_code == 409 and code == "wrong_kind":
        return WrongKind(409, code, msg)
    if r.status_code == 429 and code == "camera_limit":
        return ApiError(429, code, "Your Percia account already has 10 cameras. Remove one (percia-bridge remove NAME) first.")
    if r.status_code == 404 and off_hint:
        return FeatureOff(404, "not_found", "This Percia server does not have cameras enabled (phone cameras are switched off).")
    return ApiError(r.status_code, code, msg)


BULK_MAX = 10  # POST /api/cameras/bulk takes 1-10 cameras


class PerciaApi:
    def __init__(self, server: str, client: httpx.AsyncClient | None = None):
        self.server = server.rstrip("/")
        self.push_supported: bool | None = None  # learned from the first register call
        self.bulk_supported: bool | None = None  # learned from the first bulk call (404 = an older server)
        self.status_supported: bool | None = None  # learned from the first per-camera status call (404 = an older server)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers={"user-agent": f"percia-bridge/{__version__}"})

    async def aclose(self) -> None:
        await self.client.aclose()

    async def verify_key(self, key: str) -> None:
        r = await self.client.get(f"{self.server}/api/cameras", headers={"authorization": f"Bearer {key}"})
        if r.status_code != 200:
            raise _err(r, off_hint=True)

    def _auth(self, key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {key}"}

    async def register(self, name: str, key: str, external_id: str = "", known: CameraState | None = None) -> Registration:
        """Register a camera with Percia, idempotently. Uses the camera-push API when the server has it and falls back to the
        older create + pairing-claim flow when it does not, so one bridge works against both."""
        if self.push_supported is not False:
            body: dict = {"name": name, "kind": "push"}
            if external_id:
                body["externalId"] = external_id
            r = await self.client.post(f"{self.server}/api/cameras", json=body, headers=self._auth(key))
            if r.status_code in (200, 201):
                data = r.json()
                if data.get("whipUrl"):
                    self.push_supported = True
                    return await self._finish_push(data, name, key, external_id, known)
                self.push_supported = False  # an older server ignored `kind` and made a phone camera: claim it
                return Registration(await self._claim(data, key), flow="claim")
            if r.status_code not in (400, 404, 405, 422):
                raise _err(r, off_hint=True)
            self.push_supported = False  # an older server rejected the new fields: fall through to the old create
        r = await self.client.post(f"{self.server}/api/cameras", json={"name": name}, headers=self._auth(key))
        if r.status_code not in (200, 201):
            raise _err(r, off_hint=True)
        return Registration(await self._claim(r.json(), key), flow="claim")

    async def register_bulk(self, items: list[tuple[str, str, CameraState | None]], key: str) -> list[Registration | Exception] | None:
        """Register many push cameras with POST /api/cameras/bulk (up to 10 per call, each idempotent by externalId).

        `items` is [(name, externalId, known state or None)]; externalIds must be unique. Returns one Registration or Exception per
        item, in order, so one bad camera never hides the others. Returns None when bulk cannot be used (an older server without the
        route, or one without the push API): the caller then registers one at a time. All-or-nothing server errors (camera_limit,
        plan_limit) fail every camera of that chunk with the server's message; nothing was created for it."""
        if self.push_supported is False or self.bulk_supported is False:
            return None
        out: list[Registration | Exception | None] = [None] * len(items)
        for start in range(0, len(items), BULK_MAX):
            chunk = items[start:start + BULK_MAX]
            body = {"cameras": [{"name": n, "externalId": e} for n, e, _ in chunk]}
            r = await self.client.post(f"{self.server}/api/cameras/bulk", json=body, headers=self._auth(key))
            if r.status_code in (404, 405):
                if start == 0:
                    self.bulk_supported = False  # no such route (or cameras are off: the single-camera path then says so)
                    return None
                err: Exception = _err(r, off_hint=True)
                for i in range(start, start + len(chunk)):
                    out[i] = err
                continue
            if r.status_code == 400:
                # the server rejected the chunk as a whole without saying which camera: do these one at a time to find out
                for i, (n, e, k) in enumerate(chunk, start):
                    try:
                        out[i] = await self.register(n, key, e, k)
                    except Exception as ex:  # noqa: BLE001
                        out[i] = ex
                continue
            if r.status_code not in (200, 201):
                err = _err(r, off_hint=True)
                for i in range(start, start + len(chunk)):
                    out[i] = err
                if isinstance(err, Unauthorized | Forbidden | FeatureOff):  # same answer for every chunk: stop
                    for i in range(start + len(chunk), len(items)):
                        out[i] = err
                    break
                continue
            self.bulk_supported = True
            self.push_supported = True
            data = r.json()
            entries = data.get("cameras") if isinstance(data, dict) else None
            if not isinstance(entries, list) or len(entries) != len(chunk):
                for i in range(start, start + len(chunk)):
                    out[i] = ApiError(r.status_code, "bad_response", "Percia's bulk answer did not match the request; run `percia-bridge register` again (it is safe to repeat).")
                continue
            by_ext = {(d.get("camera") or {}).get("externalId"): d for d in entries if isinstance(d, dict)}
            for i, (n, e, k) in enumerate(chunk, start):
                d = by_ext.get(e) or entries[i - start]
                try:
                    if not isinstance(d, dict) or not d.get("whipUrl"):
                        raise ApiError(r.status_code, "bad_response", f"Percia returned no WHIP URL for {n}.")
                    out[i] = await self._finish_push(d, n, key, e, k)
                except Exception as ex:  # noqa: BLE001
                    out[i] = ex
        return [x if x is not None else ApiError(0, "error", "not registered") for x in out]

    async def _finish_push(self, data: dict, name: str, key: str, external_id: str, known: CameraState | None) -> Registration:
        cam_id = str((data.get("camera") or {}).get("id") or "")
        whip = data["whipUrl"]
        _check_https(whip)
        token = data.get("token")
        existing = bool(data.get("existing"))
        rotated = False
        if token:
            register_secret(token)
        elif known and known.camera_id == cam_id and known.token and known.kind == "push":
            token = known.token  # re-registered after a restart: we already hold the token
        else:
            token, whip = await self.rotate_token(cam_id, key)  # Percia shows the token once; we lost ours
            rotated = True
        return Registration(CameraState(cam_id, token, whip, external_id, "push"), existing=existing, rotated=rotated)

    async def _claim(self, created: dict, key: str) -> CameraState:
        pair = pairing_token(created["pairUrl"])
        register_secret(pair)
        c = await self.client.post(f"{self.server}/api/cam/claim", json={"pairing": pair})
        if c.status_code != 200:
            raise _err(c, off_hint=True)
        body = c.json()
        register_secret(body["token"])
        _check_https(body["whipUrl"])
        return CameraState(body["cameraId"], body["token"], body["whipUrl"], "", "phone")

    async def create_and_claim(self, name: str, key: str) -> CameraState:
        """Old flow only (kept for callers/tests that want it)."""
        r = await self.client.post(f"{self.server}/api/cameras", json={"name": name}, headers=self._auth(key))
        if r.status_code not in (200, 201):
            raise _err(r, off_hint=True)
        return await self._claim(r.json(), key)

    async def rotate_token(self, camera_id: str, key: str) -> tuple[str, str]:
        """POST /api/cameras/:id/token -> (new token, whip url). The old token stops working at once."""
        r = await self.client.post(f"{self.server}/api/cameras/{camera_id}/token", headers=self._auth(key))
        if r.status_code != 200:
            raise _err(r, off_hint=True)
        body = r.json()
        register_secret(body["token"])
        _check_https(body["whipUrl"])
        return body["token"], body["whipUrl"]

    async def revoke_token(self, camera_id: str, key: str) -> None:
        r = await self.client.delete(f"{self.server}/api/cameras/{camera_id}/token", headers=self._auth(key))
        if r.status_code not in (200, 204):
            raise _err(r)

    async def list_cameras(self, key: str) -> list[dict]:
        r = await self.client.get(f"{self.server}/api/cameras", headers=self._auth(key))
        if r.status_code != 200:
            raise _err(r, off_hint=True)
        data = r.json()
        return data if isinstance(data, list) else data.get("cameras", [])

    async def camera_status(self, camera_id: str, key: str) -> dict | None:
        """GET /api/cameras/:id/status -> {state, status, lastSeenAt, secondsSinceSeen, watchCount, hint, ...}; codec, resolution, fps
        and bitrate only if the server reports them. None when this server has no such route (older servers) or the camera is gone;
        after the first such answer the route is not asked again."""
        if self.status_supported is False:
            return None
        r = await self.client.get(f"{self.server}/api/cameras/{camera_id}/status", headers=self._auth(key))
        if r.status_code in (404, 405):
            if self.status_supported is None:
                self.status_supported = False
            return None
        if r.status_code == 200:
            self.status_supported = True
        if r.status_code != 200:
            raise _err(r)
        return r.json()

    async def whoami(self, key: str) -> dict:
        r = await self.client.get(f"{self.server}/api/whoami", headers=self._auth(key))
        if r.status_code != 200:
            raise _err(r)
        return r.json()

    async def server_config(self) -> dict:
        r = await self.client.get(f"{self.server}/api/config")
        return r.json() if r.status_code == 200 else {}

    async def delete_camera(self, camera_id: str, key: str) -> None:
        r = await self.client.delete(f"{self.server}/api/cameras/{camera_id}", headers={"authorization": f"Bearer {key}"})
        if r.status_code not in (200, 204, 404):
            raise _err(r)

    async def heartbeat(self, token: str) -> None:
        r = await self.client.post(f"{self.server}/api/cam/heartbeat", headers={"authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            raise _err(r)
