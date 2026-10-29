"""Command line: ``python -m rltelemetry <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from . import ini as ini_mod
from .pipeline import Pipeline, listen
from .recorder import read_jsonl
from .report import Coverage
from .transport import DEFAULT_HOST, StatsClient

log = logging.getLogger("rltelemetry")


def _client(args, **kw) -> StatsClient:
    return StatsClient(args.host, args.port, websocket=args.ws, **kw)


def _extra_sinks(args) -> list:
    if getattr(args, "osa_print", False):
        from .sinks.osa import OsaSink, PrintPublisher
        return [OsaSink(PrintPublisher())]
    return []


def cmd_ini(args) -> int:
    files = ini_mod.find_ini_files([Path(p) for p in args.path])
    if args.enable:
        targets = files or [ini_mod.preferred_ini(d) for d in ini_mod.candidate_config_dirs() if d.is_dir()]
        if not targets:
            print("No Rocket League config folder found. Pass --path <folder or ini>.")
            return 1
        updates = {"PacketSendRate": str(args.rate), "Port": str(args.tcp_port), "WebPort": str(args.web_port)}
        for p in targets:
            f = ini_mod.IniFile.load(p) if p.exists() else ini_mod.IniFile(p, "", "utf-8", "\r\n")
            f.set_values(updates)
            bak = f.save()
            print(f"updated {p}" + (f" (backup {bak.name})" if bak else " (created)"))
        print("Restart Rocket League for the change to take effect.")
        files = targets
    if not files:
        print("No TAStatsAPI.ini / DefaultStatsAPI.ini found. Looked in:")
        for d in ini_mod.candidate_config_dirs():
            print(f"  {d}{'' if d.is_dir() else '  (missing)'}")
        return 1
    ok = True
    for p in files:
        values = ini_mod.IniFile.load(p).section()
        problems = ini_mod.check(values)
        ok &= not problems
        print(f"{p}\n  {values}")
        for prob in problems:
            print(f"  ! {prob}")
    return 0 if ok else 2


def cmd_listen(args) -> int:
    pipe = Pipeline(Path(args.out), driver=args.driver, channels=not args.no_channels,
                    extra_sinks=_extra_sinks(args))
    try:
        asyncio.run(listen(_client(args), pipe))
    except KeyboardInterrupt:
        pass
    finally:
        pipe.close()
        print("\n".join(pipe.coverage.lines()))
        print(f"logs in {Path(args.out).resolve()}")
    return 0


def cmd_replay(args) -> int:
    from .replay import list_replays, run_replay
    if args.list:
        for p in list_replays()[:30]:
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}  {p}")
        return 0
    if not args.replay:
        print("give a replay file/name, or --list")
        return 1
    pov = tuple(args.pov.split(":", 1)) if args.pov else None
    pipe = Pipeline(Path(args.out), driver=args.driver, channels=not args.no_channels,
                    extra_sinks=_extra_sinks(args), tag="replay")
    ok = False
    try:
        ok = asyncio.run(run_replay(_client(args), pipe, args.replay, speed=args.speed, pov=pov,
                                    hide_hud=args.hide_hud, seek_seconds=args.seek,
                                    idle_timeout=args.idle_timeout))
    except KeyboardInterrupt:
        pass
    finally:
        pipe.close()
        print("\n".join(pipe.coverage.lines()))
    return 0 if ok else 1


def cmd_probe(args) -> int:
    """Capture raw bytes to check framing, separators and field coverage."""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = open(out, "wb")
    head = bytearray()

    def tap(chunk: bytes) -> None:
        raw.write(chunk)
        if len(head) < args.show:
            head.extend(chunk[: args.show - len(head)])

    client = _client(args, raw_tap=tap)
    cov = Coverage()

    async def run() -> None:
        async def consume() -> None:
            async for rx, msg in client.events():
                cov.add(rx, msg)
        try:
            await asyncio.wait_for(consume(), timeout=args.seconds)
        except asyncio.TimeoutError:
            pass

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    raw.close()
    print(f"raw bytes saved to {out} ({out.stat().st_size} bytes)")
    print(f"first {len(head)} bytes: {bytes(head)!r}")
    st = client.decoder.stats
    print(f"objects: {st.objects}, invalid: {st.invalid_objects}, junk bytes: {st.skipped_bytes}")
    print(f"separators between objects: {st.describe()}")
    print("\n".join(cov.lines()))
    return 0 if st.objects else 1


def cmd_send(args) -> int:
    data = json.loads(args.data) if args.data else {}
    client = _client(args, reconnect=False)

    async def run() -> None:
        async def consume() -> None:
            async for _, msg in client.events():
                if msg.event != "UpdateState" or args.ticks:
                    print(json.dumps(msg.to_json())[:300])
        task = asyncio.create_task(consume())
        await asyncio.wait_for(client.connected.wait(), timeout=10)
        await client.send(args.command, data)
        await asyncio.sleep(args.watch)
        task.cancel()

    asyncio.run(run())
    return 0


def cmd_report(args) -> int:
    for f in args.files:
        cov = Coverage()
        for rx, msg in read_jsonl(Path(f)):
            cov.add(rx, msg)
        print(f"== {f}")
        print("\n".join(cov.lines()))
    return 0


def cmd_mock(args) -> int:
    from .mock_game import MockGame

    async def run() -> None:
        game = MockGame(rate=args.rate, framing=args.framing, spectator=args.spectator,
                        match_seconds=args.match_seconds, data_as_string=args.data_as_string,
                        loop=True)
        await game.serve_tcp(DEFAULT_HOST, args.tcp_port)
        log.info("mock TCP on %s:%d (framing=%s)", DEFAULT_HOST, args.tcp_port, args.framing)
        try:
            await game.serve_ws(DEFAULT_HOST, args.web_port)
            log.info("mock WebSocket on %s:%d", DEFAULT_HOST, args.web_port)
        except ImportError:
            log.info("websockets not installed; WebSocket mock disabled")
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def _stop_on_signals() -> None:
    """Treat SIGTERM / Ctrl+Break like Ctrl+C so files are closed and indexed."""
    def stop(*_):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rltelemetry", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def conn(p) -> None:
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, help="default 49123 (TCP) or 49124 (--ws)")
        p.add_argument("--ws", action="store_true", help="use the WebSocket port instead of TCP")

    def outputs(p) -> None:
        p.add_argument("--out", default="rl-logs", help="output folder (default ./rl-logs)")
        p.add_argument("--driver", help="player name to mirror into Driver.* channels")
        p.add_argument("--no-channels", action="store_true", help="only write raw/match JSONL")
        p.add_argument("--osa-print", action="store_true", help="attach the OSA sink with a print-only publisher")

    p = sub.add_parser("ini", help="find/check/enable the Stats API INI")
    p.add_argument("--enable", action="store_true", help="write the settings (backs up first)")
    p.add_argument("--rate", type=int, default=60)
    p.add_argument("--tcp-port", type=int, default=49123)
    p.add_argument("--web-port", type=int, default=49124)
    p.add_argument("--path", action="append", default=[], help="extra folder or ini file")
    p.set_defaults(fn=cmd_ini)

    p = sub.add_parser("listen", help="record live matches to disk")
    conn(p); outputs(p)
    p.set_defaults(fn=cmd_listen)

    p = sub.add_parser("replay", help="load a replay and record it as spectator")
    conn(p); outputs(p)
    p.add_argument("replay", nargs="?", help="replay file path or name")
    p.add_argument("--list", action="store_true", help="list saved replays")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--seek", type=float, help="start at this replay time (s)")
    p.add_argument("--pov", help="Focus:Perspective, e.g. Ball:AutoCam or 1:HardAttach")
    p.add_argument("--hide-hud", action="store_true")
    p.add_argument("--idle-timeout", type=float, default=5.0,
                   help="stop when the replay frame hasn't advanced for this long")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("probe", help="dump raw bytes to check TCP framing and fields")
    conn(p)
    p.add_argument("--seconds", type=float, default=15)
    p.add_argument("--out", default="rl-logs/probe.bin")
    p.add_argument("--show", type=int, default=400, help="bytes to print")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("send", help="send one command and print what comes back")
    conn(p)
    p.add_argument("command")
    p.add_argument("data", nargs="?", help='JSON, e.g. \'{"bPaused": true}\'')
    p.add_argument("--watch", type=float, default=3.0)
    p.add_argument("--ticks", action="store_true", help="also print UpdateState")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("report", help="field coverage of recorded JSONL files")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("mock", help="run a fake game server for testing")
    p.add_argument("--tcp-port", type=int, default=49123)
    p.add_argument("--web-port", type=int, default=49124)
    p.add_argument("--rate", type=int, default=60)
    p.add_argument("--framing", default="newline", choices=["newline", "crlf", "none", "nul"])
    p.add_argument("--spectator", action="store_true")
    p.add_argument("--match-seconds", type=float, default=20)
    p.add_argument("--data-as-string", action="store_true")
    p.set_defaults(fn=cmd_mock)

    args = ap.parse_args(argv)
    _stop_on_signals()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
