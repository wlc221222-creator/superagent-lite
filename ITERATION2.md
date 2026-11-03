# 第二轮迭代任务（按 REVIEW-opus.md 评审 + 第一轮验收发现）

背景：第一版 MVP 已完成（commit f6bb87b）。验收时发现两个真实问题，必须修：
- A) 用编排器以 `--worker claude` 跑任务时，子 Claude Code 没有工具授权，无法写文件，
  却礼貌回复"请授予权限"，而工单被标成 done —— **假成功**（正是评审说的"结果无契约"）。
- B) 这台机器上 `claude` 在 `~/.local/bin/claude`，不在默认 PATH，适配器直接报找不到二进制。

## 必做修改
1. **结果契约**：ClaudeCodeWorker 在发给 `claude -p` 的任务文本末尾追加固定指令，要求它在
   回复的最后一行输出 `RESULT {"status": "success", "summary": "...", "artifacts": ["相对路径"]}`
   （失败时 status 为 "failed" 并在 summary 说明原因）。Router/Supervisor 解析最后一行 RESULT：
   - 解析成功且 status=success → done（artifacts 记录进工单结果）
   - status=failed、解析失败、或进程非 0 退出 → failed，error 写明原因
   EchoWorker 同步遵守契约（直接构造 WorkerResult 即可）。在 models/store 里把 artifacts 存下来，
   `show` 能看得到。
2. **工具授权与边界（ClaudeCodeWorker）**：
   - 默认传 `--allowedTools` 一组可配置的工具（默认 `Read,Write,Edit,Bash,Glob,Grep`，
     来自 config 的 `claude_allowed_tools`）、`--add-dir <workdir>`、`--max-turns`（config，默认 25）。
   - 子进程 env 用白名单构造：只传 PATH、HOME、LANG、TERM 等必要变量 + config 显式列出的透传项；
     不要把父进程全部环境变量透传。
   - 日志写入前脱敏：把形如 `*_KEY`、`*_TOKEN`、`*_SECRET*` 的环境变量值、以及 `sk-` 开头的串替换成 `***`。
3. **二进制定位**：config 增加 `claude_bin`；解析顺序：config 显式值 → PATH 查找 → `~/.local/bin/claude`。找不到时保持现在的清晰报错。
4. **工单字段与恢复**：Ticket 增加/落实 `attempts`、`started_at`、`heartbeat_at`（若已有部分实现则补齐）；
   Router 执行时写 started_at 并周期性更新 heartbeat_at（长任务）；store 初始化时把 heartbeat
   超过阈值（config `stale_seconds`，默认 300）的 running 工单标为 failed 并在 log/error 留痕。
5. **测试补强**（tests/）：在现有 11 条基础上新增：
   - RESULT 解析成功/失败/缺失 三种情况的判定测试（可用 stub claude 脚本或直接测解析函数）
   - 工具授权参数确实出现在子进程命令行（stub 记录 argv）
   - stale running 工单在 store 初始化时被恢复为 failed
   全部测试必须通过（`python tests/test_smoke.py` 或 unittest discover）。
6. 更新 README 的相关说明（结果契约、安全边界、claude_bin 配置）。完成后 git 提交，
   提交信息注明 iteration 2。不要写任何真实 API key。

## 验收（做完自测）
- `PATH` 不含 ~/.local/bin 时，`python -m superagent_lite run "在工作目录创建 answer.txt 内容为 chain-ok" --worker claude`
  能真正创建文件、工单 done、show 能看到 artifacts 包含 answer.txt。
- 故意让任务失败（如要求写到工作目录之外的路径）时工单是 failed 而不是 done。
