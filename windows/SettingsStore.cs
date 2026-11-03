using System.Text.Json;

namespace SuperAgentLite.Client;

/// <summary>Server address + token, stored per-user in %APPDATA%.</summary>
internal sealed class SettingsStore
{
    public string BaseUrl { get; set; } = "";
    public string Token { get; set; } = "";

    private static string FilePath
    {
        get
        {
            string dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "SuperAgentLite");
            Directory.CreateDirectory(dir);
            return Path.Combine(dir, "settings.json");
        }
    }

    public static SettingsStore Load()
    {
        try
        {
            if (File.Exists(FilePath))
            {
                var s = JsonSerializer.Deserialize<SettingsStore>(File.ReadAllText(FilePath));
                if (s != null) return s;
            }
        }
        catch
        {
            // A corrupt settings file should never block startup.
        }
        return new SettingsStore();
    }

    public void Save()
    {
        File.WriteAllText(FilePath, JsonSerializer.Serialize(this,
            new JsonSerializerOptions { WriteIndented = true }));
    }

    public bool IsConfigured => !string.IsNullOrWhiteSpace(BaseUrl);
}
