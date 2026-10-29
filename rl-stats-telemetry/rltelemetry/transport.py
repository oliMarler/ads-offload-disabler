"""TCP and WebSocket clients for the Stats API, with automatic reconnect.

The game only opens its sockets while running, so the client simply retries
until it can connect and again after every disconnect (menus, restarts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

from .protocol import JsonStreamDecoder, Message, decode_message, encode_command

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 49123
DEFAULT_WS_PORT = 49124


class NotConnected(RuntimeError):
    pass


class StatsClient:
    """Yields ``(rx_time_ns, Message)`` for every event the game sends.

    ``raw_tap`` receives every raw chunk exactly as read (used by ``probe``).
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        websocket: bool = False,
        reconnect: bool = True,
        raw_tap: Callable[[bytes], None] | None = None,
    ) -> None:
        self.host = host
        self.websocket = websocket
        self.port = port or (DEFAULT_WS_PORT if websocket else DEFAULT_TCP_PORT)
        self.reconnect = reconnect
        self.raw_tap = raw_tap
        self.decoder = JsonStreamDecoder()
        self.connected = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._ws: Any = None
        self._closing = False

    @property
    def endpoint(self) -> str:
        scheme = "ws" if self.websocket else "tcp"
        return f"{scheme}://{self.host}:{self.port}"

    async def events(self) -> AsyncIterator[tuple[int, Message]]:
        delay = 1.0
        while not self._closing:
            try:
                async for item in (self._ws_events() if self.websocket else self._tcp_events()):
                    delay = 1.0
                    yield item
                log.warning("disconnected from %s", self.endpoint)
            except (OSError, asyncio.IncompleteReadError) as exc:
                log.debug("connect to %s failed: %s", self.endpoint, exc)
            finally:
                self.connected.clear()
                self._writer = None
                self._ws = None
            if not self.reconnect or self._closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

    async def _tcp_events(self) -> AsyncIterator[tuple[int, Message]]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        self.decoder = JsonStreamDecoder()
        self.connected.set()
        log.info("connected to %s", self.endpoint)
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                rx = time.time_ns()
                if self.raw_tap:
                    self.raw_tap(chunk)
                for obj in self.decoder.feed(chunk):
                    msg = decode_message(obj)
                    if msg:
                        yield rx, msg
        finally:
            writer.close()

    async def _ws_events(self) -> AsyncIterator[tuple[int, Message]]:
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed, InvalidHandshake
        except ImportError as exc:  # pragma: no cover - depends on install
            raise SystemExit("WebSocket mode needs: pip install websockets") from exc
        try:
            async with connect(f"ws://{self.host}:{self.port}", max_size=None,
                               ping_interval=None) as ws:
                self._ws = ws
                self.decoder = JsonStreamDecoder()
                self.connected.set()
                log.info("connected to %s", self.endpoint)
                async for frame in ws:
                    rx = time.time_ns()
                    chunk = frame.encode() if isinstance(frame, str) else frame
                    if self.raw_tap:
                        self.raw_tap(chunk)
                    # A frame should hold one message, but decode it as a stream anyway.
                    for obj in self.decoder.feed(chunk):
                        msg = decode_message(obj)
                        if msg:
                            yield rx, msg
        except ConnectionClosed:
            return
        except InvalidHandshake as exc:
            raise OSError(f"websocket handshake failed: {exc}") from exc

    async def send(self, command: str, data: dict[str, Any] | None = None) -> None:
        payload = encode_command(command, data)
        if self._ws is not None:
            await self._ws.send(payload.decode())
        elif self._writer is not None:
            self._writer.write(payload)
            await self._writer.drain()
        else:
            raise NotConnected(f"not connected to {self.endpoint}")
        log.info("sent %s %s", command, json.dumps(data or {}))

    async def close(self) -> None:
        self._closing = True
        if self._writer:
            self._writer.close()
        if self._ws is not None:
            await self._ws.close()
