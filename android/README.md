# SuperAgent Lite — Android 客户端

手机上的远程客户端：服务器（主智能体）跑在 Linux 机器上，这个 App 只负责
对话和处理待批准事项，就像用 Muse 客户端一样。

功能：

- 与主智能体聊天（回复会等服务器上的任务执行完，可能要等几分钟）
- 「待处理」：需要你批准的工单直接在列表里批准/驳回
- 设置：服务器地址 + 访问令牌，带连接测试；支持深色模式（跟随系统）

## 构建

需要 JDK 17 和 Android SDK（platform 34）：

```bash
gradle assembleDebug        # 或在 Android Studio 里打开本目录
# 产物：app/build/outputs/apk/debug/app-debug.apk
```

把 APK 传到手机上安装即可（需要允许「安装未知来源应用」）。

## 配置

第一次打开会进入设置页：

- **服务器地址**：`http://<服务器地址>:8765`。服务器默认只监听本机，
  从手机连接请走 Tailscale（填服务器的 Tailscale 地址）或 SSH 隧道。
- **访问令牌**：服务器上的 `SUPERAGENT_HTTP_TOKEN`（部署在云端/Linux
  服务器时位于 `/etc/superagent/superagent-http.env`）。

设计说明见仓库根目录的 `DESIGN-BRIEF.md`（提炼自 DeepSeek DSH 面板的设计
原则与度量）。
