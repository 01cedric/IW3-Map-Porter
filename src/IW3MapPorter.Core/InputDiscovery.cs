using System.Text.RegularExpressions;

namespace IW3MapPorter.Core;

public sealed record DiscoveryResult(string MapName, string? LoadFile, IReadOnlyList<string> Iwds, string OutputRoot, string Message);

public static class InputDiscovery
{
    public static string NormalizeStem(string stem) => Regex.Replace(stem.Trim(), @"(?:\s*\(\d+\))+$", "")
        .Replace("_original_pc", "", StringComparison.OrdinalIgnoreCase).Trim().ToLowerInvariant();

    public static DiscoveryResult Discover(string mainPath)
    {
        var main = Path.GetFullPath(mainPath);
        if (!File.Exists(main)) throw new FileNotFoundException("PC fastfile is missing.", main);
        var dir = Path.GetDirectoryName(main)!;
        var name = NormalizeStem(Path.GetFileNameWithoutExtension(main));
        if (name.EndsWith("_load", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Select the main fastfile, not the _load.ff.");
        var files = Directory.GetFiles(dir);
        var loads = files.Where(p => Path.GetExtension(p).Equals(".ff", StringComparison.OrdinalIgnoreCase)
            && NormalizeStem(Path.GetFileNameWithoutExtension(p)) == name + "_load").ToArray();
        var iwds = files.Where(p => Path.GetExtension(p).Equals(".iwd", StringComparison.OrdinalIgnoreCase)
            && (NormalizeStem(Path.GetFileNameWithoutExtension(p)) == name
                || (name.StartsWith("mp_", StringComparison.Ordinal) && NormalizeStem(Path.GetFileNameWithoutExtension(p)) == name[3..]))).ToArray();
        // Never silently choose between differently numbered copies.
        var load = loads.Length == 1 ? loads[0] : null;
        var chosenIwds = iwds.Length == 1 ? iwds : [];
        var message = loads.Length > 1 || iwds.Length > 1
            ? "Multiple matching copies found; select the intended IWD/_load.ff explicitly."
            : "Matching companion files detected. You can add missing files manually.";
        return new(name, load, chosenIwds, Path.Combine(dir, "ps3_port"), message);
    }
}
