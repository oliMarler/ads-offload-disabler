"""Stage 1: lossless JSONL logging.

Layout under the output directory::

    raw/session-<time>.jsonl   every message received, in or out of a match
    matches/<MatchGuid>.jsonl  one file per match (``local-<time>`` offline)
    matches/index.jsonl        one summary line per finished match

Each line is the original message plus the receive time:
``{"rx_ns": 1730000000000000000, "Event": "...", "Data": {...}}``.
Files are line-buffered so a crash loses at most the line being written.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO, Any

from .matches import MatchInfo
from .protocol import Message


def _line(rx_ns: int, msg: Message, **extra: Any) -> str:
    rec = {"rx_ns": rx_ns, **extra, "Event": msg.event, "Data": msg.data}
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n"


class RawLog:
    def __init__(self, out_dir: Path) -> None:
        (out_dir / "raw").mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = out_dir / "raw" / f"session-{stamp}.jsonl"
        self._f = open(self.path, "a", encoding="utf-8", buffering=1)

    def write(self, rx_ns: int, msg: Message, match: MatchInfo | None) -> None:
        self._f.write(_line(rx_ns, msg, match=match.key if match else None))

    def close(self) -> None:
        self._f.close()


class MatchRecorder:
    """MatchListener that writes one JSONL file per match."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir / "matches"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, IO[str]] = {}
        self.closed_paths: list[Path] = []

    def path_for(self, match: MatchInfo) -> Path:
        suffix = f".{match.tag}" if match.tag else ""
        return self.dir / f"{match.key}{suffix}.jsonl"

    def match_opened(self, match: MatchInfo) -> None:
        # Append: reconnecting mid-match continues the same file.
        self._files[match.key] = open(self.path_for(match), "a", encoding="utf-8", buffering=1)

    def match_rekeyed(self, match: MatchInfo, old_key: str) -> None:
        f = self._files.pop(old_key)
        old_path = Path(f.name)
        f.close()
        new_path = self.path_for(match)
        if new_path.exists():
            with open(new_path, "a", encoding="utf-8") as dst:
                dst.write(old_path.read_text(encoding="utf-8"))
            old_path.unlink()
        else:
            os.replace(old_path, new_path)
        self._files[match.key] = open(new_path, "a", encoding="utf-8", buffering=1)

    def match_message(self, match: MatchInfo, rx_ns: int, msg: Message) -> None:
        self._files[match.key].write(_line(rx_ns, msg))

    def match_closed(self, match: MatchInfo) -> None:
        f = self._files.pop(match.key, None)
        if f:
            f.close()
            self.closed_paths.append(Path(f.name))
        with open(self.dir / "index.jsonl", "a", encoding="utf-8") as idx:
            idx.write(json.dumps({**match.summary(), "file": self.path_for(match).name},
                                 ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    """Yield ``(rx_ns, Message)`` from a recorder file (match or raw)."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "Event" in rec:
                yield int(rec.get("rx_ns", 0)), Message(rec["Event"], rec.get("Data") or {})
