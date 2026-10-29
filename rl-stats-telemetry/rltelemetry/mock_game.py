"""A fake Rocket League Stats API server for testing without the game.

Plays a short scripted 2v2 match per connection and answers ``LoadReplay``
by playing a "replay" in which the spectator-only car fields are present.
Framing between messages is configurable so the decoder can be exercised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from typing import Any

from .protocol import JsonStreamDecoder

log = logging.getLogger(__name__)

FRAMINGS = {"newline": b"\n", "crlf": b"\r\n", "none": b"", "nul": b"\x00"}

PLAYERS = [
    ("Son", "Steam|76561198000000001|0", 1, 0),
    ("Teammate", "Epic|abc123|0", 2, 0),
    ("Bot Rex", "Unknown|0|0", 3, 1),
    ("Bot Tex", "Unknown|0|1", 4, 1),
]


class MockGame:
    def __init__(self, rate: int = 60, framing: str = "newline", spectator: bool = False,
                 match_seconds: float = 6.0, data_as_string: bool = False,
                 guid: str = "A1B2C3D4E5F6", autoplay: bool = True, loop: bool = False) -> None:
        self.rate = rate
        self.sep = FRAMINGS[framing]
        self.spectator = spectator
        self.match_seconds = match_seconds
        self.data_as_string = data_as_string
        self.guid = guid
        self.autoplay = autoplay
        self.loop = loop
        self.speed = 1.0
        self.paused = False
        self.commands: list[dict[str, Any]] = []
        self._clients: set[Any] = set()
        self._task: asyncio.Task | None = None
        self.matches_played = 0

    # ---- transport ---------------------------------------------------------
    async def serve_tcp(self, host: str = "127.0.0.1", port: int = 49123) -> asyncio.base_events.Server:
        return await asyncio.start_server(self._on_tcp, host, port)

    async def serve_ws(self, host: str = "127.0.0.1", port: int = 49124):
        from websockets.asyncio.server import serve
        return await serve(self._on_ws, host, port)

    async def _on_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = ("tcp", writer)
        self._clients.add(client)
        self._client_connected()
        dec = JsonStreamDecoder()
        try:
            while chunk := await reader.read(4096):
                for obj in dec.feed(chunk):
                    self._command(obj)
        except ConnectionError:
            pass
        finally:
            self._clients.discard(client)
            writer.close()

    async def _on_ws(self, ws) -> None:
        client = ("ws", ws)
        self._clients.add(client)
        self._client_connected()
        try:
            async for frame in ws:
                self._command(json.loads(frame))
        except Exception:  # noqa: BLE001 - connection closed
            pass
        finally:
            self._clients.discard(client)

    def _client_connected(self) -> None:
        if self.autoplay and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._play_loop())

    async def _send(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data) if self.data_as_string else data
        text = json.dumps({"Event": event, "Data": payload})
        for kind, conn in list(self._clients):
            try:
                if kind == "tcp":
                    conn.write(text.encode() + self.sep)
                    await conn.drain()
                else:
                    await conn.send(text)
            except Exception:  # noqa: BLE001
                self._clients.discard((kind, conn))

    def _command(self, obj: dict[str, Any]) -> None:
        self.commands.append(obj)
        name, data = obj.get("Command"), obj.get("Data") or {}
        log.info("mock got command %s %s", name, data)
        if name == "LoadReplay":
            if self._task and not self._task.done():
                self._task.cancel()
            self._task = asyncio.create_task(self.play_match(replay=True))
        elif name == "SetGameSpeed":
            self.speed = max(float(data.get("Speed", 1.0)), 0.0)
        elif name == "SetMatchPaused":
            self.paused = bool(data.get("bPaused"))

    # ---- match script ------------------------------------------------------
    async def _play_loop(self) -> None:
        while True:
            await self.play_match(replay=False)
            if not self.loop:
                return
            await asyncio.sleep(1.0)

    async def play_match(self, replay: bool = False) -> None:
        spectator = self.spectator or replay
        guid = self.guid if not self.matches_played else f"{self.guid}-{self.matches_played}"
        guid += "R" if replay else ""
        rnd = random.Random(42)
        stats = {p[0]: dict(Score=0, Goals=0, Shots=0, Assists=0, Saves=0, Touches=0,
                            CarTouches=0, Demos=0) for p in PLAYERS}
        score = [0, 0]
        g = {"MatchGuid": guid}

        await self._send("MatchCreated", {})
        await self._send("MatchInitialized", {})
        for p in PLAYERS:
            await self._send("PlayerJoined", {**g, "PlayerName": p[0], "PrimaryId": p[1]})
        await self._send("CountdownBegin", g)
        await self._send("RoundStarted", g)

        ticks = int(self.match_seconds * self.rate)
        clock = 300
        for i in range(ticks):
            while self.paused:
                await asyncio.sleep(0.05)
            t = i / self.rate
            if i % self.rate == 0 and i:
                clock -= 1
                await self._send("ClockUpdatedSeconds", {**g, "TimeSeconds": clock, "bOvertime": False})
            if i % (self.rate // 2 or 1) == 5:
                who = PLAYERS[(i // (self.rate // 2 or 1)) % 4]
                stats[who[0]]["Touches"] += 1
                await self._send("BallHit", {**g, "Players": [_ref(who)], "Ball": {
                    "PreHitSpeed": 900.0, "PostHitSpeed": 2200.5,
                    "Location": {"X": rnd.uniform(-4000, 4000), "Y": rnd.uniform(-5000, 5000), "Z": 93.1}}})
            if spectator and i % self.rate == 10:
                await self._send("BoostPickup", {**g, "Player": _ref(PLAYERS[0]),
                                                 "Location": {"X": 3072.0, "Y": -4096.0, "Z": 73.0},
                                                 "BoostAmount": 100, "BoostType": "BoostType_Pill",
                                                 "bReplay": replay})
            if i == ticks // 2:
                score[0] += 1
                stats["Son"]["Goals"] += 1
                stats["Son"]["Score"] += 100
                await self._send("GoalScored", {**g, "GoalSpeed": 101.4, "GoalTime": t,
                                                "ImpactLocation": {"X": 120.0, "Y": 5120.0, "Z": 300.0},
                                                "Scorer": _ref(PLAYERS[0]), "Assister": _ref(PLAYERS[1]),
                                                "BallLastTouch": {"Player": _ref(PLAYERS[0]), "Speed": 2800.0}})
                await self._send("StatfeedEvent", {**g, "EventName": "Goal", "Type": "Goal",
                                                   "MainTarget": _ref(PLAYERS[0])})
                await self._send("GoalReplayStart", g)
                await self._send("GoalReplayEnd", g)
            if i == ticks // 3:
                stats["Bot Rex"]["Demos"] += 1
                await self._send("StatfeedEvent", {**g, "EventName": "Demolish", "Type": "Demolition",
                                                   "MainTarget": _ref(PLAYERS[2]),
                                                   "SecondaryTarget": _ref(PLAYERS[1])})
            await self._send("UpdateState", self._tick(g, t, i, clock, score, stats, spectator, replay))
            await asyncio.sleep(1.0 / self.rate / max(self.speed, 0.01))

        await self._send("MatchEnded", {**g, "WinnerTeamNum": 0})
        await self._send("PodiumStart", g)
        await self._send("MatchDestroyed", g)
        self.matches_played += 1

    def _tick(self, g, t, i, clock, score, stats, spectator, replay) -> dict[str, Any]:
        players = []
        for name, pid, shortcut, team in PLAYERS:
            p = {"Name": name, "PrimaryId": pid, "Shortcut": shortcut, "TeamNum": team,
                 **stats[name], "Loadout": []}
            if spectator:
                speed = 1100 + 1100 * math.sin(t * 1.3 + shortcut)
                p.update(bHasCar=True, Speed=round(speed, 1),
                         Boost=int(50 + 50 * math.cos(t + shortcut)), bBoosting=speed > 1800,
                         bOnGround=(i // 30) % 3 != 0, bOnWall=(i // 30) % 7 == 0,
                         bPowersliding=(i // 15) % 9 == 0, bDemolished=False,
                         bSupersonic=speed > 2200)
            players.append(p)
        game = {
            "Teams": [{"Name": "Blue", "TeamNum": 0, "Score": score[0], "ColorPrimary": "1873FF", "ColorSecondary": "E5E5E5"},
                      {"Name": "Orange", "TeamNum": 1, "Score": score[1], "ColorPrimary": "C26418", "ColorSecondary": "E5E5E5"}],
            "PlaylistId": 11, "TimeSeconds": clock, "bOvertime": False,
            "Ball": {"Speed": round(1500 + 500 * math.sin(t), 1), "TeamNum": i % 2},
            "bReplay": replay, "bHasWinner": False, "Winner": "", "Arena": "Stadium_P",
            "bHasTarget": True, "Target": _ref(PLAYERS[0]),
        }
        if replay:
            game.update(Frame=i, Elapsed=round(t, 3))
        return {**g, "Players": players, "Game": game}


def _ref(p: tuple) -> dict[str, Any]:
    return {"Name": p[0], "Shortcut": p[2], "TeamNum": p[3]}
