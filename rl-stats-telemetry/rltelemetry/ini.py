"""Find and edit the Stats API INI (``[TAGame.MatchStatsExporter_TA]``).

The official docs put it in ``<Install Dir>\\TAGame\\Config\\``; community
tools use ``Documents\\My Games\\Rocket League\\TAGame\\Config\\``. Both are
checked. Within a folder ``TAStatsAPI.ini`` is preferred and
``DefaultStatsAPI.ini`` is the fallback. The game reads it at launch only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

SECTION = "TAGame.MatchStatsExporter_TA"
FILENAMES = ("TAStatsAPI.ini", "DefaultStatsAPI.ini")
MAX_RATE = 120


def candidate_config_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for docs in (home / "Documents", home / "OneDrive" / "Documents",
                 Path(os.environ.get("OneDrive", home / "OneDrive")) / "Documents"):
        dirs.append(docs / "My Games" / "Rocket League" / "TAGame" / "Config")
    for install in _install_dirs():
        dirs.append(install / "TAGame" / "Config")
    seen, out = set(), []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _install_dirs() -> list[Path]:
    found: list[Path] = []
    # Epic: launcher manifests record InstallLocation.
    manifests = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    for item in manifests.glob("*.item") if manifests.is_dir() else []:
        try:
            m = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if "rocket league" in str(m.get("DisplayName", "")).lower() and m.get("InstallLocation"):
            found.append(Path(m["InstallLocation"]))
    # Steam: every library listed in libraryfolders.vdf.
    for steam in (Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")):
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        libs = [steam]
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        for lib in libs:
            found.append(lib / "steamapps" / "common" / "rocketleague")
    found += [Path(r"C:\Program Files\Epic Games\rocketleague"),
              Path(r"C:\Program Files (x86)\Steam\steamapps\common\rocketleague")]
    return found


@dataclass
class IniFile:
    path: Path
    text: str
    encoding: str
    newline: str

    @classmethod
    def load(cls, path: Path) -> "IniFile":
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            enc = "utf-16"
        elif raw.startswith(b"\xef\xbb\xbf"):
            enc = "utf-8-sig"
        else:
            enc = "utf-8"
        text = raw.decode(enc, errors="replace")
        return cls(path, text, enc, "\r\n" if "\r\n" in text else "\n")

    def section(self) -> dict[str, str] | None:
        values: dict[str, str] | None = None
        in_sec = False
        for line in self.text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_sec = s[1:-1].strip().lower() == SECTION.lower()
                if in_sec and values is None:
                    values = {}
                continue
            if in_sec and values is not None and "=" in s and not s.startswith((";", "#")):
                k, v = s.split("=", 1)
                values[k.strip()] = v.strip()
        return values

    def set_values(self, updates: dict[str, str]) -> None:
        lines = self.text.splitlines()
        out: list[str] = []
        in_sec = False
        found_sec = False
        pending = dict(updates)

        def flush_pending() -> None:
            # Insert missing keys before trailing blank lines of the section.
            tail = []
            while out and not out[-1].strip():
                tail.append(out.pop())
            out.extend(f"{k}={v}" for k, v in pending.items())
            out.extend(tail)
            pending.clear()

        for line in lines:
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if in_sec:
                    flush_pending()
                in_sec = s[1:-1].strip().lower() == SECTION.lower()
                found_sec = found_sec or in_sec
                out.append(line)
                continue
            if in_sec and "=" in s and not s.startswith((";", "#")):
                key = s.split("=", 1)[0].strip()
                match = next((k for k in pending if k.lower() == key.lower()), None)
                if match:
                    out.append(f"{key}={pending.pop(match)}")
                    continue
            out.append(line)
        if in_sec:
            flush_pending()
        if not found_sec:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{SECTION}]")
            out.extend(f"{k}={v}" for k, v in updates.items())
        self.text = self.newline.join(out) + self.newline

    def save(self, backup: bool = True) -> Path | None:
        bak = None
        if backup and self.path.exists():
            bak = self.path.with_name(self.path.name + time.strftime(".%Y%m%d-%H%M%S.bak"))
            shutil.copy2(self.path, bak)
        self.path.write_bytes(self.text.replace("\r\n", "\n").replace("\n", self.newline).encode(self.encoding))
        return bak


def find_ini_files(extra: list[Path] | None = None) -> list[Path]:
    out = []
    for d in (extra or []) + candidate_config_dirs():
        if d.is_file():
            out.append(d)
            continue
        for name in FILENAMES:
            p = d / name
            if p.is_file():
                out.append(p)
    return out


def preferred_ini(folder: Path) -> Path:
    """TAStatsAPI.ini if present, else DefaultStatsAPI.ini, else create TAStatsAPI.ini."""
    for name in FILENAMES:
        if (folder / name).is_file():
            return folder / name
    return folder / FILENAMES[0]


def check(values: dict[str, str] | None) -> list[str]:
    """Return problems with a section's settings (empty list = good)."""
    if values is None:
        return [f"no [{SECTION}] section"]
    problems = []
    get = {k.lower(): v for k, v in values.items()}

    def num(key: str, default: int) -> int | None:
        try:
            return int(float(get.get(key.lower(), default)))
        except ValueError:
            problems.append(f"{key} is not a number: {get.get(key.lower())!r}")
            return None

    rate, port, web = num("PacketSendRate", 0), num("Port", 49123), num("WebPort", 49124)
    if rate is not None and rate <= 0:
        problems.append("PacketSendRate is 0: UpdateState ticks are disabled")
    if rate is not None and rate > MAX_RATE:
        problems.append(f"PacketSendRate {rate} > {MAX_RATE} (game caps it)")
    if port == 0 and web == 0:
        problems.append("Port and WebPort are both 0: nothing will listen")
    if port and web and port == web:
        problems.append("Port and WebPort must differ")
    return problems
