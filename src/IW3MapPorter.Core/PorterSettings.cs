using System.Collections.ObjectModel;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.IO;

namespace IW3MapPorter.Core;

public sealed class PorterSettings
{
    public int SchemaVersion { get; set; } = 1;
    public string PcFf { get; set; } = "";
    public string MapName { get; set; } = "";
    public string PcLoadFf { get; set; } = "";
    public string TextureLibraryDirectory { get; set; } = "";
    public List<string> IwdPaths { get; set; } = [];
    public string OutputRoot { get; set; } = "";
    public string Ps3LoadReference { get; set; } = "";
    public List<string> SupportZones { get; set; } = [];
    public string PythonPath { get; set; } = "";
    public string ReportPath { get; set; } = "";
    public bool RuntimeCompatible { get; set; } = true;
    public bool RequireFullFidelity { get; set; }
    public bool BootIsolation { get; set; }
    public bool RequireLoadFile { get; set; } = true;
    public string GfxworldResourcePolicy { get; set; } = "fastfile-delayed";
    public string MaterialImageResourcePolicy { get; set; } = "fastfile-delayed";
    public string UnresolvedImagePolicy { get; set; } = "reference";
    public string UnresolvedMaterialPolicy { get; set; } = "reference";
    public string PrimaryLightPolicy { get; set; } = "source";
    public string VertexLayerPolicy { get; set; } = "source";
    public string UnsupportedFxPolicy { get; set; } = "omit";
    public string ShaderCompilation { get; set; } = "auto";
    public string DonorCatalog { get; set; } = "";
    public string PortalPolicy { get; set; } = "source";
    public string ZoneBudgetBytes { get; set; } = "off";
    public bool AutomaticZoneBudget { get; set; } = true;
    public int TextureMaxEdge { get; set; } = 0;
    public int ImageBudgetMinDimension { get; set; } = 32;
    public string CubemapEvidence { get; set; } = "";
    public string Ps3Ff { get; set; } = "";
    public string Ps3LoadFf { get; set; } = "";
    public string ElfPath { get; set; } = "";
    public string SupportDirectory { get; set; } = "";
    public bool ForceSceneRebuild { get; set; }
    public string MapMenuFf { get; set; } = "";
    public string MapMenuOriginalFf { get; set; } = "";
    public ObservableCollection<MapMenuEntry> MapMenuEntries { get; set; } = [];
    public string WorldDump { get; set; } = "";

    public static JsonSerializerOptions JsonOptions { get; } = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
        PropertyNameCaseInsensitive = true
    };

    public static PorterSettings Load(string file)
    {
        var settings = JsonSerializer.Deserialize<PorterSettings>(File.ReadAllText(file), JsonOptions)
            ?? throw new InvalidDataException("Empty profile.");
        if (settings.SchemaVersion != 1) throw new InvalidDataException("Unknown profile version.");
        settings.IwdPaths ??= [];
        settings.SupportZones ??= [];
        settings.MapMenuEntries ??= [];
        return settings;
    }

    public void Save(string file)
    {
        var path = Path.GetFullPath(file);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".tmp-" + Guid.NewGuid().ToString("N");
        File.WriteAllText(temp, JsonSerializer.Serialize(this, JsonOptions));
        File.Move(temp, path, true);
    }

    public IEnumerable<string> Validate(string command)
    {
        if (command is "port" or "analyze" or "link")
        {
            if (!string.IsNullOrWhiteSpace(TextureLibraryDirectory) && !Directory.Exists(TextureLibraryDirectory))
                yield return "PC texture library directory is missing.";
        }
        if (command is "port" or "analyze")
        {
            if (!string.IsNullOrWhiteSpace(DonorCatalog) && !File.Exists(DonorCatalog))
                yield return "TechniqueSet donor catalog (techset-donors.json) is missing.";
            if (ShaderCompilation is not ("auto" or "prefer" or "off"))
                yield return "Shader compilation must be auto, prefer or off.";
            if (!Regex.IsMatch(MapName ?? "", @"\Amp_[a-z0-9_]+\z"))
                yield return "Map name must start with mp_ followed by lowercase letters, digits and underscores.";
            if (!File.Exists(PcFf)) yield return "PC main fastfile is missing.";
            if (command == "port" && RequireLoadFile && !File.Exists(PcLoadFf))
                yield return "PC _load.ff is missing. Disable the complete-pair option for a main-only build.";
            foreach (var path in IwdPaths.Where(p => !File.Exists(p))) yield return "IWD is missing: " + path;
            if (!string.IsNullOrWhiteSpace(PcLoadFf) && !File.Exists(PcLoadFf)) yield return "PC _load.ff was not found.";
            if (!string.IsNullOrWhiteSpace(Ps3LoadReference) && !File.Exists(Ps3LoadReference)) yield return "PS3 loading-zone reference is missing.";
            if (!string.IsNullOrWhiteSpace(Ps3LoadReference) && string.IsNullOrWhiteSpace(PcLoadFf))
                yield return "A PS3 loading-zone reference requires a PC _load.ff.";
        }
        if (command == "verify" && !File.Exists(ReportPath)) yield return "Port report is missing.";
        if (command is "port" or "verify")
        {
            foreach (var path in SupportZones.Where(p => !File.Exists(p))) yield return "Support zone is missing: " + path;
            if (!AutomaticZoneBudget && !string.Equals(ZoneBudgetBytes?.Trim(), "off", StringComparison.OrdinalIgnoreCase)
                && (!long.TryParse(ZoneBudgetBytes, out var budget) || budget < 1))
                yield return "Zone budget must be a positive byte count or off.";
            if (TextureMaxEdge < 0 || ImageBudgetMinDimension < 1) yield return "Invalid texture limits.";
        }
        if (command is "emulate" or "link" or "techsets")
        {
            if (!File.Exists(Ps3Ff)) yield return "PS3 fastfile is missing.";
            if (!File.Exists(ElfPath)) yield return "Decrypted EBOOT in ELF format is missing.";
        }
        if (command is "link" or "techsets")
        {
            if (!string.IsNullOrWhiteSpace(Ps3LoadFf) && !File.Exists(Ps3LoadFf)) yield return "PS3 loading companion is missing.";
            if (!Directory.Exists(SupportDirectory)) yield return "Retail support folder is missing.";
            if (!Regex.IsMatch(MapName ?? "", @"\Amp_[a-z0-9_]+\z")) yield return "Enter a valid map name for the linker.";
        }
        if (command is "mapmenu-inspect" or "mapmenu-write")
        {
            if (!File.Exists(MapMenuFf)) yield return "PS3 ui_mp.ff is missing.";
            if (command == "mapmenu-write" && MapMenuEntries.Count == 0) yield return "Inspect the UI file first.";
        }
        if (command == "preview" && !File.Exists(WorldDump)) yield return "World dump (.npz) is missing.";
    }
}
