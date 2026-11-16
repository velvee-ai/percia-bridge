"""Home Assistant add-on entry point: pick which camera entities to send (ingress web UI) and run the bridge for them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

from . import __version__
from . import homeassistant as ha
from .api import ApiError, PerciaApi
from .bridge import Bridge, make_resolver
from .config import CameraCfg, Config, Source, Video, parse_video, validate_server
from .httpd import json_response, serve
from .logs import event, register_secret, setup_logging, strip_credentials
from .state import State
from .supervisor import Timing
from .telemetry import Telemetry

OPTIONS_PATH = Path(os.environ.get("PERCIA_ADDON_OPTIONS", "/data/options.json"))
SELECTION_PATH = Path(os.environ.get("PERCIA_ADDON_SELECTION", "/data/selection.json"))
INGRESS_PEER = "172.30.32.2"  # Home Assistant's ingress gateway; nothing else may reach the UI
MAX_CAMERAS = 10


def load_selection(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{entity_id: {"enabled": bool, "rtsp_url": str}}"""
    try:
        data = json.loads((path or SELECTION_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in (data.get("entities") or {}).items() if isinstance(v, dict) and k.startswith("camera.")}


def save_selection(sel: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    from .config import write_private
    write_private(path or SELECTION_PATH, json.dumps({"entities": sel}, indent=2))


def update_selection(sel: dict[str, dict[str, Any]], update: dict[str, Any], known: set[str]) -> dict[str, dict[str, Any]]:
    """Apply a UI update. `rtsp_url` absent = keep, "" = clear. Unknown entities and bad URLs are rejected."""
    entities = update.get("entities")
    if not isinstance(entities, dict):
        raise ValueError("expected {entities: {...}}")
    out = {k: dict(v) for k, v in sel.items()}
    for eid, v in entities.items():
        if eid not in known:
            raise ValueError(f"unknown camera entity {eid}")
        if not isinstance(v, dict):
            raise ValueError("bad entry")
        cur = out.setdefault(eid, {"enabled": False, "rtsp_url": ""})
        if "enabled" in v:
            cur["enabled"] = bool(v["enabled"])
        if "rtsp_url" in v:
            url = str(v["rtsp_url"]).strip()
            if url and not url.lower().startswith(("rtsp://", "rtsps://")):
                raise ValueError("the stream URL must start with rtsp:// or rtsps://")
            cur["rtsp_url"] = url
            register_secret(url)
    if sum(1 for v in out.values() if v.get("enabled")) > MAX_CAMERAS:
        raise ValueError(f"Percia allows at most {MAX_CAMERAS} cameras")
    return out


def assign_names(cameras: list[ha.HACamera]) -> dict[str, str]:
    """Unique valid Percia camera name per entity (collisions get ' 2', ' 3', ...). Stable for a given entity list."""
    names, used = {}, set()
    for c in cameras:
        name = base = ha.percia_name(c.name, c.entity_id)
        n = 2
        while name.lower() in used:
            name = f"{base[:56]} {n}"
            n += 1
        used.add(name.lower())
        names[c.entity_id] = name
    return names


def desired_cameras(cameras: list[ha.HACamera], sel: dict[str, dict[str, Any]], video: Video) -> list[CameraCfg]:
    names = assign_names(cameras)
    return [CameraCfg(names[c.entity_id], Source("homeassistant", camera=c.entity_id, url=sel[c.entity_id].get("rtsp_url", "")), video)
            for c in cameras if sel.get(c.entity_id, {}).get("enabled")]


def ui_cameras(cameras: list[ha.HACamera], sel: dict[str, dict[str, Any]], running: dict[str, dict]) -> list[dict]:
    names = assign_names(cameras)
    rows = []
    for c in cameras:
        s = sel.get(c.entity_id, {})
        st = running.get(names[c.entity_id], {})
        rows.append({"entity_id": c.entity_id, "name": c.name, "ha_state": c.state, "available": c.available,
                     "enabled": bool(s.get("enabled")), "rtsp_url": strip_credentials(s.get("rtsp_url", "")),
                     "has_rtsp_url": bool(s.get("rtsp_url")), "percia_name": names[c.entity_id], "state": st.get("state", ""),
                     "mode": st.get("mode", ""), "error": st.get("last_error", "")})
    return rows


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Percia Bridge</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:var(--primary-background-color,#fff);color:var(--primary-text-color,#222)}
h1{font-size:1.3rem;margin:0 0 4px}p.s{margin:0 0 16px;opacity:.7}table{border-collapse:collapse;width:100%;max-width:900px}
td,th{padding:8px;border-bottom:1px solid #8884;text-align:left;vertical-align:top}.ok{color:#2e7d32}.bad{color:#c62828}.dim{opacity:.6}
input[type=text]{width:100%;box-sizing:border-box;padding:6px}button{padding:8px 16px;font-size:1rem;margin-top:12px}#msg{margin-left:12px}
</style></head><body><h1>Percia Bridge</h1><p class="s">Tick the cameras to send to Percia. Only video is sent, never audio.</p>
<table><thead><tr><th>Send</th><th>Camera</th><th>Status</th><th>Stream URL (optional)</th></tr></thead><tbody id="rows"></tbody></table>
<button id="save">Save</button><span id="msg"></span>
<script>
const rows=document.getElementById('rows'),msg=document.getElementById('msg');let data=[];
function cell(tr,node){const td=document.createElement('td');td.appendChild(node);tr.appendChild(td);return td}
function render(){rows.textContent='';data.forEach(c=>{const tr=document.createElement('tr');
const cb=document.createElement('input');cb.type='checkbox';cb.checked=c.enabled;cb.disabled=!c.available&&!c.enabled;cb.onchange=()=>{c.enabled=cb.checked};cell(tr,cb);
const nm=document.createElement('div');nm.textContent=c.name;const id=document.createElement('div');id.className='dim';id.textContent=c.entity_id;nm.appendChild(id);cell(tr,nm);
const st=document.createElement('span');st.textContent=c.enabled?(c.state||'starting'):(c.available?'not sent':c.ha_state);st.className=c.state==='streaming'?'ok':(c.state==='backoff'||c.state==='revoked'||c.state==='setup_failed')?'bad':'dim';
const w=document.createElement('div');w.appendChild(st);if(c.error){const e=document.createElement('div');e.className='dim';e.textContent=c.error;w.appendChild(e)}cell(tr,w);
const inp=document.createElement('input');inp.type='text';inp.placeholder=c.has_rtsp_url?c.rtsp_url+' (saved)':'rtsp://... (leave empty to use Home Assistant\\'s own stream)';inp.oninput=()=>{c.new_rtsp=inp.value;c.edited=true};cell(tr,inp);rows.appendChild(tr)})}
async function load(){const r=await fetch('api/cameras');data=(await r.json()).cameras;render()}
document.getElementById('save').onclick=async()=>{const entities={};data.forEach(c=>{const e={enabled:c.enabled};if(c.edited)e.rtsp_url=c.new_rtsp;entities[c.entity_id]=e});
const r=await fetch('api/selection',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({entities})});const j=await r.json();
msg.textContent=r.ok?'Saved.':(j.error||'Error');msg.className=r.ok?'ok':'bad';if(r.ok)load()};
load();setInterval(()=>{if(!data.some(c=>c.edited))load()},5000);
</script></body></html>"""


class Addon:
    def __init__(self, bridge: Bridge, token: str, video: Video, ha_base: str = ha.SUPERVISOR_CORE, enforce_peer: bool = True):
        self.bridge, self.token, self.video, self.ha_base = bridge, token, video, ha_base
        self.enforce_peer = enforce_peer
        self.cameras: list[ha.HACamera] = []
        self.sel = load_selection()
        self.error = ""

    async def refresh(self) -> None:
        try:
            self.cameras = await ha.list_cameras(self.token, self.ha_base)
            self.error = ""
        except ha.HAError as e:
            self.error = str(e)
        await self.bridge.apply(desired_cameras(self.cameras, self.sel, self.video))

    async def http(self, method: str, path: str, headers: dict, body: bytes, peer: tuple):
        if self.enforce_peer and (not peer or peer[0] != INGRESS_PEER):
            return 403, "text/plain", b"forbidden"
        if method == "GET" and path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", PAGE.encode()
        if method == "GET" and path == "/api/cameras":
            running = {c["name"]: c for c in self.bridge.cameras()}
            return json_response(200, {"cameras": ui_cameras(self.cameras, self.sel, running), "error": self.error})
        if method == "POST" and path == "/api/selection":
            if not headers.get("content-type", "").startswith("application/json"):
                return json_response(400, {"error": "expected JSON"})
            try:
                self.sel = update_selection(self.sel, json.loads(body or b"{}"), {c.entity_id for c in self.cameras})
            except (ValueError, json.JSONDecodeError) as e:
                return json_response(400, {"error": str(e)})
            save_selection(self.sel)
            await self.bridge.apply(desired_cameras(self.cameras, self.sel, self.video))
            return json_response(200, {"ok": True})
        return 404, "text/plain", b"not found"


def build_config(opts: dict[str, Any]) -> Config:
    cfg = Config(path=Path("/data/options.json"), state_dir=Path(os.environ.get("PERCIA_BRIDGE_STATE", "/data")), auto_add=True, health_listen="", variant="ha_addon")
    cfg.server = validate_server(opts.get("server") or "https://percia.ai")
    cfg.video = parse_video({k: opts[k] for k in ("max_height", "max_fps", "max_bitrate_kbps", "mode") if opts.get(k)})
    cfg.telemetry = bool(opts.get("telemetry", False))
    cfg.telemetry_key = str(opts.get("telemetry_key", ""))
    return cfg


async def amain() -> int:
    opts = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
    setup_logging(opts.get("log_level", "info"), True)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    key = str(opts.get("api_key", "")).strip()
    register_secret(key)
    register_secret(token)
    cfg = build_config(opts)
    state = State(cfg.state_dir)
    if not state.install_id:
        state.save()

    def api_key() -> str:
        if not key:
            raise ApiError(0, "no_api_key", "Set the Percia API key in the add-on configuration.")
        return key
    api = PerciaApi(cfg.server)

    ha_base = os.environ.get("PERCIA_HA_BASE", ha.SUPERVISOR_CORE)  # overridable for tests only
    ws_url = ha_base.replace("http", "ws", 1) + "/websocket"

    async def ha_resolve(src: Source):
        url, headers, _kind = await ha.resolve_stream(src.camera, token, src.url, ha_base, ws_url)
        return url, headers
    tele = Telemetry(cfg.telemetry, cfg.telemetry_key, state.install_id, "ha_addon", __version__)
    bridge = Bridge(cfg, api, state, make_resolver(cfg, ha_resolve), api_key, tele, Timing())
    addon = Addon(bridge, token, cfg.video, ha_base)
    server = await serve(f"0.0.0.0:{os.environ.get('PERCIA_ADDON_PORT', '8099')}", addon.http)
    event(logging.INFO, "addon_started", version=__version__, api_key_set=bool(key))
    tele.emit("bridge_started", {})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        await addon.refresh()
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            pass
    await bridge.shutdown()
    server.close()
    await api.aclose()
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
