# ADS Offload Disabler

NOT APPROVED BY MOTION APPLIED

Atlas Data Server (ADS) has a "Disable Next Offload" checkbox on its main
window that resets to unchecked every time the app opens, and unchecks
itself again after each connection/offload completes. There's no persisted
setting for it, so normally you have to tick it by hand every time.

This launches ADS and ticks that checkbox automatically, then keeps watching
for as long as ADS stays open, re-ticking it any time it flips back off.

## Files

- `Start-AtlasDataServer-OffloadDisabled.ps1` — launches
  `AtlasDataServer.exe`, waits for its window, finds the "Disable Next
  Offload" checkbox via Win32 window messages (`BM_GETCHECK` / `BM_CLICK`,
  sent with a timeout so a busy ADS can't hang it), and checks it every
  250 ms for the lifetime of the process. Also shows the tray menu below.
- `Start-ADS-Silent.vbs` — invisible launcher wrapper (runs the PowerShell
  script with no console window; PowerShell's own `-WindowStyle Hidden`
  isn't reliably honored under Windows Terminal as the default console
  host). It looks for the `.ps1` in its own folder.

## Setup

1. Keep both files in the same folder.
2. Edit `$LauncherPath` in `Start-AtlasDataServer-OffloadDisabled.ps1` if
   your ADS install isn't at the default path
   (`C:\Program Files (x86)\McLaren Applied Technologies\ATLAS Data Server\Bin\AtlasDataServer.exe`).
3. Create a shortcut with:
   - Target: `wscript.exe "<path to>\Start-ADS-Silent.vbs"`
   - Working directory: the ADS `Bin` folder
   - Icon: `AtlasDataServer.exe,0`
4. Use that shortcut instead of the normal ADS icon.

## Allowing an offload

While ADS is open, an ADS icon sits in the system tray (Windows 11 may tuck
it under the `^` overflow arrow; drag it onto the taskbar to keep it
visible). Right-click it and untick **Keep next offload disabled** to let
an offload happen: the "Disable Next Offload" box in ADS is unticked and
left alone, and the tray icon changes to a warning sign. Tick it again to
go back to disabling offloads.

Every ADS launch starts with offloads disabled.

## When something goes wrong

If ADS doesn't open within 90 seconds, the checkbox can't be found (e.g. an
ADS update renamed it), or anything else fails, a warning popup appears and
the details are written to `run.log` next to the script. `run.log` also
records each time the box is re-ticked.

Only one watcher runs at a time, so double-clicking the shortcut again while
ADS is starting won't open a second copy.
