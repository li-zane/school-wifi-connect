using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// Uses the installed Evergreen runtime; no bundled browser and no Node process.
internal static class Program
{
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    internal static string Origin;
    internal static string Backend;
    internal static string Extra = "";
    internal static int Port = 18089;
    internal static string QaReport;
    internal static bool BackgroundStart;

    [STAThread]
    private static void Main(string[] args)
    {
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--port" && i + 1 < args.Length) Port = Int32.Parse(args[++i]);
            else if (args[i] == "--demo") Extra += " --demo";
            else if (args[i] == "--data-dir" && i + 1 < args.Length) Extra += " --data-dir " + Quote(args[++i]);
            else if (args[i] == "--qa-report" && i + 1 < args.Length) QaReport = args[++i];
            else if (args[i] == "--background") BackgroundStart = true;
        }
        if (Port < 1024 || Port > 65535) return;
        Origin = "http://127.0.0.1:" + Port;
        Extra += " --port " + Port;
        Backend = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "NJUSTWiFiBackend.exe");
        bool created;
        using (var mutex = new Mutex(true, "Local\\NjustWifiWindow" + Port, out created))
        {
            if (!created)
            {
                foreach (var process in Process.GetProcessesByName("NJUSTWiFiWindow"))
                {
                    if (process.Id != Process.GetCurrentProcess().Id && process.MainWindowHandle != IntPtr.Zero)
                    { ShowWindow(process.MainWindowHandle, 9); SetForegroundWindow(process.MainWindowHandle); break; }
                }
                using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiShow" + Port)) signal.Set();
                return;
            }
            try { Application.Run(new DesktopWindow()); }
            catch { MessageBox.Show("桌面界面启动失败，请重新打开程序。", "NJUST WiFi", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }
    }

    internal static string Quote(string value) { return "\"" + value.Replace("\"", "") + "\""; }

    internal static async Task<bool> RunBackend(string operation)
    {
        var info = new ProcessStartInfo(Backend, operation + Extra);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WorkingDirectory = AppDomain.CurrentDomain.BaseDirectory;
        using (var p = Process.Start(info))
        {
            bool exited = await Task.Run(() => p.WaitForExit(65000));
            return exited && p.ExitCode == 0;
        }
    }

    internal static async Task<bool> Healthy()
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(Origin + "/api/health");
            request.Proxy = null; request.Timeout = 1200; request.ReadWriteTimeout = 1200;
            var pending = request.GetResponseAsync();
            if (await Task.WhenAny(pending, Task.Delay(1500)) != pending) { request.Abort(); return false; }
            using (var response = await pending)
            using (var reader = new StreamReader(response.GetResponseStream()))
            {
                char[] buffer = new char[1024];
                int length = await reader.ReadAsync(buffer, 0, buffer.Length);
                return new string(buffer, 0, length).Contains("\"app\": \"NJUSTWiFi\"");
            }
        }
        catch { return false; }
    }
}

internal sealed class DesktopWindow : GlassWindow
{
    private WebView2 view;
    private readonly Label loading;
    private readonly EventWaitHandle showSignal;
    private RegisteredWaitHandle showWait;
    private bool initializing;
    private bool allowClose, exiting, pageReady;
    private bool? maximized;
    private bool sizingReady;
    private string sizingScreen;
    [StructLayout(LayoutKind.Sequential)]
    private struct WindowRectangle { public int Left, Top, Right, Bottom; }
    protected override bool ShowWithoutActivation { get { return Program.BackgroundStart; } }

    private async void SyncWindowState()
    {
        if (view == null || view.CoreWebView2 == null) return;
        bool value = WindowState == FormWindowState.Maximized;
        if (maximized == value) return;
        maximized = value;
        try { await view.CoreWebView2.ExecuteScriptAsync("document.documentElement.classList.toggle('native-maximized'," + (value ? "true" : "false") + ")"); }
        catch (InvalidOperationException) { }
        catch (COMException) { }
    }

