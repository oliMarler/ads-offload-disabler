"""Full loop against the mock game over real sockets."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from rltelemetry.mock_game import MockGame
from rltelemetry.pipeline import Pipeline
from rltelemetry.replay import run_replay
from rltelemetry.sinks.osa import OsaSink
from rltelemetry.transport import StatsClient


class RecordingPublisher:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


def lines(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def _live(self, framing, websocket=False, data_as_string=False):
        game = MockGame(rate=120, framing=framing, match_seconds=1.0, data_as_string=data_as_string)
        server = await (game.serve_ws("127.0.0.1", 0) if websocket else game.serve_tcp("127.0.0.1", 0))
        port = next(iter(server.sockets)).getsockname()[1]
        pub = RecordingPublisher()
        pipe = Pipeline(self.out, driver="Son", extra_sinks=[OsaSink(pub)])
        client = StatsClient("127.0.0.1", port, websocket=websocket, reconnect=False)

        async def consume():
            async for rx, msg in client.events():
                pipe.handle(rx, msg)
                if msg.event == "MatchDestroyed":
                    return
        await asyncio.wait_for(consume(), 15)
        await client.close()
        pipe.close()
        server.close()
        return pipe, pub

    async def test_live_match_tcp_all_framings(self):
        for framing in ("newline", "none", "nul"):
            with self.subTest(framing=framing):
                pipe, pub = await self._live(framing, data_as_string=(framing == "none"))
                self.assertEqual(pipe.coverage.events["MatchDestroyed"], 1)
                match_file = self.out / "matches" / "A1B2C3D4E5F6.jsonl"
                events = [l["Event"] for l in lines(match_file)]
                self.assertEqual(events[0], "MatchCreated")   # pre-guid events adopted
                self.assertEqual(events[-1], "MatchDestroyed")
                self.assertEqual(events.count("UpdateState"), 120)
                # Live play: no spectator fields
                self.assertEqual(pipe.coverage.field_hits["Speed"], 0)
                ch = lines(self.out / "channels" / "A1B2C3D4E5F6.jsonl")
                self.assertEqual(ch[0]["type"], "channels")
                samples = [c for c in ch if c["type"] == "sample"]
                self.assertEqual(samples[0]["v"]["Driver.Score"], 0)
                self.assertNotIn("Driver.Speed", samples[0]["v"])
                goal = next(c for c in ch if c["type"] == "event" and c["name"] == "GoalScored")
                self.assertEqual(goal["text"], "Goal Son (assist Teammate)")
                names = [c[0] for c in pub.calls]
                self.assertEqual(names[0], "start_session")
                self.assertIn("rename_session", names)
                self.assertEqual(names[-1], "end_session")
                self.tearDown(); self.setUp()

    async def test_live_match_websocket(self):
        pipe, _ = await self._live("newline", websocket=True)
        self.assertEqual(pipe.coverage.events["UpdateState"], 120)

    async def test_replay_mode_records_spectator_fields(self):
        game = MockGame(rate=120, match_seconds=1.0, autoplay=False)
        server = await game.serve_tcp("127.0.0.1", 0)
        port = next(iter(server.sockets)).getsockname()[1]
        pipe = Pipeline(self.out, driver="Son", tag="replay")
        client = StatsClient("127.0.0.1", port)
        ok = await asyncio.wait_for(run_replay(client, pipe, "MyReplay", speed=2.0,
                                               pov=("Ball", "AutoCam"), idle_timeout=1.0), 20)
        await client.close()
        pipe.close()
        server.close()
        self.assertTrue(ok)
        cmds = [c["Command"] for c in game.commands]
        self.assertEqual(cmds, ["LoadReplay", "SetGameSpeed", "ChangePOV"])
        self.assertEqual(game.commands[0]["Data"], {"FileName": "MyReplay"})
        self.assertEqual(pipe.coverage.field_hits["Speed"], pipe.coverage.player_ticks)
        self.assertTrue((self.out / "matches" / "A1B2C3D4E5F6R.replay.jsonl").exists())
        samples = [c for c in lines(self.out / "channels" / "A1B2C3D4E5F6R.replay.jsonl")
                   if c["type"] == "sample"]
        self.assertIn("Driver.SpeedKph", samples[-1]["v"])
        self.assertIn("Game.Elapsed", samples[-1]["v"])


if __name__ == "__main__":
    unittest.main()
