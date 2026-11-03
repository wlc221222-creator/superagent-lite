# Design Brief — superagent-lite clients (web / Android / Windows)

Distilled 2026-11-03 from DeepSeek's open-source dsh-superagent-prototype
(`docs/005-panel.md` principles + `src/client/client.css` metrics), adapted
to our three clients. Ward's standard: near-finished product, DSH-clean.

## Principles (from DSH panel doc, binding)

1. **Focus on what matters.** Every element answers: does anything need me
   now? is the system healthy? what happened with this piece of work?
   Anything else is removed. A number appears only when it changes what
   the user does.
2. **Quiet by default.** Healthy states take no extra space. Problems must
   be actionable and offer their action directly. Page explanations are
   at most one sentence; detail goes into tooltips/expandable areas.
3. **Honest state.** Counts equal the lists they link to; no contradictory
   states; labels say whom a row waits for and what it needs, never raw
   internal status codes.
4. **One place per concept.** Chat is chat; approvals live in the
   attention view; settings grouped by concept. Overview screens
   summarize and link, never duplicate.
5. **Consistent controls.** Same tokens and control behavior everywhere.

## Tokens (from DSH client.css metrics + DeepSeek brand)

- Brand/accent: DeepSeek blue `#4D6BFE`; pressed/hover one step darker.
- Text: primary near-black `#1F2329`-ish, secondary gray, caption lighter
  gray. Background: white / very light gray `#F7F8FA` for surfaces.
- Cards: radius 16px, border 0.5–1px solid light border token, padding 16px.
- Buttons: pill shaped (radius = half height); primary = brand fill +
  white text; secondary = ghost/outline with gray hover; small = 28px
  high, 12px text; normal = 14px text, 22px line height.
- Banners: radius 10px, state color at 10% tint background (warn amber,
  error red), 13px text.
- Attention rows: 2px colored bar on the left edge (warn/error), 12–13px
  text, action button inline on the row.
- Type scale: 11 / 12 / 13 / 14 px steps; line heights 18 / 20 / 22;
  labels weight 500.
- Dark mode: follow system (Android `values-night`, web
  `prefers-color-scheme`, Windows: keep light, note as future work).

## Per-client application

- **Chat (all three)**: user bubble = brand blue fill, white text, rounded
  16px (one corner tightened); agent message = plain text on background
  (no bubble) or light-gray card; generous line height; a single quiet
  "处理中…" state; timestamps off unless asked.
- **Attention/待处理**: list of rows that need the user, each with its
  action inline （批准/驳回）; when empty, one quiet line "暂无待处理事项"
  and nothing else.
- **Settings**: grouped cards, one save action per group, save feedback
  inline. Token field masked with a show toggle.
- **Naming/icon**: product name "SuperAgent Lite"; simple rounded-square
  icon, brand blue, white chat/agent glyph. No gradients, no clutter.
- **Errors**: human sentences ("连接不上服务器，请检查地址和网络"),
  technical detail folded behind a "详情" expander — never raw codes as
  the primary message.
