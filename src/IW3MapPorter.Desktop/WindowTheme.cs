using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace IW3MapPorter.Desktop;

internal static class WindowTheme
{
    // Keep native window behavior, including resizing, snapping and system buttons.
    public static void Apply(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        int dark = 1, caption = 0x00000000, text = 0x00EBE7E5;
        _ = DwmSetWindowAttribute(handle, 20, ref dark, sizeof(int));
        // Caption colors are supported on Windows 11; older systems ignore them.
        _ = DwmSetWindowAttribute(handle, 35, ref caption, sizeof(int));
        _ = DwmSetWindowAttribute(handle, 36, ref text, sizeof(int));
    }

    [DllImport("dwmapi.dll", PreserveSig = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern int DwmSetWindowAttribute(nint window, int attribute, ref int value, int size);
}
