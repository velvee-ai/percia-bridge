"""Tiny stdlib asyncio HTTP server for /healthz, /metrics and the Home Assistant add-on UI. Not a general server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from .logs import event

Handler = Callable[[str, str, dict[str, str], bytes, tuple], Awaitable[tuple[int, str, bytes]]]
MAX_BODY = 64 * 1024
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large", 503: "Service Unavailable"}


def json_response(status: int, obj) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(obj).encode()


def parse_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    return (host or "127.0.0.1"), int(port)


async def _handle(handler: Handler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            status, ctype, body = 413, "text/plain", b"too large"
        else:
            data = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
            status, ctype, body = await handler(method, path.split("?")[0], headers, data, writer.get_extra_info("peername") or ())
    except Exception:  # noqa: BLE001 - malformed client; answer 400 and move on
        status, ctype, body = 400, "text/plain", b"bad request"
    try:
        writer.write(f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                     f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def serve(listen: str, handler: Handler) -> asyncio.Server:
    host, port = parse_listen(listen)
    if host not in ("127.0.0.1", "localhost", "::1"):
        event(logging.WARNING, "health_listen_not_loopback", listen=listen,
              note="/healthz and /metrics expose camera names and states; keep this on a trusted network")
    return await asyncio.start_server(lambda r, w: _handle(handler, r, w), host, port)
