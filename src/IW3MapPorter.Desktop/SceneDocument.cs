using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Media;
using System.Windows.Media.Media3D;
using System.Windows.Media.Imaging;
using System.Windows;

namespace IW3MapPorter.Desktop;

public sealed class MapEntity
{
    public int Id { get; set; }
    public string Classname { get; set; } = "";
    public string Targetname { get; set; } = "";
    public double[]? Origin { get; set; }
    public double[] Angles { get; set; } = [0, 0, 0];
    public bool IsSpawn { get; set; }
    public Dictionary<string, string> Properties { get; set; } = [];
    [JsonIgnore] public bool HasPosition => Origin is { Length: 3 } && Origin.All(double.IsFinite);
    [JsonIgnore] public string Label => $"#{Id} · {Classname}" + (Targetname.Length > 0 ? " · " + Targetname : "") + (HasPosition ? "" : " [no position]");
}

public sealed class SceneDocument
{
    public string Schema { get; set; } = "";
    public string MeshFile { get; set; } = "";
    public string Name { get; set; } = "";
    public List<MapEntity> Entities { get; set; } = [];
    public int Triangles { get; set; }
    public double[] Mins { get; set; } = [0, 0, 0];
    public double[] Maxs { get; set; } = [1, 1, 1];
    public string Rendering { get; set; } = "";
    public Dictionary<string, string> Textures { get; set; } = [];
    [JsonIgnore] public Model3DGroup Geometry { get; private set; } = new();

    public static SceneDocument Load(string metadataPath)
    {
        var doc = JsonSerializer.Deserialize<SceneDocument>(File.ReadAllText(metadataPath), Core.PorterSettings.JsonOptions)
            ?? throw new InvalidDataException("Empty scene.");
        if (doc.Schema is not ("iw3-desktop-scene/v1" or "iw3-desktop-scene/v2")) throw new InvalidDataException("Unknown scene format.");
        bool withUv = doc.Schema.EndsWith("/v2", StringComparison.Ordinal);
        if (doc.Mins.Length != 3 || doc.Maxs.Length != 3 || !doc.Mins.Concat(doc.Maxs).All(double.IsFinite))
            throw new InvalidDataException("Invalid scene bounds.");
        var folder = Path.GetDirectoryName(Path.GetFullPath(metadataPath))!;
        if (Path.GetFileName(doc.MeshFile) != doc.MeshFile) throw new InvalidDataException("The mesh file must be alongside the metadata.");
        using var reader = new BinaryReader(File.OpenRead(Path.Combine(folder, doc.MeshFile)), Encoding.UTF8);
        if (!reader.ReadBytes(8).SequenceEqual(Encoding.ASCII.GetBytes(withUv ? "IW3SCN2\0" : "IW3SCN1\0"))) throw new InvalidDataException("Invalid mesh header.");
        var groups = reader.ReadUInt32();
        if (groups > 65536) throw new InvalidDataException("Too many material groups.");
        var root = new Model3DGroup();
        root.Children.Add(new AmbientLight(Color.FromRgb(145, 145, 145)));
        root.Children.Add(new DirectionalLight(Colors.White, new Vector3D(-0.3, -0.5, -0.8)));
        long totalTriangles = 0;
        var textureMaterials = new Dictionary<string, Material>(StringComparer.OrdinalIgnoreCase);
        for (var g = 0; g < groups; g++)
        {
            var rgba = reader.ReadBytes(4);
            if (rgba.Length != 4) throw new EndOfStreamException();
            var vc = reader.ReadUInt32(); var ic = reader.ReadUInt32();
            if (vc > 20_000_000 || ic > 60_000_000 || ic % 3 != 0 || (long)vc * (withUv ? 20 : 12) + (long)ic * 4 > reader.BaseStream.Length - reader.BaseStream.Position)
                throw new InvalidDataException("Invalid mesh sizes.");
            var points = new Point3D[(int)vc];
            for (var i = 0; i < vc; i++)
            {
                float x = reader.ReadSingle(), y = reader.ReadSingle(), z = reader.ReadSingle();
                if (!float.IsFinite(x) || !float.IsFinite(y) || !float.IsFinite(z)) throw new InvalidDataException("Invalid vertex.");
                points[i] = new Point3D(x, y, z);
            }
            var uvs = new Point[withUv ? (int)vc : 0];
            if (withUv)
            {
                for (var i = 0; i < vc; i++)
                {
                    float u = reader.ReadSingle(), v = reader.ReadSingle();
                    if (!float.IsFinite(u) || !float.IsFinite(v)) throw new InvalidDataException("Invalid UV coordinates.");
                    uvs[i] = new Point(u, v);
                }
            }
            var indices = new int[(int)ic];
            for (var i = 0; i < ic; i++)
            {
                var index = reader.ReadUInt32();
                if (index >= vc) throw new InvalidDataException("Index is outside the vertex list.");
                indices[i] = (int)index;
            }
            var mesh = new MeshGeometry3D { Positions = new Point3DCollection(points), TriangleIndices = new Int32Collection(indices), TextureCoordinates = new PointCollection(uvs) };
            Brush brush = new SolidColorBrush(Color.FromArgb(rgba[3], rgba[0], rgba[1], rgba[2]));
            Material material;
            if (withUv && doc.Textures.TryGetValue(g.ToString(System.Globalization.CultureInfo.InvariantCulture), out var textureName))
            {
                if (Path.GetFileName(textureName) != textureName) throw new InvalidDataException("The texture must be alongside the scene.");
                if (!textureMaterials.TryGetValue(textureName, out material!))
                {
                    var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
                    bitmap.UriSource = new Uri(Path.Combine(folder, textureName)); bitmap.EndInit(); bitmap.Freeze();
                    var imageBrush = new ImageBrush(bitmap) { TileMode = TileMode.Tile, Viewport = new Rect(0, 0, 1, 1), ViewportUnits = BrushMappingMode.Absolute };
                    imageBrush.Freeze(); material = new DiffuseMaterial(imageBrush); material.Freeze();
                    textureMaterials.Add(textureName, material);
                }
            }
            else material = new DiffuseMaterial(brush);
            var model = new GeometryModel3D(mesh, material) { BackMaterial = material };
            model.Freeze(); root.Children.Add(model); totalTriangles += ic / 3;
        }
        if (reader.BaseStream.Position != reader.BaseStream.Length || totalTriangles != doc.Triangles)
            throw new InvalidDataException("Mesh length or triangle count does not match the scene report.");
        root.Freeze(); doc.Geometry = root;
        return doc;
    }
}
