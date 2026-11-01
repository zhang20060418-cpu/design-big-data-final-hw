using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("SlideSentinel Launcher")]
[assembly: AssemblyDescription("Lightweight launcher for the SlideSentinel source tree")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class SlideSentinelLauncher
{
    [STAThread]
    private static void Main(string[] args)
    {
        try
        {
            string executablePath = Application.ExecutablePath;
            string root = Path.GetDirectoryName(executablePath);
            if (string.IsNullOrEmpty(root))
                throw new InvalidOperationException("Cannot determine the application directory.");

            string executableName = Path.GetFileNameWithoutExtension(executablePath);
            bool stop = executableName.EndsWith(".Stop", StringComparison.OrdinalIgnoreCase);
            bool setup = false;
            foreach (string arg in args)
            {
                if (string.Equals(arg, "--stop", StringComparison.OrdinalIgnoreCase)) stop = true;
                if (string.Equals(arg, "--setup", StringComparison.OrdinalIgnoreCase)) setup = true;
            }

            string script;
            bool visible;
            if (stop)
            {
                script = Path.Combine(root, "scripts", "stop.ps1");
                visible = false;
            }
            else
            {
                string python = Path.Combine(root, "backend", ".venv", "Scripts", "python.exe");
                string frontend = Path.Combine(root, "frontend", "dist", "index.html");
                bool needsSetup = setup || !File.Exists(python) || !File.Exists(frontend);
                script = Path.Combine(root, "scripts", needsSetup ? "start_app.ps1" : "launch.ps1");
                visible = needsSetup;
            }

            if (!File.Exists(script))
                throw new FileNotFoundException("The launcher script is missing. Keep this EXE in the agent_app root directory.", script);

            StartPowerShell(root, script, visible);
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                "SlideSentinel could not start.\r\n\r\n" + ex.Message,
                "SlideSentinel",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
        }
    }

    private static void StartPowerShell(string root, string script, bool visible)
    {
        string powerShell = Path.Combine(
            Environment.SystemDirectory,
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe");
        if (!File.Exists(powerShell)) powerShell = "powershell.exe";

        ProcessStartInfo info = new ProcessStartInfo
        {
            FileName = powerShell,
            Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"",
            WorkingDirectory = root,
            UseShellExecute = visible,
            CreateNoWindow = !visible,
            WindowStyle = visible ? ProcessWindowStyle.Normal : ProcessWindowStyle.Hidden
        };
        Process.Start(info);
    }
}
