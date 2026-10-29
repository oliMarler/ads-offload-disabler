"""Channel sinks. ``ChannelJsonlSink`` is the reference implementation;
``osa.OsaSink`` is the template for publishing into ATLAS."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from ..channels import ChannelDef, EventMark, Sample
from ..matches import MatchInfo


class ChannelJsonlSink:
    """Writes the flattened channel stream to ``channels/<key>.jsonl``.

    Useful for checking exactly what stage 2 will publish, and as an offline
    hand-off format for another process. Line types:
    ``{"type":"channels","defs":[...]}``, ``{"type":"roster",...}``,
    ``{"type":"sample","ts_ns":..,"v":{...}}``, ``{"type":"event",...}``.
    """

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir / "channels"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, IO[str]] = {}

    def _path(self, match: MatchInfo) -> Path:
        suffix = f".{match.tag}" if match.tag else ""
        return self.dir / f"{match.key}{suffix}.jsonl"

    def _write(self, match: MatchInfo, rec: dict[str, Any]) -> None:
        self._files[match.key].write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")

    def on_match_start(self, match: MatchInfo, channels: list[ChannelDef]) -> None:
        self._files[match.key] = open(self._path(match), "a", encoding="utf-8", buffering=1)
        self._write(match, {"type": "channels", "defs": [asdict(c) for c in channels]})

    def on_match_rekeyed(self, match: MatchInfo, old_key: str) -> None:
        f = self._files.pop(old_key)
        f.close()
        new = self._path(match)
        old = Path(f.name)
        if new.exists():
            with open(new, "a", encoding="utf-8") as dst:
                dst.write(old.read_text(encoding="utf-8"))
            old.unlink()
        else:
            old.replace(new)
        self._files[match.key] = open(new, "a", encoding="utf-8", buffering=1)

    def on_roster(self, match: MatchInfo, ts_ns: int, roster: dict[str, dict[str, Any]]) -> None:
        self._write(match, {"type": "roster", "ts_ns": ts_ns, "slots": roster})

    def on_sample(self, match: MatchInfo, sample: Sample) -> None:
        self._write(match, {"type": "sample", "ts_ns": sample.ts_ns, "v": sample.values})

    def on_event(self, match: MatchInfo, mark: EventMark) -> None:
        self._write(match, {"type": "event", "ts_ns": mark.ts_ns, "name": mark.name,
                            "text": mark.text, "fields": mark.fields})

    def on_match_end(self, match: MatchInfo) -> None:
        f = self._files.pop(match.key, None)
        if f:
            f.close()
