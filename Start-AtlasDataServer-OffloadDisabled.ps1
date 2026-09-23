<#
Launches Atlas Data Server and automatically ticks the "Disable Next Offload"
checkbox once its main window appears. ADS resets that checkbox to unchecked
on every launch and offers no persisted setting for it, so this clicks it
programmatically instead of you doing it by hand each time.
#>

$ErrorActionPreference = 'Stop'

$LauncherPath = 'C:\Program Files (x86)\McLaren Applied Technologies\ATLAS Data Server\Bin\AtlasDataServer.exe'
$TimeoutSeconds = 90
$PollIntervalMs = 250
$LogPath = Join-Path $PSScriptRoot 'run.log'

$source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace AdsAuto
{
    public static class OffloadDisabler
    {
        private const string CheckboxText = "Disable Next Offload";
        private const int BM_GETCHECK = 0x00F0;
        private const int BM_CLICK = 0x00F5;
        private const uint SMTO_ABORTIFHUNG = 0x0002;
        private const uint SendTimeoutMs = 2000;

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessageTimeout(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam,
            uint flags, uint timeoutMs, out IntPtr result);

        private static string logPath;

        public static void Run(string launcherPath, int timeoutSeconds, int pollIntervalMs, string logFile)
        {
            logPath = logFile;
            string processName = Path.GetFileNameWithoutExtension(launcherPath);

            // One watcher at a time: a second double-click during startup would otherwise launch ADS twice.
            bool isOnlyWatcher;
            using (new Mutex(true, @"Local\AdsOffloadDisabler_" + processName, out isOnlyWatcher))
            {
                if (!isOnlyWatcher)
                {
                    Log("Another watcher is already running; exiting.");
                    return;
                }
                Watch(launcherPath, processName, timeoutSeconds, pollIntervalMs);
            }
        }

        private static void Watch(string launcherPath, string processName, int timeoutSeconds, int pollIntervalMs)
        {
            DateTime deadline = DateTime.Now.AddSeconds(timeoutSeconds);

            Process ads = FindProcessWithWindow(processName);
            if (ads == null)
            {
                Log("Starting " + launcherPath);
                using (Process.Start(new ProcessStartInfo(launcherPath)
                {
                    UseShellExecute = true,
                    WorkingDirectory = Path.GetDirectoryName(launcherPath)
                })) { }
            }
            while (ads == null && DateTime.Now < deadline)
            {
                Thread.Sleep(500);
                ads = FindProcessWithWindow(processName);
            }
            if (ads == null)
                throw new InvalidOperationException("ADS didn't open a window within " + timeoutSeconds + " seconds.");

            using (ads)
            {
                Log("Watching ADS (PID " + ads.Id + ").");

                IntPtr checkbox = IntPtr.Zero;
                while (checkbox == IntPtr.Zero && !ads.HasExited && DateTime.Now < deadline)
                {
                    checkbox = FindCheckbox(ads);
                    if (checkbox == IntPtr.Zero) Thread.Sleep(500);
                }
                if (checkbox == IntPtr.Zero && !ads.HasExited)
                    throw new InvalidOperationException("Couldn't find the '" + CheckboxText +
                        "' checkbox in ADS - its label may have changed in an ADS update.");

                while (!ads.HasExited)
                {
                    if (!IsOffloadCheckbox(checkbox, ads.Id))
                        checkbox = FindCheckbox(ads);

                    // Re-validate after reading: BM_GETCHECK to a window destroyed mid-send reads back as 0.
                    if (checkbox != IntPtr.Zero && GetCheckState(checkbox) == 0 && IsOffloadCheckbox(checkbox, ads.Id))
                    {
                        if (Click(checkbox)) Log("Ticked '" + CheckboxText + "'.");
                        else Log("ADS didn't respond to the tick; will retry.");
                    }
                    Thread.Sleep(pollIntervalMs);
                }
                Log("ADS closed.");
            }
        }

        private static Process FindProcessWithWindow(string processName)
        {
            Process match = null;
            foreach (Process p in Process.GetProcessesByName(processName))
            {
                if (match == null && CurrentMainWindow(p) != IntPtr.Zero) match = p;
                else p.Dispose();
            }
            return match;
        }

        private static IntPtr CurrentMainWindow(Process p)
        {
            p.Refresh();
            try { return p.MainWindowHandle; }
            catch (InvalidOperationException) { return IntPtr.Zero; } // process exited mid-query
        }

        private static IntPtr FindCheckbox(Process ads)
        {
            IntPtr mainWindow = CurrentMainWindow(ads);
            IntPtr found = IntPtr.Zero;
            if (mainWindow == IntPtr.Zero) return found;

            int pid = ads.Id;
            EnumChildWindows(mainWindow, (hWnd, lParam) =>
            {
                if (!IsOffloadCheckbox(hWnd, pid)) return true;
                found = hWnd;
                return false;
            }, IntPtr.Zero);
            return found;
        }

        // Re-validated every poll because a destroyed HWND's value can be reused by an unrelated window.
        private static bool IsOffloadCheckbox(IntPtr hWnd, int processId)
        {
            uint owner;
            if (GetWindowThreadProcessId(hWnd, out owner) == 0 || owner != processId) return false;

            var text = new StringBuilder(64);
            GetWindowText(hWnd, text, text.Capacity);
            return text.ToString() == CheckboxText;
        }

        private static int? GetCheckState(IntPtr checkbox)
        {
            IntPtr state;
            if (SendMessageTimeout(checkbox, BM_GETCHECK, IntPtr.Zero, IntPtr.Zero,
                    SMTO_ABORTIFHUNG, SendTimeoutMs, out state) == IntPtr.Zero)
                return null;
            return state.ToInt32();
        }

        private static bool Click(IntPtr checkbox)
        {
            IntPtr ignored;
            return SendMessageTimeout(checkbox, BM_CLICK, IntPtr.Zero, IntPtr.Zero,
                SMTO_ABORTIFHUNG, SendTimeoutMs, out ignored) != IntPtr.Zero;
        }

        private static void Log(string message)
        {
            File.AppendAllText(logPath, DateTime.Now.ToString("o") + "  " + message + Environment.NewLine);
        }
    }
}
'@

try {
    $refs = [System.Diagnostics.Process], [string], [System.Threading.Mutex], [System.IO.File] |
        ForEach-Object { $_.Assembly.Location } | Select-Object -Unique
    Add-Type -TypeDefinition $source -Language CSharp -ReferencedAssemblies $refs
    [AdsAuto.OffloadDisabler]::Run($LauncherPath, $TimeoutSeconds, $PollIntervalMs, $LogPath)
}
catch {
    $message = $_.Exception.GetBaseException().Message
    [System.IO.File]::AppendAllText($LogPath, "$(Get-Date -Format o)  ERROR: $message`r`n")
    $warningTopmost = 0x30 -bor 0x1000
    $null = (New-Object -ComObject WScript.Shell).Popup(
        "Stopped watching ADS:`n`n$message`n`nTick 'Disable Next Offload' manually for now. Details: $LogPath",
        0, 'ADS Offload Disabler', $warningTopmost)
}
