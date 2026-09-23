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
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

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

                // The tray icon needs an STA thread with a message loop; the PowerShell host thread may be neither.
                Exception failure = null;
                var ui = new Thread(() =>
                {
                    try { Watch(launcherPath, processName, timeoutSeconds, pollIntervalMs); }
                    catch (Exception ex) { failure = ex; }
                });
                ui.SetApartmentState(ApartmentState.STA);
                ui.Start();
                ui.Join();
                if (failure != null) throw failure;
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
                if (ads.HasExited)
                {
                    Log("ADS closed.");
                    return;
                }
                if (checkbox == IntPtr.Zero)
                    throw new InvalidOperationException("Couldn't find the '" + CheckboxText +
                        "' checkbox in ADS - its label may have changed in an ADS update.");

                using (var session = new Session(ads, checkbox, launcherPath, pollIntervalMs))
                {
                    Application.Run(session);
                    if (session.Failure != null) throw session.Failure;
                }
            }
        }

        // One ADS run: polls the checkbox and shows a tray menu whose "Keep next offload disabled"
        // toggle lets the customer take an offload (unticks the box and stops re-ticking it).
        private sealed class Session : ApplicationContext
        {
            private const string Title = "ADS Offload Disabler";

            private readonly Process ads;
            private readonly Icon adsIcon;
            private readonly NotifyIcon tray;
            private readonly ToolStripMenuItem keepDisabledItem;
            private readonly System.Windows.Forms.Timer poll;
            private IntPtr checkbox;
            private bool keepDisabled = true;

            public Exception Failure { get; private set; }

            public Session(Process ads, IntPtr checkbox, string launcherPath, int pollIntervalMs)
            {
                this.ads = ads;
                this.checkbox = checkbox;
                adsIcon = File.Exists(launcherPath) ? Icon.ExtractAssociatedIcon(launcherPath) : SystemIcons.Application;

                keepDisabledItem = new ToolStripMenuItem("Keep next offload disabled") { Checked = true, CheckOnClick = true };
                keepDisabledItem.CheckedChanged += delegate { Guard(OnToggle); };
                var menu = new ContextMenuStrip();
                menu.Items.Add(keepDisabledItem);

                tray = new NotifyIcon { ContextMenuStrip = menu, Visible = true };
                UpdateTray();
                tray.ShowBalloonTip(5000, Title, "Next offload is disabled. Right-click this icon to allow offloads.", ToolTipIcon.Info);

                poll = new System.Windows.Forms.Timer { Interval = pollIntervalMs };
                poll.Tick += delegate { Guard(Poll); };
                poll.Start();
            }

            private void Poll()
            {
                if (ads.HasExited)
                {
                    Log("ADS closed.");
                    ExitThread();
                    return;
                }
                // Re-validate after reading: BM_GETCHECK to a window destroyed mid-send reads back as 0.
                if (keepDisabled && RefreshCheckbox() && GetCheckState(checkbox) == 0 && IsOffloadCheckbox(checkbox, ads.Id))
                {
                    if (Click(checkbox)) Log("Ticked '" + CheckboxText + "'.");
                    else Log("ADS didn't respond to the tick; will retry.");
                }
            }

            private void OnToggle()
            {
                keepDisabled = keepDisabledItem.Checked;
                UpdateTray();
                if (keepDisabled)
                {
                    Log("Auto-disable turned on.");
                    Poll();
                    return;
                }

                Log("Auto-disable turned off; offloads allowed.");
                int? state = RefreshCheckbox() ? GetCheckState(checkbox) : null;
                if (state == 0) return;
                if (state == 1 && IsOffloadCheckbox(checkbox, ads.Id) && Click(checkbox))
                {
                    Log("Unticked '" + CheckboxText + "'.");
                    return;
                }
                Log("Couldn't untick '" + CheckboxText + "'.");
                tray.ShowBalloonTip(5000, Title, "Couldn't untick 'Disable Next Offload' - untick it in ADS.", ToolTipIcon.Warning);
            }

            private bool RefreshCheckbox()
            {
                if (!IsOffloadCheckbox(checkbox, ads.Id)) checkbox = FindCheckbox(ads);
                return checkbox != IntPtr.Zero;
            }

            private void UpdateTray()
            {
                tray.Icon = keepDisabled ? adsIcon : SystemIcons.Warning;
                tray.Text = keepDisabled ? "ADS: next offload disabled - right-click to change"
                                         : "ADS: offloads allowed - right-click to change";
            }

            private void Guard(Action action)
            {
                try { action(); }
                catch (Exception ex)
                {
                    Failure = ex;
                    ExitThread();
                }
            }

            protected override void ExitThreadCore()
            {
                poll.Stop();
                tray.Visible = false;
                base.ExitThreadCore();
            }

            protected override void Dispose(bool disposing)
            {
                if (disposing)
                {
                    poll.Dispose();
                    tray.Dispose();
                }
                base.Dispose(disposing);
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
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $refs = [System.Diagnostics.Process], [string], [System.Threading.Mutex], [System.IO.File],
            [System.Windows.Forms.NotifyIcon], [System.Drawing.Icon] |
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
