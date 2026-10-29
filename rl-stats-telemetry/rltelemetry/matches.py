"""Splits the event stream into matches.

Matches are keyed by ``MatchGuid``. The guid only exists for online/LAN
matches and may not be on the first lifecycle events, so a match without one
gets a provisional ``local-<time>`` key that is swapped for the guid as soon
as one shows up (listeners get ``match_rekeyed``).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .protocol import UPDATE_STATE, Message

# Events that can lazily open a match when we connect mid-game without a guid.
_IN_MATCH_EVENTS = {
    UPDATE_STATE, "BallHit", "BoostPickup", "ClockUpdatedSeconds", "CrossbarHit",
    "GoalScored", "StatfeedEvent", "CountdownBegin", "RoundStarted",
    "GoalReplayStart", "GoalReplayWillEnd", "GoalReplayEnd", "MatchPaused",
    "MatchUnpaused", "MatchEnded", "PodiumStart",
}


@dataclass
class MatchInfo:
    key: str
    guid: str | None
    started_ns: int
    tag: str = ""
    ended_ns: int | None = None
    messages: int = 0
    seen_created: bool = False
    seen_tick: bool = False
    arena: str | None = None
    playlist: Any = None
    teams: dict[int, dict[str, Any]] = field(default_factory=dict)
    players: dict[str, dict[str, Any]] = field(default_factory=dict)
    winner_team: int | None = None
    replay: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "MatchGuid": self.guid,
            "tag": self.tag or None,
            "started": _iso(self.started_ns),
            "ended": _iso(self.ended_ns) if self.ended_ns else None,
            "messages": self.messages,
            "Arena": self.arena,
            "PlaylistId": self.playlist,
            "replay": self.replay,
            "teams": [self.teams[k] for k in sorted(self.teams)],
            "WinnerTeamNum": self.winner_team,
            "players": list(self.players.values()),
        }

    def absorb(self, msg: Message) -> None:
        """Keep a running summary for the match index."""
        d = msg.data
        if msg.event == UPDATE_STATE:
            self.seen_tick = True
            game = d.get("Game") or {}
            self.arena = game.get("Arena", self.arena)
            self.playlist = game.get("PlaylistId", self.playlist)
            self.replay = self.replay or bool(game.get("bReplay"))
            for t in game.get("Teams") or []:
                if isinstance(t, dict) and "TeamNum" in t:
                    self.teams[int(t["TeamNum"])] = {
                        k: t.get(k) for k in ("TeamNum", "Name", "Score")
                    }
            for p in d.get("Players") or []:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("PrimaryId") or p.get("Name"))
                self.players[pid] = {
                    k: p.get(k) for k in (
                        "Name", "PrimaryId", "TeamNum", "Score", "Goals", "Shots",
                        "Assists", "Saves", "Touches", "Demos",
                    )
                }
        elif msg.event == "MatchEnded":
            w = d.get("WinnerTeamNum")
            self.winner_team = int(w) if isinstance(w, (int, float)) else self.winner_team
        elif msg.event == "MatchCreated":
            self.seen_created = True


class MatchListener(Protocol):
    def match_opened(self, match: MatchInfo) -> None: ...
    def match_rekeyed(self, match: MatchInfo, old_key: str) -> None: ...
    def match_message(self, match: MatchInfo, rx_ns: int, msg: Message) -> None: ...
    def match_closed(self, match: MatchInfo) -> None: ...


class MatchTracker:
    def __init__(self, listeners: list[MatchListener], tag: str = "") -> None:
        self.listeners = listeners
        self.tag = tag
        self.current: MatchInfo | None = None

    def handle(self, rx_ns: int, msg: Message) -> MatchInfo | None:
        """Route one message; returns the match it was attributed to, if any."""
        guid = msg.data.get("MatchGuid")
        guid = guid if isinstance(guid, str) and guid.strip() else None
        cur = self.current

        if guid:
            if cur is not None and cur.guid is None:
                old = cur.key
                cur.guid = guid
                cur.key = safe_key(guid)
                for li in self.listeners:
                    li.match_rekeyed(cur, old)
            elif cur is not None and cur.guid != guid:
                self.close()
                cur = self._open(rx_ns, guid)
            elif cur is None:
                cur = self._open(rx_ns, guid)
        elif msg.event == "MatchCreated":
            if cur is not None and (cur.seen_created or cur.seen_tick):
                self.close()
                cur = None
            if cur is None:
                cur = self._open(rx_ns, None)
        elif cur is None and (msg.event == "MatchInitialized" or msg.event in _IN_MATCH_EVENTS):
            cur = self._open(rx_ns, None)

        if cur is None:
            return None
        cur.messages += 1
        cur.absorb(msg)
        for li in self.listeners:
            li.match_message(cur, rx_ns, msg)
        if msg.event == "MatchDestroyed":
            cur.ended_ns = rx_ns
            self.close()
        return cur

    def close(self) -> None:
        cur = self.current
        if cur is None:
            return
        if cur.ended_ns is None:
            cur.ended_ns = time.time_ns()
        self.current = None
        for li in self.listeners:
            li.match_closed(cur)

    def _open(self, rx_ns: int, guid: str | None) -> MatchInfo:
        key = safe_key(guid) if guid else "local-" + time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(rx_ns / 1e9))
        match = MatchInfo(key=key, guid=guid, started_ns=rx_ns, tag=self.tag)
        self.current = match
        for li in self.listeners:
            li.match_opened(match)
        return match


def safe_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:120]


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ns / 1e9)) + f".{ns // 1_000_000 % 1000:03d}"