    internal DesktopWindow()
    {
        // WebView uses logical pixels. WinForms bounds are physical pixels in
        // this per-monitor-DPI-aware process; apply the conversion explicitly.
        AutoScaleMode = AutoScaleMode.None;
        Text = "NJUST WiFi";
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(242, 242, 239);
        loading = new Label { Text = "正在启动…", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter, Font = new Font("Microsoft YaHei UI", 12) };
        Content.Controls.Add(loading);
        Resize += (s, e) => SyncWindowState();
        showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiShow" + Program.Port);
        Shown += async (s, e) => {
            showWait = ThreadPool.RegisterWaitForSingleObject(showSignal, (state, timedOut) => {
                if (!IsDisposed) try { BeginInvoke(new Action(Restore)); } catch (InvalidOperationException) { }
            }, null, Timeout.Infinite, false);
            await Initialize();
        };
        FormClosing += async (s, e) => {
            if (allowClose || e.CloseReason == CloseReason.WindowsShutDown || e.CloseReason == CloseReason.TaskManagerClosing) return;
            e.Cancel = true;
            if (pageReady && view != null && view.CoreWebView2 != null)
                await view.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new Event('njust-close-request'))");
            else {
                var choice = MessageBox.Show(this, "退出程序？选择“否”关闭到托盘。", "NJUST WiFi", MessageBoxButtons.YesNoCancel);
                if (choice == DialogResult.Yes) await ExitProgram();
                else if (choice == DialogResult.No) ToTray();
            }
        };
        FormClosed += (s, e) => {
            if (showWait != null) showWait.Unregister(null);
            showSignal.Dispose();
            ReleaseView();
        };
    }

    internal static Size WindowSizeForDpi(Size logical, int dpi, Size workingArea)
    {
        double scale = Math.Max(96, dpi) / 96.0;
        return new Size(Math.Max(1, Math.Min(workingArea.Width, (int)Math.Round(logical.Width * scale))),
                        Math.Max(1, Math.Min(workingArea.Height, (int)Math.Round(logical.Height * scale))));
    }

    private void UpdateWindowLimits(Screen screen, int dpi)
    {
        sizingScreen = screen.DeviceName;
        MinimumSize = WindowSizeForDpi(new Size(860, 620), dpi, screen.WorkingArea.Size);
        MaximizedBounds = screen.WorkingArea;
        Padding = new Padding(Math.Max(5, dpi * 5 / 96));
    }

    protected override void OnLoad(EventArgs e)
    {
        var screen = Screen.FromControl(this);
        UpdateWindowLimits(screen, WindowDpi);
        Size = WindowSizeForDpi(new Size(1120, 780), WindowDpi, screen.WorkingArea.Size);
        Location = new Point(screen.WorkingArea.Left + (screen.WorkingArea.Width - Width) / 2,
                             screen.WorkingArea.Top + (screen.WorkingArea.Height - Height) / 2);
        sizingReady = true;
        base.OnLoad(e);
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x02E0 && sizingReady) // WM_DPICHANGED
        {
            // Handle the native message: this minimal shell does not opt into
            // WinForms automatic scaling, so its managed DpiChanged may not fire.
            var rect = (WindowRectangle)Marshal.PtrToStructure(m.LParam, typeof(WindowRectangle));
            var suggested = Rectangle.FromLTRB(rect.Left, rect.Top, rect.Right, rect.Bottom);
            var screen = Screen.FromRectangle(suggested);
            UpdateWindowLimits(screen, (int)(m.WParam.ToInt64() & 0xffff));
            if (WindowState == FormWindowState.Normal)
            {
                var area = screen.WorkingArea;
                var size = new Size(Math.Max(MinimumSize.Width, Math.Min(area.Width, suggested.Width)),
                                    Math.Max(MinimumSize.Height, Math.Min(area.Height, suggested.Height)));
                Bounds = new Rectangle(new Point(Math.Max(area.Left, Math.Min(suggested.Left, area.Right - size.Width)),
                                                 Math.Max(area.Top, Math.Min(suggested.Top, area.Bottom - size.Height))), size);
            }
            m.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref m);
    }

    protected override void OnLocationChanged(EventArgs e)
    {
        base.OnLocationChanged(e);
        if (!sizingReady) return;
        var screen = Screen.FromControl(this);
        if (sizingScreen == screen.DeviceName) return;
        UpdateWindowLimits(screen, WindowDpi);
        if (WindowState == FormWindowState.Normal)
            Size = new Size(Math.Min(Width, screen.WorkingArea.Width), Math.Min(Height, screen.WorkingArea.Height));
    }

    private void ToTray()
    {
        allowClose = true; Close();
    }

    private async Task ExitProgram()
    {
        if (exiting) return;
        exiting = true;
        if (await Program.RunBackend("--stop-background")) {
            for (int i = 0; i < 40 && await Program.Healthy(); i++) await Task.Delay(250);
            if (await Program.Healthy()) {
                exiting = false; MessageBox.Show(this, "后台仍在退出，请稍后重试。", "NJUST WiFi"); return;
            }
            using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiTrayExit" + Program.Port)) signal.Set();
            allowClose = true; Close();
        } else {
            exiting = false;
            MessageBox.Show(this, "后台尚未退出，请重试。", "NJUST WiFi");
        }
    }

    private async void Restore()
    {
        ShowInTaskbar = true; WindowState = FormWindowState.Normal; Show(); Activate();
        await Initialize();
    }

    private void ReleaseView()
    {
        if (view != null) { Content.Controls.Remove(view); view.Dispose(); view = null; }
        if (!loading.IsDisposed) loading.Visible = true;
        // Release the browser processes before idling in the tray.
        GC.Collect(); GC.WaitForPendingFinalizers();
    }

    private async Task Initialize()
    {
        if (initializing || view != null) return;
        initializing = true;
        loading.Text = "正在启动…";
        try
        {
            if (!await Program.Healthy())
            {
                if (!await Program.RunBackend("--bootstrap")) throw new InvalidOperationException();
                bool healthy = false;
                for (int i = 0; i < 80 && !IsDisposed; i++)
                { if (await Program.Healthy()) { healthy = true; break; } await Task.Delay(250); }
                if (!healthy) throw new InvalidOperationException();
            }
            if (IsDisposed) return;
            string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NJUSTWiFi", "desktop", Program.Port.ToString());
            var options = new CoreWebView2EnvironmentOptions("--disable-background-networking --disable-extensions --disable-sync");
            var environment = await CoreWebView2Environment.CreateAsync(null, data, options);
            if (IsDisposed) return;
            view = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor, AccessibleName = "NJUST WiFi" };
            Content.Controls.Add(view); view.BringToFront();
            await view.EnsureCoreWebView2Async(environment);
            var core = view.CoreWebView2;
            await core.AddScriptToExecuteOnDocumentCreatedAsync("window.NJUST_DESKTOP = true;");
            core.WebMessageReceived += async (s, e) => {
                if (e.Source != Program.Origin + "/") return;
                try {
                    string message = e.TryGetWebMessageAsString();
                    if (message == "theme:dark" || message == "theme:light") SetChromeTheme(message == "theme:dark");
                    else if (message == "window:ready") { pageReady = true; maximized = null; SyncWindowState(); }
                    else if (message == "window:minimize") WindowState = FormWindowState.Minimized;
                    else if (message == "window:maximize") ToggleMaximize();
                    else if (message == "window:drag") DragWindow();
                    else if (message == "window:resize-bottom-right") ResizeBottomRight();
                    else if (message == "window:close") Close();
                    else if (message == "window:tray") ToTray();
                    else if (message == "window:exit") await ExitProgram();
                } catch (ArgumentException) { }
            };
            core.MemoryUsageTargetLevel = CoreWebView2MemoryUsageTargetLevel.Low;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Profile.IsPasswordAutosaveEnabled = false;
            core.Profile.IsGeneralAutofillEnabled = false;
            core.NavigationStarting += (s, e) => {
                Uri uri;
                if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out uri) || uri.GetLeftPart(UriPartial.Authority) != Program.Origin) e.Cancel = true;
            };
            core.NewWindowRequested += (s, e) => { e.Handled = true; };
            core.PermissionRequested += (s, e) => { e.State = CoreWebView2PermissionState.Deny; };
            core.DownloadStarting += (s, e) => {
                var dialog = new SaveFileDialog { FileName = "NJUSTWiFi-diagnostics.json", Filter = "JSON|*.json" };
                using (dialog)
                { if (dialog.ShowDialog(this) == DialogResult.OK) { e.ResultFilePath = dialog.FileName; e.Handled = true; } else e.Cancel = true; }
            };
            core.NavigationCompleted += async (s, e) => {
                if (!e.IsSuccess || view == null) return;
                loading.Visible = false;
                if (Program.QaReport != null)
                {
                    await Task.Delay(1200);
                    if (view == null) return;
                    // Only booleans/version for isolated QA, never account or balance values.
                    string report = await core.ExecuteScriptAsync("JSON.stringify({title:document.title,version:document.querySelector('#version-text').textContent,settings:!!document.querySelector('#form-config'),balance:!!document.querySelector('#balance-val'),origin:location.origin,viewportWidth:innerWidth,viewportHeight:innerHeight,pixelRatio:devicePixelRatio,nativeDpi:" + WindowDpi + ",minimumWidth:" + MinimumSize.Width + ",minimumHeight:" + MinimumSize.Height + "})");
                    File.WriteAllText(Program.QaReport, report);
                }
            };
            core.Navigate(Program.Origin + "/");
        }
        catch (WebView2RuntimeNotFoundException)
        {
            ReleaseView(); loading.Text = "请安装 Microsoft Edge WebView2 Runtime 后重新打开。";
        }
        catch
        {
            ReleaseView(); loading.Text = "启动失败，请退出后重新打开。";
        }
        finally
        {
            initializing = false;
        }
    }
}
