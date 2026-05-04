# Flow Harvester — Windows 安装手册

适用 Windows 10 / Windows 11。**不需要装 Python**，整个工具已经打包成 exe。

---

## 一、需要装好（一次性）

**Google Chrome** — https://www.google.com/chrome/

工具会调用客户机上已安装的 Chrome 来登录 Google 账号，所以这是必需的。

> Python / 任何其他依赖都**不需要**安装。

---

## 二、首次安装（1 分钟）

1. 收到 `FlowHarvester-bundle.zip`。
2. 解压到一个固定位置，例如 `D:\FlowHarvester\`。
3. 双击 `FlowHarvester.exe`。
4. 第一次启动 Windows Defender / SmartScreen 可能提示"未识别的应用"，点击 **更多信息** → **仍要运行**。
5. 一个 cmd 窗口打开（显示日志），几秒后默认浏览器自动打开 `http://127.0.0.1:8080/`，里面就是 Flow Harvester 的总览页 — 完成。

---

## 三、日常使用

**双击 `FlowHarvester.exe`** → cmd 窗口出现，浏览器自动打开仪表盘。

> cmd 窗口里滚动的是实时日志（worker / scheduler / web 服务）。**不要关 cmd 窗口**，关掉就停了。
> 操作都在浏览器里完成。如果浏览器没自动开，手动访问 cmd 窗口里显示的 URL（默认 `http://127.0.0.1:8080/`）。

要停止：**关 cmd 窗口** 或在 cmd 窗口里按 `Ctrl+C`。

> 想跟 Windows 一起开机自启动？把 `FlowHarvester.exe` 的快捷方式拖到 `shell:startup` 文件夹（按 `Win+R` → 输入 `shell:startup` → 回车）。

---

## 四、文件存哪里

| 类型 | Windows 位置 |
|------|--------------|
| 配置 / DB | `%LOCALAPPDATA%\FlowHarvester\flow_harvester.sqlite` |
| Chrome profile（账号登录态） | `%LOCALAPPDATA%\FlowHarvester\profiles\WS_X\` |
| 上传的参考图 | `%LOCALAPPDATA%\FlowHarvester\assets\<task_id>\` |
| 运行日志 | `%LOCALAPPDATA%\FlowHarvester\logs\` |
| 启动崩溃记录 | `%LOCALAPPDATA%\FlowHarvester\logs\crash.log` |
| **采集到的视频（重要！）** | `%USERPROFILE%\Documents\FlowHarvester\output\` |

> `%LOCALAPPDATA%` 一般是 `C:\Users\<你的用户名>\AppData\Local`，是隐藏文件夹。
> `%USERPROFILE%\Documents` 就是「文档」文件夹。

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

### Q：双击 exe 一闪就消失？

启动失败。看 `%LOCALAPPDATA%\FlowHarvester\logs\crash.log`，里面有完整 traceback。cmd 窗口里在出错时会暂停等你按回车，所以正常情况能直接看到错误信息。

### Q：Windows Defender SmartScreen 拦截？

第一次运行会拦截（"未识别的应用"），点 **更多信息** → **仍要运行**。后续不会再提示。

如果是公司管控严的杀毒软件直接删 exe，找 IT 加白名单：`FlowHarvester.exe` 路径 + `%LOCALAPPDATA%\FlowHarvester\` 整个文件夹。

### Q：浏览器没自动打开？

看 cmd 窗口顶部的横幅 banner，里面写着 `Dashboard: http://127.0.0.1:<端口>/`，复制这个 URL 到浏览器即可。

### Q：占用 8080 端口？

工具会自动检测端口，被占用就换一个空的（cmd 窗口里会打印实际用的端口）。

### Q：Chrome 没装会怎样？

工具能启动；加账号 → 登录这一步会失败。装好 Chrome 即可，无需重启工具。

### Q：怎么知道工具在跑还是已经关了？

看任务栏有没有 cmd 窗口（标题栏写着 `FlowHarvester.exe` 或类似路径）。关了 cmd 窗口 = 工具完全停止。

---

## 七、给开发者反馈问题

带上这三样东西：

1. 浏览器里出问题页面的截图
2. `%LOCALAPPDATA%\FlowHarvester\logs\app.log` 最后 50 行（或全部 zip 起来发）
3. 出问题的任务编号（形如 `T_20260503T123456_abcdef`）

如果是启动失败，附上 `%LOCALAPPDATA%\FlowHarvester\logs\crash.log` + cmd 窗口截图。

---

## 附录 A — 开发者从源码运行（不需要打包）

如果你是开发者要直接跑代码：

1. 装 Python 3.10+ 和 Git。
2. `git clone <repo>` + `cd Flow-picker-tool`
3. `python -m venv .venv && .venv\Scripts\activate`
4. `pip install -e ".[dev]"`
5. `python -m app`（cmd 启 server + 自动开浏览器）或 `flow-harvester serve`（仅启 server，浏览器自己开）

## 附录 B — 开发者打包 exe

在 Windows 10 / 11 上:

1. 跑过一次 `setup.bat` 建好 venv。
2. 双击 `build.bat` — 输出 `dist\FlowHarvester\` 文件夹（含 `FlowHarvester.exe` + DLL）。
3. 把整个 `dist\FlowHarvester\` 打包成 zip，发给客户。

或者一步到位：`build.bat zip` 直接产出 `FlowHarvester-bundle.zip`。
