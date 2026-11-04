using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

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

    // _http is for ordinary calls; streams get their own client so the
    // streaming call can read the body incrementally (ResponseHeadersRead).
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMinutes(10) };
    private readonly HttpClient _streamHttp = new(new SocketsHttpHandler
    {
        ConnectTimeout = TimeSpan.FromSeconds(15),
    })
    { Timeout = TimeSpan.FromMinutes(10) };
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

    /// <summary>
    /// Chat through /chat/stream. <paramref name="onEvent"/> runs on the
    /// caller's thread as each SSE event arrives; the result is the final
    /// done/error payload. If the stream fails before any event, the
    /// exception is thrown and the caller can retry with <see cref="ChatAsync"/>.
    /// </summary>
    public async Task<JsonElement> ChatStreamAsync(string text, Action<string, JsonElement> onEvent)
    {
        if (string.IsNullOrWhiteSpace(_settings.BaseUrl))
            throw new ApiException("还没有设置服务器地址", "base_url is empty");

        string url = _settings.BaseUrl.Trim().TrimEnd('/') + "/chat/stream";
        using var req = new HttpRequestMessage(HttpMethod.Post, url);
        if (!string.IsNullOrWhiteSpace(_settings.Token))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _settings.Token.Trim());
        req.Headers.Accept.ParseAdd("text/event-stream");
        req.Content = new StringContent(
            JsonSerializer.Serialize(new { text, user_id = "windows" }),
            Encoding.UTF8, "application/json");

        HttpResponseMessage resp;
        try
        {
            resp = await _streamHttp.SendAsync(req, HttpCompletionOption.ResponseHeadersRead)
                .ConfigureAwait(false);
        }
        catch (Exception e)
        {
            throw new ApiException("连接不上服务器，请检查地址和网络", e.Message);
        }

        using (resp)
        {
            if (!resp.IsSuccessStatusCode)
            {
                string body = "";
                try { body = await resp.Content.ReadAsStringAsync().ConfigureAwait(false); } catch { }
                if (resp.StatusCode == System.Net.HttpStatusCode.Unauthorized)
                    throw new ApiException("访问令牌不正确，请在设置里检查", "HTTP 401: " + body);
                string serverError = ParseErrorMessage(body);
                throw new ApiException(
                    string.IsNullOrEmpty(serverError) ? "服务器返回了错误" : "服务器：" + serverError,
                    $"HTTP {(int)resp.StatusCode}: {body}");
            }

            string? eventName = null;
            var data = new StringBuilder();
            JsonElement last = default;
            bool gotFinal = false;
            try
            {
                await using Stream stream = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
                using var reader = new StreamReader(stream, Encoding.UTF8);
                while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
                {
                    if (line.Length == 0)
                    {
                        // Blank line ends a frame.
                        JsonElement? ev = DispatchEvent(eventName, data.ToString());
                        eventName = null;
                        data.Clear();
                        if (ev is not { } current) continue;
                        string type = EventType(current);
                        onEvent(type, current);
                        if (type is "done" or "error")
                        {
                            last = current;
                            gotFinal = true;
                            break;
                        }
                        continue;
                    }
                    if (line.StartsWith(':')) continue; // SSE comment / keep-alive
                    if (line.StartsWith("event:", StringComparison.Ordinal))
                        eventName = line[6..].Trim();
                    else if (line.StartsWith("data:", StringComparison.Ordinal))
                    {
                        if (data.Length > 0) data.Append('\n');
                        data.Append(line[5..].Trim());
                    }
                    // id:/retry: fields are irrelevant here.
                }
            }
            catch (Exception e)
            {
                throw new ApiException("连接中断，没有收到完整回复", e.Message);
            }

            if (!gotFinal)
                throw new ApiException("连接中断，没有收到完整回复", "stream ended without a done event");
            return last;
        }
    }

    /// <summary>
    /// Build an event from one SSE frame. The type normally rides inside
    /// the JSON ("type"), but fall back to the SSE event: field if absent.
    /// </summary>
    private static JsonElement? DispatchEvent(string? eventName, string data)
    {
        if (data.Length == 0) return null;
        JsonObject obj;
        try { obj = JsonNode.Parse(data)?.AsObject() ?? new JsonObject(); }
        catch { return null; } // malformed frame; keep reading
        if (eventName != null && !obj.ContainsKey("type"))
            obj["type"] = eventName;
        return JsonSerializer.SerializeToElement(obj);
    }

    private static string EventType(JsonElement ev) =>
        ev.ValueKind == JsonValueKind.Object && ev.TryGetProperty("type", out var t)
            ? t.GetString() ?? "" : "";

    private static string ParseErrorMessage(string body)
    {
        try
        {
            using JsonDocument err = JsonDocument.Parse(body);
            if (err.RootElement.TryGetProperty("error", out var e2)) return e2.GetString() ?? "";
            if (err.RootElement.TryGetProperty("reply", out var e3)) return e3.GetString() ?? "";
        }
        catch { /* keep the generic message */ }
        return "";
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
