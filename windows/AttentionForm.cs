namespace SuperAgentLite.Client;

/// <summary>
/// Things waiting for the user. Quiet by default; each actionable row
/// carries its action directly (DESIGN-BRIEF.md).
/// </summary>
internal sealed class AttentionForm : Form
{
    private readonly SettingsStore _settings;
    private readonly ListView _list = new();
    private readonly Label _empty = new();
    private readonly Button _approve;
    private readonly Button _reject;
    private List<ApiClient.Ticket> _tickets = new();

    public AttentionForm(SettingsStore settings)
    {
        _settings = settings;
        Text = "待处理";
        StartPosition = FormStartPosition.CenterParent;
        Size = new Size(640, 420);
        MinimumSize = new Size(480, 320);
        Font = Theme.UiFont;
        BackColor = Theme.Bg;

        _list.Dock = DockStyle.Fill;
        _list.View = View.Details;
        _list.FullRowSelect = true;
        _list.MultiSelect = false;
        _list.BorderStyle = BorderStyle.None;
        _list.Columns.Add("事项", 380);
        _list.Columns.Add("状态", 140);
        _list.SelectedIndexChanged += (_, _) => UpdateButtons();

        _empty.Text = "暂无待处理事项";
        _empty.ForeColor = Theme.TextSecondary;
        _empty.Font = Theme.UiFont;
        _empty.TextAlign = ContentAlignment.MiddleCenter;
        _empty.Dock = DockStyle.Fill;
        _empty.Visible = false;

        var bottom = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 52,
            Padding = new Padding(10, 10, 10, 10),
            BackColor = Theme.Bg,
        };
        _approve = Theme.PrimaryButton("批准");
        _approve.Size = new Size(84, 32);
        _approve.Click += async (_, _) => await ActAsync(approve: true);
        _reject = Theme.GhostButton("驳回");
        _reject.Size = new Size(84, 32);
        _reject.Margin = new Padding(8, 0, 0, 0);
        _reject.Click += async (_, _) => await ActAsync(approve: false);
        Button refresh = Theme.GhostButton("刷新");
        refresh.Size = new Size(84, 32);
        refresh.Margin = new Padding(8, 0, 0, 0);
        refresh.Click += async (_, _) => await LoadAsync();
        bottom.Controls.Add(_approve);
        bottom.Controls.Add(_reject);
        bottom.Controls.Add(refresh);

        Controls.Add(_list);
        Controls.Add(_empty);
        Controls.Add(bottom);
        _empty.BringToFront();

        Shown += async (_, _) => await LoadAsync();
        UpdateButtons();
    }

    private static string StatusLabel(string status) => status switch
    {
        "awaiting_approval" => "等待你批准",
        "needs_attention" => "需要你关注",
        _ => status,
    };

    private async Task LoadAsync()
    {
        try
        {
            _tickets = await new ApiClient(_settings).AttentionAsync();
            _list.Items.Clear();
            foreach (var t in _tickets)
            {
                var item = new ListViewItem(string.IsNullOrEmpty(t.Description) ? t.Id : t.Description);
                item.SubItems.Add(StatusLabel(t.Status));
                item.Tag = t;
                _list.Items.Add(item);
            }
            _empty.Visible = _tickets.Count == 0;
            _list.Visible = _tickets.Count > 0;
        }
        catch (ApiClient.ApiException e)
        {
            MessageBox.Show(this, e.Message + "\n（详情：" + e.Detail + "）", "待处理",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        UpdateButtons();
    }

    private ApiClient.Ticket? Selected()
    {
        if (_list.SelectedItems.Count == 0) return null;
        return _list.SelectedItems[0].Tag as ApiClient.Ticket;
    }

    private void UpdateButtons()
    {
        var t = Selected();
        bool actionable = t != null && t.Status == "awaiting_approval";
        _approve.Enabled = actionable;
        _reject.Enabled = actionable;
    }

    private async Task ActAsync(bool approve)
    {
        var t = Selected();
        if (t == null) return;
        try
        {
            var api = new ApiClient(_settings);
            if (approve) await api.ApproveAsync(t.Id); else await api.RejectAsync(t.Id);
            await LoadAsync();
        }
        catch (ApiClient.ApiException e)
        {
            MessageBox.Show(this, e.Message + "\n（详情：" + e.Detail + "）", "待处理",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }
}
