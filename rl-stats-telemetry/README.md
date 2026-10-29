# rltelemetry: Rocket League Stats API → match logs → ATLAS

A listener for Rocket League's official [Stats API](https://rocketleague.com/developer/stats-api)
that:

1. records every message to disk, one JSONL file per match keyed by `MatchGuid`;
2. maps the stream to fixed telemetry channels and events, behind a sink interface
   that an ATLAS / Open Streaming Architecture publisher plugs into;
3. drives replay playback (`LoadReplay`) so the spectator-only car channels
   can be captured for a match that has already been played.

Python 3.10+, standard library only. `websockets` is optional and only needed
for the WebSocket port (`pip install websockets`).

## Why Python

The OSA Stream API is gRPC, so the publisher can be written in any language.
Motion Applied's own client libraries are C#/.NET (NuGet), but this side of
the problem is mostly JSON wrangling and file I/O, which is quickest in Python.
Stage 1 doesn't depend on ATLAS at all. For stage 2 there are two routes:

- **Python all the way:** generate Python stubs from the Stream API protos
  your PC metrics bridge already uses (`python -m grpc_tools.protoc ...`) and
  implement `OsaPublisher` (see `rltelemetry/sinks/osa.py`).
- **Reuse a C# bridge:** have it read `channels/<match>.jsonl` (or a
  socket/pipe sink you add) and publish through its existing code. The
  channel catalogue is written as the first line of each file, so the bridge
  can define parameters before any data arrives.

## Setup (Windows PC)

```bat
cd rl-stats-telemetry
py -m pip install -e .[ws]        :: or just run "py -m rltelemetry" from this folder
py -m rltelemetry ini              :: find and check the INI file(s)
py -m rltelemetry ini --enable --rate 60
```

`ini` looks in both documented places:

