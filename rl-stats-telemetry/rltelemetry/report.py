"""Field-coverage report: answers the "verify on a live client" questions
(do car channels appear, real tick rate, is MatchGuid present...)."""

from __future__ import annotations

from collections import Counter

from .protocol import EVENTS, SPECTATOR_PLAYER_FIELDS, UPDATE_STATE, Message


class Coverage:
    def __init__(self) -> None:
        self.events: Counter = Counter()
        self.player_ticks = 0
        self.field_hits: Counter = Counter()
        self.replay_ticks = 0
        self.guid_ticks = 0
        self.frame_ticks = 0
        self.first_tick_ns: int | None = None
        self.last_tick_ns: int | None = None
        self.unknown_events: Counter = Counter()

    def add(self, rx_ns: int, msg: Message) -> None:
        self.events[msg.event] += 1
        if msg.event not in EVENTS:
            self.unknown_events[msg.event] += 1
        if msg.event != UPDATE_STATE:
            return
        d = msg.data
        game = d.get("Game") or {}
        self.first_tick_ns = self.first_tick_ns or rx_ns
        self.last_tick_ns = rx_ns
        self.replay_ticks += bool(game.get("bReplay"))
        self.guid_ticks += bool(d.get("MatchGuid"))
        self.frame_ticks += "Frame" in game
        for p in d.get("Players") or []:
            if isinstance(p, dict):
                self.player_ticks += 1
                for f in SPECTATOR_PLAYER_FIELDS:
                    self.field_hits[f] += f in p

    def tick_rate(self) -> float | None:
        n = self.events[UPDATE_STATE]
        if n < 2 or not self.first_tick_ns or self.last_tick_ns == self.first_tick_ns:
            return None
        return (n - 1) / ((self.last_tick_ns - self.first_tick_ns) / 1e9)

    def lines(self) -> list[str]:
        ticks = self.events[UPDATE_STATE]
        out = [f"messages: {sum(self.events.values())}, UpdateState ticks: {ticks}"]
        rate = self.tick_rate()
        if rate:
            out.append(f"measured tick rate: {rate:.1f} Hz (rx clock)")
        if ticks:
            out.append(f"ticks with MatchGuid: {self.guid_ticks}/{ticks}, "
                       f"bReplay: {self.replay_ticks}/{ticks}, Frame/Elapsed: {self.frame_ticks}/{ticks}")
        if self.player_ticks:
            out.append(f"spectator-only car fields over {self.player_ticks} player-ticks:")
            for f in SPECTATOR_PLAYER_FIELDS:
                pct = 100.0 * self.field_hits[f] / self.player_ticks
                out.append(f"  {f:<14} {pct:5.1f}%")
        out.append("events: " + ", ".join(f"{k} {v}" for k, v in sorted(self.events.items())))
        missing = [e for e in EVENTS if not self.events[e]]
        if missing:
            out.append("never seen: " + ", ".join(missing))
        if self.unknown_events:
            out.append("undocumented events: " + ", ".join(self.unknown_events))
        return out
