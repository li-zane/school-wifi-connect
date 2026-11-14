// On-demand UI Automation bridge. All writes invoke the client's own switches.
// No registry, configuration-file, keyboard, pixel or core-IPC writes.
using System;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;

public static class ProxyClientBridge {
    static readonly TreeWalker Walker = TreeWalker.ControlViewWalker;
    delegate bool EnumWindow(IntPtr handle, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr data);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr handle, out uint id);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr handle, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr handle);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr handle, int command);
    static string Compact(string text) { return String.Concat((text ?? "").Where(c => !Char.IsWhiteSpace(c))); }
    static AutomationElement[] Elements(AutomationElement root) {
        return root.FindAll(TreeScope.Descendants, Condition.TrueCondition).Cast<AutomationElement>().ToArray();
    }
    static bool Pattern(AutomationElement element, out TogglePattern toggle) {
        object value;
        toggle = element.TryGetCurrentPattern(TogglePattern.Pattern, out value) ? (TogglePattern)value : null;
        return toggle != null;
    }
    public static AutomationElement FindSwitch(AutomationElement root, string[] labels) {
        var found = new System.Collections.Generic.List<AutomationElement>();
        foreach (var label in Elements(root).Where(e => labels.Contains(Compact(e.Current.Name)))) {
            // Flutter exposes a labelled group containing its checkbox.
            var children = Elements(label).Where(e => { TogglePattern p; return Pattern(e,out p); }).ToArray();
            if (children.Length == 1) found.Add(children[0]);
            // Verge exposes a text label followed by tooltip buttons and a switch.
            if (label.Current.ControlType == ControlType.Text) {
                var sibling = Walker.GetNextSibling(label);
                for (int i=0; sibling != null && i<6; i++, sibling=Walker.GetNextSibling(sibling)) {
                    TogglePattern toggle;
                    if (Pattern(sibling,out toggle)) { found.Add(sibling); break; }
                    if (sibling.Current.ControlType == ControlType.Text || sibling.Current.ControlType == ControlType.ListItem) break;
                }
            }
        }
        var unique = found.GroupBy(e => String.Join(",", e.GetRuntimeId())).Select(g=>g.First()).ToArray();
        if (unique.Length != 1) throw new InvalidOperationException("switch unavailable or ambiguous");
        return unique[0];
    }
    static readonly string[] ProxyLabels = {"系统代理","Systemproxy","SystemProxy"};
    static readonly string[] TunLabels = {"虚拟网卡模式","虚拟网卡","TUN","Tun","TUN模式","TunMode"};
    public static bool Read(AutomationElement root, string field) {
        var element=FindSwitch(root,field=="tun" ? TunLabels : ProxyLabels);
        TogglePattern p;
        if (!Pattern(element,out p) || p.Current.ToggleState==ToggleState.Indeterminate) throw new InvalidOperationException("state uncertain");
        return p.Current.ToggleState==ToggleState.On;
    }
    public static void Set(AutomationElement root, string field, bool expected, bool desired) {
        var element=FindSwitch(root,field=="tun" ? TunLabels : ProxyLabels);
        TogglePattern p;
        if (!element.Current.IsEnabled || !Pattern(element,out p)) throw new InvalidOperationException("switch disabled");
        var state=p.Current.ToggleState;
        if (state==ToggleState.Indeterminate || (state==ToggleState.On)!=expected) throw new InvalidOperationException("state changed");
        if (expected==desired) return;
        p.Toggle(); // Exactly once. Never retry a toggle whose outcome is unknown.
        var until=DateTime.UtcNow.AddSeconds(6);
        do {
            Thread.Sleep(150);
            try { if(Read(root,field)==desired) return; } catch(InvalidOperationException) {} catch(ElementNotAvailableException) {}
        } while(DateTime.UtcNow<until);
        throw new InvalidOperationException("client did not confirm switch");
    }
    static bool Invoke(AutomationElement root, string[] names, bool prefix=false) {
        var buttons=Elements(root).Where(e=>e.Current.ControlType==ControlType.Button &&
            names.Any(n=>prefix ? Compact(e.Current.Name).StartsWith(n,StringComparison.Ordinal) : Compact(e.Current.Name)==n)).ToArray();
        if(buttons.Length==0) {
            // Flutter's navigation rows may be an unnamed button with a text child.
            buttons=Elements(root).Where(e=>e.Current.ControlType==ControlType.Text && names.Contains(Compact(e.Current.Name)))
                .Select(e=>Walker.GetParent(e)).Where(e=>e!=null && e.Current.ControlType==ControlType.Button).ToArray();
        }
        if(buttons.Length!=1 || !buttons[0].Current.IsEnabled) return false;
        object value;
        if(!buttons[0].TryGetCurrentPattern(InvokePattern.Pattern,out value)) return false;
        ((InvokePattern)value).Invoke();
        Thread.Sleep(350);
        return true;
    }
    static bool Ready(AutomationElement root) {
        try { Read(root,"proxy"); Read(root,"tun"); return true; } catch(InvalidOperationException) { return false; }
    }
    static void Navigate(AutomationElement root, string client) {
        // Do not dismiss someone else's settings dialog or unsaved editor.
        if(Elements(root).Any(e=>e.Current.ControlType==ControlType.Window)) throw new InvalidOperationException("client dialog open");
        if(Ready(root)) return;
        if(client=="clash-verge") {
            if(!Invoke(root,new[]{"设置","Settings"})) throw new InvalidOperationException("settings unavailable");
        } else {
            if(!Invoke(root,new[]{"工具","Tools"})) throw new InvalidOperationException("tools unavailable");
            for(int i=0;i<4;i++) {
                if(Invoke(root,new[]{"网络","Network"})) break;
                if(Invoke(root,new[]{"进阶配置","Advancedconfiguration","AdvancedConfig"},true)) continue;
                if(!Invoke(root,new[]{"返回","Back"})) break;
            }
        }
        var until=DateTime.UtcNow.AddSeconds(4);
        while(!Ready(root) && DateTime.UtcNow<until) Thread.Sleep(200);
        if(!Ready(root)) throw new InvalidOperationException("network settings unavailable");
    }
    static IntPtr Window(string client) {
        string process=client=="flclash" ? "FlClash" : "clash-verge";
        var ids=Process.GetProcessesByName(process).Where(p=>p.SessionId==Process.GetCurrentProcess().SessionId).Select(p=>(uint)p.Id).ToArray();
        var windows=new System.Collections.Generic.List<IntPtr>();
        EnumWindows((handle,data)=>{
            uint id; GetWindowThreadProcessId(handle,out id);
            var text=new StringBuilder(256);GetWindowText(handle,text,256);
            if(ids.Contains(id) && text.ToString()==(client=="flclash" ? "FlClash" : "Clash Verge")) windows.Add(handle);
            return true;
        },IntPtr.Zero);
        if(windows.Count!=1) throw new InvalidOperationException("client window unavailable");
        return windows[0];
    }
    [STAThread] public static int Main(string[] args) {
        Console.OutputEncoding=new UTF8Encoding(false);
        try {
            if(args.Length<2 || (args[0]!="clash-verge" && args[0]!="flclash") || (args[1]!="read" && args[1]!="set")) throw new ArgumentException();
            bool setting=args[1]=="set";
            if(setting && (args.Length!=5 || (args[2]!="proxy" && args[2]!="tun"))) throw new ArgumentException();
            var handle=Window(args[0]);
            bool wasHidden=!IsWindowVisible(handle);
            object windowPattern;
            var root=AutomationElement.FromHandle(handle);
            bool minimized=root.TryGetCurrentPattern(WindowPattern.Pattern,out windowPattern) && ((WindowPattern)windowPattern).Current.WindowVisualState==WindowVisualState.Minimized;
            if(wasHidden) ShowWindow(handle,4); // show without activation; never launch a second client
            if(minimized) ((WindowPattern)windowPattern).SetWindowVisualState(WindowVisualState.Normal);
            try {
                Navigate(root,args[0]);
                if(setting) Set(root,args[2],Boolean.Parse(args[3]),Boolean.Parse(args[4]));
                Console.WriteLine("{\"ok\":true,\"proxy\":"+Read(root,"proxy").ToString().ToLowerInvariant()+",\"tun\":"+Read(root,"tun").ToString().ToLowerInvariant()+"}");
            } finally {
                if(minimized) ((WindowPattern)windowPattern).SetWindowVisualState(WindowVisualState.Minimized);
                if(wasHidden) ShowWindow(handle,0);
            }
            return 0;
        } catch {
            Console.WriteLine("{\"ok\":false,\"error\":\"client_switch_unavailable\"}");
            return 1;
        }
    }
}
