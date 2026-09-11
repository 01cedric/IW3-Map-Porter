using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;

namespace IW3MapPorter.Desktop;

/// <summary>
/// Deterministic FX preview playback over the backend's fx-simulation.json.
/// Every frame is authored data from the linked FxEffectDef simulation; this
/// class only builds camera-facing billboard geometry for one frame at a time.
/// </summary>
public sealed class FxSimulationDocument
{
    public string Schema { get; set; } = "";
    public List<FxEffectTimeline> Effects { get; set; } = [];
    public List<JsonElement> Errors { get; set; } = [];
    public int EffectCount { get; set; }
    public string Contract { get; set; } = "";
    [JsonIgnore] public string Folder { get; private set; } = "";

    public static FxSimulationDocument Load(string path)
    {
        var doc = JsonSerializer.Deserialize<FxSimulationDocument>(
                File.ReadAllText(path), Core.PorterSettings.JsonOptions)
            ?? throw new InvalidDataException("Empty FX simulation file.");
        if (doc.Schema != "iw3-fx-simulation/v1")
            throw new InvalidDataException("Unknown FX simulation format.");
        doc.Folder = Path.GetDirectoryName(Path.GetFullPath(path))!;
        foreach (var effect in doc.Effects) effect.Validate();
        return doc;
    }
}

public sealed class FxEffectTimeline
{
    public string Name { get; set; } = "";
    public double FrameStep { get; set; } = 1.0 / 12.0;
    public int Frames { get; set; }
    public List<FxElementTimeline> Elements { get; set; } = [];
    public int TotalParticles { get; set; }
    [JsonIgnore] public string Label => $"{Name} · {TotalParticles} particles";

    public void Validate()
    {
        if (FrameStep <= 0 || Frames <= 0 || Frames > 512)
            throw new InvalidDataException($"FX '{Name}' has an invalid timeline.");
        foreach (var element in Elements)
        {
            if (element.FramesData.Count != Frames)
                throw new InvalidDataException($"FX '{Name}' element frame count mismatch.");
        }
    }
}

public sealed class FxElementTimeline
{
    public int Index { get; set; }
    public string Category { get; set; } = "";
    public int ElemType { get; set; }
    public string? Material { get; set; }
    public string? Texture { get; set; }
    public int Particles { get; set; }
    [JsonPropertyName("frames")] public List<List<JsonElement>> FramesData { get; set; } = [];
}

public sealed class FxPlacementRow
{
    public string Fxid { get; set; } = "";
    public string Effect { get; set; } = "";
    public string Kind { get; set; } = "";
    public double[]? Origin { get; set; }
    public double[] Angles { get; set; } = [0, 0, 0];
    [JsonIgnore] public FxEffectTimeline? Timeline { get; set; }
    [JsonIgnore] public bool HasPosition => Origin is { Length: 3 } && Origin.All(double.IsFinite);
}

/// <summary>
/// Ambient FX placements parsed by the backend from the map's own createfx
/// scripts (fx-placements.json).  Weather is exactly such placed loops.
/// </summary>
public sealed class FxPlacementsDocument
{
    public string Schema { get; set; } = "";
    public string Map { get; set; } = "";
    public List<FxPlacementRow> Placements { get; set; } = [];
    public List<string> UnresolvedFxids { get; set; } = [];

    public static FxPlacementsDocument Load(string path)
    {
        var doc = JsonSerializer.Deserialize<FxPlacementsDocument>(
                File.ReadAllText(path), Core.PorterSettings.JsonOptions)
            ?? throw new InvalidDataException("Empty FX placements file.");
        if (doc.Schema != "iw3-fx-placements/v1")
            throw new InvalidDataException("Unknown FX placements format.");
        doc.Placements = doc.Placements.Where(p => p.HasPosition).ToList();
        return doc;
    }
}

public static class FxPlayback
{
    /// <summary>Build one frame's billboards, centered on <paramref name="origin"/>.</summary>
    public static Model3DGroup BuildFrame(FxEffectTimeline effect, int frame, Point3D origin,
                                          Vector3D cameraRight, Vector3D cameraUp,
                                          Func<string, Material?> materialLookup)
    {
        var group = new Model3DGroup();
        foreach (var element in effect.Elements)
        {
            if (frame >= element.FramesData.Count) continue;
            Material material = (element.Texture is { } texture ? materialLookup(texture) : null)
                ?? DefaultMaterial();
            var mesh = new MeshGeometry3D();
            int quad = 0;
            foreach (var row in element.FramesData[frame])
            {
                // [x, y, z, sizeX, sizeY, [r,g,b,a], rotation]
                if (row.ValueKind != JsonValueKind.Array || row.GetArrayLength() < 6) continue;
                double x = row[0].GetDouble(), y = row[1].GetDouble(), z = row[2].GetDouble();
                double halfWidth = Math.Max(0.25, row[3].GetDouble());
                double halfHeight = Math.Max(0.25, row[4].GetDouble());
                double rotation = row.GetArrayLength() > 6 ? row[6].GetDouble() : 0.0;
                var center = new Point3D(origin.X + x, origin.Y + y, origin.Z + z);
                var (sin, cos) = Math.SinCos(rotation);
                var right = cameraRight * cos + cameraUp * sin;
                var up = cameraUp * cos - cameraRight * sin;
                var rightScaled = right * halfWidth;
                var upScaled = up * halfHeight;
                int baseIndex = quad * 4;
                mesh.Positions.Add(center - rightScaled - upScaled);
                mesh.Positions.Add(center + rightScaled - upScaled);
                mesh.Positions.Add(center + rightScaled + upScaled);
                mesh.Positions.Add(center - rightScaled + upScaled);
                mesh.TextureCoordinates.Add(new Point(0, 1));
                mesh.TextureCoordinates.Add(new Point(1, 1));
                mesh.TextureCoordinates.Add(new Point(1, 0));
                mesh.TextureCoordinates.Add(new Point(0, 0));
                mesh.TriangleIndices.Add(baseIndex); mesh.TriangleIndices.Add(baseIndex + 1); mesh.TriangleIndices.Add(baseIndex + 2);
                mesh.TriangleIndices.Add(baseIndex); mesh.TriangleIndices.Add(baseIndex + 2); mesh.TriangleIndices.Add(baseIndex + 3);
                quad++;
            }
            if (quad == 0) continue;
            var model = new GeometryModel3D(mesh, material) { BackMaterial = material };
            group.Children.Add(model);
        }
        group.Freeze();
        return group;
    }

    public static Material? LoadTextureMaterial(string folder, string fileName,
                                                Dictionary<string, Material> cache)
    {
        if (Path.GetFileName(fileName) != fileName) return null;
        if (cache.TryGetValue(fileName, out var cached)) return cached;
        var path = Path.Combine(folder, fileName);
        if (!File.Exists(path)) return null;
        var bitmap = new BitmapImage();
        bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.UriSource = new Uri(path); bitmap.EndInit(); bitmap.Freeze();
        var brush = new ImageBrush(bitmap) { Stretch = Stretch.Fill, Opacity = 0.92 };
        brush.Freeze();
        var material = new EmissiveMaterial(brush);
        material.Freeze();
        cache[fileName] = material;
        return material;
    }

    private static Material DefaultMaterial()
    {
        var brush = new RadialGradientBrush(Color.FromArgb(190, 255, 235, 205),
                                            Color.FromArgb(0, 255, 235, 205));
        brush.Freeze();
        var material = new EmissiveMaterial(brush);
        material.Freeze();
        return material;
    }
}
