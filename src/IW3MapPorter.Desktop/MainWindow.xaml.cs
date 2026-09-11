using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Media3D;
using System.Windows.Threading;
using IW3MapPorter.Core;
using Microsoft.Win32;

namespace IW3MapPorter.Desktop;

public partial class MainWindow : Window
{
    private PorterSettings settings = new();
    private readonly string stateDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "IW3MapPorter");
    private readonly ConcurrentQueue<BackendEvent> events = new();
    private readonly ObservableCollection<ArtifactItem> artifacts = [];
    private CancellationTokenSource? active;
    private readonly Stopwatch stopwatch = new();
    private readonly DispatcherTimer timer;
    private string? jobDirectory, pendingScene, pendingMenuInspection;
    private FxSimulationDocument? fxDocument;
    private FxEffectTimeline? fxActive;
    private List<FxPlacementRow> fxAmbient = [];
    private bool fxAmbientActive;
    private int fxAmbientSkipped;
    private int fxFrame;
    private readonly ModelVisual3D fxVisual = new();
    private readonly DispatcherTimer fxTimer;
    private readonly Dictionary<string, System.Windows.Media.Media3D.Material> fxTextureCache = new(StringComparer.OrdinalIgnoreCase);
    private bool closeAfterCancel;
    private SceneDocument? scene;
    private List<MapEntity> filteredEntities = [];
    private Point? dragPoint;
    private double yaw, pitch;

    public MainWindow()
    {
        InitializeComponent();
        SourceInitialized += (_, _) => WindowTheme.Apply(this);
        GfxPolicy.ItemsSource = new[] { "fastfile-delayed", "self-contained" };
        MaterialPolicy.ItemsSource = new[] { "fastfile-delayed", "balanced-split", "self-contained" };
        ImageFallback.ItemsSource = new[] { "reference", "neutral" };
        MaterialFallback.ItemsSource = new[] { "reference", "default" };
        LightPolicy.ItemsSource = new[] { "source", "sun-only" };
        UnsupportedFxPolicy.ItemsSource = new[] { "omit", "error" };
        ShaderCompilationPolicy.ItemsSource = new[] { "auto", "prefer", "off" };
        LayerPolicy.ItemsSource = PortalPolicy.ItemsSource = new[] { "source", "omit" };
        ArtifactList.ItemsSource = artifacts;
        try { if (File.Exists(StatePath)) settings = PorterSettings.Load(StatePath); }
        catch (Exception exc) when (exc is IOException or System.Text.Json.JsonException or InvalidDataException)
        { events.Enqueue(new("log", "Could not load the saved profile: " + exc.Message)); }
        RefreshBindings();
        timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(150) };
        timer.Tick += (_, _) => { FlushEvents(); if (active is not null) ElapsedText.Text = stopwatch.Elapsed.ToString(@"hh\:mm\:ss"); };
        timer.Start();
        fxTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(83) };
        fxTimer.Tick += (_, _) => AdvanceFx();
    }

    private string StatePath => Path.Combine(stateDirectory, "settings.json");
    private void RefreshBindings()
    {
        DataContext = null; DataContext = settings;
        MapMenuGrid.ItemsSource = settings.MapMenuEntries;
        IwdList.ItemsSource = settings.IwdPaths.ToArray(); SupportList.ItemsSource = settings.SupportZones.ToArray();
    }

    private bool UpdateInputs()
    {
        bool valid = true;
        void Walk(DependencyObject node)
        {
            if (node is TextBox text && text.GetBindingExpression(TextBox.TextProperty) is { } binding)
            { binding.UpdateSource(); if (Validation.GetHasError(text)) valid = false; }
            for (var i = 0; i < VisualTreeHelper.GetChildrenCount(node); i++) Walk(VisualTreeHelper.GetChild(node, i));
        }
        Walk(this);
        if (!valid) StatusText.Text = "Please correct the inputs highlighted in red.";
        return valid;
    }

    private void BrowseMain(object sender, RoutedEventArgs e)
    {
        if (active is not null) return;
        var dialog = new OpenFileDialog { Filter = "PC main fastfile|*.ff|All files|*.*" };
        if (dialog.ShowDialog(this) == true) SetMain(dialog.FileName);
    }
    private void SetMain(string file)
    {
        try
        {
            UpdateInputs(); var found = InputDiscovery.Discover(file);
            settings.PcFf = Path.GetFullPath(file); settings.MapName = found.MapName;
            settings.PcLoadFf = found.LoadFile ?? ""; settings.IwdPaths = found.Iwds.ToList();
            settings.OutputRoot = found.OutputRoot; RefreshBindings(); StatusText.Text = found.Message;
        }
        catch (Exception ex) { ShowError(ex.Message); }
    }
    private void DetectFiles(object sender, RoutedEventArgs e) { if (UpdateInputs()) SetMain(settings.PcFf); }

    private void BrowseFile(object sender, RoutedEventArgs e)
    {
        if (active is not null) return;
        var property = (string)((Button)sender).Tag;
        string filter = property switch
        {
            "ReportPath" or "CubemapEvidence" => "JSON|*.json|All files|*.*",
            "DonorCatalog" => "TechniqueSet donor catalog|techset-donors.json;*.json|All files|*.*",
            "ElfPath" => "PS3 ELF|*.elf|All files|*.*",
            "WorldDump" => "World dump|*.npz|All files|*.*",
            "PythonPath" => "Python|python.exe;python3.exe|EXE|*.exe",
            _ => "Fastfiles / zones|*.ff;*.bin|All files|*.*"
        };
        var dialog = new OpenFileDialog { Filter = filter };
        if (dialog.ShowDialog(this) != true) return;
        UpdateInputs(); typeof(PorterSettings).GetProperty(property)!.SetValue(settings, dialog.FileName); RefreshBindings();
    }
    private void BrowseFolder(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog();
        if (dialog.ShowDialog(this) != true) return;
        UpdateInputs(); typeof(PorterSettings).GetProperty((string)((Button)sender).Tag)!.SetValue(settings, dialog.FolderName); RefreshBindings();
    }
    private void AddFiles(List<string> list, string filter)
    {
        var dialog = new OpenFileDialog { Filter = filter, Multiselect = true };
        if (dialog.ShowDialog(this) != true) return;
        UpdateInputs();
        foreach (var file in dialog.FileNames) if (!list.Contains(file, StringComparer.OrdinalIgnoreCase)) list.Add(file);
        RefreshBindings();
    }
    private void AddIwd(object sender, RoutedEventArgs e) => AddFiles(settings.IwdPaths, "IWD archives|*.iwd");
    private void AddSupport(object sender, RoutedEventArgs e) => AddFiles(settings.SupportZones, "PS3 support zones|*.ff");
    private void RemoveIwd(object sender, RoutedEventArgs e) { if (IwdList.SelectedItem is string file) settings.IwdPaths.Remove(file); RefreshBindings(); }
    private void RemoveSupport(object sender, RoutedEventArgs e) { if (SupportList.SelectedItem is string file) settings.SupportZones.Remove(file); RefreshBindings(); }

    private void SaveProfile(object sender, RoutedEventArgs e)
    {
        if (!UpdateInputs()) return;
        var dialog = new SaveFileDialog { Filter = "IW3MapPorter profile|*.iw3profile.json", FileName = "map.iw3profile.json" };
        if (dialog.ShowDialog(this) == true) { try { settings.Save(dialog.FileName); StatusText.Text = "Profile saved."; } catch (Exception ex) { ShowError(ex.Message); } }
    }
    private void LoadProfile(object sender, RoutedEventArgs e)
    {
        if (active is not null) return;
        var dialog = new OpenFileDialog { Filter = "IW3MapPorter profile|*.json" };
        if (dialog.ShowDialog(this) != true) return;
        try { settings = PorterSettings.Load(dialog.FileName); RefreshBindings(); StatusText.Text = "Profile loaded."; }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private async void StartJob(object sender, RoutedEventArgs e)
    {
        if (active is not null || !UpdateInputs()) return;
        var command = (string)((Button)sender).Tag;
        var errors = settings.Validate(command).ToArray();
        if (errors.Length > 0) { ShowError(string.Join("\n", errors)); return; }
        try
        {
            var root = string.IsNullOrWhiteSpace(settings.OutputRoot) ? Path.Combine(stateDirectory, "runs") : Path.GetFullPath(settings.OutputRoot);
            var jobName = System.Text.RegularExpressions.Regex.IsMatch(settings.MapName ?? "", @"\Amp_[a-z0-9_]+\z") ? settings.MapName : command;
            jobDirectory = Path.Combine(root, $"{jobName}_{DateTime.Now:yyyyMMdd_HHmmss}_{Guid.NewGuid().ToString("N")[..6]}");
            settings.Save(StatePath); pendingScene = null; pendingMenuInspection = null; artifacts.Clear();
            StopFx();
            active = new CancellationTokenSource(); SetBusy(true); stopwatch.Restart();
            StatusText.Text = "Starting job…"; LogBox.AppendText($"\n=== {command} · {jobDirectory} ===\n");
            var runner = new BackendRunner(AppContext.BaseDirectory);
            var result = await runner.RunAsync(command, settings, jobDirectory, new EventQueue(events), active.Token);
            FlushEvents(int.MaxValue);
            StatusText.Text = result.Message;
            StatusIndicator.Fill = (Brush)FindResource(result.Status == "failed" ? "Error" : result.Status == "passed" ? "Success" : "Warning");
            if (pendingScene is not null)
            {
                try { await LoadSceneAsync(pendingScene); }
                catch (Exception ex) { events.Enqueue(new("log", ex.ToString())); ShowError("Could not open the preview: " + ex.Message); }
            }
            else if (command.StartsWith("mapmenu-", StringComparison.Ordinal))
            {
                if (pendingMenuInspection is not null) LoadMapMenuInspection(pendingMenuInspection);
                MainTabs.SelectedItem = MapMenuTab;
            }
            else MainTabs.SelectedItem = ResultsTab;
            if (artifacts.Count > 0) ArtifactList.SelectedIndex = 0;
        }
        catch (OperationCanceledException) { FlushEvents(int.MaxValue); StatusText.Text = "Canceled. Output in the run folder is incomplete and has not been verified."; }
        catch (Exception ex) { events.Enqueue(new("log", ex.ToString())); ShowError(ex.Message); }
        finally
        {
            stopwatch.Stop(); active?.Dispose(); active = null; SetBusy(false); RefreshBindings();
            if (closeAfterCancel) Close();
        }
    }
    private void SetBusy(bool busy)
    {
        PortInputs.IsEnabled = EmuInputs.IsEnabled = OptionInputs.IsEnabled = MapMenuInputs.IsEnabled = !busy;
        CancelButton.IsEnabled = busy; BusyBar.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        if (busy) StatusIndicator.Fill = (Brush)FindResource("Ink");
    }
    private void CancelJob(object sender, RoutedEventArgs e) { active?.Cancel(); CancelButton.IsEnabled = false; StatusText.Text = "Canceling…"; }
    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (active is not null) { e.Cancel = true; closeAfterCancel = true; active.Cancel(); return; }
        try { UpdateInputs(); settings.Save(StatePath); } catch (Exception) { }
        timer.Stop();
        fxTimer.Stop();
    }
    private sealed class EventQueue(ConcurrentQueue<BackendEvent> queue) : IProgress<BackendEvent>
    { public void Report(BackendEvent value) => queue.Enqueue(value); }
    private void FlushEvents(int limit = 500)
    {
        var text = new StringBuilder();
        for (var i = 0; i < limit && events.TryDequeue(out var ev); i++)
        {
            if (ev.Type == "stage") StatusText.Text = ev.Message;
            if (ev.Type == "artifact" && ev.Path is { } file)
            {
                if (!artifacts.Any(a => a.Path == file)) artifacts.Add(new(file, ev.Category ?? "report"));
                if (ev.Category == "map-menu-inspection") pendingMenuInspection = file;
                if (ev.Category == "scene") pendingScene = file;
                if (ev.Category == "port-report") settings.ReportPath = file;
                if (ev.Category == "fastfile" && !file.EndsWith("_load.ff", StringComparison.OrdinalIgnoreCase)) settings.Ps3Ff = file;
            }
            text.Append('[').Append(ev.Type).Append("] ").AppendLine(ev.Message);
        }
        if (text.Length == 0) return;
        LogBox.AppendText(text.ToString());
        if (LogBox.Text.Length > 250000) LogBox.Text = "[Earlier lines are available in the full desktop-session.log.]\n" + LogBox.Text[^180000..];
        LogBox.ScrollToEnd();
    }
    private void ShowError(string message) { StatusText.Text = message; StatusIndicator.Fill = (Brush)FindResource("Error"); }
    private static void OpenPath(string path) => Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
    private void OpenJob(object sender, RoutedEventArgs e) { if (jobDirectory is not null && Directory.Exists(jobDirectory)) OpenPath(jobDirectory); }
    private void OpenArtifact(object sender, RoutedEventArgs e) { if (ArtifactList.SelectedItem is ArtifactItem item && File.Exists(item.Path)) OpenPath(item.Path); }
    private void OpenArtifactFolder(object sender, RoutedEventArgs e) { if (ArtifactList.SelectedItem is ArtifactItem item) OpenPath(Path.GetDirectoryName(item.Path)!); }
    private void ArtifactSelected(object sender, SelectionChangedEventArgs e)
    {
        if (ArtifactList.SelectedItem is not ArtifactItem item) return;
        try
        {
            if (Path.GetExtension(item.Path) is ".json" or ".log" or ".txt")
            { using var reader = new StreamReader(item.Path); var chars = new char[500000]; var read = reader.ReadBlock(chars, 0, chars.Length); ReportText.Text = new string(chars, 0, read) + (reader.EndOfStream ? "" : "\n[Preview truncated; use Open file to view the complete file.]"); }
            else ReportText.Text = $"{item.Path}\n\n{new FileInfo(item.Path).Length:N0} Bytes";
        }
        catch (Exception ex) { ReportText.Text = ex.Message; }
    }
    private sealed record ArtifactItem(string Path, string Category) { public string Label => $"{System.IO.Path.GetFileName(Path)}  [{Category}]"; }
    private void CopyLog(object sender, RoutedEventArgs e) => Clipboard.SetText(LogBox.Text);
    private void ClearLog(object sender, RoutedEventArgs e) => LogBox.Clear();
    private void OnDragOver(object sender, DragEventArgs e) { e.Effects = active is null && e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None; e.Handled = true; }
    private void OnDrop(object sender, DragEventArgs e)
    {
        if (active is not null || e.Data.GetData(DataFormats.FileDrop) is not string[] files || files.Length != 1) return;
        if (Path.GetExtension(files[0]).Equals(".ff", StringComparison.OrdinalIgnoreCase)) SetMain(files[0]);
    }

    private async void OpenScene(object sender, RoutedEventArgs e)
    {
        if (active is not null) return;
        var dialog = new OpenFileDialog { Filter = "Desktop scene|*.scene.json" };
        if (dialog.ShowDialog(this) != true) return;
        try { await LoadSceneAsync(dialog.FileName); } catch (Exception ex) { ShowError(ex.Message); }
    }
    private async void InspectShader(object sender, RoutedEventArgs e)
    {
        if (active is not null) return;
        var dialog = new OpenFileDialog { Filter = "RSX / Cg program|*.bin;*.vpo;*.fpo;*.cgb|All files|*.*" };
        if (dialog.ShowDialog(this) != true) return;
        try
        {
            bool fragment = (string)((Button)sender).Tag == "fragment";
            var report = await Task.Run(() => RsxInspector.Inspect(File.ReadAllBytes(dialog.FileName), fragment));
            jobDirectory = Path.Combine(stateDirectory, "runs", "rsx_" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(jobDirectory);
            var path = Path.Combine(jobDirectory, "shader-inspection.json");
            await File.WriteAllTextAsync(path, report); artifacts.Add(new(path, "rsx"));
            MainTabs.SelectedItem = ResultsTab; ArtifactList.SelectedIndex = artifacts.Count - 1;
            StatusText.Text = "RSX shader decoded. Engine bindings and execution are not implemented yet.";
        }
        catch (Exception ex) { ShowError(ex.Message); }
    }
    private async Task LoadSceneAsync(string file)
    {
        var loadingTime = Stopwatch.StartNew();
        StatusText.Text = "Preparing 3D geometry…";
        var loaded = await Task.Run(() => SceneDocument.Load(file)); scene = loaded;
        events.Enqueue(new("log", $"WPF geometry prepared in {loadingTime.Elapsed.TotalSeconds:0.00}s."));
        StopFx();
        Viewport.Children.Clear(); Viewport.Children.Add(new ModelVisual3D { Content = loaded.Geometry });
        Viewport.Children.Add(fxVisual);
        LoadFxForScene(loaded);
        SceneStats.Text = $"{loaded.Triangles:N0} triangles · {loaded.Entities.Count:N0} Entities";
        SceneNotice.Text = loaded.Rendering;
        FilterEntities(this, new RoutedEventArgs()); MainTabs.SelectedItem = SceneTab;
        if (filteredEntities.Count > 0) EntityList.SelectedIndex = 0; else FitOverview();
        StatusText.Text = $"3D scene ready. Geometry preparation: {loadingTime.Elapsed.TotalSeconds:0.00}s.";
    }
    private void LoadFxForScene(SceneDocument loaded)
    {
        fxDocument = null; fxTextureCache.Clear(); FxList.ItemsSource = null; FxPlayButton.IsEnabled = false;
        fxAmbient = []; fxAmbientSkipped = 0; FxAmbientButton.IsEnabled = false;
        if (string.IsNullOrWhiteSpace(loaded.FxFile)) return;
        try
        {
            var path = Path.Combine(loaded.Folder, Path.GetFileName(loaded.FxFile));
            if (!File.Exists(path)) return;
            fxDocument = FxSimulationDocument.Load(path);
            var playable = fxDocument.Effects.Where(x => x.TotalParticles > 0).ToList();
            FxList.ItemsSource = playable;
            if (playable.Count > 0) { FxList.SelectedIndex = 0; FxPlayButton.IsEnabled = true; }
            events.Enqueue(new("log", $"FX preview simulation loaded: {playable.Count} playable effects (deterministic; not engine playback)."));
            LoadAmbientPlacements(loaded, playable);
        }
        catch (Exception ex) { events.Enqueue(new("log", "FX simulation could not be loaded: " + ex.Message)); }
    }

    private void LoadAmbientPlacements(SceneDocument loaded, List<FxEffectTimeline> playable)
    {
        if (string.IsNullOrWhiteSpace(loaded.FxPlacementsFile)) return;
        try
        {
            var path = Path.Combine(loaded.Folder, Path.GetFileName(loaded.FxPlacementsFile));
            if (!File.Exists(path)) return;
            var placements = FxPlacementsDocument.Load(path);
            var byName = new Dictionary<string, FxEffectTimeline>(StringComparer.OrdinalIgnoreCase);
            foreach (var effect in playable) byName.TryAdd(effect.Name.TrimStart('/'), effect);
            long particleBudget = 0;
            foreach (var row in placements.Placements)
            {
                if (string.Equals(row.Kind, "exploder", StringComparison.OrdinalIgnoreCase)) continue; // trigger-driven, not ambient
                if (!byName.TryGetValue(row.Effect.TrimStart('/'), out var timeline)) continue;
                if (fxAmbient.Count >= 64 || particleBudget + timeline.TotalParticles > 24_000) { fxAmbientSkipped++; continue; }
                row.Timeline = timeline; particleBudget += timeline.TotalParticles;
                fxAmbient.Add(row);
            }
            FxAmbientButton.IsEnabled = fxAmbient.Count > 0;
            var skippedNote = fxAmbientSkipped > 0 ? $" ({fxAmbientSkipped} beyond the preview budget)" : "";
            events.Enqueue(new("log", $"Ambient FX placements: {fxAmbient.Count} of {placements.Placements.Count} createfx placements playable{skippedNote}."));
        }
        catch (Exception ex) { events.Enqueue(new("log", "FX placements could not be loaded: " + ex.Message)); }
    }

    private void ToggleFx(object sender, RoutedEventArgs e)
    {
        if (fxActive is not null) { StopFx(); return; }
        if (FxList.SelectedItem is not FxEffectTimeline effect) return;
        StopFx();
        fxActive = effect; fxFrame = 0;
        fxTimer.Interval = TimeSpan.FromSeconds(Math.Clamp(effect.FrameStep, 0.02, 0.5));
        FxPlayButton.Content = "Stop FX";
        SceneNotice.Text = $"FX preview: {effect.Name} · deterministic simulation from fx-simulation.json (sprites only; neutral runtime values).";
        fxTimer.Start(); AdvanceFx();
    }

    private void ToggleAmbientFx(object sender, RoutedEventArgs e)
    {
        if (fxAmbientActive) { StopFx(); return; }
        if (fxAmbient.Count == 0) return;
        StopFx();
        fxAmbientActive = true; fxFrame = 0;
        var step = fxAmbient.Min(p => p.Timeline!.FrameStep);
        fxTimer.Interval = TimeSpan.FromSeconds(Math.Clamp(step, 0.02, 0.5));
        FxAmbientButton.Content = "Stop ambient";
        SceneNotice.Text = $"Ambient FX preview: {fxAmbient.Count} placed loop/oneshot effects from the map's createfx scripts (weather included; deterministic simulation)."
            + (fxAmbientSkipped > 0 ? $" {fxAmbientSkipped} placements beyond the preview budget are not shown." : "");
        fxTimer.Start(); AdvanceFx();
    }

    private void StopFx()
    {
        fxTimer.Stop(); fxActive = null; fxAmbientActive = false; fxVisual.Content = null;
        if (FxPlayButton is not null) FxPlayButton.Content = "Play FX";
        if (FxAmbientButton is not null) FxAmbientButton.Content = "Ambient FX";
    }

    private Point3D FxOrigin()
    {
        if (EntityList.SelectedItem is MapEntity { HasPosition: true } entity)
            return new Point3D(entity.Origin![0], entity.Origin[1], entity.Origin[2]);
        if (scene is not null)
            return new Point3D((scene.Mins[0] + scene.Maxs[0]) / 2, (scene.Mins[1] + scene.Maxs[1]) / 2,
                               (scene.Mins[2] + scene.Maxs[2]) / 2);
        return default;
    }

    private void AdvanceFx()
    {
        if (fxDocument is null || (fxActive is null && !fxAmbientActive)) { StopFx(); return; }
        var look = Camera.LookDirection; look.Normalize();
        var right = Vector3D.CrossProduct(look, Camera.UpDirection);
        if (right.LengthSquared < 1e-9) right = new Vector3D(1, 0, 0);
        right.Normalize();
        var up = Vector3D.CrossProduct(right, look); up.Normalize();
        var folder = fxDocument.Folder;
        System.Windows.Media.Media3D.Material? Lookup(string texture) => FxPlayback.LoadTextureMaterial(folder, texture, fxTextureCache);
        if (fxAmbientActive)
        {
            // Every placed ambient effect at its real world position, each
            // looping on its own timeline length.
            var group = new Model3DGroup();
            foreach (var placement in fxAmbient)
            {
                var timeline = placement.Timeline!;
                var origin = new Point3D(placement.Origin![0], placement.Origin[1], placement.Origin[2]);
                group.Children.Add(FxPlayback.BuildFrame(timeline, fxFrame % Math.Max(1, timeline.Frames), origin, right, up, Lookup));
            }
            group.Freeze();
            fxVisual.Content = group;
        }
        else
        {
            fxVisual.Content = FxPlayback.BuildFrame(
                fxActive!, fxFrame % Math.Max(1, fxActive!.Frames), FxOrigin(), right, up, Lookup);
        }
        if (++fxFrame >= 1_000_000_000) fxFrame = 0;
    }

    private void FilterEntities(object sender, RoutedEventArgs e)
    {
        if (EntityList is null || scene is null) return;
        var selectedId = (EntityList.SelectedItem as MapEntity)?.Id;
        var query = EntitySearch.Text.Trim();
        filteredEntities = scene.Entities.Where(x => (SpawnFilter.IsChecked != true || x.IsSpawn)
            && (PositionFilter.IsChecked != true || x.HasPosition)
            && (query.Length == 0 || x.Label.Contains(query, StringComparison.OrdinalIgnoreCase))).ToList();
        EntityList.ItemsSource = filteredEntities;
        EntityCountText.Text = $"{filteredEntities.Count} / {scene.Entities.Count} Entities";
        EntityList.SelectedItem = filteredEntities.FirstOrDefault(x => x.Id == selectedId);
    }
    private void EntitySelected(object sender, SelectionChangedEventArgs e)
    {
        if (EntityList.SelectedItem is not MapEntity entity) return;
        EntityDetails.Text = string.Join("\n", entity.Properties.Select(p => $"{p.Key} = {p.Value}"));
        MoveToEntity(entity);
    }
    private void MoveToEntity(MapEntity entity)
    {
        if (!entity.HasPosition) { SceneNotice.Text = "This entity has no valid origin position. The camera stays in place."; return; }
        if (!double.TryParse(EyeHeightBox.Text.Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var height) || !double.IsFinite(height)) height = 60;
        var origin = entity.Origin!; Camera.Position = new Point3D(origin[0], origin[1], origin[2] + height);
        pitch = entity.Angles.Length == 3 && double.IsFinite(entity.Angles[0]) ? entity.Angles[0] : 0;
        yaw = entity.Angles.Length == 3 && double.IsFinite(entity.Angles[1]) ? entity.Angles[1] : 0;
        UpdateLook(); SceneNotice.Text = $"{entity.Label} · origin {string.Join(", ", origin.Select(v => v.ToString("0.##", CultureInfo.InvariantCulture)))} · Eye height +{height:0.##}";
    }
    private void StepEntity(int direction)
    {
        if (filteredEntities.Count == 0) return;
        EntityList.SelectedIndex = (Math.Max(0, EntityList.SelectedIndex) + direction + filteredEntities.Count) % filteredEntities.Count;
        EntityList.ScrollIntoView(EntityList.SelectedItem);
    }
    private void PreviousEntity(object sender, RoutedEventArgs e) => StepEntity(-1);
    private void NextEntity(object sender, RoutedEventArgs e) => StepEntity(1);
    private void JumpToEntity(object sender, RoutedEventArgs e) { if (EntityList.SelectedItem is MapEntity entity) MoveToEntity(entity); }
    private void Overview(object sender, RoutedEventArgs e) => FitOverview();
    private void FitOverview()
    {
        if (scene is null) return;
        var center = new Point3D((scene.Mins[0] + scene.Maxs[0]) / 2, (scene.Mins[1] + scene.Maxs[1]) / 2, (scene.Mins[2] + scene.Maxs[2]) / 2);
        var distance = Math.Max(100, Enumerable.Range(0, 3).Max(i => scene.Maxs[i] - scene.Mins[i]) * 1.2);
        Camera.Position = center + new Vector3D(distance, -distance, distance * .7);
        Camera.LookDirection = center - Camera.Position; Camera.UpDirection = new(0, 0, 1);
        var v = Camera.LookDirection; yaw = Math.Atan2(v.Y, v.X) * 180 / Math.PI; pitch = -Math.Atan2(v.Z, Math.Sqrt(v.X * v.X + v.Y * v.Y)) * 180 / Math.PI;
    }
    private void UpdateLook()
    {
        pitch = Math.Clamp(pitch, -89.9, 89.9); yaw %= 360;
        double p = pitch * Math.PI / 180, y = yaw * Math.PI / 180;
        Camera.LookDirection = new Vector3D(Math.Cos(p) * Math.Cos(y), Math.Cos(p) * Math.Sin(y), -Math.Sin(p)); Camera.UpDirection = new(0, 0, 1);
    }
    private void RotateCamera(object sender, RoutedEventArgs e) { yaw += double.Parse((string)((Button)sender).Tag, CultureInfo.InvariantCulture); UpdateLook(); }
    private void ViewportDown(object sender, MouseButtonEventArgs e) { ViewportHost.Focus(); dragPoint = e.GetPosition(ViewportHost); ViewportHost.CaptureMouse(); e.Handled = true; }
    private void ViewportUp(object sender, MouseButtonEventArgs e) { dragPoint = null; ViewportHost.ReleaseMouseCapture(); }
    private void ViewportMove(object sender, MouseEventArgs e)
    {
        if (dragPoint is not { } previous || e.LeftButton != MouseButtonState.Pressed) return;
        var current = e.GetPosition(ViewportHost); yaw -= (current.X - previous.X) * .25; pitch += (current.Y - previous.Y) * .25; dragPoint = current; UpdateLook();
    }
    private void ViewportWheel(object sender, MouseWheelEventArgs e) { var forward = Camera.LookDirection; forward.Normalize(); Camera.Position += forward * (e.Delta / 120.0 * 40); e.Handled = true; }
    private void ViewportKey(object sender, KeyEventArgs e)
    {
        var forward = Camera.LookDirection; forward.Normalize(); var right = Vector3D.CrossProduct(forward, new(0, 0, 1)); right.Normalize();
        var step = Keyboard.Modifiers.HasFlag(ModifierKeys.Shift) ? 100 : 20;
        Vector3D move = e.Key switch { Key.W => forward, Key.S => -forward, Key.D => right, Key.A => -right, Key.E => new(0, 0, 1), Key.Q => new(0, 0, -1), _ => new() };
        if (e.Key == Key.Home) JumpToEntity(sender, e);
        if (move.LengthSquared > 0) { Camera.Position += move * step; e.Handled = true; }
    }
}
