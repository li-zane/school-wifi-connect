using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;

// The scheduler runs this GUI-subsystem executable instead of PowerShell so
// the installer never allocates a console, even briefly at process startup.
internal static class UpdateRunner
{
    private static int Main()
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
        var start = new ProcessStartInfo(powershell,
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" +
            Path.Combine(directory, "apply-update.ps1") + "\" -Plan \"" +
            Path.Combine(directory, "plan.json") + "\"");
        start.WorkingDirectory = directory;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        try
        {
            using (var child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Win32Exception) { return 1; }
    }
}