- `Documents\My Games\Rocket League\TAGame\Config\` (also OneDrive's `Documents`)
- `<Install Dir>\TAGame\Config\`. Epic installs are found from the launcher
  manifests, Steam installs from every library in `libraryfolders.vdf`.

In each folder it uses `TAStatsAPI.ini` and falls back to
`DefaultStatsAPI.ini`. `--enable` writes `PacketSendRate`, `Port` and `WebPort`
into `[TAGame.MatchStatsExporter_TA]` and keeps the rest of the file, its
encoding and its line endings. It makes a timestamped `.bak` first. If neither
file exists it creates `TAStatsAPI.ini` in every config folder it found. Use
`--path <folder or file>` to point it at a specific location. **Restart
the game afterwards.** Game updates may reset these files, so run `ini` again
after a patch.

## First run: check the things the docs don't pin down

With the game running and a match (or a replay) in progress:

```bat
py -m rltelemetry probe --seconds 15
py -m rltelemetry probe --seconds 15 --ws
```

`probe` saves the raw bytes to `rl-logs/probe.bin` and prints:

- the first few hundred bytes as they came off the wire;
- the **separators between JSON objects** (`'\n' x812`, `'' x3`, `'\x00'`...),
  which answers the TCP framing question. The decoder finds object boundaries
  itself, so recording works either way;
- the measured tick rate against `PacketSendRate`;
- how many ticks carry `MatchGuid`, `bReplay` and `Frame`/`Elapsed`;
- the percentage of player-ticks carrying each **spectator-only car field**.
  Expect 0% while your son is driving and ~100% while spectating or playing a
  replay. That confirms (or refutes) the replay workaround.

`report <file.jsonl>` prints the same summary for any recorded file.

Commands: the docs don't say which port accepts them. `replay` and `send`
use the connection you pick, so if `send` over TCP gets no reaction, add
`--ws`:

```bat
py -m rltelemetry send SetMatchPaused "{\"bPaused\": true}" --ws
```

## Recording

```bat
py -m rltelemetry listen --driver "SonsInGameName"
```

(or double-click `record.bat` after setting the name in it). It runs until
Ctrl+C and reconnects on its own when the game restarts or you sit in menus.
Output in `rl-logs/`:

| Path | Contents |
|---|---|
| `raw/session-<time>.jsonl` | every message received, including menus, tagged with the match key |
| `matches/<MatchGuid>.jsonl` | one file per match, the original `{"Event","Data"}` plus `rx_ns` |
| `matches/index.jsonl` | one summary per finished match: arena, playlist, teams, score, winner, per-player stats |
| `channels/<MatchGuid>.jsonl` | the flattened stream that stage 2 publishes (see below) |

Match splitting: a new `MatchGuid` starts a new file and `MatchDestroyed`
closes it. Offline matches have no guid and get `local-<date>-<time>`. If the
guid only arrives after `MatchCreated`, the provisional file is renamed to the
guid. A reconnect mid-match appends to the same file.

Every line carries `rx_ns`, the receive time in Unix nanoseconds. The API's
own clock is only `TimeSeconds` (remaining) in live play, plus
`Frame`/`Elapsed` in replays.

## Channels (stage 2 contract)

`rltelemetry/channels.py` defines everything a publisher sees:

- **Game:** `Game.TimeRemaining`, `Game.Overtime`, `Game.ScoreBlue/Orange`,
  `Game.Replay`, `Game.HasWinner`, `Game.Frame`, `Game.Elapsed`, `Ball.Speed`
  (uu/s), `Ball.SpeedKph`, `Ball.LastTouchTeam`
- **Per player**, in fixed slots `Blue1..4` / `Orange1..4` so parameter names
  are the same in every match: `Score Goals Shots Assists Saves Touches
  CarTouches Demos Present`, plus the spectator-only `HasCar Speed SpeedKph
  Boost Boosting OnGround OnWall Powersliding Demolished Supersonic`. Slots
  go in order of first appearance. A `Roster` event records which player is
  in which slot.
- **`Driver.*`**: the same set mirrored for the `--driver` player, so his
  data lives under one name whatever team or slot he lands in.
- **Events:** every non-tick message becomes an `EventMark` with a label
  (`Goal Son (assist Amy)`, `Demolish Bot -> Son`...) and all numeric fields
  flattened (`ImpactLocation.X`, `Ball.PostHitSpeed`, `BoostAmount`...). That
  is how positions get into ATLAS, because the tick carries none.

Bools are 0/1. Speeds are uu/s with km/h companions (`× 0.036`). Values
missing from a tick are left out, never zero-filled, so a channel that's absent
in live play just has no data.

A sink implements `on_match_start / on_match_rekeyed / on_roster / on_sample /
on_event / on_match_end`. `ChannelJsonlSink` is the reference. `OsaSink` adds
per-channel batching (100 ms default) and calls an `OsaPublisher` that
you write with your Stream API stubs. The mapping is in its docstring:
session per match, parameters from the catalogue, timestamped data,
events, end session. Try the flow with `--osa-print`, which logs the calls
instead of sending them.

## Replay mode (spectator-only car channels)

The game must be running and sitting in the main menu.

```bat
py -m rltelemetry replay --list                     :: saved replays, newest first
py -m rltelemetry replay "C:\...\Demos\ABC123.replay" --driver "SonsInGameName"
py -m rltelemetry replay ABC123 --speed 2 --pov Ball:AutoCam --hide-hud
```

It sends `LoadReplay` (`Path` for an existing file, otherwise `FileName`),
waits for replay ticks, then applies `--seek`, `--speed`, `--hide-hud` and
`--pov`. It records until the frame stops advancing (`--idle-timeout`, 5 s)
or `MatchDestroyed`. Output files get a `.replay` suffix so they never mix
with the live recording of the same `MatchGuid`. At the end it prints the
coverage report, so you can see straight away whether the car fields came
through.

To get a replay: save it from the post-match screen. `ReplayCreated` events
are logged with the file name.

## Testing without the game

```bat
py -m rltelemetry mock --spectator             :: fake game on 49123/49124
py -m rltelemetry listen                       :: in another window
py -m unittest discover -s tests
```

The mock plays a scripted 2v2 (goal, demo, hits, boost pickups). Its
`--framing newline|crlf|none|nul` and `--data-as-string` options cover the
wire-format variants seen in the wild. It answers `LoadReplay` with a
replay-style match that includes the car fields.

## Known unknowns (verify on the real client)

- TCP framing: handled whatever it is, but `probe` will tell you which.
- Which port accepts commands: try TCP first, then `--ws`.
- Whether `MatchGuid` is on `MatchCreated`/`MatchInitialized` or only on
  later events: handled either way through the provisional key.
- Whether replays carry the original `MatchGuid`: handled through the
  `.replay` suffix.
- How the end of a replay shows up: the idle timeout covers it. Raise it if
  you pause the replay by hand.
