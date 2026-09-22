# ADS Offload Disabler

Atlas Data Server (ADS) has a "Disable Next Offload" checkbox on its main
window that resets to unchecked every time the app opens, and unchecks
itself again after each connection/offload completes. There's no persisted
setting for it, so normally you have to tick it by hand every time.

This launches ADS and ticks that checkbox automatically, then keeps watching
for as long as ADS stays open, re-ticking it any time it flips back off.

## Files

- `Start-AtlasDataServer-OffloadDisabled.ps1` — launches
  `AtlasDataServer.exe`, waits for its window, finds the "Disable Next
  Offload" checkbox via Win32 window messages (`BM_GETCHECK` / `BM_CLICK`),
  and watches it for the lifetime of the process.
- `Start-ADS-Silent.vbs` — invisible launcher wrapper (runs the PowerShell
  script with no console window; PowerShell's own `-WindowStyle Hidden`
  isn't reliably honored under Windows Terminal as the default console
  host).

## Setup

1. Edit `$LauncherPath` in `Start-AtlasDataServer-OffloadDisabled.ps1` if
   your ADS install isn't at the default path
   (`C:\Program Files (x86)\McLaren Applied Technologies\ATLAS Data Server\Bin\AtlasDataServer.exe`).
2. Create a shortcut with:
   - Target: `wscript.exe "<path to>\Start-ADS-Silent.vbs"`
   - Working directory: the ADS `Bin` folder
   - Icon: `AtlasDataServer.exe,0`
3. Use that shortcut instead of the normal ADS icon.
