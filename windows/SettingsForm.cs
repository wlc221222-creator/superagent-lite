namespace SuperAgentLite.Client;

/// <summary>Server address + token. Saved per-user under %APPDATA%.</summary>
internal sealed class SettingsForm : Form
{
    private readonly SettingsStore _settings;
    private readonly TextBox _baseUrl = new();
    private readonly TextBox _token = new();
    private readonly Label _status = new();

    public SettingsForm(SettingsStore settings)
    {
        _settings = settings;
        Text = "设置";
        StartPosition = FormStartPosition.CenterParent;
        Size = new Size(480, 330);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        Font = Theme.UiFont;
        BackColor = Theme.Bg;

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(18),
            ColumnCount = 1,
            RowCount = 7,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        layout.Controls.Add(new Label
        {
            Text = "服务器地址（主智能体所在的 Linux 服务器）",
            Font = Theme.UiFontBold,
            ForeColor = Theme.TextPrimary,
            AutoSize = true,
        });
        _baseUrl.Text = _settings.BaseUrl;
        _baseUrl.PlaceholderText = "http://192.168.1.10:8765";
        _baseUrl.Dock = DockStyle.Top;
        _baseUrl.Margin = new Padding(0, 6, 0, 14);
        layout.Controls.Add(_baseUrl);

        layout.Controls.Add(new Label
        {
            Text = "访问令牌（服务器上的 SUPERAGENT_HTTP_TOKEN）",
            Font = Theme.UiFontBold,
            ForeColor = Theme.TextPrimary,
            AutoSize = true,
        });
        _token.Text = _settings.Token;
        _token.UseSystemPasswordChar = true;
        _token.Dock = DockStyle.Top;
        _token.Margin = new Padding(0, 6, 0, 14);
        layout.Controls.Add(_token);

        var buttons = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 40 };
        Button test = Theme.GhostButton("测试连接");
        test.Size = new Size(104, 34);
        test.Click += async (_, _) =>
        {
            SaveValues();
            _status.Text = "正在连接…";
            try
            {
                _status.Text = await new ApiClient(_settings).HealthAsync();
            }
            catch (ApiClient.ApiException e)
            {
                _status.Text = e.Message + "（详情：" + e.Detail + "）";
            }
        };
        Button save = Theme.PrimaryButton("保存");
        save.Size = new Size(104, 34);
        save.Margin = new Padding(10, 0, 0, 0);
        save.Click += (_, _) =>
        {
            SaveValues();
            DialogResult = DialogResult.OK;
            Close();
        };
        buttons.Controls.Add(test);
        buttons.Controls.Add(save);
        layout.Controls.Add(buttons);

        _status.ForeColor = Theme.TextSecondary;
        _status.Font = Theme.UiFontSmall;
        _status.AutoSize = true;
        _status.MaximumSize = new Size(430, 0);
        layout.Controls.Add(_status);

        Controls.Add(layout);
    }

    private void SaveValues()
    {
        _settings.BaseUrl = _baseUrl.Text.Trim();
        _settings.Token = _token.Text.Trim();
        _settings.Save();
    }
}
