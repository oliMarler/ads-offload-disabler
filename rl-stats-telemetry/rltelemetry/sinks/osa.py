"""Template for publishing the channel stream into ATLAS via the Open
Streaming Architecture (Stream API, gRPC).

The Stream API protos aren't publicly downloadable, so this module stops at
the boundary: ``OsaSink`` does the RL-specific work (session lifecycle,
parameter catalogue, per-channel batching, event marks) and hands it to an
``OsaPublisher`` that you implement with the stubs from your existing PC
metrics bridge. The intended mapping:

  match opened     -> new connection/session; session key = match.key,
                      details = arena, playlist, teams, MatchGuid
  channel catalogue-> parameter definitions (name, unit, description); bools
                      and counters can go in as plain numeric parameters
  on_roster        -> session details / an event, so slot names (Blue1..)
                      can be resolved to player names in ATLAS
  batches          -> timestamped/row data packets per parameter
  EventMark        -> event definitions + event packets (goal, hit, demo...)
  match closed     -> end session

Timestamps are wall-clock ns at receive (``rx_ns``). In replay mode,
``Game.Elapsed`` is also published as a channel so laps of a replay can be
aligned to replay time if that matters more than wall time.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Protocol

from ..channels import ChannelDef, EventMark, Sample
from ..matches import MatchInfo


class OsaPublisher(Protocol):
    def start_session(self, key: str, details: dict[str, Any], channels: list[ChannelDef]) -> None: ...
    def rename_session(self, old_key: str, new_key: str) -> None: ...
    def write_channels(self, key: str, batch: dict[str, tuple[list[int], list[float]]]) -> None: ...
    def write_event(self, key: str, ts_ns: int, name: str, text: str, fields: dict[str, float]) -> None: ...
    def end_session(self, key: str, details: dict[str, Any]) -> None: ...


class OsaSink:
    """Channel sink that batches samples and drives an ``OsaPublisher``."""

    def __init__(self, publisher: OsaPublisher, batch_ms: int = 100) -> None:
        self.pub = publisher
        self.batch_ns = batch_ms * 1_000_000
        self._batch: dict[str, tuple[list[int], list[float]]] = defaultdict(lambda: ([], []))
        self._batch_start = 0

    def _flush(self, match: MatchInfo) -> None:
        if self._batch:
            self.pub.write_channels(match.key, dict(self._batch))
            self._batch.clear()

    def on_match_start(self, match: MatchInfo, channels: list[ChannelDef]) -> None:
        self.pub.start_session(match.key, match.summary(), channels)

    def on_match_rekeyed(self, match: MatchInfo, old_key: str) -> None:
        self.pub.rename_session(old_key, match.key)

    def on_roster(self, match: MatchInfo, ts_ns: int, roster: dict[str, dict[str, Any]]) -> None:
        text = ", ".join(f"{slot}={p.get('Name')}" for slot, p in sorted(roster.items()))
        self.pub.write_event(match.key, ts_ns, "Roster", text, {})

    def on_sample(self, match: MatchInfo, sample: Sample) -> None:
        if not self._batch:
            self._batch_start = sample.ts_ns
        for name, value in sample.values.items():
            ts, vals = self._batch[name]
            ts.append(sample.ts_ns)
            vals.append(value)
        if sample.ts_ns - self._batch_start >= self.batch_ns:
            self._flush(match)

    def on_event(self, match: MatchInfo, mark: EventMark) -> None:
        self._flush(match)  # keep channel data ahead of the event in time order
        self.pub.write_event(match.key, mark.ts_ns, mark.name, mark.text, mark.fields)

    def on_match_end(self, match: MatchInfo) -> None:
        self._flush(match)
        self.pub.end_session(match.key, match.summary())


class PrintPublisher:
    """Stand-in publisher that logs what would be sent; swap for the gRPC one."""

    def start_session(self, key, details, channels):
        print(f"[osa] start session {key}: {len(channels)} parameters, arena={details.get('Arena')}")

    def rename_session(self, old_key, new_key):
        print(f"[osa] rename session {old_key} -> {new_key}")

    def write_channels(self, key, batch):
        n = sum(len(ts) for ts, _ in batch.values())
        print(f"[osa] {key}: {len(batch)} channels, {n} points")

    def write_event(self, key, ts_ns, name, text, fields):
        print(f"[osa] {key}: event {name}: {text}")

    def end_session(self, key, details):
        print(f"[osa] end session {key}: winner team {details.get('WinnerTeamNum')}")
