using System.Collections.ObjectModel;
using System.IO;
using Microsoft.Win32;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using IW3MapPorter.Core;

namespace IW3MapPorter.Desktop;

public partial class MainWindow
{
    private void LoadMapMenuInspection(string path)
    {
        using var document = JsonDocument.Parse(File.ReadAllText(path));
        var rows = document.RootElement.GetProperty("slots").Deserialize<ObservableCollection<MapMenuEntry>>(PorterSettings.JsonOptions)
            ?? throw new InvalidDataException("The map menu inspection has no slots.");
        settings.MapMenuEntries = rows;
        if (document.RootElement.TryGetProperty("original_source", out var original) && original.GetString() is { Length: > 0 } originalPath) settings.MapMenuOriginalFf = originalPath;
        RefreshBindings();
        MapMenuNotice.Text = $"{rows.Count} maps loaded. Added rows appear in Custom Maps. Up to 16 added maps; names can use 64 characters.";
    }

    private void AddMapMenuEntry(object sender, RoutedEventArgs e)
    {
        if (!CommitMapMenuEdits()) return;
        if (settings.MapMenuEntries.Count < 16) { ShowError("Inspect a supported UI first."); return; }
        if (settings.MapMenuEntries.Count >= 32) { ShowError("Custom Maps supports 16 added maps (32 choices total)."); return; }
        var entry = new MapMenuEntry { Slot = settings.MapMenuEntries.Count + 1, StockMap = "Custom", MaxNameLength = 64 };
        settings.MapMenuEntries.Add(entry);
        MapMenuGrid.SelectedItem = entry;
        MapMenuGrid.ScrollIntoView(entry);
    }

    private async void SetMapMenuLoadingPicture(object sender, RoutedEventArgs e)
    {
        if (active is not null || !CommitMapMenuEdits() || !UpdateInputs()) return;
        if (MapMenuGrid.SelectedItem is not MapMenuEntry entry) { ShowError("Select a map row first."); return; }
        if (entry.Slot <= 16 && entry.MapId == entry.StockMap) { ShowError("Select a custom map row. Unchanged stock maps keep their original pictures."); return; }
        var dialog = new OpenFileDialog { Title = "Select the converted PS3 loading zone", Filter = "PS3 loading fastfile|*.ff|All files|*.*" };
        if (dialog.ShowDialog(this) != true) return;
        try
        {
            jobDirectory = Path.Combine(stateDirectory, "runs", "menu-picture-" + Guid.NewGuid().ToString("N"));
            active = new CancellationTokenSource(); SetBusy(true); stopwatch.Restart();
            var request = new PorterSettings { PythonPath = settings.PythonPath, MapName = entry.MapId.Trim(), Ps3LoadFf = dialog.FileName };
            var runner = new BackendRunner(AppContext.BaseDirectory);
            var result = await runner.RunAsync("mapmenu-picture", request, jobDirectory, new EventQueue(events), active.Token);
            FlushEvents(int.MaxValue);
            if (result.ExitCode != 0 || result.Status != "passed") { ShowError(result.Message); return; }
            using var report = JsonDocument.Parse(File.ReadAllText(Path.Combine(jobDirectory, "map-menu-picture.json")));
            var picture = report.RootElement;
            if (string.IsNullOrWhiteSpace(entry.MapId)) entry.MapId = picture.GetProperty("map_id").GetString()!;
            entry.PreviewLoadFf = dialog.FileName;
            entry.PreviewImagePath = picture.GetProperty("preview_png").GetString()!;
            settings.Save(StatePath);
            MapMenuNotice.Text = result.Message;
            StatusText.Text = "Picture extracted and fitted. Export modified ui_mp.ff to embed it.";
        }
        catch (OperationCanceledException) { StatusText.Text = "Picture import canceled. The previous assignment is retained."; }
        catch (Exception ex) { ShowError(ex.Message); }
        finally
        {
            stopwatch.Stop(); active?.Dispose(); active = null; SetBusy(false);
            if (closeAfterCancel) Close();
        }
    }

    private void RemoveMapMenuEntry(object sender, RoutedEventArgs e)
    {
        if (!CommitMapMenuEdits()) return;
        if (MapMenuGrid.SelectedItem is not MapMenuEntry entry) return;
        if (entry.Slot <= 16) { ShowError("Keep the original 16 rows. You can change their names or map IDs."); return; }
        settings.MapMenuEntries.Remove(entry);
        for (var i = 0; i < settings.MapMenuEntries.Count; i++) settings.MapMenuEntries[i].Slot = i + 1;
    }

    private bool CommitMapMenuEdits() =>
        MapMenuGrid.CommitEdit(DataGridEditingUnit.Cell, true) &&
        MapMenuGrid.CommitEdit(DataGridEditingUnit.Row, true);

    private void RunMapMenuJob(object sender, RoutedEventArgs e)
    {
        if (!CommitMapMenuEdits()) return;
        StartJob(sender, e);
    }
}
