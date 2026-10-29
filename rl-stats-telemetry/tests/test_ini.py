import tempfile
import unittest
from pathlib import Path

from rltelemetry.ini import IniFile, check, preferred_ini


class IniTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_edits_existing_section_preserving_rest(self):
        p = self.dir / "TAStatsAPI.ini"
        p.write_bytes(b"[Other]\r\nPort=1\r\n\r\n[TAGame.MatchStatsExporter_TA]\r\n"
                      b"; comment\r\nPacketSendRate=0\r\nPort=49123\r\n\r\n[Last]\r\nX=1\r\n")
        f = IniFile.load(p)
        self.assertEqual(check(f.section())[0], "PacketSendRate is 0: UpdateState ticks are disabled")
        f.set_values({"PacketSendRate": "60", "Port": "49123", "WebPort": "49124"})
        bak = f.save()
        self.assertTrue(bak.exists())
        text = p.read_bytes().decode()
        self.assertIn("[Other]\r\nPort=1\r\n", text)
        self.assertIn("; comment\r\nPacketSendRate=60\r\nPort=49123\r\nWebPort=49124\r\n\r\n[Last]", text)
        self.assertEqual(IniFile.load(p).section(),
                         {"PacketSendRate": "60", "Port": "49123", "WebPort": "49124"})
        self.assertEqual(check(IniFile.load(p).section()), [])

    def test_adds_missing_section_utf16(self):
        p = self.dir / "DefaultStatsAPI.ini"
        p.write_bytes("[Core]\nA=1\n".encode("utf-16"))
        f = IniFile.load(p)
        self.assertIsNone(f.section())
        f.set_values({"PacketSendRate": "120"})
        f.save(backup=False)
        self.assertTrue(p.read_bytes().startswith(b"\xff\xfe"))
        self.assertEqual(IniFile.load(p).section(), {"PacketSendRate": "120"})

    def test_checks(self):
        self.assertIn("Port and WebPort must differ",
                      check({"PacketSendRate": "60", "Port": "5", "WebPort": "5"}))
        self.assertTrue(any("caps" in x for x in check({"PacketSendRate": "240"})))

    def test_preferred_ini(self):
        self.assertEqual(preferred_ini(self.dir).name, "TAStatsAPI.ini")
        (self.dir / "DefaultStatsAPI.ini").write_text("")
        self.assertEqual(preferred_ini(self.dir).name, "DefaultStatsAPI.ini")
        (self.dir / "TAStatsAPI.ini").write_text("")
        self.assertEqual(preferred_ini(self.dir).name, "TAStatsAPI.ini")


if __name__ == "__main__":
    unittest.main()
