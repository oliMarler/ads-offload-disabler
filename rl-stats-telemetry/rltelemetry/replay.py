"""Replay mode: load a saved replay and record it as a spectator, which is
when the car-level channels (Speed, Boost, OnGround...) are sent.

Rocket League must already be running and sitting in the main menu.
Saved replays live in ``Documents\\My Games\\Rocket League\\TAGame\\Demos``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from .pipeline import Pipeline
from .protocol import UPDATE_STATE
from .transport import StatsClient

log = logging.getLogger(__name__)


def demos_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Documents" / "My Games" / "Rocket League" / "TAGame" / "Demos"


def list_replays(folder: Path | None = None) -> list[Path]:
    folder = folder or demos_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.replay"), key=lambda p: p.stat().st_mtime, reverse=True)


def load_replay_data(replay: str) -> dict[str, str]:
    """``Path`` for a path, ``FileName`` for a bare replay name (both documented)."""
    p = Path(replay)
    if p.is_file():
        return {"Path": str(p.resolve())}
    if os.sep in replay or "/" in replay:
        return {"Path": replay}
    return {"FileName": replay}


async def run_replay(client: StatsClient, pipeline: Pipeline, replay: str, *,
                     speed: float = 1.0, pov: tuple[str, str] | None = None,
                     hide_hud: bool = False, seek_seconds: float | None = None,
                     start_timeout: float = 60.0, idle_timeout: float = 5.0) -> bool:
    """Returns True if replay ticks were recorded until the replay stopped."""
    state = {"replay_ticks": 0, "frame": None, "last_advance": time.monotonic(), "destroyed": False}

    async def pump() -> None:
        async for rx_ns, msg in client.events():
            pipeline.handle(rx_ns, msg)
            if msg.event == UPDATE_STATE:
                game = msg.data.get("Game") or {}
                if game.get("bReplay") or "Frame" in game:
                    state["replay_ticks"] += 1
                    frame = game.get("Frame", game.get("Elapsed"))
                    if frame != state["frame"]:
                        state["frame"] = frame
                        state["last_advance"] = time.monotonic()
            elif msg.event == "MatchDestroyed" and state["replay_ticks"]:
                state["destroyed"] = True

    pump_task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(client.connected.wait(), timeout=start_timeout)
        await client.send("LoadReplay", load_replay_data(replay))

        deadline = time.monotonic() + start_timeout
        while not state["replay_ticks"]:
            if time.monotonic() > deadline:
                log.error("no replay ticks within %.0fs. Is the game at the main menu, is "
                          "PacketSendRate > 0, and are commands accepted on this port? "
                          "(try --ws)", start_timeout)
                return False
            await asyncio.sleep(0.1)
        log.info("replay playing")

        if seek_seconds is not None:
            await client.send("SeekReplay", {"TimeSeconds": seek_seconds})
        if speed != 1.0:
            await client.send("SetGameSpeed", {"Speed": speed})
        if hide_hud:
            await client.send("SetHUDVisibility", {"bVisible": False})
        if pov:
            await client.send("ChangePOV", {"Focus": pov[0], "Perspective": pov[1]})

        state["last_advance"] = time.monotonic()
        while not state["destroyed"]:
            if time.monotonic() - state["last_advance"] > idle_timeout:
                log.info("replay frame stopped advancing for %.0fs; assuming it ended", idle_timeout)
                break
            if pump_task.done():
                break
            await asyncio.sleep(0.2)
        return True
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
