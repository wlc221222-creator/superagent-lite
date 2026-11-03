# superagent-lite 规格（第一版 MVP）

## 一句话定位
运行在自己服务器上的「主智能体」：远端用户只和主智能体交互；主智能体理解任务后，把复杂任务
下发给执行型智能体（如 Claude Code、Codex），并负责调度、监管和验收。设计参考 DeepSeek
Super-agent 的 Router/Worker 双层架构。目标是未来开源（先跑通 MVP）。

## 架构（参考 Super-agent 思路）
- **Router（主智能体）**：唯一对外的入口。接收任务、维护全局上下文、把任务拆成 Ticket、
  分派给 Worker、汇总结果回复用户。保持「人只跟一个智能体说话」。
- **Ticket（工单）**：每个任务一张工单：id、描述、状态（pending/running/done/failed）、
  指派的 Worker、工作目录、超时、结果摘要、完整日志路径。落盘到 SQLite，进程重启不丢。
- **Worker（执行层）**：上下文隔离，每个 Ticket 一个独立工作目录。Worker 是适配器：
  - `ClaudeCodeWorker`：子进程调用本机 `claude -p "<任务>"`（非交互模式），捕获 stdout/stderr，
    支持超时 kill、指定模型。
  - `CodexWorker`：先做接口占位（同样的接口，run() 暂抛 NotImplementedError 并说明原因）。
  - `EchoWorker`：测试用假 Worker，不依赖任何外部模型，保证骨架可离线自测。
- **Supervisor（监管）**：由主智能体执行分派时负责：超时控制、失败记录、结果回收。
  （后续再做自动重试与 Watchdog，MVP 只预留接口/字段。）
- **事件/日志**：每张 Ticket 的执行日志写到 `runtime/logs/<ticket_id>.log`，便于事后审计。

## MVP 必须实现
1. Python 3.12 项目，尽量只用标准库（不装重依赖）。包名 `superagent_lite`。
2. 模块划分（保持简单清晰）：
   - `superagent_lite/models.py` — Ticket 数据结构与状态枚举
   - `superagent_lite/store.py` — SQLite 工单存储（建表、增、改状态、查列表）
   - `superagent_lite/workers/base.py` — Worker 抽象接口（name、run(ticket, workdir) -> 结果）
   - `superagent_lite/workers/claude_code.py` — Claude Code 适配器（子进程、超时、日志）
   - `superagent_lite/workers/codex.py` — 占位适配器
   - `superagent_lite/workers/echo.py` — 测试用适配器
   - `superagent_lite/router.py` — 主智能体：submit(text, worker=...) 建单分派、list、result
   - `superagent_lite/cli.py` — 命令行入口
   - `main.py` 或 `pyproject.toml` 提供 `superagent` 命令等价入口（`python -m superagent_lite ...`）
3. CLI 至少支持：
   - `python -m superagent_lite run "<任务描述>" --worker echo|claude` → 建单、执行、打印结果摘要
   - `python -m superagent_lite list` → 列出历史工单及状态
   - `python -m superagent_lite show <ticket_id>` → 看某张工单详情与结果
4. 配置：`config.example.yaml` 或 `.json`（标准库能解析的优先 JSON）给出示例：默认 worker、
   claude 模型、超时秒数、runtime 目录。代码从 `config.json` 读（不存在时用默认值）。
5. `tests/test_smoke.py`：用 EchoWorker 跑通「建单→执行→完成→查询」的冒烟测试，
   用 `python -m pytest` 或直接 `python tests/test_smoke.py` 能跑（优先不依赖 pytest 也能跑）。
6. `README.md`：项目定位、架构图（文字版即可）、快速开始、如何接 Claude Code、
   后续路线图（Codex 适配、HTTP API、消息渠道、Watchdog、开源许可待定）。
7. 代码里不要写任何真实 API key；Claude Code 的 key/地址走它自己的 `~/.claude/settings.json`。

## 验收标准
- `python -m superagent_lite run "写一个测试任务" --worker echo` 能完成并在 list 里看到 done。
- 冒烟测试通过。
- 目录结构清晰，`git init` 并做一次初始提交（git 用户信息缺失时用 `agent <agent@localhost>` 占位）。

## 不在 MVP 范围
- Web UI、HTTP 服务、多用户权限、消息平台接入、自动重试/Watchdog 实体实现（只预留）。
