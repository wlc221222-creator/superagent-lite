using System.Text.Json;

namespace SuperAgentLite.Client;

/// <summary>Chat window: talk to the main agent on the server.</summary>
internal sealed class MainForm : Form
{
    private readonly RichTextBox _transcript = new();
    private readonly TextBox _input = new();
    private readonly Button _send;
    private readonly Label _status = new();
    private SettingsStore _settings = SettingsStore.Load();
    private bool _sending;
    // Set from the stream's background callback; read after the await to
    // decide whether a fallback /chat retry is still safe.
    private volatile bool _streamSawEvent;

    public MainForm()
    {
        Text = "SuperAgent Lite";
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(780, 580);
        MinimumSize = new Size(560, 420);
        Font = Theme.UiFont;
        BackColor = Theme.Bg;

        var toolbar = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 46,
            Padding = new Padding(10, 8, 10, 6),
            BackColor = Theme.Bg,
        };
        Button attentionBtn = Theme.GhostButton("待处理");
        attentionBtn.Size = new Size(84, 30);
        attentionBtn.Click += (_, _) => new AttentionForm(SettingsStore.Load()).ShowDialog(this);
        Button settingsBtn = Theme.GhostButton("设置");
        settingsBtn.Size = new Size(72, 30);
        settingsBtn.Click += (_, _) =>
        {
            if (new SettingsForm(_settings).ShowDialog(this) == DialogResult.OK)
                _settings = SettingsStore.Load();
        };
        toolbar.Controls.Add(attentionBtn);
        toolbar.Controls.Add(settingsBtn);

        _transcript.Dock = DockStyle.Fill;
        _transcript.ReadOnly = true;
        _transcript.BorderStyle = BorderStyle.None;
        _transcript.BackColor = Theme.Bg;
        _transcript.Font = Theme.UiFont;
        _transcript.Margin = new Padding(0);

        // One quiet progress line above the composer; hidden when idle.
        // Dock order matters: Bottom controls stack in reverse add order,
        // so add the status strip first and it sits above the input row.
        var statusStrip = new Panel
        {
            Dock = DockStyle.Bottom,
            Height = 24,
            Padding = new Padding(12, 2, 12, 0),
            BackColor = Theme.Bg,
            Visible = false,
        };
        _status.Dock = DockStyle.Fill;
        _status.AutoEllipsis = true;
        _status.Font = Theme.UiFontSmall;
        _status.ForeColor = Theme.TextSecondary;
        _status.TextAlign = ContentAlignment.MiddleLeft;
        statusStrip.Controls.Add(_status);

        var bottom = new Panel { Dock = DockStyle.Bottom, Height = 58, Padding = new Padding(10, 10, 10, 12), BackColor = Theme.Bg };
        _send = Theme.PrimaryButton("发送");
        _send.Size = new Size(84, 36);
        _send.Dock = DockStyle.Right;
        _send.Click += async (_, _) => await SendAsync();
        _input.Dock = DockStyle.Fill;
        _input.Multiline = true;
        _input.Font = Theme.UiFont;
        _input.BorderStyle = BorderStyle.FixedSingle;
        _input.KeyDown += async (_, e) =>
        {
            if (e.KeyCode == Keys.Enter && !e.Shift)
            {
                e.SuppressKeyPress = true;
                await SendAsync();
            }
        };
        bottom.Controls.Add(_input);
        bottom.Controls.Add(_send);
        // The input must not slide under the send button.
        _input.Margin = new Padding(0, 0, 8, 0);
        bottom.Resize += (_, _) => _input.Width = bottom.ClientSize.Width - _send.Width - 28;

        Controls.Add(_transcript);
        Controls.Add(statusStrip);
        Controls.Add(bottom);
        Controls.Add(toolbar);

        Shown += (_, _) =>
        {
            if (!_settings.IsConfigured)
            {
                MessageBox.Show(this, "先设置服务器地址和访问令牌。", "SuperAgent Lite",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                if (new SettingsForm(_settings).ShowDialog(this) == DialogResult.OK)
                    _settings = SettingsStore.Load();
            }
            _input.Focus();
        };
    }

