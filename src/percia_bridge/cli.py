"""percia-bridge command line."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from . import doctor as doctor_mod
from . import frigate as frigate_mod
from . import onvif as onvif_mod
from .api import ApiError, FeatureOff, Forbidden, PerciaApi, Unauthorized, WrongKind, describe_exception, read_api_key
from .bridge import Bridge, make_resolver
from .config import (
    NAME_RE,
    CameraCfg,
    Config,
    ConfigError,
    default_config_path,
    load,
    load_raw,
    parse,
    save_raw,
    write_private,
)
from .httpd import parse_listen, serve
from .identity import resolve_external_id
from .logs import event, register_secret, setup_logging, strip_credentials
from .state import State, StateError
from .supervisor import Timing
from .telemetry import Telemetry, bucket

CONFIG_TEMPLATE = """# Percia Bridge config. Docs: https://github.com/velvee-ai/percia-bridge
server: {server}
api_key_file: {key_file}      # 0600 file; the env var PERCIA_API_KEY overrides it
state_dir: {state_dir}        # camera tokens live here (cameras.json, 0600)
auto_add: true                # `run` registers cameras that are in this file but not yet registered
health:
  listen: 127.0.0.1:9477      # /healthz and /metrics, loopback only; "" disables
video:
  mode: auto                  # auto = copy H.264, transcode anything else to H.264 baseline
  max_fps: 15
  max_bitrate_kbps: 1500
