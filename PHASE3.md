# 第三阶段规格（PHASE3）

目标形态（Ward 2026-11-03 确认）：自托管版 Muse——体验类似 Muse（常驻、远端只与主
智能体聊天交互、主智能体调度并监管执行者、结果回原对话），区别是跑在用户自己的服务器上，
模型与数据自管。

## 里程碑划分
- **里程碑 A（先做）**：superagentd 守护进程 + 队列 + Worker 池 + 看门狗（组件 1 全部）。
- **里程碑 B（后做）**：Brain 规划脑 + 聊天入口（CLI REPL + HTTP）+ 最小记忆（组件 2、3、4）。

## 组件
1. **superagentd 常驻守护进程**
   - SQLite 持久队列：Ticket 状态 pending → claimed → running → done/failed。
   - Worker 池：并发上限可配置（默认 2），认领机制（原子 UPDATE ... WHERE status='pending'）、
     心跳续约、看门狗：心跳过期回收并标 failed/可重试（attempts < max_retries 时回队列）。
   - 优雅退出：SIGTERM 后停认新单，等在跑的收尾或标回 pending。
2. **主智能体规划脑（Brain）**
   - 通过与 Worker 相同的模型接口（Anthropic 兼容 /v1/messages，base_url+token 走配置，
     不写死供应商）调用"规划模型"：把用户一句话任务拆成 1..N 个 Ticket（含 depends_on），
     或判断为简单问答直接回复。
   - 模型分工可配置：日常规划用便宜模型（如 cn:deepseek-v4.1-flash），复杂规划才用贵模型
     （如 claude-opus-5-5）——按 Ward 的规矩，贵模型额度只花在规划上。默认只用便宜模型，
     贵模型需显式开启或任务被判定为复杂时才升级。
   - 汇总：所有 Ticket 完成后，Brain 把各 Worker 的结构化结果汇成一段给用户的回复。
3. **远程聊天入口（先做最小）**
   - HTTP API（标准库 http.server 起步）：`POST /chat {user_id, text}` → 返回主智能体回复
     （同步等简单任务/异步回工单号二选一，先做同步+超时降级为工单号）。
   - `GET /tickets/<id>` 查进度。鉴权先做静态 token（环境变量），为后续接消息渠道预留。
   - CLI 聊天模式 `superagent chat`：本地 REPL，方便先把交互逻辑跑通（与 HTTP 共用 Brain）。
4. **记忆（最小版）**
   - `memory.md` 纯文本档案（用户偏好、项目背景），Brain 调用时注入摘要；先手动维护，
     不做自动提炼。

## 不做（留到第四阶段）
- 消息平台接入（飞书/微信/Telegram）、Web UI、多用户、自动记忆提炼、DAG 并行调度优化。

## 验收
- `superagentd` 启动后，CLI 提交的工单被守护进程认领执行（提交端不等执行完）。
- `superagent chat` 里说"帮我在 /tmp/demo 建个 hello.txt 并写一行字"，能拆单→执行→验收→汇总回复。
- 杀掉守护进程再重启，running 中的工单被回收处理，不丢单、不重复执行已完成单。
