using System.IO;
using System.Globalization;
using System.Windows;
using System.Windows.Markup;

namespace IW3MapPorter.Desktop;
public partial class App : Application
{
    private bool reportingError;

    protected override void OnStartup(StartupEventArgs e)
    {
        var english = CultureInfo.GetCultureInfo("en-US");
        CultureInfo.DefaultThreadCurrentCulture = english;
        CultureInfo.DefaultThreadCurrentUICulture = english;
        CultureInfo.CurrentCulture = english;
        CultureInfo.CurrentUICulture = english;
        FrameworkElement.LanguageProperty.OverrideMetadata(typeof(FrameworkElement),
            new FrameworkPropertyMetadata(XmlLanguage.GetLanguage(english.IetfLanguageTag)));
        DispatcherUnhandledException += (_, args) =>
        {
            args.Handled = true;
            if (reportingError) return;
            reportingError = true;
            var folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "IW3MapPorter", "logs");
            try { Directory.CreateDirectory(folder); File.AppendAllText(Path.Combine(folder, "desktop-errors.log"), DateTimeOffset.Now + "\n" + args.Exception + "\n"); } catch (IOException) { }
            // A modal error dialog pumps the dispatcher. Stop layout before showing
            // it, otherwise another layout exception can recursively open dialogs.
            if (MainWindow is MainWindow window) window.Visibility = Visibility.Hidden;
            MessageBox.Show(args.Exception.Message + "\n\nThe application will close. Details were saved to desktop-errors.log.", "IW3MapPorter – Error", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        };
        base.OnStartup(e);
    }
}
