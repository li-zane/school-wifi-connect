using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Runtime.InteropServices;
using System.Windows.Forms;

// This process never loads WebView2 assemblies. The disposable window is separate.
internal static class TrayProgram
{
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [STAThread]
    private static void Main(string[] args)
    {
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
        int port = 18089;
        for (int i = 0; i + 1 < args.Length; i++) if (args[i] == "--port") port = Int32.Parse(args[i + 1]);
        bool created;
        using (var mutex = new Mutex(true, "Local\\NjustWifiTray" + port, out created))
        {
            if (!created)
            {
                using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiTrayShow" + port)) signal.Set();
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var context = new TrayContext(args, port)) Application.Run(context);
        }
    }
}

internal sealed class TrayContext : ApplicationContext
{
    private readonly NotifyIcon tray;
    private readonly Control invoker = new Control();
    private readonly EventWaitHandle showSignal;
    private readonly RegisteredWaitHandle wait;
    private readonly EventWaitHandle exitSignal;
    private readonly RegisteredWaitHandle exitWait;
    private readonly string arguments;
    private readonly string backendArguments;
    private readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    private readonly int port;
    private Process window;
    private readonly WindowJob job = new WindowJob();

    internal TrayContext(string[] args, int port)
    {
        this.port = port;
        string all = "", backend = "";
        for (int i = 0; i < args.Length; i++)
        {
            string item = args[i];
            all += " " + Quote(item);
            if (item != "--qa-report") backend += " " + Quote(item);
            else if (i + 1 < args.Length) all += " " + Quote(args[++i]);
        }
        arguments = all; backendArguments = backend;
        var handle = invoker.Handle;
        var menu = new ContextMenuStrip();
        menu.Items.Add("打开", null, (s, e) => Open());
        menu.Items.Add("退出界面", null, (s, e) => ExitThread());
        menu.Items.Add("退出程序", null, async (s, e) => {
            try
            {
                var start = new ProcessStartInfo(Path.Combine(root, "NJUSTWiFiBackend.exe"), "--stop-background" + backendArguments);
                start.CreateNoWindow = true; start.UseShellExecute = false;
                using (var child = Process.Start(start))
                {
                    bool done = await System.Threading.Tasks.Task.Run(() => child.WaitForExit(30000));
                    if (!done || child.ExitCode != 0) throw new InvalidOperationException();
                }
                ExitThread();
            }
            catch { MessageBox.Show("后台尚未退出，请重试。", "NJUST WiFi"); }
        });
        tray = new NotifyIcon { Icon = new Icon(Path.Combine(root, "NJUSTWiFi.ico"), 32, 32), Text = "NJUST WiFi", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (s, e) => Open();
        showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiTrayShow" + port);
        wait = ThreadPool.RegisterWaitForSingleObject(showSignal, (s, timedOut) => {
            try { invoker.BeginInvoke(new Action(Open)); } catch (InvalidOperationException) { }
        }, null, Timeout.Infinite, false);
        exitSignal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiTrayExit" + port);
        exitWait = ThreadPool.RegisterWaitForSingleObject(exitSignal, (s, timedOut) => {
            try { invoker.BeginInvoke(new Action(ExitThread)); } catch (InvalidOperationException) { }
        }, null, Timeout.Infinite, false);
        Open();
    }

    private static string Quote(string s) { return "\"" + s.Replace("\"", "") + "\""; }
    private void Open()
    {
        try
        {
            if (window != null && !window.HasExited)
            {
                using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\NjustWifiShow" + port)) signal.Set();
                return;
            }
            if (window != null) window.Dispose();
            var start = new ProcessStartInfo(Path.Combine(root, "NJUSTWiFiWindow.exe"), arguments);
            start.UseShellExecute = false; start.WorkingDirectory = root;
            window = Process.Start(start);
            job.Add(window);
        }
        catch { MessageBox.Show("窗口启动失败，请确认发布包已完整解压。", "NJUST WiFi"); }
    }

    protected override void ExitThreadCore()
    {
        if (window != null) { try { if (!window.HasExited) window.CloseMainWindow(); } catch { } }
        base.ExitThreadCore();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            wait.Unregister(null); exitWait.Unregister(null); showSignal.Dispose(); exitSignal.Dispose(); invoker.Dispose();
            tray.Visible = false; tray.Dispose(); if (window != null) window.Dispose();
            job.Dispose();
        }
        base.Dispose(disposing);
    }
}

// Also release the window/browser tree when Task Scheduler terminates the tray.
internal sealed class WindowJob : IDisposable
{
    [StructLayout(LayoutKind.Sequential)] private struct Limits
    {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Counters
    { public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits
    {
        public Limits Basic;
        public Counters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll")] private static extern bool SetInformationJobObject(IntPtr job, int info, ref ExtendedLimits limits, int length);
    [DllImport("kernel32.dll")] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr value);
    private IntPtr handle;
    internal WindowJob()
    {
        handle = CreateJobObject(IntPtr.Zero, null);
        var limits = new ExtendedLimits(); limits.Basic.Flags = 0x2000; // KILL_ON_JOB_CLOSE
        if (handle == IntPtr.Zero || !SetInformationJobObject(handle, 9, ref limits, Marshal.SizeOf(typeof(ExtendedLimits))))
        { Dispose(); throw new InvalidOperationException("Window job creation failed"); }
    }
    internal void Add(Process process)
    {
        if (!AssignProcessToJobObject(handle, process.Handle))
        { try { process.Kill(); } catch { } throw new InvalidOperationException("Window process assignment failed"); }
    }
    public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
}
