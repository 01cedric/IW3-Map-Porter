using System.Reflection;

namespace IW3MapPorter.Core;

public static class VersionInfo
{
    public static string Number { get; } = typeof(VersionInfo).Assembly
        .GetCustomAttribute<AssemblyInformationalVersionAttribute>()!
        .InformationalVersion.Split('+')[0];
    public static string Label => $"Version {Number}";
    public static string WindowTitle => $"IW3MapPorter · {Label} · .NET 9";
    public static string WorkspaceLabel => $"{Label} · .NET 9";
}
