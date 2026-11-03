# SuperAgent Lite — Windows 客户端

Windows 上的远程客户端：服务器（主智能体）跑在 Linux 机器上，这个程序只负责
对话和处理待批准事项 —— 大多数人就是这样用的：在 Windows 上，使用 Linux 服务器。

功能：

- 与主智能体聊天（回复会等服务器上的任务执行完，可能要等几分钟）
- 「待处理」：选中等待批准的工单，直接批准/驳回
- 设置：服务器地址 + 访问令牌（保存在 `%APPDATA%\SuperAgentLite\settings.json`），带连接测试

## 构建

需要 .NET 8 SDK（任意平台均可交叉编译出 Windows 程序）：

```bash
dotnet publish -c Release -r win-x64 --self-contained true \
  -p:PublishSingleFile=true -p:EnableWindowsTargeting=true
# 产物：bin/Release/net8.0-windows/win-x64/publish/SuperAgentLite.exe
```

产物是单个自包含的 exe，拷到 Windows 上双击即可运行，不需要安装 .NET。

## 配置

第一次打开会提示进入设置：

- **服务器地址**：`http://<服务器地址>:8765`，局域网直连或填 Tailscale 地址。
- **访问令牌**：服务器上的 `SUPERAGENT_HTTP_TOKEN`
  （位于服务器的 `/etc/superagent/superagent-http.env`）。

设计说明见仓库根目录的 `DESIGN-BRIEF.md`。
