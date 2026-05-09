# Flow Harvester V2 架构设计

**版本**：草案 v0.6（inline 集成 challenges v0.0.1 → v0.0.5 共 67 条）
**日期**：2026-05-10
**状态**：架构决定 + 立项 gate G1-G11 + spike 验证才能进 V2 实施
**前置阅读**：
- [V1 Customer Delivery Plan](v1-customer-delivery-plan.md)
- [Architecture (V1)](architecture.md)
- **[V2 Architecture Challenges](v2-architecture-challenges.md)** — 跟本文档对偶迭代
- **[V1 Fragility Regression Suite](v1-fragility-regression-suite.md)** — 35 条 V1 fragility 在 V2 的对应处理（C-032，spike Phase 0.5 输出）

> **v0.3 规则（C-014 修正）持续生效**：本文档所有 challenge 响应**必须 inline edit 到正文章节**，§11 仅保留 change-log 索引。每个未完成项必须在正文章节顶部用 ⚠️ 标记"待 spike 决定"，不再隐藏在 §11 段。

> **v0.5 重大架构反转（用户约束 2026-05-09）**：
> - **❌ 不上 Chrome Web Store**——extension 只走 unpacked 分发（C-042 OBSOLETE，约束反转之前 v0.4 §5.3 / §8 commit "首选 Web Store" 决策）
> - **后果**：unpacked-only 是单点故障（chrome 主版本升级可 disable）→ §6.2 风险登记册 "扩展更新困难" 升 **极高/极高**；新增 G10 立项 gate（chrome 升级管控）；§5.3 commit chrome 升级监控 + Co-Pilot reload 助手 + 测试机先升级
> - manifest 加 `key` 字段（C-050）固定 unpacked ext_id，让 ws_token / Origin 校验稳定

> **v0.5 其他更新（v0.0.4 集成 12 条）**：
> - §4.2 Locale 处理路径**重写为 CSP 兼容性矩阵**（C-041，4 条假设逐条标 CSP 可行性 + spike #2 验收升级；接受 V2 Locale 优势重定位为 fail-fast 不是 locale-independent）
> - **新增 §4.6 多 tab 管理**（C-043，工位 active_flow_tab_id + tab 锁定 + chrome restore 处理）
> - **新增 §4.7 扩展自身 update lifecycle**（C-048，延迟 update 直到 task idle + content script 版本同步 + onSuspend flush）
> - §4.4 加 SQLite WAL batch write + checkpoint 策略（C-046）
> - §13.5 **GDPR vs cloudflared 全部重写**（C-044，cloudflared 默认关 + named tunnel + 30 分钟自动关 + 诊断包加密）
> - **新增 §12.6 V2.x 内部 rollback SOP**（C-045）+ §12.7 unified release 管理（C-055）
> - **新增 §17 后端契约监控**（C-047，金丝雀 + 远程 selector hot update + ErrorType 加 captcha/contract_drift/onboarding_required）
> - §4.3 ErrorType 加 `login_required`（C-051） + filename sanitize NTFS（C-049）
> - §13.7 dashboard log dedup + level filter（C-053）
> - 立项 gate 扩展到 **G1-G10**（加 G10 chrome 升级管控）

> **v0.4 → v0.5 既有 commit 持续生效（v0.0.1-v0.0.3 集成）**：
> - §2.2 + §5.4 commit 专门账号（C-028）/ §3.1 用户进程 + 开机自启（C-033）/ §4.2 扩展冲突检测 + storage.sync 禁用（C-029 / C-030）/ §4.4 task lifecycle + reconnect storm 防御（C-001 / C-034）/ §13.4 + §13.6 license machine binding + tier（C-027）/ §15 chrome 版本兼容矩阵（C-031）/ §16 V1 fragility regression（C-032）

> **v0.6 重大更新（v0.0.5 集成 14 条）**：
> - **新增 §18 客户协作框架**（C-056）+ 运营成本明细（C-064）：PoC 客户参与 / NDA / DPA / 商业关系 / spike 拆 Phase A 独立 + Phase B 客户协作
> - **新增 §19 测试基础设施**（C-058）+ i18n 测试矩阵（C-065）：测试机 3 台清单 + Veo 测试账号 + CI 选型 + Phase -1 测试搭建
> - **§5.3 install wizard 加 GPO 检测**（C-057）+ chrome 命令行 fallback + G11 立项 gate "客户机可装 unpacked 扩展"
> - **§5.3 install wizard 加 WS 网络层检测**（C-060）：Defender Network Protection + 公司代理 + DoH + AV 干扰
> - §4.6 多 tab 升级到 **(window_id, tab_id) + single-window 强制策略**（C-059）
> - §4.2 content script **懒加载 + MutationObserver 限缩范围 + chrome.alarms 周期动态**（C-062）+ spike #17 性能 benchmark
> - §4.2 加扩展 **self-check**（C-067）：chrome.storage / 下载目录 / 自身扩展状态 first-run + 周期性
> - §4.3 / §4.4 cancel_task 加阶段约束（C-061）：create_committed 后 reject + dashboard UX 二次确认 + 残留 mp4 兜底下载
> - §4.3 mp4 codec 实测 + 客户文档披露（C-066）
> - **§6.1 milestone 重排为 Phase -1 / 0 / 0.5 / 1 / 2，spike 4-6 周**（C-063）
> - §12.7 unified release zip 结构定义（C-068）+ §12.8 数据备份恢复策略（C-069）
> - 立项 gate 扩到 **G1-G11**（v0.6 加 G11 客户机可装 unpacked）

---

## 1. 为什么要做 V2

### 1.1 V1 的根本矛盾

V1 用 **patchright 驱动 Chrome** 跟 Google Flow 交互。这是"外部自动化"模式——patchright 启动一个独立 Chrome 进程，通过 CDP 注入命令。

> **核心矛盾**：Google 是反"外部自动化"的世界级专家。我们在 patchright 层永远追不上他们的反检测。

### 1.2 v0.0.1→v0.1.0 6 轮迭代揭示的模式

| 版本 | 修复 | 暴露的下一层问题 |
|---|---|---|
| v0.0.1 | bundle + license + 自愈 | 客户出问题没诊断手段 |
| v0.0.2 | forensic log + 诊断包 + cloudflared | 登录后 project URL 不识别 |
| v0.0.3 | locale URL regex + output_count | 越南语 UI 找不到 Start 按钮 |
| v0.0.4 | 13 语言 selector 列表 | 中文 UI 找不到 Create 按钮 |
| v0.1.0 | 操作员驱动账号语言切换 | ??? |

**每轮都是上一轮暴露的问题，每轮都被新的下一层问题打脸**。这是结构性的——我们在打"猫鼠游戏"，而 Google 是猫。

### 1.3 patchright 在 Google 产品上的明确边界

[`memory/project_patchright_limits.md`](../.claude/projects/-Users-shenpeng-Git-Flow-picker-tool/memory/project_patchright_limits.md) 记录了 patchright 1.59.1 README 列出的能过的 13 个 detector：

> Cloudflare / Datadome / Akamai / F5 / Bet365 / Kasada / Shape / Fingerprint.com / Brotector / CreepJS / Sannysoft / Incolumitas / IPHey / Browserscan / Pixelscan

**Google 任何一个产品都不在列表上**。Google 用未公开的内部检测对 Flow / Veo / Bard 做账号级风控。

### 1.4 V1 的"已经穷尽"信号

v0.1.0 的"操作员驱动账号语言切换"是 patchright 层能做的**最后一个结构性优化**：

- 它解决了 locale 类问题（一次切换永久消除）
- 但 anti-bot / selector drift / 账号信誉这些底层问题**没解**
- 继续在 v0.1.x 加 forensic log / 多语言列表 / 隧道 fallback 都是边际改进，不解决根本

**结论**：到 V2 是必然的。问题只是"什么时候"。

---

## 2. V2 架构愿景

### 2.1 核心比喻：中控 + 扩展

借工业控制系统的 C&C 拓扑：

```
                    ┌─────────────────────────┐
                    │     中控（Co-Pilot）      │
                    │  ────────────────────   │
                    │  ► 任务队列 / 调度        │
                    │  ► 工位池管理            │
                    │  ► 操作员 dashboard      │
                    │  ► DB / 报表 / license   │
                    │  ► Setup wizard          │
                    └────────────┬────────────┘
                                 │ WebSocket
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        ┌──────────┐       ┌──────────┐       ┌──────────┐
        │ Chrome A │       │ Chrome B │       │ Chrome C │
        │ profile  │       │ profile  │       │ profile  │
        │ (账号 A) │       │ (账号 B) │       │ (账号 C) │
        │          │       │          │       │          │
        │ + 扩展   │       │ + 扩展   │       │ + 扩展   │
        │ (执行)   │       │ (执行)   │       │ (执行)   │
        └──────────┘       └──────────┘       └──────────┘
```

- **中控（Co-Pilot）= 大脑**：调度 / 任务 / DB / 操作员 UI
- **扩展（Extension）= 手脚**：在客户日常 Chrome 里执行 Flow 操作
- **WebSocket = 神经**：双向通信

### 2.2 期望解决的根本问题（带 caveat）

> ⚠️ "V2 期望解决"≠"V2 自动解决"。下表中带 ⚠️ 的项是 spike 待验证假设，spike 失败前不能宣称 V2 已解决。

| 关切 | V1（patchright） | V2（扩展期望） |
|---|---|---|
| Anti-bot | 持续磨损（patchright 痕迹被 Google 识别） | 显著降低（扩展是 Chrome 一等公民），但 Google 仍可能基于行为 fingerprint |
| Locale（v0.5 改，C-041） | 多语言 selector + 操作员切英文 | **≈ V1 多语言列表 + fail-fast 截图上报**（CSP 限制下 locale-independent 几乎不可能；V2 优势重定位为运维体验，不是 locale） |
| Selector drift（v0.5 改，C-041 / C-047） | Google 改 UI = 全部重新 patch | **≈ V1 + 远程 selector hot update**（§17 中控 push 字典不重发版，缩短 fix 时间窗口） |
| 账号信誉（C-028 改） | patchright chrome = 陌生设备 | **≈ 持平 V1（同样使用专门账号）**——见 §5.4 账号性质决策；V2 优势主要在 selector / locale / 调试，**不在账号信誉** |
| 调试 | 客户邮件诊断包 + cloudflared 远程隧道 | **保留 cloudflared 隧道**（C-017）+ 扩展端日志推送中控 + 远程截图协议 |
| 安装包 | 75 MB（含 patchright） | ~50-55 MB（Co-Pilot 30 MB + cloudflared 25 MB + extension 1 MB） |
| 可观测性 | 中控聚合 worker logs + WS 状态 | **增强**：扩展端 console.log → 中控 forensic_log 表；中控可主动 screenshot_request 任意 WS |
| 任务可恢复性 | patchright crash → zombie cleanup | ⚠️ chrome 关闭中途 → §4.4 task lifecycle 状态机 + reconnect 对账 |
| Locale 处理代价 | 38 条多语言 selector + 操作员切英文 | 同样需要兜底；但失败模式从 silent → fail-fast |

### 2.3 为什么不用替代方案

| 方案 | 否决理由 |
|---|---|
| 继续修 patchright | 已论证：每轮新问题，永无止境 |
| Camoufox / Nodriver / CloakBrowser | 没有任何一个公开声称过 Google 产品；重写代价大 |
| Cloud SaaS | Veo 没公开 API；账号 cookies 上云 = 安全风险；IP 集中 = 风控集中 |
| 直接发邮件 | （非自动化）= 客户不要这个产品 |

---

## 3. 部署形态

### 3.1 V2.0：Local 中控（先做）

> ⚠️ **G1 立项 gate（待客户回答）**：客户机能否接受白天 N 个 chrome profile 长开 + 客户日常 chrome 受限？  
> ⚠️ **G2 立项 gate（待客户回答）**：客户机 RAM ≥16 GB？8 GB 跑不动 5 profile。  
> ⚠️ **G8 立项 gate（C-028）**：客户用日常账号还是专门账号？（设计已 commit "专门账号"，需客户确认）  
> ⚠️ **G9 立项 gate（C-031）**：客户机 chrome 版本审计 ≥117？

#### Co-Pilot 进程模型（v0.4 commit，C-033）

> **v0.3 含糊"Windows service / 后台进程"两可。v0.4 commit：Co-Pilot.exe = 用户进程 + 开机自启。**

| 维度 | Windows service（已废弃） | 用户进程 ✅（v0.4 选择） |
|---|---|---|
| logout 后存活 | ✅ 跑 | ❌ 死 — 但操作员日常不 logout（跟 chrome 必须开同等代价） |
| 访问 user chrome profile | ❌ | ✅ 必需（chrome_profile_launcher.py 启 chrome / 读 profile data） |
| 安装权限 | 需 admin（客户机能力上限） | 不需要 admin |
| chrome 启动时序 | service 启不了 chrome | ✅ Co-Pilot 启动后主动 stagger 启 N 个 profile chrome 窗口 |
| License 文件位置 | ProgramData（系统级） | %APPDATA%（用户级，跟 chrome 同 user）|
| output/ 写权限 | SYSTEM 写，user 读不到 | 同 user，无问题 |

**自启动实现**（install wizard 自动配置，无需 admin）：
- 注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\FlowHarvester` = `C:\Users\xxx\AppData\Local\FlowHarvester\Co-Pilot.exe --autostart`
- 或任务计划"用户登录时启动"

**chrome 启动时序**（C-033）：
1. 操作员开机 → Win11 logon → Co-Pilot 自动启动
2. Co-Pilot 检测 dashboard 操作员是否点了"启动调度器"——是则主动 launch chrome profiles
3. **Stagger 启动**：每 profile 间隔 5 秒，避免 N 个 chrome 同时撞起来 OOM
4. chrome 启动后 5-10 秒扩展才 inject → register
5. dashboard 显示"启动中（X / N 个工位连接中）"loading state；5 分钟没全 online 红色告警

**install wizard 一次性检测**（C-033 / C-030 / C-035 / C-036 集成）：
- ✅ admin 状态（不需要 admin 但提示）
- ✅ user session 类型（远程桌面 vs 本机交互）
- ✅ chrome 版本 ≥117（C-031 G9）
- ✅ chrome sync 关闭状态（C-030）
- ✅ RAM 余量 ≥推荐值（按 G2 决策）
- ✅ Win11 power plan 为"高性能" + sleep / hibernate 关闭（C-035）
- ✅ Windows Defender 排除项 + AV 白名单（C-036）
- ✅ 已装 chrome 扩展枚举（C-029 冲突检测）
- 任何不达标提示原因 + fix 路径

#### Plan A（首选）：扩展跑在客户**专用** chrome profile（v0.4 改）

> **v0.3 误导**：原写"扩展跑客户日常 chrome 共享 profile"。v0.4 修正（C-028 / C-029）：客户**为 V2 创建专门 profile（chrome 自带 multi-profile）**——账号是专门 V2 账号、profile 不开 sync、不装 Grammarly / AdBlock 等冲突扩展、chrome translate 关闭。
>
> **理由**：(1) C-028 账号信誉论据本不稳，专门账号跟 V1 一致风险可控；(2) C-029 客户日常 chrome 装的扩展会跟 V2 抢 DOM；(3) C-030 chrome sync 把 V2 扩展 + storage 同步到操作员个人设备 = 数据 + license 泄露。

**中控跟扩展同机部署**：

```
客户的 Win11 PC（Co-Pilot 用户进程，C-033）
├── %APPDATA%\FlowHarvester\Co-Pilot.exe   ← 用户进程，开机自启
│   ├── localhost:8080 dashboard（FastAPI 强制 127.0.0.1 bind，C-015）
│   ├── SQLite (current schema)
│   ├── /ws/extension/<ws_id>            ← 新增 WebSocket 路由
│   └── 操作员浏览器访问 dashboard
│
└── Chrome (操作员 chrome 实例，N 个 V2 专用 profile)
    ├── Profile WS_A（V2 专用，无 sync）→ Flow Harvester 扩展 → 连 ws://localhost:8080/ws/extension/A
    ├── Profile WS_B（V2 专用，无 sync）→ Flow Harvester 扩展 → 连 ws://localhost:8080/ws/extension/B
    └── Profile WS_C（V2 专用，无 sync）→ Flow Harvester 扩展 → 连 ws://localhost:8080/ws/extension/C
```

**优势**：
- ✅ 复用当前 backend ~85%（FastAPI / SQLite / scheduler / dashboard 不动；删 patchright；保留 cloudflared 因 C-017）
- ✅ 沿用 PyInstaller bundle 分发（用户级，无需 admin）
- ✅ 通信走 localhost，零延迟（但需 token auth + Origin 校验，C-015）
- ✅ 完全私有，符合 V1 客户期望
- ✅ V2 专用 profile 隔离客户日常 chrome（C-029 / C-030）

**代价**（C-003 / C-007 / C-013 量化）：
- ⚠️ chrome 必须长开（误关 = 任务停，§4.4 恢复机制兜底）
- ⚠️ N profile × 500MB-1GB chrome 内存（按客户 PC RAM 限制并发 N，默认 N=2-3，§5.4）
- ⚠️ chrome 自动 update 后 unpacked 扩展可能 disable（§5.3 缓解）
- ⚠️ 操作员开机后日常**不 logout**（用户进程模型代价）

**适合**：当前客户场景（5 个账号在一台 PC 上）+ 客户接受上述代价

#### Plan B（fallback）：扩展跑在独立 chrome 实例（客户拒绝 Plan A 时）

如果 G1 客户不接受日常 chrome 长开，退到独立 chrome 模式：

```
Co-Pilot 启动时:
  for each workstation:
    chrome.exe \
      --user-data-dir="C:\FlowHarvester\profiles\WS_X" \
      --load-extension="C:\FlowHarvester\extension" \
      --no-first-run --no-default-browser-check \
      https://labs.google/fx/tools/flow/<project_url>
  ↓
  独立 chrome 实例（不跟客户日常 chrome 共享 profile）
  ↓
  扩展自动注册 → 中控调度 → 跑任务
```

**Plan B 跟 Plan A 的退化**：
- ❌ 失去"真实使用历史 + 真实信誉"（独立 profile = 陌生设备，跟 patchright 同样问题）
- ❌ 失去"客户用自己 chrome"的简洁感
- ✅ 但仍比 patchright 强：扩展是 chrome 一等公民，没 webdriver / CDP fingerprint
- ✅ 客户日常 chrome 不受影响

**Plan B 决策点**：spike 阶段实测独立 chrome 实例（无 V1 历史）能否跑通 anti-bot。如果跑得通，Plan B 是 V2.0 备选；如果跑不通（跟 patchright 同样命运），V2 立项就要重新评估。

### 3.2 V3.0：Remote 中控（V2 跑稳后做）

**中控上云 / 客户独立服务器**：

```
                ┌─────────────────────┐
                │  云上中控（VPS / VPC）│
                │  Co-Pilot + DB       │
                │  HTTPS dashboard     │
                │  WSS endpoint        │
                └─────────┬────────────┘
                          │ WSS（加密）
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   ┌─────────┐       ┌─────────┐       ┌─────────┐
   │ 客户 PC1 │       │ 客户 PC2 │       │ 操作员手机│
   │ Chrome+ │       │ Chrome+ │       │ 看 dashboard │
   │ 扩展     │       │ 扩展     │       │            │
   └─────────┘       └─────────┘       └─────────┘
