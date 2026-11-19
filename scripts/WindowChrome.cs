using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal class GlassWindow : Form
{
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
    protected readonly Panel Content = new Panel { Dock = DockStyle.Fill };
    private Color border = Color.FromArgb(222, 222, 218);
    // DeviceDpi can remain 96 in a framework shell without a WinForms DPI
    // app.config, even when its WebView and HWND are per-monitor DPI aware.
    protected int WindowDpi
    {
        get {
            try { if (IsHandleCreated) { int dpi = (int)GetDpiForWindow(Handle); if (dpi > 0) return dpi; } }
            catch (EntryPointNotFoundException) { }
            return DeviceDpi;
        }
    }
    protected GlassWindow()
    {
        FormBorderStyle = FormBorderStyle.None;
        Padding = new Padding(5);
        Font = new Font("Microsoft YaHei UI", 9);
        string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "NJUSTWiFi.ico");
        if (File.Exists(path)) Icon = new Icon(path, 32, 32);
        Controls.Add(Content);
        SetChromeTheme(false);
    }
    protected void DragWindow()
    { ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero); }
    protected void ResizeBottomRight()
    {
        if (WindowState != FormWindowState.Normal) return;
        ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(17), IntPtr.Zero);
    }
    protected void ToggleMaximize()
    {
        MaximizedBounds = Screen.FromControl(this).WorkingArea;
        WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
    }
    protected void SetChromeTheme(bool dark)
    {
        BackColor = Content.BackColor = dark ? Color.FromArgb(15, 16, 18) : Color.FromArgb(242, 242, 239);
        border = dark ? Color.FromArgb(38, 40, 44) : Color.FromArgb(222, 222, 218);
        Invalidate();
    }
    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        using (var pen = new Pen(border)) e.Graphics.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
    }
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x84 && WindowState == FormWindowState.Normal)
        {
            long position = m.LParam.ToInt64();
            Point p = PointToClient(new Point((short)(position & 0xffff), (short)((position >> 16) & 0xffff)));
            int edge = Math.Max(5, WindowDpi * 5 / 96);
            int corner = Math.Max(28, WindowDpi * 28 / 96);
            if (p.X >= ClientSize.Width - corner && p.Y >= ClientSize.Height - corner &&
                p.X < ClientSize.Width && p.Y < ClientSize.Height)
            { m.Result = new IntPtr(17); return; }
            bool left = p.X < edge, right = p.X >= Width - edge, top = p.Y < edge, bottom = p.Y >= Height - edge;
            int hit = top && left ? 13 : top && right ? 14 : bottom && left ? 16 : bottom && right ? 17 : left ? 10 : right ? 11 : top ? 12 : bottom ? 15 : 0;
            if (hit != 0) { m.Result = new IntPtr(hit); return; }
        }
        base.WndProc(ref m);
    }
}
