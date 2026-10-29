import json
import unittest

from rltelemetry.protocol import JsonStreamDecoder, decode_message


class DecoderTests(unittest.TestCase):
    def test_concatenated_without_separator(self):
        d = JsonStreamDecoder()
        out = d.feed(b'{"Event":"A","Data":{}}{"Event":"B","Data":{}}')
        self.assertEqual([o["Event"] for o in out], ["A", "B"])
        self.assertEqual(d.stats.separators[""], 1)

    def test_split_across_chunks_and_separators(self):
        stream = b'{"Event":"A","Data":{"s":"x}{\\"y"}}\n{"Event":"B","Data":{"n":[1,{"k":2}]}}\x00{"Event":"C","Data":{}}\r\n'
        d = JsonStreamDecoder()
        out = []
        for i in range(len(stream)):
            out += d.feed(stream[i:i + 1])
        self.assertEqual([o["Event"] for o in out], ["A", "B", "C"])
        self.assertEqual(out[0]["Data"]["s"], 'x}{"y')
        self.assertEqual(d.stats.separators["\n"], 1)
        self.assertEqual(d.stats.separators["\x00"], 1)

    def test_multibyte_utf8_split(self):
        payload = json.dumps({"Event": "PlayerJoined", "Data": {"PlayerName": "Zoë ⚽"}},
                             ensure_ascii=False).encode()
        cut = payload.index("ë".encode()) + 1  # split inside the two-byte char
        d = JsonStreamDecoder()
        out = d.feed(payload[:cut]) + d.feed(payload[cut:])
        self.assertEqual(out[0]["Data"]["PlayerName"], "Zoë ⚽")

    def test_junk_and_invalid(self):
        d = JsonStreamDecoder()
        out = d.feed(b'garbage{"Event":"A","Data":{}}{bad json}{"Event":"B","Data":{}}')
        self.assertEqual([o["Event"] for o in out], ["A", "B"])
        self.assertEqual(d.stats.invalid_objects, 1)
        self.assertEqual(d.stats.skipped_bytes, len("garbage"))

    def test_buffer_does_not_grow(self):
        d = JsonStreamDecoder()
        for _ in range(1000):
            d.feed(b'{"Event":"A","Data":{}}\n')
        self.assertLess(len(d._buf), 4)


class MessageTests(unittest.TestCase):
    def test_data_as_json_string(self):
        m = decode_message({"Event": "GoalScored", "Data": '{"GoalSpeed": 99.5}'})
        self.assertEqual(m.data, {"GoalSpeed": 99.5})

    def test_non_messages(self):
        self.assertIsNone(decode_message([1, 2]))
        self.assertIsNone(decode_message({"Command": "X"}))
        self.assertEqual(decode_message({"Event": "MatchCreated"}).data, {})


if __name__ == "__main__":
    unittest.main()