```

**适合**：商业化扩展，多机器 fleet，多操作员协作。**V2 跑稳后再启动**，本设计文档不展开。

---

## 4. 组件设计

### 4.1 中控（Co-Pilot）

#### 改动范围

```
保留（90%，全部不动）:
✅ app/web/server.py                  FastAPI 框架
✅ app/web/routes/*                   dashboard 路由（含 v0.0.2 诊断 + transition）
✅ app/web/templates/*                Jinja2 模板
✅ app/db/                           SQLite schema + migration
✅ app/scheduler/                    daemon + claim_one + state machine
✅ app/workstations/repository.py    workstation CRUD
✅ app/tasks/                        任务 CRUD + CSV 导入
✅ app/runner/multi.py               多 WS 调度（仅改派发方式）
✅ app/reports/daily.py              报表
✅ app/license.py                    license 校验
✅ app/diagnostics.py                诊断包
✅ flow_harvester.spec               PyInstaller spec

替换:
❌ app/worker/flow_playwright.py     → 删除（约 1900 行 Python）
❌ app/workstations/login_session.py → 删除（扩展自管登录态）
❌ app/workstations/profile_check.py → 删除（Plan A）/保留（Plan B）
❌ app/web/routes/login.py           → 简化（扩展自动上报已登录账号）
❌ patchright 依赖                    → 移除
✅ cloudflared 依赖                   → **保留**（C-017 反对取消；可观测性必须）

新增 Python:
✨ app/web/routes/extension_ws.py    WebSocket 路由 + token auth + Origin 校验（C-015）
✨ app/extension_dispatcher.py       中控派 task / 收回报 / reconnect 对账（C-009 / C-020）
✨ app/extension_token.py            Co-Pilot 启动生成 ws_token 写 %APPDATA%（C-015）
✨ app/chrome_profile_launcher.py    "一键启动 chrome+profile_X"（Plan A）/独立 chrome 实例 launcher（Plan B）
✨ app/security/dns_rebinding.py     FastAPI Host header 白名单中间件（C-015）

新增 TypeScript（扩展端）:
✨ extension/                        新独立子项目（Vite + @crxjs/vite-plugin）
   ├── manifest.json
   ├── src/background.ts             ~300-500 行
   ├── src/content/                  ~1500-2500 行（DOM ops + state machine + recovery）
   ├── src/popup + side_panel + options ~800-1200 行
   └── src/lib/                      ~500 行（ws client + storage + protocol）
```

**代码量诚实估算（C-010 修正）**：
- Python 端：删 ~2400 行，加 ~1500 行 → **Python 净减少 ~900 行**
- TypeScript 端：**新增 ~3500-5000 行**（V1 worker 1900 行的 TS 重写 + 多语言 fallback + storage/recovery 逻辑）
- **总代码量基本持平或略增**。但维护性提升（TS 类型安全 + chrome devtools 客户端调试），不是"代码减少"。

#### 调度逻辑变化

**当前（V1）**：

```python
# app/runner/multi.py
def _execute_in_thread(workstation, task, ...):
    # 启 patchright，跑完整任务，关 patchright
    flow_port = PlaywrightFlowPort(...)
    outcome = execute_task(conn, flow_port, ...)
    flow_port.close()
```

**V2**：

```python
# app/extension_dispatcher.py
def _dispatch_to_extension(workstation_id, task):
    # 通过 WebSocket 派 task 给已注册的扩展
    extension_session = registry.get(workstation_id)
    if not extension_session.is_alive():
        raise WorkstationOfflineError(...)  # 扩展失联
    extension_session.send_task(task)
    # 异步等扩展回报完成 / 失败 / 进度
    return await extension_session.wait_for_outcome(timeout=...)
```

中控不再"启动 chrome 跑代码"——而是"派 task，等结果"。

#### 工位状态机的小改

工位状态扩展两个：
- `online`（扩展已连接）
- `offline`（扩展失联）

跟现有的 `healthy / busy / cooldown / manual_check / nurturing / disabled` 正交：

```
扩展层:    online | offline
调度层:    healthy | busy | cooldown | manual_check | nurturing | disabled
```

操作员看到 `online + healthy` 才是真正可派 task。

### 4.2 扩展（Chrome Extension）

#### 技术栈

```
extension/
├── manifest.json                   # manifest v3
├── package.json                    # npm + Vite
├── vite.config.ts                  # @crxjs/vite-plugin
├── tsconfig.json
└── src/
    ├── background.ts               # service worker
    ├── content/
    │   ├── flow_dom.ts             # labs.google/* DOM 操作
    │   ├── flow_state.ts           # page state 检测
    │   └── flow_download.ts        # 下载 mp4
    ├── popup/
    │   ├── popup.tsx               # 工具栏图标弹窗
    │   └── popup.html
    ├── side_panel/
    │   ├── progress.tsx            # 实时进度
    │   └── side_panel.html
    ├── options/
    │   ├── options.tsx             # 账号绑定配置
    │   └── options.html
    └── lib/
        ├── ws_client.ts            # WebSocket 连中控
        ├── protocol.ts             # 消息 schema
        └── storage.ts              # chrome.storage.local 封装
```

**为什么 TypeScript**：扩展跑在客户 chrome 里，没有 unit test 套，type 错误线下抓比线上调试便宜很多。

**为什么 Vite + @crxjs/vite-plugin**：modern bundler 带 HMR，开发体验好；输出符合 manifest v3 规范。

#### Manifest v3 关键配置（最小权限 + C-015 安全 / C-021 一次性放足 / C-024 unlimitedStorage / C-029 management / C-038 不开 incognito）

```json
{
  "manifest_version": 3,
  "name": "Flow Harvester",
  "version": "2.0.0",
  "default_locale": "zh_CN",
  "key": "<base64-encoded-public-key>",
  "permissions": [
    "storage",
    "unlimitedStorage",
    "scripting",
    "tabs",
    "downloads",
    "alarms",
    "notifications",
    "management"
  ],
  "host_permissions": [
    "https://labs.google/fx/tools/flow/*",
    "http://localhost:8080/*",
    "http://127.0.0.1:8080/*"
  ],
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "content_scripts": [
    {
      "matches": ["https://labs.google/fx/tools/flow/*"],
      "js": ["content.js"],
      "run_at": "document_idle",
      "all_frames": false
    }
  ],
  "action": {
    "default_popup": "popup.html",
    "default_icon": "icons/icon128.png"
  },
  "side_panel": {
    "default_path": "side_panel.html"
  },
  "incognito": "not_allowed"
}
```

**关键决策**：
- ❌ **不要** `https://*.google.com/*` — 给扩展读 Gmail/Drive/Pay/Cloud Console cookie 权限是合规雷（C-015）
- ❌ **不要** `chrome.cookies` / `chrome.identity` API — V2 不需要
- ✅ host_permissions 列 `localhost:8080`（C-012 否则 fetch 撞 CORS）
- ✅ `unlimitedStorage` 防 5MB quota（C-024，task lifecycle 状态长期持久化）
- ✅ permissions 一次性放足 — 后续升级**只删不加**（C-021，避免触发 chrome review dialog）
- ✅ `management` 权限 — 启动时枚举已装扩展做冲突检测（C-029）
- ✅ `incognito: not_allowed`（C-038）— 操作员误开隐身模式时扩展不跑，popup 显著提示
- ✅ `default_locale: zh_CN` + `_locales/zh_CN/messages.json` + `_locales/en/messages.json`（C-039）— 跟 V1 中文 dashboard 风格对齐
- ✅ **`key` 字段**（v0.5 NEW，C-050）— base64 公钥固定 unpacked ext_id：
  - **理由**：unpacked ext_id 默认基于扩展目录路径 hash 生成（每客户机 / 每 reload 可能不同），导致 ws_token / Origin 校验失败
  - **生成**：作者本地 `openssl genrsa 2048` 生成密钥对 → 取公钥 base64 编码塞 manifest `key` 字段 → 所有客户机 unpacked 装同一份 manifest = 同一 ext_id
  - **效果**：固定 chrome-extension://<deterministic_id>/ Origin → ws_token 校验稳定 + Co-Pilot 知道唯一合法 ext_id

#### chrome.storage 使用规则（C-030 NEW）

> **强制禁用**：扩展代码**禁止使用 `chrome.storage.sync`**——sync 会跨设备同步到 chrome 登录账号，导致 license / business data / ws_token 泄露到操作员个人 Google 账号。

```typescript
// extension/src/lib/storage.ts —— 唯一允许的 storage 入口
export const storage = chrome.storage.local;       // ✅ 本机
// export const sync = chrome.storage.sync;        // ❌ 严禁使用
```

- ✅ CI lint rule 检测 `chrome.storage.sync` 出现 → 构建失败
- ✅ 扩展启动检测 chrome.runtime.id 是否在已知设备列表里（首次注册写入 chrome.storage.local）— 不一致 → 提示"该 profile 是从其他设备同步过来的，请重新跑 setup wizard"
- ✅ Setup wizard 强制要求操作员关闭该 profile chrome sync 才让继续（C-030）

#### 扩展冲突检测（C-029 NEW）

启动时 + 周期性（每小时）枚举已装扩展，跟"已知冲突黑名单"比对：

```typescript
// extension/src/background.ts
const KNOWN_CONFLICT_IDS = {
  // 行为冲突类
  'kbfnbcaeplbcioakkpcpgfkobkghlhen': 'Grammarly',     // input 监听
  'aeblfdkhhhdcdjpifhhbdiojplfjncoa': '1Password',     // autofill
  'hdokiejnpimakedhajhdlcegeplioahd': 'LastPass',
  // 自动化竞品（直接拒绝注册）
  '<veo-automation-id>': 'VEO Automation',
  '<auto-flow-pro-id>': 'Auto Flow Pro',
  '<flowforge-pro-id>': 'FlowForge Pro',
}

async function checkExtensionConflicts() {
  const installed = await chrome.management.getAll()
  const conflicts = installed.filter(e => e.enabled && KNOWN_CONFLICT_IDS[e.id])
  if (conflicts.some(e => /Veo|Flow/.test(KNOWN_CONFLICT_IDS[e.id]))) {
    // 自动化竞品：扩展拒绝注册
    ws.send({ type: 'register_fail', reason: 'competing_automation', conflicts })
    showCriticalBanner('⚠️ 检测到 Veo 自动化竞品扩展，请先卸载')
    return false
  }
  if (conflicts.length) {
    // 行为冲突：dashboard 红色告警 + popup 警告
    ws.send({ type: 'log', level: 'warn', message: `Conflict extensions: ${conflicts.map(e => e.name).join(',')}` })
    showWarningBanner('部分扩展可能干扰 V2，请到 V2 专用 profile 卸载')
  }
  return true
}
```

#### content script 防御性写法（C-029）

绕过其他扩展的 DOM hook：

```typescript
// extension/src/content/flow_dom.ts

// (1) prompt 输入：绕过 Grammarly input 事件监听 + 操作员中文 IME composition（C-052）
function setReactInputValue(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
  setter.call(el, value)
  // 派 React 内部 event（绕过 Grammarly + 跳过 IME composition 阶段）
  el.dispatchEvent(new Event('input', { bubbles: true }))
  // 必要时显式 dispatch composition end（防 React onChange 收到 stale composition state）
  el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: value }))
}
// 注意：不用 V1 的 `keyboard.type 60-110ms delay` —— content script 没此 API；用上面的 setter 路径直接绕开 IME（C-052）
// 中文 prompt 测试矩阵：操作员 chrome 中文 IME 开启 + V2 prompt = "中文 SKU 名" 验证内容 = page state value 一致

// (2) 文件上传：DataTransfer 直接构造 drop event（绕过 1Password autofill）
function uploadFileDirect(input: HTMLInputElement, file: File) {
  const dt = new DataTransfer()
  dt.items.add(file)
  input.files = dt.files
  input.dispatchEvent(new Event('change', { bubbles: true }))
}

// (3) chrome translate 显式禁
const meta = document.createElement('meta')
meta.name = 'google'
meta.content = 'notranslate'
document.head.appendChild(meta)

// (4) 注入 page world（绕过其他扩展的 isolated world 拦截）
chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func: ... })
```

**spike 验收新增 #11（C-029）**：装常见冲突扩展（Grammarly + 1Password + AdBlock + chrome translate）跑 V2 端到端，验证 prompt 内容正确 / 上传成功 / Create 不被拦截。

#### 内部分工（C-001 状态持久化加固）

| 模块 | 职责 |
|---|---|
| `background.ts` (service worker) | **薄层消息中转**：长连 WebSocket 到中控；`chrome.alarms` 30s 触发 + 持久化心跳；**所有 task 状态持久化到 chrome.storage.local，不放 sw 内存** |
| `content/flow_dom.ts` | 注入到 Flow page；操作 DOM（点 Start / 上传图 / 输 prompt / 点 Create）；**task lifecycle 主导权放这里**（content script 跟 page 绑定，page 不关就一直在） |
| `content/flow_state.ts` | 检测 page state（unusual_activity / no_flow_access / generation_complete） |
| `content/flow_download.ts` | 监听 Flow 视频生成完成；通过 `chrome.downloads` API 触发下载（chrome.downloads.onChanged 拿到最终 filename，C-019） |
| `content/recovery.ts` | sw 重连后跟 content script 对账：从 chrome.storage 读最后一次 commit 的 task state，恢复执行（§4.4） |
| `popup/popup.tsx` | 工具栏图标点击的快速控制；**显示当前 ws_id + email**（C-018 操作员肉眼可校） |
| `side_panel/progress.tsx` | 实时进度展开页（更详细） |
| `options/options.tsx` | 中控 token 输入 / ws_id 绑定 wizard（§4.5） |

**Service worker hibernate 防御（C-001）**：
- ❌ **不要**把 task lifecycle 状态放在 sw 内存（hibernate 后丢）
- ✅ **每个 task state transition 都同步落 chrome.storage.local**：`pending_create / create_committed / round_N_complete / task_complete`
- ✅ sw 重连后从 chrome.storage 恢复中断的 task；中控收到 `task_resume` 消息时跟自己 DB 对账
- ✅ 中控对 WS 断连不立即 flip 工位状态——给 60-120s grace（sw 重连窗口）
- ⚠️ **spike 验收 #1**：跑 10 分钟假任务，强制 stop service worker，看任务恢复是否正确

#### Locale 处理路径（v0.5 重写为 CSP 兼容性矩阵，C-041）

> ⚠️ **v0.5 关键修正（C-041）**：v0.4 把 4 条 locale-independent 假设当作 V2 优势论据，但**4 条全部撞 manifest v3 + labs.google CSP 双重限制**，需要逐条标 CSP 可行性。
>
> **V2 Locale 优势重定位**：不是"locale-independent"，而是 **fail-fast + 精准告警 + DevTools 可调试**。spike #2 pass 后只能是 MutationObserver + text 多语言 selector，跟 V1 多语言列表本质相同——但失败模式从 silent → fail-fast。

V1 的 selector drift 痛点本质是 patchright 看不到 React 内部，只能靠"找按钮文本"。理论上扩展能做的 4 条假设逐条 CSP 评估：

| # | 假设 | CSP 可行性 | locale-independent | 结论 |
|---|---|---|---|---|
| 1 | 通过 `__REACT_DEVTOOLS_GLOBAL_HOOK__` 读组件状态 | ❌ **production React build 默认不暴露 hook** | 是（如果 hook 可用） | **直接放弃**（spike #2 第一步先 verify hook 不存在，不浪费时间） |
| 2 | MutationObserver 监听 DOM 结构变化 | ✅ chrome API 调用，不受 CSP 约束 | ❌ **本质仍是 watch text/DOM**——locale 改变 selector 仍变 | **可用但跟 locale 无关**（只是 polling 方式更优雅，不解决根本问题） |
| 3 | 拦截 fetch / XHR | ⚠️ **必须 chrome.scripting + world: 'MAIN'** 注入 monkey-patch；inject 时机是脚本加载前，但 monkey-patch 受 page CSP `script-src 'self' 'nonce-xxx'` 约束 | 是（API endpoint 不依赖 locale） | **CSP 穿透成本高，spike 实测验证；Veo API path/shape 频繁改增加风险（C-047）** |
| 4 | DOM 结构 + data-attribute | ✅ 不需要 eval / world MAIN，直接 isolated world DOM 操作 | 是（如果 Google 给 data-testid） | **未实证**：V1 6 轮迭代证明 Google **没**给 data-testid（早给的话 V1 就用了） |

**CSP 穿透方案明确**（spike 阶段定）：

- 选项 A：用 `chrome.scripting.executeScript({world: 'MAIN', func})` 注入 — 受 page CSP 约束，inline script / eval 直接拒
- 选项 B：通过扩展 `declarativeNetRequest` 改 page CSP header — manifest v3 支持，但 anti-CSP 路径**违反 chrome 扩展最佳实践**且增加合规风险
- 选项 C：**接受 isolated world 限制**，只用 chrome content script 标准 API（chrome.scripting / DOM 操作 / MutationObserver）— 跟 V1 多语言列表本质相同

**spike 验收 #2（v0.5 改写，C-041）**：

> 用越南语账号 + chrome 117/124/130 三版本跑 Flow，**逐条**验证 4 条假设：

```
spike #2.1: __REACT_DEVTOOLS_GLOBAL_HOOK__ 是否存在
  → window.__REACT_DEVTOOLS_GLOBAL_HOOK__ undefined → 假设 1 直接 mark FAIL，不再尝试

spike #2.2: MutationObserver locale-independent 锚点
  → 写 50 行 content script 用 MutationObserver 找 upload/generate button
  → 是否能用 DOM 结构（不依赖文本）定位 → 极大概率 FAIL，退回 text 多语言

spike #2.3: chrome.scripting world:'MAIN' 注入 fetch monkey-patch
  → 在 labs.google page 注入代码 → page CSP 是否拒绝
  → CSP report 看 inline script blocked → 评估穿透方案

spike #2.4: 是否有 data-testid / data-* attribute
  → DOM inspect 看 button 上的非 text attribute → 几乎肯定无
```

**spike pass 标准重写**（v0.5 改）：
- 假设 1 / 4 几乎肯定 FAIL（基于 v0.0.4 C-041 论据）→ spike 必须达到 "假设 2 OR 假设 3 跑通"
- 假设 2 跑通但 locale-dependent → V2 Locale 优势重定位为 fail-fast（**不是** locale-independent）→ §2.2 表 Locale 行从 "⚠️ 假设可不依赖文本" 改为 "**≈ V1 多语言列表 + fail-fast + 截图上报**"
- 假设 3 跑通且 CSP 可穿透 → V2 真有 locale-independent 路径

**v0.5 退路接受**：spike 只能跑通假设 2 → 接受 V2 仍要做 locale 层（13 语言 selector 列表移植），但失败模式比 V1 进步：
- V1：silent timeout 60s
- V2：找不到 selector 立刻截图 + WS 上报 + 中控 forensic_log + 暂停工位

这是 patchright 没有的优势：**扩展能 fail-fast + 精准告警**，比 V1 的"silent timeout 60s"运维体验好得多。**但 locale 层维护工作不消失**。

#### 扩展性能硬化（v0.6 NEW，C-062）

> v0.5 假设扩展跑客户日常 chrome 是优势。**v0.6 修正**：扩展持续 inject + MutationObserver + chrome.alarms 30s 会持续影响日常浏览体验 → 必须性能硬化。

##### 1. content script 懒加载（不要 manifest auto match）

```typescript
// extension/src/background.ts
// ❌ v0.5 写法：manifest content_scripts auto match → 每打开 Flow page 都 inject
// ✅ v0.6 改：sw 收到 task_assign 时再用 chrome.scripting.executeScript 主动 inject
async function dispatchTask(task: Task) {
  const { window_id, tab_id } = await pickWorkstationTab(task)
  // 主动 inject content script
  await chrome.scripting.executeScript({
    target: { tabId: tab_id },
    files: ['content.js'],
  })
  ws.send({ type: 'task_assigned_to_tab', tab_id })
}

async function onTaskComplete(task_id: string, tab_id: number) {
  // 任务结束 → 清理 inject 的 listener / observer
  await chrome.scripting.executeScript({
    target: { tabId: tab_id },
    func: () => { window.__FLOW_HARVESTER_CLEANUP__?.() }
  })
}
```

manifest 改：去掉 `content_scripts` auto match，留 `host_permissions` 给 chrome.scripting（已有）。

效果：客户打开 Flow page 不跑任务 → 不 inject → 客户日常浏览 0 性能开销。

##### 2. MutationObserver 限缩范围 + throttle

```typescript
// extension/src/content/flow_state.ts
// ❌ 监听整个 document 子树 (V1 过的写法)
// observer.observe(document.body, { subtree: true, childList: true })

// ✅ 限缩到 Flow main container（labs.google specific selector）
const flowMain = document.querySelector('main[data-flow-app]') ?? document.querySelector('#app-root')
const observer = new MutationObserver(throttle(handleDomChange, 100))   // 100ms throttle
observer.observe(flowMain, { subtree: true, childList: true })

// 任务结束 disconnect
window.__FLOW_HARVESTER_CLEANUP__ = () => observer.disconnect()
```

##### 3. chrome.alarms 周期动态

```typescript
// extension/src/background.ts
function adjustAlarmPeriod() {
  const hasActiveTask = Object.values(TAB_STATE).some(s => s.task_id !== null)
  if (hasActiveTask) {
    chrome.alarms.create('heartbeat', { periodInMinutes: 0.5 })  // 30s 跑中
  } else {
    chrome.alarms.create('heartbeat', { periodInMinutes: 5 })    // 5min idle（chrome 后台 throttle 后实际不会 < 1min）
  }
}
```

效果：5 工位 idle 时累计 CPU 抖动从 5/30s × 5 = 10/min 降到 5/5min = 1/min。

##### 4. spike 验收新增 #17 性能 benchmark（v0.6 NEW，C-062）

装 V2 扩展 + chrome 开 10 个客户日常 tab + V2 跑 1 个任务 → 测：
- chrome 主线程 CPU 增量（vs 不装 V2 的 baseline）≤ 5%
- chrome RAM 增量 ≤ 200 MB / 工位
- Flow page render 帧率不掉到 30fps 以下

#### 扩展 self-check（v0.6 NEW，C-067）

> 操作员误操作 chrome 设置（清浏览数据 / disable 扩展 / 改下载目录）会 silent 破坏 V2。扩展必须主动 self-check + 中控告警。

```typescript
// extension/src/background.ts
async function selfCheck() {
  const issues: string[] = []

  // 1. ws_token 完整性（防客户清浏览数据，C-055 串联）
  const token = await storage.get('ws_token')
  if (!token) {
    // 自愈：从 Co-Pilot 重 fetch（C-055 双持久化）
    const recovered = await fetch('http://127.0.0.1:8080/extension/token?profile_id_hash=...')
    if (recovered.ok) {
      await storage.set({ ws_token: await recovered.text() })
    } else {
      issues.push('ws_token_lost')   // 需操作员重做 setup wizard
    }
  }

  // 2. setup_complete 标志
  const setup = await storage.get('setup_complete')
  if (!setup) issues.push('setup_incomplete')

  // 3. chrome.downloads default folder 是否被改
  const downloads = await chrome.downloads.search({ limit: 1 })
  if (downloads[0] && !downloads[0].filename.includes('FlowHarvester')) {
    issues.push('download_folder_changed')
  }

  // 4. chrome.management 自身扩展状态（不可读自身但能 fetch Co-Pilot 间接判）
  // sw 跑 = 自身没被 disable（disable 后 sw 不跑）；这条不需要 self-check

  // 5. chrome power state（C-035）
  const powerLevel = await chrome.power?.requestKeepAwake?.('display')
  // 实测 chrome.power API 仅特定权限可用，简化为 navigator.getBattery + Win11 API

  // 上报中控
  if (issues.length) {
    ws.send({ type: 'self_check_failed', issues })
  }
}

// 触发时机
chrome.runtime.onStartup.addListener(selfCheck)
chrome.alarms.create('self_check', { periodInMinutes: 60 })
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'self_check') selfCheck() })
```

中控 dashboard 显示 self-check 结果，红色告警操作员误操作。

### 4.3 通信协议

#### WebSocket 端点

```
ws://localhost:8080/ws/extension/<ws_id>
```

`<ws_id>` 是工位 id（WS_A / WS_B / ...），扩展启动时从 `chrome.storage.local` 读 + 跟用户绑定的账号关联。

#### 消息 schema（JSON over WebSocket）

```typescript
// 协议 v1（C-011 修补，C-015 / C-018 / C-020 加固）
// 所有 message 必须带 ws_token（C-015 防 localhost 假冒）
type Envelope<T> = T & { ws_token: string; protocol_version: 1 }

type ErrorType =
  | 'generation_failed' | 'unusual_activity' | 'no_flow_access'
  | 'service_unavailable' | 'audio_failure' | 'timeout'
  | 'locale_drift' | 'extension_crash' | 'page_navigation_failed'
  | 'download_failed' | 'asset_missing' | 'flow_project_unreachable'

// 扩展 → 中控
type ExtensionToCenter =
  | {
      type: 'register'
      workstation_id: string             // 操作员 wizard 输入
      account_email: string              // C-018 anchor #1
      profile_id_hash: string            // C-018 anchor #2（chrome.runtime.id + 首次生成 UUID）
      extension_version: string
      chrome_version: string              // C-025 兼容矩阵
    }
  | {
      type: 'heartbeat'
      timestamp: number
    }
  | {
      type: 'task_progress'
      task_id: string
      round: number
      stage: 'uploading' | 'prompt_typed' | 'create_pending' | 'create_committed'
              | 'generating' | 'downloading' | 'complete' | 'error'
      flow_project_url?: string          // C-011 SPA 切 project 信号
      details: object
    }
  | {
      type: 'task_complete'
      task_id: string
      round: number
      mp4_files: Array<{
        filename: string                 // chrome.downloads 实际写入的最终名（C-019）
        size: number
        sha256?: string
      }>
    }
  | {
      type: 'task_error'
      task_id: string
      error_type: ErrorType              // C-011 改 enum
      error_message: string
      screenshot_data_url?: string
    }
  | {
      type: 'task_resume'                // C-001 sw 重连后跟中控对账
      task_id: string
      last_known_stage: string
      last_known_round: number
      flow_project_url?: string
    }
  | {
      type: 'log'                        // C-017 扩展端日志推送中控
      level: 'debug' | 'info' | 'warn' | 'error'
      message: string
      stack?: string
    }
  | {
      type: 'login_state'
      logged_in: boolean
      account_email?: string
    }

// 中控 → 扩展
type CenterToExtension =
  | {
      type: 'task_assign'
      task_id: string
      flow_project_url: string
      asset_paths: string[]
      prompt: string
      target_count: number               // C-011 multi-round 早停判断
      inter_round_pause_sec?: number     // C-011 客户 yaml override
      stagger_sec?: number
      mode: {
        tab: 'video'
        subtab: 'ingredients' | 'frames'
        aspect: string
        output_count: number
        duration_sec: number
        model: string
      }
    }
  | {
      type: 'cancel_task'
      task_id: string
      reason: string                     // 用于 audit
      force?: boolean                    // v0.6 NEW，C-061 — 客户明确承担配额代价时 true
    }
  | {
      type: 'cancel_rejected'            // v0.6 NEW，C-061 — 扩展回报 cancel 阶段超过 create_committed
      task_id: string
      stage: 'create_committed' | 'generating' | 'downloading'
      committed_generation_count: number  // 已 commit Veo 配额数
    }
  | {
      type: 'screenshot_request'         // C-011 / C-017 中控主动让扩展截图
      reason: string
    }
  | {
      type: 'health_check'
    }
  | {
      type: 'config_update'
      stagger_sec?: number
    }
```

#### 文件传输（asset 上传 + mp4 下载）

##### asset 上传：扩展 fetch 中控

任务素材图存 co-pilot `assets/<task_id>/`。扩展的 content script 直接 `fetch('http://localhost:8080/files/<rel_path>')` 拿图（manifest host_permissions 已声明 localhost，C-012），用 `DataTransfer` API 上传给 Flow page file input。中控 `/files/` 路由（V1 已有）服务此路径。

##### mp4 下载（C-005 / C-019 选 A 默认）

> **决策反转**（v0.1 选 C → v0.3 改 A）：方案 C 用 WebSocket 传 200MB mp4 有内存 / 可靠性 / message size 三重风险（C-005）。方案 A 让 chrome.downloads 直接落到 Co-Pilot output 子目录最稳。

**方案 A（首选）**：`chrome.downloads.download({url, filename, conflictAction: 'uniquify'})`

```typescript
// 扩展端
const filename = `${ws_id}_${task_id}_r${round}_v${idx}_${ts}.mp4`  // C-019 防冲突
chrome.downloads.download({
  url: flow_video_url,
  filename: `FlowHarvester/output/${date}/${sku}/${creative}/segment_${seg}/${filename}`,
  conflictAction: 'uniquify',
}, (downloadId) => {
  chrome.downloads.onChanged.addListener((delta) => {
    if (delta.id === downloadId && delta.state?.current === 'complete') {
      chrome.downloads.search({id: downloadId}, (items) => {
        const final_filename = items[0].filename  // 实际写入路径，可能被 uniquify 改
        ws.send({type: 'task_complete', mp4_files: [{filename: final_filename, size: items[0].fileSize}]})
      })
    }
  })
})
```

**Co-Pilot 端**：监听 chrome 默认下载目录的 `FlowHarvester/output/` 子树（用 `watchdog` 库），新文件 stat 校验大小 → 关联到 task_results。

**filename 防冲突（C-019）**：
- 文件名包含 `ws_id` + `task_id` + `round` + `video_idx` + `ts` 唯一确定
- chrome.downloads `conflictAction: 'uniquify'` 兜底（极端情况 `(1)` 后缀）
- 中控收到 task_complete 后跟预期 filename 比对，不一致告警（说明撞名 = 配置 bug）

**NTFS filename sanitize（v0.5 NEW，C-049）**：

NTFS 不允许 `: < > | / \ ? *`，Win 路径长度默认 260 字符。SKU 含中文 / 特殊字符 / 空格的处理：

```python
# Co-Pilot 端 (app/files/sanitize.py)
import unicodedata

NTFS_FORBIDDEN = re.compile(r'[<>:"|?*\/\\]')

def sanitize_path_segment(name: str, max_len: int = 100) -> str:
    """SKU / creative 字段过 sanitize 才进 chrome.downloads filename"""
    # 1. NFC normalize unicode（防 macOS NFD 落到 Win NTFS 出问题）
    name = unicodedata.normalize('NFC', name)
    # 2. 替换 NTFS 禁字符
    name = NTFS_FORBIDDEN.sub('_', name)
    # 3. 截断
    name = name[:max_len]
    # 4. 防尾部空格 / 句点（NTFS 禁）
    return name.strip(' .')

def assemble_download_filename(...) -> str:
    parts = [sanitize_path_segment(date), sanitize_path_segment(sku), sanitize_path_segment(creative), ...]
    full = '/'.join(parts) + '/' + filename
    assert len(full) < 220, f'path too long: {full}'  # 留 40 字节 buffer
    return full
```

- DB 保留原文 SKU；chrome.downloads filename 用 sanitized
- 中文 SKU 用 NFC 后落盘 OK（chrome 内部用 utf-8）；如客户机 chcp ≠ utf-8 编码不一致 → install wizard 检测
- spike #4 验收升级（C-049）："含中文 / 特殊字符 SKU 的 filename 落盘正确 + 总长度 < 220"

**方案 B（fallback）**：方案 A 撞 chrome 安全策略不允许深路径时，落 chrome 默认目录 + 监控移动到 `output/`。

**方案 C（不再首选）**：扩展 POST 二进制给中控。仅作为 A/B 双失败时的最后选项；如果走必须用 HTTP chunked POST 不是 WebSocket（C-005）。

**spike 验收 #4（C-005）**：实测 200 MB 文件方案 A 可写到 Co-Pilot 指定子目录吗？5 profile 并发同 task 同 round 是否撞名？

##### mp4 codec 实测 + 客户披露（v0.6 NEW，C-066）

V1 没记录 Veo 输出 mp4 codec。V2 spike 阶段必须实测 + 客户文档披露：

- spike #4 同时记录：Veo 输出 mp4 的 codec（h264 / h265 / VP9）+ 容器（mp4 / webm）+ 分辨率 + 帧率 + 音轨编码
- `customer-manual.md` 加"输出文件兼容性"段：列实测 codec + 客户 NLE（Premiere / 剪映 / DaVinci）/ Win Media Player 兼容性
- 客户 NLE 不直接支持 → 提供 ffmpeg 转码命令模板（一行 sh 脚本）

### 4.4 任务生命周期 + 恢复设计（NEW，C-009 / C-001 / C-020）

V1 的任务恢复（`reset_zombie_state_on_startup` + `MAX(generation_round)` 权威源）只覆盖"patchright 进程重启"。V2 多了"扩展 sw hibernate / chrome 关闭 / chrome update / 网络断"等失稳场景，必须重新设计。

#### 4.4.1 任务状态机

```
              ┌── pending（中控 DB 初始）
              │
              ▼
        assigned（中控派给某 ws，等扩展 ack）
              │
              ▼
       (ws 接收并 chrome.storage 持久化)
              │
              ▼
        uploading（扩展正在上传 asset）
              │
              ▼
       prompt_typed
              │
              ▼
       create_pending（扩展点了 Create 但等待 Flow project 状态变化确认提交，C-020）
              │
              ▼
       create_committed（Flow project URL 切换 / generation 列表新增 = Veo 后端已收）
              │
              ▼
       generating（等候 Veo 出 mp4，60-300 秒）
              │
              ▼
       downloading（chrome.downloads 拉取 mp4）
              │
              ▼
       round_complete（一个 round 全部 video 落盘，更新 task_results）
              │
              ▼
   (multi-round 循环 → 回 prompt_typed 或新 prompt_typed)
              │
              ▼
       task_complete（target_count 达成）

任何状态都可能 → error（明确归类） / interrupted（chrome 关 / sw hibernate 失稳超时）
```

#### 4.4.2 状态持久化（双写 + v0.5 SQLite 并发硬化，C-046）

每个状态 transition **同步双写**：
- ✅ 扩展端：`chrome.storage.local.set({task_state: {task_id, stage, round, flow_project_url, ts}})`
- ✅ 中控端：通过 WS message → `task_progress` → 写 SQLite `tasks.error_type / error_message / generation_round_count`

**双写失败兜底**：扩展端 storage 是真理之源（chrome 不会丢）；中控端如果 message 丢，扩展重连后 `task_resume` 主动同步。

##### SQLite WAL 高并发承载（v0.5 NEW，C-046）

V1 单 worker SQLite 写频率低。V2 多扩展并发上报放大写压力 1-2 个数量级（5 ws × heartbeat + log push + state transition = 高峰 1000+ 写/分钟）。WAL 模式 1 writer + N readers，写排队 + checkpoint 锁库 = 风险。**v0.5 强制实施**：

| 缓解 | 实施 |
|---|---|
| **task_progress batch** | extension_dispatcher.py 不每个 transition 立即写 SQLite；5 秒内同 task 的 progress 合并写（保留最后状态） |
| **关键状态立即写** | create_pending / create_committed / round_complete / task_complete / task_error 不 batch（防 Veo 双扣费 / 状态丢失） |
| **扩展端 console.log batch** | 扩展 chrome.storage.local 累积 + 5 秒 batch 推送中控（不每条立即 push） |
| **forensic_log 按日 partition** | `forensic_log_20260510 / forensic_log_20260511 / ...`；30 天后 DROP TABLE 老分区（C-037 串联） |
| **WAL checkpoint 策略** | `PRAGMA wal_autocheckpoint=1000`（默认）；凌晨 3am `PRAGMA wal_checkpoint(TRUNCATE)` 强制 checkpoint；WAL 文件 > 100 MB 告警 |
| **busy_timeout** | `PRAGMA busy_timeout=5000`（5s）让并发写有 retry 机会；应用层捕获 `OperationalError` 自动 retry 3 次 |
| **dashboard read replica 预演** | dashboard 用 read-only connection (`PRAGMA query_only=1`) → V3 上 PostgreSQL 时切 read replica |

**spike 验收新增 #15（C-046）**：模拟 5 工位 × 1 周连续跑，监测 SQLite 写延迟（p95 < 50ms）/ WAL 大小（< 200 MB）/ `database is locked` 错误率（< 0.01%）。

#### 4.4.3 中断恢复路径（v0.4 加 reconnect storm 防御 + 时间同步，C-034）

| 中断场景 | 检测 | 恢复 |
|---|---|---|
| sw hibernate（30s idle） | sw 重新唤醒时拿不到内存上下文 | 从 chrome.storage 读最后状态；重连 WS；发 `task_resume` 跟中控对账 |
| WS 断（网络抖 / 中控重启） | sw `onclose` 事件 | **指数退避 + jitter**（5s + random(0,30s) → 双层封顶 30s，C-034）；重连后发 `task_resume` |
| chrome 关（操作员误关） | 中控 WS 断 + 60s 没 reconnect | 中控 flip ws → offline；任务**不立即 mark failed**，等 60-120s grace（C-001）；超时后 task → `interrupted`，等扩展重连或操作员手动 resume |
| chrome update（扩展 disable） | 同上但操作员能力限制 | 中控 dashboard 显著告警："WS_X 失联超过 N 分钟"；提供"resume"按钮 |
| Co-Pilot 重启 | 中控启动时调 `reset_zombie_state_on_startup`（V1 沿用） | 任务从 `running` flip 回 `retry_waiting`；下次扩展 register 时 task_resume 拉起 |
| **多 ws 同时 reconnect storm**（v0.4 NEW，C-034） | Co-Pilot 重启 → N 个扩展同时 detect WS 断 | 扩展端 jitter（5s + random(0,30s)）+ 中控端 register stagger（每 ws 间隔 5-10s mark online）+ "warm up window"（重启后 1 分钟不主动派 task） |
| **客户 PC 时间偏移 / DST**（v0.4 NEW，C-034） | register 时 client_ts vs server_ts diff > 5min | 中控 register reject + 提示操作员校准 PC 时间；log 时间戳一律用 server_ts；超时判断用 monotonic clock |

#### 4.4.5 reconnect storm 防御（v0.4 NEW，C-034）

V1 单 worker 不存在 storm。V2 5 个扩展同时 detect WS 断会风暴：

**扩展端（jitter 错峰）**：

```typescript
// extension/src/lib/ws_client.ts
async function reconnectWithJitter(retryCount: number) {
  const base = Math.min(30, Math.pow(2, retryCount))  // 1, 2, 4, 8, 16, 30
  const jitter = Math.random() * 30                    // 0-30s 随机
  const delay = base + jitter                          // 1+rand ~ 60s
  await new Promise(r => setTimeout(r, delay * 1000))
  ws.connect()
}
```

效果：5 个扩展 backoff 起点不同 → 60 秒内陆续 reconnect，不集中撞 server / Google 风控。

**中控端（register stagger + warm-up window）**：

```python
# app/extension_dispatcher.py
WARM_UP_WINDOW_SEC = 60
COPILOT_START_TS = monotonic()

def on_register(ws_id, register_msg):
    # 1. 校验 client_ts vs server_ts
    diff = abs(register_msg.client_ts - time.time())
    if diff > 300:  # 5 min
        return reject('clock_skew', f'PC clock off by {diff}s, please sync')

    # 2. ack 但不立刻 mark online — 按到达顺序间隔 5-10s
    pending_register_queue.put(ws_id)

    # 3. 后台 task 每 5-10s 从 queue 拿一个 mark online
    # ...

def can_assign_task(ws_id):
    # warm up window: Co-Pilot 重启后 1 分钟不主动派 task
    if monotonic() - COPILOT_START_TS < WARM_UP_WINDOW_SEC:
        return False
    return ws.online and ws.healthy
```

#### 4.4.6 时间同步防御（v0.4 NEW，C-034）

客户 Win11 NTP 失败 / 时区错 / DST 切换会影响 heartbeat 时序判断：

- ✅ **WS register**：扩展上报 `client_ts`，Co-Pilot 比对自身 ts → diff > 5min reject + 告警操作员
- ✅ **log 时间戳一律 server_ts**（Co-Pilot 收到时打），不信 client_ts
- ✅ **daily report 用 server timezone**（不是 client）
- ✅ **heartbeat 超时判断用 monotonic clock**（Python `time.monotonic()`），不受 DST / NTP 跳变影响
- ✅ **forensic_log 双字段**：`server_ts` 主，`client_ts` 仅 audit；debug 时序混乱时优先看 server_ts

#### 4.4.4 Veo 双扣费防御（C-020）

最严重场景：扩展点了 Create，sw hibernate，中控 timeout retry → 扩展重连后再点 Create → Veo 后端收 2 份请求 → 客户配额双扣。

**防御**：
1. **点 Create 前后**双重持久化：
   - 之前：chrome.storage 写 `pending_create: {task_id, round, ts}` + WS push `create_pending`
   - 之后：等 Flow project 状态变化（V1 v0.0.3 已知信号：URL 切换 / generation 列表新增 N 项）→ chrome.storage 写 `create_committed: {project_url, generation_ids, ts}` + WS push
2. **中控收到 `create_pending` 后不主动 retry**：
   - 状态机 transition 到 `awaiting_commit`
   - 即使 WS 断了不重派；等扩展重连后扩展自己上报 `create_committed` 或 `create_aborted`
   - 超过 30 分钟没收到任何信号 → mark task `unknown_state` → 操作员手动决策
3. **Veo 后端状态查询**：扩展重连后查 Flow project page 的 generation 列表（DOM 可读）；如果发现跟 pending_create 时间窗口匹配的 in-progress generation → 认为已提交 → 不再点 Create
4. **配额预警**：中控记录每账号每日 Create click 次数；超 12 次告警，停派该账号

**spike 验收 #6（C-020）**：连续 2 次快速点同一 Create button，Veo 后端是否生成 2 份？如果 Veo 自己 dedup → C-020 严重度降级；如果不 dedup → 上述防御必须实现。

#### 4.4.7 cancel_task 阶段约束 + 残留 mp4 兜底（v0.6 NEW，C-061）

> v0.5 cancel_task 是 "fire and forget"，没说不同 lifecycle 阶段的实际行为。Veo 后端 click Create 后不可取消 → 客户配额 silent 浪费。

##### 阶段约束矩阵

| 当前 stage | cancel_task 行为 | 配额代价 |
|---|---|---|
| `pending` | ✅ OK，扩展未收 | 无 |
| `assigned` / `uploading` / `prompt_typed` | ✅ 扩展中止 | 无 |
| `create_pending`（Create 已点等 Veo 确认） | ⚠️ 扩展回报 `cancel_rejected: create_pending`；中控显示"等待提交确认"；force=true 才中止 | 可能已扣 |
| `create_committed` / `generating` | ❌ 扩展回报 `cancel_rejected: already_committed`；除非 force=true 否则不取消 | 已扣 |
| `downloading` | ✅ 扩展中止下载，但 mp4 在 Flow page 仍可下载（兜底，见下文） | 已扣（无法退） |

##### Dashboard UX 二次确认

```
[操作员点 "取消任务" 按钮]
  ↓
中控查 task 当前 stage
  ↓
stage ≥ create_committed:
  弹对话框：
  ┌─────────────────────────────────────────┐
  │ ⚠️ 任务已提交 Veo                          │
  │ 该任务进度 67% / Veo 配额已扣 4 个 mp4     │
  │ 取消后 mp4 仍生成但不下载                   │
  │ 配额无法退还                                │
  │                                              │
  │ [仍然取消] [继续等待]                       │
  └─────────────────────────────────────────┘
  ↓
用户点"仍然取消" → 中控派 cancel_task with force=true
```

##### 残留 mp4 兜底下载（v0.6 NEW，C-061）

cancel 后 Veo 仍生成的 mp4 在 Flow project page 可见。扩展兜底处理：

```typescript
// extension/src/content/flow_dom.ts
async function onCancelAfterCommit(task_id: string) {
  // 不立即关 page，等 5 分钟 poll Flow page 看残留 mp4
  for (let i = 0; i < 20; i++) {
    const remaining = await detectRemainingMp4(task_id)
    if (remaining.length > 0) {
      // 仍下载（写到 output_v2/cancelled/<task_id>/...）
      for (const m of remaining) {
        await chrome.downloads.download({
          url: m.url,
          filename: `FlowHarvester/output_v2/cancelled/${task_id}/${m.filename}`,
        })
      }
      ws.send({
        type: 'task_complete_after_cancel',
        task_id,
        committed_count: remaining.length,
        downloaded_count: remaining.length,
      })
      return
    }
    await new Promise(r => setTimeout(r, 15_000))  // 15s 间隔
  }
}
```

DB 记录"任务 cancel 但配额已用 N 个 + 残留 mp4 全下载到 output_v2/cancelled/"，dashboard 显示账号当日 Create 计数（包括 cancel 部分）。

##### 配额账户级监控

- 中控记每 account_email 每日 Create count（无论 cancel 与否）
- dashboard 显示账号配额使用率
- 超 daily_quota_alert 阈值 → 暂停派该账号 task 直到次日

### 4.5 WS_id ↔ chrome profile 绑定（NEW，C-018）

V1 把 ws_id ↔ chrome profile 一一映射放中控配置（`workstations.browser_profile_path`）。V2 扩展自己上报 ws_id 给中控，**误配 silent 灾难**风险高（操作员 5 profile × 5 ws_id 容易选错）。

#### 4.5.1 双因子绑定 anchor

扩展 `register` 必须带：
- **anchor #1：account_email**（chrome.identity API）— 操作员可见可读
- **anchor #2：profile_id_hash** — chrome.runtime.id + 首次安装时生成的 UUID，存 chrome.storage.local；profile 重装会变

中控 `workstations` 表新增列：
```sql
ALTER TABLE workstations ADD COLUMN expected_email TEXT;
ALTER TABLE workstations ADD COLUMN bound_profile_id_hash TEXT;
```

`register` 校验：
- email 跟 expected_email 不一致 → `register_rejected: email_mismatch`
- profile_id_hash 跟 bound_profile_id_hash 不一致 → `register_rejected: profile_changed`

操作员看到红色告警，必须中控 admin 手动 unbind + rebind。

#### 4.5.2 首次绑定 wizard

扩展首次安装到 chrome profile 时弹 fullscreen options 页：

```
[首次设置 Flow Harvester]

1. 在中控 dashboard 点"绑定新工位"按钮 → 系统生成 setup_token
2. 复制 setup_token: AB7K-9X2P-MNQR-3F8L
3. 粘贴到下方:
   [_______________________]
4. 点确认

系统会自动:
✓ 跟中控握手 → 拿到分配的 ws_id（WS_A / WS_B / ...）
✓ 上报你的 Google 账号: xxx@gmail.com
✓ 绑定该 chrome profile
✓ 之后每次开 chrome 自动连中控
```

绑定完成后扩展 popup 顶部固定显示：

```
┌────────────────────────────────┐
│ Flow Harvester                  │
│ ─────────────────────────       │
│ 📍 Workstation: WS_A             │
│ 👤 Account:     foo@gmail.com    │
│ 🟢 Online                        │
└────────────────────────────────┘
```

操作员一眼能校。

#### 4.5.3 strike 系统硬绑定 email（C-018）

V1 strike 按 `workstation_id` 累计。V2 改按 `account_email` 累计——避免 ws_id 误配杀错号：

```python
# app/scheduler/state.py
def _apply_workstation_outcome(ws_id, outcome):
    ws = get_workstation(ws_id)
    # strike 累计目标改为 ws.account_email 而不是 ws.id
    if outcome == 'unusual_activity_strike':
        increment_strike_for_email(ws.account_email)
```

#### 4.5.4 误配纠正路径

| 错误情形 | 中控检测 | 缓解 |
|---|---|---|
| 操作员 wizard 输错 setup_token | token 不存在或已用 | 重新生成 setup_token，重新粘贴 |
| 操作员后来在 chrome 切了其他账号 | register 检测 email 变 | 暂停发送 task；告警操作员"工位 X 当前账号已变"；操作员 confirm 后 admin 手动 rebind |
| 操作员 chrome profile 重装 | profile_id_hash 变 | 同上；若 email 仍一致可半自动 rebind |

**spike 验收 #7（C-018）**：模拟操作员误配（A profile 装到 WS_B），中控应在第一次 task_assign 前就 reject。

### 4.6 多 tab + 多 window 管理（v0.5 NEW + v0.6 升级，C-043 / C-059）

V1 单 patchright chrome 实例 → 1 个 tab，无歧义。V2 客户日常 chrome **多 tab + 多 window 同 profile**，扩展行为复杂：

#### 4.6.1 工位 active_window_id + active_tab_id（v0.6 改 C-059）

> v0.5 只有 active_flow_tab_id 单一指向，v0.6 升级为 (window_id, tab_id) 双因子（C-059）。chrome.tabs.Tab 含 windowId 字段；多 window 同 profile 是 chrome 用户日常常见行为。

扩展 service worker 维护 per-workstation 状态：

```typescript
// extension/src/background.ts
interface WorkstationTabState {
  ws_id: string
  active_window_id: number | null      // v0.6 NEW，C-059
  active_tab_id: number | null
  task_id: string | null
  tab_pinned_at: number
}

const TAB_STATE: Record<string, WorkstationTabState> = {}
```

#### 4.6.2 任务派发时 window + tab 选择 + 锁定（v0.6 改 C-059）

```typescript
async function dispatchTask(task: Task) {
  const state = TAB_STATE[ws_id]

  // 1. 复用已有的 (window, tab)
  if (state.active_window_id && state.active_tab_id) {
    const tab = await chrome.tabs.get(state.active_tab_id).catch(() => null)
    if (tab && tab.windowId === state.active_window_id) {
      chrome.tabs.update(tab.id, { active: true, url: task.flow_project_url })
      return { window_id: tab.windowId, tab_id: tab.id }
    }
  }

  // 2. v0.6 single-window 强制策略（C-059）：找/创建工位专用 window
  let workWindow = state.active_window_id
    ? await chrome.windows.get(state.active_window_id).catch(() => null)
    : null
  if (!workWindow) {
    workWindow = await chrome.windows.create({
      url: task.flow_project_url,
      type: 'normal',
      focused: true,
    })
  }

  // 3. 在 workWindow 内创建 / 复用 tab
  const flowTabs = await chrome.tabs.query({
    url: 'https://labs.google/fx/tools/flow/*',
    windowId: workWindow.id,
  })
  const tab = flowTabs.length > 0
    ? flowTabs.sort((a, b) => b.lastAccessed - a.lastAccessed)[0]
    : await chrome.tabs.create({ windowId: workWindow.id, url: task.flow_project_url, active: true })

  // 4. tab + window 锁定（视觉提示）
  await chrome.tabs.update(tab.id, { pinned: true })
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (wsId) => { document.title = `[${wsId} 工作中] ${document.title}` },
    args: [ws_id],
  })

  TAB_STATE[ws_id] = {
    ws_id,
    active_window_id: workWindow.id,
    active_tab_id: tab.id,
    task_id: task.task_id,
    tab_pinned_at: Date.now(),
  }
  return { window_id: workWindow.id, tab_id: tab.id }
}
```

#### 4.6.3 tab / window 关闭 / 切后台 / restore 处理（v0.6 加 window 维度，C-059）

| 场景 | 检测 | 处理 |
|---|---|---|
| 操作员手抖关 tab | `chrome.tabs.onRemoved` 回调（同时校验 windowId） | task → `interrupted`；WS 上报中控；dashboard 红色告警可 resume |
| **操作员关整个 window**（v0.6 NEW） | `chrome.windows.onRemoved` 回调 | 同上；TAB_STATE.active_window_id 清空 |
| 操作员切到别的 tab（Flow tab 后台） | content script 检测 `document.visibilityState === 'hidden'` | 关键步骤前 sw 调 `chrome.tabs.update({active: true})` + `chrome.windows.update({focused: true})` 切前台 |
| chrome restore 上次 tab/window（重启后） | sw 启动检测 chrome.storage 有 pending task → 不立即 resume | 先验证 page state；多 Flow window 时弹 popup 让操作员选哪个是工位；不匹配 → mark `interrupted` |
| 操作员误开新 Flow tab 切别的 project | 新 tab content script 上报 project_url + windowId ≠ TAB_STATE 的 active | 中控按 active_window_id 派 task，新 tab 不抢；popup "WS_X 已锁 window Y" |
| **操作员开第二 chrome window（同 profile）**（v0.6 NEW，C-059） | `chrome.windows.onCreated` 回调 + 检测 incognito state | 视觉告警操作员"V2 不应在多 window 同时开 Flow"；如果新 window 加载 Flow URL → 自动 close 或弹 popup 让操作员选 |
| **chrome DevTools 弹独立 window** | `chrome.windows.onCreated` type === 'devtools' | 忽略（不影响 inject，但占资源 — RAM 监控告警） |
| **操作员开 incognito window** | windowId.incognito === true | 不 inject V2（manifest `incognito: not_allowed`，C-038）+ chrome.power 监控 RAM 压力（C-007 串联） |

#### 4.6.4 后台 tab throttle 防御

chrome 后台 tab 会 throttle：MutationObserver 触发率降 / setTimeout 受 throttle / chrome 109+ Memory Saver 暂停整 tab。

- ✅ 关键时序操作前 `chrome.tabs.update({active: true})` 主动切前台
- ✅ `chrome.alarms` 30s 周期（不受 setTimeout throttle 影响）作为 fallback 心跳
- ✅ chrome 启动 flag `--disable-background-tab-throttling`（chrome_profile_launcher.py Plan B 启 chrome 时加）

**spike 验收新增 #13（C-043）**：开 5 个 Flow tab（不同 project）+ 操作员手动关其中正在跑任务的 tab → 验证扩展上报 `interrupted` + dashboard 告警 + 任务可 resume。

**spike 验收新增 #18（v0.6 NEW，C-059）**：操作员同 profile 开 2 个 chrome window 都装 V2 + 加载 labs.google → 验证 sw 按 (window_id, tab_id) 区分 + 派 task 给正确 window；新开非工位 window 时 popup 告警操作员。

### 4.7 扩展自身 update lifecycle（v0.5 NEW，C-048）

unpacked 升级 = 操作员手动 chrome://extensions reload → sw + content script 同时 kill → page reload 后重新 inject。跑中任务直接断。

#### 4.7.1 升级前 idle check

```typescript
// extension/src/background.ts
// chrome.runtime.onUpdateAvailable 在 unpacked 不会触发；通过中控 WS push 'extension_update_available' 通知
ws.on('extension_update_available', async (msg) => {
  const activeCount = await countActiveTasks()
  if (activeCount > 0) {
    showWarningBanner(`当前 ${activeCount} 个任务跑中，建议 idle 后再升级`)
    return
  }
  // 全 idle → 提示操作员手动 reload
  showActionBanner('全部 idle，可以现在 chrome://extensions 点 reload')
})
```

#### 4.7.2 onSuspend flush

```typescript
// sw 关闭前 chrome 给 30s grace
chrome.runtime.onSuspend.addListener(async () => {
  await flushAllTaskState()           // 强制写 chrome.storage.local
  ws.send({ type: 'sw_suspend' })     // 通知中控
})

// 新 sw 启动时检测"上次没正常 shutdown"
chrome.runtime.onStartup.addListener(async () => {
  const lastClean = await storage.get('last_clean_shutdown_ts')
  if (!lastClean || Date.now() - lastClean > 60000) {
    // 上次没正常 shutdown → 标记所有 active task interrupted
    await markAllActiveTasksInterrupted()
  }
})
```

#### 4.7.3 协议 forward compat

V2.x WS message envelope `protocol_version` 字段每次升级递增。**V2.1 sw 必须接受 V2.0 message 形态**（不破坏字段）：

```typescript
// V2.1 dispatcher 收到 register 时校验
if (registerMsg.protocol_version < V2_MIN_SUPPORTED_PROTOCOL) {
  return reject('protocol_version_too_old', '请升级扩展到 V2.x')
}
// V2.1 给 V2.0 扩展派 task 时只用 V2.0 字段，不带 V2.1 新字段
```

#### 4.7.4 unpacked 升级时机告知

- Co-Pilot dashboard 加"建议在 dashboard 全 idle 时升级"提示
- 自动检测 idle 时间 > 30 分钟才允许触发升级流程
- 升级流程：dashboard "升级到 v2.x" 按钮 → 检测每个 profile 扩展状态 → step-by-step guided wizard 带操作员每个 profile chrome://extensions reload

**spike 验收新增 #14（C-048）**：V2.0 任务跑中触发扩展手动 reload → 验证 onSuspend flush 完成；新 sw 启动时 mark task interrupted；中控收到 sw_suspend 不主动 retry（防 Veo 双扣费）。

### 5.1 操作员日常流程

```
早上：
  1. 开机，Co-Pilot 自动启动（Windows service）
  2. 打开 Chrome，启动多个 profile（profile_A, profile_B, profile_C, ...）
  3. 每个 profile 的扩展自动连中控 ws://localhost:8080
  4. 浏览器访问 http://127.0.0.1:8080 看 dashboard
  5. dashboard 显示"5 个工位 online: WS_A WS_B WS_C WS_D WS_E"
  6. 加载 CSV / 创建任务
  7. 点"启动调度器"

白天：
  ─ 中控按 stagger 派 task 给空闲扩展
  ─ 扩展在自己 profile 的 Flow 页面执行：上传 / 输 prompt / 点 Create / 等结果 / 下载 mp4
  ─ 扩展每步报进度回中控
  ─ Dashboard 实时显示
  ─ 操作员可以最小化 Chrome（不要关）
  ─ 如果某个 profile 撞 unusual_activity，扩展上报，中控按 strike 系统处理（不变）

晚上：
  ─ Dashboard 上看日报（产能 / 成功率 / 错误分布）
  ─ 关 Chrome（中控继续跑，明天接着用）
```

### 5.2 跟 V1 的对比

| 操作 | V1 (v0.1.0) | V2 |
|---|---|---|
| 安装 | 双击 .exe | 装 Co-Pilot + chrome 装扩展（Setup wizard 引导） |
| 启动工位 | 点"登录"→ patchright 启 chrome | Chrome 已开 → 扩展自动注册 → 工位 online |
| 加账号 | 点登录 + 切换语言 | 在 chrome 里登录该账号（正常用法）→ 扩展自动检测 + 注册 |
| Locale 问题 | 多语言列表 + 操作员手动切英文 | **不存在**——扩展用 DOM 结构定位 |
| Anti-bot | 持续磨损 | **不存在**——扩展是 chrome 一等公民 |
| 调试 | 诊断包 zip 邮件 / cloudflared | DevTools 直连 + 扩展可主动上报 |
| 后台跑 | patchright headless / headed | 操作员要保持 chrome 开（trade-off） |

### 5.3 安装路径（v0.5 大反转：unpacked-only，C-042 OBSOLETE / C-054 / C-055）

> **v0.5 架构约束**：用户明确**不上 Chrome Web Store**。extension 只走 unpacked 分发（manifest `key` 字段固定 ext_id，C-050）。
>
> ❌ ~~v0.4 commit "首选 Chrome Web Store"~~ → ✅ v0.5 commit **unpacked-only**
>
> 理由：(1) Web Store 对自动化 Google 自家产品（Veo / Flow）takedown 高敏感，下架后客户全停摆是单点故障（C-042，已 OBSOLETE 因约束反转）；(2) 用户决策保留分发完全可控；(3) 跟 V1 同等的"私有部署"语义。

> ⚠️ **G7 立项 gate（v0.5 改）**：~~扩展分发选 Web Store / unpacked~~ → **unpacked-only 部署摩擦客户能否接受**（chrome 主版本升级时 5 profile reload + chrome 警告 banner 长期存在）  
> ⚠️ **G10 立项 gate（v0.5 NEW，C-054）**：客户机 chrome 升级管控——能否锁版本 / 接受 update 期间停摆？  
> ⚠️ **G11 立项 gate（v0.6 NEW，C-057）**：客户机能装 unpacked 扩展吗？公司 GPO 是否禁 unpacked / 禁开发者模式 / 禁 ExtensionInstallSources？

#### 5.3.1 unpacked 分发流程（v0.5 主路径）

```
1. 下 Flow Harvester V2 zip（含 Co-Pilot.exe + extension/ + install.bat）
2. 双击 install.bat → 自动:
   - 装 Co-Pilot 到 %APPDATA%\FlowHarvester\
   - 配置 HKCU 注册表开机自启（C-033）
   - 写 ws_token 到 %APPDATA%\FlowHarvester\ws_token
   - install wizard 一次性检测（§3.1 + §5.3.5 GPO 检测 + §5.3.6 WS 网络层检测，v0.6 NEW）
3. 打开 chrome → chrome://extensions → 开"开发者模式" → 加载已解压 → 选 %APPDATA%\FlowHarvester\extension\
4. 重复步骤 3 在每个 V2 专用 profile（5 个账号 = 5 次）⚠️
5. 每个 profile 跑 setup wizard 输 setup_token（§4.5.2）
6. 完成
```

#### 5.3.5 chrome enterprise GPO 检测（v0.6 NEW，C-057）

> ⚠️ **客户机如果是公司机受 GPO 管控，可能完全禁 unpacked 扩展**。没 Web Store fallback 后这是 dead-end，必须 install wizard 早期检测。

**install wizard 必检 GPO 状态**：

```python
# Co-Pilot 启动时 + install wizard 一次性
import winreg

def check_chrome_gpo_blocks_unpacked() -> dict:
    """读 Win11 注册表 chrome enterprise policy"""
    block_signals = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Google\Chrome") as k:
            # ExtensionInstallBlocklist = '*' 拒所有非白名单
            try:
                blocklist, _ = winreg.QueryValueEx(k, "ExtensionInstallBlocklist")
                if "*" in blocklist:
                    block_signals.append("ExtensionInstallBlocklist=*")
            except FileNotFoundError:
                pass
            # BlockExternalExtensions / DeveloperToolsAvailability / ExtensionAllowedTypes
            for key, expected in [
                ("BlockExternalExtensions", 1),
                ("DeveloperToolsAvailability", 2),  # 2 = blocked
            ]:
                try:
                    v, _ = winreg.QueryValueEx(k, key)
                    if v == expected:
                        block_signals.append(f"{key}={v}")
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass  # 无 GPO 配置，最佳情况
    return {"blocked": bool(block_signals), "signals": block_signals}
```

**检测时机**：
- ✅ install wizard 第一步（早于装 Co-Pilot）→ GPO 拦 → 直接 fail + 不创建任何文件
- ✅ Co-Pilot 启动时再次校验（GPO 可能 deploy 后变化）

**失败处理**：

| GPO 状态 | install wizard 行为 |
|---|---|
| 无 GPO（个人机） | ✅ 正常进 |
| `ExtensionInstallSources` 白名单只允许 Web Store | ❌ fail；提供 GPO 修改模板让客户 IT 加 V2 安装路径白名单 |
| `BlockExternalExtensions=1` / `DeveloperToolsAvailability=2` | ❌ fail；客户公司 IT 必须改 GPO 才能装 V2 |
| `ExtensionInstallBlocklist=*` 全禁 | ❌ fail；V2 在该客户机不可部署，project manager 跟客户沟通 |

**chrome 命令行 fallback（GPO 部分允许时）**：

如客户 IT 同意，部分 GPO 仍允许 chrome `--load-extension=` flag。chrome_profile_launcher.py 启动 chrome 时带：

```python
# app/chrome_profile_launcher.py（仅 G11 fallback 路径）
subprocess.Popen([
    chrome_exe,
    f"--user-data-dir={profile_path}",
    f"--load-extension={EXT_PATH}",          # 命令行加载扩展
    "--no-first-run", "--no-default-browser-check",
])
```

代价：操作员开 chrome 必须经 Co-Pilot launcher，不能直接双击 chrome 桌面图标——客户体验差。

**G11 立项 gate**：客户机部署前必检 GPO 状态。不可装 → V2 不部署该客户机。

**GPO 配置模板**（项目根 `docs/chrome-policy/v2-allow-unpacked.reg`）：

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Google\Chrome]
"BlockExternalExtensions"=dword:00000000
"DeveloperToolsAvailability"=dword:00000001

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Google\Chrome\ExtensionInstallSources]
"1"="file:///C:/Users/*/AppData/Local/FlowHarvester/extension/*"
```

客户 IT 一次性 deploy 这个 .reg 文件即可允许 V2 部署。

#### 5.3.6 WS 网络层检测（v0.6 NEW，C-060）

> 设计稿假设 ws://localhost 通信"零延迟，零网络风险"——但客户机网络层有多重拦截（Defender Network Protection / 公司代理 / DNS / AV / Hyper-V）。

**install wizard 必检网络层**：

```python
# Co-Pilot 启动 + install wizard
import socket, requests

def check_ws_network() -> list[str]:
    failures = []

    # 1. 127.0.0.1:8080 listen 成功（FastAPI 启动后）
    try:
        sock = socket.create_connection(("127.0.0.1", 8080), timeout=3)
        sock.close()
    except OSError as e:
        failures.append(f"localhost:8080 unreachable: {e}")

    # 2. 主机 hosts 文件没改 localhost
    if socket.gethostbyname("localhost") != "127.0.0.1":
        failures.append("hosts file modified: localhost != 127.0.0.1")

    # 3. chrome 通过 fetch http://127.0.0.1:8080/health → 由扩展 first-run 检测
    # 4. ws://127.0.0.1:8080/ws/extension/test 探活 → 由扩展 first-run 检测

    return failures

# 扩展端 first-run（在浏览器内跑）
async function checkExtensionWsHealth() {
  try {
    const res = await fetch('http://127.0.0.1:8080/health')
    if (!res.ok) throw new Error(`status ${res.status}`)
  } catch (e) {
    showCriticalBanner('扩展无法连接 Co-Pilot，请检查 Defender / 公司代理 / 杀毒拦截')
    return false
  }
  return true
}
```

**install wizard 自动配置**：
- ✅ Win Defender 排除项（PowerShell admin 一次性）：
  ```powershell
  Add-MpPreference -ExclusionPath "C:\Users\xxx\AppData\Local\FlowHarvester"
  Add-MpPreference -ExclusionProcess "Co-Pilot.exe"
  ```
- ✅ 提示客户关 chrome DNS over HTTPS：Settings → Privacy → "DNS over HTTPS" → "Off" 或 "With current service provider"
- ✅ 客户公司代理 / 杀毒（卡巴斯基 / 360 / 腾讯电脑管家）白名单步骤写到 `docs/customer-install-windows.md`
- ✅ Hyper-V / WSL 网络隔离场景检测：扩展 fetch 失败时提示常见原因 checklist

**失败兜底**：扩展端 WS reconnect 失败 → 上报 onError code → 中控记录失败原因；重复失败 → 提示客户检查 troubleshooting checklist。

#### 5.3.2 chrome 升级单点故障（C-054 NEW，v0.5 critical）

> **风险评估**：chrome 主版本升级（每 4 周）有时 disable 所有未签名 unpacked 扩展 → 5 工位同时停摆 → 客户产能瞬间归零，**没有备份恢复路径**（无 Web Store fallback）。

**缓解措施**（必须 V2.0 release 前实施）：

1. **chrome 升级监控（Co-Pilot 实施）**：
   - Co-Pilot 每日 fetch `chromiumdash.appspot.com/fetch_releases?channel=Stable` 检查 next-version
   - 下周升级前 7 天告警操作员：dashboard 横幅"chrome 即将升级到 vXXX，建议测试机先验证"
   - chrome stable 升级日 → 中控 paused 派 task（自动 mark active task `paused`），等扩展 health check 后恢复

2. **扩展状态探测（Co-Pilot 一键 reload 助手）**：
   - Co-Pilot 启用 chrome `--remote-debugging-port=9222` 通过 chrome DevTools Protocol 探测扩展状态
   - 检测到 disabled / 不存在 → guided wizard 带操作员每个 profile 重 reload
   - dashboard "工位重连状态" 横幅显示 reload 进度（5 / 5 重连完成）

3. **客户机 chrome 锁版本（如客户接受）**：
   - 跟客户确认能否启用 chrome enterprise policy 锁定 chrome 版本（不 auto-update）
   - 锁在 chrome 117+（C-031 兼容下限）某稳定版本
   - 客户每月跟作者确认"换不换 chrome 版本"

4. **测试机先升级**：
   - V2 部署架构含 1 台**测试机用 chrome beta channel**（比 stable 提前 4 周）
   - 提前看 chrome 大版本影响 → 来不来得及发 V2 patch

5. **替代浏览器探索**（spike 阶段研究，非 V2.0 范围）：
   - **Edge / Brave / Vivaldi 等 chromium-based** 浏览器对 unpacked 是否更宽松（部分实测 Edge 比 chrome 警告少）
   - chrome enterprise policy `ExtensionInstallSources` 白名单 — 客户机一次配置永久有效，但需 GPO/registry 改动
   - 自签名 .crx + chrome `--load-extension=` flag（操作员每次启动 chrome 需要带 flag，install wizard 用 chrome_profile_launcher 自动加）

#### 5.3.3 unpacked 摩擦缓解（v0.4 → v0.5 持续）

- chrome 每次启动显示"You are using extensions that are not from the Web Store" → 操作员习惯（接受常态化）
- 每个 chrome profile 独立装扩展（chrome 是 per-profile）→ 5 profile = 5 次手动 reload
- permissions 一次性放足（C-021）→ 后续 V2.x 升级**只删不加**避免 review dialog
- manifest `key` 字段固定 ext_id（C-050）→ ws_token / Origin 校验跨 reload 稳定
- release cadence 极保守（每月一次，避免 5 profile reload 频繁打扰客户）

#### 5.3.4 决策（v0.5）

**唯一路径**：unpacked + manifest `key` 字段固定 ext_id + chrome 升级监控 + Co-Pilot reload 助手。

**spike 验收 #5（v0.5 改）**：原"Web Store 上架 PoC"取消（C-042 OBSOLETE）；改为"chrome 117 / 124 / 130 三版升级 disable unpacked 行为实测 + Co-Pilot reload 助手能否在 5 分钟内恢复 5 工位"。

### 5.4 多账号管理（C-007 加并发限制 + C-028 账号性质 commit）

> ⚠️ **G2 立项 gate**：客户机 RAM ≥16 GB？8 GB 跑不动 5 profile。  
> ⚠️ **G8 立项 gate（C-028 NEW）**：客户用日常账号还是专门账号？

#### 5.4.1 账号性质决策（C-028，v0.4 commit "专门账号"）

> **核心 commit**：V2 客户**必须为 V2 创建专门 Google 账号**（跟 V1 一致），不要绑客户日常业务账号。

**理由**：
- §2.2 v0.3 暗示用日常账号（"真实使用历史"）— **被 v0.0.3 C-028 证伪**：
  - 专门账号没"真实使用历史"——刚开账号没 Gmail 流量、没 Drive 文件、没浏览历史 → Google 风控视为"陌生空号"，跟 patchright profile 等同
  - 日常账号被 ban → 客户业务停摆（Gmail / Drive / Workspace），不可接受
- **V2 vs V1 anti-bot 优势的核心论据动摇**——v0.4 诚实承认：V2 优势主要在 selector / locale / 调试体验，**不在账号信誉**
- 这跟 V1 现状一致（V1 客户用 Flow 专开账号）

**风险披露**（C-028 强制要求）：
- `docs/customer-manual.md` 开头**警告**："V2 扩展行为可能影响所运行 Google 账号信誉，**不建议用客户业务核心账号**"
- Setup wizard 第一屏强制操作员勾选确认"我已读懂账号风险声明"才能继续
- 客户 onboarding 表单留 audit 记录

#### 5.4.2 strike 系统改造（C-028）

V1 strike 触发 → 直接 disable workstation（账号停跑）。V2 改为：

```
触发 strike → cooldown 24-72h（账号"休息"，不直接 disable）
   ↓
严重 strike（连续 3 次） → 通知操作员手动登录该账号做"人工保活"
   ↓ （操作员在 chrome 里看 Gmail / 用 Drive 5 分钟）
跟 V1 disable 行为对齐：连续 5 strike 后 disable workstation
```

理由：专门账号"创建后立刻跑批" 跟 Google 期望的"真人使用模式"差距越大越被 ban；适度 cooldown + 人工保活拉长账号寿命。

#### 5.4.3 并发 N 限制

- 默认 `max_concurrent_active_workstations: 3`（settings.yaml 可配）
- 8 GB RAM → 推荐 N=2（chrome × 2 profile ~2 GB + Co-Pilot 1 GB + Win11 4 GB = 7 GB，留 1 GB 给客户日常）
- 16 GB RAM → 推荐 N=3
- 32 GB+ → N=5

**轻量模式（备选）**：每次只开 1 个 profile chrome 窗口，跑完关掉切下一个。失去并行优势但内存友好；客户机 8 GB 时强制此模式。

#### 5.4.4 操作员日常

1. 在 chrome 里创建 N 个 V2 **专门 profile**（不是日常 profile，C-029 / C-030）：
   - 每个 profile 用一个**专门 Google 账号**（不是客户业务核心账号）
   - 该 profile **chrome sync 关闭**（C-030）
   - 该 profile **不装 Grammarly / 1Password / AdBlock 等其他扩展**（C-029）
   - chrome translate 该 profile 设为关闭（C-029）
2. 每个 profile 装扩展 + 跑绑定 wizard（§4.5.2）
3. 中控 dashboard "启动调度器" → Co-Pilot 按 N 限制 stagger 启动 profile chrome 窗口（chrome_profile_launcher.py）
4. 跑完一轮，Co-Pilot 自动关掉某 profile chrome → 切下一组

**资源监控**：Co-Pilot 加 `psutil` 检测系统 RAM 余量，<2 GB 时暂停派 task。

**spike 验收 #8（C-007）**：实测客户机 5 profile 并发的 RAM / CPU / 稳定性。

#### 5.4.5 Google 账号 cookie 续期（v0.5 NEW，C-051）

Google session cookie 大约 14-30 天过期（active 使用会续期，闲置不续）。V2 专门账号日常**只跑 V2 任务**，没"真实使用历史" → cookie 不续 → 30 天后扩展跑任务时 Flow page redirect 到登录 → V2 没法解（自动登录 §6.3 排除）。

**处理**：
- 扩展检测 redirect 到 `accounts.google.com/signin` → 上报 `login_required` ErrorType（§17.4）
- 中控 dashboard 显著告警 "WS_X 需要重新登录"
- 工位状态 `manual_check`，等操作员手动登录该账号

**预防（操作员习惯养成）**：
- `customer-manual.md` 加"账号 cookie 续期"建议：**每周登录一次** V2 专门账号到 chrome（手动看 1 分钟 Gmail）让 cookie 续
- Co-Pilot 每月 1 号 dashboard 提醒"建议登录所有 V2 账号续期 cookie"

**长期（V3 探索）**：考虑 Google App Password / refresh token 机制，本 V2.0 不做。

---

## 6. 实施计划

### 6.1 Milestones（C-006 重排为 8-12 周 + MVP gate）

| 阶段 | 内容 | 估时 | 输出 |
|---|---|---|---|
| **-1. 测试基础设施搭建**（v0.6 NEW，C-058） | 3 台测试机（chrome 117 / chrome stable / chrome beta channel）+ 5 个 Veo 测试账号注册（含越南语 / 中文 / 阿非利卡 / 主测）+ CI runner 选型 + Fixture 库 repo 建立 | **1-2 周**（先决条件） | §19 测试基础设施 落地 |
| **0. 客户协作期 + 独立访谈**（v0.6 NEW，C-056 / C-057） | G1 / G2 / G7 / G8 / G10 / G11 客户访谈 + GPO check + NDA / DPA 谈判 + PoC 客户选型 | **1-2 周**（异步，跟 -1 并行） | §18 客户协作框架 落地 + Plan B 客户出口预案 |
| **0.5. Fragility 知识沉淀**（v0.4，C-032 + v0.6 修正） | V1 35 条 fragility 中 **Top-10 priority** reproducer fixture（v0.4 全 35 不现实，v0.6 修正按优先级渐进，其余 25 条在 V2 实施期间补） | **1 周** | `docs/v1-fragility-regression-suite.md` Top-10 fixture + 三态标注 |
| **1. Spike Phase A（独立验证）**（v0.6 改 C-063） | 9 个独立 spike 项 #1 / #2 / #4 / #6 / #8 / #9 / #10 / #11 SQLite 长跑（后台 7 天）/ #16 selector hot update / #17 性能 benchmark / #18 多 window | **5-7 天** wall-clock（SQLite 长跑后台并行） | 立项 gate G3 / G4 / G6 / G9 ✅ |
| **2. Spike Phase B（客户协作）**（v0.6 改 C-063） | 客户机 PoC：spike #7 5 profile RAM + #12 chrome 版本矩阵 + #13 / #14 / #15 客户机实测 + 安全 review | **1 周** | 立项 gate G2 / G7 / G10 / G11 ✅ |
| **3. 协议 v1 设计** | message schema 定稿（C-011 字段全 + C-015 token + C-027 license schema + C-061 cancel_rejected） | 2-3 天 | `docs/v2-protocol.md` v1 |
| **4. MVP** | 单 task / 单 round / video 模式 / Frames + multi-round 不做 / 仅英文 selector | **2 周** | MVP 跑通 1 个 task 端到端 |
| **5. 扩展功能补全** | Frames 模式 / multi-round / 13 语言 fallback / 错误分流 / 状态机持久化 + 剩余 25 条 fragility 移植 | **3-4 周** | 接近 V1 v0.1.0 feature parity + 35/35 regression pass |
| **6. 后端改造** | 删 patchright / 加 extension_dispatcher / 加 §13 安全模型实现（含 license machine binding + tier schema） | 1-2 周 | Co-Pilot 用 V1 dashboard 但底层换扩展 |
| **7. 客户体验** | Setup wizard（GPO + 网络层 + admin/chrome 版本/sync/RAM/power/AV/扩展冲突）/ chrome profile launcher / unified release zip / i18n（zh_CN+en） | 1 周 | V2.0-beta |
| **8. 内测 + 迁移** | 按账号增量切 + §12 迁移策略落地 + §12.6 rollback 演练 + §12.8 备份策略 | **2-3 周** | V2.0 release |

**Spike 总计 4-6 周**（v0.6 修正 C-063：v0.5 的 5-7 天严重低估）  
**实施总计 V2.0 release 12-16 周**（含 spike + 测试基础设施 + Phase 0.5 + 客户协作 + 实施 + 内测）

**MVP gate**（阶段 2 完成）：必须能跑通"单 task / 单 round / video 模式 / 英文 selector"端到端，否则 spike 假设破产，回头评估方案。

### 6.2 风险登记册（C-008 / C-021 修正）

| 风险 | 概率 | 严重度 | 缓解 |
|---|---|---|---|
| Manifest v3 service worker 30s idle hibernate（**修正：30s 不是 5min**） | **高** | **高** | task 状态 chrome.storage 持久化 + reconnect 对账（§4.4）；中控 60-120s grace |
| 客户 chrome 关了 = 任务停 | **高** | **中** | §4.4 task lifecycle 状态机 + interrupted 状态 + 操作员 manual resume |
| **扩展分发单点故障（unpacked-only，v0.5 升级）** | **极高** | **极高** | C-054：unpacked-only = chrome 主版本 update 唯一分发路径；Co-Pilot 每日 chrome stable 升级监控；测试机用 chrome beta 提前 4 周；Co-Pilot reload 助手通过 CDP 探测扩展状态；G10 立项 gate；release cadence 每月一次极保守 |
| 扩展更新摩擦（5 profile × manual reload，C-021 / C-055） | **高** | **高** | manifest permissions 一次放足只删不加；统一 release zip Co-Pilot+extension 同 version；Co-Pilot 主动版本检查 mismatch reject + dashboard 红色告警 |
| **CSP 合规（C-041，v0.5 NEW）** | **高** | **高** | manifest v3 禁 eval；spike #2 验证 4 条 locale 假设的 CSP 兼容性；接受 V2 Locale 优势重定位为 fail-fast 不是 locale-independent |
| **多 tab 管理（C-043，v0.5 NEW）** | **中** | **高** | §4.6 active_flow_tab_id + tab pin/title 锁定 + chrome.tabs.onRemoved 监听 + chrome restore 不立即 resume |
| **GDPR vs cloudflared 矛盾（C-044，v0.5 NEW）** | **中** | **高** | §13.5 cloudflared 默认关 + named tunnel + access policy + 30 分钟自动关闭 + 诊断包 GPG 加密 + DPA 模板 |
| **SQLite WAL 高并发承载（C-046，v0.5 NEW）** | **中** | **高** | §4.4 task_progress batch 5s 合并 + forensic_log 按日 partition + WAL checkpoint(TRUNCATE) 凌晨 3am + busy_timeout 5s |
| **后端契约变更（C-047，v0.5 NEW）** | **高** | **高** | §17 金丝雀 healthcheck 任务每日 + 远程 selector hot update + ErrorType 加 captcha/contract_drift/onboarding_required |
| **扩展自身 update race（C-048，v0.5 NEW）** | **中** | **中** | §4.7 延迟 update 直到 task idle + content script reload broadcast + onSuspend flush + 协议 forward compat |
| **客户 PoC 配合意愿（C-056，v0.6 NEW）** | **高** | **极高** | §18 客户协作框架 + spike 拆 Phase A 独立 / Phase B 客户协作 + V2 价值"新客户首发，老客户自愿升级" + 商业关系（免费 PoC 换早期折扣 / 收费咨询）+ Plan B 客户出口预案 |
| **chrome enterprise GPO 阻塞（C-057，v0.6 NEW）** | **高** | **极高** | §5.3.5 install wizard 早期 GPO 检测 + chrome 命令行 fallback + 客户公司 IT GPO 模板（docs/chrome-policy/v2-allow-unpacked.reg）+ G11 立项 gate |
| **测试基础设施空白（C-058，v0.6 NEW）** | **高** | **高** | §19 测试基础设施 + Phase -1 搭建（1-2 周）+ 测试机预算列入 V2 项目 |
| **客户机 WS 网络拦截（C-060，v0.6 NEW）** | **中** | **高** | §5.3.6 install wizard 网络层检测 + Defender 排除项 PowerShell + DoH 关闭引导 + AV 白名单 SOP |
| **多 chrome window 同 profile（C-059，v0.6 NEW）** | 中 | 中 | §4.6 (window_id, tab_id) 双因子 + single-window 强制策略 + chrome.windows.onCreated 监听 + spike #18 |
| **cancel_task Veo 配额浪费（C-061，v0.6 NEW）** | 中 | 高 | §4.4.7 cancel 阶段约束 + dashboard UX 二次确认 + 残留 mp4 兜底下载 + 配额账户级监控 |
| **扩展性能开销影响日常 chrome（C-062，v0.6 NEW）** | **高** | 中 | §4.2 content script 懒加载（不 manifest auto match）+ MutationObserver 限缩 + chrome.alarms 周期动态 + spike #17 性能 benchmark |
| **Spike 5-7 天严重低估（C-063，v0.6 NEW）** | **高** | 高 | §6.1 重排 4-6 周 spike + Phase -1/0/0.5/1/2 拆分 + 客户访谈 gate 排前面 fast fail |
| **运营成本翻倍（C-064，v0.6 NEW）** | 中 | 中 | §18 成本明细 + V2 license 提价 / 升级 SLA 加价 + V2 单客户 break-even 分析 |
| **数据备份缺失（C-069，v0.6 NEW）** | 中 | 中 | §12.8 SQLite 每日 dump + output/ 30 天 + workstation binding JSON 备份 + 恢复 SOP |
| 多 chrome profile RAM 撑爆 | **中** | **高** | N=2-3 并发限制（§5.4）；客户机 8 GB 强制轻量模式；启动前 RAM 检测 |
| Google 检测扩展层（未来） | 低 | 高 | extension ID 可重新生成；代码混淆；at present Google 没公开 fingerprint 扩展 |
| 操作员误配 ws_id ↔ profile（C-018） | **中** | **高** | 双因子绑定（email + profile_id_hash）+ register reject + dashboard 红色告警 |
| Veo 双扣费（sw hibernate 期间 retry，C-020） | 中 | 高 | create_pending / create_committed 双重持久化 + 中控不主动 retry pending |
| chrome.downloads 文件名冲突（C-019） | 中 | 中 | filename 包含 ws_id + task_id + round + idx + ts；onChanged 拿最终名 |
| WebSocket 端口 / token 安全（C-015） | 中 | 高 | FastAPI 强制 127.0.0.1 bind / Origin 校验 / DNS rebinding 中间件 / ws_token 校验 |
| 离线 / 网络不稳 | 中 | 低 | 扩展端 retry + 中控端 task queue |
| chrome 版本兼容（C-025 / C-031） | **高** | **高** | 详见 §15 兼容矩阵；G9 客户机审计 ≥117；spike 在 117/124/130 三版跑全验收；扩展 startup graceful degradation；Co-Pilot 周期监控 chrome stable beta 提前告警 |
| 扩展冲突（C-029） | **高** | **高** | management permission 启动枚举 + KNOWN_CONFLICT_IDS 黑名单；content script 防御性写法（setReactInputValue / DataTransfer / world:MAIN）；客户用 V2 专用 profile |
| chrome sync 跨设备扩散（C-030） | 中 | **高** | 代码层禁 storage.sync + lint；wizard 强制关 sync；machine binding 二次拦截；customer-manual 强警告 |
| license 拷贝绕过（C-027） | **高** | **高** | machine_id_hash 绑定 + chrome.storage.local signature 1h 过期 + customer_id 不可转移条款；V2.1+ 在线 revoke |
| Co-Pilot 进程权限（C-033） | 中 | 高 | 用户进程 + 开机自启（注册表 HKCU）；install wizard 一次性检测；chrome 启动 stagger 5s |
| reconnect storm + 时间不同步（C-034） | 中 | 高 | reconnect jitter + register stagger + 时间偏差 reject + monotonic clock |
| V1 fragility 回归（C-032） | **高** | 高 | spike Phase 0.5 沉淀 35 条 fragility regression suite；CI 跑 35/35 pass 才能 release |
| Win11 power / AV / disk（C-035 / C-036 / C-037） | 中 | 中 | install wizard 配置高性能电源 + AV 白名单 + 30 天 forensic_log rotation |

### 6.3 不在 V2 范围（V3+）

- 远程中控（云端 / 多机器 fleet）
- 多操作员 / 多租户
- 跨平台扩展（Sora / Runway / Pika）
- 商业化分层（Free / Pro / Enterprise）
- 自动化登录 + 凭证管理（Google 反 bot 太严，不做）

---

## 7. 验收标准（C-004 重写为可执行定义）

### 7.1 V1 v0.1.0 baseline 测试（spike 阶段就建）

**固定测试集**：
- 任务集 A：10 个任务 × 8 video × `output_count=2` × `frames_pair` 模式（半 Frames + 半 Ingredients）
- 任务集 B：10 个任务 × 4 video × video 模式 × `inter_round_pause_sec=5`
- 账号：3 个 healthy WS（A / B / C）
- 网络：客户机 + 客户 VPN
- chrome 版本：固定（spike 阶段记录）

**baseline 指标**（V1 v0.1.0 跑 3 次取平均）：
- task 成功率 = 成功任务数 / 总任务数
- video 产出率 = 成功落盘 mp4 数 / 预期 mp4 数（target_count × 任务数）
- 端到端时长（avg / p95）
- 失败原因分布（按 error_type）
- 操作员介入次数（manual_review + 重启 chrome 等）

**输出**：`docs/v2-baseline.md` 含 baseline 数据 + 控制变量记录。

### 7.2 V2.0 release 验收

V2.0 必须**用同一测试集**跑：

| # | 验收项 | 通过标准 | 决策含义 |
|---|---|---|---|
| 1 | 装机迁移可达性 | V1 客户按 §12 迁移指南 1 周内完成 V2 切换（含装扩展 + 配账号 + setup wizard） | 客户能力上限验证 |
| 2 | 单 task 端到端 | MVP（§6.1 阶段 2）跑通：上传 → prompt → Create → 生成 → 下载 → DB → output 落盘 | 基础能力 |
| 3 | Feature parity（软化，C-006） | V2.0 至少覆盖 Ingredients 模式 + 单 round；Frames + multi-round 可放 V2.1 | 接受比 V1 功能少 |
| 4 | 错误分流 | 13 个 ErrorType（§4.3 protocol）每个都能正确驱动工位状态机（cooldown / nurturing / manual_check） | 跟 V1 状态机对齐 |
| 5 | Locale 测试 | 用越南语 / 中文 / 阿非利卡账号跑任务集 A 第 5 行（特意选有挑战的）— 通过率 ≥V1 baseline | C-002 假设验证 |
| 6 | 任务恢复 | sw hibernate 强制触发 5 次（spike #1）+ chrome 关闭中断 5 次 — task 恢复成功率 ≥80% | C-001 / C-009 |
| 7 | 安全 review | §13 威胁模型表所有 trust boundary 实施了缓解措施；spike 安全 review 出报告 | C-015 |
| 8 | 稳定性 | 跑任务集 A+B 连续 1 周，**对比 V1 baseline**：<br>- task 成功率 ≥ V1 + 5%<br>- video 产出率 ≥ V1 baseline<br>- 失败原因分布中 selector_drift / locale_drift 占比 ≤ V1 - 50%<br>- 操作员介入次数 ≤ V1 baseline | 跟 V1 同基准对比，不是空想"≥85%" |
| 9 | 可观测性 | cloudflared 隧道仍可用 + 扩展端日志可推送中控 + 中控可主动 screenshot_request | C-017 |
| **10**（v0.4 NEW） | V1 fragility 回归 | 35 条 fragility 全部 verified（35/35 pass in V2 regression suite，CI 集成） | C-032 |
| **11**（v0.4 NEW） | License 机器绑定 + tier 限制 | 装 V2 + license.lic → 在另一台机器 license 拒；超 tier max_workstations 调度暂停；超 max_daily_tasks 配额超 | C-027 |
| **12**（v0.4 NEW） | 扩展冲突 + chrome sync | 装 Grammarly + 1Password 跑端到端不被干扰；开 chrome sync → wizard 拒绝注册 | C-029 / C-030 |

**注意**：第 8 项的"跟 V1 baseline 对比"代替 v0.1 那个"60 个任务 ≥85%"虚浮指标。

### 7.3 spike 阶段验收（前置条件）

| spike # | 验收项 | 来源 | 责任人 | due | pass criteria |
|---|---|---|---|---|---|
| 1 | sw hibernate 状态恢复 | C-001 | dev（具体人，spike 启动时定） | spike day 3 | 跑 10min 假任务，强制 stop sw 5 次后任务恢复 ≥4/5 |
| 2 | locale-independent 路径 | C-002 | 同上 | spike day 5 | 越南语账号下 50 行 content script 找到 upload / generate 锚点（任一） |
| 3 | V1 baseline 建立 | C-004 | 同上 | spike day 7 | docs/v2-baseline.md 提交 |
| 4 | 200MB 文件传输 | C-005 | 同上 | spike day 4 | 方案 A `chrome.downloads.download` filename 子目录写入成功；5 profile 并发不撞名 |
| 5 | Chrome Web Store 上架 PoC | C-008 | 同上 | spike day 10（异步） | 测试版扩展上架审核通过 |
| 6 | Veo 后端 idempotency | C-020 | 同上 | spike day 3 | 连续 click Create 同 prompt — Veo 是否生成 1 份还是 2 份 |
| 7 | 客户机 5 profile RAM | C-007 | 客户 PoC（远程） | spike day 7 | 5 profile + Co-Pilot 运行时 RAM 余量 ≥2 GB |
| 8 | 误配 ws_id 检测 | C-018 | dev | spike day 6 | A profile 输 WS_B token，中控在 register 时 reject |
| **9**（v0.4 NEW） | 扩展冲突场景 | C-029 | dev | spike day 4 | 装 Grammarly + 1Password 跑 prompt 输入端到端，内容不被改 |
| **10**（v0.4 NEW） | chrome sync 防御 | C-030 | dev | spike day 4 | 开 chrome sync 装 V2 → 个人设备同步过去时 wizard reject |
| **11**（v0.4 NEW） | reconnect storm | C-034 | dev | spike day 5 | Co-Pilot 重启 → 5 ws 同时 register，验证 jitter + register stagger 不撞 Google 风控 |
| **12**（v0.4 NEW） | chrome 版本矩阵 | C-031 | dev | spike day 7 | spike 在 chrome 117 / 124 / 130 三版分别跑 #1-#11 全验收 |
| **13**（v0.5 NEW） | 多 tab 关闭 / restore | C-043 | dev | Phase 1 day 4 | 5 Flow tab 操作员关跑中 tab → 上报 interrupted + dashboard 告警可 resume |
| **14**（v0.5 NEW） | 扩展 update race | C-048 | dev | Phase 1 day 5 | 任务跑中触发扩展 reload → onSuspend flush + 新 sw mark task interrupted + 中控不主动 retry |
| **15**（v0.5 NEW） | SQLite 1 周长跑 | C-046 | dev | Phase 1 后台并行 7 天 | p95 写延迟 < 50ms / WAL < 200 MB / locked 错误率 < 0.01% |
| **16**（v0.5 NEW） | selector hot update | C-047 | dev | Phase 1 day 6 | 模拟 selector 失败 → 中控推 selector_config_update → 客户机扩展 hot update 成功 |
| **17**（v0.6 NEW） | 扩展性能 benchmark | C-062 | dev | Phase 1 day 7 | 装 V2 + chrome 10 个客户日常 tab → CPU 增量 ≤ 5% / RAM 增量 ≤ 200 MB/工位 / Flow page 帧率 ≥ 30fps |
| **18**（v0.6 NEW） | 多 chrome window | C-059 | dev | Phase 1 day 4 | 同 profile 开 2 chrome window 都加载 labs.google → sw 按 (window_id, tab_id) 区分 + 派 task 给正确 window |

### 7.4 立项 gate（必须 OK 才能进 V2 spike）

| Gate | 来源 | 决策路径 | 责任人 | 状态 | Plan B（gate fail） |
|---|---|---|---|---|---|
| G1 客户接受 chrome 必须开 | C-003 | 客户访谈 | 项目经理 | ⏳ pending | 走 §3.1 Plan B 独立 chrome（退化为 patchright 风险） |
| G2 客户机 RAM ≥16 GB | C-007 | 客户访谈 | 项目经理 | ⏳ pending | N=2 轻量模式或换机器 |
| G3 sw hibernate 验证 | C-001 | spike #1 | dev | ⏳ pending | V2 立项暂停 |
| G4 locale 路径验证 | C-002 | spike #2 | dev | ⏳ pending | 接受 V2 仍要做 locale 层 |
| G5 V1 baseline | C-004 | spike #3 | dev | ⏳ pending | spike 结束前必须有 |
| G6 200MB 文件传输 | C-005 | spike #4 | dev | ⏳ pending | 走方案 B 或 C |
| G7 扩展分发路径 | C-008 | spike #5 + 决策会 | 项目经理 + dev | ⏳ pending | unpacked + Co-Pilot 升级助手（可行但摩擦大） |
| **G8 账号性质**（v0.4 NEW） | C-028 | 客户访谈 + setup wizard 强制确认 | 项目经理 | ⏳ pending | 客户拒绝接受"专门账号" → V2 anti-bot 优势论据彻底破，重新评估 |
| **G9 chrome 版本审计**（v0.4 NEW） | C-031 | install wizard 自动检测 + 客户审计 | 项目经理 | ⏳ pending | <117 → 客户先升级 chrome 才装 V2；客户 GPO 锁版本 → V2 部署延后 |
| **G10 chrome 升级管控**（v0.5 NEW） | C-054 | 客户访谈 + chrome enterprise policy 评估 | 项目经理 | ⏳ pending | 客户不接受 chrome 锁版本且不接受 update 期间停摆 → V2 立项暂停或评估替代浏览器（Edge / Brave） |
| **G7 unpacked 摩擦**（v0.5 改） | C-008 / C-054 / C-055 | 客户访谈 + 测试机 PoC | 项目经理 | ⏳ pending | 客户拒绝接受 5 profile reload + chrome 警告 banner 长期存在 → 评估 Edge / Brave 或 chrome enterprise policy 白名单 |
| **G11 客户机可装 unpacked 扩展**（v0.6 NEW，C-057） | C-057 | install wizard GPO 检测 + 客户 IT 协作 | 项目经理 + 客户 IT | ⏳ pending | GPO 全禁 → V2 在该客户机不可部署，project manager 跟客户沟通；客户 IT 同意改 GPO → 提供 .reg 模板部署后通过 |

---

## 8. 决策记录（v0.6 修正）

| 决策 | 选择 | 理由 |
|---|---|---|
| Stealth library | 扔掉 patchright | Google 不在其测试列表；架构层根因 |
| 浏览器自动化范式 | Chrome 扩展 | 真实账号信誉 + 不被 anti-bot 命中 |
| 中控部署位置 | Local（V2.0）→ Remote（V3.0） | 复用 ~85% 现有 backend；客户场景单机 |
| **Co-Pilot 进程模型**（v0.4 改，C-033） | **用户进程 + 开机自启**（HKCU 注册表），不是 Windows service | service 启不了 chrome；用户日常不 logout；不需要 admin |
| **扩展所在 chrome profile**（v0.4 改，C-028 / C-029 / C-030） | **客户专用 V2 profile**（不开 sync / 不装其他扩展 / chrome translate 关），不是日常 profile | 账号信誉论据本不稳；扩展冲突 + sync 泄露不可控 |
| **账号性质**（v0.4 改，C-028） | **专门 Google 账号**（跟 V1 一致），不绑客户业务核心账号 | 风控 ban 影响半径必须可控；客户必须 G8 gate 确认 |
| 扩展技术栈 | TypeScript + Vite + @crxjs/vite-plugin | 现代 + HMR + manifest v3 原生支持 |
| **扩展分发**（v0.5 大反转，C-042 OBSOLETE / C-054） | **唯一路径 unpacked**（manifest `key` 字段固定 ext_id；不上 Chrome Web Store） | 用户决策保留分发可控；避开 Web Store 自动化 Google 服务下架风险（C-042 OBSOLETE 后约束）；代价是 chrome 升级单点故障，靠监控 + reload 助手缓解 |
| **Locale 优势重定位**（v0.5 改，C-041） | **fail-fast + 精准告警 + DevTools 调试**，不是 locale-independent | manifest v3 + labs.google CSP 双重限制下 locale-independent 几乎不可能；接受 V2 跟 V1 同样 13 语言 selector 列表，但失败模式从 silent → fail-fast |
| 通信协议 | JSON over WebSocket + ws_token auth + Origin 校验 | C-015 加固 |
| **文件传输**（v0.3 改） | **首选方案 A**（chrome.downloads filename 子目录），方案 C 不再首选 | C-005 修正：WebSocket 二进制 200MB 风险大 |
| 自动登录 | 不做 | Google 反 bot 在登录页极严，ROI 低 |
| **可观测性**（v0.3 改） | **保留 cloudflared 隧道** + 扩展日志推送 + 远程截图 | C-017 修正：取消 cloudflared 是错的 |
| **任务恢复**（v0.3 新 + v0.4 加 storm 防御） | task lifecycle 状态机 + chrome.storage 持久化 + reconnect 对账 + reconnect jitter + register stagger + 时间同步 reject | §4.4 + C-001 / C-009 / C-020 / C-034 |
| **WS↔profile 绑定**（v0.3 新） | 双因子（email + profile_id_hash）+ register reject + dashboard 告警 | §4.5 + C-018 |
| **strike 累计**（v0.3 改） | 按 account_email 而非 ws_id | C-018 防误配杀错号 |
| **strike 触发处置**（v0.4 改，C-028） | cooldown 24-72h（账号"休息"） + 严重 strike 通知操作员人工保活，不直接 disable | 拉长专门账号寿命 |
| **License 模型**（v0.4 大改，C-027） | machine_id 绑定 + tier schema（trial/standard/pro/enterprise）+ chrome.storage signature 1h 过期 + customer_id 不可转移 + V2.1+ 在线 revoke | 拷贝防御 / revoke 能力 / 商业化预埋 |
| **chrome 兼容下限**（v0.4 commit，C-031） | chrome 117+，spike 在 117/124/130 三版验证 | C-001 缓解依赖 chrome.alarms 30s 周期 |
| **chrome.storage.sync**（v0.4 改，C-030） | **代码层禁用** + CI lint + Setup wizard 强制关 sync | 防 license / business data / token 泄露到 Google |
| **V2 schema 多租户预埋**（v0.4 新，C-040） | customer_id / tenant_id NULLABLE 字段（默认 'default'） | V3 演进低成本，V2.0 行为不变 |
| **i18n**（v0.4 新，C-039） | chrome.i18n API + zh_CN 默认 + en fallback | 跟 V1 中文 dashboard 体验对齐 |
| **多 tab 管理**（v0.5 新，C-043） | active_flow_tab_id + tab pinned + onRemoved 监听 + chrome restore 不立即 resume | V1 单 tab 无歧义；V2 客户日常 chrome 多 tab 场景 |
| **扩展 update lifecycle**（v0.5 新，C-048） | 升级前 idle check + onSuspend flush + protocol forward compat | unpacked 升级 = 操作员手动 reload，跑中任务必须 graceful interrupted |
| **GDPR vs cloudflared**（v0.5 改，C-044） | **cloudflared 默认关** + named tunnel + 30 分钟自动关闭 + 诊断包 GPG 加密 + DPA 模板 | v0.4 括号补丁矛盾 → v0.5 commit 数据流明确表 |
| **V2.x 内部 rollback**（v0.5 新，C-045） | 三种 rollback 类型 + 旧版本保留 + schema 只加 NULLABLE + protocol forward compat + spike rollback 演练 | unpacked-only 失去 chrome auto-update，rollback 必须明确 |
| **统一 release 管理**（v0.5 新，C-055） | unified release zip Co-Pilot+extension 同 version + 主动版本检查 + ws_token 双持久化 | unpacked-only 必须保证 5 profile 版本一致性 |
| **后端契约监控**（v0.5 新，C-047） | 金丝雀 healthcheck 每日 + 远程 selector hot update + ErrorType 加 captcha/contract_drift/onboarding_required/login_required | V1 6 轮迭代每轮源于 Google contract 变 → V2 必须有早期信号 + 缩短 fix 时间窗 |
| **SQLite WAL 硬化**（v0.5 新，C-046） | task_progress batch + forensic_log 按日 partition + WAL checkpoint + busy_timeout 5s | V2 多扩展并发写压力 1-2 个数量级，不硬化会撞 locked 错误 |
| **客户协作框架**（v0.6 新，C-056） | §18 全新（PoC 客户选型 + 投入承诺 + 商业关系三选一 + NDA/DPA + 出口预案） | v0.5 致命缺失：11 gate 中 5 个需客户配合但客户配合机制空白 |
| **chrome enterprise GPO 检测**（v0.6 新，C-057） | §5.3.5 install wizard 早期检测 + G11 立项 gate + chrome 命令行 fallback + GPO 模板 | unpacked-only 后 GPO 阻塞 = dead-end，不早期检测客户机一半部不了 |
| **WS 网络层防御**（v0.6 新，C-060） | §5.3.6 install wizard 网络层检测 + Defender 排除 + DoH 关闭 + AV 白名单 | 客户机 Defender / 公司代理 / 杀毒拦 localhost ws 是 silent failure |
| **多 chrome window**（v0.6 改 C-059） | §4.6 (window_id, tab_id) 双因子 + single-window 强制策略 + chrome.windows 监听 | v0.5 单 active_flow_tab_id 不能区分多 window |
| **扩展性能硬化**（v0.6 新，C-062） | §4.2 content script 懒加载（不 manifest auto match）+ MutationObserver 限缩 + alarms 周期动态 | 持续 inject 影响客户日常 chrome UX，操作员归咎 V2 |
| **cancel_task 阶段约束**（v0.6 新，C-061） | §4.4.7 阶段约束矩阵 + dashboard UX 二次确认 + 残留 mp4 兜底下载 + 配额账户级监控 | Veo 后端不可取消，silent 浪费配额 |
| **测试基础设施**（v0.6 新，C-058） | §19 全新（3 台测试机 + 5 测试账号 + GitHub Actions self-hosted + Fixture repo + Phase -1 1-2 周） | v0.5 假设有测试基础设施但完全空白 |
| **数据备份恢复**（v0.6 新，C-069） | §12.8 全新（SQLite 每日 dump + workstation JSON + license bak + 客户机硬盘故障 SOP） | V1 V2 都没显式备份，客户机硬盘故障 = 数据全丢 |
| **i18n 测试矩阵**（v0.6 新，C-065） | §19.3 5 语种 × 输入 / 显示 / 上传 测试矩阵 | 客户产品多国卖，单越南语 spike 不够 |
| **mp4 codec 披露**（v0.6 新，C-066） | §4.3 spike #4 同时记录 codec + customer-manual 加兼容性段 + ffmpeg 转码模板 | 客户 NLE 不一定支持 h265 / VP9 |
| **扩展 self-check**（v0.6 新，C-067） | §4.2 first-run + 周期性检测（ws_token / setup / 下载目录 / chrome power） + 中控告警 | 操作员误操作 chrome 设置 silent 破坏 V2 |
| **Spike 时间表重排**（v0.6 改 C-063） | §6.1 4-6 周（Phase -1 / 0 / 0.5 / 1 / 2 拆分异步并行）；不再 5-7 天 | v0.5 严重低估 |
| **运营成本 + 商业模型**（v0.6 新，C-064） | §18.7 V2 单客户支持 5-10 工时/月 + license 必须翻倍 V1 价格 + 单客户上限 | V2 支持成本翻倍但 license 没翻 → 商业模型不成立 |

---

## 9. 待澄清问题（已升格到 §7.4 立项 gate）

待澄清问题已升格为可执行的 §7.4 G1-G7 gate。本节保留作向后引用：

- ~~客户接受 chrome 必须开~~ → §7.4 G1
- ~~客户机 RAM~~ → §7.4 G2 + spike #7
- chrome 版本 → C-025 / spike 阶段定 chrome 117+ 下限
- V3 商业化预埋 → §6.3（明确不在 V2 范围）

---

## 10. 附录

### A. 参考开源项目

- [trgkyle/veo-automation-user-guide](https://github.com/trgkyle/veo-automation-user-guide) — chrome 扩展形态批量 Veo 自动化，可读源码学习
- Chrome Web Store 第三方现成扩展：VEO Automation / Auto Flow Pro / FlowForge Pro（参考 C-008，部分通过审核）

### B. 替代方案考量记录

详见 [`memory/project_v2_architecture.md`](../.claude/projects/-Users-shenpeng-Git-Flow-picker-tool/memory/project_v2_architecture.md) 的 "为什么不用替代方案" 段。

### C. V1 → V2 迁移清单

详见 §12（v0.3 inline 化），release 前 1 周再补 docs/v2-migration-guide.md 落地手册。

---

## 11. Change log（v0.6 持续 inline 索引，C-014 修正）

> v0.2 把响应集中放 §11 不改正文是错的（C-014）。v0.3+ 所有响应已 inline 到对应正文章节；本节仅记 change log 索引。

### 11.-2 v0.0.5 → v0.6 集成索引（v0.6 NEW）

| Challenge | 严重度 | 状态 | inline 位置 |
|---|---|---|---|
| C-056 PoC 客户参与机制 | Blocker | ✅ inlined | §18 全新（客户选型 + 投入承诺 + 商业关系 + NDA/DPA + 出口预案）+ §6.1 Phase 0 客户协作期 |
| C-057 chrome enterprise GPO 阻塞 | Blocker | ✅ inlined | §5.3.5 全新（install wizard GPO 检测 + chrome 命令行 fallback + GPO .reg 模板）+ §7.4 G11 立项 gate |
| C-058 测试基础设施零 | Major | ✅ inlined | §19 全新（3 台测试机 + 5 测试账号 + GitHub Actions self-hosted + Fixture repo）+ §6.1 Phase -1 1-2 周 |
| C-059 多 chrome window 同 profile | Major | ✅ inlined | §4.6 升级到 (window_id, tab_id) + single-window 强制策略 + chrome.windows.onCreated 监听 + spike #18 |
| C-060 客户机 WS 网络稳定性 | Major | ✅ inlined | §5.3.6 全新（install wizard 网络层检测 + Defender 排除 + DoH 关闭 + AV 白名单）+ install.bat 自动配置 |
| C-061 cancel_task Veo 不可取消 | Major | ✅ inlined | §4.4.7 全新（阶段约束矩阵 + dashboard UX 二次确认 + 残留 mp4 兜底下载 + 配额账户级监控）+ §4.3 cancel_rejected message type |
| C-062 扩展性能开销 | Major | ✅ inlined | §4.2 content script 懒加载 + MutationObserver 限缩 + chrome.alarms 周期动态 + spike #17 性能 benchmark |
| C-063 spike 5-7 天不现实 | Major | ✅ inlined | §6.1 milestone 重排 4-6 周 spike（Phase -1 / 0 / 0.5 / 1 / 2 拆分异步并行）+ 总实施 12-16 周 |
| C-064 V2 运营成本量化 | Minor | ✅ inlined | §18.7 全新（成本明细 + V2 单客户支持工时 + license 必须翻倍 V1 价格 + 单客户上限） |
| C-065 i18n 矩阵深挖 | Minor | ✅ inlined | §19.3 i18n 测试矩阵（5 语种 × 输入 / 显示 / 上传） |
| C-066 mp4 codec 兼容 | Minor | ✅ inlined | §4.3 mp4 codec 实测 + 客户披露 + spike #4 记录 codec + customer-manual 加 ffmpeg 转码模板 |
| C-067 操作员误操作 self-check | Minor | ✅ inlined | §4.2 扩展 self-check（first-run + 周期性 60min 检测 ws_token / setup / 下载目录 / chrome power）+ 中控告警 |
| C-068 unified release packaging | Minor | ✅ inlined | §12.7.1 完整 release zip 结构定义 + install.bat / update.bat / chrome-policy/ / docs/ + GitHub Actions 自动打包 |
| C-069 数据备份恢复 | Minor | ✅ inlined | §12.8 全新（SQLite 每日 dump + workstation JSON + license bak + 客户机硬盘故障恢复 SOP + dashboard 备份状态卡片） |

### 11.-1 v0.0.4 → v0.5 集成索引（v0.5 NEW）

| Challenge | 严重度 | 状态 | inline 位置 |
|---|---|---|---|
| C-041 CSP 合规 + Locale 假设证伪 | Blocker | ✅ inlined | §4.2 Locale 处理路径重写为 CSP 兼容性矩阵 + spike #2 验收升级 4 子项 + §2.2 表 Locale 行重定位 + §8 决策"Locale 优势重定位" |
| C-042 Chrome Web Store 下架风险 | Blocker | ⏹️ **OBSOLETE**（v0.0.4 修订：用户约束不上 Web Store） | §5.3 commit unpacked-only + §8 扩展分发反转决策记录 |
| C-043 多 tab 管理 | Major | ✅ inlined | §4.6 全新（active_flow_tab_id + tab pin/title 锁定 + chrome restore 不立即 resume）+ spike #13 |
| C-044 GDPR vs cloudflared 矛盾 | Major | ✅ inlined | §13.5 全部重写（数据流表 + cloudflared 默认关 + named tunnel + 30 分钟自动关闭 + 诊断包 GPG 加密 + DPA 模板） |
| C-045 V2.x 内部 rollback SOP | Major | ✅ inlined | §12.6 全新（三种类型 + 旧版本保留 + schema 兼容铁律 + manifest 严格管理 + protocol forward compat + rollback 演练） |
| C-046 SQLite WAL 并发承载 | Major | ✅ inlined | §4.4.2 SQLite 高并发硬化（batch / partition / checkpoint / busy_timeout）+ spike #15 |
| C-047 后端契约变更监控 | Major | ✅ inlined | §17 全新（金丝雀 + 多客户机汇总 + 远程 selector hot update + ErrorType 4 个新类型 + hot fix flow）+ spike #16 |
| C-048 扩展自身 update race | Major | ✅ inlined | §4.7 全新（升级前 idle check + onSuspend flush + protocol forward compat + unpacked 升级 wizard）+ spike #14 |
| C-049 NTFS filename | Minor | ✅ inlined | §4.3 文件传输 NTFS sanitize（NFC normalize + 禁字符替换 + 路径长度 < 220）+ spike #4 验收升级 |
| C-050 ext_id 跨渠道 | Minor | ✅ inlined（v0.0.4 修订简化为 unpacked-only） | §4.2 manifest `key` 字段固定 unpacked ext_id |
| C-051 Cookie 30 天过期 | Minor | ✅ inlined | §5.4.5 全新 + §17.4 ErrorType 加 `login_required` + customer-manual 加每周登录建议 |
| C-052 IME composition | Minor | ✅ inlined | §4.2 setReactInputValue 加 compositionend dispatch + 中文 prompt 测试矩阵 |
| C-053 Log 过载 | Minor | ✅ inlined | §13.7 全新（dedup + level filter + alert 阈值 + cloudflared 远程访问体验） |
| C-054 unpacked-only chrome 升级单点 | Blocker | ✅ inlined | §5.3.2 chrome 升级监控 + Co-Pilot reload 助手 + 测试机 chrome beta + G10 立项 gate + §6.2 风险登记册升 极高/极高 |
| C-055 unpacked-only 版本一致性 | Major | ✅ inlined | §12.7 全新（unified release zip + 主动版本检查 + ws_token 双持久化 + 升级前 idle check） |

### 11.0 v0.0.3 → v0.4 集成索引（v0.4 NEW）

| Challenge | 严重度 | 状态 | inline 位置 |
|---|---|---|---|
| C-027 license 模型漏洞（machine binding / tier / revoke） | Blocker | ✅ inlined | §13.4 大重写 + §13.6 NEW license schema + §6.2 风险登记册 |
| C-028 账号信誉 paradox（日常 vs 专门） | Blocker | ✅ inlined | §2.2 表 + §5.4.1 commit 专门账号 + §5.4.2 strike 改造 + §7.4 G8 |
| C-029 客户日常 chrome 扩展冲突 | Major | ✅ inlined | §4.2 management permission + KNOWN_CONFLICT_IDS + content script 防御性写法 + §3.1 V2 专用 profile |
| C-030 chrome sync 跨设备扩散 | Major | ✅ inlined | §4.2 storage.sync 禁用 + §13.2.6 第 6 个威胁场景 + §13.4 license signature 1h 过期 + §3.1 wizard 检测 |
| C-031 chrome 版本兼容矩阵 | Major | ✅ inlined | §15 NEW 完整矩阵 + §7.4 G9 + §6.2 风险登记册升级到 高/高 |
| C-032 V1 35 fragility 回归测试集 | Major | ✅ inlined | §16 NEW + §6.1 Phase 0.5 + §7.2 第 10 项 + `docs/v1-fragility-regression-suite.md` |
| C-033 §3.1 service vs 用户进程两可 | Major | ✅ inlined | §3.1 commit "用户进程 + 开机自启" + install wizard 一次性检测 |
| C-034 reconnect storm + 时间不同步 | Major | ✅ inlined | §4.4.3 表新增 2 行 + §4.4.5 NEW reconnect storm 防御 + §4.4.6 NEW 时间同步 + §6.2 风险登记册 |
| C-035 Win11 power management | Minor | ✅ inlined | §3.1 install wizard "Win11 power plan 高性能" |
| C-036 Windows Defender / AV | Minor | ✅ inlined | §13.5.2 NEW + customer-install 文档 + code signing cert |
| C-037 forensic_log 磁盘累积 | Minor | ✅ inlined | §13.5.1 NEW rotation policy |
| C-038 chrome incognito 默认不跑 | Minor | ✅ inlined | §4.2 manifest `incognito: not_allowed` |
| C-039 dashboard 中文 vs popup 英文 i18n | Minor | ✅ inlined | §14 NEW i18n + §4.2 manifest `default_locale` |
| C-040 V3 multi-tenant schema 预埋 | Minor | ✅ inlined | §12.1.1 NEW customer_id / tenant_id NULLABLE |

### 11.1 v0.0.1 → v0.0.2 集成索引

| Challenge | 状态 | inline 位置 |
|---|---|---|
| C-001 sw hibernate | ✅ inlined | §4.2 内部分工 + §4.4 |
| C-002 不依赖文本 = 假设 | ✅ inlined | §4.2 Locale 处理路径 + §2.2 |
| C-003 chrome 必须开 = go/no-go | ✅ inlined | §3.1 + §7.4 G1 |
| C-004 验收 ≥85% 不可执行 | ✅ inlined | §7.1-7.3 重写 |
| C-005 WebSocket 二进制 | ✅ inlined | §4.3 文件传输 选 A |
| C-006 4-6 周不现实 | ✅ inlined | §6.1 改 8-12 周 + MVP gate |
| C-007 5 profile RAM | ✅ inlined | §5.4 N=2-3 + §7.4 G2 |
| C-008 unpacked 摩擦 | ✅ inlined | §5.3 重新评估 + §6.2 风险高/高 |
| C-009 任务恢复 | ✅ inlined | §4.4 全新章节 |
| C-010 代码量误导 | ✅ inlined | §4.1 修正 |
| C-011 schema 漏字段 | ✅ inlined | §4.3 protocol v1 |
| C-012 localhost host_permissions | ✅ inlined | §4.2 manifest |
| C-013 trade-off 量化 | ✅ inlined | §3.1 代价段 |
| **C-014 v0.2 §11 自相矛盾**（meta） | ✅ 修正 | 本节即 §11 重写 |
| C-015 安全模型空白 | ✅ inlined | §4.2 manifest 最小权限 + §13 全新章节 |
| C-016 V1→V2 迁移路径 | ✅ inlined | §12 全新章节 |
| C-017 失去 cloudflared | ✅ inlined | §2.2 + §4.1 保留 + §13 |
| C-018 WS↔profile 绑定 | ✅ inlined | §4.5 全新章节 |
| C-019 文件名冲突 | ✅ inlined | §4.3 文件传输 |
| C-020 Veo 双扣费 | ✅ inlined | §4.4 4.4.4 |
| C-021 manifest 升级摩擦 | ✅ inlined | §4.2 一次放足 + §5.3 |
| C-022 spike 验收项缺 owner/due/criteria | ✅ inlined | §7.3 表格补全 |
| C-023 G1-G7 缺决策路径 | ✅ inlined | §7.4 表格补全 |
| C-024 chrome.storage 5MB | ✅ inlined | §4.2 manifest unlimitedStorage |
| C-025 chrome 版本兼容矩阵 | ⚠️ partial | §6.2 风险登记册行 + 待 spike 完善 |
| C-026 扩展 e2e 测试栈 | ⚠️ partial | 等 v0.0.3 challenges 后补完整测试章节 |

### 11.2 v0.6 仍未完成 / 待 v0.0.6 challenges 推进

- **Co-Pilot self-update**：Co-Pilot.exe 自身怎么升级？V1 是手动 .exe 替换，V2 加扩展共升级；自动 self-update 流程 → 待 v0.0.6
- **扩展跟 Co-Pilot 协议向后兼容压力测试**：V2.5 扩展能不能跟 V2.0 Co-Pilot 跑？双向兼容矩阵 → 待 v0.0.6
- **多客户机 fleet 监控（V3 预演）**：V2 是单机但作者要支持 N 个客户机，集中监控的最简模式 → 待 v0.0.6
- **客户报障 SLA**：V2 出问题客户什么时候得到响应，作者响应 SLA 怎么定 → 待 v0.0.6
- **扩展开发流程 + DevX**：开发期 hot reload / 调试 / 分支管理；不是测试基础设施而是日常 dev 流程 → 待 v0.0.6
- **扩展用 React 还是 Vanilla TS**：popup / side_panel / options UI 框架选型（v0.4 §4.2 没明说） → 待 v0.0.6
- **V2 release semver + breaking change policy**：V2.0 → V2.1 → V3 演进规则 → 待 v0.0.6
- **legal / TOS 风险**：V2 自动化 Veo 是否违反 Google Workspace TOS / Veo 服务条款；客户合规暴露 → 待 v0.0.6
- **task 失败重试策略细化**：V1 strike 系统 + V2 strike accounts，重试策略具体 schedule（cooldown 时长 / max attempts / backoff） → 待 v0.0.6
- **dashboard 移动端响应式**：cloudflared 远程访问 dashboard 时操作员可能用手机看 → 移动端体验 → 待 v0.0.6

---

## 12. V1 → V2 迁移策略（NEW，C-016）

V2.0 不能"一刀切"上线。V1 跑了 6 轮迭代客户已经熟练，V2 切换需要双跑期 + 回退路径。

### 12.1 物理冲突盘点（C-016）

| 冲突 | V1 现状 | V2 需求 | 缓解 |
|---|---|---|---|
| chrome profile 互斥 | patchright 用独立 profile | 扩展跑客户专用 V2 profile（v0.4 改，C-029 / C-030） | **不能同账号双跑**——V1 跑账号 A，V2 必须跑 B |
| DB schema | workstations 表有 `chrome_profile_path` 等 | V2 增 `expected_email` / `bound_profile_id_hash` / **`customer_id`**（v0.4 新，C-040） / **`tenant_id`**（v0.4 新，C-040） / **`dispatcher_kind`** | **V2 schema backward compatible** — 加列不删；V1 字段保留但 V2 ignore；NULLABLE 默认 `'default'` |
| 端口 | localhost:8080 | localhost:**8081** 双跑期 | 双跑期 V2 改 8081；客户全切后 V2 才回 8080 |
| output 目录 | `output/<date>/<sku>/...` | `output_v2/<date>/<sku>/...` 隔离 | 双跑互不覆盖 |
| settings.yaml | 含 chrome_profile_path | 扩展不需要 | V2 沿用 yaml 但加 deprecated 注释；新加 `extension_ws_token` 字段 |

#### 12.1.1 V3 multi-tenant schema 预埋（v0.4 NEW，C-040）

V3 范围列了"多操作员 / 多租户"，但**V2 schema 不预埋则 V3 数据迁移痛苦**。v0.4 在 V2.0 schema 加 NULLABLE 字段，演进低成本：

```sql
-- V2.0 migration
ALTER TABLE workstations ADD COLUMN customer_id TEXT DEFAULT 'default';
ALTER TABLE workstations ADD COLUMN tenant_id TEXT DEFAULT 'default';
ALTER TABLE tasks ADD COLUMN customer_id TEXT DEFAULT 'default';
ALTER TABLE tasks ADD COLUMN tenant_id TEXT DEFAULT 'default';
ALTER TABLE task_results ADD COLUMN customer_id TEXT DEFAULT 'default';
ALTER TABLE task_results ADD COLUMN tenant_id TEXT DEFAULT 'default';
-- forensic_log 同样
ALTER TABLE forensic_log ADD COLUMN customer_id TEXT DEFAULT 'default';
ALTER TABLE forensic_log ADD COLUMN tenant_id TEXT DEFAULT 'default';

-- V3 切 multi-tenant 时改 NOT NULL + UNIQUE constraint 即可
-- ALTER TABLE workstations ALTER COLUMN customer_id SET NOT NULL;
```

**WS register 协议**也加：
```typescript
type RegisterMessage = {
  type: 'register'
  workstation_id: string
  account_email: string
  profile_id_hash: string
  extension_version: string
  chrome_version: string
  customer_id?: string   // optional，默认 'default'
  tenant_id?: string     // optional，默认 'default'
}
```

**license schema** 已预埋 `customer_id`（§13.6）→ Co-Pilot 启动校验 license 时自动把 customer_id 注入所有 register message → 多租户隔离自然实现。

**V2.0 行为不变**：所有 `customer_id = 'default'`，dashboard / 报表 / 调度逻辑跟 V1 一致。

### 12.2 切换粒度

**按账号增量切**（不是按客户切）：
1. 客户拿到 V2.0 zip
2. 选 1 个账号试 V2（绑定 wizard 完成）→ 该账号停 V1 dispatch
3. V2 跑 1 周看效果
4. OK 后切第 2 个账号
5. 全切完后客户卸 V1

**对调度的影响**：V1 V2 共用 SQLite tasks 表 → 同一时间只有一个 dispatcher 在跑（V1 patchright OR V2 extension）；workstations 表加 `dispatcher_kind: 'v1' | 'v2'` 字段，scheduler 按 kind 派给对应 dispatcher。

### 12.3 回退路径

V2 出问题 1 步切回 V1：
1. 客户机保留 V1 PyInstaller bundle（`FlowHarvester-bundle-v0.1.0.zip` 不删）
2. 关 V2 Co-Pilot service
3. 启 V1 .exe（沿用同 SQLite DB）
4. 操作员看 V1 dashboard `localhost:8080`（V2 占 8081）
5. SQLite schema 因 backward compatible，V1 跑得通

### 12.4 数据完整性（双跑期）

- task_results 表加 `dispatcher_kind` 字段（v1 patchright / v2 extension）→ 报表可分别统计
- output 路径隔离 → 客户视觉上能区分 V1 V2 产出
- error_logs 表加 `dispatcher_kind` → 失败原因可按 kind 拆分对比

**spike 验收项**：开 V1 跑 1 任务 → 关 V1 → 开 V2 跑 1 任务 → 看 V1 dashboard 是否仍正常显示历史 + V2 数据。

### 12.5 客户文档

- `docs/v2-migration-guide.md`（release 前 1 周写）：图文演示双跑步骤
- 加视频教程（5 分钟）：装 V2 / 绑定 wizard / 切第 1 个账号
- 操作员快速参考卡：4 步操作覆盖 90% 日常

### 12.6 V2.x 内部 rollback SOP（v0.5 NEW，C-045）

§12.3 设计了 V2→V1 一刀切回退。但 V2 内部版本演进（V2.0 → V2.1 → V2.2）的 rollback **必须明确**：

#### 12.6.1 三种 rollback 类型

| 类型 | 场景 | 步骤 | 数据影响 |
|---|---|---|---|
| **扩展 only** | 新版扩展行为异常但 Co-Pilot 协议兼容 | Co-Pilot dashboard "扩展紧急回退"按钮 → guided wizard 带操作员每个 profile chrome://extensions remove + reload 旧 unpacked | 无（扩展不持久化业务数据） |
| **Co-Pilot only** | 新版 Co-Pilot bug 但扩展 OK | 停 Co-Pilot → 替换为 backup/<old_version>/Co-Pilot.exe → 启动 | 注意 SQLite schema 兼容（看 12.6.3） |
| **同时回退** | 协议不兼容 / 协调失败 | 1. 停 Co-Pilot → 2. dashboard wizard reload 旧扩展 → 3. 启旧 Co-Pilot | schema 回退 + 双方协议对齐 |

#### 12.6.2 旧版本保留

- Co-Pilot install 时备份当前 .exe 到 `%APPDATA%\FlowHarvester\backup\v2.<x>\`
- 旧版本 `extension/` 目录保留在 `%APPDATA%\FlowHarvester\extension-v2.<x>\`
- 默认保留**最近 2 个版本**（V2.x 当前 + V2.x-1 fallback）+ V1 PyInstaller bundle
- 客户机磁盘代价：~150 MB（可接受）

#### 12.6.3 DB schema 兼容性原则（v0.5 强制）

> **铁律**：V2 schema 演进**只加 NULLABLE 列，不删不改 type**。

- rollback 不需要 schema 降级，旧版 ignore 新列
- 大型 schema 重构走专门 migration tool（V1 已有）+ rollback 路径单独验证
- ALTER TABLE DROP COLUMN 在 SQLite 3.35+ 才支持，客户机 Win11 自带 SQLite 版本不可控 → 不依赖此能力

#### 12.6.4 manifest permission 严格管理

- V2.0 manifest 一次性放足所有未来 1 年用得到的 permission（C-021 持续生效）
- V2.x 升级 permission **只删不加** → rollback 不会撞 permission 不一致
- 万一加新 permission（不可避免时），release notes 明确"该版本不可 rollback 到 v2.<x-1>"

#### 12.6.5 多版本协议兼容（forward compat）

- WS message envelope `protocol_version: <int>` 每次升级递增
- V2.1 sw 必须接受 V2.0 message 形态（不破坏字段）→ V2.0 扩展 + V2.1 Co-Pilot 仍能通信
- V2.1 给 V2.0 扩展派 task 时只用 V2.0 字段，不带 V2.1 新字段
- `protocol_min_supported` 字段在 register 时校验：太旧 → reject + dashboard "请升级扩展"

#### 12.6.6 rollback 测试 plan

- spike Phase 0.5 加"rollback 演练"步骤：V2.0 → V2.1 升级后 rollback 一次
- 每次 V2.x release 前必须跑 rollback 测试
- CI 集成：rollback 失败 → block release

### 12.7 Unified release 管理（v0.5 NEW，C-055）

unpacked-only 失去 chrome auto-update 保障 → 必须重新设计版本一致性管理：

#### 12.7.1 统一 release zip（v0.6 完整结构定义，C-068）

```
flow-harvester-v2.<x>.zip
├── Co-Pilot.exe                    # 同一 version（PyInstaller bundle）
├── extension/                      # unpacked，同一 version
│   ├── manifest.json               # version: "2.x.0" + key 字段固定 ext_id（C-050）
│   ├── _locales/                   # zh_CN + en（C-039）
│   ├── background.js / content.js / popup.html / ...
│   └── ...
├── install.bat                     # v0.6 NEW，C-068：一次性自动化
│   # - GPO 检测（C-057）
│   # - 网络层检测（C-060）+ Defender 排除项 PowerShell（admin 一次性）
│   # - 装 Co-Pilot 到 %APPDATA%\FlowHarvester\
│   # - 配置 HKCU 注册表开机自启（C-033）
│   # - 写 ws_token + 复制 extension/ 到 %APPDATA%\FlowHarvester\extension\
│   # - 提示操作员 chrome://extensions reload 每个 V2 专用 profile
├── update.bat                      # v0.6 NEW，C-068：升级路径（替换 .exe + 更新 extension + 提示 reload）
├── chrome-policy/                  # v0.6 NEW，C-057
│   ├── v2-allow-unpacked.reg       # 客户公司 IT 部署 GPO 白名单
│   └── README.txt                  # GPO 部署说明
├── docs/                           # v0.6 NEW，C-068
│   ├── customer-manual.md          # 中文操作员手册
│   ├── customer-install-windows.md # 装机步骤 + AV / DoH 说明
│   ├── v2-migration-guide.md       # V1→V2 切换图文（release 前 1 周写）
│   └── v2-troubleshooting.md       # GPO / 网络 / chrome 升级 troubleshooting checklist
├── version-manifest.json           # SHA256 + version + 兼容矩阵 + min_protocol_version
└── README.txt                      # 中文，1 页快速参考
```

**GitHub Actions release workflow** 自动打 zip，含 SHA256 校验 + signed by 作者 GPG。

**强制**：客户必须**同时**升级 Co-Pilot + 全部 5 profile 扩展。**部分升级 = 拒绝跑**：
- Co-Pilot 启动校验当前 extension 目录 `manifest.json version` 跟自身 version 一致
- 不一致 → install.bat 自动 guide 操作员每个 profile reload

#### 12.7.2 Co-Pilot 主动版本检查

```python
# app/extension_dispatcher.py
def on_register(register_msg):
    if register_msg.extension_version != COPILOT_VERSION:
        return reject('version_mismatch', f'扩展 v{register_msg.extension_version} vs Co-Pilot v{COPILOT_VERSION}，请重新装扩展')
    if register_msg.protocol_version < V2_MIN_SUPPORTED_PROTOCOL:
        return reject('protocol_too_old', '请升级扩展')
    # mismatch 期间不派 task；dashboard 红色告警 "WS_X 扩展版本 v2.0 vs Co-Pilot v2.1"
```

#### 12.7.3 ws_token 双持久化（v0.5 NEW，C-055）

操作员清浏览数据 → chrome.storage 清空 → ws_token 丢 → 操作员要重新 setup wizard。**v0.5 改进**：

```
1. 扩展 register 时 chrome.storage 没 token → 自动 fetch http://localhost:8080/extension/token?profile_id_hash=<hash> 取
2. Co-Pilot 端验证 profile_id_hash 已绑定 → 返回 token（不需要重做 wizard）
3. profile_id_hash 没绑定 → 仍走原 setup wizard 流程（first-time）
```

效果：操作员清浏览数据后扩展自愈，不用重做 setup wizard。

#### 12.7.4 升级前 idle check

- 操作员点 "升级到 V2.x" 按钮前，dashboard 检测当前任务数
- > 0 个 active task → 提示"建议 idle 后再升级"，30 分钟 idle 才允许触发
- 强制升级（用户明确选择）→ active task mark `interrupted` + 记录 audit log

#### 12.7.5 测试机版本一致性

- 作者推 V2.1 release 前先在自己测试机跑 1 周
- 5 个客户机分批升级（避免全停摆），同 customer_id 内的工位**必须同时升级**（避免协议混跑）
- 升级期间作者跑 V2.1 测试，客户跑 V2.0 → 同样客户问题作者本地复现：保留 V2.0 测试环境 1 个月

### 12.8 数据备份 + 恢复策略（v0.6 NEW，C-069）

> **v0.5 缺失**：V1 也没显式备份策略。V2 任务结果 mp4 + DB 历史 + forensic_log 累计 5-10 GB / 客户机硬盘故障 / Win11 重装 / 病毒加密 → 数据全丢。**v0.6 必须 commit 备份策略**。

#### 12.8.1 备份资产清单 + 频率

| 资产 | 备份频率 | 备份位置 | 大小估算 |
|---|---|---|---|
| **SQLite DB**（tasks / workstations / forensic_log / task_results） | 每日 3am dump | `%APPDATA%\FlowHarvester\backup\db_YYYYMMDD.sqlite`（保留最近 30 天） | 100 MB - 1 GB |
| **workstation binding 配置** | 每次 register 后写一次 | `%APPDATA%\FlowHarvester\backup\workstations.json` | < 10 KB |
| **License + setup token** | install 时一次 + 升级时 | `%APPDATA%\FlowHarvester\backup\license.lic.bak` + `setup_tokens.json.bak` | < 10 KB |
| **output/** mp4（任务产出） | 客户负责（NAS / OneDrive / 外接硬盘镜像，外部） | 客户配置 | 5-10 GB |
| **forensic_log + screenshots** | 30 天 partition rotation（C-037） | 同 §13.5.6 | 累积 GB 级 |
| **chrome profile**（账号 cookie / V2 扩展状态） | chrome 自带 sync 关闭后无 → 不备份 | N/A | N/A |

#### 12.8.2 SQLite 每日 dump 实施

```python
# app/scheduler/cleanup.py
import shutil
from datetime import datetime

def daily_db_backup():
    """每日 3am cleanup task 同时跑（C-046 串联）"""
    src = SETTINGS.db_path
    dst = SETTINGS.backup_dir / f'db_{datetime.now():%Y%m%d}.sqlite'
    # SQLite online backup API（不锁库）
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    # 保留最近 30 天，删旧
    prune_old_backups(SETTINGS.backup_dir, days=30)
```

#### 12.8.3 客户机硬盘故障恢复 SOP

```
[新客户机 / Win11 重装]
  ↓
1. 装 V2（跑 install.bat）
2. 跑 restore.bat：
   - 复制 SQLite backup 最新一份到 %APPDATA%\FlowHarvester\flow_harvester.db
   - 复制 workstations.json 恢复工位 binding
   - 复制 license.lic.bak 恢复 license（注意：machine_id 不同需要作者重新签发）
3. 提示操作员重新做 chrome profile 装扩展（chrome profile 数据丢了无法恢复）
4. 每个 profile 跑 setup wizard（用 backup 里的 setup_tokens.json 恢复）
5. dashboard "备份恢复完成" 横幅
```

**关键限制**：
- chrome profile 数据（cookie / 登录态）**无法跨机器迁移**——客户必须重新登录每个 V2 账号 + 重新切英文（V1 v0.1.0 操作员驱动语言切换流程）
- License **必须重新签发**（C-027 machine binding：新机器 machine_id 不同，旧 license 失效）
- 客户跟作者协调换机后重新签发 license（运营成本：1 客户每年换机一次量级）

#### 12.8.4 Dashboard 备份状态指示

```
┌──────────────────────────────────────────┐
│ 备份状态                                    │
│ ─────────────────────                      │
│ 最后 DB 备份: 2026-05-10 03:02 ✅           │
│ 最后 Workstation 备份: 2026-05-10 14:35 ✅  │
│ 备份目录大小: 1.2 GB / 30 days              │
│ [立即备份] [查看备份历史]                    │
└──────────────────────────────────────────┘
```

dashboard 加"备份状态"卡片，操作员一眼可见。备份失败连续 3 天 → 红色告警。

---

## 13. 安全模型（NEW，C-015）

V2 扩展跑在客户 google 登录态的 chrome 内，攻击面比 V1 patchright 大。本章列威胁场景 + 缓解措施。

### 13.1 Trust boundary

```
┌─────────────────────────────────────────────────────────┐
│ TRUSTED                                                  │
│   ├── 客户 Google 账号（Gmail / Drive / Pay）             │
│   ├── 客户日常 chrome 数据（书签 / 历史 / 其他扩展）       │
│   └── 客户业务数据（prompts / SKU / 已生成 mp4）          │
└─────────────────────────────────────────────────────────┘
                ↑                    ↑
                │                    │
         (boundary 1)          (boundary 2)
         扩展 ↔ Google         扩展 ↔ Co-Pilot
                │                    │
┌─────────────────────────────────────────────────────────┐
│ SEMI-TRUSTED (我们写的代码)                               │
│   ├── Chrome 扩展（manifest v3）                          │
│   └── Co-Pilot (FastAPI / SQLite)                        │
└─────────────────────────────────────────────────────────┘
                ↑
         (boundary 3)
         Co-Pilot ↔ 客户机其他进程
                │
┌─────────────────────────────────────────────────────────┐
│ UNTRUSTED                                                │
│   ├── 客户机其他进程（恶意软件 / 浏览器 fingerprint 脚本） │
│   ├── 客户访问的恶意网页                                  │
│   └── 网络（VPN / ISP 中间人）                           │
└─────────────────────────────────────────────────────────┘
```

### 13.2 威胁场景 + 缓解（v0.4: 6 个，新增 chrome sync）

#### 13.2.1 扩展权限过宽（C-015 威胁 1）

**威胁**：扩展声明 `https://*.google.com/*` → 给扩展读所有 Google 服务 cookie / DOM 权限：Gmail / Drive / Pay / Cloud Console / Ads。一旦扩展代码被恶意修改（攻击者 / 操作员），客户全部 Google 数据泄露。

**缓解**：
- ✅ manifest host_permissions **只列** `https://labs.google/fx/tools/flow/*` + `http://localhost:8080/*`
- ✅ **不要** `chrome.cookies` / `chrome.identity` / `chrome.history` API
- ✅ content_scripts match 限定 `/fx/tools/flow/*`（§4.2 已 inline）
- ✅ 扩展代码混淆 + 关键 selector 字典加密（提门槛，不是反工程）

#### 13.2.2 扩展代码 = 明文资产（C-015 威胁 2）

**威胁**：扩展代码在 chrome 解压目录是明文 JS。操作员复制走 → 装到自己的 chrome → 用客户 license 跑。第三方拿到扩展代码 → 跑自己的 Co-Pilot 复制品 → license 完全绕过。

**缓解**：
- ✅ 扩展 init 时跟 Co-Pilot 校验 license：扩展拿到 license 签名 challenge → Co-Pilot 用 license server 公钥验证
- ✅ license 包含 `customer_id` → Co-Pilot 跟扩展握手时校验
- ✅ 扩展代码混淆（webpack obfuscator）

#### 13.2.3 localhost WS 无认证（C-015 威胁 3）

**威胁**：`ws://localhost:8080/ws/extension/<ws_id>` 任何本机进程都能连接。本机恶意软件可假装扩展 register 偷 task 数据；可假装中控 push fake task_complete 让 Co-Pilot DB 记假数据。**DNS rebinding** 攻击：恶意网页用 DNS rebinding 绕过 SOP 直接访问 localhost:8080。

**缓解**：
- ✅ FastAPI **强制 bind 127.0.0.1**（不是 0.0.0.0）— 启动时 assert
- ✅ Co-Pilot 启动时生成 32-byte secret token 写 `%APPDATA%\FlowHarvester\ws_token`
- ✅ 扩展 options 页让操作员粘贴 token → 存 chrome.storage.local
- ✅ WS register 消息必须带 token → server 端 reject 无 token / 错 token
- ✅ WS endpoint 校验 Origin header（chrome 扩展会发送 `chrome-extension://<id>` Origin；reject 其他 Origin）
- ✅ FastAPI 加 Host header 白名单中间件（只接受 `localhost` / `127.0.0.1`）防 DNS rebinding
- ✅ 关键操作（task_assign / config_update）用 short-lived 二级 token（防 replay）

#### 13.2.4 扩展窃取 cookie 风险（C-015 威胁 4）

**威胁**：chrome.cookies API 配合 host_permissions 可读所有 google.com cookie，包括 SSO token。扩展上传日志到中控时日志夹带 cookie 不被发现 → 中控诊断包邮件 → 第三方拿到客户 Google session。

**缓解**：
- ✅ manifest **不声明** `cookies` permission — 没这权限就读不到
- ✅ 扩展端日志推送中控前 client-side 脱敏：正则过滤 `cookie:` / `Authorization:` / `*.SID=` / `__Secure-*` 等模式
- ✅ Co-Pilot 诊断包打 zip 前再次过滤同模式（双重保险）
- ✅ 诊断包不含 chrome profile 数据（只含 logs + DB + screenshots）

#### 13.2.5 Co-Pilot ↔ Chrome 文件系统隔离（C-015 + C-019）

**威胁**：扩展 chrome.downloads 写到 Co-Pilot 子目录可能被恶意网页 XSS 利用，写到任意路径覆盖系统文件。

**缓解**：
- ✅ chrome.downloads filename 必须经过 server-side 校验：路径 prefix 必须是 `FlowHarvester/output/`
- ✅ chrome 自身限制 filename 不允许 `..` / 绝对路径（chrome 内置防御）
- ✅ Co-Pilot 监听目录 watch 时校验文件路径符合 `output/<date>/<sku>/<creative>/...` 模板
- ✅ Co-Pilot 拒接非 V2 命名规范的文件（防外部进程往 output/ 投毒）

#### 13.2.6 chrome sync 跨设备扩散（C-030 NEW，v0.4 新增第 6 个威胁）

**威胁**：chrome 默认开 sync。V2 扩展装在客户 chrome → 如果操作员或客户的 chrome 登录账号开了 sync：
- **场景 1**：Web Store 路径分发的 V2 扩展会被 sync 自动安装到操作员家里 chrome（同 Google 账号）→ license 跨机器扩散
- **场景 2**：误用 `chrome.storage.sync` → ws_token / setup_token / license_signature 同步到 Google 服务器
- **场景 3**：客户企业 Workspace 账号跨设备同步 → V2 扩展出现在客户家用 PC，license_signature 离线复制可能仍能用
- **场景 4**：操作员离职后 chrome 个人账号仍登 sync → 长期"远程访问"客户业务配置

**缓解**（C-030）：
- ✅ **代码层**：lib/storage.ts 唯一暴露 `chrome.storage.local`，CI lint rule 检测 `chrome.storage.sync` 出现 → 构建失败（§4.2）
- ✅ **Setup wizard 强制检测**：扩展首次启动检测 chrome sync 状态 → 开 sync 弹窗要求操作员关闭"扩展同步"才让继续
- ✅ **客户使用 V2 专用 profile**（§5.4 / §3.1）：该 profile 不绑个人 Google 账号 sync
- ✅ **License machine binding**（§13.4 / §13.6 v0.4 重写）：即使扩展跨设备同步，license 校验机器指纹失败 → 扩展拒绝注册
- ✅ **chrome.storage.local rotation**：扩展 chrome.storage.local 持久化的 license_signature 1 小时过期，每小时跟 Co-Pilot 重新握手（§13.6）
- ✅ **operator 文档强警告**：customer-manual 前置警告"V2 扩展所在 chrome profile 必须不能开 sync"
- ✅ **客户 onboarding 表单确认勾选** + Setup wizard 第一屏强制确认

**spike 验收新增 #12（C-030）**：开 chrome sync 装 V2 → 个人设备 chrome 是否真的会自动安装 V2 扩展 + 拷过 storage 数据；machine binding 是否拦下离线复制场景。

### 13.3 spike 安全 review

spike 阶段必须出 **安全 review 报告**：
1. 列每个 trust boundary 的威胁
2. 验证缓解措施实施
3. 渗透测试：模拟本机恶意进程攻击 WS endpoint，看 token / Origin / 127.0.0.1 bind 是否生效
4. 模拟 DNS rebinding：本机起一个 server 解析 localhost.evil.com 到 127.0.0.1，访问看 Host 校验是否拒绝

### 13.4 license 模型（v0.4 大重写，C-027）

> **v0.3 license 模型有结构性漏洞（C-027）**：无 machine binding → 整包复制即可绕过；无 revoke 机制 → 客户违约 / 操作员离职无法撤销；无 trial/paid 分层 → 商业化预埋空白；chrome.storage signature 无过期 → 拷贝即可长期使用。
>
> **v0.4 修正**：machine binding + 在线 revoke（V2.1+）+ tier schema + signature 短期过期 + 不可转移条款。具体 schema 见 §13.6。

#### 13.4.1 校验流程（v0.4）

```
[Co-Pilot 启动]
1. 读 license.lic（V2.0 离线签名）+ 读本机 machine_id_hash
2. 校验 license.signature（公钥）
3. 校验 license.machine_id_hash == 本机 machine_id_hash → 不一致 → 拒跑（拷贝防御）
4. 校验 license.expiry > now → 过期 → 拒跑
5. 校验 license.tier （限 max_concurrent_workstations / max_daily_tasks）
6. (V2.1+) 在线 ping license server 取 revoke list → 命中 → 拒跑

[扩展 register]
1. 扩展跟 Co-Pilot 握手 → Co-Pilot 给扩展返 short-lived signature（1 小时过期）+ tier
2. 扩展 chrome.storage.local 持久化 license_signature + tier
3. 扩展每小时跟 Co-Pilot 重新握手取新 signature（拷贝防御：1 小时后失效）
4. 扩展每次 task_assign 前校验 signature 未过期 + tier 未超限

[license revoke 路径]
- V2.0：客户机删 license.lic + 重启 Co-Pilot → 所有扩展 1 小时内失效（chrome.storage signature 自然过期）
- V2.1+：license server revoke list ping → Co-Pilot 拒所有 task_assign → 扩展自动停
```

#### 13.4.2 machine binding（C-027）

```python
# Co-Pilot 启动时计算 machine_id_hash
import hashlib, winreg, subprocess

def compute_machine_id_hash() -> str:
    parts = []
    # Win11 MachineGuid（注册表，稳定）
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
        parts.append(winreg.QueryValueEx(k, "MachineGuid")[0])
    # 主板 serial（稳定）
    parts.append(subprocess.check_output(["wmic", "baseboard", "get", "serialnumber"]).decode())
    # CPU id（稳定）
    parts.append(subprocess.check_output(["wmic", "cpu", "get", "processorid"]).decode())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]
```

- license.lic 签发时绑该 hash（客户首次安装 → 上报 hash → 后端签发对应 license.lic）
- 客户换机器 → 重新签发（运营成本可接受，量级：1 客户每年换机一次）

#### 13.4.3 不可转移条款（C-027）

license JSON 含：
- `customer_id`（绑定客户主体）
- `customer_name`
- `bound_machine_id_hash`
- `issued_at` / `expiry`

dashboard 显著显示："License: 客户XXX (machine: YYY) — 有效期至 2026-12-31"——客户转手即可见违约。

#### 13.4.4 V2.0 离线 license + V2.1+ online callback（C-027）

| 阶段 | 模式 | revoke 能力 |
|---|---|---|
| **V2.0** | 离线 license.lic（运营简单） | 删 license.lic 重启 → 1 小时内全失效（依赖 §13.6 signature 过期机制）|
| **V2.1+** | online 模式 + 7 天容忍 | Co-Pilot 每天 ping license server 取 expiry / revoke list；网络抖一天客户不停 |

### 13.5 合规 + 数据流（v0.5 重写，C-044）

> **v0.4 矛盾**：§13.5 写"客户机 V2 不上传任何客户数据"用括号补丁带过 cloudflared，跟 §2.2 / §4.1 / §11 多处 cloudflared 保留矛盾。v0.5 全部重写为"数据流 + 保留期 + 默认状态"明确表。

#### 13.5.1 数据资产 + 流向 + 保留期

| 数据资产 | 客户机本地 | cloudflared 可访问 | 邮件诊断包流出 | 保留期 |
|---|---|---|---|---|
| Prompt 文本 | ✅ SQLite tasks 表 | ⚠️ 仅启用时 | ⚠️ 仅诊断包前 50 字符（截断） | 30 天 |
| SKU / creative | ✅ tasks 表 | ⚠️ 仅启用时 | ✅ 完整（业务背景需要） | 30 天 |
| Screenshot | ✅ output/.snapshots/ | ⚠️ 仅启用时 | ✅ 全包含 | 30 天 / 5GB |
| forensic_log | ✅ SQLite forensic_log 表 | ⚠️ 仅启用时 | ✅ 全包含 | 30 天 |
| mp4 输出 | ✅ output/ | ❌ 默认不暴露（只暴露 dashboard） | ❌ 不含 | 客户管理 |
| chrome.storage.local（task_state） | ✅ chrome 内部 | ❌ | ❌ | sw 重连后 cleanup |
| ws_token / license_signature | ✅ %APPDATA% / chrome.storage | ❌ 严禁 | ❌ 诊断包过滤 | 跟 V2 同生命周期 |

#### 13.5.2 cloudflared 隧道安全模型（v0.5 重写）

> **v0.5 commit**：cloudflared **默认关闭**，operator 主动 dashboard 按钮启用，30 分钟自动关闭，named tunnel + access policy。

```
Operator (operator@example.com)
        │ Cloudflare Zero Trust SSO
        ▼
cloudflared named tunnel ──── access policy ──── Co-Pilot dashboard (127.0.0.1:8080)
        │                     ↑                      │
        │                绑作者邮箱                    │
        │                                            │
        ▼                                            ▼
仅作者邮箱白名单可访问                          Token-based auth
```

**关键决策**（v0.5 commit）：
- ❌ **不用匿名 tunnel**（匿名 URL 公开后任何人可访问 dashboard，无 auth）
- ✅ **named tunnel + cloudflare zero-trust access policy**：客户机 install wizard 配置 access policy 绑作者邮箱
- ✅ **默认关**：V2 启动时 cloudflared 不自动启动；operator 在 dashboard 显式按"开启远程支持"按钮才启
- ✅ **30 分钟自动关闭**：启用时 dashboard 显著横幅"远程支持已启用，作者可访问，剩余 28 分钟"；超时自动关
- ✅ **审计日志**：每次 cloudflared 启用 / 关闭写 forensic_log，作者每次访问写 forensic_log（cloudflared 提供 access log）
- ✅ **token-based auth**：不是匿名 URL，通过 cloudflare access SSO 验证作者身份

#### 13.5.3 诊断包加密（v0.5 NEW，C-044）

```
客户机 Co-Pilot 生成诊断包流程：
1. 收集 logs + DB + screenshots → tar
2. 自动脱敏：
   - prompt 截断前 50 字符
   - 替换客户名为 <customer-redacted>
   - 过滤所有 cookie / Authorization / SID / __Secure-* (§13.2.4)
3. 用作者公钥（GPG / age）加密 → 输出 .tar.age
4. 邮件发出仅作者私钥可解密
```

**作者侧约束**：
- 收到诊断包 → 解密查看 → 处理完成后 30 天必须删除
- 作者机器加密 disk（macOS FileVault / Win11 BitLocker 强制）
- 不上传到任何第三方服务（github gist / pastebin 等严禁）

#### 13.5.4 GDPR DPA（数据处理协议）

- V2 跨欧盟客户使用 → 客户作为 controller，作者作为 processor → GDPR Article 28 要求 DPA
- spike 阶段写 `docs/v2-dpa-template.md` 标准 DPA 模板
- 客户合规要求时双方签 DPA → 远程支持 SLA 含数据处理条款
- 诊断包加密 + 30 天作者侧保留期 + cloudflared access log 都是 DPA 合规材料

#### 13.5.5 隐私政策

- 必须有 web 页面说明数据收集 / 使用（即使不上 chrome web store，customer-manual.md 链接到的隐私政策也必须有）
- 客户 onboarding 表单确认勾选

#### 13.5.6 磁盘累积 + cleanup 策略（v0.4 → v0.5 沿用，C-037）

V2 长跑 1-3 个月后磁盘 GB 级累积（5 profile × 100 task/day × 2-3 screenshots × 30 天）。C 盘满 = Win11 卡 = V2 整体崩。**rotation policy 必须实现**：

| 资产 | 保留策略 | 实现 |
|---|---|---|
| `forensic_log` SQLite 表 | **按日 partition + 30 天 rotation**（v0.5 改，C-046 串联） | Co-Pilot 每日 3am cleanup task `DROP TABLE forensic_log_<old_date>` + `VACUUM` |
| screenshots（task 错误截图） | 30 天 / 5 GB 上限（先到先删） | watchdog 监控 `output/.snapshots/`，按 mtime + 总大小 prune |
| 诊断包 zip | 保留最新 5 个 | Co-Pilot 启动 + 每日 cleanup |
| chrome.downloads 临时文件 | chrome 自管 | N/A |

dashboard 显示磁盘占用 + 手动清理按钮（紧急释放空间）。

#### 13.5.7 Windows Defender / AV 干扰（v0.4 → v0.5 沿用，C-036）

- ✅ Co-Pilot.exe 上 **code signing cert**（~$500/年，EV cert 更佳）→ Windows SmartScreen 直接通过
- ✅ install wizard 自动加 Windows Defender 排除项（`Add-MpPreference -ExclusionPath`，admin 一次性）
- ✅ `customer-install.md` 加常见 AV 白名单步骤（卡巴斯基 / 360 / 腾讯电脑管家 / Norton 单独说明）
- ✅ Win11 默认 Defender Firewall 不拦 localhost；企业 GPO 拦的客户提供 GPO snippet

### 13.6 license tier schema（v0.4 NEW，C-027）

> v0.3 license 模型没预埋 trial/paid 分层 → 商业化时改 schema 大改。v0.4 schema 一次设全，V2.0 用 default tier，V3 商业化直接配 tier 即可。

#### 13.6.1 license JSON 结构

```json
{
  "license_format_version": 1,
  "customer_id": "cust_001",
  "customer_name": "ACME Inc.",
  "bound_machine_id_hash": "a1b2c3d4...",
  "issued_at": "2026-05-09T00:00:00Z",
  "expiry": "2027-05-09T00:00:00Z",
  "tier": "standard",
  "limits": {
    "max_concurrent_workstations": 5,
    "max_daily_tasks": 200,
    "max_total_videos_per_month": 5000,
    "allow_remote_dashboard": true,
    "allow_cloudflared_tunnel": true
  },
  "features": {
    "frames_mode": true,
    "multi_round": true,
    "extension_chrome_store": true
  },
  "signature": "base64-rsa-signature"
}
```

**Tier 枚举**：

| Tier | max_workstations | max_daily_tasks | 用途 |
|---|---|---|---|
| `trial` | 1 | 10 | 试用 7 天 |
| `standard` | 5 | 200 | 当前 V1/V2 客户场景 |
| `pro` | 10 | 500 | 大客户 |
| `enterprise` | 无限 | 无限 | V3 商业化 |

#### 13.6.2 Co-Pilot 校验

```python
# Co-Pilot 启动时
license = parse_license(license_lic_path)
verify_signature(license, license_pubkey)
assert license.bound_machine_id_hash == compute_machine_id_hash()
assert license.expiry > now
TIER_LIMITS = license.limits

# 调度时（claim_one）
def claim_one(...):
    active_count = count_busy_workstations()
    if active_count >= TIER_LIMITS.max_concurrent_workstations:
        return None  # 调度暂停
    daily_count = count_today_tasks()
    if daily_count >= TIER_LIMITS.max_daily_tasks:
        return None  # 配额超
```

#### 13.6.3 chrome.storage signature 过期

```typescript
// extension/src/background.ts
async function ensureValidLicense() {
  const sig = await storage.get('license_signature')
  if (!sig || sig.expires_at < Date.now()) {
    // 跟 Co-Pilot 握手取新 signature（1 小时有效）
    const fresh = await ws.requestLicenseSignature()
    await storage.set({ license_signature: fresh })
  }
}
chrome.alarms.create('license_refresh', { periodInMinutes: 50 })
```

效果：扩展整包拷贝走 → 离开 Co-Pilot 1 小时即失效。

#### 13.6.4 dashboard license display

```
┌──────────────────────────────────────────────┐
│ License: ACME Inc. (cust_001)                 │
│ Tier: Standard | 5 ws | 200 tasks/day         │
│ Machine: a1b2c3d4...                          │
│ Expiry: 2027-05-09 (在 365 天后过期)          │
│ ✅ 已激活                                       │
└──────────────────────────────────────────────┘
```

客户违约转手即可见 customer_id / machine_id 不一致。

### 13.7 Dashboard log 过载控制（v0.5 NEW，C-053）

§4.3 protocol `log` 消息频率高（5 工位 × 50-100 条/分钟 → 250-500 条/分钟 forensic_log INSERT），**没有过载控制 dashboard 信息淹没**：

#### 13.7.1 中控 log dedup

```python
# app/extension_dispatcher.py
class LogDedup:
    """5 分钟窗口内相同 (level, message_stem) 合并"""
    def __init__(self):
        self.window: dict[tuple[str, str], dict] = {}

    def push(self, ws_id: str, level: str, msg: str):
        stem = self._compute_stem(msg)        # 截断 timestamp / id 等可变部分
        key = (level, stem)
        if key in self.window:
            self.window[key]['count'] += 1
            self.window[key]['last_ws'] = ws_id
            return  # dedup, 不写 SQLite
        # 新 message → 写入 + 5 分钟后 flush count
        self.window[key] = {'count': 1, 'first_ts': time.time(), 'last_ws': ws_id}
        write_forensic_log(ws_id, level, msg)
```

效果：相同 selector_drift 连发 100 次 → SQLite 只 1 条 + count 字段 100。

#### 13.7.2 Dashboard 默认过滤

- 默认只显示 `ERROR` + `WARN`，`DEBUG` / `INFO` 折叠（操作员展开按钮）
- 高频重复 error 自动收敛成"this error 100 次/min" 单行
- error rate trend 24h 图表（按工位 + error_type 拆分）

#### 13.7.3 Alert 阈值

- error rate > 10/min → dashboard 红色横幅
- 持续 30 min → 邮件告警作者（如 cloudflared 启用 + audit）
- contract_drift 错误任何 1 次 → 立刻告警（不靠阈值）

#### 13.7.4 cloudflared 远程访问体验

- 作者远程登录 dashboard 时也受同样过滤（不被信息过载）
- 全 log 查询走 SQLite 直接 query（dashboard "高级 log 查询" 页面），不走主页

---

## 14. 国际化（i18n，v0.4 NEW，C-039）

> v0.3 扩展 popup / options 全英文，跟 V1 中文 dashboard 体验割裂（操作员看 dashboard 中文 → 看扩展 popup 英文）。v0.4 用 chrome.i18n API 对齐。

### 14.1 文件结构

```
extension/
├── manifest.json                    # default_locale: zh_CN
└── _locales/
    ├── zh_CN/messages.json          # 默认（V1 操作员习惯）
    └── en/messages.json             # fallback
```

### 14.2 调用约定

```typescript
// 取代 hardcoded "Workstation: WS_A"
import { i18n } from 'chrome'
const label = i18n.getMessage('popup_workstation_label') + ': WS_A'

// _locales/zh_CN/messages.json
// { "popup_workstation_label": { "message": "工位" } }
// _locales/en/messages.json
// { "popup_workstation_label": { "message": "Workstation" } }
```

### 14.3 文档对齐

- `docs/customer-manual.md` 加中文版本（已有）
- `docs/customer-install-windows.md` 加中文版本（已有）
- 新增 `docs/customer-install-windows-en.md`（V3 国际化客户预备）

---

## 15. Chrome 版本兼容矩阵（v0.4 NEW，C-031）

> v0.3 仅写"chrome 117+ 下限"，但**没具体版本对照表 + 没考虑客户机版本审计 + 没考虑 chrome 升级摩擦**。v0.4 建完整矩阵 + 立 G9 gate + 升级监控。

### 15.1 客户机 chrome 版本审计（G9 立项 gate）

> ⚠️ **G9 立项 gate（NEW）**：客户机 chrome 版本审计 ≥117。

- 客户机部署前 V2 install wizard 检测 chrome 版本（`chrome.exe --version` / 注册表 `HKLM\SOFTWARE\Google\Chrome\BLBeacon\version`）
- <117 install 失败 + 提示升级路径（Google Chrome 官网下载链接）
- 不达标的客户机先升级 chrome，再装 V2

### 15.2 关键 API 跨版本行为

| chrome 版本 | 关键 API 行为 | V2 影响 | 支持状态 |
|---|---|---|---|
| 88-94 | manifest v3 引入；mv2 共存；sw 不稳 | spike 失败概率高 | ❌ 不支持 |
| 95-101 | sw idle timeout 5min；scripting v1 | sw hibernate 测试结果可能偏乐观 | ❌ 不支持 |
| 102-108 | scripting API v2；mv2 弃用启动 | scripting injection 模式变 | ❌ 不支持 |
| 109-116 | offscreen documents API（解决 sw hibernate 真正解，比 alarms 优） | V2.0 不依赖此但保留升级空间 | ⚠️ 兼容（不优化） |
| **117-119** | chrome.alarms 最小周期改 30s | C-001 缓解措施依赖此 | ✅ V2.0 最低支持 |
| **120-124** | mv2 完全弃用 | 只剩 mv3，跟 V2 一致 | ✅ V2.0 推荐 |
| **125-129** | declarativeNetRequest 新限制 / webRequest blocking 弃用 | 网络拦截方案重写（V2 不用） | ✅ V2.0 推荐 |
| **130+** | service worker 行为细微调整 | 待 release notes 确认 | ✅ V2.0 跟进 |

### 15.3 spike 测试矩阵（C-031）

spike 必须在至少 **3 个 chrome 大版本**跑：117 / 124 / 130（覆盖最低支持 / 推荐 / 最新）。每个版本跑 §7.3 全部 spike 验收 #1-#12。不一致行为标 risk。

### 15.4 graceful degradation

扩展 startup 检测 chrome 版本：

```typescript
// extension/src/background.ts
const chromeVersion = parseInt(navigator.userAgent.match(/Chrome\/(\d+)/)![1])
if (chromeVersion < 117) {
  showCriticalBanner('chrome 版本过低，请升级到 117+')
  return  // 拒绝注册
}
if (chromeVersion < 120) {
  // alarms 周期 1min 降级（chrome 117-119 实测 30s 可能不准）
  ALARMS_PERIOD_MIN = 1
  ws.send({ type: 'log', level: 'warn', message: 'chrome version <120, alarms degraded to 1min' })
}
```

### 15.5 chrome 升级监控

- Co-Pilot 每周比对客户机 chrome 版本 vs chrome stable release（fetch `chromiumdash.appspot.com/fetch_releases?channel=Stable`）
- chrome 即将大升级（next stable beta 已发布）→ 中控告警："下周 chrome 大升级到 vXXX，建议先在测试机验证"
- Co-Pilot release cadence：chrome 大版本升级前 2 周冻结 V2 release，先在 chrome beta 验证

### 15.6 Edge / Brave / Vivaldi（明确不在 V2.0 范围）

- chromium-based 浏览器某些 manifest v3 API 实现略不同
- V2.0 **只支持 Google Chrome**（write to §6.3）
- 客户机如果是 Edge → install wizard 拒绝并提示装 chrome
- Edge / Brave 进入 V3+ 范围考虑

---

## 16. V1 Fragility 回归测试集（v0.4 NEW，C-032）

> v0.3 §7.2 验收用 "task 成功率 ≥ V1 baseline + 5%" 间接判定，**端到端测试无法证明每条 V1 fragility 在 V2 是 fix 状态**——可能"刚好这次没撞到"。v0.4 强制建立逐条对应的 regression suite。

### 16.1 文档归属

详见 [`docs/v1-fragility-regression-suite.md`](v1-fragility-regression-suite.md)（spike Phase 0.5 输出）。本节仅标 reference + 验收要求。

### 16.2 三态标注（每条 V1 fragility 必标）

| 状态 | 含义 | V2 处理 |
|---|---|---|
| **V2 仍存在** | 行为层 fragility，扩展同样会撞 | 必须重新实现（移植 V1 fix 到 TS）|
| **V2 架构层消除** | patchright 特有的 fragility（webdriver / CDP fingerprint） | 仍要测，证明真消除 |
| **V2 用新机制处理** | V1 SQL filter / Python state machine 在 V2 改成扩展 / chrome.storage / WS protocol | 测新机制行为对齐 |

### 16.3 fixture 库

- DOM snapshot：V1 客户复现的 Flow page DOM 状态 → 写成 HTML fixture（`tests/fixtures/v1_dom/*.html`）
- 网络 stub：mock Veo 后端 unusual_activity / no_flow_access / generation_failed response
- 时序 fixture：sw hibernate / chrome 关 / WS 断 各种时序

### 16.4 CI 集成

- 每次 V2 build 跑 35 条 regression
- **必须 35/35 pass 才能 release**
- regression 失败的 commit 自动 revert（GitHub Actions block merge）

### 16.5 §6.1 milestone 集成

- **Phase 0.5（spike 之前，1 周）** — 建立 `v1-fragility-regression-suite.md` + reproducer fixture + 每条 V2 状态标注
- §7.2 V2.0 release 验收第 10 项：V1 35 条 fragility 全部 verified（35/35 pass in V2）

---

## 17. 后端契约监控（v0.5 NEW，C-047）

V1 6 轮迭代每轮都源于 Google contract 变化（Flow UI / DOM 结构 / Veo API path）。设计稿 v0.4 完全没"Google 改 contract → V2 检测 + 响应"机制。**v0.5 必须实施**。

### 17.1 金丝雀 healthcheck 任务

V2 内置每日"healthcheck"任务，提前于客户业务任务发现 contract drift：

```python
# app/scheduler/healthcheck.py
def run_daily_healthcheck():
    """每日凌晨 2am 跑 1 个固定任务，发现 contract drift"""
    healthcheck_task = {
        'task_id': f'_healthcheck_{date.today()}',
        'sku': 'HEALTHCHECK',
        'creative': 'baseline',
        'flow_project_url': SETTINGS.healthcheck_project_url,
        'asset_paths': [SETTINGS.healthcheck_image_path],
        'prompt': SETTINGS.healthcheck_prompt,
        'mode': {'subtab': 'ingredients', 'output_count': 1, ...},
    }
    outcome = dispatch_to_extension(workstation=SETTINGS.healthcheck_ws, task=healthcheck_task)
    if not outcome.success:
        # 中控告警 + dashboard 红色横幅 "contract 可能变了"
        alert(f'Healthcheck failed: {outcome.error_type} - {outcome.error_message}')
```

- **专用账号**：客户分配 1 个专门 healthcheck 账号（不跟业务账号混）
- **时间**：每日 2am（业务低峰，不影响产能）
- **失败信号**：`unknown_error` / `selector_drift` / 任何非 `unusual_activity` 的失败 → 立刻告警

### 17.2 多客户机汇总监控

多客户机分散撞同一 contract drift，集中监控才能区分"账号问题"vs"contract 变"：

- 客户机 forensic_log 中"unknown error type"频率上升 → 高度怀疑 contract drift
- dashboard 显示"近 24 小时 unknown error 趋势图"
- error_type 分布对比：本周 vs 上周，selector_drift 占比突然 > 20% → 自动告警

### 17.3 远程 selector 配置 hot update

selector 字典放中控 SQLite + 启动时下发扩展（不打包扩展）：

```typescript
// extension/src/lib/selector_config.ts
async function fetchSelectorConfig() {
  // 启动时 + 每小时拉一次最新字典
  const config = await ws.request({ type: 'selector_config_get' })
  await storage.set({ flow_selectors: config })
}

// content/flow_dom.ts 用 storage 里的 selector，不是 hardcoded
const selectors = await storage.get('flow_selectors')
const generateBtn = await findElement(selectors.generate_button)
```

**WS 协议加 message type**：
- `selector_config_update` (server → ext): 中控 push 新字典
- `selector_config_get` (ext → server): 扩展启动时 fetch

**hot update flow**：
```
作者 detect Google 改 Flow UI
  → push 新 selector dictionary 到中控（cloudflared 启用时通过隧道 / 客户机本地通过 SSH）
  → 中控写 SQLite selector_configs 表
  → 中控 WS broadcast selector_config_update 给所有 ws
  → 扩展更新 chrome.storage.local
  → 立即生效，不用重发 V2 版本
```

效果：v0.0.4 类型的 fix 从"3 天发版"缩短到"1 小时下发"。

### 17.4 ErrorType 扩展（v0.5 NEW，C-047）

§4.3 protocol ErrorType enum 新增：

```typescript
type ErrorType =
  | 'generation_failed' | 'unusual_activity' | 'no_flow_access'
  | 'service_unavailable' | 'audio_failure' | 'timeout'
  | 'locale_drift' | 'extension_crash' | 'page_navigation_failed'
  | 'download_failed' | 'asset_missing' | 'flow_project_unreachable'
  | 'captcha_required'              // v0.5 NEW，C-047
  | 'contract_drift'                // v0.5 NEW，C-047 — selector 全找不到 / Veo API 行为变
  | 'onboarding_required'           // v0.5 NEW，C-047 — Google 加新 onboarding step
  | 'login_required'                // v0.5 NEW，C-051 — cookie 过期 / redirect 到 login
```

**中控按类型分流**：
- `captcha_required` → 操作员 dashboard 显著告警，工位状态 `manual_check`，等操作员人工解
- `contract_drift` → 触发 §17.1 healthcheck + §17.3 远程 selector update 流程
- `onboarding_required` → 扩展自动尝试 dismiss 已知 onboarding popup（multilingual），失败再 `manual_check`
- `login_required` → 工位 `manual_check`，等操作员手动登录该账号

### 17.5 变更触发的 hot fix flow

```
[Google 改 Flow]
  ↓ 客户机扩展 selector 失败 → 上报 contract_drift
[中控 dashboard 告警]
  ↓ 作者 cloudflared 隧道远程登录客户机 dashboard 看 forensic_log
[作者本地复现]
  ↓ 在本地测试机重现，写新 selector
[中控 selector_configs 更新]
  ↓ 通过 cloudflared 隧道传新字典
[扩展自动 hot update]
  ↓ 1 小时内全客户机生效
[V2.x release]
  作为下一次版本一致性升级（C-055）
```

**spike 验收新增 #16（C-047）**：模拟 selector 失败 → 中控通过 selector_config_update 推送修复 → 客户机扩展不重启 hot update 成功。

---

## 18. 客户协作框架（v0.6 NEW，C-056 / C-064）

> **v0.5 致命缺失**：11 个立项 gate 中 5 个需要客户配合（G1/G2/G7/G8/G10/G11 + spike #7），但客户已"V1 v0.1.0 交付完成"，配合意愿 / NDA / 时间窗口 / 收费 / 责任全部空白。**v0.6 必须 inline 客户协作框架**。

### 18.1 PoC 客户选型

| 选项 | 优势 | 劣势 |
|---|---|---|
| **现有 V1 客户参与 PoC**（首选） | 业务场景已知 / 操作员熟悉 / 测试有连续性 | 客户当前没痛点，配合动力低 |
| 新发展 V2 lead customer | 客户对 V2 有期待 / 配合度高 | 业务场景陌生 / 需重头建立信任 |
| 内部模拟客户（作者自己测试机扮演） | 不依赖外部 | 数据不可信，gate 数据无说服力 |

**v0.6 commit**：先尝试现有 V1 客户中**最配合的 1-2 个**，提供"免费 PoC 换早期 V2 折扣"激励；同时备份发展新 V2 lead customer 作 Plan B。

### 18.2 PoC 周期 + 客户投入承诺

| 阶段 | 客户投入 | 时间窗口 |
|---|---|---|
| 客户访谈（G1-G11 涉客户） | 操作员 2-4 工时 + 决策人 1-2 工时 | 1 周 |
| GPO 检测 + 公司 IT 协调 | 客户 IT 团队 4-8 工时 | 1-2 周（异步） |
| spike Phase B（客户机 RAM / 实测） | 操作员 4-8 工时 + 客户机访问权 | 1 周 |
| V2.0 内测（按账号增量切，§12.2） | 操作员 5-10 工时/周 | 2-3 周 |

**总投入**：客户 20-40 工时 + 客户 IT 4-8 工时 + 客户机访问 1-2 周

### 18.3 商业关系（三选一）

| 模式 | 内容 | 适合场景 |
|---|---|---|
| **A: 免费 PoC 换早期折扣**（首选） | 客户 PoC 免费 → V2 release 后享 30-50% license 折扣 + 1 年免费升级 | 现有 V1 客户 |
| **B: 收费咨询模式** | 作者收 PoC 咨询费 → 客户全程深度参与 → V2 共建权益 | 新 V2 lead customer 大客户 |
| **C: 共建合伙模式** | 客户参与产品决策 → V2 商业化共享分成 | 长期战略客户 |

### 18.4 NDA + DPA 模板

PoC 启动前签：
- **NDA**：作者访问客户机 + 看 prompt / SKU / mp4 元数据 = 商业秘密保密义务
- **DPA**（GDPR Article 28）：作者作为 processor，客户作为 controller，明确数据处理范围 / 保留期 / 销毁路径
- 模板放 `docs/v2-nda-template.md` + `docs/v2-dpa-template.md`（spike 阶段写）

### 18.5 失败责任 + 出口预案

| 风险 | 责任划分 |
|---|---|
| spike 中客户账号被 ban | 作者承担（PoC 风险预案：作者预付 N 个备份账号） |
| 客户机崩 / 数据丢 | 作者承担（spike 前必须备份 §12.8 + DPA 约定） |
| spike 失败客户已投入时间 | 作者免费维持 V1 6 个月 + V2 release 后 5 折升级 |

**spike 失败客户出口**：
- V1 老客户保留 V1 v0.1.0，作者继续维护 6 个月
- V2 找新客户首发（"V2 是新客户首发版本，V1 老客户自愿升级"，减老客户配合压力）
- V2 价值再定位避免强制切换

### 18.6 §6.1 milestone "客户协作期" 串联

§6.1 Phase 0（客户协作期）跟 Phase -1（测试基础设施）**异步并行**——任何 Phase A/0/0.5 失败不浪费另一边客户时间。

### 18.7 V2 运营成本明细（v0.6 NEW，C-064）

| 项目 | 成本 | 频率 |
|---|---|---|
| 测试机硬件 | $3000-5000（3 台 Win11 PC） | 一次性（V2.0 release 前） |
| Veo 测试账号 | $300-500 | 月度 |
| chrome 升级跟进 | 1-2 工时 | 月度 |
| extension release（unpacked 5 profile reload） | 4-8 工时/客户/月 | 月度（每个客户） |
| 客户支持 SLA（V2 vs V1） | **5-10 工时/月/客户**（chrome 升级跟进 + extension reload + GPO 配置协助） | 月度 |
| 5 客户机 = 25-50 工时/月 | = **1-2 工程师全职** | 月度 |
| cloudflared 流量（GB 级） | $0（免费 tier 够用） | N/A |
| GPG / age 加密密钥管理 | 0.5 工时 | 季度 |
| 测试基础设施月度运维 | 2-4 工时 | 月度 |

**V2 单客户 break-even 分析**：
- V1 license 价格假设：$X/年/客户
- V2 客户支持成本：5-10 工时/月 × 12 月 = 60-120 工时/年
- 若每工时成本 $50-100 → 单客户支持成本 $3000-12000/年
- V2 license **必须翻倍 V1 价格**，或者 V2 限制每作者 5-10 客户上限避免运营崩溃

**v0.6 commit**：V2 商业模型必须 V2.0 release 前敲定（license 提价 / 升级 SLA 加价 / 单客户上限），不能"先做再算账"。

---

## 19. 测试基础设施（v0.6 NEW，C-058 / C-065）

> **v0.5 致命缺失**：spike 多处提到"测试机"作为基础设施，但**怎么搭、谁付钱、什么账号**全部空白。**v0.6 必须 inline 测试基础设施 + Phase -1 搭建期**。

### 19.1 测试机最小集

| 测试机 | 用途 | OS / chrome 版本 |
|---|---|---|
| **主测试机** | spike 独立验证 + V2 实施期日常开发测试 | Win11 + chrome stable 117 兼容下限 |
| **chrome beta channel 永久跑** | 提前 4 周看 chrome 大版本升级影响（C-054 / §17.5） | Win11 + chrome beta channel |
| **客户机镜像（client-spec）** | 模拟客户低端机（8 GB RAM）跑 spike #7 + 性能测试 | Win11 + 8 GB RAM + 客户机相似网络 |

总计 **3 台 Win11 PC + 网络**，预算 $3000-5000 一次性。

### 19.2 Veo 测试账号清单

| 账号 | 语种 | 用途 |
|---|---|---|
| 主测账号 | 英文（en-US） | spike #1 / #4 / #6 / #11 / #15 |
| 越南账号 | 越南语（vi） | spike #2 locale + C-041 CSP 验证 |
| 中文账号 | zh-CN | C-052 IME 输入验证 |
| 阿非利卡账号 | af | locale 多语种边缘场景 |
| 日文账号 | ja | i18n 矩阵（C-065） |

**月度成本**：$50-200/账号 × 5 = **$250-1000/月** Veo 配额。**列入 V2 项目预算**。

### 19.3 i18n 测试矩阵（v0.6 NEW，C-065）

客户产品多国卖意味着 SKU / prompt 含多语种文本组合（中文 SKU + 英文 prompt / 日文 SKU + 中文 prompt 等）。spike + V2.0 release 前必须验证：

| 测试维度 | 内容 | 验收 |
|---|---|---|
| Flow UI locale | 越 / 中 / 阿非利卡 / 日 / 英 5 种 | spike #2 + 13 语言 selector 列表 fallback |
| SKU 输入 | 中文 / 日文 / 韩文 / 泰文 / 阿语 RTL | NTFS sanitize + filename 落盘正确（C-049） |
| Prompt 输入 | 跨语种（中 SKU + 英 prompt 等组合 5 种） | setReactInputValue + IME composition 防御（C-052）+ Veo 后端处理一致性 |
| 输出 mp4 codec | h264 / h265 / VP9（如有） | 客户 NLE / Win Media Player 兼容（C-066） |

文档加多语种支持声明 + 已知边缘场景。

### 19.4 CI runner 选型

| 选项 | 优势 | 劣势 |
|---|---|---|
| **GitHub Actions self-hosted runner**（首选） | 跑在测试机上 → 解决 Google 网络限制 + 跑真实 Veo 后端 | 需要自维护 runner |
| GitHub Actions cloud runner | 无运维 | 不能跑真实 Veo（IP 可能被风控）+ 无 Win11 + chrome 多版本 |
| 全 mock Veo（fixture-based） | 跑得快 + 无 Veo 配额消耗 | 不能验真实后端契约（C-047） |

**v0.6 commit**：GitHub Actions self-hosted runner 跑在主测试机 + chrome beta 测试机。

### 19.5 Fixture 库管理

- **存放**：独立 repo `flow-picker-tool-fixtures` 或 Git LFS（DOM snapshot html 文件大）
- **分类**：见 [docs/v1-fragility-regression-suite.md](v1-fragility-regression-suite.md) §"fixture 库目录约定"
- **版本化**：每次 Google 改 Flow UI → 添加 dated fixture（`flow_phrase_unusual_activity_20260510.html`）保留历史
- **CI 消费**：每次 V2 build 跑 35 fragility regression（C-032）

### 19.6 Phase -1 搭建（§6.1 milestone）

总计 **1-2 周** wall-clock：
- Week 1: 3 台测试机硬件采购 / OS 安装 / chrome 多版本配置
- Week 2: Veo 测试账号注册 / CI runner 配置 / Fixture repo 建立 / 主流程冒烟

Phase -1 可跟 Phase 0（客户协作期）异步并行，互不阻塞。

---

**End of design draft v0.6.**

**当前状态**：
- v0.1 → v0.2（集中响应 §11，错路）→ v0.3（**inline 集成** v0.0.1 + v0.0.2）→ v0.4（**inline 集成** v0.0.3）→ v0.5（**inline 集成** v0.0.4 共 12 条 + **架构反转 unpacked-only**）→ v0.6（**inline 集成** v0.0.5 共 14 条）
- 4 Blocker（v0.0.1）+ 1 meta + 2 Blocker（v0.0.2）+ 2 Blocker（v0.0.3）+ 1 Blocker (C-041) + 1 Blocker (C-054)（v0.0.4）+ **2 Blocker (C-056 / C-057)（v0.0.5）** = **13 条 spike-gate 必须的 Blocker**（C-042 OBSOLETE）
- 时间表 **12-16 周**（v0.6 重排：Phase -1 测试基础设施 1-2 周 + Phase 0 客户协作访谈 1-2 周（异步并行）+ Phase 0.5 fragility Top-10 1 周 + Spike Phase A 独立 1 周 + Spike Phase B 客户协作 1 周 + 实施 9-12 周）
- §12 V1→V2 迁移 + §12.6 V2.x rollback SOP + §12.7 unified release + §12.8 备份恢复（v0.6 NEW）+ §13 安全模型 + §14 i18n + §15 chrome 兼容矩阵 + §16 V1 fragility regression + §17 后端契约监控 + **§18 客户协作框架（v0.6 NEW）+ §19 测试基础设施（v0.6 NEW）**
- 立项 gate **G1-G11**（v0.6 加 G11 客户机可装 unpacked 扩展）
- spike 验收 **18 项**（v0.6 加 #17 性能 / #18 多 window）
- 等 challenges v0.0.6 推动剩余 10 个未深挖领域
- 等 challenges v0.0.5 推动剩余 10 个未深挖领域
- 等 challenges v0.0.4 推动剩余 8 个未深挖领域（CSP / Web Store policy / 多 tab / GDPR 边界 / rollback 细化 / 运营成本 / Veo 后端契约监控 / IME 多语言）
