"""A real local HTTP server that speaks the Percia camera API (setup + heartbeat), for supervisor and CLI tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-key-not-real"
TOKEN = "pc_TESTTOKEN0123456789abcdefABCDEF"
PAIR = "PAIRTOKEN0123456789"


class FakePercia:
    def __init__(self):
        self.created, self.claimed, self.deleted = [], 0, []
        self.heartbeats, self.bad_heartbeats = 0, 0
        self.revoked = False
        self.cameras_enabled = True
        self.delete_status = 204
        self.camera_limit = False
        # Camera-push API (spec: percia-cf docs/specs/cameras/camera-push-api.md). Off = an older server that ignores `kind`.
        self.push = False
        self.strict_create = False  # older server that answers 400 to unknown fields
        self.scopes = ["cameras:read", "cameras:write", "watches:delete"]
        self.cameras_status_route = False
        self.status_video = False  # the final contract has no codec/resolution/bitrate yet; True simulates a future server that does
        self.bulk = True  # POST /api/cameras/bulk exists (percia-cf bb95c92). False = an older push server without it
        self.bulk_calls = []  # externalIds per bulk request
        self.status_calls = 0
        self.plan_limit = False
        self.push_cams: dict[str, dict] = {}  # id -> {name, externalId, token, whip, lastSeenAt}
        self.tokens_issued = 0
        self.rotated = []
        self.revoked_tokens = []
        outer = self

        self.whip_base = None  # e2e: a real local MediaMTX WHIP endpoint
        self.publish_ok, self.publish_denied = [], []  # tokens MediaMTX asked us about (the real server's /api/internal/media/auth)

        def whip(cid):
            return f"{outer.whip_base or outer.url + '/media'}/cam/{cid}/whip"

        def new_token():
            outer.tokens_issued += 1
            return TOKEN if outer.tokens_issued == 1 else f"pc_ROTATED{outer.tokens_issued:02d}0123456789abcdefABCDEF"

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _err(self, code, c, m):
                self._send(code, {"error": {"code": c, "message": m}})

            def do_GET(self):
                if self.path == "/api/config":
                    return self._send(200, {"phoneCameraEnabled": outer.cameras_enabled})
                if self.path == "/api/whoami":
                    if self.headers.get("authorization") != f"Bearer {KEY}":
                        return self._err(401, "unauthenticated", "Unauthorized.")
                    return self._send(200, {"userId": "u", "credential": "key", "scopes": outer.scopes})
                if not outer.cameras_enabled:
                    return self._err(404, "not_found", "Not found.")
                if self.headers.get("authorization") == f"Bearer {KEY}" and "cameras:read" not in outer.scopes:
                    return self._err(403, "forbidden", "Missing scope cameras:read.")
                if self.headers.get("authorization") == f"Bearer {KEY}" and self.path.endswith("/status"):
                    cid = self.path.split("/")[-2]
                    if not outer.cameras_status_route or cid not in outer.push_cams:
                        return self._err(404, "not_found", "Not found.")
                    c = outer.push_cams[cid]
                    outer.status_calls += 1
                    seen = c["lastSeenAt"]
                    state = "revoked" if not c["token"] else "waiting" if not seen else "online" if not c.get("stale") else "offline"
                    body = {"id": cid, "kind": "push", "state": state, "status": {"waiting": "never", "revoked": "offline"}.get(state, state), "lastSeenAt": seen,
                            "secondsSinceSeen": None if not seen else 5 if state == "online" else 3600, "watchCount": 0,
                            "hint": None if state == "online" else f"hint for {state}"}
                    if outer.status_video:
                        body["video"] = {"codec": "h264", "width": 640, "height": 360, "fps": 15, "bitrate": 1500000}
                    return self._send(200, body)
                if self.headers.get("authorization") == f"Bearer {KEY}" and self.path == "/api/cameras" and outer.push:
                    return self._send(200, [{"id": c["id"], "name": c["name"], "kind": "push", "externalId": c["externalId"], "status": "online" if c["lastSeenAt"] else "offline",
                                             "lastSeenAt": c["lastSeenAt"], "watchCount": 0} for c in outer.push_cams.values()])
                if self.headers.get("authorization") != f"Bearer {KEY}":
                    return self._err(401, "unauthenticated", "Unauthorized.")
                self._send(200, [])

            def do_DELETE(self):
                if self.headers.get("authorization") != f"Bearer {KEY}":
                    return self._err(401, "unauthenticated", "Unauthorized.")
                if self.path.endswith("/token"):
                    cid = self.path.split("/")[-2]
                    outer.revoked_tokens.append(cid)
                    if cid in outer.push_cams:
                        outer.push_cams[cid]["token"] = None
                    return self._send(200, {"ok": True})
                outer.deleted.append(self.path.rsplit("/", 1)[-1])
                if outer.delete_status == 409:
                    return self._err(409, "camera_in_use", "A watch uses this camera.")
                self.send_response(outer.delete_status)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self):
                body = self._body()
                auth = self.headers.get("authorization", "")
                if not outer.cameras_enabled:
                    return self._err(404, "not_found", "Not found.")
                if self.path == "/api/internal/media/auth":  # MediaMTX external auth: publish only with the camera's current token
                    path = body.get("path", "")
                    cid = path.split("/", 1)[-1]
                    cam = outer.push_cams.get(cid)
                    if body.get("action") == "publish" and cam and cam["token"] and body.get("token") == cam["token"]:
                        outer.publish_ok.append(body["token"])
                        cam["lastSeenAt"] = "2026-11-14T12:00:00Z"
                        return self._send(200, {})
                    if body.get("action") != "publish":
                        return self._send(200, {})
                    outer.publish_denied.append(body.get("token", ""))
                    return self._err(401, "unauthenticated", "denied")
                if self.path.startswith("/api/cameras/") and self.path.endswith("/token"):
                    if auth != f"Bearer {KEY}":
                        return self._err(401, "unauthenticated", "Unauthorized.")
                    if "watches:delete" not in outer.scopes:
                        return self._err(403, "forbidden", "Missing scope watches:delete.")
                    cid = self.path.split("/")[-2]
                    if not outer.push or cid not in outer.push_cams:
                        return self._err(409 if cid == "cam_1" and outer.push is False else 404, "wrong_kind" if outer.push is False else "not_found", "nope")
                    tok = new_token()
                    outer.push_cams[cid]["token"] = tok
                    outer.rotated.append(cid)
                    return self._send(200, {"token": tok, "whipUrl": whip(cid), "expiresAt": None})
                if self.path == "/api/cameras/bulk":
                    if not outer.bulk or not outer.push:
                        return self._err(404, "not_found", "Not found.")
                    if auth != f"Bearer {KEY}":
                        return self._err(401, "unauthenticated", "Unauthorized.")
                    items = body.get("cameras")
                    exts = [i.get("externalId") for i in items or []]
                    if not items or len(items) > 10 or len(set(exts)) != len(exts) or not all(i.get("name") and i.get("externalId") for i in items):
                        return self._err(400, "invalid_request", "1-10 cameras with unique externalIds.")
                    outer.bulk_calls.append(exts)
                    fresh = [i for i in items if not any(c["externalId"] == i["externalId"] for c in outer.push_cams.values())]
                    if outer.plan_limit and fresh:
                        return self._err(429, "plan_limit", "Your plan allows fewer cameras.")
                    if outer.camera_limit or len(outer.push_cams) + len(fresh) > 10:
                        return self._err(429, "camera_limit", "At most 10 cameras.")
                    out = []
                    for i in items:
                        found = next((c for c in outer.push_cams.values() if c["externalId"] == i["externalId"]), None)
                        if found:
                            out.append({"camera": {"id": found["id"], "name": found["name"], "kind": "push", "externalId": found["externalId"]},
                                        "whipUrl": whip(found["id"]), "token": None, "expiresAt": None, "existing": True})
                            continue
                        cid = f"cam_push{len(outer.push_cams) + 1}"
                        tok = new_token()
                        outer.push_cams[cid] = {"id": cid, "name": i["name"], "externalId": i["externalId"], "token": tok, "lastSeenAt": None}
                        outer.created.append(i["name"])
                        out.append({"camera": {"id": cid, "name": i["name"], "kind": "push", "externalId": i["externalId"]}, "whipUrl": whip(cid), "token": tok,
                                    "expiresAt": None, "existing": False})
                    return self._send(201 if fresh else 200, {"cameras": out})
                if self.path == "/api/cameras":
                    if auth != f"Bearer {KEY}":
                        return self._err(401, "unauthenticated", "Unauthorized.")
                    if outer.strict_create and set(body) - {"name"}:
                        return self._err(400, "invalid_request", "Unrecognized key(s) in object")
                    if outer.push and body.get("kind") == "push":
                        ext = body.get("externalId")
                        found = next((c for c in outer.push_cams.values() if ext and c["externalId"] == ext), None)
                        if found:
                            return self._send(200, {"camera": {"id": found["id"], "name": found["name"], "kind": "push"}, "whipUrl": whip(found["id"]), "token": None,
                                                    "expiresAt": None, "existing": True})
                        if outer.camera_limit:
                            return self._err(429, "camera_limit", "At most 10 cameras.")
                        cid = f"cam_push{len(outer.push_cams) + 1}"
                        tok = new_token()
                        outer.push_cams[cid] = {"id": cid, "name": body["name"], "externalId": ext, "token": tok, "lastSeenAt": None}
                        outer.created.append(body["name"])
                        return self._send(201, {"camera": {"id": cid, "name": body["name"], "kind": "push"}, "whipUrl": whip(cid), "token": tok, "expiresAt": None,
                                                "publish": {"ffmpegCopy": "ffmpeg ... $PERCIA_CAMERA_TOKEN"}})
                    if outer.camera_limit:
                        return self._err(429, "camera_limit", "At most 10 cameras.")
                    outer.created.append(body["name"])
                    return self._send(201, {"camera": {"id": "cam_1"}, "pairUrl": f"{outer.url}/cam/{PAIR}", "pairExpiresAt": "x"})
                if self.path == "/api/cam/claim":
                    if body.get("pairing") != PAIR:
                        return self._err(400, "pairing_invalid", "bad pairing")
                    outer.claimed += 1
                    return self._send(200, {"cameraId": "cam_1", "name": "n", "token": TOKEN, "whipUrl": f"{outer.url}/media/cam/cam_1/whip"})
                if self.path == "/api/cam/heartbeat":
                    live = {f"Bearer {c['token']}" for c in outer.push_cams.values() if c["token"]}
                    ok_auth = auth in live if outer.push else auth == f"Bearer {TOKEN}"
                    if not ok_auth or outer.revoked:
                        outer.bad_heartbeats += 1
                        return self._err(401, "unauthenticated", "Unauthorized.")
                    outer.heartbeats += 1
                    for c in outer.push_cams.values():
                        if auth == f"Bearer {c['token']}":
                            c["lastSeenAt"] = "2026-11-14T12:00:00Z"
                    return self._send(200, {"ok": True})
                self._err(404, "not_found", "nope")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
