import json
import tempfile
import unittest
from pathlib import Path

from rltelemetry.channels import ChannelMapper, channel_catalogue
from rltelemetry.pipeline import Pipeline
from rltelemetry.protocol import Message


def tick(guid=None, players=(), **game):
    d = {"Players": list(players), "Game": {"TimeSeconds": 300, **game}}
    if guid:
        d["MatchGuid"] = guid
    return Message("UpdateState", d)


def player(name, team, **kw):
    return {"Name": name, "PrimaryId": f"Steam|{name}|0", "TeamNum": team, "Score": 10, **kw}


class Collect:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_match_adopts_guid_and_splits_by_guid(self):
        pipe = Pipeline(self.out)
        pipe.handle(1, Message("MatchCreated", {}))
        pipe.handle(2, Message("MatchInitialized", {}))
        pipe.handle(3, tick("G1"))
        pipe.handle(4, Message("MatchDestroyed", {"MatchGuid": "G1"}))
        pipe.handle(5, Message("PlayerJoined", {"PlayerName": "menu"}))  # between matches
        pipe.handle(6, tick("G2"))
        pipe.handle(7, tick("G3"))  # guid change without MatchDestroyed
        pipe.close()

        files = sorted(p.name for p in (self.out / "matches").glob("*.jsonl"))
        self.assertEqual(files, ["G1.jsonl", "G2.jsonl", "G3.jsonl", "index.jsonl"])
        g1 = [json.loads(l)["Event"] for l in (self.out / "matches/G1.jsonl").read_text().splitlines()]
        self.assertEqual(g1, ["MatchCreated", "MatchInitialized", "UpdateState", "MatchDestroyed"])
        index = [json.loads(l) for l in (self.out / "matches/index.jsonl").read_text().splitlines()]
        self.assertEqual([i["MatchGuid"] for i in index], ["G1", "G2", "G3"])
        raw = (self.out / "raw").glob("*.jsonl").__next__().read_text().splitlines()
        self.assertEqual(len(raw), 7)
        self.assertIsNone(json.loads(raw[4])["match"])
        self.assertTrue((self.out / "channels/G1.jsonl").exists())

    def test_offline_match_keeps_local_key(self):
        pipe = Pipeline(self.out, channels=False)
        pipe.handle(1, tick())  # connected mid-match, no guid
        pipe.handle(2, Message("MatchEnded", {"WinnerTeamNum": 1}))
        pipe.handle(3, Message("MatchDestroyed", {}))
        pipe.close()
        idx = json.loads((self.out / "matches/index.jsonl").read_text())
        self.assertTrue(idx["key"].startswith("local-"))
        self.assertEqual(idx["WinnerTeamNum"], 1)


class ChannelTests(unittest.TestCase):
    def test_slots_driver_and_spectator_fields(self):
        sink = Collect()
        mapper = ChannelMapper([sink], driver_name="son")
        from rltelemetry.matches import MatchInfo
        m = MatchInfo("k", None, 0)
        mapper.match_opened(m)
        s = mapper.sample(1, tick(players=[
            player("Bob", 1),
            player("Son", 0, Speed="1000", Boost=33, bOnGround=True),
            player("Amy", 0),
        ], Ball={"Speed": 1000, "TeamNum": 255}, Teams=[{"TeamNum": 0, "Score": 2}]).data, m)
        v = s.values
        self.assertEqual(v["Orange1.Score"], 10)
        self.assertEqual(v["Blue1.Speed"], 1000)
        self.assertAlmostEqual(v["Blue1.SpeedKph"], 36.0)
        self.assertAlmostEqual(v["Driver.SpeedKph"], 36.0)
        self.assertEqual(v["Driver.OnGround"], 1.0)
        self.assertNotIn("Blue2.Speed", v)   # not spectating: no car fields
        self.assertEqual(v["Blue2.Present"], 1.0)
        self.assertEqual(v["Game.ScoreBlue"], 2)
        self.assertAlmostEqual(v["Ball.SpeedKph"], 36.0)
        roster = [c for c in sink.calls if c[0] == "on_roster"][0][1][2]
        self.assertEqual(roster["Blue2"]["Name"], "Amy")
        names = {c.name for c in channel_catalogue()}
        self.assertTrue(set(v) <= names, set(v) - names)

    def test_event_flattening(self):
        sink = Collect()
        mapper = ChannelMapper([sink])
        from rltelemetry.matches import MatchInfo
        m = MatchInfo("k", None, 0)
        mapper.match_message(m, 5, Message("GoalScored", {
            "GoalSpeed": 120.5, "Scorer": {"Name": "Son", "TeamNum": 0},
            "Assister": {"Name": "Amy"}, "ImpactLocation": {"X": 1, "Y": 2, "Z": 3}}))
        mark = sink.calls[-1][1][1]
        self.assertEqual(mark.text, "Goal Son (assist Amy)")
        self.assertEqual(mark.fields["ImpactLocation.Y"], 2)
        self.assertEqual(mark.fields["Scorer.TeamNum"], 0)


if __name__ == "__main__":
    unittest.main()