cameras: []
"""


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]  # noqa: F821
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _config_path(args) -> Path:
    return Path(args.config).expanduser() if getattr(args, "config", None) else default_config_path()


def _load(args) -> Config:
    path = _config_path(args)
    try:
        return load(path)
    except ConfigError as e:
        die(str(e), 2)


def _ffmpeg_info(ffmpeg: str = "ffmpeg") -> tuple[str, bool]:
    exe = shutil.which(ffmpeg)
    if not exe:
        return "", False
    try:
        v = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        m = subprocess.run([exe, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=10).stdout
        return v, " whip " in m
    except (OSError, subprocess.SubprocessError, IndexError):
        return "", False


def _need_ffmpeg(cfg: Config) -> None:
    v, has_whip = _ffmpeg_info(cfg.ffmpeg_bin)
    if not v:
        die(f"ffmpeg not found ({cfg.ffmpeg_bin}). Install ffmpeg >= 7.1, or use the Docker image.", 3)
    if not has_whip:
        die(f"this ffmpeg has no WHIP muxer ({v}). Percia Bridge needs ffmpeg >= 7.1.", 3)


# ---------------- commands ----------------
def cmd_version(args) -> int:
    v, has_whip = _ffmpeg_info()
    print(f"percia-bridge {__version__}")
    print(f"ffmpeg: {v or 'not found'}; whip muxer: {'yes' if has_whip else 'NO'}")
    return 0 if has_whip else 1


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _slug(text: str, fallback: str = "camera") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:50]
    return s or fallback


def _key_problem(e: Exception) -> str:
    if isinstance(e, Unauthorized):
        return "Percia rejected this API key (401). Check you copied all of it, and that it was not revoked."
    if isinstance(e, Forbidden):
        return e.message
    if isinstance(e, FeatureOff):
        return e.message
    return describe_exception(e)


async def _validate_key(server: str, key: str) -> tuple[list[doctor_mod.Check], str]:
    """(checks, fatal message). Fatal = the key does not work at all; scope or switch problems come back as checks."""
    api = PerciaApi(server)
    try:
        await api.verify_key(key)  # 401 bad key, 403 no cameras:read, 404 cameras switched off
        try:
            return doctor_mod.key_checks(await api.whoami(key), await api.server_config()), ""
        except ApiError:
            return [doctor_mod.Check("API key scopes", doctor_mod.WARN, "could not read the key's scopes from this server")], ""
    except Exception as e:  # noqa: BLE001
        return [], _key_problem(e)
    finally:
        await api.aclose()


NEXT_STEPS = """
Next steps
  1. Find your cameras:   percia-bridge discover            (ONVIF on this network)
                          percia-bridge discover --frigate http://frigate:5000
  2. Add one:             percia-bridge add front-door --rtsp rtsp://USER:PASS@192.168.1.20/stream1
                          (several at once: percia-bridge add-all --frigate http://frigate:5000)
  3. Check the setup:     percia-bridge doctor
  4. Start streaming:     percia-bridge run               (Docker/systemd: see the README)
  5. See it from Percia:  percia-bridge status
The bridge only dials out: no port forwarding or inbound firewall rule is needed."""


def cmd_init(args) -> int:
    path = _config_path(args)
    if path.exists() and not args.force:
        die(f"{path} already exists (use --force to overwrite, or `percia-bridge check` to validate it)")
    interactive = _tty() and not (args.api_key_stdin or args.api_key_env) and not args.yes
    server = args.server
    if interactive and not args.server_given:
        print("Percia Bridge setup. It publishes cameras on your network to Percia; it only dials out.\n")
        server = input("Percia server [https://percia.ai]: ").strip() or server
    server = server.rstrip("/")
    from .config import default_state_dir, validate_server

    try:
        validate_server(server)
    except ConfigError as e:
        die(str(e), 2)
    attempts = 3 if interactive else 1
    key = ""
    for attempt in range(attempts):
        if args.api_key_stdin:
            key = sys.stdin.readline().strip()
        elif args.api_key_env:
            key = os.environ.get(args.api_key_env, "").strip()
            if not key:
                die(f"environment variable {args.api_key_env} is empty")
        else:
            key = os.environ.get("PERCIA_API_KEY", "").strip() if attempt == 0 else ""
            if not key:
                if attempt == 0:
                    print("Create an API key in the Percia dashboard (API keys) with the scopes:\n"
                          "  cameras:read, cameras:write, watches:delete   (watches:delete lets the bridge rotate and remove cameras)\n")
                key = getpass.getpass("Percia API key (input hidden): ").strip()
        if not key:
            die("no API key given")
        register_secret(key)
        if args.offline:
            break
        try:
            checks, fatal = asyncio.run(_validate_key(server, key))
        except Exception as e:  # noqa: BLE001
            die(f"cannot reach {server}: {describe_exception(e)} (use --offline to skip the check)")
        if fatal:
            if attempt < attempts - 1:
                print(f"  {fatal}\n  Try again.\n")
                continue
            die(fatal)
        for c in checks:
            print(f"  [{doctor_mod.SYMBOL[c.status]}] {c.name}: {c.detail}")
            if c.fix and c.status in (doctor_mod.WARN, doctor_mod.FAIL):
                print(f"           fix: {c.fix}")
        if any(c.status == doctor_mod.FAIL for c in checks):
            die("fix the above, then run `percia-bridge init` again.")
        break
    key_file = path.parent / "api_key"
    write_private(key_file, key + "\n")
    state_dir = default_state_dir(path)
    write_private(path, CONFIG_TEMPLATE.format(server=server, key_file=key_file, state_dir=state_dir))
    print(f"\nWrote {path} and {key_file} (both mode 0600; the key is not stored anywhere else).")
    print(NEXT_STEPS)
    return 0


def _onvif_add_line(d, user: str = "<user>", name: str = "") -> str:
    name = name or _slug(d.name or d.hardware or d.host)
    return f"percia-bridge add {name} --onvif {d.host} --port {d.port} --user {user} --password-env ONVIF_PASSWORD"


async def _discover_onvif(args) -> int:
    print("Searching for ONVIF cameras on this network (WS-Discovery, multicast, local network only)...")
    devices = await onvif_mod.discover(args.timeout, args.bind)
    if not devices:
        print("  none found.\n  - In Docker, discovery needs `network_mode: host` (multicast does not cross the bridge network).\n"
              "  - Many cameras ship with ONVIF off: enable it in the camera's settings (and create an ONVIF user).\n"
              "  - If you know the camera's address, skip discovery: percia-bridge add NAME --rtsp rtsp://USER:PASS@IP/stream1")
        return 0
    user = args.user or ""
    pw = ""
    if not args.no_probe:
        if not user and _tty():
            user = input("ONVIF username for listing profiles (blank to skip): ").strip()
        if user:
            pw = os.environ.get(args.password_env, "") if args.password_env else ""
            if not pw and _tty():
                pw = getpass.getpass("ONVIF password (hidden): ")
            register_secret(pw)
    print(f"\nFound {len(devices)} ONVIF device{'s' if len(devices) != 1 else ''}:")
    for d in devices:
        label = d.name or d.hardware or "camera"
        profs = f" [{', '.join(d.profiles)}]" if d.profiles else ""
        print(f"\n{d.host}:{d.port}  {label}{profs}")
        if user and pw and not args.no_probe:
            client = onvif_mod.OnvifClient(d.host, d.port, user, pw)
            try:
                for p in await client.list_profiles():
                    print(f"    profile {p.name or p.token}: {p.encoding or '?'} {p.width}x{p.height}  {strip_credentials(p.uri)}")
            except onvif_mod.OnvifError as e:
                print(f"    could not list profiles: {e}")
            finally:
                await client.aclose()
        print(f"    add with: export ONVIF_PASSWORD=...; {_onvif_add_line(d, user or '<user>')}")
    return 0


async def _discover_frigate(args) -> int:
    try:
        if args.frigate_config:
            cams = frigate_mod.parse_config_file(Path(args.frigate_config), args.rtsp_port)
            base = args.frigate or "http://frigate:5000"
        else:
            cams = await frigate_mod.fetch_cameras(args.frigate, args.rtsp_port)
            base = args.frigate
    except frigate_mod.FrigateError as e:
        print(f"Frigate: {e}\n  Is the URL right (http://HOST:5000)? From Docker, use the container name of Frigate, not localhost.")
        return 1
    print(f"Frigate cameras ({len(cams)}):")
    for c in cams:
        url = frigate_mod.restream_url(base, c.stream, args.rtsp_port) if c.stream else "(no restream)"
        print(f"  {c.name}{'' if c.enabled else ' [disabled]'}  {url}  {c.note}".rstrip())
        if c.stream:
            print(f"    add with: percia-bridge add {_slug(c.name)} --frigate {base} --camera {c.name}")
    usable = [c for c in cams if c.stream and c.enabled]
    if len(usable) > 1:
        print(f"\nAll {len(usable)} at once:  percia-bridge add-all --frigate {base}" + (f" --frigate-config {args.frigate_config}" if args.frigate_config else ""))
    return 0


async def _discover_ha(args) -> int:
    from . import homeassistant as ha

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print("Home Assistant discovery runs inside the add-on (SUPERVISOR_TOKEN is only set there). "
              "Install the Percia Bridge add-on and tick cameras in its panel, or see the README.")
        return 1
    try:
        cams = await ha.list_cameras(token)
        print(f"Home Assistant cameras ({len(cams)}):")
        for c in cams:
            print(f"  {c.entity_id}  {c.name}  ({c.state})")
            print(f"    add with: percia-bridge add {ha.percia_name(c.name, c.entity_id)} --homeassistant {c.entity_id}")
    except ha.HAError as e:
        print(f"Home Assistant: {e}")
        return 1
    return 0


def cmd_discover(args) -> int:
    async def go() -> int:
        rc = 0
        if args.frigate or args.frigate_config:
            rc |= await _discover_frigate(args)
        if args.home_assistant:
            rc |= await _discover_ha(args)
        if args.onvif or not (args.frigate or args.frigate_config or args.home_assistant):
            rc |= await _discover_onvif(args)
        return rc
    return asyncio.run(go())


def _load_key(cfg: Config) -> str:
    try:
        return read_api_key(file=cfg.api_key_file)
    except ApiError as e:
        die(e.message)


def _explain(e: Exception, name: str = "") -> str:
    """One human line for a failed API call during add / register / rotate."""
    if isinstance(e, Unauthorized):
        return "Percia rejected the API key (401). Put a valid key in the key file or PERCIA_API_KEY (`percia-bridge init --force`)."
    if isinstance(e, Forbidden):
        return e.message
    if isinstance(e, ApiError):
        extra = "  (free one with `percia-bridge remove NAME`)" if e.code == "camera_limit" else ""
        return (e.message if isinstance(e, FeatureOff) else f"{e.code}: {e.message}" if e.status else e.message) + extra
    return describe_exception(e)


def _reg_note(reg) -> str:
    if reg.existing:
        note = "Percia already had this camera: reusing it" + (" (new token issued: it had none stored here)" if reg.rotated else "")
    elif reg.flow == "claim":
        note = "created (this Percia server uses the older pairing flow)"
    else:
        note = "created"
    return f"{note} (camera id {reg.state.camera_id})"


async def _register_many(cfg: Config, state: State, cams: list[CameraCfg], key: str, force: bool = False) -> list[tuple[str, str, str]]:
    """Register each camera with Percia. With two or more to do and a server that has POST /api/cameras/bulk, they go in chunks of
    10 (one call each); otherwise one at a time on one connection (a household NAT trips per-IP limits otherwise). Idempotent by
    externalId either way, so a camera Percia already has is returned, not duplicated. One failure never stops the rest.
    Returns [(name, "ok"|"error", message)] in the order of `cams`."""
    api = PerciaApi(cfg.server)
    results: dict[str, tuple[str, str, str]] = {}
    try:
        todo: list[tuple[CameraCfg, str]] = []
        for cam in cams:
            known = state.get(cam.name)
            if known and not force:
                results[cam.name] = (cam.name, "ok", f"already registered (camera id {known.camera_id})")
            else:
                todo.append((cam, await resolve_external_id(cam)))
        # bulk needs unique externalIds; a camera sharing one with an earlier camera is the same Percia camera, so it goes alone
        seen: set[str] = set()
        bulk_items = [(c, e) for c, e in todo if not (e in seen or seen.add(e))]
        single = [(c, e) for c, e in todo if (c, e) not in bulk_items]
        if len(bulk_items) >= 2:
            outcomes = await api.register_bulk([(c.name, e, state.get(c.name)) for c, e in bulk_items], key)
            if outcomes is not None:
                for (cam, _), o in zip(bulk_items, outcomes):
                    if isinstance(o, Exception):
                        results[cam.name] = (cam.name, "error", _explain(o, cam.name))
                    else:
                        state.put(cam.name, o.state)
                        results[cam.name] = (cam.name, "ok", _reg_note(o))
                bulk_items = []
        rest = [*bulk_items, *single]
        for n, (cam, ext) in enumerate(rest):  # one at a time: a single camera, an older server, or duplicates
            try:
                reg = await api.register(cam.name, key, ext, state.get(cam.name))
                state.put(cam.name, reg.state)
                results[cam.name] = (cam.name, "ok", _reg_note(reg))
            except Exception as e:  # noqa: BLE001
                results[cam.name] = (cam.name, "error", _explain(e, cam.name))
                if isinstance(e, (Unauthorized, FeatureOff, Forbidden)):  # same answer for every camera: stop early
                    for c, _ in rest[n + 1:]:
                        results[c.name] = (c.name, "error", "skipped")
                    break
    finally:
        await api.aclose()
    return [results.get(c.name, (c.name, "error", "skipped")) for c in cams]


def _print_results(results: list[tuple[str, str, str]]) -> int:
    bad = 0
    for name, status, msg in results:
        print(f"  {'ok   ' if status == 'ok' else 'FAIL '} {name}: {msg}", file=sys.stdout if status == "ok" else sys.stderr)
        bad += status != "ok"
    return 1 if bad else 0


def _source_from_args(args) -> dict:
    if args.rtsp:
        return {"type": "rtsp", "url": args.rtsp}
    if args.onvif:
        user = args.user or input("ONVIF username: ").strip()
        env = args.password_env
        pw = os.environ.get(env, "") if env else getpass.getpass("ONVIF password (hidden): ")
        if env and not pw:
            die(f"environment variable {env} is empty (export it first)")
        register_secret(pw)
        src = {"type": "onvif", "host": args.onvif, "port": args.port, "username": user, "password": f"${{{env}}}" if env else pw}
        if args.profile:
            src["profile"] = args.profile
        return src
    if args.frigate:
        if not args.camera:
            die("--frigate needs --camera NAME (the Frigate camera). List them: percia-bridge discover --frigate URL", 2)
        return {"type": "frigate", "url": args.frigate, "camera": args.camera, "rtsp_port": args.rtsp_port}
    if args.homeassistant:
        return {"type": "homeassistant", "camera": args.homeassistant}
    return {}


def _check_onvif(cam: CameraCfg) -> None:
    async def verify():
        c = onvif_mod.OnvifClient(cam.source.host, cam.source.port, cam.source.username, cam.source.password)
        try:
            return await c.list_profiles()
        finally:
            await c.aclose()
    try:
        onvif_mod.choose_profile(asyncio.run(verify()), cam.source.profile)
    except onvif_mod.OnvifError as e:
        die(f"ONVIF: {e}")


def cmd_add(args) -> int:
    if not NAME_RE.match(args.name):
        die("camera name must be 1-60 characters: letters, digits, space, _ . -", 2)
    path = _config_path(args)
    raw = load_raw(path) if path.exists() else die(f"{path} not found; run `percia-bridge init` first", 2)
    cams = raw.setdefault("cameras", []) or []
    raw["cameras"] = cams
    exists = next((c for c in cams if c.get("name", "").lower() == args.name.lower()), None)
    chosen = sum(bool(x) for x in (args.rtsp, args.onvif, args.frigate, args.homeassistant))
    if exists is None and chosen != 1:
        die("give exactly one source: --rtsp URL, --onvif HOST, --frigate URL --camera NAME or --homeassistant ENTITY (see `percia-bridge discover`)", 2)
    if exists is not None and chosen:
        die(f"{args.name} is already in the config; edit {path} or `percia-bridge remove {args.name}` first", 2)
    if exists is None and len(cams) >= 10:
        die("Percia allows at most 10 cameras per account")
    source = _source_from_args(args)
    try:
        new_cams = cams if exists is not None else [*cams, {"name": args.name, "source": source}]
        cfg = parse({**raw, "cameras": new_cams}, path, dict(os.environ))
    except ConfigError as e:
        die(str(e), 2)
    cam = cfg.camera(args.name)
    if cam is None:
        die(f"{args.name} not found in config")
    if cam.source.type == "onvif":  # fail early on wrong credentials
        _check_onvif(cam)
    try:
        state = State(cfg.state_dir)
    except StateError as e:
        die(str(e))
    if state.get(args.name) and not args.force:
        print(f"{args.name}: already registered (camera id {state.get(args.name).camera_id}). "
              f"Use `percia-bridge rotate {args.name}` for a new token, or --force to register from scratch.")
        return 0
    key = _load_key(cfg)
    if args.force and state.get(args.name):
        old = state.get(args.name)
        if old.kind == "phone":  # the old pairing flow has no idempotency: drop the stale camera first
            try:
                asyncio.run(_delete_remote(cfg, old.camera_id, key))
            except Exception:  # noqa: BLE001
                pass
        state.drop(args.name)
    results = asyncio.run(_register_many(cfg, state, [cam], key))
    rc = _print_results(results)
    if rc:
        return rc
    if exists is None:
        entry = {"name": args.name, "source": source}
        entry["external_id"] = asyncio.run(resolve_external_id(cam))
        raw["cameras"].append(entry)
        save_raw(path, raw)
    print(f"Start streaming with `percia-bridge run`{' (a running bridge needs a restart to pick up a new camera)' if exists is None else ''}.")
    return 0


async def _delete_remote(cfg: Config, camera_id: str, key: str) -> None:
    api = PerciaApi(cfg.server)
    try:
        await api.delete_camera(camera_id, key)
    finally:
        await api.aclose()


def cmd_add_all(args) -> int:
    """Bulk: add every camera Frigate knows about (or a chosen subset) and register them all."""
    path = _config_path(args)
    if not path.exists():
        die(f"{path} not found; run `percia-bridge init` first", 2)
    if not (args.frigate or args.frigate_config):
        die("add-all needs --frigate URL (or --frigate-config FILE). For ONVIF, run `percia-bridge discover` and add each camera.", 2)
    base = args.frigate or "http://frigate:5000"
    try:
        found = (frigate_mod.parse_config_file(Path(args.frigate_config), args.rtsp_port) if args.frigate_config
                 else asyncio.run(frigate_mod.fetch_cameras(args.frigate, args.rtsp_port)))
    except frigate_mod.FrigateError as e:
        die(f"Frigate: {e}")
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    picked = [c for c in found if c.enabled and c.stream and (only is None or c.name in only)]
    if only and (missing := only - {c.name for c in found}):
        die(f"Frigate has no camera named: {', '.join(sorted(missing))}", 2)
    if not picked:
        die("no usable Frigate cameras found (disabled, or no restream)")
    raw = load_raw(path)
    cams = raw.get("cameras") or []
    have = {c.get("name", "").lower() for c in cams}
    new = [c for c in picked if _slug(c.name).lower() not in have]
    if len(cams) + len(new) > 10:
        die(f"that would be {len(cams) + len(new)} cameras; Percia allows 10 per account. Choose some with --only a,b,c", 2)
    entries = [{"name": _slug(c.name), "source": {"type": "frigate", "url": base, "camera": c.name, "rtsp_port": args.rtsp_port}} for c in new]
    try:
        cfg = parse({**raw, "cameras": [*cams, *entries]}, path, dict(os.environ))
        state = State(cfg.state_dir)
    except (ConfigError, StateError) as e:
        die(str(e), 2)
    key = _load_key(cfg)
    results = asyncio.run(_register_many(cfg, state, [cfg.camera(e["name"]) for e in entries], key))
    ok = {n for n, st, _ in results if st == "ok"}
    for e in entries:
        if e["name"] in ok:
            e["external_id"] = asyncio.run(resolve_external_id(cfg.camera(e["name"])))
            raw["cameras"] = [*cams, *[x for x in entries if x["name"] in ok]]
    if ok:
        save_raw(path, raw)
    skipped = [c.name for c in picked if c not in new]
    for n in skipped:
        print(f"  skip  {n}: already in the config")
    rc = _print_results(results)
    print(f"{len(ok)} added. Start streaming with `percia-bridge run`.")
    return rc


def cmd_register(args) -> int:
    """Register every configured camera that has no token yet (what `run` does by itself with auto_add)."""
    cfg = _load(args)
    try:
        state = State(cfg.state_dir)
    except StateError as e:
        die(str(e))
    todo = [c for c in cfg.cameras if args.force or not state.get(c.name)]
    if not cfg.cameras:
        die("no cameras in the config yet: `percia-bridge add NAME --rtsp URL`", 2)
    if not todo:
        print(f"All {len(cfg.cameras)} configured camera(s) are already registered.")
        return 0
    rc = _print_results(asyncio.run(_register_many(cfg, state, todo, _load_key(cfg), args.force)))
    return rc


def cmd_rotate(args) -> int:
    cfg = _load(args)
    try:
        state = State(cfg.state_dir)
    except StateError as e:
        die(str(e))
    cs = state.get(args.name)
    if cs is None:
        die(f"{args.name} is not registered here (`percia-bridge status`, or `percia-bridge add {args.name} ...`)")
    key = _load_key(cfg)

    async def go():
        api = PerciaApi(cfg.server)
        try:
            if args.revoke:
                await api.revoke_token(cs.camera_id, key)
                return None
            return await api.rotate_token(cs.camera_id, key)
        finally:
            await api.aclose()
    try:
        out = asyncio.run(go())
    except WrongKind:
        die(f"{args.name} was made with the older pairing flow and has no rotatable token. Re-register it: percia-bridge add {args.name} --force")
    except ApiError as e:
        if e.status == 404:
            die(f"Percia no longer has camera {cs.camera_id} (deleted?). Register again: percia-bridge add {args.name} --force")
        die(_explain(e))
    except Exception as e:  # noqa: BLE001
        die(_explain(e))
    if args.revoke:
        print(f"Revoked the token of '{args.name}': publishing is refused until you run `percia-bridge rotate {args.name}`. The camera stays in Percia.")
        return 0
    token, whip = out
    state.put(args.name, type(cs)(cs.camera_id, token, whip, cs.external_id, "push"))
    print(f"New token stored for '{args.name}'; the old one stopped working. A running bridge picks the new token up within seconds"
          " (if it runs in another container, make sure it shares the state directory).")
    return 0


def cmd_remove(args) -> int:
    path = _config_path(args)
    cfg = _load(args)
    raw = load_raw(path)
    state = State(cfg.state_dir)
    cs = state.get(args.name)
    if cs and not args.keep_remote:
        try:
            key = read_api_key(file=cfg.api_key_file)

            async def go():
                api = PerciaApi(cfg.server)
                try:
                    await api.delete_camera(cs.camera_id, key)
                finally:
                    await api.aclose()
            asyncio.run(go())
        except ApiError as e:
            if e.code == "camera_in_use":
                die(f"{args.name} is used by an active or paused watch; stop the watch first (or use --keep-remote)")
            die(f"{e.code}: {e.message}")
    state.drop(args.name)
    raw["cameras"] = [c for c in (raw.get("cameras") or []) if c.get("name", "").lower() != args.name.lower()]
    save_raw(path, raw)
    print(f"Removed '{args.name}'.")
    return 0


def _fetch_health(listen: str) -> dict | None:
    host, port = parse_listen(listen)
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=3) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)
    except Exception:  # noqa: BLE001
        return None


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")) if isinstance(iso, str) else datetime.fromtimestamp(float(iso) / 1000, timezone.utc)
    except (ValueError, TypeError):
        return str(iso)
    secs = max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
    return f"{secs}s ago" if secs < 90 else f"{secs // 60}m ago" if secs < 5400 else f"{secs // 3600}h ago" if secs < 172800 else f"{secs // 86400}d ago"


def video_summary(st: dict | None) -> str:
    """'h264 1280x720 15fps' from whatever the status route returned; '-' when the server reports nothing."""
    if not st:
        return "-"
    v = st.get("video") if isinstance(st.get("video"), dict) else st
    codec = v.get("codec") or ""
    w, h = v.get("width") or 0, v.get("height") or 0
    fps = v.get("fps") or v.get("framerate") or 0
    bps = v.get("bitrate") or 0  # bits per second, only if the server ever reports it (it does not yet)
    parts = [codec, f"{w}x{h}" if w and h else "", f"{round(float(fps))}fps" if fps else "", f"{float(bps) / 1e6:.1f}Mbit/s" if bps else ""]
    return " ".join(p for p in parts if p) or "-"


async def _remote_status(cfg: Config, state: State, key: str) -> dict[str, dict]:
    """name -> {online, last, video, note} from Percia: GET /api/cameras for the list, plus GET /api/cameras/:id/status per camera
    (state, hint, and codec/resolution/fps/bitrate when the server reports them). Older servers without the status route: list only."""
    api = PerciaApi(cfg.server)
    out: dict[str, dict] = {}
    try:
        listed = {c.get("id"): c for c in await api.list_cameras(key)}
        for cam in cfg.cameras:
            cs = state.get(cam.name)
            if not cs:
                continue
            row = listed.get(cs.camera_id)
            if row is None:
                out[cam.name] = {"note": "not found on Percia (removed?)", "online": None, "last": None, "video": "-"}
                continue
            try:
                st = await api.camera_status(cs.camera_id, key)
            except Exception:  # noqa: BLE001
                st = None
            st = st or {}
            last = st.get("lastSeenAt") or st.get("lastPublishAt") or row.get("lastSeenAt")
            if isinstance(st.get("online"), bool):
                online = st["online"]
            elif st.get("state") in ("online", "offline", "waiting", "revoked"):
                online = st["state"] == "online"
            else:
                online = st.get("status", row.get("status")) == "online"
            state_name = st.get("state") or ""
            note = (st.get("hint") or "") if state_name and state_name != "online" else ""
            out[cam.name] = {"online": online, "last": last, "video": video_summary(st), "note": note, "state": state_name}
    finally:
        await api.aclose()
    return out


def cmd_status(args) -> int:
    cfg = _load(args)
    h = _fetch_health(args.listen or cfg.health_listen) if (args.listen or cfg.health_listen) else None
    if args.quiet:
        return 0 if h is not None and h["status"] == "ok" else 1
    remote: dict[str, dict] = {}
    remote_note = ""
    if not args.no_remote and cfg.cameras:
        try:
            state = State(cfg.state_dir)
            remote = asyncio.run(_remote_status(cfg, state, read_api_key(file=cfg.api_key_file)))
        except ApiError as e:
            remote_note = f"Percia side not shown: {e.message}"
        except Exception as e:  # noqa: BLE001
            remote_note = f"Percia side not shown: {describe_exception(e)}"
    local = {c["name"]: c for c in (h or {}).get("cameras", [])}
    if h:
        print(f"percia-bridge {h['version']} running for {h['uptime_s']}s: {h['status']}")
    else:
        print("No running bridge answered on the health port (start it with `percia-bridge run`).")
    if cfg.cameras:
        st = State(cfg.state_dir) if cfg.state_dir.exists() else None
        rows = [("CAMERA", "BRIDGE", "ONLINE", "LAST PUBLISH", "VIDEO")]
        notes = []
        for c in cfg.cameras:
            lc, rc_ = local.get(c.name), remote.get(c.name)
            bridge = (lc["state"] + (f" ({lc['mode']})" if lc["state"] == "streaming" and lc["mode"] else "")) if lc else ("registered" if st and st.get(c.name) else "not added")
            online = "-" if not rc_ or rc_["online"] is None else "yes" if rc_["online"] else (rc_.get("state") or "no")
            rows.append((c.name, bridge, online, _ago(rc_["last"]) if rc_ else "-", rc_["video"] if rc_ else "-"))
            for n in (lc and lc["last_error"], rc_ and rc_["note"]):
                if n:
                    notes.append(f"  {c.name}: {n}")
        widths = [max(len(r[i]) for r in rows) for i in range(5)]
        for r in rows:
            print("  " + "  ".join(v.ljust(widths[i]) for i, v in enumerate(r)).rstrip())
        for n in notes:
            print(n)
    if remote_note:
        print(f"\n{remote_note}")
    return 0 if h is not None and h["status"] == "ok" else 1


def cmd_doctor(args) -> int:
    path = _config_path(args)
    cfg, err = None, ""
    try:
        cfg = load(path)
    except ConfigError as e:
        err = str(e)
    resolver = make_resolver(cfg) if cfg else None
    print(f"percia-bridge doctor ({path})\n")
    checks = asyncio.run(doctor_mod.run_doctor(cfg, err, offline=args.offline, resolver=resolver))
    print(doctor_mod.render(checks))
    return doctor_mod.exit_code(checks)


def cmd_check(args) -> int:
    """Validate the config file without starting anything."""
    path = _config_path(args)
    cfg = _load(args)
    problems = []
    if cfg.api_key_file and not cfg.api_key_file.exists() and not os.environ.get("PERCIA_API_KEY"):
        problems.append(f"api_key_file {cfg.api_key_file} does not exist (run `percia-bridge init`, or set PERCIA_API_KEY)")
    try:
        st = State(cfg.state_dir)
        missing = [c.name for c in cfg.cameras if not st.get(c.name)]
        if missing and not cfg.auto_add:
            problems.append(f"not registered yet and auto_add is off: {', '.join(missing)} (run `percia-bridge register`)")
    except StateError as e:
        problems.append(str(e))
    print(f"{path}: valid. server {cfg.server}, {len(cfg.cameras)} camera(s): " + (", ".join(f"{c.name} ({c.source.describe()})" for c in cfg.cameras) or "none"))
    for p in problems:
        print(f"  warning: {p}")
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    setup_logging(args.log_level, None if args.log_format == "auto" else args.log_format == "json")
    cfg.variant = os.environ.get("PERCIA_BRIDGE_VARIANT", cfg.variant)
    _need_ffmpeg(cfg)
    try:
        state = State(cfg.state_dir)
        key_fn = lambda: read_api_key(file=cfg.api_key_file)  # noqa: E731
        if cfg.auto_add and any(not state.get(c.name) for c in cfg.cameras):
            key_fn()
    except (StateError, ApiError) as e:
        die(str(e))

    async def main() -> int:
        api = PerciaApi(cfg.server)
        state.save() if not state.install_id else None
        tele = Telemetry(cfg.telemetry, cfg.telemetry_key, state.install_id, cfg.variant, __version__)
        timing = Timing(heartbeat_interval=float(os.environ.get("PERCIA_BRIDGE_HEARTBEAT_S", "20")),  # env knobs are for tests
                        revoked_poll=float(os.environ.get("PERCIA_BRIDGE_REVOKED_POLL_S", "5")))
        bridge = Bridge(cfg, api, state, make_resolver(cfg), key_fn, tele, timing)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        server = None
        if cfg.health_listen:
            server = await serve(cfg.health_listen, bridge.http)
        v, _ = _ffmpeg_info(cfg.ffmpeg_bin)
        event(logging.INFO, "bridge_started", version=__version__, ffmpeg=v, cameras=len(cfg.cameras), health=cfg.health_listen or "off")
        tele.emit("bridge_started", {"camera_bucket": bucket(len(cfg.cameras))})
        if not cfg.cameras:
            event(logging.WARNING, "no_cameras", hint="add one with `percia-bridge add`")
        await bridge.apply(cfg.cameras)
        await stop.wait()
        event(logging.INFO, "shutting_down")
        await bridge.shutdown()
        if server:
            server.close()
        await api.aclose()
        return 0

    return asyncio.run(main())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="percia-bridge", description="Send cameras on your own network to Percia.")
    p.add_argument("--config", help="config file (default: $PERCIA_BRIDGE_CONFIG, /config/config.yaml, ~/.config/percia-bridge/config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write the config and store the API key")
    s.add_argument("--server", default=None, help="Percia server (default https://percia.ai; asked for when interactive)")
    s.add_argument("--api-key-env", help="read the key from this environment variable")
    s.add_argument("--api-key-stdin", action="store_true", help="read the key from stdin")
    s.add_argument("--offline", action="store_true", help="do not check the key against the server")
    s.add_argument("--force", action="store_true")
    s.add_argument("--yes", action="store_true", help="never prompt (use with --api-key-env / PERCIA_API_KEY)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("discover", help="find ONVIF cameras, or list Frigate / Home Assistant cameras")
    s.add_argument("--onvif", action="store_true", help="ONVIF WS-Discovery (the default)")
    s.add_argument("--frigate", metavar="URL", help="Frigate base URL, e.g. http://frigate:5000")
    s.add_argument("--frigate-config", metavar="FILE", help="read a Frigate config.yml instead of the API")
    s.add_argument("--rtsp-port", type=int, default=8554)
    s.add_argument("--home-assistant", action="store_true")
    s.add_argument("--user", help="ONVIF username for listing profiles")
    s.add_argument("--password-env", help="environment variable holding the ONVIF password")
    s.add_argument("--no-probe", action="store_true", help="only list addresses, do not log in")
    s.add_argument("--timeout", type=float, default=3.0)
    s.add_argument("--bind", metavar="IP", help="send the discovery probe from this local IP")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("add", help="add a camera: register it with Percia (idempotent) and store its publish token")
    s.add_argument("name")
    s.add_argument("--rtsp", metavar="URL")
    s.add_argument("--onvif", metavar="HOST")
    s.add_argument("--port", type=int, default=80)
    s.add_argument("--user")
    s.add_argument("--password-env")
    s.add_argument("--profile")
    s.add_argument("--frigate", metavar="URL")
    s.add_argument("--camera", help="Frigate camera name")
    s.add_argument("--homeassistant", metavar="ENTITY", help="Home Assistant camera entity id (inside the add-on)")
    s.add_argument("--rtsp-port", type=int, default=8554)
    s.add_argument("--force", action="store_true", help="register from scratch (after the camera was removed on Percia)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("add-all", help="add many cameras at once (all Frigate cameras, or --only some) and register them")
    s.add_argument("--frigate", metavar="URL")
    s.add_argument("--frigate-config", metavar="FILE")
    s.add_argument("--only", metavar="a,b,c", help="only these Frigate camera names")
    s.add_argument("--rtsp-port", type=int, default=8554)
    s.set_defaults(fn=cmd_add_all)

    s = sub.add_parser("register", help="register every configured camera that is not registered yet (idempotent)")
    s.add_argument("--force", action="store_true", help="ask Percia again for all of them (reuses cameras it already has)")
    s.set_defaults(fn=cmd_register)

    s = sub.add_parser("rotate", help="get a new publish token for a camera (after a 401, or if one leaked)")
    s.add_argument("name")
    s.add_argument("--revoke", action="store_true", help="only revoke: stop accepting this camera's token until you rotate")
    s.set_defaults(fn=cmd_rotate)

    s = sub.add_parser("doctor", help="check ffmpeg, network, clock, API key and every camera; explains how to fix problems")
    s.add_argument("--offline", action="store_true", help="skip the checks that need the internet")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("check", help="validate the config file")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("run", help="stream all cameras")
    s.add_argument("--log-level", default=os.environ.get("PERCIA_BRIDGE_LOG_LEVEL", "info"))
    s.add_argument("--log-format", choices=["auto", "json", "text"], default="auto")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="show camera states of a running bridge")
    s.add_argument("--listen")
    s.add_argument("--quiet", action="store_true", help="no output, exit code only (Docker HEALTHCHECK)")
    s.add_argument("--no-remote", action="store_true", help="do not ask Percia (online / last publish / video)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("remove", help="delete a camera on Percia and forget it here")
    s.add_argument("name")
    s.add_argument("--keep-remote", action="store_true")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("version", help="versions and ffmpeg capability")
    s.set_defaults(fn=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        args.server_given = args.server is not None
        args.server = args.server or "https://percia.ai"
    if args.cmd != "run":
        setup_logging("warning")
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
