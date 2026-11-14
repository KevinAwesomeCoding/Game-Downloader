using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

internal static class Launcher
{
    private static volatile Process startingProcess;
    private static volatile bool stopSplash;

    private static void ShowSplash()
    {
        var watch = Stopwatch.StartNew();
        using (var form = new Form())
        using (var timer = new System.Windows.Forms.Timer()) {
            form.Text = "Game Installer";
            form.StartPosition = FormStartPosition.CenterScreen;
            form.FormBorderStyle = FormBorderStyle.FixedDialog;
            form.MaximizeBox = false;
            form.MinimizeBox = false;
            form.ControlBox = false;
            form.ClientSize = new System.Drawing.Size(400, 125);
            form.BackColor = System.Drawing.Color.FromArgb(18, 25, 39);
            var label = new Label { Dock = DockStyle.Fill, TextAlign = System.Drawing.ContentAlignment.MiddleCenter,
                ForeColor = System.Drawing.Color.White, Font = new System.Drawing.Font("Segoe UI", 11),
                Text = "Opening Game Installer...\nPlease wait." };
            form.Controls.Add(label);
            timer.Interval = 250;
            timer.Tick += delegate {
                label.Text = "Opening Game Installer...\n" + (int)watch.Elapsed.TotalSeconds + " seconds elapsed\nPlease wait; you don't need to open it again.";
                Process child = startingProcess;
                if (stopSplash) { form.Close(); return; }
                if (child != null) {
                    try { child.Refresh(); if (child.HasExited || child.MainWindowHandle != IntPtr.Zero) form.Close(); }
                    catch (InvalidOperationException) { form.Close(); }
                }
            };
            timer.Start();
            Application.Run(form);
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        bool diagnostic = args.Length == 1 && args[0] == "--check-download-browser";
        if (!diagnostic) {
            var splashThread = new Thread(ShowSplash);
            splashThread.IsBackground = false;
            splashThread.SetApartmentState(ApartmentState.STA);
            splashThread.Start();
        }
        try {
            string launcher = Assembly.GetExecutingAssembly().Location;
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GI");
            Directory.CreateDirectory(root);
            string key;
            using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream("app.zip"))
            using (var sha = SHA256.Create())
                key = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
            string target = Path.Combine(root, key.Substring(0, 16));
            string exe = Path.Combine(target, "GameInstaller.exe");
            using (var mutex = new Mutex(false, "Local\\GameInstallerCache-" + key)) {
                bool locked = false;
                try {
                    try { locked = mutex.WaitOne(TimeSpan.FromMinutes(5)); }
                    catch (AbandonedMutexException) { locked = true; }
                    if (!locked) throw new IOException("Timed out preparing the application cache.");
                    if (!File.Exists(Path.Combine(target, ".complete")) || !File.Exists(exe)) {
                        string temp = Path.Combine(root, Guid.NewGuid().ToString("N").Substring(0, 16));
                        Directory.CreateDirectory(temp);
                        try {
                            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("app.zip"))
                            using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
                                zip.ExtractToDirectory(temp);
                            if (!File.Exists(Path.Combine(temp, "GameInstaller.exe"))) throw new IOException("Application is missing from the package.");
                            File.WriteAllText(Path.Combine(temp, ".complete"), key);
                            if (Directory.Exists(target)) Directory.Delete(target, true);
                            Directory.Move(temp, target);
                        } finally { if (Directory.Exists(temp)) Directory.Delete(temp, true); }
                    }
                } finally { if (locked) mutex.ReleaseMutex(); }
            }
            var info = new ProcessStartInfo(exe);
            info.UseShellExecute = false;
            info.WorkingDirectory = Path.GetDirectoryName(launcher);
            info.EnvironmentVariables["GAMEINSTALLER_LAUNCHER"] = launcher;
            // Only this internal diagnostic has arguments; normal launch has none.
            if (args.Length == 1 && args[0] == "--check-download-browser") info.Arguments = args[0];
            var child = Process.Start(info);
            startingProcess = child;
            if (args.Length == 1 && args[0] == "--check-download-browser") { child.WaitForExit(); return child.ExitCode; }
            return 0;
        } catch (Exception error) {
            stopSplash = true;
            MessageBox.Show("Game Installer could not start: " + error.Message, "Game Installer", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
