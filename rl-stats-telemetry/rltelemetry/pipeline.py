"""Wires a message source to the recorder, channel mapper and sinks."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

from .channels import ChannelMapper, Sink
from .matches import MatchInfo, MatchTracker
from .protocol import Message
from .recorder import MatchRecorder, RawLog
from .report import Coverage
from .sinks import ChannelJsonlSink
from .transport import StatsClient

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, out_dir: Path, driver: str | None = None, channels: bool = True,
                 extra_sinks: Iterable[Sink] = (), tag: str = "", raw: bool = True) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        self.recorder = MatchRecorder(out_dir)
        sinks: list[Sink] = [ChannelJsonlSink(out_dir)] if channels else []
        sinks += list(extra_sinks)
        listeners: list = [self.recorder]
        if sinks:
            listeners.append(ChannelMapper(sinks, driver))
        self.tracker = MatchTracker(listeners, tag=tag)
        self.raw = RawLog(out_dir) if raw else None
        self.coverage = Coverage()

    def handle(self, rx_ns: int, msg: Message) -> MatchInfo | None:
        self.coverage.add(rx_ns, msg)
        match = self.tracker.handle(rx_ns, msg)
        if self.raw:
            self.raw.write(rx_ns, msg, match)
        return match

    def close(self) -> None:
        self.tracker.close()
        if self.raw:
            self.raw.close()


async def listen(client: StatsClient, pipeline: Pipeline, status_every: float = 10.0) -> None:
    """Record until cancelled; logs a status line periodically."""
    last_status = time.monotonic()
    count = 0
    log.info("waiting for Rocket League on %s (start a match or replay)", client.endpoint)
    async for rx_ns, msg in client.events():
        count += 1
        match = pipeline.handle(rx_ns, msg)
        if msg.event in ("MatchCreated", "MatchEnded", "MatchDestroyed", "GoalScored", "ReplayCreated"):
            log.info("%s %s", msg.event, match.key if match else "")
        now = time.monotonic()
        if now - last_status >= status_every:
            cur = pipeline.tracker.current
            log.info("%d msgs in last %.0fs; match: %s", count, now - last_status,
                     cur.key if cur else "none")
            last_status, count = now, 0
