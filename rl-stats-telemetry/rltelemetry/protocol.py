"""Wire format of the Rocket League Stats API.

Outbound messages are ``{"Event": name, "Data": {...}}``; inbound commands are
``{"Command": name, "Data": {...}}``. Two quirks are handled here:

* TCP framing is undocumented. Community clients see back-to-back JSON
  objects, sometimes newline separated, sometimes not. ``JsonStreamDecoder``
  therefore finds object boundaries by brace depth and does not depend on any
  delimiter. It also records what it saw between objects so ``probe`` can
  report the real framing.
* ``Data`` has been observed both as an object and as a JSON-encoded string.
"""

from __future__ import annotations

import codecs
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Lifecycle and gameplay events listed in the official docs.
UPDATE_STATE = "UpdateState"
EVENTS = (
    UPDATE_STATE,
    "BallHit", "BoostPickup", "ClockUpdatedSeconds", "CrossbarHit", "GoalScored",
    "StatfeedEvent", "MatchCreated", "MatchInitialized", "CountdownBegin",
    "RoundStarted", "GoalReplayStart", "GoalReplayWillEnd", "GoalReplayEnd",
    "MatchPaused", "MatchUnpaused", "MatchEnded", "PodiumStart", "MatchDestroyed",
    "PlayerJoined", "PlayerLeft", "ReplayCreated",
)

# Car-level UpdateState player fields that the docs say only exist when spectating.
SPECTATOR_PLAYER_FIELDS = (
    "bHasCar", "Speed", "Boost", "bBoosting", "bOnGround", "bOnWall",
    "bPowersliding", "bDemolished", "bSupersonic",
)

POV_PERSPECTIVES = ("Fly", "SoftAttach", "HardAttach", "PlayerView", "AutoCam", "Camera_Director")


@dataclass(frozen=True)
class Message:
    event: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"Event": self.event, "Data": self.data}


def decode_message(obj: Any) -> Message | None:
    """Turn a decoded JSON value into a Message, or None if it isn't one."""
    if not isinstance(obj, dict):
        return None
    event = obj.get("Event", obj.get("event"))
    if not isinstance(event, str):
        return None
    data = obj.get("Data", obj.get("data"))
    if isinstance(data, str):
        try:
            data = json.loads(data) if data.strip() else {}
        except json.JSONDecodeError:
            data = {"_raw": data}
    if not isinstance(data, dict):
        data = {} if data is None else {"_value": data}
    return Message(event, data)


def encode_command(name: str, data: dict[str, Any] | None = None) -> bytes:
    return json.dumps({"Command": name, "Data": data or {}}, separators=(",", ":")).encode()


def to_number(value: Any) -> float | None:
    """Numbers sometimes arrive as strings; bools become 0/1."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass
class FramingStats:
    objects: int = 0
    invalid_objects: int = 0
    skipped_bytes: int = 0
    # Exact text found between consecutive objects, e.g. {"\n": 812, "": 3}.
    separators: Counter = field(default_factory=Counter)

    def describe(self) -> str:
        if not self.separators:
            return "no separators observed yet"
        parts = [f"{sep!r} x{n}" for sep, n in self.separators.most_common()]
        return ", ".join(parts)


class JsonStreamDecoder:
    """Incremental decoder for a byte stream of concatenated JSON objects."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._pos = 0          # scan position in _buf
        self._start = -1       # start of the current object, -1 when between objects
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._gap = ""         # text seen since the previous object ended
        self._seen_first = False
        self.stats = FramingStats()

    def feed(self, chunk: bytes) -> list[Any]:
        self._buf += self._utf8.decode(chunk)
        out: list[Any] = []
        buf = self._buf
        i = self._pos
        n = len(buf)
        while i < n:
            c = buf[i]
            if self._start < 0:
                if c == "{" or c == "[":
                    self._start = i
                    self._depth = 1
                else:
                    self._gap += c
                    if not c.isspace() and c not in "\x00,;":
                        self.stats.skipped_bytes += 1
                i += 1
                continue
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif c == "\\":
                    self._escape = True
                elif c == '"':
                    self._in_string = False
            elif c == '"':
                self._in_string = True
            elif c == "{" or c == "[":
                self._depth += 1
            elif c == "}" or c == "]":
                self._depth -= 1
                if self._depth == 0:
                    text = buf[self._start:i + 1]
                    if self._seen_first:
                        self.stats.separators[self._gap] += 1
                    self._seen_first = True
                    self._gap = ""
                    self._start = -1
                    try:
                        out.append(json.loads(text))
                        self.stats.objects += 1
                    except json.JSONDecodeError:
                        self.stats.invalid_objects += 1
            i += 1
        # Drop everything already consumed so the buffer only holds a partial object.
        keep_from = self._start if self._start >= 0 else n
        self._buf = buf[keep_from:]
        self._pos = i - keep_from
        if self._start >= 0:
            self._start = 0
        return out
