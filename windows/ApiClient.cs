using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace SuperAgentLite.Client;

/// <summary>
/// Client for the superagent-lite HTTP API. Error messages are human
/// sentences first; the technical detail is kept in <see cref="ApiException.Detail"/>.
/// </summary>
internal sealed class ApiClient
{
    public sealed record Ticket(string Id, string Description, string Status);

    public sealed class ApiException : Exception
    {
        public string Detail { get; }
        public ApiException(string message, string detail) : base(message) => Detail = detail;
    }

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMinutes(10) };
    private readonly SettingsStore _settings;

    public ApiClient(SettingsStore settings) => _settings = settings;

    public async Task<string> HealthAsync()
    {
        using JsonDocument doc = await RequestAsync("GET", "/health", null, auth: false).ConfigureAwait(false);
        string auth = doc.RootElement.TryGetProperty("auth", out var a) ? a.GetString() ?? "?" : "?";
        return "连接正常（鉴权：" + (auth == "token" ? "已开启" : "未开启") + "）";
    }

    public async Task<string> ChatAsync(string text)
    {
        string body = JsonSerializer.Serialize(new { text, user_id = "windows" });
        using JsonDocument doc = await RequestAsync("POST", "/chat", body, auth: true).ConfigureAwait(false);
        string reply = doc.RootElement.TryGetProperty("reply", out var r) ? r.GetString() ?? "" : "";
        return string.IsNullOrEmpty(reply) ? "（主智能体没有返回内容）" : reply;
    }

    public async Task<List<Ticket>> AttentionAsync()
    {
        using JsonDocument doc = await RequestAsync("GET", "/attention", null, auth: true).ConfigureAwait(false);
        var list = new List<Ticket>();
        if (doc.RootElement.TryGetProperty("tickets", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement t in arr.EnumerateArray())
            {
                list.Add(new Ticket(
                    t.TryGetProperty("id", out var id) ? id.GetString() ?? "" : "",
                    t.TryGetProperty("description", out var d) ? d.GetString() ?? "" : "",
                    t.TryGetProperty("status", out var s) ? s.GetString() ?? "" : ""));
            }
        }
        return list;
    }

    public Task ApproveAsync(string ticketId) => ActAsync(ticketId, "approve");
    public Task RejectAsync(string ticketId) => ActAsync(ticketId, "reject");

    private async Task ActAsync(string ticketId, string action)
    {
        using JsonDocument _ = await RequestAsync(
            "POST", $"/tickets/{ticketId}/{action}", null, auth: true).ConfigureAwait(false);
    }

    private async Task<JsonDocument> RequestAsync(string method, string path, string? body, bool auth)
    {
        if (string.IsNullOrWhiteSpace(_settings.BaseUrl))
            throw new ApiException("还没有设置服务器地址", "base_url is empty");

        string url = _settings.BaseUrl.Trim().TrimEnd('/') + path;
        using var req = new HttpRequestMessage(new HttpMethod(method), url);
        if (auth && !string.IsNullOrWhiteSpace(_settings.Token))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _settings.Token.Trim());
        if (body != null)
            req.Content = new StringContent(body, Encoding.UTF8, "application/json");

        HttpResponseMessage resp;
        try
        {
            resp = await _http.SendAsync(req).ConfigureAwait(false);
        }
        catch (Exception e)
        {
            throw new ApiException("连接不上服务器，请检查地址和网络", e.Message);
        }

        using (resp)
        {
            string text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (resp.StatusCode == System.Net.HttpStatusCode.Unauthorized)
                throw new ApiException("访问令牌不正确，请在设置里检查", "HTTP 401: " + text);
            if (!resp.IsSuccessStatusCode)
            {
                string serverError = "";
                try
                {
                    using JsonDocument err = JsonDocument.Parse(text);
                    if (err.RootElement.TryGetProperty("error", out var e2))
                        serverError = e2.GetString() ?? "";
                    else if (err.RootElement.TryGetProperty("reply", out var e3))
                        serverError = e3.GetString() ?? ""; // Brain errors: {reply, mode:"error"}
                }
                catch { /* keep the generic message */ }
                throw new ApiException(
                    string.IsNullOrEmpty(serverError) ? "服务器返回了错误" : "服务器：" + serverError,
                    $"HTTP {(int)resp.StatusCode}: {text}");
            }
            return JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
        }
    }
}