    private async Task SendAsync()
    {
        if (_sending) return;
        string text = _input.Text.Trim();
        if (text.Length == 0) return;
        _settings = SettingsStore.Load();
        if (!_settings.IsConfigured)
        {
            MessageBox.Show(this, "先在设置里填写服务器地址。", "SuperAgent Lite",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        _input.Clear();
        AppendLine("我", text, Theme.Brand);
        AppendLine("主智能体", "处理中…", Theme.TextSecondary);
        _sending = true;
        _send.Enabled = false;
        _streamSawEvent = false;
        var api = new ApiClient(_settings);
        try
        {
            JsonElement final = await api.ChatStreamAsync(text, (type, ev) =>
            {
                _streamSawEvent = true;
                ShowStreamEvent(type, ev);
            });
            string reply = GetString(final, "reply");
            bool failed = GetString(final, "type") == "error";
            ReplaceLastLine("主智能体",
                reply.Length == 0
                    ? (failed ? "服务器返回了错误" : "（主智能体没有返回内容）")
                    : reply,
                failed ? Theme.Error : Theme.TextPrimary);
        }
        catch (ApiClient.ApiException e)
        {
            if (_streamSawEvent)
            {
                // The stream died mid-flight: report it where the reply goes.
                ReplaceLastLine("主智能体", e.Message + "\n（详情：" + e.Detail + "）", Theme.Error);
            }
            else
            {
                // Nothing arrived (old server, proxy blocking SSE): use /chat.
                await FallbackChatAsync(api, text);
            }
        }
        finally
        {
            SetStatus("");
            _sending = false;
            _send.Enabled = true;
            _input.Focus();
        }
    }

    /// <summary>Retry through the blocking /chat endpoint when the stream never started.</summary>
    private async Task FallbackChatAsync(ApiClient api, string text)
    {
        try
        {
            string reply = await api.ChatAsync(text);
            ReplaceLastLine("主智能体", reply, Theme.TextPrimary);
        }
        catch (ApiClient.ApiException e)
        {
            ReplaceLastLine("主智能体", e.Message + "\n（详情：" + e.Detail + "）", Theme.Error);
        }
    }

    /// <summary>One quiet line above the composer, mirroring the server's progress.</summary>
    private void ShowStreamEvent(string type, JsonElement ev)
    {
        string? text = type switch
        {
            "accepted" => "已收到",
            "planning" => "正在规划…",
            "tickets" => "已派单 " + TicketCount(ev) + " 张",
            "ticket" => "工单 #" + ShortId(GetString(ev, "id")) + " " + StatusLabel(GetString(ev, "status")),
            "done" => "",
            _ => null, // error and anything unknown: the caller clears the line
        };
        if (text == null) return;
        SetStatus(text);
    }

    private void SetStatus(string text)
    {
        if (IsDisposed) return;
        if (InvokeRequired)
        {
            // Events arrive on the stream's worker; hop to the UI thread.
            try { BeginInvoke(() => SetStatus(text)); }
            catch (InvalidOperationException) { /* window is closing */ }
            return;
        }
        _status.Text = text;
        // The strip is hidden when idle so the composer keeps its old spacing.
        if (_status.Parent is Panel strip) strip.Visible = text.Length > 0;
    }

    private static int TicketCount(JsonElement ev)
    {
        if (ev.ValueKind == JsonValueKind.Object
            && ev.TryGetProperty("ticket_ids", out var ids)
            && ids.ValueKind == JsonValueKind.Array)
            return ids.GetArrayLength();
        return ev.ValueKind == JsonValueKind.Object
               && ev.TryGetProperty("count", out var c) && c.TryGetInt32(out int n) ? n : 0;
    }

    private static string ShortId(string id) => id.Length > 8 ? id[..8] : id;

    private static string StatusLabel(string status) => status switch
    {
        "pending" => "排队中",
        "running" => "执行中",
        "awaiting_approval" => "等待你批准",
        "needs_attention" => "需要你关注",
        "done" => "已完成",
        "failed" => "失败",
        _ => status,
    };

    private static string GetString(JsonElement ev, string name) =>
        ev.ValueKind == JsonValueKind.Object
        && ev.TryGetProperty(name, out var v)
        && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? ""
            : "";

    private void AppendLine(string who, string text, Color color)
    {
        _transcript.SelectionStart = _transcript.TextLength;
        _transcript.SelectionFont = Theme.UiFontBold;
        _transcript.SelectionColor = color;
        _transcript.AppendText(who + "\n");
        _transcript.SelectionFont = Theme.UiFont;
        _transcript.SelectionColor = Theme.TextPrimary;
        _transcript.AppendText(text + "\n\n");
        _transcript.ScrollToCaret();
    }

    private void ReplaceLastLine(string who, string text, Color color)
    {
        // The placeholder block is the last "who\n处理中…\n\n" segment.
        string marker = who + "\n处理中…\n\n";
        int idx = _transcript.Text.LastIndexOf(marker, StringComparison.Ordinal);
        if (idx < 0)
        {
            AppendLine(who, text, color);
            return;
        }
        _transcript.Select(idx, marker.Length);
        _transcript.SelectedText = "";
        AppendLine(who, text, color);
    }
}
