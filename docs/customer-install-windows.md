# Flow Harvester — Windows 安装手册

适用 Windows 10 / Windows 11。

---

## 一、需要先装好（一次性）

1. **Google Chrome** — https://www.google.com/chrome/
   工具会调用客户机上已安装的 Chrome，所以这是必需的。
2. **Python 3.10 或更新版** — https://www.python.org/downloads/
   安装时**勾选 "Add Python to PATH"**（很重要，否则 setup.bat 找不到 Python）。

> 公司机器一般已经装好 Chrome；Python 多数没有，需要装一下。如果有 IT 限制装不了，找开发者要绿色版方案。

---

## 二、首次安装（5 分钟）

1. 把整个 `Flow-picker-tool` 项目文件夹拷到客户机器上（建议放在 `D:\FlowHarvester\` 之类的固定位置，不要放桌面）。
2. **双击 `setup.bat`**：
   - 自动检测 Python
   - 创建虚拟环境 `.venv\`
   - 安装所有依赖（联网，1-2 分钟）
   - 检测 Chrome 是否安装
3. 看到 `=== Setup complete ===` 就成功了。

> 中途如果失败，命令窗口会显示原因。常见问题：
> - `Python 3.10+ not found` → 装 Python 时没勾 PATH，重新安装
> - `pip install failed` → 网络问题，挂代理重试
> - `Chrome not found` → 装 Chrome

---

## 三、日常启动

**双击 `start.bat`**。

会发生：
- 一个黑色命令窗口打开（**不要关闭它**，关了等于停止工具）
- 浏览器自动打开 `http://127.0.0.1:8080/`
- 看到"Flow Harvester"总览页就启动成功

按 `Ctrl+C` 或直接关闭命令窗口可以停止工具。

> 想让工具跟着开机自启动？把 `start.bat` 的快捷方式拖到 `shell:startup` 文件夹（按 `Win+R` 输入 `shell:startup` 回车打开）。

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

## 五、卸载 / 重装

- **删 DB 重新开始**：删除 `%LOCALAPPDATA%\FlowHarvester\flow_harvester.sqlite`，下次 start.bat 会重建。
- **彻底卸载**：
  1. 删整个 `%LOCALAPPDATA%\FlowHarvester\` 文件夹（DB / profile / logs / 上传素材都清掉）
  2. 删 `%USERPROFILE%\Documents\FlowHarvester\` 文件夹（视频也清掉）
  3. 删项目文件夹
- **不删数据，只重装代码**：用新版本覆盖项目文件夹，再跑一次 `setup.bat` 即可。

---

## 六、常见问题

### Q：双击 setup.bat 闪一下就没了？

命令窗口最后有 `pause` 等回车，但如果有错误在更早就 exit /b 了。**手动**：右键 `setup.bat` → 选 "在终端中运行"（或先 `Win+R` 打开 `cmd`，再 `cd` 进项目目录手动执行 `setup.bat`），看到完整错误。

### Q：浏览器没自动打开？

手动浏览器开 `http://127.0.0.1:8080/`，效果一样。

### Q：start.bat 报 `flow-harvester is not recognized`？

`setup.bat` 没装好。重跑 setup.bat。

### Q：杀毒软件 / 防火墙拦截？

工具只在 localhost 监听 8080，不联网（除了 patchright 调你正常使用的 Chrome 去 labs.google），加白名单即可。

### Q：占用 8080 端口？

改 `start.bat` 里 `--port 8080` 为别的端口（例如 `--port 18080`），同时把 `start http://127.0.0.1:8080/` 也改对应端口。

---

## 七、给开发者反馈问题

带上这三样东西：

1. 浏览器里出问题页面的截图
2. 命令窗口最后 30 行的内容（右键标记复制）
3. 出问题的任务编号（形如 `T_20260503T123456_abcdef`）

日志位置：`%LOCALAPPDATA%\FlowHarvester\logs\` — 把里面 `worker_*.log` 和 `scheduler.log` 一起打包发过去。
