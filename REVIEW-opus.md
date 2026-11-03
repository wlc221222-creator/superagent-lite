# 架构评审（claude-opus-5-5，仅规划，2026-11-03）

## 三个关键缺口／风险
1. **结果判定没有契约**——Worker 只要求返回"结果摘要"，但 `claude -p` 经常退出码为 0 而任务实为失败。没有结构化结果（退出码、成功标志、产物清单）和"由谁判定 done"的规则，工单状态不可信。
2. **安全边界缺失**——远端用户自由文本直接进入 `claude -p`，而 Claude Code 有本机文件与执行权限。需要工具白名单、路径约束、环境变量隔离、联网策略，否则等于把服务器交给提示注入。
3. **执行模型是同步 CLI**——超时 kill 意味着一次 run 阻塞进程直到超时；跨进程重启时没有遗留 `running` 工单的处理策略。

## MVP 值得补的三点
1. `WorkerResult` 定为 `{status, exit_code, summary, artifacts[]}`，要求 Worker 在 stdout 末尾输出一行 `RESULT {json}`，Supervisor 据此判定；解析不了就判 failed，不按退出码猜。
2. ClaudeWorker 增加 `--allowed-tools`、`--max-turns`、`--add-dir` 限定工作区；子进程 env 用白名单显式构造；日志写入前脱敏 `*_KEY`、`*_TOKEN`。
3. Ticket 增加 `attempts`、`started_at`、`heartbeat_at`；store 初始化时把超过心跳阈值的 `running` 工单标为 failed 并留痕。冒烟测试补 timeout、failed、重启恢复三条用例。

## 第二阶段第一件事
常驻 `superagentd`：SQLite 持久队列 + Worker 池（并发上限、认领、心跳）。这是 Router 拆解 DAG、自动重试、监控、HTTP 入口之前必须先落地的地基。拆解最小版：先给 ticket 增加 `depends_on` 字段，不急着做调度器。
