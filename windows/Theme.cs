namespace SuperAgentLite.Client;

/// <summary>Design tokens (DESIGN-BRIEF.md): DSH-clean, one brand blue.</summary>
internal static class Theme
{
    public static readonly Color Brand = Color.FromArgb(0x4D, 0x6B, 0xFE);
    public static readonly Color BrandPressed = Color.FromArgb(0x3B, 0x54, 0xD8);
    public static readonly Color Bg = Color.White;
    public static readonly Color Surface = Color.FromArgb(0xF7, 0xF8, 0xFA);
    public static readonly Color TextPrimary = Color.FromArgb(0x1F, 0x23, 0x29);
    public static readonly Color TextSecondary = Color.FromArgb(0x64, 0x6A, 0x73);
    public static readonly Color Border = Color.FromArgb(0xE5, 0xE8, 0xEC);
    public static readonly Color Warn = Color.FromArgb(0xE8, 0xA3, 0x3D);
    public static readonly Color Error = Color.FromArgb(0xD6, 0x45, 0x45);

    public static readonly Font UiFont = new("Segoe UI", 9.5f);
    public static readonly Font UiFontSmall = new("Segoe UI", 8.5f);
    public static readonly Font UiFontBold = new("Segoe UI", 9.5f, FontStyle.Bold);

    public static Button PrimaryButton(string text)
    {
        var b = new Button
        {
            Text = text,
            FlatStyle = FlatStyle.Flat,
            BackColor = Brand,
            ForeColor = Color.White,
            Font = UiFont,
            Cursor = Cursors.Hand,
        };
        b.FlatAppearance.BorderSize = 0;
        b.FlatAppearance.MouseDownBackColor = BrandPressed;
        b.FlatAppearance.MouseOverBackColor = BrandPressed;
        return b;
    }

    public static Button GhostButton(string text)
    {
        var b = new Button
        {
            Text = text,
            FlatStyle = FlatStyle.Flat,
            BackColor = Surface,
            ForeColor = TextPrimary,
            Font = UiFont,
            Cursor = Cursors.Hand,
        };
        b.FlatAppearance.BorderColor = Border;
        b.FlatAppearance.BorderSize = 1;
        return b;
    }
}
