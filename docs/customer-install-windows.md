# Flow Harvester — Windows 安装手册

适用 Windows 10 / Windows 11。**不需要装 Python**，整个工具已经打包成单个 exe。

---

## 一、需要装好（一次性）

**Google Chrome** — https://www.google.com/chrome/

工具会调用客户机上已安装的 Chrome 来登录 Google 账号，所以这是必需的。

> Chrome 通常公司机器都有；如果没有就装一下。Python / 任何其他依赖都**不需要**安装。

---

## 二、首次安装（1 分钟）

1. 收到 `FlowHarvester-bundle.zip`。
2. 解压到一个固定位置，例如 `D:\FlowHarvester\`。
3. 双击 `FlowHarvester.exe`（或 `Run Flow Harvester.cmd`）。
4. 第一次启动 Windows Defender / SmartScreen 可能提示"未识别的应用"，点击 **更多信息** → **仍要运行**。
5. 命令窗口打开，浏览器自动打开 `http://127.0.0.1:8080/`，看到「Flow Harvester」总览页就启动成功。

---

## 三、日常使用

**双击 `FlowHarvester.exe`**。

会发生：
- 一个黑色命令窗口打开（**不要关闭它**，关了等于停止工具）
- 浏览器自动打开 dashboard
- 任务在后台跑，关浏览器不影响

要停止：在命令窗口按 `Ctrl+C`，或直接关闭命令窗口。

> 想跟 Windows 一起开机自启动？把 `FlowHarvester.exe` 的快捷方式拖到 `shell:startup` 文件夹（按 `Win+R` → 输入 `shell:startup` → 回车）。

---

## 四、文件存哪里

| 类型 | Windows 位置 |
|------|--------------|
| 配置 / DB | `%LOCALAPPDATA%\FlowHarvester\flow_harvester.sqlite` |
| Chrome profile（账号登录态） | `%LOCALAPPDATA%\FlowHarvester\profiles\WS_X\` |
| 上传的参考图 | `%LOCALAPPDATA%\FlowHarvester\assets\<task_id>\` |
| 运行日志 | `%LOCALAPPDATA%\FlowHarvester\logs\` |
| **采集到的视频（重要！）** | `%USERPROFILE%\Documents\FlowHarvester\output\` |

> `%LOCALAPPDATA%` 一般是 `C:\Users\<你的用户名>\AppData\Local`，是隐藏文件夹。
> `%USERPROFILE%\Documents` 就是「文档」文件夹，平时能直接看到。

任务详情页有 **打开文件夹** 按钮，会自动跳转到对应输出目录。

---

## 五、卸载 / 更新

- **更新到新版本**：删旧的解压文件夹，把新版 zip 解压到同位置即可。**数据 / 账号 / 历史任务全保留**（它们都在 `%LOCALAPPDATA%\FlowHarvester\` 里，不在 exe 旁边）。
- **删 DB 重新开始**：删除 `%LOCALAPPDATA%\FlowHarvester\flow_harvester.sqlite`，下次启动会重建。
- **彻底卸载**：
  1. 删 exe 解压文件夹
  2. 删整个 `%LOCALAPPDATA%\FlowHarvester\` 文件夹（DB / profile / logs / 上传素材都清掉）
  3. 删 `%USERPROFILE%\Documents\FlowHarvester\` 文件夹（视频也清掉）

---

## 六、常见问题

### Q：双击 exe 闪一下就消失？

工具运行时命令窗口要保留。如果窗口出现一秒就消失，多半是出错了被自动关。**手动**：先打开 cmd（`Win+R` → `cmd` → 回车），再 `cd` 进解压文件夹，运行 `FlowHarvester.exe`，就能看到完整错误。

### Q：Windows Defender SmartScreen 拦截？

第一次运行会拦截（"未识别的应用"），点 **更多信息** → **仍要运行**。后续不会再提示。

如果是公司管控严的杀毒软件直接拦截删了 exe，找 IT 加白名单：`FlowHarvester.exe` 路径 + `%LOCALAPPDATA%\FlowHarvester\` 整个文件夹。

### Q：浏览器没自动打开？

手动浏览器开 `http://127.0.0.1:8080/`，效果一样。

### Q：占用 8080 端口？

设置环境变量 `FLOW_HARVESTER_PORT=18080`（或其他端口）后再启动 exe。

```cmd
set FLOW_HARVESTER_PORT=18080
FlowHarvester.exe
```

### Q：Chrome 没装会怎样？

可以启动 server 但加账号 → 登录这一步会失败。装好 Chrome 即可，工具不需要重启。

---

## 七、给开发者反馈问题

带上这三样东西：

1. 浏览器里出问题页面的截图
2. 命令窗口最后 30 行的内容（右键标记复制）
3. 出问题的任务编号（形如 `T_20260503T123456_abcdef`）

日志位置：`%LOCALAPPDATA%\FlowHarvester\logs\` — 把里面 `worker_*.log` 和 `scheduler.log` 一起打包发过去。

---

## 附录 A — 开发者从源码运行（不需要打包）

如果你是开发者要直接跑代码（修 bug / 加功能）：

1. 装 Python 3.10+ 和 Git。
2. `git clone <repo>` + `cd Flow-picker-tool`
3. 双击 `setup.bat`（建 venv + 装依赖）
4. 双击 `start.bat`

源码模式和打包模式行为一致。

## 附录 B — 开发者打包 exe

在 Windows 10 / 11 上：

1. 跑过一次 `setup.bat` 建好 venv。
2. 双击 `build.bat` — 输出 `dist\FlowHarvester\` 文件夹（含 `FlowHarvester.exe` + DLL）。
3. 把整个 `dist\FlowHarvester\` 打包成 zip，发给客户。

或者一步到位：`build.bat zip` 直接产出 `FlowHarvester-bundle.zip`。
