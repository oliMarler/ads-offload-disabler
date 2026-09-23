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

                // The window needs an STA thread with a message loop; the PowerShell host thread may be neither.
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

                Application.EnableVisualStyles();
                using (var window = new ControlWindow(ads, checkbox, launcherPath, pollIntervalMs))
                {
                    Application.Run(window);
                    if (window.Failure != null) throw window.Failure;
                }
            }
        }

        // Small window shown beside ADS: polls the checkbox, and its button lets the customer allow an
        // offload (unticks the box and stops re-ticking it) or go back to disabling offloads.
        // This script does not launch ADS itself; it only interfaces with an already-running ADS window.
        private sealed class ControlWindow : Form
        {
            [StructLayout(LayoutKind.Sequential)]
            private struct RECT { public int Left, Top, Right, Bottom; }

            [DllImport("user32.dll")]
            private static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

            [DllImport("user32.dll")]
            private static extern bool ShowWindow(IntPtr hWnd, int cmdShow);

            private const int SW_SHOWNOACTIVATE = 4;

            private readonly Process ads;
            private readonly Icon adsIcon;
            private readonly Label status;
            private readonly Button toggle;
            private readonly System.Windows.Forms.Timer poll;
            private IntPtr checkbox;
            private bool keepDisabled = true;

            public Exception Failure { get; private set; }

            public ControlWindow(Process ads, IntPtr checkbox, string launcherPath, int pollIntervalMs)
            {
                this.ads = ads;
                this.checkbox = checkbox;
                adsIcon = File.Exists(launcherPath) ? Icon.ExtractAssociatedIcon(launcherPath) : SystemIcons.Application;

                Text = "ADS Offload Disabler";
                Font = new Font("Segoe UI", 10F);
                FormBorderStyle = FormBorderStyle.FixedSingle;
                MaximizeBox = false;
                MinimizeBox = true;
                StartPosition = FormStartPosition.Manual;
                AutoSize = false;
                Size = new Size(340, 150);
                MinimumSize = new Size(340, 150);
                MaximumSize = new Size(340, 150);
                Padding = new Padding(10);

                status = new Label
                {
                    Width = 260,
                    Height = 28,
                    Font = new Font(Font, FontStyle.Bold),
                    TextAlign = ContentAlignment.MiddleCenter,
                    AutoEllipsis = true,
                    BackColor = Color.Transparent,
                    ForeColor = Color.DarkGreen
                };
                toggle = new Button
                {
                    Width = 175,
                    Height = 30,
                    Padding = new Padding(6, 3, 6, 3)
                };
                toggle.Click += delegate { Guard(Toggle); };
                Controls.Add(status);
                Controls.Add(toggle);
                UpdateView();

                poll = new System.Windows.Forms.Timer { Interval = pollIntervalMs };
                poll.Tick += delegate { Guard(Poll); };
                poll.Start();
            }

            protected override void OnLoad(EventArgs e)
            {
                base.OnLoad(e);
                status.Left = (ClientSize.Width - status.Width) / 2;
                status.Top = 20;
                toggle.Left = (ClientSize.Width - toggle.Width) / 2;
                toggle.Top = 58;

                RECT r;
                IntPtr adsWindow = CurrentMainWindow(ads);
                if (adsWindow == IntPtr.Zero || !GetWindowRect(adsWindow, out r)) return;
                Rectangle area = Screen.FromHandle(adsWindow).WorkingArea;
                Location = new Point(Math.Min(r.Right + 8, area.Right - Width), Math.Max(r.Top, area.Top));
            }

            protected override void OnShown(EventArgs e)
            {
                base.OnShown(e);
                // The VBS starts us hidden, and Windows applies that to our first top-level window;
                // the second ShowWindow isn't overridden.
                ShowWindow(Handle, SW_SHOWNOACTIVATE);
            }

            protected override void OnFormClosing(FormClosingEventArgs e)
            {
                // Allow the X button to close the helper normally.
                // If ADS exits, the watcher already calls Finish() and closes cleanly.
                base.OnFormClosing(e);
            }

            private void Poll()
            {
                if (ads.HasExited)
                {
                    Log("ADS closed.");
                    Finish();
                    return;
                }
                // Re-validate after reading: BM_GETCHECK to a window destroyed mid-send reads back as 0.
                if (keepDisabled && RefreshCheckbox() && GetCheckState(checkbox) == 0 && IsOffloadCheckbox(checkbox, ads.Id))
                {
                    if (ClickCheckbox(checkbox)) Log("Ticked '" + CheckboxText + "'.");
                    else Log("ADS didn't respond to the tick; will retry.");
                }
            }

            private void Toggle()
            {
                if (keepDisabled)
                {
                    Log("Offload allowed from the window.");
                    if (!RefreshCheckbox())
                    {
                        Log("Couldn't find '" + CheckboxText + "' to untick.");
                        MessageBox.Show(this, "Couldn't find 'Disable Next Offload' - untick it in ADS.", Text,
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        return;
                    }

                    int? state = GetCheckState(checkbox);
                    if (state == 0)
                    {
                        keepDisabled = false;
                        UpdateView();
                        return;
                    }
                    if (state == 1 && IsOffloadCheckbox(checkbox, ads.Id) && ClickCheckbox(checkbox))
                    {
                        keepDisabled = false;
                        UpdateView();
                        Log("Unticked '" + CheckboxText + "'.");
                        return;
                    }

                    Log("Couldn't untick '" + CheckboxText + "'.");
                    MessageBox.Show(this, "Couldn't untick 'Disable Next Offload' - untick it in ADS.", Text,
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }

                keepDisabled = true;
                UpdateView();
                Log("Offloads disabled again from the window.");
                Poll();
            }

            private bool RefreshCheckbox()
            {
                if (!IsOffloadCheckbox(checkbox, ads.Id)) checkbox = FindCheckbox(ads);
                return checkbox != IntPtr.Zero;
            }

            private void UpdateView()
            {
                Icon = keepDisabled ? adsIcon : SystemIcons.Warning;
                status.Text = keepDisabled ? "Next offload: DISABLED" : "Next offload: ALLOWED";
                status.ForeColor = keepDisabled ? Color.DarkGreen : Color.OrangeRed;
                toggle.Text = keepDisabled ? "Allow offload" : "Disable offload";
            }

            private void Finish()
            {
                poll.Stop();
                Close();
            }

            private void Guard(Action action)
            {
                try { action(); }
                catch (Exception ex)
                {
                    Failure = ex;
                    Finish();
                }
            }

            protected override void Dispose(bool disposing)
            {
                if (disposing) poll.Dispose();
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

        private static bool ClickCheckbox(IntPtr checkbox)
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
            [System.Windows.Forms.Form], [System.Drawing.Icon] |
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
