@echo off
rem Records every Rocket League match to .\rl-logs. Close this window or press Ctrl+C to stop.
rem Change "Son" to the in-game name to get Driver.* channels for that player.
cd /d "%~dp0"
py -m rltelemetry listen --driver "Son" --out rl-logs
pause
