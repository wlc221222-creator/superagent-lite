"""The single-file web chat page served at ``GET /``.

No build step, no CDN, no external resource: one HTML document with inline
CSS and vanilla JavaScript.  It talks to the same token-authenticated API as
every other client:

* ``POST /chat`` with the message;
* ``GET  /tickets/<id>`` to poll progress after a ticket was queued;
* ``GET  /attention`` for the read-only "needs attention" list (tickets
  awaiting approval and tickets whose semantic review was inconclusive);
* the artifact endpoints to list and download what a worker produced.

The attention list is display-only: approving a gated ticket stays on the
CLI (``approve <id>``) and the HTTP control path
(``POST /tickets/<id>/approve``), not in this page.

The token is kept in ``sessionStorage`` only, never in the URL, so it does not
leak through history, referrers or proxy logs.  The page itself is public --
without a token it renders, and every API call answers 401.
"""

from __future__ import annotations

CHAT_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SuperAgent Lite</title>
<style>
  /* Design tokens: DESIGN-BRIEF.md (distilled from DSH panel metrics). */
  :root {
    color-scheme: light dark;
    --brand: #4d6bfe;
    --brand-pressed: #3b54d8;
    --bg: #ffffff;
    --surface: #f7f8fa;
    --panel: #ffffff;
    --text: #1f2329;
    --muted: #646a73;
    --line: #e5e8ec;
    --user: #4d6bfe;
    --ok: #1a7f4b;
    --bad: #d64545;
    --warn: #e8a33d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --brand: #6b85ff; --brand-pressed: #4d6bfe;
      --bg: #17181a; --surface: #232427; --panel: #1d1e21;
      --text: #e8eaed; --muted: #9aa0a6; --line: #33363b;
      --user: #4d6bfe;
      --ok: #58c48a; --bad: #e06060; --warn: #e8a33d;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.65 -apple-system, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  }
  #app { max-width: 820px; margin: 0 auto; min-height: 100vh; display: flex; flex-direction: column; }
  header { padding: 16px 16px 8px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  header h1 { font-size: 16px; margin: 0 10px 0 0; font-weight: 600; letter-spacing: .2px; }
  header .hint { color: var(--muted); font-size: 12px; }
  .token-row { display: flex; gap: 8px; margin-left: auto; }
  input, button, textarea { font: inherit; }
  input[type="password"], input[type="text"] {
    background: var(--surface); color: var(--text); border: 0.5px solid var(--line);
    border-radius: 20px; padding: 7px 14px; font-size: 13px;
  }
  #token { width: 180px; }
  button {
    background: var(--brand); color: #fff; border: 0; border-radius: 20px;
    padding: 8px 18px; cursor: pointer; font-size: 14px;
  }
  button:hover { background: var(--brand-pressed); }
  button:disabled { opacity: .55; cursor: default; }
  button.secondary { background: transparent; color: var(--text); border: 0.5px solid var(--line); }
  button.secondary:hover { background: var(--surface); }
  main { flex: 1; padding: 8px 16px 16px; overflow-y: auto; display: flex; flex-direction: column; }
  .msg { margin: 6px 0; padding: 10px 14px; max-width: 84%; align-self: flex-start;
         border-radius: 4px 16px 16px 16px; background: var(--surface);
         white-space: pre-wrap; word-break: break-word; }
  .msg.user { background: var(--user); color: #fff; align-self: flex-end;
              border-radius: 16px 16px 4px 16px; }
  .msg .meta { color: var(--muted); font-size: 11px; margin-bottom: 3px; white-space: normal; }
  .msg.user .meta { color: rgba(255,255,255,.75); }
  .msg .meta .ticket { font-family: ui-monospace, Menlo, Consolas, monospace; }
  .progress { margin-top: 8px; padding-top: 8px; border-top: 0.5px solid var(--line);
              font-size: 12.5px; color: var(--muted); white-space: normal; }
  .progress .row { display: flex; justify-content: space-between; gap: 10px; padding: 2px 0; }
  .state-done { color: var(--ok); }
  .state-failed { color: var(--bad); }
  .artifacts { margin-top: 6px; }
  .artifacts a { color: var(--brand); margin-right: 12px; cursor: pointer;
                 text-decoration: underline; font-size: 13px; }
  .msg.user .artifacts a { color: #fff; }
  .error { color: var(--bad); }
  .state-awaiting_approval { color: var(--warn); font-weight: 600; }
  .attention { margin: 8px 16px 0; padding: 14px 16px; border: 0.5px solid var(--line);
               border-radius: 16px; background: var(--panel); font-size: 13px; }
  .attention h2 { font-size: 12px; margin: 0 0 8px; color: var(--muted); font-weight: 600; }
  .attention .row { padding: 4px 0 4px 10px; border-left: 2px solid var(--warn); margin: 6px 0; }
  .attention .tag { display: inline-block; min-width: 116px; color: var(--muted); }
  .attention .id { font-family: ui-monospace, Menlo, Consolas, monospace; }
  .attention .how { color: var(--muted); margin-top: 8px; font-size: 12px; }
  footer { padding: 10px 16px 18px; }
  .composer { display: flex; gap: 8px; align-items: flex-end; }
  textarea {
    flex: 1; min-height: 44px; max-height: 180px; resize: vertical; background: var(--surface);
    color: var(--text); border: 0.5px solid var(--line); border-radius: 16px; padding: 10px 14px;
  }
  #send { height: 44px; padding: 0 22px; }
  .footnote { color: var(--muted); font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>SuperAgent Lite</h1>
    <span class="hint">主智能体控制台</span>
    <div class="token-row">
      <input id="token" type="password" placeholder="访问 token" autocomplete="off">
      <button id="save-token" class="secondary" type="button">保存</button>
    </div>
  </header>
  <section id="attention" class="attention" hidden>
    <h2>需要关注 / needs attention（只读）</h2>
    <div id="attention-rows"></div>
    <div class="how">审批请用 CLI <code>approve &lt;id&gt;</code> /
      <code>reject &lt;id&gt;</code> 或 API <code>POST /tickets/&lt;id&gt;/approve</code>。</div>
  </section>
  <main id="log">
    <div class="msg">
      <div class="meta">agent</div>
      你好，我是主智能体。输入任务，我会拆成工单交给执行代理；完成后在这里汇总结果。
      若已设置 <code>SUPERAGENT_HTTP_TOKEN</code>，请先在右上角填入 token。
    </div>
  </main>
  <footer>
    <div class="composer">
      <textarea id="message" placeholder="描述你的任务，Enter 发送，Shift+Enter 换行"></textarea>
      <button id="send" type="button">发送</button>
    </div>
    <div class="footnote">token 仅保存在本标签页的 sessionStorage 中，不会写进 URL。</div>
  </footer>
</div>
<script>
(function () {
  "use strict";

  var POLL_MS = 1500;
  var MAX_POLLS = 240;

  var logEl = document.getElementById("log");
  var tokenEl = document.getElementById("token");
  var messageEl = document.getElementById("message");
  var sendEl = document.getElementById("send");

  try { tokenEl.value = sessionStorage.getItem("superagent_token") || ""; } catch (e) { /* ignore */ }

  function headers() {
    var h = { "Content-Type": "application/json" };
    var token = tokenEl.value.trim();
    if (token) { h["Authorization"] = "Bearer " + token; }
    return h;
  }

  function api(path, options) {
    options = options || {};
    options.headers = headers();
    return fetch(path, options).then(function (response) {
      return response.text().then(function (raw) {
        var data = null;
        try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = { error: raw }; }
        if (!response.ok) {
          var message = (data && (data.error || data.reply)) || ("HTTP " + response.status);
          throw new Error(message);
        }
        return data;
      });
    });
  }

  function bubble(kind, text) {
    var el = document.createElement("div");
    el.className = "msg" + (kind === "user" ? " user" : "");
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = kind === "user" ? "you" : "agent";
    var body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    el.appendChild(meta);
    el.appendChild(body);
    logEl.appendChild(el);
    el.scrollIntoView({ block: "end" });
    return el;
  }

  function setBusy(busy) {
    sendEl.disabled = busy;
    messageEl.disabled = busy;
  }

  function terminal(status) {
    return status === "done" || status === "failed"
      || status === "awaiting_approval";
  }

  function refreshAttention() {
    var box = document.getElementById("attention");
    var rows = document.getElementById("attention-rows");
    if (!tokenEl.value.trim()) { box.hidden = true; return; }
    api("/attention").then(function (data) {
      var items = (data && data.tickets) || [];
      rows.textContent = "";
      box.hidden = items.length === 0;
      items.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "row";
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = item.reason === "awaiting_approval"
          ? "待审批" : "审查未定论";
        var id = document.createElement("span");
        id.className = "id";
        id.textContent = item.id;
        var desc = document.createElement("span");
        desc.textContent = "  " + String(item.description || "").split(String.fromCharCode(10))[0].slice(0, 90);
        row.appendChild(tag);
        row.appendChild(id);
        row.appendChild(desc);
        rows.appendChild(row);
      });
    }).catch(function () { box.hidden = true; });
  }

  function renderProgress(el, ticket, index) {
    var state = ticket.status || "unknown";
    var cls = state === "done" ? " state-done"
      : (state === "failed" ? " state-failed"
      : (state === "awaiting_approval" ? " state-awaiting_approval" : ""));
    var line = document.createElement("div");
    line.className = "row";
    var name = document.createElement("span");
    name.className = "ticket";
    name.textContent = "工单 " + (index + 1) + " · " + ticket.id;
    var status = document.createElement("span");
    status.className = "state" + cls;
    status.textContent = state;
    line.appendChild(name);
    line.appendChild(status);
    el.appendChild(line);
    if (ticket.result_summary) {
      var summary = document.createElement("div");
      summary.textContent = ticket.result_summary;
      el.appendChild(summary);
    }
    if (ticket.error) {
      var error = document.createElement("div");
      error.className = "error";
      error.textContent = ticket.error;
      el.appendChild(error);
    }
    if (ticket.review_status && ticket.review_status !== "none") {
      var review = document.createElement("div");
      review.textContent = "审查 review: " + ticket.review_status
        + ((ticket.review_reasons || []).length
           ? " — " + ticket.review_reasons.join("; ") : "");
      if (ticket.review_status === "rejected"
          || ticket.review_status === "inconclusive") {
        review.className = "error";
      }
      el.appendChild(review);
    }
    if (state === "awaiting_approval") {
      var wait = document.createElement("div");
      wait.textContent = "等待审批：用 CLI approve " + ticket.id
        + " 或 POST /tickets/" + ticket.id + "/approve。";
      el.appendChild(wait);
    }
    var artifacts = ticket.artifacts || [];
    if (artifacts.length) {
      var box = document.createElement("div");
      box.className = "artifacts";
      artifacts.forEach(function (name) {
        var link = document.createElement("a");
        link.textContent = "下载 " + name;
        link.href = "#";
        link.addEventListener("click", function (event) {
          event.preventDefault();
          download(ticket.id, name);
        });
        box.appendChild(link);
      });
      el.appendChild(box);
    }
  }

  function download(ticketId, name) {
    var encoded = name.split("/").map(encodeURIComponent).join("/");
    var url = "/tickets/" + encodeURIComponent(ticketId) + "/artifacts/" + encoded;
    fetch(url, { headers: headers() }).then(function (response) {
      if (!response.ok) { throw new Error("下载失败 HTTP " + response.status); }
      return response.blob();
    }).then(function (blob) {
      var objectUrl = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = objectUrl;
      link.download = name.split("/").pop();
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 4000);
    }).catch(function (error) {
      alert(error.message);
    });
  }

  function pollTickets(bubbleEl, ids) {
    var panel = document.createElement("div");
    panel.className = "progress";
    bubbleEl.appendChild(panel);
    var attempts = 0;

    function tick() {
      attempts += 1;
      Promise.all(ids.map(function (id) {
        return api("/tickets/" + encodeURIComponent(id)).catch(function () { return null; });
      })).then(function (tickets) {
        panel.textContent = "";
        var allDone = true;
        tickets.forEach(function (ticket, index) {
          if (!ticket) {
            var missing = document.createElement("div");
            missing.className = "row";
            missing.textContent = "工单 " + (index + 1) + " · 状态未知";
            panel.appendChild(missing);
            allDone = false;
            return;
          }
          renderProgress(panel, ticket, index);
          if (!terminal(ticket.status)) { allDone = false; }
        });
        if (allDone) { return; }
        if (attempts >= MAX_POLLS) {
          var stop = document.createElement("div");
          stop.textContent = "停止轮询；可用工单 id 稍后再查看。";
          panel.appendChild(stop);
          return;
        }
        setTimeout(tick, POLL_MS);
      });
    }
    tick();
  }

  function send() {
    var text = messageEl.value.trim();
    if (!text) { return; }
    var token = tokenEl.value.trim();
    try { sessionStorage.setItem("superagent_token", token); } catch (e) { /* ignore */ }

    bubble("user", text);
    messageEl.value = "";
    setBusy(true);
    var pending = bubble("agent", "…");

    api("/chat", {
      method: "POST",
      body: JSON.stringify({ text: text, user_id: "web" })
    }).then(function (data) {
      pending.remove();
      var answer = bubble("agent", data.reply || "(empty reply)");
      var meta = answer.querySelector(".meta");
      var bits = ["mode: " + (data.mode || "?")];
      if (data.model) { bits.push("model: " + data.model); }
      if (data.timed_out) { bits.push("仍有工单在执行"); }
      meta.textContent = "agent · " + bits.join(" · ");
      var ids = data.ticket_ids || [];
      if (ids.length) {
        var idLine = document.createElement("div");
        idLine.className = "meta";
        idLine.textContent = "tickets: " + ids.join(", ");
        answer.insertBefore(idLine, answer.querySelector(".body").nextSibling);
        pollTickets(answer, ids);
      }
    }).catch(function (error) {
      pending.remove();
      var failed = bubble("agent", "请求失败：" + error.message);
      failed.querySelector(".meta").textContent = "agent · error";
    }).then(function () {
      setBusy(false);
      messageEl.focus();
      refreshAttention();
    });
  }

  sendEl.addEventListener("click", send);
  document.getElementById("save-token").addEventListener("click", function () {
    try { sessionStorage.setItem("superagent_token", tokenEl.value.trim()); } catch (e) { /* ignore */ }
    this.textContent = "已保存";
    var button = this;
    setTimeout(function () { button.textContent = "保存"; }, 1200);
    refreshAttention();
  });
  messageEl.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  messageEl.focus();
  refreshAttention();
})();
</script>
</body>
</html>
"""
