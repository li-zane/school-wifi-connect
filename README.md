# NJUST WiFi

用于在 Windows 上保持网络连接和常用程序在线。断网时尝试重连，远程控制等常用程序退出时自动重启。

[下载最新版](https://github.com/li-zane/school-wifi-connect/releases/latest)

## 支持的功能

- 自动登录 NJUST 校园网，断网时尝试恢复 Wi-Fi 连接。
- 校园网不可用时，按设置的顺序尝试连接备用 Wi-Fi。
- 守护指定的 Windows 程序，检测到退出时尝试重新启动。
- 支持 Clash Verge 和 FlClash 的代理出口检测、故障保护和按程序分流。
- 支持托盘后台运行和 Windows 登录前自动联网。
- 查看网络状态、校园网余额和日志，导出脱敏诊断报告。
- 应用内下载、校验和安装新版本，启动失败时恢复旧版。

## 安装与更新

- 在 Windows 10/11（64 位）运行**安装版** `NJUSTWiFi-<版本>-Windows-x64-Setup.exe`，然后从开始菜单打开 **NJUST WiFi**。
- 使用**便携版**时，完整解压 `NJUSTWiFi-<版本>-Windows-x64.zip`，双击 `NJUSTWiFi.exe`。
- 首次使用时，在「设置」填写校园网账号或添加备用 Wi-Fi，再回到「状态」点击「开始监控」。在「守护」中添加要监控的程序。
- 更新时，点击右上角「检查更新」→「下载更新」→「安装并重启」。
