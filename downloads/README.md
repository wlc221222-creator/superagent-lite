# 下载

## 安卓 App

`superagent-lite.apk` — 直接下载安装（调试签名，安装时需允许「未知来源」）。

## Windows 客户端（分卷）

`SuperAgentLite.exe` 有 161MB，超过 Git 单文件上限，拆成了 3 个分卷：

1. 下载 `SuperAgentLite.exe.part00`、`.part01`、`.part02` 和 `join.bat`，放进同一个文件夹
2. 双击 `join.bat`，会合并出 `SuperAgentLite.exe`，双击即可运行（免安装）

手动合并（命令行）：`copy /b SuperAgentLite.exe.part00+SuperAgentLite.exe.part01+SuperAgentLite.exe.part02 SuperAgentLite.exe`

合并后可用 `SuperAgentLite.exe.sha256` 里的哈希校验完整性。
