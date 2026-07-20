// Tiny launcher for PoE Flipper — starts the tray app and exits.
// Path-relative: works from whatever folder it sits in alongside
// flipper.py and the .venv.  The app itself is single-instance
// (a second launch exits silently).
using System;
using System.Diagnostics;
using System.IO;

static class Launcher
{
    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        var psi = new ProcessStartInfo
        {
            FileName = Path.Combine(root, ".venv", "Scripts", "pythonw.exe"),
            Arguments = "\"" + Path.Combine(root, "flipper.py") + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        Process.Start(psi);
    }
}
