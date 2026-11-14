using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal class GlassWindow : Form
{
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wparam, IntPtr lparam);
    protected readonly Panel Content = new Panel { Dock = DockStyle.Fill };
    private readonly Panel titlebar = new Panel { Dock = DockStyle.Top, Height = 48 };
    private readonly Label title = new Label { Text = "NJUST WiFi", AutoSize = false, TextAlign = ContentAlignment.MiddleLeft, Dock = DockStyle.Fill };
    private readonly ChromeButton minimize = new ChromeButton("min", "最小化到托盘");
    private readonly ChromeButton maximize = new ChromeButton("max", "最大化或还原");
    private readonly ChromeButton close = new ChromeButton("close", "关闭到托盘");
    private Color border = Color.FromArgb(226, 231, 239);

    protected GlassWindow()
    {
        FormBorderStyle = FormBorderStyle.None;
        Padding = new Padding(6);
        Font = new Font("Microsoft YaHei UI", 9);
        var actions = new FlowLayoutPanel { Dock = DockStyle.Right, Width = 144, WrapContents = false, Margin = new Padding(0), Padding = new Padding(0, 4, 0, 4) };
        actions.Controls.Add(minimize); actions.Controls.Add(maximize); actions.Controls.Add(close);
        var logo = new PictureBox { Dock = DockStyle.Left, Width = 40, SizeMode = PictureBoxSizeMode.CenterImage, Padding = new Padding(6) };
        string iconPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "NJUSTWiFi.ico");
        if (File.Exists(iconPath))
        {
            Icon = new Icon(iconPath, 32, 32);
            using (var small = new Icon(iconPath, 24, 24)) logo.Image = small.ToBitmap();
        }
        titlebar.Controls.Add(title); titlebar.Controls.Add(logo); titlebar.Controls.Add(actions);
        Controls.Add(Content); Controls.Add(titlebar);
        title.Font = new Font(Font.FontFamily, 10, FontStyle.Bold);
        minimize.Click += (s, e) => Close();
        maximize.Click += (s, e) => ToggleMaximize();
        close.Click += (s, e) => Close();
        foreach (Control surface in new Control[] { titlebar, title, logo })
        {
            surface.MouseDown += (s, e) => {
                if (e.Button == MouseButtons.Left) { ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero); }
            };
            surface.DoubleClick += (s, e) => ToggleMaximize();
        }
        SetChromeTheme(false);
        Activated += (s, e) => ResetChromeButtons();
        Deactivate += (s, e) => ResetChromeButtons();
    }

    private void ResetChromeButtons()
    {
        foreach (var button in new[] { minimize, maximize, close }) button.ResetInteraction();
    }

    private void ToggleMaximize()
    {
        MaximizedBounds = Screen.FromControl(this).WorkingArea;
        WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
    }

    protected void SetChromeTheme(bool dark)
    {
        Color background = dark ? Color.FromArgb(21, 25, 34) : Color.FromArgb(246, 249, 253);
        Color foreground = dark ? Color.FromArgb(237, 241, 250) : Color.FromArgb(34, 41, 58);
        BackColor = background;
        titlebar.BackColor = background; title.ForeColor = foreground;
        Content.BackColor = background;
        border = dark ? Color.FromArgb(52, 64, 86) : Color.FromArgb(226, 231, 239);
        foreach (var button in new[] { minimize, maximize, close })
        { button.BackColor = background; button.ForeColor = foreground; button.Dark = dark; button.Invalidate(); }
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        using (var pen = new Pen(border)) e.Graphics.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
    }

    protected override void WndProc(ref Message m)
    {
        const int HitTest = 0x84;
        if (m.Msg == HitTest && WindowState == FormWindowState.Normal)
        {
            long position = m.LParam.ToInt64();
            Point p = PointToClient(new Point((short)(position & 0xffff), (short)((position >> 16) & 0xffff)));
            int edge = Math.Max(5, DeviceDpi * 5 / 96);
            bool left = p.X < edge, right = p.X >= Width - edge, top = p.Y < edge, bottom = p.Y >= Height - edge;
            int hit = top && left ? 13 : top && right ? 14 : bottom && left ? 16 : bottom && right ? 17 : left ? 10 : right ? 11 : top ? 12 : bottom ? 15 : 0;
            if (hit != 0) { m.Result = new IntPtr(hit); return; }
        }
        base.WndProc(ref m);
    }
}

internal sealed class ChromeButton : Button
{
    private readonly string kind;
    private bool hover;
    private bool keyboardFocus;
    internal bool Dark;
    internal ChromeButton(string kind, string label)
    {
        this.kind = kind; AccessibleName = label; Size = new Size(44, 40); Margin = new Padding(0);
        FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; TabStop = true;
        SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
        MouseEnter += (s, e) => { hover = true; Invalidate(); };
        MouseLeave += (s, e) => { hover = false; Invalidate(); };
        MouseDown += (s, e) => { keyboardFocus = false; };
        KeyDown += (s, e) => { keyboardFocus = true; Invalidate(); };
        GotFocus += (s, e) => { keyboardFocus = !Control.MouseButtons.HasFlag(MouseButtons.Left); Invalidate(); };
        LostFocus += (s, e) => { keyboardFocus = false; Invalidate(); };
    }
    internal void ResetInteraction() { hover = false; keyboardFocus = false; Invalidate(); }
    protected override void OnClick(EventArgs e)
    { ResetInteraction(); base.OnClick(e); }
    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(BackColor); e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        Color ink = ForeColor;
        if (hover || (Focused && keyboardFocus && ShowFocusCues))
        {
            Color fill = kind == "close" ? (Dark ? Color.FromArgb(81, 52, 63) : Color.FromArgb(251, 230, 232)) : Dark ? Color.FromArgb(48, 69, 97) : Color.FromArgb(230, 239, 250);
            using (var brush = new SolidBrush(fill)) e.Graphics.FillEllipse(brush, Width / 2 - 15, Height / 2 - 15, 30, 30);
            if (kind == "close") ink = Dark ? Color.FromArgb(250, 192, 203) : Color.FromArgb(170, 79, 97);
        }
        using (var pen = new Pen(ink, 1.5f))
        {
            int x = Width / 2, y = Height / 2;
            if (kind == "min") e.Graphics.DrawLine(pen, x - 5, y + 2, x + 5, y + 2);
            else if (kind == "max") e.Graphics.DrawRectangle(pen, x - 5, y - 5, 10, 10);
            else { e.Graphics.DrawLine(pen, x - 4, y - 4, x + 4, y + 4); e.Graphics.DrawLine(pen, x + 4, y - 4, x - 4, y + 4); }
        }
    }
}
