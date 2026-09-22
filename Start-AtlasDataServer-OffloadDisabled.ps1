<#
Launches Atlas Data Server and automatically ticks the "Disable Next Offload"
checkbox once its main window appears. ADS resets that checkbox to unchecked
on every launch and offers no persisted setting for it, so this clicks it
programmatically instead of you doing it by hand each time.
#>

$LauncherPath = 'C:\Program Files (x86)\McLaren Applied Technologies\ATLAS Data Server\Bin\AtlasDataServer.exe'
$TimeoutSeconds = 90

$source = @'
using System;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

namespace AdsAuto
{
    public static class OffloadDisabler
    {
        [DllImport("user32.dll")]
        private static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll")]
        private static extern int SendMessage(IntPtr hWnd, int Msg, int wParam, int lParam);

        private const int BM_GETCHECK = 0x00F0;
        private const int BM_CLICK = 0x00F5;

        private static IntPtr FindCheckbox(IntPtr mainWindow)
        {
            IntPtr checkbox = IntPtr.Zero;
            EnumChildWindows(mainWindow, (hWnd, lParam) =>
            {
                var sb = new StringBuilder(256);
                GetWindowText(hWnd, sb, 256);
                if (sb.ToString() == "Disable Next Offload")
                {
                    checkbox = hWnd;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return checkbox;
        }

        // Launches ADS (if needed), ticks "Disable Next Offload" once its window appears,
        // then keeps watching for as long as ADS stays open: every pollIntervalMs it
        // re-checks the box, so it flips it back on any time ADS auto-unchecks it
        // after processing a connection/offload.
        public static string Run(string launcherPath, int timeoutSeconds, int pollIntervalMs)
        {
            var deadline = DateTime.Now.AddSeconds(timeoutSeconds);

            Process target = null;
            foreach (var p in Process.GetProcessesByName("AtlasDataServer"))
            {
                if (p.MainWindowHandle != IntPtr.Zero) { target = p; break; }
            }

            if (target == null && !string.IsNullOrEmpty(launcherPath))
            {
                try
                {
                    Process.Start(new ProcessStartInfo(launcherPath)
                    {
                        UseShellExecute = true,
                        WorkingDirectory = System.IO.Path.GetDirectoryName(launcherPath)
                    });
                }
                catch (Exception ex)
                {
                    return "launch-failed: " + ex.Message;
                }
            }

            while (target == null && DateTime.Now < deadline)
            {
                foreach (var p in Process.GetProcessesByName("AtlasDataServer"))
                {
                    if (p.MainWindowHandle != IntPtr.Zero) { target = p; break; }
                }
                if (target == null) Thread.Sleep(500);
            }
            if (target == null) return "no-window";

            IntPtr checkbox = IntPtr.Zero;
            while (checkbox == IntPtr.Zero && DateTime.Now < deadline)
            {
                checkbox = FindCheckbox(target.MainWindowHandle);
                if (checkbox == IntPtr.Zero) Thread.Sleep(500);
            }
            if (checkbox == IntPtr.Zero) return "no-checkbox";

            int clickCount = 0;
            while (!target.HasExited)
            {
                int state = SendMessage(checkbox, BM_GETCHECK, 0, 0);
                if (state == 0)
                {
                    SendMessage(checkbox, BM_CLICK, 0, 0);
                    clickCount++;
                }
                Thread.Sleep(pollIntervalMs);
            }
            return "watched, re-clicked " + clickCount + " time(s), ADS closed";
        }
    }
}
'@

$refs = @(
    ([System.Reflection.Assembly]::GetAssembly([System.Diagnostics.Process])).Location,
    ([System.Reflection.Assembly]::GetAssembly([string])).Location
) | Select-Object -Unique

Add-Type -TypeDefinition $source -Language CSharp -ReferencedAssemblies $refs
$PollIntervalMs = 1000
$result = [AdsAuto.OffloadDisabler]::Run($LauncherPath, $TimeoutSeconds, $PollIntervalMs)
"$(Get-Date -Format o)  $result" | Out-File -FilePath "$PSScriptRoot\run.log" -Append -Encoding utf8
