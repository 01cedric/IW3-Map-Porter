using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Collections.Specialized;
using System.IO;
using System.Text.Json;
using IW3MapPorter.Core;

var root = args.Length > 0 ? Path.GetFullPath(args[0]) : throw new ArgumentException("Project root required.");
var python = args.Length > 1 ? args[1] : "python3";
int count = 0;
void Check(bool condition, string label) { if (!condition) throw new Exception("FAIL: " + label); Console.WriteLine("PASS: " + label); count++; }
var temp = Path.Combine(Path.GetTempPath(), "IW3 checks ü & space " + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temp);
try
{
    var main = Path.Combine(temp, "mp_test(1).ff"); File.WriteAllText(main, "fixture");
    File.WriteAllText(Path.Combine(temp, "mp_test_load(1).ff"), "fixture");
    File.WriteAllText(Path.Combine(temp, "mp_test(1).iwd"), "fixture");
    var discovered = InputDiscovery.Discover(main);
    Check(discovered.MapName == "mp_test" && discovered.LoadFile is not null && discovered.Iwds.Count == 1, "companion discovery with download suffix");
    File.WriteAllText(Path.Combine(temp, "mp_test_load(2).ff"), "fixture");
    Check(InputDiscovery.Discover(main).LoadFile is null, "ambiguous companion is not silently selected");
    var raid = Path.Combine(temp, "mp_bo2raid3.ff"); File.WriteAllText(raid, "fixture");
    File.WriteAllText(Path.Combine(temp, "bo2raid3.iwd"), "fixture");
    Check(InputDiscovery.Discover(raid).Iwds.Single().EndsWith("bo2raid3.iwd"), "IWD without mp_ prefix is detected");
    File.WriteAllText(Path.Combine(temp, "mp_bo2raid3.iwd"), "fixture");
    Check(InputDiscovery.Discover(raid).Iwds.Count == 0, "prefixed and unprefixed IWD ambiguity requires selection");
    Check(VersionInfo.Number == File.ReadAllText(Path.Combine(root, "VERSION")).Trim(), "build version follows VERSION file");
    var settings = new PorterSettings { PcFf = main, MapName = "mp_test\n", PythonPath = python };
    Check(settings.Validate("analyze").Any(), "map name newline rejected");
    settings.MapName = "mp_test"; settings.Save(Path.Combine(temp, "profile.json"));
    Check(PorterSettings.Load(Path.Combine(temp, "profile.json")).PcFf == main, "profile roundtrip with Unicode path");
    Check(settings.AutomaticZoneBudget && settings.TextureMaxEdge == 0, "automatic budget without a fixed texture cap by default");
    var legacyProfile = Path.Combine(temp, "legacy-profile.json");
    File.WriteAllText(legacyProfile, "{\"schema_version\":1,\"zone_budget_bytes\":\"90000000\"}");
    var migrated = PorterSettings.Load(legacyProfile);
    Check(migrated.AutomaticZoneBudget && migrated.ZoneBudgetBytes == "90000000", "old profile enables automatic budget and retains manual override");
    migrated.AutomaticZoneBudget = false;
    migrated.Save(legacyProfile);
    Check(!PorterSettings.Load(legacyProfile).AutomaticZoneBudget, "explicit manual budget survives profile roundtrip");
    migrated.ZoneBudgetBytes = "invalid";
    Check(migrated.Validate("verify").Any(e => e.StartsWith("Zone budget")), "invalid manual budget rejected");
    migrated.AutomaticZoneBudget = true;
    Check(!migrated.Validate("verify").Any(e => e.StartsWith("Zone budget")), "inactive manual override cannot block automatic budgeting");
    var mapChanges = new List<NotifyCollectionChangedAction>();
    settings.MapMenuEntries.CollectionChanged += (_, e) => mapChanges.Add(e.Action);
    var mapEntry = new MapMenuEntry { Slot = 17, MapId = "mp_test", DisplayName = "Test" };
    settings.MapMenuEntries.Add(mapEntry);
    var propertyChanges = new List<string?>();
    mapEntry.PropertyChanged += (_, e) => propertyChanges.Add(e.PropertyName);
    mapEntry.PreviewLoadFf = "[Embedded in UI]";
    mapEntry.Slot = 18;
    settings.MapMenuOriginalFf = main;
    settings.Save(Path.Combine(temp, "menu-profile.json"));
    var reopened = PorterSettings.Load(Path.Combine(temp, "menu-profile.json"));
    Check(reopened.MapMenuOriginalFf == main && reopened.MapMenuEntries.Single().PreviewLoadFf == "[Embedded in UI]", "map menu state survives profile roundtrip");
    settings.MapMenuEntries.Remove(mapEntry);
    Check(mapChanges.SequenceEqual(new[] { NotifyCollectionChangedAction.Add, NotifyCollectionChangedAction.Remove }), "map additions and removals notify WPF binding");
    Check(propertyChanges.SequenceEqual(new[] { "PreviewLoadFf", "Slot" }), "picture changes and row reindexing notify WPF binding");
    Check(RsxInspector.SwapFragmentLanes(RsxInspector.SwapFragmentLanes(0x12345678)) == 0x12345678, "RSX fragment lanes inverse");
    Check(Math.Abs(RsxInspector.DecodePlacement(0x7fc00000).Z - 1) < 1e-6, "packed placement +Z");
    Check(Math.Abs(RsxInspector.DecodeVertex(0x7fc00000).Z - 32704f / 32767f) < 1e-6, "RSX vertex normalization distinct from placement");
    Check(RsxInspector.EffectiveTextureRemap(0x1AAE4, 0x86) == 0xAAE4, "BC1 remap ignores special-format high word");
    var vertex = new byte[80]; BinaryPrimitives.WriteUInt32BigEndian(vertex.AsSpan(0x18), 16); BinaryPrimitives.WriteUInt32BigEndian(vertex.AsSpan(0x1c), 64); vertex[79] = 1;
    using var decoded = JsonDocument.Parse(RsxInspector.Inspect(vertex, false));
    Check(decoded.RootElement.GetProperty("complete_instruction_stream").GetBoolean(), "real upstream vertex decoder end marker");
    var fragment = (byte[])vertex.Clone(); Array.Clear(fragment, 64, 16); BinaryPrimitives.WriteUInt32BigEndian(fragment.AsSpan(64), RsxInspector.SwapFragmentLanes(1));
    using var fp = JsonDocument.Parse(RsxInspector.Inspect(fragment, true));
    Check(fp.RootElement.GetProperty("complete_instruction_stream").GetBoolean(), "real upstream fragment decoder end marker");
    var queue = new ConcurrentQueue<BackendEvent>(); var progress = new QueueProgress(queue);
    var runner = new BackendRunner(root);
    var doctor = await runner.RunAsync("doctor", settings, Path.Combine(temp, "doctor ü & files"), progress, CancellationToken.None);
    Check(doctor.ExitCode == 0 && doctor.Status == "passed" && queue.Any(e => e.Type == "artifact"), "real Python bridge under Unicode/spaces/metacharacters");
    var invalid = await runner.RunAsync("analyze", settings, Path.Combine(temp, "invalid map"), progress, CancellationToken.None);
    Check(invalid.ExitCode != 0 && invalid.Status == "failed", "malformed fastfile cannot become success");
    var stub = Path.Combine(temp, "stub"); Directory.CreateDirectory(Path.Combine(stub, "backend"));
    var script = Path.Combine(stub, "backend", "gui_bridge.py"); File.WriteAllText(script, "print('no terminal result')\n");
    var stubRunner = new BackendRunner(stub);
    var absent = await stubRunner.RunAsync("doctor", settings, Path.Combine(temp, "no-result"), progress, CancellationToken.None);
    Check(absent.Status == "failed", "exit zero without terminal report rejected");
    File.WriteAllText(script, "import time, subprocess, sys\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\nprint('child '+str(child.pid),flush=True)\ntime.sleep(30)\n");
    using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(400));
    var cancellationTime = System.Diagnostics.Stopwatch.StartNew();
    bool cancelled = false;
    try { await stubRunner.RunAsync("doctor", settings, Path.Combine(temp, "cancel"), progress, cancel.Token); }
    catch (OperationCanceledException) { cancelled = true; }
    Check(cancelled && cancellationTime.Elapsed < TimeSpan.FromSeconds(5), "prompt cancellation with a running child process");
    Console.WriteLine($"SUMMARY: {count} checks passed.");
}
finally { Directory.Delete(temp, true); }

sealed class QueueProgress(ConcurrentQueue<BackendEvent> queue) : IProgress<BackendEvent>
{ public void Report(BackendEvent value) => queue.Enqueue(value); }
