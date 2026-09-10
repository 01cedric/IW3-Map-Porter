using System.IO;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace IW3MapPorter.Core;

public sealed class MapMenuEntry : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    private int slot, maxNameLength;
    private string stockMap = "", mapId = "", displayName = "", description = "", previewLoadFf = "", previewImagePath = "";
    private void Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
    public int Slot { get => slot; set => Set(ref slot, value); }
    public int MaxNameLength { get => maxNameLength; set => Set(ref maxNameLength, value); }
    public string StockMap { get => stockMap; set => Set(ref stockMap, value); }
    public string MapId { get => mapId; set => Set(ref mapId, value); }
    public string DisplayName { get => displayName; set => Set(ref displayName, value); }
    public string Description { get => description; set => Set(ref description, value); }
    public string PreviewLoadFf { get => previewLoadFf; set => Set(ref previewLoadFf, value); }
    public string PreviewImagePath { get => previewImagePath; set => Set(ref previewImagePath, value); }
}
