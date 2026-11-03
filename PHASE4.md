# 第四阶段：补齐远程交互与部署（PHASE4）

背景：前三阶段核心闭环已通。这一阶段补上一轮汇报里明确缺的三块：网页界面、产物下载、
消息渠道骨架，外加可部署的常驻服务定义。保持只用 Python 标准库。

## 必做
1. **网页聊天界面**：`serve` 的 HTTP 服务在 `GET /` 返回一个单文件 HTML 聊天页
   （原生 JS，无外部资源）：填 token、输入消息、调用 `POST /chat`、展示回复；
   若回复带 ticket id，轮询 `GET /tickets/<id>` 显示进度。页面作为包内静态文件或内嵌字符串均可，
   需有测试断言 `GET /` 返回 200 且含聊天页关键元素，未带 token 时 API 仍按原规则鉴权（页面本身公开没关系）。
2. **产物下载接口**：
   - `GET /tickets/<id>/artifacts` → JSON 列出该工单 artifacts（文件名、大小）。
   - `GET /tickets/<id>/artifacts/<name>` → 下载文件内容（合适的 Content-Type 猜测 + Content-Disposition）。
   - 安全：只允许工单 workdir 内的文件；`../` 等路径穿越一律 404/400，必须有穿越尝试的测试。同样需要 token 鉴权。
3. **飞书渠道骨架**：`POST /channels/feishu`：
   - 能响应飞书的 url_verification challenge；
   - 收到文本消息事件时提取文本 → 走 Brain → 如配置了飞书应用凭证（环境变量
     `FEISHU_APP_ID`/`FEISHU_APP_SECRET`）则调用飞书 API 回复，未配置时只记录日志并返回 200；
   - 在 README 写清开通步骤（需要用户自己的飞书应用凭证，本阶段不接真实账号）。有 challenge 与事件解析的测试（假数据）。
4. **部署物料**：`deploy/` 目录：
   - `superagentd.service`、`superagent-http.service` 两个 systemd unit（WorkingDirectory 指向项目目录、
     重启策略 Restart=on-failure、EnvironmentFile 可选）；
   - `deploy/install.sh`：把项目装到目标机后启用两个服务的脚本（幂等、带简单校验）；
   - README 新增"部署与远程访问"一节：systemd 部署步骤、http_host/http_port 配置、
     远程访问的推荐方式（内网/Tailscale/反向代理 + token），明确不要裸奔公网。
5. 全部新功能都要有测试；跑全量测试必须全过。完成后 git 提交，信息注明 phase 4。

## 效率要求（重要）
- 自检从简：单元测试跑完即可；真实模型端到端最多做一次（用 serve + 一次 /chat 小任务），
  不要反复跑真实链路。先提交再写总结，避免超时丢提交。
- 不要改 claude_model 默认值（已定为 cn:deepseek-v4.1-flash）。不要写入任何真实 key/凭证。
