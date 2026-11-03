namespace SuperAgentLite.Client;

/// <summary>Chat window: talk to the main agent on the server.</summary>
internal sealed class MainForm : Form
{
    private readonly RichTextBox _transcript = new();
    private readonly TextBox _input = new();
    private readonly Button _send;
    private SettingsStore _settings = SettingsStore.Load();
    private bool _sending;

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
        try
        {
            string reply = await new ApiClient(_settings).ChatAsync(text);
            ReplaceLastLine("主智能体", reply, Theme.TextPrimary);
        }
        catch (ApiClient.ApiException e)
        {
            ReplaceLastLine("主智能体", e.Message + "\n（详情：" + e.Detail + "）", Theme.Error);
        }
        finally
        {
            _sending = false;
            _send.Enabled = true;
            _input.Focus();
        }
    }

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
