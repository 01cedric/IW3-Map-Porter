using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace IW3MapPorter.Core;

public sealed record BackendEvent(string Type, string Message = "", string? Path = null, string? Category = null, string? Status = null);
public sealed record BackendResult(int ExitCode, string Status, string Message);

public sealed class BackendRunner(string applicationDirectory)
{
    public string BackendDirectory { get; } = Path.Combine(applicationDirectory, "backend");

    public async Task<BackendResult> RunAsync(string command, PorterSettings settings, string jobDirectory,
        IProgress<BackendEvent> progress, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(jobDirectory);
        var requestPath = Path.Combine(jobDirectory, "request.json");
        var request = new { schema_version = 1, command, settings, output_dir = Path.GetFullPath(jobDirectory) };
        await File.WriteAllTextAsync(requestPath, JsonSerializer.Serialize(request, PorterSettings.JsonOptions), cancellationToken);
        var bridge = Path.Combine(BackendDirectory, "gui_bridge.py");
        if (!File.Exists(bridge)) throw new FileNotFoundException("The bundled backend folder is missing.", bridge);
        var bundledPython = Path.Combine(applicationDirectory, "runtime", "python", "python.exe");
        var executable = !string.IsNullOrWhiteSpace(settings.PythonPath) ? settings.PythonPath
            : File.Exists(bundledPython) ? bundledPython : OperatingSystem.IsWindows() ? "py" : "python3";
        var start = new ProcessStartInfo(executable)
        {
            WorkingDirectory = BackendDirectory,
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        if (executable == "py") start.ArgumentList.Add("-3");
        start.ArgumentList.Add("-u");
        start.ArgumentList.Add(bridge);
        start.ArgumentList.Add("--request");
        start.ArgumentList.Add(requestPath);
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        start.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        start.Environment["PYTHONPATH"] = BackendDirectory;
        start.Environment["PYTHONHASHSEED"] = "0";
        using var process = new Process { StartInfo = start };
        using var log = new StreamWriter(Path.Combine(jobDirectory, "desktop-session.log"), false, new UTF8Encoding(false)) { AutoFlush = true };
        object sync = new();
        BackendEvent? terminal = null;
        void Receive(string line, bool stderr)
        {
            BackendEvent ev;
            try
            {
                ev = stderr ? new("log", line) : JsonSerializer.Deserialize<BackendEvent>(line,
                    new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? new("log", line);
            }
            catch (JsonException) { ev = new("log", line); }
            lock (sync)
            {
                log.WriteLine($"{DateTimeOffset.Now:O} [{ev.Type}] {line}");
                if (ev.Type == "result") terminal = ev;
            }
            progress.Report(ev);
        }
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!process.Start()) throw new InvalidOperationException("Could not start Python.");
            using var registration = cancellationToken.Register(() =>
            {
                try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) { }
                catch (System.ComponentModel.Win32Exception) { }
            });
            async Task Drain(StreamReader reader, bool stderr)
            {
                while (await reader.ReadLineAsync(cancellationToken) is { } line) Receive(line, stderr);
            }
            await Task.WhenAll(Drain(process.StandardOutput, false), Drain(process.StandardError, true), process.WaitForExitAsync(cancellationToken));
            cancellationToken.ThrowIfCancellationRequested();
            var final = terminal;
            if (final is null) return new(process.ExitCode, "failed", "Backend exited without a completion report. See the log for details.");
            if (process.ExitCode != 0 || final.Status == "failed") return new(process.ExitCode, "failed", final.Message);
            return new(process.ExitCode, final.Status ?? "unproven", final.Message);
        }
        catch (OperationCanceledException)
        {
            progress.Report(new("log", "Canceled. Incomplete files remain in this job's run folder only."));
            throw;
        }
    }
}
