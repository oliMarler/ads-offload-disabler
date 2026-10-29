"""Stage 2 contract: turns Stats API messages into telemetry channels.

Sinks never see the game's JSON. They get:

* ``ChannelDef``  - a fixed catalogue (name, unit, type, description) that can
  be registered with ATLAS up front, the same way for every match.
* ``Sample``      - one row per UpdateState tick: ``{channel: value}``, only for
  values that were present (spectator-only fields are absent while driving).
* ``EventMark``   - one per discrete event (goal, hit, demo...), with numeric
  fields flattened (``Ball.Location.X``) and a short text label.

Players are mapped onto fixed slots ``Blue1..Blue4`` / ``Orange1..Orange4`` in
order of first appearance in the match, so parameter names are stable across
matches. The slot -> player mapping is reported via ``Sink.on_roster``.
Optionally one named player is mirrored to ``Driver.*`` (e.g. your son), which
gives a constant channel set no matter which team or slot he lands in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .matches import MatchInfo
from .protocol import UPDATE_STATE, Message, to_number

UU_TO_KPH = 0.036  # 1 uu = 1 cm, so uu/s * 3600 / 100000
TEAMS = {0: "Blue", 1: "Orange"}
SLOTS_PER_TEAM = 4


@dataclass(frozen=True)
class ChannelDef:
    name: str
    unit: str
    kind: str            # "float" | "int" | "bool"
    description: str
    spectator_only: bool = False


@dataclass
class Sample:
    ts_ns: int
    values: dict[str, float]


@dataclass
class EventMark:
    ts_ns: int
    name: str
    text: str
    fields: dict[str, float] = field(default_factory=dict)


# (json field, channel suffix, unit, kind, description, spectator-only)
_PLAYER_FIELDS = (
    ("Score", "Score", "pts", "int", "Match score", False),
    ("Goals", "Goals", "", "int", "Goals", False),
    ("Shots", "Shots", "", "int", "Shots", False),
    ("Assists", "Assists", "", "int", "Assists", False),
    ("Saves", "Saves", "", "int", "Saves", False),
    ("Touches", "Touches", "", "int", "Ball touches", False),
    ("CarTouches", "CarTouches", "", "int", "Car touches", False),
    ("Demos", "Demos", "", "int", "Demolitions inflicted", False),
    ("bHasCar", "HasCar", "", "bool", "Car exists", True),
    ("Speed", "Speed", "uu/s", "float", "Car speed", True),
    ("Boost", "Boost", "%", "float", "Boost amount 0-100", True),
    ("bBoosting", "Boosting", "", "bool", "Boost active", True),
    ("bOnGround", "OnGround", "", "bool", "3+ wheels on ground", True),
    ("bOnWall", "OnWall", "", "bool", "On wall", True),
    ("bPowersliding", "Powersliding", "", "bool", "Handbrake / powerslide", True),
    ("bDemolished", "Demolished", "", "bool", "Currently demolished", True),
    ("bSupersonic", "Supersonic", "", "bool", "Supersonic", True),
)

_GAME_CHANNELS = (
    ChannelDef("Game.TimeRemaining", "s", "float", "Match clock remaining"),
    ChannelDef("Game.Overtime", "", "bool", "Overtime"),
    ChannelDef("Game.ScoreBlue", "", "int", "Blue team score"),
    ChannelDef("Game.ScoreOrange", "", "int", "Orange team score"),
    ChannelDef("Game.Replay", "", "bool", "Replay playing (goal replay or replay file)"),
    ChannelDef("Game.HasWinner", "", "bool", "Winner decided"),
    ChannelDef("Game.Frame", "", "int", "Replay frame (replay only)"),
    ChannelDef("Game.Elapsed", "s", "float", "Replay elapsed time (replay only)"),
    ChannelDef("Ball.Speed", "uu/s", "float", "Ball speed"),
    ChannelDef("Ball.SpeedKph", "km/h", "float", "Ball speed"),
    ChannelDef("Ball.LastTouchTeam", "", "int", "Team of last touch, 255 untouched"),
)


def _team(raw: Any) -> str | None:
    n = to_number(raw)
    return TEAMS.get(int(n)) if n is not None else None


def _player_defs(prefix: str) -> list[ChannelDef]:
    defs = []
    for _, suffix, unit, kind, desc, spec in _PLAYER_FIELDS:
        defs.append(ChannelDef(f"{prefix}.{suffix}", unit, kind, desc, spec))
        if suffix == "Speed":
            defs.append(ChannelDef(f"{prefix}.SpeedKph", "km/h", "float", "Car speed", spec))
    defs.append(ChannelDef(f"{prefix}.Present", "", "bool", "Slot occupied this tick"))
    return defs


def channel_catalogue(driver: bool = True) -> list[ChannelDef]:
    defs = list(_GAME_CHANNELS)
    for team in TEAMS.values():
        for i in range(1, SLOTS_PER_TEAM + 1):
            defs += _player_defs(f"{team}{i}")
    if driver:
        defs += _player_defs("Driver")
    return defs


class Sink(Protocol):
    """Implemented by anything that publishes channels (JSONL, ATLAS/OSA...)."""

    def on_match_start(self, match: MatchInfo, channels: list[ChannelDef]) -> None: ...
    def on_match_rekeyed(self, match: MatchInfo, old_key: str) -> None: ...
    def on_roster(self, match: MatchInfo, ts_ns: int, roster: dict[str, dict[str, Any]]) -> None: ...
    def on_sample(self, match: MatchInfo, sample: Sample) -> None: ...
    def on_event(self, match: MatchInfo, mark: EventMark) -> None: ...
    def on_match_end(self, match: MatchInfo) -> None: ...


def flatten(data: Any, prefix: str = "") -> tuple[dict[str, float], dict[str, str]]:
    """Split a nested payload into numeric fields and string fields."""
    nums: dict[str, float] = {}
    texts: dict[str, str] = {}

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, sub in v.items():
                walk(sub, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                walk(sub, f"{path}[{i}]")
        elif isinstance(v, str):
            n = to_number(v)
            if n is None:
                texts[path] = v
            else:
                nums[path] = n
        else:
            n = to_number(v)
            if n is not None:
                nums[path] = n

    walk(data, prefix)
    return nums, texts


def _event_text(msg: Message, texts: dict[str, str]) -> str:
    d = msg.data
    def name(obj: Any) -> str:
        return obj.get("Name", "?") if isinstance(obj, dict) else str(obj)
    if msg.event == "GoalScored":
        s = f"Goal {name(d.get('Scorer'))}"
        if d.get("Assister"):
            s += f" (assist {name(d['Assister'])})"
        return s
    if msg.event == "StatfeedEvent":
        s = f"{d.get('EventName', '?')} {name(d.get('MainTarget'))}"
        if d.get("SecondaryTarget"):
            s += f" -> {name(d['SecondaryTarget'])}"
        return s
    if msg.event == "BallHit":
        return "Hit " + ", ".join(name(p) for p in d.get("Players") or [])
    if msg.event == "BoostPickup":
        return f"Boost {name(d.get('Player'))} {d.get('BoostType', '')}".strip()
    if msg.event in ("PlayerJoined", "PlayerLeft"):
        return f"{msg.event} {d.get('PlayerName', '?')}"
    if msg.event == "MatchEnded":
        return f"MatchEnded winner {TEAMS.get(d.get('WinnerTeamNum'), d.get('WinnerTeamNum'))}"
    return msg.event


class ChannelMapper:
    """MatchListener that feeds flattened channels to sinks."""

    def __init__(self, sinks: Iterable[Sink], driver_name: str | None = None) -> None:
        self.sinks = list(sinks)
        self.driver_name = driver_name.casefold() if driver_name else None
        self.catalogue = channel_catalogue(driver=bool(driver_name))
        self._slots: dict[str, str] = {}      # player id -> slot prefix
        self._roster: dict[str, dict[str, Any]] = {}

    # MatchListener -------------------------------------------------------
    def match_opened(self, match: MatchInfo) -> None:
        self._slots.clear()
        self._roster.clear()
        for s in self.sinks:
            s.on_match_start(match, self.catalogue)

    def match_rekeyed(self, match: MatchInfo, old_key: str) -> None:
        for s in self.sinks:
            s.on_match_rekeyed(match, old_key)

    def match_closed(self, match: MatchInfo) -> None:
        for s in self.sinks:
            s.on_match_end(match)

    def match_message(self, match: MatchInfo, rx_ns: int, msg: Message) -> None:
        if msg.event == UPDATE_STATE:
            sample = self.sample(rx_ns, msg.data, match)
            for s in self.sinks:
                s.on_sample(match, sample)
        else:
            nums, texts = flatten(msg.data)
            mark = EventMark(rx_ns, msg.event, _event_text(msg, texts), nums)
            for s in self.sinks:
                s.on_event(match, mark)

    # ---------------------------------------------------------------------
    def _slot(self, player: dict[str, Any]) -> str | None:
        pid = str(player.get("PrimaryId") or player.get("Name") or "")
        if pid in self._slots:
            return self._slots[pid]
        team = _team(player.get("TeamNum"))
        if team is None:
            return None
        used = {v for v in self._slots.values() if v.startswith(team)}
        for i in range(1, SLOTS_PER_TEAM + 1):
            if f"{team}{i}" not in used:
                self._slots[pid] = f"{team}{i}"
                return self._slots[pid]
        return None

    def sample(self, rx_ns: int, data: dict[str, Any], match: MatchInfo) -> Sample:
        v: dict[str, float] = {}
        game = data.get("Game") or {}

        def put(name: str, raw: Any) -> None:
            n = to_number(raw)
            if n is not None:
                v[name] = n

        put("Game.TimeRemaining", game.get("TimeSeconds"))
        put("Game.Overtime", game.get("bOvertime"))
        put("Game.Replay", game.get("bReplay"))
        put("Game.HasWinner", game.get("bHasWinner"))
        put("Game.Frame", game.get("Frame"))
        put("Game.Elapsed", game.get("Elapsed"))
        for t in game.get("Teams") or []:
            team = _team(t.get("TeamNum")) if isinstance(t, dict) else None
            if team:
                put(f"Game.Score{team}", t.get("Score"))
        ball = game.get("Ball") or {}
        put("Ball.Speed", ball.get("Speed"))
        if "Ball.Speed" in v:
            v["Ball.SpeedKph"] = round(v["Ball.Speed"] * UU_TO_KPH, 3)
        put("Ball.LastTouchTeam", ball.get("TeamNum"))

        roster_changed = False
        for p in data.get("Players") or []:
            if not isinstance(p, dict):
                continue
            slot = self._slot(p)
            prefixes = [slot] if slot else []
            if self.driver_name and str(p.get("Name", "")).casefold() == self.driver_name:
                prefixes.append("Driver")
            if slot:
                entry = {"Name": p.get("Name"), "PrimaryId": p.get("PrimaryId"),
                         "TeamNum": p.get("TeamNum"), "Shortcut": p.get("Shortcut")}
                if self._roster.get(slot) != entry:
                    self._roster[slot] = entry
                    roster_changed = True
            for prefix in prefixes:
                v[f"{prefix}.Present"] = 1.0
                for key, suffix, *_ in _PLAYER_FIELDS:
                    put(f"{prefix}.{suffix}", p.get(key))
                if f"{prefix}.Speed" in v:
                    v[f"{prefix}.SpeedKph"] = round(v[f"{prefix}.Speed"] * UU_TO_KPH, 3)

        if roster_changed:
            for s in self.sinks:
                s.on_roster(match, rx_ns, dict(self._roster))
        return Sample(rx_ns, v)
