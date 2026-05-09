---
版本: 0.0.5
日期: 2026-05-10
状态: v0.0.5 第五轮 challenge（针对 design v0.5 inline 集成版 + unpacked-only 约束）
针对文档: docs/v2-architecture-design.md (草案 v0.5 含 v0.0.1-v0.0.4 inline 集成 + unpacked-only 反转)
作者角色: 资深架构师 / V1 实战 6 轮迭代后回头质疑
变更日志:
  - v0.0.6 (2026-05-10): **防过拟合**收紧到 5 条（1 Meta + 1 Blocker + 2 Major + 1 Minor）。重点：65 条后进入 diminishing returns / Google TOS 合规法律风险 / 架构层 stop-loss criteria / V1+V2 双轨运维长期化 / Co-Pilot ↔ 扩展启动顺序 race。**显式 skip** v0.0.5 末尾 10 项中的 7 项（实施细节 / 已有方案 / V3 scope / 业务问题）
  - v0.0.5 (2026-05-10): 新增 13 条（2 Blocker + 6 Major + 5 Minor）。重点：客户 PoC 参与机制 / chrome enterprise GPO 阻塞 / 测试基础设施零 / 多 chrome window 同 profile / 客户机 WS 网络稳定性 / cancel_task 彻底性 + Veo 配额浪费 / 扩展性能开销 / spike 5-7 天时间表不现实 / 运营成本 / i18n 矩阵 / mp4 codec / 误操作自愈 / 数据备份
  - v0.0.4 修订 (2026-05-09): **架构约束更新** — 用户明确不上 Chrome Web Store。C-042 OBSOLETE；C-045/C-048/C-050 缩减；C-008/C-021 风险加重；新增 C-054/C-055（unpacked-only 单点故障 + 版本一致性管理）
  - v0.0.4 (2026-05-09): 新增 12 条（2 Blocker + 5 Major + 5 Minor）。重点：CSP 合规 / Chrome Web Store 自动化 Google 服务下架风险 / 多 tab + Flow tab 定位 / GDPR vs cloudflared 矛盾 / V2.x 内部 rollback / SQLite 并发承载 / Veo 后端契约变更监控 / 扩展自身 update race / NTFS 文件名限制
  - v0.0.3 (2026-05-09): 新增 14 条（2 Blocker + 6 Major + 6 Minor）。重点：license 模型 V2 失效 / 账号信誉双解释悖论 / 扩展生态冲突 / chrome sync 泄露 / 版本矩阵升级 / V1 fragility 回归集 / Win service 权限 / reconnect storm / power+AV+i18n+rollback 一组 minor
  - v0.0.2 (2026-05-09): 新增 13 条（1 meta + 2 Blocker + 5 Major + 5 Minor）。重点：design v0.2 §11 响应没回写正文 / 安全模型 / V1→V2 迁移 / WS_id 绑定 / Veo 幂等 / chrome 升级摩擦 / 可观测性
  - v0.0.1 (2026-05-09): 初版 13 条（4 Blocker + 5 Major + 4 Minor）
---

# Flow Harvester V2 架构 Challenge 文档

本文档对 `docs/v2-architecture-design.md` 进行**架构层面的 challenge**，目标不是给答案，而是逼出"还没想清楚的地方"。每条 challenge 包含编号、针对章节、问题、依据、影响、建议方向。

## 严重度分级

- **Blocker**：不解决就不能进入 V2 spike
- **Major**：spike 阶段可以接受悬而未决，但 release 前必须有结论
- **Minor**：迭代过程中正常处理即可

---

# v0.0.1 — 第一轮 challenge

## 一、Blocker

### C-001 [Blocker] Manifest v3 service worker hibernate 对长任务的致命影响被低估

- **针对**：第 4.2 "manifest v3 关键配置" + 第 6.2 风险登记册第 1 行
- **问题描述**：
  设计稿说"`chrome.alarms` 30s ping + WS keepalive"就能解决 service worker 5 分钟 idle hibernate。但 V1 实测 Veo 单 round 生成 mp4 通常 60-120s，**multi-round + Frames 模式可能单任务跑 5-15 分钟**。设计稿没说清楚：
  1. service worker hibernate 后 WebSocket 状态是否保留？答案：**不保留**——chrome 会关闭所有 sw 持有的连接。重新唤醒后要重连，期间中控派的 task 会丢。
  2. content script 不在 sw 里跑（content script 跟 page lifecycle 绑定），但 content script ↔ service worker 之间的 `chrome.runtime.sendMessage` 在 sw hibernate 后**会失败**。content script 收到 Flow 生成完成事件却没人接，怎么办？
  3. `chrome.alarms` 最小周期 chrome 117+ 是 30s，老版本是 1min。30s 真的够吗？sw 必须每 30s 内做"实际工作"才不被回收，光 ping 不够——**chrome 检测的是 event loop 活跃度**，不是定时器存在。
- **依据**：
  - Chrome MV3 spec：service worker idle timeout 是 30s（不是 5min！设计稿写错了），event 触发后从睡眠恢复
  - V1 worker.flow_playwright 单 round 实测 60-120s（memory: project_known_fragility 第 8 条 "Veo poster image 早出现，mp4 60-90s 后挂"）
  - manifest v3 实战教训：长任务在 sw 必须用 `chrome.alarms` + 持久化状态到 `chrome.storage`
- **影响**：
  - 任务到一半 sw 被回收 → WebSocket 断 → 中控以为工位 offline → flip 状态 → 重派给别人或 mark failed
  - 比 V1 的 patchright crash 还糟：V1 是进程级失败，状态机能恢复；V2 是"任务在 chrome 还在跑，但中控失联"，状态不一致
  - 客户操作员看到"任务 60% 进度突然消失重新开始"会困惑
- **建议方向**：
  1. **不要把 task lifecycle 状态放在 sw 内存**。所有进度必须每步落 `chrome.storage.local` + 重连后从 storage 恢复
  2. content script 持有 task 主导权，sw 只是消息中转。content script 跟 page 绑定，page 不关就一直在
  3. 中控对 WS 断连不要立刻 flip 工位状态——给 60-120s grace period（sw 重连窗口）
  4. spike 阶段必须实测：跑 10 分钟任务，强制 sw hibernate（chrome devtools "stop" service worker），看任务能否恢复

### C-002 [Blocker] "扩展不依赖文本"的核心假设没被实证，可能是"换个层面同样的问题"

- **针对**：第 4.2 "为什么扩展不依赖文本" + 第 2.2 解决的根本问题表的"Locale"行
- **问题描述**：
  设计稿列了 4 条"locale-independent"路径：React fiber / MutationObserver 找 props / 拦截 fetch+XHR / data-attribute。但**这 4 条全部是 unproven 假设**：
  1. **React fiber 读组件状态**：Google 用 production React build，组件名全 minify（`<a23>`、`<n7>`），props 名也压缩。设计稿没证明能从 minify 后的 fiber 树里稳定定位"upload first frame button"组件
  2. **`__REACT_DEVTOOLS_GLOBAL_HOOK__`**：production build **默认不暴露**这个 hook（除非用户装了 React DevTools 扩展）。Google labs.google 大概率是 production build
  3. **拦截 fetch / XHR**：Veo 后端 API endpoint 也会 minify + 频繁改路径（V1 没观察过 API 层稳定性，因为我们一直在 DOM 层做）。"看 API 比看 DOM 可靠"是想当然
  4. **data-attribute**：设计稿假设 Flow 有 data-testid 之类的稳定标识，但 V1 6 轮迭代撞过的"text-based selector drift"本质就是 Google **没**给我们 data-testid（如果有早就用了）。**没有证据 Flow UI 上有 locale-independent 的稳定 hook**
- **依据**：
  - V1 v0.0.4 的 13 语言列表恰恰是因为找不到 locale-independent 锚点才退回多语言文本
  - memory project_locale_strategy: "Tier 1 操作员切英文是真正解决方案"——本质承认了"找不到 locale-independent 路径"
  - V1 所有 selector（prompt-attach / generate / popup-dismiss）全是 text-based，没一个是 attribute-based。**这是 Google 主动决策的结果**，不是 patchright 的限制
- **影响**：
  - 如果扩展也只能靠"找按钮文本"，那扩展跟 patchright 的 selector drift 痛点是**完全一样的**——只是从 Python 端换到 TS 端
  - 设计稿"V2 不需要 locale 处理"的核心承诺会**直接破产**
  - 6 轮 V1 selector 修复经验在 V2 全部白做，要在 TS 重新踩一遍
- **建议方向**：
  1. **spike 必须包含一项 locale 验证**：用越南语账号跑 Flow，写 50 行 content script 试图不依赖文本找 upload / generate 按钮。能找到才进 V2，找不到就承认 V2 仍要做 locale 层
  2. 即使找不到 attribute 锚点，扩展仍有一个 patchright 没有的优势：**MutationObserver 看页面状态机比 polling 可靠**——可以利用，但不能宣称"不依赖文本"
  3. 退一步方案：扩展一旦定位失败就**自动截图 + 上报中控 + 暂停工位**，操作员介入。比 V1 的"selector drift = 全员 fail"好

### C-003 [Blocker] 第 9 节"待澄清问题"里的"客户接不接受 chrome 必须开着"是 V2 立项前置条件，不是 V2 实施细节

- **针对**：第 5.1 "操作员日常流程" + 第 9 待澄清问题第 1 项 + 第 3.1 V2.0 部署形态
- **问题描述**：
  设计稿说"操作员可以最小化 Chrome（不要关）"，但**这是 V2 跟 V1 最大的 UX 退化**：
  - V1 patchright 是后台进程，操作员可以做别的事 / 关机睡觉 / 周末不在
  - V2 要求 chrome 持续打开 5 个 profile 窗口（5 个独立 chrome 进程）。操作员的日常浏览器不能正常用——不能"清掉所有 tab"、不能"重启 chrome 解决卡顿"、不能"切到 Edge / Firefox 测网页"
  - 这是个**产品决策**，不是技术决策。但设计稿把它当作 V2 实施过程中的"待澄清"，而不是 V2 立项的**否决项**
- **依据**：
  - V1 客户工作流：白天看 dashboard，patchright 在后台跑；客户能用自己的 chrome 做别的事（memory: project_v1_delivery）
  - 实测内存：5 个 profile + 多个 tab × 500MB-1GB ≈ 4-7 GB 仅 chrome；客户 PC 可能只有 8-16 GB
  - 操作员误关 chrome / chrome 自动 update 重启 = 所有任务停摆，没有恢复机制
- **影响**：
  - 客户拒绝 V2 整体方案（合理后果）
  - 客户接受但实际使用中频繁踩坑（误关 / 卡顿 / 内存不足），口碑崩
  - V2 上线后才发现要回退到"独立 chrome 实例"模式（其实就是变种 patchright），白做 4-6 周
- **建议方向**：
  1. **本周内必须问客户**：你们能接受白天 chrome 必须开着 5 个 profile 窗口吗？
  2. 备选 fallback：用 chrome 命令行 `--user-data-dir=...` 启动**独立 chrome 实例**（不是日常 chrome），客户日常 chrome 不受影响。但这样就失去"真实使用历史 + 真实信誉"优势——本质退化成 patchright 没用 webdriver 版本
  3. 如果客户拒绝，V2 应改为：扩展不动，但用 chrome `--app=...` 模式 + Co-Pilot 自启 + 操作员看不到 chrome 窗口（藏起来）。这又变成 patchright 风格，但比 patchright 更隐蔽（无 CDP）。**这条路要在设计稿里讨论**

### C-004 [Blocker] 第 7 节验收标准 ≥85% 没有可执行定义，跟 V1 70% baseline 不可比

- **针对**：第 7 验收标准第 6 项 "60 个任务连续跑成功率 ≥85%（V1 v0.1.0 baseline ~70%）"
- **问题描述**：
  - "60 个任务"什么任务？video / Frames？多 round？哪些 SKU / prompt？
  - "连续跑"在哪台机器、哪个网络、几个账号？
  - "成功率"分母是任务数还是 video 数？任务有 8 个 video 但出 6 个算成功还是失败？
  - V1 70% baseline 来自哪里？memory 里只有 "12-15 mp4/session 后 ban" 的实测，没有 "60 任务 / 70%" 的数据
  - 没有控制变量的"提升 15 个百分点"是统计噪音范围，没法验收
- **依据**：
  - memory project_known_fragility 第 60 行 "实测产能：3 账号 stagger=60s 并行 → 45 mp4 in ~60min"——这是产能数据不是成功率
  - V1 v0.0.1→v0.1.0 6 轮迭代，每轮成功率提升幅度都没量化记录
  - 客户 reproduction 是 case-by-case，没建立 reproducible 任务集
- **影响**：
  - V2 跑出来"看起来比 V1 好"但说不清楚到底好多少，扯皮
  - 客户问"为什么我升级 V2 还是 fail"，没有 baseline 比较口径
  - 万一 V2 成功率没 V1 高（合理可能：扩展层 selector drift + chrome 必须开），决策"回退还是继续"无依据
- **建议方向**：
  1. spike 阶段先建 **V1 v0.1.0 测试基线**：固定任务集（10 任务 × 8 video，3 账号 stagger=60s，记录每个 video 的失败原因分布），跑 3 次，求平均
  2. V2 验收用**同样测试基线**跑，对比项不止成功率：
     - 端到端时长（V2 应该更短，因为没 patchright crash）
     - 失败原因分布（V2 应该消除 selector drift / locale 类）
     - 操作员介入次数（V2 应该更少）
  3. 验收报告必须列控制变量（chrome 版本、账号、网络、IP）

## 二、Major

### C-005 [Major] WebSocket 二进制传 50-200 MB mp4 的可靠性 / 内存压力没评估（方案 C 最终决定的依据薄）

- **针对**：第 4.3 文件传输方案 C "选 C：跟 V1 一致，运维更简单"
- **问题描述**：
  设计稿选方案 C（扩展 POST mp4 二进制给中控），但理由只一句"跟 V1 一致"——而 V1 的 download_candidate 是 **patchright 在 Python 端直接 fetch 视频 URL**，没有 chrome→localhost 的二进制传输路径。方案 C 实际是 V2 全新引入的：
  1. mp4 单文件 50-200 MB（Veo 默认 8s @ 1080p ≈ 30-50 MB；longer / higher-res 可能 200 MB+）
  2. WebSocket message size 限制：FastAPI 默认 max_size=16 MiB，Starlette 内部 buffer 全部读到内存才 dispatch；扩展端 chrome MV3 sw 内存上限较紧
  3. 一次任务可能产出 8 video，连续 8 次 200 MB 上传 → service worker 内存压力
  4. WebSocket 二进制 frame 失败要重传整个文件（没有 HTTP range / resume）
- **依据**：
  - FastAPI WebSocket 实测：> 16 MiB 默认会断开
  - Chrome service worker memory：MV3 没明确限制但实测 100-200 MB 后会被终止
  - V1 是 patchright Python 进程读，跟 chrome 内存分开
- **影响**：
  - 大 mp4 上传失败 → 任务 mark failed 但视频实际生成成功了 → DB / 实际产出不一致（V1 已经踩过 silent failure 坑，详见 memory project_known_fragility 第 25-28 条）
  - sw 因内存被终止 → 任务中断 → 反复 retry → 重复消耗 Veo 配额
- **建议方向**：
  1. 重新评估方案 A：让 chrome 下载到 Co-Pilot 指定目录。chrome.downloads API 允许相对路径（chrome 默认下载目录子路径），可以用 `filename: "FlowHarvester/output/.../foo.mp4"` 落到固定子目录，Co-Pilot 监听 watch 该目录
  2. 如果坚持方案 C，必须切到 HTTP POST（不是 WebSocket）：扩展用 `chrome.downloads.download` 拿到 blob URL → fetch 拿 blob → chunked POST 到 `http://localhost:8080/upload` → 服务端用 Starlette `UploadFile` streaming
  3. spike 阶段实测一次 200 MB 文件传输，验证内存占用和稳定性

### C-006 [Major] 第 6.1 估时 4-6 周覆盖 V1 6 轮迭代踩出来的所有 edge case 不现实

- **针对**：第 6.1 milestone 表 "扩展核心 1-2 周" + "Feature parity 1 周"
- **问题描述**：
  V1 v0.0.1 → v0.1.0 跑了 3 天 6 个版本，但每个版本背后是**前期数周的客户复现 + log 分析**才发现 edge case。V2 列了"扩展核心 1-2 周"覆盖：
  - 上传 first_frame / last_frame / reference image（含 prompt-attach `<img>` click strategy，V1 试了 4 种才稳定）
  - 输 prompt（V1 用 `keyboard.type` 逐字符 60-110ms delay，否则 Veo 报"Failed, oops something went wrong"）
  - 点 Create / 选 mode preset / 选模型
  - multi-round 状态恢复 + `inter_round_pause_sec` gate
  - 等待 mp4 生成（含 stale Failed card phrase / poster early-exit / UUID 去重）
  - 下载 mp4
  - 错误分流：unusual_activity / no_flow_access / service_unavailable / generation_failed / audio_failure（5 种 phrase 状态机）
  - silent failure 加固（download 写文件 try/except + size verify）
  - SPA 路由 page.url 不更新（V1 用 3 路并行检测）
  - claim 守卫（必须过滤 flow_project_url）
  - Strike 系统集成
  - Frames 模式 vs Ingredients 模式
  - aspect / output_count / duration / model 参数注入

  这些是 V1 6 轮迭代积累的，**每一个都是踩坑后才加的**。V2 不可能凭空 1-2 周全覆盖，多半要再走一遍 6 轮迭代
- **依据**：
  - memory project_known_fragility 列了 35 条已知 fragility，V2 大部分要重新实现
  - V1 worker.flow_playwright 单文件 1900 行，全是这些 edge case 的处理代码
  - V2 列的"删除 ~2400 vs 新增 ~1000"假设了"扩展端实现简单 1.5 倍"——没有依据
- **影响**：
  - 4-6 周变 12-16 周（更现实）
  - 客户期待 V2 "1 周内迁移"在不现实时间表上做承诺
  - 中途发现踩不完坑就 release，客户接到一堆退化的 V2
- **建议方向**：
  1. 拆 V2 milestone：先做 **MVP**（单 task / 单 round / video 模式 / 不处理错误），再做 feature parity。MVP 1-2 周可行
  2. **edge case 移植清单**：把 35 条 fragility 逐条评估"V2 是否仍存在 / 用什么机制处理"，给每条排工时
  3. **不要承诺 V2.0 完全 feature parity** —— 接受 V2.0 比 V1 v0.1.0 功能少（比如先不做 Frames / 不做 multi-round），跑稳后再补
  4. 估时改 **8-12 周**（含 MVP 2 周 + feature parity 4-6 周 + 内测迁移 2-4 周）

### C-007 [Major] 多 chrome profile × 5 个 = 客户 PC 内存 / CPU 评估缺失

- **针对**：第 5.4 多账号管理 + 第 9 待澄清问题第 2 项
- **问题描述**：
  - 5 个 chrome profile 各开一个独立 chrome 窗口 = 5 个 chrome 进程组（每个有 main + renderer + GPU + utility，~10+ subprocess）
  - 每个 profile chrome 内存 500 MB-1 GB（无 tab 时 300 MB，每个 tab 100-300 MB）
  - 客户 PC 配置未知（V1 客户已知是 Win11，但 RAM 8 / 16 / 32 GB 没记录）
  - V1 patchright headed 是 1 个 chrome 进程，5 个 WS 串行 stagger，**不会 5 个 chrome 同时跑**
- **依据**：
  - 实测 chrome × 5 profile × 1 tab on M1 Mac：6.8 GB RAM
  - V1 customer Win11 PC 内存未在 memory 中记录
  - 客户场景"5 个账号"不一定 = 5 个 chrome 同时活跃；可能 staggered，但 V2 设计是 5 个长开
- **影响**：
  - 客户 PC RAM 16 GB → chrome 占 7 GB + Co-Pilot 1 GB + Win11 系统 4 GB = 还剩 4 GB 给客户日常使用，体验差
  - chrome OOM crash → 任务停 → 跟 C-001 串联（任务恢复机制脆弱）
  - 客户 PC 8 GB RAM 直接跑不动 5 个 profile
- **建议方向**：
  1. 立项前问客户 PC RAM
  2. 不要假设 5 个 profile 都同时活跃。**stagger 跑**：中控调度时只让 N 个工位同时 active（N 可配，默认 2-3），其他 profile chrome 可以 minimize 或 chrome 自己 throttle 后台 tab
  3. 提供"轻量模式":每次只开 1 个 profile，跑完关掉切下一个。失去并行优势但内存友好
  4. spike 阶段必须实测客户机 5 profile 并发的稳定性

### C-008 [Major] chrome 自动 update 会 disable 开发者模式扩展 + 客户安装路径假设过于乐观

- **针对**：第 5.3 安装路径 + 第 6.2 风险登记册"扩展更新困难（不上 store） 低/低"
- **问题描述**：
  - chrome 88+ 起，每次启动都会显示"You are using extensions that are not from the Web Store"警告 banner，要求用户每次确认
  - chrome stable 主版本升级（每 4 周）有时会**禁用所有 unpacked 扩展**，要求用户重新点"加载已解压"
  - 设计稿 5 步安装："开发者模式 → 加载已解压 → 选 extension/ 目录"——每个 V1 客户都要做这事，且每次 chrome update 后可能要重做
  - 风险登记册评 "更新困难 低/低" 严重低估
- **依据**：
  - chrome enterprise policy `ExtensionInstallSources` 可以白名单本地目录，但客户是 Win11 个人版没企业策略
  - 第三方 chrome 扩展（VEO Automation 等）大多数选择上 Chrome Web Store 而不是 unpacked，正是为了避免这个
  - V1 客户已经能力有限（不能跑 CLI / 不能编辑 yaml，memory: project_v1_delivery），unpacked 重装步骤是新负担
- **影响**：
  - 客户 chrome update 后 V2 整套不工作，客户不知道为什么
  - 客户被警告 banner 烦到关掉扩展
  - 客户每个新 profile 都要装一遍扩展（设计稿说"unpacked 一次性所有 profile 共享"——**错的**，chrome 是 per-profile 安装）
- **建议方向**：
  1. **必须重新评估上 Chrome Web Store**。设计稿说"审核大概率拒"——但实际上有 VEO Automation / Auto Flow Pro / FlowForge Pro 通过审核。不一定会被拒
  2. 退一步：注册 Chrome Enterprise（免费）→ 用 ExtensionInstallSources policy 白名单本地路径。需要客户机做 registry / GPO 配置，但一次配置永久有效
  3. 退两步：用 .crx 自签名 + Edge / Brave 浏览器（chromium-based 但更宽松）。客户体验有损但可控
  4. 验证 "unpacked 一次性所有 profile 共享" —— 实测应该是 per-profile 独立安装

### C-009 [Major] 操作员误关 chrome / chrome crash 的任务恢复设计完全缺失

- **针对**：第 5.1 操作员日常流程 + 第 6.2 风险登记册 "客户 chrome 关了 = 任务停"
- **问题描述**：
  风险登记册写"中控 detect 失联 → flip ws online→offline → 不再派 task；恢复时操作员再开 chrome"——这只覆盖了"未来不再派 task"，**没覆盖"任务正在跑中途 chrome 关闭"的恢复**：
  - 任务跑到 multi-round 第 5/8 轮，chrome 被关
  - 已生成的 video 已下载到 chrome 默认目录（方案 A/B）或已 POST 给中控（方案 C 部分成功）
  - DB 里 task status='running'，session_round_count=5，generation_round 已写入 5 条 task_results
  - 操作员重开 chrome → 扩展重连 → 中控看到 ws 重新 online
  - **接下来怎么办？继续从 round 6 跑？还是从头跑？还是 mark failed？**
  - 如果从 round 6 跑，扩展怎么知道"上次是哪个 Flow project 的哪个 prompt"？session 状态在哪？
- **依据**：
  - V1 有 `reset_zombie_state_on_startup`（memory: project_known_fragility 第 22 条），但那是 patchright 进程重启场景，状态机更简单
  - V1 task 用 `MAX(generation_round) FROM task_results WHERE task_id=?` 作权威源恢复（memory 第 21 条），V2 仍能用，但 V2 多了"扩展跟 chrome 状态"维度
  - V1 download 期间崩溃靠 silent failure 加固（memory 第 25-26 条）兜底
- **影响**：
  - 任务半完成状态混乱，DB 不一致
  - 操作员日常会误关 chrome（这是已知行为），每次发生都需要人工排查
  - 严重时可能出现"重复生成"消耗账号配额或"漏生成"客户少交付
- **建议方向**：
  1. **明确 task lifecycle 状态机**：在哪些状态点持久化（chrome.storage + Co-Pilot DB 双写）
  2. 扩展 reconnect 时主动跟中控**对账**：上次跑到哪一步、最后一个产物是什么。用 task_id + round 作 idempotency key
  3. 中控提供 "task resume" 接口：扩展重连后查 `MAX(generation_round)` + 当前 Flow project URL，从 round N+1 恢复
  4. 极端情况（operator 关 chrome 30+ 分钟）→ 任务自动 mark `interrupted`，等操作员手动 resume 或 retry

## 三、Minor

### C-010 [Minor] 第 4.1 "净减少 ~2400 行 vs 新增 ~1000 行" 估算误导

- **针对**：第 4.1 中控改动范围 + memory project_v2_architecture 同样数据
- **问题描述**：删 Python 1900 行 + 新增 1000 行 Python + 新增 **TypeScript 扩展代码（设计稿没估）**。扩展代码至少要 2000-3000 行 TS（content script DOM 操作 + state machine + WS client + storage + popup + side panel + options）。"净减少代码"是错觉
- **依据**：V1 worker.flow_playwright 1900 行已经是 V1 6 轮迭代后的最简化形态；V2 扩展要做同样事 + 多语言 / locale 处理（如果 C-002 验证下来还要做）
- **影响**：误导维护成本评估
- **建议方向**：诚实记录"代码量基本持平或略增，但维护性更好（TS type / hot reload / 客户端 debugability）"

### C-011 [Minor] WebSocket message schema 漏了关键字段

- **针对**：第 4.3 消息 schema
- **问题描述**：
  - `task_assign` 缺 `target_count`（V1 任务有 target_video_count，影响 multi-round 早停判断）
  - `task_assign` 缺 `inter_round_pause_sec` / `stagger` 等动态参数（V1 settings.yaml 有客户 override）
  - `task_progress` 缺 `flow_project_url`（V1 SPA 切 project 后 URL 变化是状态变化的关键信号）
  - `task_error` `error_type` 是 free string，应该是 enum（unusual_activity / no_flow_access / service_unavailable / generation_failed / audio_failure / locale_drift / extension_crash / timeout）
  - 没有 `screenshot_request`（中控主动让扩展截图，调试用）
- **影响**：协议 v1 后续要打 patch 加字段，跟 V1 一样要兼容老扩展
- **建议方向**：spike 阶段从 V1 task lifecycle 反推所有需要传的字段，定 v1 schema 时一次性放足

### C-012 [Minor] 第 4.3 文件传输 "扩展 fetch http://localhost:8080/files/<rel_path>" 安全边界

- **针对**：第 4.3 "扩展的 content script 直接 fetch ..."
- **问题描述**：
  - `host_permissions` 只声明了 `https://labs.google/*` 和 `https://*.google.com/*`，没有 `http://localhost/*`
  - manifest v3 要求 host_permissions 显式列 localhost，否则 fetch 会被 CORS 拦
  - content script 跨 origin fetch 也要 manifest 声明
- **影响**：spike 阶段会撞 CORS 错误才发现，浪费时间
- **建议方向**：manifest 加 `"host_permissions": ["http://localhost:8080/*", "https://labs.google/*", ...]`

### C-013 [Minor] "操作员要保持 chrome 开（trade-off）" 的 trade-off 没量化

- **针对**：第 5.2 跟 V1 对比表最后一行
- **问题描述**：表格用"trade-off"模糊带过，但实际 trade-off 内容（chrome 不能关 / 内存 / 误操作）没列。这跟 C-003 串联——客户没看见明确成本就同意了，事后反悔
- **建议方向**：列出明确 trade-off 项："chrome 必须保持运行 / 占用内存 X-Y GB / 不能日常切换浏览器 / chrome 自动 update 后扩展可能要重新加载"

---

# v0.0.2 — 第二轮 challenge（针对 design v0.2 含 §11 响应）

## 0. Meta-challenge

### C-014 [Blocker] design v0.2 §11 "Challenge 响应"未真正集成到设计正文，文档自相矛盾

- **针对**：design v0.2 整篇 — §11 "Challenge 响应"段集中写了 "接受 / 完全接受"，但**正文章节没改动**。结果是"读者读 §3-§9 拿到的还是 v0.1 错误信息，只有读到 §11 才知道架构师其实改了主意"
- **问题描述**：
  v0.0.1 提了 13 条，v0.2 §11 一一回应"接受 / 部分接受"。但**正文 10 处该改的没改**：

  | v0.0.1 challenge | §11 承诺 | 正文实际状态 |
  |---|---|---|
  | C-001 sw hibernate → §4.2 加 task lifecycle 持久化 | "已加" | §4.2 内部分工表完全没变，仍写 "service worker; chrome.alarms 30s keepalive 防 hibernate" |
  | C-002 不依赖文本是假设 → 改写 §4.2 | "改写为路径假设" | §4.2 "为什么扩展不依赖文本"4 条仍是断言句式 |
  | C-003 客户 chrome 长开 → §3.1 加 fallback B | "已加" | §3.1 V2.0 部署形态没出现 fallback B |
  | C-004 验收 ≥85% 不可执行 → §7 重写 | "已重写" | §7 验收标准 7 条原文未动，仍写"60 个任务 / ≥85%（V1 70% baseline）" |
  | C-005 方案 C 风险 → 默认改方案 A | "默认改 A" | §4.3 末尾仍写"**选 C**：跟 V1 一致，运维更简单" |
  | C-006 4-6 周不现实 → 8-12 周 | "重排" | §6.1 milestone 表仍 7 行加总 "4-6 周" |
  | C-007 5 profile RAM 没评估 → 默认 N=2-3 | "已改" | §5.4 仍写"启动每个 profile 的 chrome 窗口 → 扩展自动注册不同 ws_id"，没有 N 限制 |
  | C-008 chrome update disable unpacked → 升风险高/高 | "已升级" | §6.2 风险登记册仍写"扩展更新困难 低/低" |
  | C-009 任务恢复 → 新增 §4.4 | "见下面 §4.4 update" | §4.4 章节根本不存在；§4 跳过 4.4 直接到 §5 |
  | C-010 代码量误导 → 改成"持平或略增" | "诚实记录" | §4.1 仍写"净减少 ~2400 行 vs 新增 ~1000 行" |

- **依据**：直接读 design v0.2 行 1-600 比对 §11 行 603-779
- **影响**：
  - 协作性破坏：实施工程师看 §3-§9 写代码，会按 v0.1 的错误前提开工。读到 §11 才发现"哦我做错了"
  - 客户读 design 不会读到 §11（架构师的内部纠错段），看到 §3.1 "完全私有，符合 V1 客户期望"会以为产品侧没风险
  - **§11 不是设计修订**，是"我承认你说的对"的辩白稿。架构层 spike 决策没法基于这个文档做
  - 这同时暴露了一个流程问题：challenges → response 应该是**正文 inline edit**，response 段只放"v0.0.1 全部已 inline 集成，参见 §X.Y"
- **建议方向**：
  1. **design v0.3 强制规则**：§11 的每条响应必须直接 inline 到对应正文章节；§11 段保留索引（"C-001 已 inline 到 §4.2 / §4.4"）
  2. v0.3 必须新增的章节：§3.1 fallback B、§4.4 任务生命周期 + 恢复设计、§4.5 spike 协议、§12 V1→V2 迁移策略
  3. v0.3 必须修订的章节：§4.1 代码量数字、§4.2 内部分工 / 不依赖文本论述、§4.3 文件传输方案选 A、§5.4 多账号 N 限制、§6.1 milestone 8-12 周、§6.2 风险登记册扩展更新困难升级、§7 验收标准重写
  4. v0.3 仍未完成的项必须显式列在正文章节顶部 "⚠️ 待 spike 决定" 而不是塞进 §11

## 一、Blocker（v0.0.2 新增）

### C-015 [Blocker] V2 安全模型完全空白：扩展 = 客户全 Google 账号攻击面 + WS 端口无认证 + license 模型实质失效

- **针对**：第 4.2 扩展 manifest（host_permissions） + 第 4.3 WebSocket 端点 + design 整体未提威胁模型 + memory: feature_license
- **问题描述**：
  V1 是 patchright 启独立 chrome，沙箱跟客户日常浏览隔离。V2 扩展跑在**客户日常 Google 登录态的 chrome 内**，攻击面全面扩大，但设计稿一句话没提：

  **威胁场景 1：扩展权限过宽**
  - manifest 声明 `host_permissions: https://*.google.com/*` —— 这给扩展"读所有 google 服务 cookie / DOM"权限：Gmail / Drive / Pay / Cloud Console / Ads
  - 扩展只需要 `https://labs.google/fx/tools/flow/*`（content script match 也只是这个），无理由开放整个 `*.google.com`
  - 一旦扩展代码被恶意修改（攻击者 / 操作员），可读客户全部 Google 数据

  **威胁场景 2：扩展代码 = 明文资产**
  - 扩展代码是 chrome 解压目录里的 JS 明文文件，`C:\Users\xxx\AppData\Local\Google\Chrome\User Data\Profile X\Extensions\<ext_id>\<version>\`
  - 操作员复制走 → 装到自己的 chrome → 用客户 license 跑（license 只在 Co-Pilot 中控校验，扩展跑在客户机）
  - 第三方拿到扩展代码 → 跑自己的 Co-Pilot 复制品 → license 完全绕过

  **威胁场景 3：localhost WS 无认证**
  - `ws://localhost:8080/ws/extension/<ws_id>` 任何本机进程都能连接
  - 本机恶意软件 / 浏览器 fingerprinting 脚本可以：
    - 假装扩展 register 注册一个 fake ws_id → 偷取 task_assign 拿到 prompt / asset paths
    - 假装中控 push fake task_complete → 让 Co-Pilot DB 记假数据
  - DNS rebinding 攻击：恶意网页用 DNS rebinding 绕过 SOP 直接访问 localhost:8080
  - FastAPI 默认绑定 0.0.0.0 还是 127.0.0.1？设计稿没说

  **威胁场景 4：扩展窃取 cookie 风险**
  - chrome.cookies API 配合 `host_permissions` 可以读所有 google.com cookie，包括 SSO token
  - 如果扩展上传日志到中控（C-017 的合理建议），日志里夹带 cookie 不被发现 → 中控诊断包邮件 → 第三方拿到客户 Google session

- **依据**：
  - manifest v3 best practice：host_permissions 最小化（chrome dev docs）
  - V1 license 文件路径已知（memory: project_v1_delivery），客户机 license.lic 加 patchright 二进制 = 复制门槛较高；V2 扩展 = 拷贝目录就走
  - localhost WS 无认证攻击：chrome 117+ 对 mixed-content 收紧，但本机进程不受限
- **影响**：
  - **合规风险**：扩展拥有客户 Gmail 读权限是潜在的合规雷（GDPR / 客户隐私协议）
  - **license 模型崩塌**：V2 扩展能脱离 license 跑，付费转免费分发
  - **本机进程攻击**：客户机一旦感染恶意软件，攻击者可控整个 V2 流水线
  - **DNS rebinding**：客户访问恶意网页时本机 Co-Pilot 被远程操作
- **建议方向**：
  1. **威胁模型先写在设计稿**（新章节 §13 安全模型）：定 trust boundary（客户机 / 操作员 / 网络 / Google），列每个 boundary 的攻击面 + 缓解
  2. **manifest 最小权限**：
     - host_permissions 只列 `https://labs.google/fx/tools/flow/*` 不要 `*.google.com/*`
     - 不要 chrome.cookies / chrome.identity / chrome.history 这种敏感 API（V2 不需要）
     - content_scripts match 也限定在 `/fx/tools/flow/*`
  3. **WS 端口认证**：
     - Co-Pilot 启动时生成 secret token 写到 `%APPDATA%\FlowHarvester\ws_token`
     - 扩展 options 页让操作员粘贴 token → 存 chrome.storage.local
     - WS register 消息必须带 token，server 端 reject 无 token / 错 token
     - FastAPI 强制 bind 127.0.0.1（不是 0.0.0.0）
     - WS endpoint 校验 Origin header（chrome 扩展会发送 `chrome-extension://<id>` Origin）
  4. **扩展防复制**：
     - 扩展 init 时跟 Co-Pilot 校验 license（扩展拿到 license 签名 challenge → Co-Pilot 用 license server 公钥验证）
     - 扩展代码混淆 + 关键 selector 字典加密（不是反工程，是抬高门槛）
     - license 包含"绑定 customer_id" → Co-Pilot 跟扩展握手时校验
  5. **DNS rebinding 防御**：FastAPI 加 Host header 白名单（只接受 `localhost` / `127.0.0.1`，拒绝任意域名解析到 127.0.0.1 的请求）
  6. **日志脱敏**：诊断包 zip 前过滤 cookie / token / email
  7. **spike 必须包含一个安全 review**：列威胁场景，挨个验证缓解措施有效

### C-016 [Blocker] V1→V2 数据迁移路径未设计，"双跑对比"在物理上不可行

- **针对**：第 6.1 milestone 第 6 阶段 "v0.1.0 客户切到 V2 双跑对比稳定性 1-2 周" + design 整体未提迁移策略
- **问题描述**：
  设计稿 §6.1 把"双跑"作为 V2 release 前的验证手段（1-2 周），但**物理上跑不通**：

  **冲突 1：chrome profile 互斥**
  - V1 patchright 启 chrome 用 `--user-data-dir=<patchright_profile_path>`，V1 自管 profile
  - V2 扩展跑在客户日常 chrome profile（设计稿核心论点：账号信誉来自真实使用）
  - 同一 Google 账号同时登在 V1 patchright profile 和 V2 客户日常 profile —— Google 风控会立刻识别"同账号 2 个会话不同 device fingerprint" → 双方都触发 unusual_activity
  - 物理 fix：双跑必须用**不同账号集** —— 但客户没那么多账号余量

  **冲突 2：DB schema 共用还是分开？**
  - V1 SQLite schema：workstations 表有 `chrome_profile_path` / `login_session_status` / `login_state` 字段，V2 扩展自管登录态这些字段失去意义
  - V1 schema 字段不能直接 DROP（SQLite 不支持 ALTER DROP COLUMN，要 rebuild table）
  - 如果 V1 V2 共享 DB：V2 写 task_results 会污染 V1 任务表的统计；V1 写 workstation status 会让 V2 认为 ws 不可用
  - 如果 V1 V2 分开 DB：客户机 2 份数据，统计 / 报表无法跨 DB 关联，"对比"等于零
  - V1 settings.yaml workstations 段引用 chrome_profile_path —— V2 不再用，怎么处理？

  **冲突 3：单机端口冲突**
  - V1 dashboard `http://localhost:8080`，V2 dashboard 也 `http://localhost:8080`（设计稿明确写）
  - 双跑必须改 V2 端口到 8081 → 但扩展 manifest host_permissions 写死端口；改端口要重打包扩展 → 客户每次 V1 V2 切要重装

  **冲突 4：操作员能力**
  - V1 客户能力有限（不能 CLI / 不能编辑 yaml，memory: project_v1_delivery）
  - 双跑要求操作员同时管 V1 patchright + V2 扩展 + 2 个 dashboard + 2 个 chrome 配置 → 远超能力上限

  **冲突 5：output 目录冲突**
  - V1 output `output/<date>/<sku>/<round>.mp4`，V2 同样路径（设计稿"跟 V1 一致"）
  - 双跑同 SKU 同 round 写同一文件 → 覆盖 → 漏交付

- **依据**：
  - V1 schema 文件路径见 memory: reference_paths
  - SQLite 不支持 ALTER DROP COLUMN（SQLite 3.35+ 才支持，客户机版本未知）
  - V1 workstation 字段已知（memory: project_known_fragility 提到 chrome_profile_path）
  - V1 customer 能力上限见 customer-manual.md / customer-install-windows.md
- **影响**：
  - "双跑 1-2 周对比稳定性" 在交付计划里是空话 → 客户拿 V2 直接全切风险全压
  - 客户切 V2 后想回退 V1（如果 V2 不稳）— 没设计回退路径，DB schema 已被 V2 改 → 一刀切单向门
  - 双跑期间任何冲突冒泡都会被算到"V2 不稳定"头上，V2 评分被低估
- **建议方向**：
  1. **设计 §12 V1→V2 迁移策略章节**，必须包含：
     - 迁移单位：是按账号 / 按 SKU / 按客户机？建议按账号集（先 1-2 个账号试 V2，其他保持 V1）
     - 双跑物理：单机不能双跑 → 用**两台测试机 + 不同账号** 或 **客户机 V1 + 测试机 V2 同任务集分开跑**
     - DB 兼容：V2 schema 必须 backward compatible —— V1 字段保留但 V2 ignore；V2 加新字段 nullable
     - settings.yaml：V2 chrome_profile_path 字段保留但加 deprecated comment；V2 加 `extension_ws_token` 字段
     - output 路径：V2 默认 `output_v2/<date>/<sku>/...` 跟 V1 隔离
     - 端口：V2 默认 8081（V1 长期占 8080）；客户全切后 V2 才改回 8080
     - 回退路径：V2 失败如何 1 步切回 V1（保留 V1 PyInstaller bundle 在客户机）
  2. spike 阶段就建 V1+V2 同 DB 兼容验证：开 V1 跑一轮，开 V2 跑一轮，看 V1 dashboard 是否仍正常显示历史数据
  3. 客户切 V2 之前必须文档说明"如果出问题如何回到 V1"

## 二、Major（v0.0.2 新增）

### C-017 [Major] 可观测性退化：失去 cloudflared = 客户出问题没远程支持渠道，回到 v0.0.1 之前

- **针对**：第 4.1 "cloudflared 依赖 → 移除（V2.0 暂保留诊断包，隧道功能去掉）" + 第 5.2 "调试：DevTools 直连 + 扩展可主动上报"
- **问题描述**：
  V1 v0.0.2 加 cloudflared 是因为 v0.0.1 客户出问题"作者收到诊断包邮件 → 解压 → 看 log → 回邮件"循环耗时 1-3 天。cloudflared 让作者直接进客户 dashboard 实时排查，**问题解决周期从天降到分钟**。设计稿 V2 一笔取消，理由"DevTools 直连"——但：

  - **"DevTools 直连"是误导**。chrome DevTools 默认只能在客户本机 F12 打开，作者远程进不了客户机 chrome。要远程要么：
    - chrome 启动加 `--remote-debugging-port=9222` + 客户开 SSH tunnel + 作者 chrome 输 chrome://inspect → 客户能力上限远超
    - 或客户开 TeamViewer / AnyDesk → 看着作者操作客户 chrome → 客户隐私 + 操作员误操作风险
  - 实际工作流降级到："客户 F12 → 截图 → 邮件给作者 → 作者远程指导 F12 操作 → 反复来回" = **回到 v0.0.1 时代**
  - 扩展端 unhandled exception / sw 崩溃 / WS reconnect 失败这些事件，没有 cloudflared 隧道的话**作者完全看不见**
  - V2 比 V1 多一个 chrome / extension 层，可观测性反而**应该增强**才对，结果是**减弱**

- **依据**：
  - V1 v0.0.2 加 cloudflared 的原因 memory: project_known_fragility（"客户出问题没诊断手段"）
  - cloudflared bundle 体积 25 MB（设计稿 §7 第 7 项明说"可移除 cloudflared 节省 25 MB"）
  - 客户能力上限：不能 CLI / 不能 yaml / 不能配 SSH（memory: project_v1_delivery）
- **影响**：
  - 客户出问题平均解决时间（MTTR）从 V1 v0.0.2 后的 ~小时级 倒退回 ~天级
  - 扩展端 silent failure 完全无可观测（client-side error 不会主动到中控）
  - 客户口碑：V2 比 V1 升级，但出问题反而更难解决 → 客户 LTV 降低
- **建议方向**：
  1. **保留 cloudflared**。25 MB 节省 vs 客户支持成本 → 经济账完全不平衡
  2. **新增"扩展端日志推送"**：扩展所有 console.log / console.error / unhandled exception → push 到中控 WS → 中控持久化到 SQLite forensic_log 表 → 诊断包包含
  3. **中控 dashboard 加"扩展端实时控制台"面板**：作者通过 cloudflared 进 dashboard 看每个 WS 扩展端的 live console（类似 chrome devtools console 但中控聚合）
  4. **诊断包扩展项**：诊断包打包时 zip 包含每个 WS 的扩展端日志快照 + chrome.storage 状态 dump
  5. **远程截图协议**：design §11 C-011 已提"`screenshot_request`" — 中控可主动让任何 WS 扩展截图（包括 5 profile 中任何一个），作为远程排查兜底
  6. **不要因为 25 MB 节省砍掉远程支持渠道**

### C-018 [Major] WS_id ↔ chrome profile 绑定逻辑设计为零，操作员误配 = 任务派给错账号 = 静默灾难

- **针对**：第 4.3 "`<ws_id>` 是工位 id... 扩展启动时从 `chrome.storage.local` 读 + 跟用户绑定的账号关联" + 第 5.4 多账号管理
- **问题描述**：
  设计稿一句话把 ws_id ↔ profile 绑定甩给"chrome.storage.local"，但**没说怎么写进去 / 怎么验证 / 怎么纠正**。这是 V2 操作员日常踩坑频率最高的隐患：

  **不清楚的点 1：初始绑定时机**
  - 扩展 unpacked 安装到所有 profile 是同一份代码（C-008 验证 per-profile 后细节再说），那 chrome.storage.local 是 per-profile 隔离的（chrome 设计如此）
  - 操作员第一次开 Profile A chrome → 扩展 first run → 怎么知道这是 WS_A 还是 WS_B？
  - 设计稿没有 onboarding：扩展弹 popup 让操作员选？让操作员去 options 页输 ws_id？

  **不清楚的点 2：绑定 anchor**
  - 用什么作为 chrome profile 的稳定 ID？
    - chrome profile 路径（`User Data\Profile 5`）：稳定但操作员不可见 / 不可读
    - chrome 主账号 email：可见可读，但操作员可能在同一 profile 切多个账号
    - chrome `Profile.ProfileName`：操作员可改名，不稳定
    - chrome `Profile.ID` (GUID)：内部 ID 操作员不可见
  - 设计稿假设了 email 绑定（§4.3 register message 有 `account_email` 字段），但 §5.4 又是按 profile 创建，两个 anchor 混用

  **不清楚的点 3：误配纠正**
  - 操作员开 Profile A chrome 时手抖在 options 页输 `WS_B` → 中控 register 收到 WS_B from Profile A → 派 WS_B 任务 → 用账号 A 跑 WS_B 的 prompt
  - 中控如何检测这种错配？只看 ws_id 看不出，只看 email 看出来了但策略是什么（拒绝 register / 警告 / 自动 rebind）？
  - 操作员怎么 reset？chrome.storage.local 清不了（操作员不会 chrome devtools）

  **不清楚的点 4：strike 系统跟错账号交互**
  - V1 strike 系统按 workstation_id 累计风险（unusual_activity 撞过几次）
  - V2 如果 WS_A 和 WS_B 误配（实际 Profile A 在干 WS_B 任务），strike 累计错对象 → 错账号被 disable
  - 严重：误配可能"杀错号"

- **依据**：
  - chrome.storage.local 是 per-extension-per-profile 隔离（chrome dev docs）
  - V1 是 patchright 启 profile，workstation_id ↔ profile_path 是中控配置，没有"扩展自己上报"的歧义路径（memory: project_known_fragility）
  - V1 strike 系统按 workstation 累计（memory: project_known_fragility 第 30+ 条）
- **影响**：
  - 操作员日常会误配（5 个 profile × 5 个 ws_id 组合错很容易）
  - 错配是 silent failure：dashboard 显示 5 个 WS online + 任务正常派；只有事后看 task_results 跟账号对不上才发现 → 几小时后才察觉
  - strike 错账号 / 配额错账号都是不可恢复的影响
- **建议方向**：
  1. **绑定 anchor = email + profile path hash 双因子**：
     - 扩展 register message 同时上报 `account_email`（chrome.identity）+ `profile_id_hash`（chrome.runtime.id + chrome.storage.local 里第一次生成的 UUID）
     - 中控 workstations 表加 `expected_email` + `bound_profile_id_hash` 字段
     - register 时校验：email 跟 expected_email 不一致 → reject + 通知操作员
  2. **首次 onboarding wizard**：
     - 扩展 first install 弹 fullscreen 页面：中控生成 setup token → 操作员粘贴 token → 扩展 register 时上报 token + email → 中控用 token 找到 ws_id 然后绑定 email + profile_id_hash
     - 之后该 profile 永远是该 ws_id；切换其他 ws_id 只能在中控解绑后重新做 wizard
  3. **绑定状态可视化**：
     - 扩展 popup 顶部大字显示 "I am Workstation: WS_A (account: xxx@gmail.com)"，操作员肉眼可校
     - 中控 dashboard 工位卡片显示 "expected: xxx@gmail.com / actual: xxx@gmail.com" 不一致红色告警
  4. **error case 处理**：
     - email 改了（操作员在 chrome 里换登录账号）→ 扩展 register 检测到 email mismatch → 暂停发送 task / 告警操作员
     - profile path 改了（chrome 重装 / 客户机迁移）→ profile_id_hash mismatch → 同样告警
  5. **strike 系统硬绑定 email 而不是 ws_id**：strike 累计按 account_email，跟 ws_id 解耦，避免误配杀错号

### C-019 [Major] chrome.downloads 文件命名冲突 + 5 profile 并发写同目录 = silent overwrite 重现 V1 旧坑

- **针对**：第 4.3 文件传输 方案 A "filename: 'output/2026-05-09/.../foo.mp4'" + 任务结果回传
- **问题描述**：
  设计稿假设 chrome.downloads.download 把 mp4 写到 Co-Pilot output 子路径就完事，**但没有处理文件名冲突 + 并发 + 中控对账**：

  - chrome.downloads `FilenameConflictAction` 默认是 `'uniquify'`：file.mp4 已存在自动改 `file (1).mp4` → **扩展不知道最终名字，中控 DB 记的是 file.mp4，实际盘上是 file (1).mp4** → 跟 V1 silent failure 同样症状（memory: project_known_fragility 第 25-28 条）
  - 5 个 profile 并发同时下 `output/2026-05-09/SKU_X/round_1.mp4` → 5 个 chrome 都尝试写 → 互相 uniquify → 5 个文件名 (1) (2) (3) (4)
  - chrome.downloads.download 完成后扩展拿到的 `downloadId` 不直接给最终 filename，要再调 `chrome.downloads.search({id})` 拿到 `filename` 字段
  - 跨 profile 写同目录的 race：chrome 各自检查存在性 → 同时认为不存在 → 同时写 → 实际只一个成功，其他 chrome.downloads 报 conflict 错
  - chrome 安全策略：filename 包含 `..` / 绝对路径会拒绝，子目录最大深度限制（chrome 拒绝过深路径）

- **依据**：
  - chrome.downloads API 文档（uniquify 默认行为）
  - V1 silent failure 教训（memory: project_known_fragility 第 25-28 条 - download 写文件 try/except + size verify）
  - 跨进程文件系统 race condition 是已知模式
- **影响**：
  - mp4 写到错文件名 → DB 记录跟实盘不一致 → 客户日报"产出 X 个"实际盘上 Y 个 → 客户信任崩
  - 漏 video 不被发现：DB 记 task_complete 但实际只有 7 个 video（少 1 个被覆盖了）
  - 严重时跟 C-020 串联：扩展认为下载失败 → retry → 重复消耗 Veo 配额
- **建议方向**：
  1. **文件名强制唯一**：filename = `{workstation_id}_{task_id}_r{round}_v{video_idx}_{ts}.mp4`
     - 包含 ws_id 避免跨 profile 冲突
     - 包含 task_id + round + video_idx 唯一定位
     - 包含 timestamp 避免极端情况（手动 retry 同任务）
  2. **扩展必须 confirm 最终文件名**：
     - chrome.downloads.download → 拿 downloadId
     - chrome.downloads.onChanged 监听 → state 变 'complete' 时 → chrome.downloads.search({id}) → 拿到实际 filename
     - WS push task_complete 时携带实际 filename（不是预期 filename）
  3. **filenameConflictAction 显式设为 `'uniquify'` + 校验**：
     - 接受 chrome 加 (1) 行为，但中控收到的 filename 必须跟预期不一致时告警（说明撞名了 → 配置 bug）
  4. **中控 file 落盘后大小校验**：跟 V1 silent failure 加固一致 — Co-Pilot 收到 task_complete 后 stat filename → 拿到大小 → 跟 task_progress 阶段扩展上报的预期大小对比 → 不一致 mark task warning
  5. spike 阶段实测：5 profile 并发下载同 SKU 同 round，验证 filename 不冲突

### C-020 [Major] Veo 后端无 idempotency = 中控 retry 重复点 Create 双倍消耗配额

- **针对**：v0.0.1 C-009 任务恢复设计 + 第 4.3 task_assign / cancel_task 协议
- **问题描述**：
  C-009 提了"任务中断恢复"，但只覆盖了"扩展知道上次跑到哪"。**Google Veo 后端不暴露 idempotency key API**，意味着扩展点击 Create 这个动作本身没法去重：

  **场景重现**：
  1. 中控 task_assign WS_A
  2. 扩展 register received → 加载 Flow project URL
  3. 扩展点 Upload first_frame → typed prompt → **点 Create**
  4. Create click 后 1 秒，sw hibernate / chrome 卡顿 / WS message 丢
  5. 中控 30s timeout → 认为扩展无响应 → cancel_task + 重派给 WS_A 或别的 WS
  6. 重派来的 WS 重新点 Create → Veo 后端收到 2 个相同 prompt 请求 → 生成 2 次 mp4
  7. **客户配额消耗 2 倍，但只有 1 个 mp4 被中控记录**

  设计稿 §4.3 的协议有 `cancel_task` 但没有"扩展确认 cancel 已执行"的回报；而且 Veo 后端不接受 cancel（一旦 click submit 就不可逆）

  V1 没这问题不是因为 V1 处理了，而是因为 V1 patchright 进程崩溃 = 整轮算 fail = 重跑同 round 但消耗的额度跟 V2 重复点 Create 一样多。但 V1 patchright 崩溃概率低，V2 sw hibernate 概率高得多。

- **依据**：
  - Veo / Flow 没公开 API（design §2.3 已论证），更不存在 idempotency-key header
  - V1 单 round 12-15 mp4 是账号配额上限（memory: project_test_protocol）
  - V2 sw hibernate 是高概率事件（manifest v3 设计就是这样）
- **影响**：
  - 单任务双倍消耗 → 撞 12-15 mp4 上限提前 → 单日产能下降 50%
  - 客户付费配额浪费
  - DB 记录跟实际 Veo 后端状态分歧 → 后续对账困难
- **建议方向**：
  1. **扩展点 Create 之前 + 之后**双重持久化：
     - 之前：chrome.storage.local 写 `pending_create: {task_id, round, ts}` + WS push `create_pending`
     - 之后：等 Flow project URL 变化（V1 v0.0.3 已知信号）→ chrome.storage.local 写 `create_committed: {project_url, ts}` + WS push `create_committed`
     - 两步之间任何中断 → reconnect 后扩展查 chrome.storage 知道处于哪个状态
  2. **中控不主动 cancel + retry**：
     - 收到 `create_pending` 后 → 中控状态机 transition 到 awaiting_commit
     - 即使 WS 断了也不重派；等 WS 重连后扩展自己上报 create_committed 或 create_aborted
     - 超过 30 分钟没收到任何信号 → mark task `unknown_state` → 操作员介入手动决策
  3. **Veo 后端状态查询**：
     - 扩展重连后，主动查 Flow project page 的 generation 列表（page DOM 可读）
     - 如果发现 1-N 个 in-progress generation 跟 pending_create 时间窗口匹配 → 认为已 committed → 不再点 Create
     - 这个查询本质就是 V1 v0.0.3 的 SPA URL 检测的扩展版
  4. **配额预警**：中控记录每个账号每日 Create click 次数（不是 mp4 完成次数）→ 超过 12 次告警操作员（不再派 task 给该账号）
  5. spike 必须验证：连续 click Create 同 prompt 短时间内，Veo 后端是否真的生成多份 → 如果 Veo 自己有 dedup 那 C-020 严重度降级

### C-021 [Major] manifest v3 升级 = 用户被打断 review permissions + unpacked 没自动更新机制 = V2 迭代摩擦极高

- **针对**：第 6.2 风险登记册"扩展更新困难（不上 store） 低/低"（v0.0.1 C-008 已升 高/高，但 design v0.2 §11 没真的升级正文）+ 第 5.3 安装路径
- **问题描述**：
  V1 v0.0.1→v0.1.0 迭代了 6 版（3 天），客户每次都收到一个 .exe 双击替换。V2 扩展每次升级要客户做：

  - **chrome 自动 disable 行为**：每次 manifest permissions 变（哪怕只加一个 host_permissions 条目），chrome 弹"扩展请求新权限：xxx，是否允许"对话框 → 操作员必须点允许，否则扩展 disabled
  - **unpacked 没 update 通道**：客户拿到新版 zip → 解压覆盖旧目录 → chrome://extensions 找到扩展 → 点"reload" 按钮 → 扩展重启
  - **chrome 主版本升级 disable unpacked**：每 4 周 chrome stable 升一次大版本，部分版本会 disable 所有未签名 unpacked 扩展（chrome 安全策略），客户重新点"加载已解压"
  - **每个 profile 独立操作**：5 profile × 上述每个步骤 = 操作员升级一次扩展要点 25-50 次

  **跟 V1 对比**：V1 .exe 替换 1 次 = 5 个工位全部升级；V2 扩展每个 profile 都得手动 reload

  设计稿 §6.2 风险登记册仍写"低/低"（v0.0.1 C-008 已要求改高/高，design v0.2 §11.2 写"已升级"但正文没改 → C-014 meta 验证此处）

- **依据**：
  - chrome 扩展 permission upgrade 行为（chrome dev docs - "Permissions"）
  - chrome stable cadence 4 周（chrome release schedule）
  - V1 客户操作能力（memory: project_v1_delivery，"不能 CLI / 不能编辑 yaml"）
- **影响**：
  - V2 一次小补丁 = 客户操作员 30 分钟手工 chrome 操作 → 客户拒绝升级
  - 长期看 V2 难以快速迭代修复 bug → 比 V1 时代更慢
  - chrome 主版本升级期间 V2 大批客户停摆
- **建议方向**：
  1. **manifest 一次性放足权限** —— 上线第一版 manifest 就声明所有可能用到的 permissions / host_permissions：
     - permissions: storage, scripting, tabs, downloads, alarms, notifications, webRequest, declarativeNetRequest
     - host_permissions: 扩展启动 list 完整（labs.google + flow URL + localhost:8080）
     - 后续升级**只删不加** permissions，避免触发 review dialog
  2. **可选权限晚激活**：危险权限放 `optional_permissions` + `chrome.permissions.request` 用户首次需要时再要 → 升级时不触发
  3. **必须重新评估 Chrome Web Store**：
     - 上 store 后扩展自动更新（chrome 后台 update 不打扰用户）
     - 上 store 后跨 chrome 版本兼容性更好
     - design v0.2 §11.2 C-008 说"重新评估"但 §5.3 正文没体现 → v0.3 必须明确选 store 还是 unpacked，并附带审核风险评估
  4. **unpacked fallback 必须做版本管理工具**：
     - Co-Pilot 内置"扩展升级助手"：检测扩展版本不匹配 → guided wizard 一步步带操作员 reload 5 profile
     - 或更激进：Co-Pilot 用 chrome DevTools Protocol 自动 reload 扩展（需要客户启 chrome 加 `--remote-debugging-port`，复杂度高）
  5. **release cadence 减缓**：V2 不像 V1 那样 3 天 6 版 — 按 1-2 周 release 一次，降低升级摩擦
  6. **风险登记册正文修正**："扩展更新困难 高/高 — 缓解：上 Web Store / Co-Pilot 升级助手"

## 三、Minor（v0.0.2 新增）

### C-022 [Minor] §11 spike 验收项 #1-#5 没有 owner / deadline / pass criteria

- **针对**：design v0.2 §11.1-11.2 spike 验收项 1-5
- **问题描述**：5 个 spike 验收项只列了"做什么"，缺：谁负责执行 / 什么时候完成 / pass / fail 的客观判定标准是什么。spike 在没有客观标准时容易"看着差不多就过"，引入 V2 实施期再爆问题
- **建议方向**：每条加 3 字段：owner（具体人名，不是"团队"）/ due（绝对日期不是"spike 阶段"）/ pass criteria（量化指标，e.g. "10 分钟任务跑完 sw hibernate 至少 5 次后任务能恢复 ≥4/5"）

### C-023 [Minor] §11.4 G1-G7 立项 gate 状态全是 "⏳ pending"，没有判定责任人 / 决策路径

- **针对**：design v0.2 §11.4 立项前置条件表
- **问题描述**：7 个 gate 哪些是客户回答（G1-G2）/ 哪些是 spike 验证（G3-G7），混在一起。客户回答类的 gate 需要谁去问 / 什么时候问 / 客户拒绝怎么办没说
- **建议方向**：表格加 3 列：决策路径（客户访谈 / spike 实测 / 内部决策）/ 责任人 / 备选 plan B（gate 失败如何）

### C-024 [Minor] chrome.storage.local 5MB quota 限制未声明 unlimitedStorage 权限

- **针对**：第 4.2 manifest + 第 4.2 内部分工 background.ts
- **问题描述**：chrome.storage.local 默认配额 5MB（chrome.storage.local quota）。V2 把 task lifecycle 状态 / multi-round 进度 / 错误日志全持久化到 storage.local（C-001 + C-009 + C-017 都依赖），多任务长跑容易撞上限。manifest 没声明 `unlimitedStorage` permission
- **建议方向**：manifest permissions 加 `unlimitedStorage`；spike 阶段实测 1 周连续跑 storage 占用增长

### C-025 [Minor] chrome 版本兼容矩阵未定义

- **针对**：第 9 待澄清问题第 3 项 "客户机的 chrome 版本统一吗？"
- **问题描述**：设计稿假设"chrome 88+"就够 manifest v3，但 chrome 100 → 130 之间 manifest v3 行为有显著差异（chrome.alarms 最小周期、chrome.scripting API 行为、offscreen API 可用性、service worker idle timeout 调整）。客户机 chrome 实际版本未知（可能不更新跟 chrome stable 拉开几十版）
- **建议方向**：spike 阶段定支持下限（建议 chrome 117+，即 30s alarms 最小周期）；客户机做 chrome 版本审计；扩展启动检测 chrome 版本，过低告警

### C-026 [Minor] 扩展 e2e 测试栈未选型

- **针对**：第 6.1 milestone 完全没列测试 / QA 阶段
- **问题描述**：V2 扩展功能 = V1 worker 1900 行的 TS 重写，但**没说怎么测**。chrome 扩展 e2e 主流是 puppeteer / playwright `--load-extension`，但跟 V2 unpacked 部署有差异。单元测要 mock chrome.* API（jest-chrome / vitest-chrome）。设计稿假设"扩展跑在客户 chrome 里没 unit test 套" → 跟 V1 一样靠客户复现踩坑
- **建议方向**：
  - 选型 playwright + chrome --load-extension 做 e2e（spike 阶段就把测试脚手架立起来，不要等 release 前补）
  - 单元测：vitest + chrome API mock（chrome.storage / chrome.runtime / chrome.downloads）
  - 集成测 fixture：mock Flow page DOM 跑 content script

---

---

# v0.0.3 — 第三轮 challenge（针对 design v0.3 inline 集成版）

design v0.3 已把 v0.0.1 + v0.0.2 共 26 条 inline 集成正文（C-014 meta 修正生效），新增 §12 迁移策略 / §13 安全模型，时间表 8-12 周，立项 7 gate 表格化。本轮 challenge 聚焦 v0.0.2 末尾"下一版迭代方向"列出的 8 大领域，**对 design v0.3 仍存在的盲区深挖**。

## 一、Blocker（v0.0.3 新增）

### C-027 [Blocker] §13.4 license 模型仍有结构性漏洞，V2 扩展+Co-Pilot 整包复制无法防御

- **针对**：design v0.3 §13.4 license 模型 + §13.2.2 扩展代码 = 明文资产
- **问题描述**：
  §13.4 设计了"Co-Pilot 启动校验 license → 扩展 register 时 Co-Pilot 给签名 → 扩展每次 task_assign 前握手"流程。这能挡"扩展单独被复制"，但**挡不住更现实的攻击**：

  **场景 A：整包复制**
  - 攻击者拷贝客户的整个 V2 安装目录（Co-Pilot.exe + license.lic + extension/ + 配置）→ 装到自己机器 → license 校验 OK（license 文件在）→ 扩展 register OK → 整套跑起来
  - 设计稿没有 **machine binding**：license.lic 不绑硬件指纹（CPU / 主板 / Win11 install GUID），可任意机器跑

  **场景 B：license server 离线模式**
  - §13.4 写"Co-Pilot 启动校验 license"——但 license server 在哪？V1 license.lic 是离线签名文件
  - 离线 license 没有 revocation 机制：license 给客户后客户违约 / 离职 → 没法远程撤销
  - 设计稿§13.4 末尾"删 license.lic + 重启 → 所有扩展失效"——但 license.lic 在客户机，作者根本删不了

  **场景 C：扩展 chrome.storage 持久化 license_signature**
  - §13.4 第 3 步"扩展 chrome.storage 持久化 license_signature"——chrome.storage.local 不加密 → 攻击者拷贝 chrome 数据 + license + Co-Pilot 全部走，绕过初始化握手
  - signature 是 token，没绑过期时间 / 没绑 device

  **场景 D：trial / paid 分层**
  - V1 license 没区分试用 vs 付费（memory: project_v1_delivery）
  - V2 §13.4 同样没说，扩展不知道当前 license tier → 没法限制 trial 用户的并发 / 任务量
  - 商业化时改 license schema 又是大改

  **场景 E：客户 A → 客户 B 转手**
  - 客户 A 不再用了，license.lic + 扩展整包给客户 B → B 跑得通
  - 设计没有"license 跟 customer_id 绑定且不可转移"的防御

- **依据**：
  - §13.4 步骤 1-5 没出现 machine_id / hardware fingerprint
  - §13.2.2 缓解措施有 customer_id 但没 machine_id
  - V1 license.lic 实际行为见 memory: project_v1_delivery
  - 离线 license + 在线 revoke 业界是难题（需 license server callback）
- **影响**：
  - **license 收入模型崩**：客户拷贝给非付费第三方 / 跨机器使用，付费方失效
  - **revoke 不可行**：客户违约 / 操作员离职后没有撤销路径
  - **trial 不可控**：未来商业化要 trial-paid 分层，license schema 没预埋
  - **核心商业风险**：V2 越成功，越多客户能拷贝走，越快丢失付费基础
- **建议方向**：
  1. **machine binding**：
     - license.lic 绑 machine_id hash（Win11 `MachineGuid` 注册表 + 主板 serial + CPU id）
     - Co-Pilot 启动 mismatch 拒跑
     - 客户换机器要重新签发（运营成本可接受，1 客户每年换机一次量级）
  2. **license server callback（可选 online 模式）**：
     - V2.0 仍接受离线 license（运营简单）
     - V2.1+ 加 online 模式：Co-Pilot 每天 ping license server 取 expiry / revoke list
     - online 模式失败 grace period：7 天容忍（避免客户网络抖一天就停）
  3. **license schema 预埋分层**：
     - license JSON 含 `tier: 'trial' | 'standard' | 'pro'` + `max_concurrent_workstations` + `max_daily_tasks`
     - Co-Pilot 启动按 tier 限并发；扩展每次 task_assign 校验
  4. **不可转移条款**：
     - license JSON 含 `customer_id` + `customer_name` + `bound_machine_id_hash`
     - dashboard 显著显示"License: 客户XXX / 机器YYY"——客户转手即可见违约
  5. **chrome.storage signature 加 short expiry**：
     - signature 1 小时过期，扩展每小时跟 Co-Pilot 重新握手
     - 拷贝走后没有 Co-Pilot 互动 1 小时即失效
  6. **设计稿 §13.4 必须重写，加上 §13.6 license tier schema** 章节

### C-028 [Blocker] §2.2 "账号信誉=真实使用历史" 跟 §5.4 "创建 profile_A 用账号 A 登录" 自相矛盾，账号 ban 影响范围未定

- **针对**：design v0.3 §2.2 "账号信誉：客户日常 chrome = 真实使用历史 ✓" + §5.4 "操作员在 chrome 里：点头像 → Add another account → 创建 profile_A → 用账号 A 登录"
- **问题描述**：
  V2 vs V1 anti-bot 优势的核心论点是"账号信誉=客户日常使用历史"——账号被 Google 看作"真人在用"。但这跟 §5.4 操作步骤直接冲突：

  **悖论 1：账号是"日常账号"还是"专门账号"？**
  - §2.2 暗示用户主账号（"日常 chrome 真实使用历史"——含 Gmail / Drive / 浏览历史）
  - §5.4 实操"创建 profile_A 用账号 A 登录"——意味着**为 V2 专门开 5 个账号**
  - 专门账号没"真实使用历史"——刚开账号没 Gmail 流量、没 Drive 文件、没浏览历史 → Google 风控看就是"陌生空号"
  - 这跟 patchright profile 在 anti-bot 维度等同，V2 优势消失

  **悖论 2：账号被 ban 影响半径不同**
  - 用日常账号：ban → 客户业务停摆（Gmail / Drive / 公司 Workspace）→ 不可接受
  - 用专门账号：ban → 创建新账号补回（V1 模式）→ 可接受但失去"真实历史"优势
  - V2.2 表 strike 系统不变，但 strike 的目标账号性质变了——危险半径变了，设计未澄清

  **悖论 3：V1 客户实际是哪种？**
  - memory: project_v1_delivery 提到"5 个账号"是为 Flow 专开的（"Flow 专门账号"假设）
  - 但 V2 设计读起来像鼓励客户用日常账号
  - 客户读 V2 文档会困惑：要不要把日常 Gmail 账号绑进 V2？

  **悖论 4：strike 触发后的处置**
  - V1 strike 累计 → disable workstation → 该 profile 不再派 task
  - V2 §4.5.3 strike 改按 email 累计 → disable email
  - 但 disable email 不等于 disable Google 账号——账号还能继续 Gmail / Drive 用，只是 V2 不派
  - **如果是日常账号**：disable 后客户日常仍受影响（unusual_activity 触发后 Google 会要求 captcha 重新登录所有设备）

  **悖论 5：Google 风控蔓延**
  - Google T&S policy：unusual_activity / TOS violation 风控会蔓延到同账号其他服务
  - 即使专门账号，如果该账号同时是客户某员工的辅助账号 → 该员工 Gmail 受影响

- **依据**：
  - §2.2 表第 4 行（账号信誉）的两种语义同时存在
  - §5.4 step 1-2 写"Add another account → 创建 profile_A"
  - V1 实践 5 账号是 Flow 专开（memory: project_v1_delivery）
  - Google T&S 政策（公开文档）
- **影响**：
  - 客户接 V2 时不知该用什么账号：日常账号 → ban 灾难；专门账号 → V2 anti-bot 优势消失
  - 操作员凭直觉选——大概率选日常账号（"用得熟"）→ 高风险
  - V2 vs V1 anti-bot 优势论证基础动摇
  - 客户被 ban 后的赔偿 / 责任无定义
- **建议方向**：
  1. **设计稿明确选边并量化**（§2.2 + §5.4 必须改）：
     - 选 A：**专门账号**（跟 V1 一致）— 明确告知客户"V2 anti-bot 优势主要在 selector / locale / 调试，不在账号信誉"；§2.2 第 4 行从 "✓" 改为 "≈持平 V1"
     - 选 B：**日常账号 + 严控产能**：< 5 mp4/day/account，账号"信誉补给"日常使用为主；strike 阈值更激进，触发后立刻 cooldown 72h
     - 选 C：**分层使用** — 客户专门账号做 batch（70% 流量）；客户某些"真实活跃"账号偶发使用（30% 流量）
  2. **风险披露**：
     - V2 客户文档（customer-manual）开头警告：扩展行为可能影响所运行 Google 账号信誉，**不建议用客户业务核心账号**
     - 风险声明在 setup wizard 第一屏让操作员勾选确认
  3. **strike 系统改造**：
     - 触发后**不直接 disable**（V1 行为），改为 cooldown 24-72h（账号"休息"）
     - 严重 strike → 通知操作员手动登录该账号做"人工保活"动作（看 Gmail / 用 Drive 5 分钟）
  4. **客户访谈（升立项 gate）**：
     - 加 G8 立项 gate："客户使用日常账号还是专门账号？"
     - 客户决策前不进 spike
  5. **设计稿 §2.2 表 + §5.4 必须 inline 修订**说明账号性质，**不能两可**

## 二、Major（v0.0.3 新增）

### C-029 [Major] 客户日常 chrome 已装的扩展（Grammarly / 1Password / AdBlock / chrome translate / 既有 VEO Automation）跟 V2 扩展冲突，§4.2 完全没防御

- **针对**：design v0.3 §2.2 "账号信誉=客户日常 chrome" + §4.2 内部分工 + §10.A 提到的 "VEO Automation / Auto Flow Pro / FlowForge Pro"
- **问题描述**：
  设计稿把"扩展是 chrome 一等公民"当纯优势，但**客户日常 chrome 通常装了一堆扩展**——这些扩展会跟 V2 扩展抢 DOM / 抢事件 / 互相打架：

  **冲突 1：Grammarly / 输入法扩展**
  - Grammarly inject 进 textarea 监听输入事件 + 自动改文本（语法修正）
  - V1 prompt 输入用 `keyboard.type` 60-110ms delay（memory: project_known_fragility 第 5 条），content script 在 chrome 里用 `dispatchEvent('input')` **同样会被 Grammarly 拦**
  - 实际提交的 prompt = 客户输入 → Grammarly 改 → V2 没察觉 → 提交给 Veo 的 prompt 跟客户预期不符
  - 客户日报"任务完成"但产出语义偏差

  **冲突 2：AdBlock / uBlock Origin**
  - 可能 block Flow 后端 API requests（hostname 撞 ad-related blocklist）
  - 改 page 网络栈，content script fetch 行为变
  - 静默吞掉 Flow API 错误，扩展拿到错误的 page state

  **冲突 3：1Password / LastPass autofill**
  - 自动填充覆盖 V2 上传 first_frame 的 file input
  - popup 干扰 V2 click 顺序（先点 1Password 弹窗才能点 Create）

  **冲突 4：chrome 自带 translate**
  - chrome 自动 translate 把 Flow UI 翻译成中文 → 即使 §4.2 验证了 locale-independent 锚点，translate 是 chrome 主动改 DOM
  - V1 v0.0.4 已经踩过（13 语言 selector），但那是 Flow 自身 locale；chrome translate 是另一层
  - V2 设计假设 chrome 不 translate，没说怎么禁

  **冲突 5：既有 Veo 自动化扩展**
  - design §10.A 自己提了 VEO Automation / Auto Flow Pro / FlowForge Pro
  - 客户**可能已经装过这些试用**（设计稿没问"客户是不是已经装了这类扩展"）
  - 同时 inject 到 labs.google → 跟 V2 同 page 抢同 selector → 双方 state 撕裂
  - 操作员不知道哪个扩展在干活，dashboard 数据跟实际产出对不上

  **冲突 6：chrome 扩展执行顺序不确定**
  - 多扩展同时 match 同 host → chrome 加载顺序看 ext_id 字典序
  - V2 ext_id 不固定，可能在其他扩展之前或之后 → DOM 干预时序不一致

- **依据**：
  - 客户日常 chrome 必装常见扩展是统计现实
  - V1 v0.0.4 多语言 selector 已承认 chrome translate 影响（memory: project_known_fragility）
  - design §10.A 自己列了竞品扩展存在
- **影响**：
  - **Grammarly 改 prompt** → 客户日报跟实际产出语义不符 → 客户信任崩
  - **静默冲突**：扩展互相干扰但无错误抛出 → 现场调试地狱
  - **客户 chrome 装 V2 + 之前试用过的 VEO Automation 共存** → 双方都点 Create 撞 Veo 后端
  - 排查工时：每次客户问题"你 chrome 装了什么扩展"作者要枚举
- **建议方向**：
  1. **扩展冲突检测（启动时 + 周期性）**：
     - manifest 加 `management` 权限 → 调 `chrome.management.getAll()` 枚举已装扩展
     - 已知冲突 ID 列表（Grammarly / VEO Automation 等）→ 中控 dashboard 红色告警
     - 严重冲突（Veo 自动化竞品）→ 扩展拒绝注册
  2. **content script 防御性写法**：
     - prompt 输入用 `Object.defineProperty` 直接改 textarea value + 派 React 内部 event（绕过 Grammarly input 监听）
     - 上传文件用 DataTransfer 直接构造 drop event（绕过 1Password autofill）
     - chrome.scripting.executeScript 用 `world: 'MAIN'` 注入到 page world（绕过其他扩展的 isolated world 拦截）
  3. **建议客户 V2 用专用 chrome profile**（C-003 的 Plan B 进化版）：
     - 不是"扩展跑客户日常 profile"，而是"客户为 V2 创建专门 profile（chrome 自带 multi-profile）"
     - 该 profile **只装 V2 扩展**，不装 Grammarly / AdBlock 等
     - chrome translate 在该 profile 关闭
     - 损失"日常使用历史"——但根据 C-028 反正这个优势论据本来就不稳
  4. **chrome translate 显式禁**：扩展启动注入 `<meta name="google" content="notranslate">`
  5. **冲突测试**：spike 必须在装常见扩展（Grammarly + 1Password + AdBlock + chrome translate）的 chrome 上跑 V2 验证
  6. **客户 onboarding 问卷**：客户机部署前问"chrome 装了哪些扩展？"，已装 Veo 自动化竞品的客户必须先卸再装 V2

### C-030 [Major] chrome profile sync 把 V2 扩展自动同步到操作员个人设备 = license 泄露 + 业务数据泄露

- **针对**：design v0.3 §5.4 多账号管理 + §13 安全模型完全未提 chrome sync + §13.4 license 模型
- **问题描述**：
  chrome 默认开 sync：扩展列表 / chrome.storage.sync / 书签 / 设置都自动同步到 chrome 登录账号的 Google 账户。V2 扩展装在客户 chrome → 如果操作员或客户的 chrome 登录账号开了 sync：

  **泄露场景 1：扩展自动跨设备安装**
  - 操作员在客户机 chrome 装 V2 扩展（unpacked 不会 sync，但走 Chrome Web Store 路径**会 sync**——§5.3 选项 A 决策为首选）
  - 操作员家里 chrome 登录同 Google 账号 → V2 扩展自动出现在家里 chrome → 操作员能在家里跑 V2 任务
  - **license 跨机器扩散**（跟 C-027 machine binding 缺失串联）

  **泄露场景 2：chrome.storage.sync 跨设备**
  - 设计稿 §4.2 写 "lib/storage.ts # chrome.storage.local 封装" — 但 .sync 也是 chrome 内置 namespace
  - 如果工程师误用 `.sync` 存 ws_token / setup_token / license_signature → 全部跨设备同步 → token 泄露到 Google 服务器
  - 设计稿没**显式禁止使用 storage.sync**

  **泄露场景 3：客户跨设备同步**
  - 客户在公司 PC 装 V2，公司 PC chrome 登录的是公司 Workspace 账号
  - 同 Workspace 账号在客户家用 PC 也登 chrome → V2 扩展同步过去
  - 客户家用 PC 没装 Co-Pilot → 扩展找不到中控（被动失败）
  - 但 chrome.storage 同步过去的是真实 license_signature → 离线复制可能仍能用

  **泄露场景 4：操作员离职**
  - 操作员离职 → chrome 个人账号还登 sync → 仍能"看到"客户 V2 扩展配置 + storage 数据
  - revoke license 不会清扩展（chrome 只能客户机 chrome 卸，操作员家里的卸不掉）

  **泄露场景 5：设计稿空白**
  - §13 安全模型 5 个威胁场景没列 chrome sync
  - §5.4 多账号管理只说"创建 profile" 没说 sync 状态
  - install wizard 没检测 sync 是否开

- **依据**：
  - chrome sync 默认开（用户登 Google 时自动启）— chrome dev docs
  - chrome.storage.sync vs storage.local 区别（sync 跨设备，local 本机）
  - chrome 扩展通过 sync 同步是 chrome 设计行为
- **影响**：
  - **license 模型再次崩塌**（C-027 + C-030 双因子）
  - **客户业务数据**（task / prompt / output 元数据 / sa​​ task_state）泄露到操作员个人 Google 账号 → 合规风险（GDPR）
  - **离职操作员长期"远程访问"**客户业务配置
- **建议方向**：
  1. **storage 严格禁用 sync namespace**：
     - design §4.2 lib/storage.ts 必须 inline 注明"**禁止使用 chrome.storage.sync**"
     - 代码层 lint rule 检测 `chrome.storage.sync` 使用 → CI fail
  2. **install wizard 检测 chrome sync**：
     - 扩展首次启动检测 chrome sync 状态（chrome.identity API 可知）
     - sync 开 → 弹窗要求关闭"扩展同步"才让继续
     - 或要求操作员用专门"工作账号"登 chrome（不绑个人 Google）
  3. **manifest 标记 not_syncable**：
     - chrome 没有原生 manifest 字段禁 sync
     - 唯一路径：通过 Chrome Enterprise policy `BlockExternalExtensions` 控制 — 客户机部署成本高
     - 或：扩展 ID 不固定（每次安装变），让 chrome 不识别为同一扩展（损失：升级麻烦）
  4. **document 警告强化**：
     - operator 文档前置警告"V2 扩展所在 chrome profile 必须不能开 sync 到操作员个人 Google 账号"
     - 客户 onboarding 表单确认勾选
  5. **§13 安全模型加第 6 个威胁场景**：chrome sync 跨设备扩散
  6. **spike 验证**：开 sync 装 V2 → 个人设备 chrome 是否真的会自动安装 V2 扩展 + 拷过 storage 数据

### C-031 [Major] §6.2 / §11.2 仅写"chrome 117+ 下限"但没建立完整版本兼容矩阵 → spike 验证基础不牢

- **针对**：design v0.3 §6.2 风险登记册 "chrome 版本兼容（C-025）" + §11.2 "chrome 版本兼容矩阵 / 平台兼容性 → 待 spike 阶段定下限" + §9 "chrome 版本 → C-025 / spike 阶段定 chrome 117+ 下限"
- **问题描述**：
  v0.0.2 C-025 标 Minor 是低估了。设计 v0.3 接受了 chrome 117+ 下限，但**没有具体版本对照表**：

  **缺失 1：客户机 chrome 实际版本审计未做**
  - V1 客户机 chrome 版本 memory 没记录
  - 假设 chrome 117+ 客户都满足——客户企业机 chrome 不强制更新（特别是关 auto-update 或受 GPO 控制的机器）→ 实际版本可能 90-105
  - 立项 G2/G7 没列"客户机 chrome 版本审计" gate

  **缺失 2：manifest v3 跨版本 API 行为对照**

  | chrome 版本 | 关键 API 行为 | 影响 V2 |
  |---|---|---|
  | 88-94 | manifest v3 引入；mv2 共存；sw 不稳 | spike 失败概率高 |
  | 95-101 | sw idle timeout 5min；scripting v1 | sw hibernate 测试结果可能偏乐观 |
  | 102 | scripting API v2 | scripting injection 模式变 |
  | 109+ | offscreen documents API（解决 sw hibernate 真正解，比 alarms 优） | 设计稿没用，但 chrome 109+ 可用 |
  | 116-117 | chrome.alarms 最小周期改 30s | C-001 缓解措施依赖此 |
  | 120+ | mv2 完全弃用 | 影响升级摩擦 |
  | 125+ | declarativeNetRequest 新限制 / webRequest blocking 弃用 | 网络拦截方案重写 |
  | 130+ | service worker 行为细微调整 | 待 release notes 确认 |

  **缺失 3：spike 测试 chrome 版本未指定**
  - §7.3 spike 验收 #1-#8 没说在哪个 chrome 版本测
  - spike 在最新 chrome 130 测全过 → 客户机 chrome 105 → 行为不同 → 客户复现失败
  - C-025 升 Major 但缓解仍模糊

  **缺失 4：Edge / Brave / Vivaldi 等 chromium-based 兼容性**
  - 客户可能用 Edge（chromium-based）—某些 manifest v3 API 实现略不同
  - 设计 §10.A 提到 "Edge / Brave 浏览器（chromium-based 但更宽松）" 作为退路 — 但兼容矩阵没建
  - 客户机如果是 Edge 而非 chrome，整个 V2 部署能跑吗？

  **缺失 5：chrome 升级 channel**
  - stable 4 周 cadence，每次大版本可能改 mv3 行为
  - V2 部署后 chrome 升级 → 跑得好的功能突然 break
  - 没有"先在 beta 测试"的预警机制

- **依据**：
  - chrome release notes（公开）
  - design §6.2 "chrome 117+" 假设
  - C-001 缓解（chrome.alarms 30s）依赖 chrome 117+
- **影响**：
  - spike 在 chrome 130 跑通的方案在客户 chrome 105 上失败
  - 客户机 chrome 自动 update 后 V2 行为变 → 客户问"为啥昨天好的今天不行"
  - V2 部署后 4-8 周必然撞 chrome 大版本升级
  - Edge 用户被排除（设计假设是 chrome）
- **建议方向**：
  1. **客户机 chrome 版本审计（升立项 gate）**：
     - 加 G9 立项 gate："客户机 chrome 版本审计 ≥117"
     - 客户机部署前 V2 launcher 检测 chrome 版本，<117 install 失败 + 提示升级路径
     - 不达标的客户机先升级 chrome，再装 V2
  2. **完整兼容矩阵（设计稿新增 §15）**：
     - 列 chrome 117 / 120 / 125 / 130 + Edge stable + Brave stable 兼容矩阵
     - 每个 chrome 大版本对 V2 关键 API 的支持矩阵（chrome.alarms / chrome.downloads / chrome.scripting / offscreen）
     - 标"已验证 / 部分验证 / 不支持"
  3. **spike 测试矩阵**：
     - spike 必须在至少 3 个 chrome 大版本跑：117 / 124 / 130
     - 每个版本跑 §7.3 全部 spike 验收 #1-#8
     - 不一致行为标 risk
  4. **降级策略**：
     - 扩展 startup 检测 chrome 版本：
       - chrome < 117：alarms 周期 1min（功能降级），警告中控
       - chrome < 109：禁用 offscreen 优化路径
     - 不做完全向下兼容（成本太高），只做 graceful degradation
  5. **chrome 升级监控**：
     - Co-Pilot 每周比对客户机 chrome 版本 vs chrome stable release
     - chrome 即将大升级（next stable beta 已发布）→ 中控告警 "下周 chrome 大升级，建议先在测试机验证"
  6. **Edge / Brave 不在 V2.0 范围**：明确写到 §6.3，避免承诺过头

### C-032 [Major] V1 35 条 fragility 没建立"V2 一一对应回归测试集"，§7 验收只能跑端到端不能证明每条 fragility 处理状态

- **针对**：design v0.3 §7.2 V2.0 release 验收 + memory: project_known_fragility（V1 35 条）+ v0.0.2 C-026
- **问题描述**：
  V1 worker.flow_playwright 1900 行 = 6 轮迭代踩出来的 35 条 fragility 处理。memory: project_known_fragility 列了所有 35 条。V2 重写 TS 后，**判定每条 fragility 在 V2 是 fix / unfix / 用新机制处理的标准不存在**：

  **缺失 1：fragility 没有"V2 状态"标注**
  - 35 条每条都需要回答：V2 仍存在？V2 用什么机制处理？V2 不再相关？
  - 设计稿 §7 验收用"端到端跑得通 + ≥V1 baseline 5%" 间接判定，但**端到端测试无法证明每条 fragility 在 V2 是 fix 状态**——可能"刚好这次没撞到"
  - V2 release 后 V1 已修 bug 重新出现，客户感觉退化

  **缺失 2：reproducer fixture 不存在**
  - 35 条没有"复现脚本 + 期望行为"的 fixture
  - V2 spike 阶段没法跑"V1 fragility 回归测"——没 fixture
  - 验收时只能"看着差不多就过"

  **缺失 3：V2 新机制处理的 fragility 没有"new mechanism 测试"**
  - 比如 fragility "claim 必须过滤 flow_project_url"——V1 是 SQL 层 filter；V2 也要做同样事但是在 dispatcher 层
  - V2 的实现没有专门测试，跟 V1 行为一致只能靠端到端覆盖

  **缺失 4：§7.3 spike 验收 8 项跟 35 条 fragility 关系不清**
  - spike #1-#8 是验证 v0.0.1 + v0.0.2 challenges，没回答"V1 已知 35 条 fragility 在 V2 何时验"
  - V1 fragility 中很多是 v0.0.1 / v0.0.2 challenge 没覆盖的（比如 prompt-attach 4 种 click 策略 / poster early-exit / multi-round 状态恢复 / inter_round_pause_sec gate / SPA URL 不更新 3 路并行检测 / Strike 系统集成）

  **缺失 5：CI 集成 + regression test 缺位**
  - v0.0.2 C-026 提了 e2e 测试栈选型 → design v0.3 §11.2 标 "partial"
  - "测试栈选型"跟"V1 fragility 回归测"是两个事——选了 playwright 不等于回归测建好了

- **依据**：
  - memory: project_known_fragility 列 35 条
  - V1 v0.0.1→v0.1.0 6 轮每轮修复都案例驱动，没自动化测试沉淀
  - design §7.2 验收用 "task 成功率 ≥V1 + 5%" 间接判定
- **影响**：
  - V2 release 后 V1 已修 bug 复发 → 客户失去信任
  - spike 验收只能"看着没问题"，主观——没有量化"V2 比 V1 好多少 / 哪些回归"
  - V2.x 升级期间不知道改动是否 break V1 已知 fix
- **建议方向**：
  1. **建立 V1 fragility 回归测试集（spike 阶段必做）**：
     - 35 条逐条写"复现脚本 + 期望行为 + V2 实现验证项"
     - 输出到新文档 `docs/v1-fragility-regression-suite.md`
     - 每条三态标注：
       - "V1 解决，V2 仍存在 → V2 必须重新实现（移植）"
       - "V1 解决，V2 架构层消除 → 仍要测，证明真消除"
       - "V1 解决，V2 用新机制处理 → 测新机制行为对齐"
  2. **测试 fixture 库**：
     - DOM snapshot：V1 客户复现的 Flow page DOM 状态 → 写成 HTML fixture
     - 网络 stub：mock Veo 后端 unusual_activity / no_flow_access / generation_failed response
     - 时序 fixture：sw hibernate / chrome 关 / WS 断 各种时序
  3. **CI 集成**：
     - 每次 V2 build 跑 35 条 regression
     - 必须 35/35 pass 才能 release
     - regression 失败的 commit 自动 revert
  4. **spike milestone 加新阶段**：
     - 设计稿 §6.1 加 **Phase 0.5 - V1 fragility 知识沉淀**（1 周，spike 之前）
     - 输出：`v1-fragility-regression-suite.md` + reproducer fixture + 每条 V2 状态标注
  5. **§7.2 验收增加第 10 项**：V1 35 条 fragility 全部 verified（35/35 fix in V2）

### C-033 [Major] §3.1 "Co-Pilot.exe ← Windows service / 后台进程" 仍然两可，权限模型 + chrome 启动时序未定义

- **针对**：design v0.3 §3.1 Plan A "Flow Harvester Co-Pilot.exe ← Windows service / 后台进程" + §4.1 chrome_profile_launcher.py + §13 安全模型未涉及
- **问题描述**：
  设计稿仍写"Windows service / 后台进程"两可，但**两者权限模型差异巨大**：

  **Windows service（SYSTEM 账户）vs 用户进程对比**：

  | 维度 | Windows service | 用户进程 |
  |---|---|---|
  | logout 后存活 | ✅ 跑 | ❌ 死 |
  | 访问 user chrome profile（`%LOCALAPPDATA%\Google\Chrome\User Data\`） | ❌ 看不到 | ✅ 能 |
  | chrome_profile_launcher.py 启 chrome | ❌ 启不起来（service 启 chrome 没 user session） | ✅ 能 |
  | dashboard 访问 | 浏览器走 localhost:8080 → 通 | 通 |
  | License 文件位置 | ProgramData（系统级） | LocalAppData（用户级） |
  | 安装权限 | 需 admin | 不需要 |
  | 客户 logout 重新 login | service 仍在跑 | 进程死，重启后扩展自动 reconnect |

  **设计稿冲突点**：
  - §3.1 写 "Windows service / 后台进程"——两者都不行
  - §4.1 新增 chrome_profile_launcher.py 启 chrome → 必须用户模式（service 模式启不了）
  - §3.1 Plan A "扩展跑客户日常 chrome" → 用户必须 login chrome → 用户模式合理
  - §3.1 Plan B "独立 chrome 实例 by chrome_profile_launcher" → 也要用户模式
  - **结论**：必须用户模式，但设计稿没 commit

  **缺失 1：customer 操作员行为未匹配**
  - 客户操作员习惯**不 logout**（V1 patchright 一直跑，习惯延续）→ 用户模式 OK
  - 但客户 PC 重启 / 自动登出（GPO / 公司策略） → 用户模式 V2 死
  - 设计稿没说 "Co-Pilot 必须设为开机自启" 的实现路径

  **缺失 2：chrome_profile_launcher 启动时序**
  - 操作员开机 → Win11 logon → Co-Pilot 自启（需配置）→ 等扩展连？
  - 还是 Co-Pilot 启动后**主动启 chrome + profile**（如设计 §4.1 chrome_profile_launcher）？
  - chrome 启动后 5-10 秒扩展才 inject → 中控 register window
  - 5 个 profile 串行启动，要 30-60 秒——dashboard 启动几分钟内"工位 offline"

  **缺失 3：Co-Pilot 启动 / 关闭路径**
  - service 模式：自动 + 系统级管理
  - 用户模式：开机自启项 / 任务计划登录后启动 / 操作员手动双击
  - 设计稿没说

  **缺失 4：权限矩阵跟 §13 安全模型脱钩**
  - §13.2.5 提"Co-Pilot ↔ Chrome 文件系统隔离"——但具体路径权限（output/ 写权限谁有）没说
  - service 模式下 SYSTEM 写 output/ → 用户读不到（权限问题）
  - 用户模式下两者同 user，无问题

- **依据**：
  - Windows service 权限模型（Microsoft docs）
  - chrome user data 路径默认 user-mode
  - V1 客户机 admin 状态未在 memory 记录
- **影响**：
  - 客户 V2 安装失败（admin 缺失）/ 卡在权限对话框
  - chrome 启动起不来（service 模式）/ 客户 logout 死（用户模式）
  - 启动时序混乱：dashboard 几分钟内"全 offline"，操作员困惑
  - 权限错位：output/ 写不到 / 读不到
- **建议方向**：
  1. **明确选用户模式 + 自启动**：
     - design §3.1 改成"Co-Pilot.exe **用户进程**，开机自启"
     - 不需要 admin 安装
     - logout 死 → 客户操作员习惯就是不 logout（合理 trade-off，跟 chrome 必须开同等代价）
  2. **自启动实现**：
     - 写到注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\FlowHarvester`
     - 或任务计划"用户登录时启动"
     - install wizard 自动配置
  3. **chrome 启动时序**：
     - Co-Pilot 启动后**等 30 秒** 让操作员手动开 chrome
     - 或：Co-Pilot 主动 chrome_profile_launcher 启 N 个 profile（按 §5.4 N=2-3 限制）
     - 启动 stagger（每 profile 间隔 5 秒），避免 5 个 chrome 同时撞起来 OOM
  4. **dashboard 启动告知**：
     - dashboard 有"启动中（X / N 个工位连接中）"loading state
     - 5 分钟没全 online → 红色告警
  5. **install wizard 检测**：
     - admin 状态 / user session 类型 / chrome 版本 / RAM 余量 / sync 状态（C-030）一次性检测
     - 任何不达标提示原因 + fix
  6. **设计稿 §3.1 必须 inline 修订**："Windows service / 后台进程"二选一，定为"用户进程 + 开机自启"

### C-034 [Major] §4.4 reconnect 机制是单工位视角，5 工位同时 reconnect storm + 时间不同步导致风控雷 / log 错乱

- **针对**：design v0.3 §4.4.3 中断恢复路径 + §4.5 WS_id 绑定 + heartbeat 协议
- **问题描述**：
  §4.4.3 设计了"sw 断→ 指数退避重连（1s/2s/4s/...）"——但**这是单工位视角**。多工位场景下：

  **场景 1：reconnect storm**
  - Co-Pilot 重启（升级 / 客户机重启）→ 5 个扩展同时 detect WS 断 → 同时按指数退避重连
  - **5 个扩展 backoff 起点都是 1s** → 同时重连 → server 同时握手 register → 风暴
  - register 通过后中控立即派 task → 5 个 chrome 同时 navigate Flow page → 同 IP 短时间 5 个 Flow 后端请求 → **Google 风控雷**
  - design §4.4.3 backoff 30s 封顶——但每个扩展**独立** backoff，没 jitter / 错峰

  **场景 2：register stagger 缺位**
  - 5 个扩展同时 register → 中控同时 ack → 中控同时认为 5 工位 online → 立即按 stagger_sec 派 task
  - 但 stagger_sec=60s 是**首次派 task 的间隔**，不是首次 register 后的等待
  - 所有工位被认为 healthy 后立即接 task → 跟"独立 backoff"叠加成爆发

  **场景 3：时间不同步**
  - 扩展用 `Date.now()`（客户 PC 系统时间）作 heartbeat timestamp
  - 客户 Win11 时间偶有错位：
    - NTP 失败（公司防火墙）→ PC 时间偏 5-30 分钟
    - 时区错（用户改过）
    - 双系统切换 / 跨设备同步引起的偏移
  - heartbeat ts 错乱 → 中控判 ws timeout 错位（client_ts > server_ts 几分钟 → 中控认为 client 在未来时间发的 → 验证 fail）
  - forensic_log 时间戳混 client_ts + server_ts → debug 时序混乱
  - daily report 跨午夜计算：00:00 customer local vs UTC，跨时区客户报表 shift 一天

  **场景 4：DST（夏令时）切换**
  - 客户 PC 时间夏令时切换瞬间跳 1 小时 → heartbeat ts 跳 → 中控认为 60min 没心跳 → flip ws offline → 误判 1 小时
  - 设计稿没考虑

- **依据**：
  - thundering herd 是已知分布式模式
  - chrome 不强制 NTP 同步 PC 时间
  - V1 forensic_log 已有 timestamp（memory: project_known_fragility）但单 worker 无 storm 风险
  - Google 风控基于 IP 短时间请求频率（公开行业知识）
- **影响**：
  - **Co-Pilot 重启 5-10 秒内 Google 风控 burst**（5 个扩展同时被 ban 风险）
  - log 时间错乱难调试（v0.0.2 C-017 加扩展端日志推送中控，时间戳不可靠就更难用）
  - daily report 数据 shift 一天（客户报表跟实际产出对不上）
  - heartbeat 时间偏差 → ws 假 offline / online 切换抖动
- **建议方向**：
  1. **reconnect 加 jitter**：
     - 扩展 reconnect delay = `5s + random(0, 30s)` 错峰
     - 保留指数退避 + jitter 双层
     - 5 工位 60 秒内陆续 reconnect，不集中
  2. **register stagger（中控端控制）**：
     - 中控收到 register 后 ack，但**不立刻 mark online**
     - 按到达顺序间隔 5-10 秒 mark online（5 工位陆续 ready）
     - mark online 后 stagger_sec 间隔派 task（V1 现有逻辑）
  3. **task_assign rate limit**：
     - Co-Pilot 重启后 1 分钟"warm up window"
     - warm up 期间不主动派 task（即使 stagger 到点）
     - 5 工位逐个 warm up 完成后才正常调度
  4. **时间同步策略**：
     - WS register 时扩展上报 `client_ts`，Co-Pilot 比对自身 ts → diff > 5min 告警
     - 时间偏差大于 5 分钟 → 拒绝 register（让操作员先校准 PC 时间）
     - log 时间戳一律用 `server_ts`（Co-Pilot 收到时打），不信 client_ts
     - daily report 用 server timezone（不是 client）
  5. **DST 防御**：
     - heartbeat 超时判断用 `server_ts - last_server_ts`（server 视角），跟 client 时间无关
     - DST 切换瞬间 server 自身也会受影响——server 必须用 monotonic clock（Python time.monotonic()）做超时判断
  6. **spike 验收增加**：
     - spike #9：Co-Pilot 重启场景 5 ws 同时 register，监测 reconnect storm 行为
     - spike #10：客户 PC 时间偏移 30min，验证 register reject + log timestamp 正确

## 三、Minor（v0.0.3 新增）

### C-035 [Minor] Win11 power management / sleep / battery saver 影响 sw alarms 频率，§4.4 没考虑

- **针对**：§4.2 alarms 30s + §4.4 reconnect
- **问题**：
  - 客户 Win11 sleep → chrome 暂停 → 任务停（设计稿假设客户不让 PC 睡，但默认 power plan 是"平衡"，30 分钟 idle sleep）
  - battery saver mode（笔记本）→ chrome 后台 throttle 更激进 → sw alarms 实际触发周期 > 30s
  - 监视器关 → chrome throttle background tabs（chrome 109+ 引入 Memory Saver / Energy Saver）
  - chrome.alarms 的 30s 最小周期是文档值，**实际触发频率受系统电源策略影响**
- **建议**：
  - install wizard 配置 Win11 power plan "高性能" + 关 sleep / hibernate
  - chrome 启动加 `--disable-background-timer-throttling` flag（chrome_profile_launcher Plan B 可加）
  - 扩展启动检测 chrome power state（chrome.power API），告警

### C-036 [Minor] Windows Defender / 客户 AV / firewall 干扰未列

- **针对**：§5.3 安装路径 + §13 安全模型
- **问题**：
  - Windows Defender 对 unsigned exe / unpacked 扩展可能触发 SmartScreen 警告
  - 客户 AV（卡巴斯基 / 360 / 腾讯电脑管家）扫描扩展 dir 卡顿，chrome 启动慢
  - 防火墙拦 ws://localhost:8080 内进程通信（罕见但有）— Win11 默认 Defender Firewall 不拦本机但企业 GPO 可能拦
  - V1 PyInstaller bundle 已有 SmartScreen 烦客户的历史
- **建议**：
  - Co-Pilot.exe 上 code signing cert（~$500/年，EV cert 更佳）→ SmartScreen 直接通过
  - customer-install 文档加 AV 白名单步骤（卡巴斯基 / 360 单独说明）
  - install wizard 自动加 Windows Defender 排除项（需 admin 权限一次性，操作员同意后写）

### C-037 [Minor] forensic_log + screenshots + 诊断包磁盘累积无清理策略

- **针对**：§4.1 app/diagnostics.py 保留 + §13.5 "诊断包默认本地 30 天后过期"（仅一句）
- **问题**：
  - 5 profile × 100 task/day × 平均 2-3 screenshots（错误时） → 几百张/天
  - forensic_log SQLite 表持续增长
  - 诊断包 zip 累积（客户每次问题都生成一个）
  - 长跑 1-3 个月后 GB 级磁盘占用
  - C 盘满 → Win11 卡 → V2 整体崩
  - §13.5 "30 天后过期"是声明，没说**实现路径**（谁删 / 怎么删 / 删失败怎么办）
- **建议**：
  - rotation policy：forensic_log 按日 partition，30 天后自动 DELETE + VACUUM；screenshots 30 天 / 5 GB 上限
  - 诊断包：保留最新 5 个，超出删旧
  - Co-Pilot 每日 3am 跑 cleanup task（非 task 高峰）
  - dashboard 显示磁盘占用 + 清理按钮

### C-038 [Minor] chrome incognito / guest 模式扩展默认不跑

- **针对**：§4.2 manifest 配置 + 操作员误用场景
- **问题**：
  - 操作员误开 incognito（Ctrl+Shift+N）→ V2 扩展默认不跑
  - chrome guest 模式同样无扩展
  - 操作员困惑："chrome 开了 V2 扩展也装了为啥工位 offline"
  - manifest 没标 `incognito: split` 或 `incognito: spanning`
- **建议**：
  - manifest 不开 incognito 支持（保持 default not_allowed）
  - 扩展首次启动检测 chrome.extension.inIncognitoContext，是 true → popup 显著提示"Flow Harvester 不能在隐身模式运行"
  - 操作员 onboarding 文档加注

### C-039 [Minor] dashboard 中文 vs 扩展 popup 默认英文落差

- **针对**：§4.2 popup / side_panel / options 全 TS 默认英文 + V1 dashboard 中文
- **问题**：
  - V1 dashboard 中文，操作员习惯
  - V2 扩展 popup / options 默认英文（设计稿全英文 UI 文案）
  - 操作员看 dashboard 中文 → 看扩展 popup 英文 → 体验割裂
  - chrome i18n API 标准方案没在设计稿提
- **建议**：
  - 扩展用 chrome.i18n API：`_locales/zh_CN/messages.json` + `en/messages.json`
  - 默认 chrome.i18n.getUILanguage() 自动选语言
  - V1 中文 dashboard 风格对齐
  - 文档（customer-manual / install-windows）加中文版本

### C-040 [Minor] V3 multi-tenant 是否在 V2 schema 预埋未决

- **针对**：§6.3 V3 范围 + §12.2 切换粒度（按账号增量切）
- **问题**：
  - design §6.3 V3 范围列了"多操作员 / 多租户"——但 V2 schema 是否预埋 customer_id 没说
  - 不预埋：V3 数据迁移痛苦（workstations / tasks / task_results 全要加 customer_id）
  - 预埋：V2 多 1 个 NULLABLE 字段，演进低成本
  - §12 迁移策略加了 dispatcher_kind 字段——但没加 customer_id
- **建议**：
  - V2.0 schema 加 `customer_id` / `tenant_id` NULLABLE 字段（默认 'default'）
  - WS register 协议加 `tenant_id` optional 字段（默认 'default'）
  - V3 切 multi-tenant 时改 NOT NULL + UNIQUE constraint 即可
  - 不影响 V2.0 行为

---

---

# v0.0.4 — 第四轮 challenge（针对 design v0.4 inline 集成版）

design v0.4 已把 v0.0.1 + v0.0.2 + v0.0.3 共 40 条 inline 集成正文，commit 了"专门账号"模式（C-028）/ 用户进程 + 自启（C-033）/ 扩展冲突检测（C-029）/ chrome sync 禁用（C-030）/ Phase 0.5 fragility 沉淀（C-032）/ §15 chrome 版本矩阵（C-031）/ §13.4 license machine binding + tier（C-027）。本轮聚焦 v0.0.3 末尾"下一版迭代方向"列出的 8 大领域 + 新增深角度（CSP / Web Store policy / 多 tab / SQLite 并发 / 扩展自身 update race）。

## 一、Blocker（v0.0.4 新增）

### C-041 [Blocker] CSP 合规漏洞 — manifest v3 严禁 eval + labs.google 自身 CSP 双重限制，§4.2 "React fiber 读取" 等假设在 CSP 下技术可行性未验证

- **针对**：design v0.4 §4.2 "Locale 处理路径" 4 条假设 + manifest v3 安全策略 + labs.google 自身 CSP header
- **问题描述**：
  §4.2 列了 4 条 locale-independent 路径作为 spike #2 验证项，但**这 4 条全部撞 CSP 雷**：

  **CSP 雷点 1：manifest v3 禁 eval / Function constructor / setTimeout(string)**
  - manifest v3 默认 `extension_pages` CSP：`"script-src 'self'; object-src 'self'"` — 严禁 eval 和动态代码生成
  - §4.2 假设 1 "通过 `__REACT_DEVTOOLS_GLOBAL_HOOK__` 读组件状态" — 这个 hook 是 page world 全局变量，**content script 默认在 isolated world，看不到 page world 全局**
  - 要访问 page world 必须 chrome.scripting.executeScript({world: 'MAIN'}) — 这个 OK，但 inject 的代码本身仍受 page CSP 约束（labs.google 自己的 CSP）

  **CSP 雷点 2：labs.google 自身 CSP**
  - Google 产品 CSP 通常含 `script-src 'self' https://www.gstatic.com 'nonce-xxx'`
  - 如果 V2 用 chrome.scripting + world: 'MAIN' inject 脚本 → 受 Google CSP 约束
  - inline script / eval 直接被 page CSP 拒绝
  - chrome 扩展 isolated world 不受 page CSP 约束，**但跨 isolated/MAIN world 访问 React fiber 极复杂**
  - 设计稿没说怎么穿透 page CSP

  **CSP 雷点 3：production React build 不暴露 hook**
  - `__REACT_DEVTOOLS_GLOBAL_HOOK__` **production 模式默认不挂在 window 上**（除非用户装了 React DevTools 扩展）
  - labs.google 是 production build → hook 不存在
  - v0.0.1 C-002 已经提到这点，但 design v0.4 §4.2 仍把这条作为假设 1 列出 → **未实证**

  **CSP 雷点 4：fetch / XHR 拦截方式**
  - §4.2 假设 3 "拦截 fetch / XHR" — chrome 扩展 isolated world 拦不到 page world 的 fetch 调用
  - 要拦必须 `chrome.scripting + world: 'MAIN'` 注入 monkey-patch 代码
  - monkey-patch 是动态行为，受 CSP 约束（虽然 inject 时机是脚本加载前，但 `'self'` 只允许同源脚本）

  **CSP 雷点 5：MutationObserver 无 CSP 问题但定位锚点仍未实证**
  - §4.2 假设 2 "MutationObserver" — 这条 CSP 上没问题（API 调用，非动态代码）
  - 但**只是 polling 方式更优雅**，不解决"找到 locale-independent 锚点"的根本问题
  - 假设 2 实际是"用什么方式 watch DOM"而不是"locale 怎么解决"——design 把它当 locale 解决方案是混淆

  **设计稿 §4.2 spike #2 验证标准模糊**：
  - "用越南语账号跑 Flow，写 50 行 content script 试图不依赖文本找 upload / generate 按钮"
  - **没说**用 isolated world 还是 MAIN world，没说 CSP 报错怎么处理
  - spike pass 标准 "找到锚点（任一）" — 4 条假设里**任一**就过太松；如果是 MutationObserver（假设 2）"找到了"但本质还是 text selector，不算解决

- **依据**：
  - manifest v3 default CSP（chrome dev docs - "Content security policy"）
  - Google 产品 CSP 普遍含 nonce-based script-src（公开 HTTP header）
  - React production build 不暴露 hook（React docs）
  - chrome content script isolated world vs MAIN world（chrome dev docs）
- **影响**：
  - spike #2 跑出来"找到锚点"但实际是 isolated world MutationObserver 看 text → 跟 V1 多语言列表本质相同 → V2 Locale 优势消失
  - V2 release 后客户用越南语账号跑，selector drift 复发
  - §2.2 表"Locale: 假设可不依赖文本" 直接证伪
  - design §4.2 假设 1 / 3 / 4 因 CSP 直接不可行，假设 2 是 MutationObserver 跟 locale 无关
- **建议方向**：
  1. **spike #2 验收标准重写**：
     - 区分"技术可行"（找到 DOM 锚点）vs "locale-independent"（锚点不依赖文本）
     - 必须达到 locale-independent 才算 pass
     - 4 条假设逐条 PoC：哪条在 CSP 下技术可跑 + 哪条真不依赖文本
  2. **CSP 穿透方案明确**：
     - 用 `chrome.scripting.executeScript({world: 'MAIN', func})` 注入 — 受 page CSP 约束
     - 通过扩展 declarativeNetRequest 改 page CSP header（manifest v3 支持，需要 declarativeNetRequest 权限）— 这是 anti-CSP 路径，可能违反 Web Store policy（C-042）
     - 接受 isolated world 限制，只用 chrome content script 标准 API（chrome.scripting / DOM 操作）
  3. **接受 locale 退路**：
     - spike #2 pass 但只能"text + MutationObserver" → §2.2 Locale 行降级到 "≈ V1（同样多语言列表，但 fail-fast）"
     - V2 优势重定位：不是 locale，而是**fail-fast + 精准告警 + DevTools 可调试**
  4. **设计稿 §4.2 必须 inline 加 CSP 章节**：列每条假设的 CSP 兼容性 + 穿透成本 + spike 验证步骤
  5. **生产 React build 真相**：spike #2 第一步先验证 `__REACT_DEVTOOLS_GLOBAL_HOOK__` 是否存在 → 不存在直接放弃假设 1，不要浪费时间

### C-042 [Blocker] design §5.3 / §11 commit "首选 Chrome Web Store" 但没评估 Google 对自动化 Veo / Flow 自家产品的下架风险

- **针对**：design v0.4 §5.3 选项 A "Chrome Web Store" + spike #5 "Chrome Web Store 上架 PoC" + §10.A 提到 VEO Automation / Auto Flow Pro 通过审核
- **问题描述**：
  设计稿基于"VEO Automation 等通过审核"得出"V2 也能通过"——这个推理跳了几步：

  **跳跃 1：通过审核 ≠ 长期上架**
  - VEO Automation 当下在 Web Store 不代表明天还在
  - Google 政策变化 / 用户举报 / TOS 解读变化随时可触发下架
  - 第三方 VEO Automation 下架 V2 可能受牵连（"自动化 Google 自家产品"被定性）

  **跳跃 2：Web Store 政策对 "自动化 Google 服务"特别敏感**
  - Chrome Web Store Developer Program Policies 明文："Don't engage in any deceptive or invasive practices on Google services"
  - "Single Purpose Policy" — 扩展必须有单一明确用途，"自动化 Veo 视频生成"是 single purpose 但范围会被解读为"绕过 Veo 配额 / 滥用 Veo"
  - "Limited Use of User Data" — V2 扩展处理用户 prompt + 上传 frame image + 下载 mp4 全是 user data，policy 要求"only what's necessary for stated single purpose"
  - **Google 自家产品自动化是 takedown 高敏感度类**：Gmail 自动化扩展、Drive 自动化扩展、Veo / Flow 自动化扩展，Google T&S 团队主动审查频率高
  - VEO Automation 通过审核可能是审核员 miss，**不是 policy 允许**

  **跳跃 3：审核拒绝路径不清**
  - 设计稿 spike #5 "上一个 minimal 测试版本" — 但 minimal 版本和 V2 全功能版本审核标准不同
  - V2 真实版本含：自动 click Create button + 上传 frame + 下载 mp4 + 长连 WebSocket 到 localhost
  - 审核员看到 **localhost WebSocket 通信**会标记为 "External Server Communication" 触发额外审核
  - 拒绝后申诉路径周期 4-12 周，期间客户没法装

  **跳跃 4：takedown 后客户机扩展自动 disable**
  - Web Store 下架后 chrome 自动 disable 已安装版本
  - 不是"已安装的还能用，只是新用户装不了"——chrome 24-72 小时内通过 sync 通知 disable
  - 客户机 V2 突然全停摆，没有恢复时间窗口
  - 这是 V2 的**单点故障**，比 unpacked 还脆弱（unpacked 至少 chrome update 才 disable）

  **跳跃 5：V2 spike #5 "minimal 测试版本"误导**
  - minimal hello world 扩展通过审核 = 几乎肯定（policy 不会拒空扩展）
  - 但 V2 真实功能版本审核结果跟 minimal 完全无关
  - spike #5 pass 不能说明 V2 能上架 → 立项 gate G7 评估失误

- **依据**：
  - Chrome Web Store Developer Program Policies（公开）
  - Google T&S 历史上对 Gmail / Workspace 自动化扩展的下架记录（公开 google takedown news）
  - design §5.3 选项 A "Google 政策变化时可能下架（需要 monitor）"已轻描淡写承认
  - design §10.A "VEO Automation / Auto Flow Pro / FlowForge Pro" 部分通过 — 没说哪个长期稳定
- **影响**：
  - **下架后客户全停摆**——V2 单点故障比 V1 严重
  - 上架 PoC 误导：minimal 通过 ≠ 真实版本通过；客户期望被设错
  - 审核拒绝后 4-12 周申诉期间没分发渠道
  - 即使初次通过，长期仍随 Google 政策变化
- **建议方向**：
  1. **不能 single-bet Web Store**：
     - V2 必须**同时支持 unpacked + Web Store** 两种分发，不是 OR 而是 AND
     - Web Store 是首选客户体验路径，unpacked 是备份（避免下架后无渠道）
     - install wizard 自动检测：扩展若被 chrome disable（Web Store 下架），自动切 unpacked 重装
  2. **spike #5 验证升级**：
     - 不是 hello world，是**功能性 PoC**：含 chrome.downloads + WebSocket + content script DOM 操作
     - 审核 4-12 周排队，spike 7 天等不及 → spike #5 改异步（spike 不阻塞）
     - 设计稿 §7.4 G7 决策路径：spike #5 上架成功才走 Web Store；若拒 / 长时间 pending 走 unpacked
  3. **合规自查**：
     - 写 privacy policy 网页（Web Store 强制）
     - 在 Web Store description 明确 "Single Purpose: Automate user's own Veo video generation tasks"
     - 不要描述为"绕过配额 / 加速生成"避免 takedown 触发词
  4. **takedown 监控 + 应急预案**：
     - Co-Pilot 每日 ping Web Store 检查 V2 扩展状态
     - 检测到 disabled / removed → 中控告警 + 自动切 unpacked
     - 客户支持 SLA："Web Store 下架后 24h 内 unpacked 替换"
  5. **法律层评估**：扩展用于 Google 服务自动化是否违反 Google Workspace TOS / Veo 服务条款 — 客户合规风险（不只是 V2 风险）
  6. **设计稿 §5.3 必须 inline 加 takedown 风险评估章节** + §6.2 风险登记册新增"Web Store takedown 高/极高"

## 二、Major（v0.0.4 新增）

### C-043 [Major] 多 tab 管理 + Flow tab 定位策略缺失，操作员关 Flow tab / content script 多次 inject 的处理未设计

- **针对**：design v0.4 §4.2 内部分工 "content_scripts: matches `https://labs.google/fx/tools/flow/*`" + §5.1 操作员日常流程
- **问题描述**：
  设计稿假设"扩展 inject 到 Flow page 跑任务" — 但**操作员 chrome 实际有多个 tab**，扩展行为复杂：

  **场景 1：操作员有多个 Flow tab**
  - 操作员同时开 3 个 Flow tab（不同 project URL）查看历史产出
  - V2 扩展 content script `matches: labs.google/fx/tools/flow/*` 会**inject 到全部 3 个 tab**
  - 中控派 task → sw 不知道哪个 tab 是"目标 tab" → broadcast 还是选一个？
  - 多个 content script 同时跑 → DOM 操作 race
  - 多个 page state 检测 → 上报中控的 page state 来自哪个 tab？

  **场景 2：操作员关掉正在跑任务的 Flow tab**
  - 任务跑到 multi-round 第 5 轮，操作员手抖关了 tab
  - content script 死，service worker 没收到 task 中断信号（content script 没 onbeforeunload 把状态推给 sw）
  - sw 30s heartbeat / chrome.storage.local 都没 update → 中控以为还在跑
  - C-009 / §4.4 设计了 reconnect 对账 — 但前提是扩展知道 tab 死了；tab 死扩展不一定知道（content script 跟 page lifecycle 同生死）

  **场景 3：操作员切到别的 tab（Flow tab 后台）**
  - chrome 后台 tab throttle：MutationObserver 触发率降低、setTimeout 受 throttle、chrome 109+ Memory Saver 整 tab 暂停
  - V2 task 中途 tab 切后台 → 状态机检测变慢 → mp4 已生成中控不知道 → 超时 mark failed
  - C-035 提了 power management，但没具体到 tab throttle

  **场景 4：操作员误开新 Flow tab 切到了别的 project**
  - 操作员看历史，在 V2 跑任务的 tab 旁边开新 tab → 新 tab 加载 Flow project URL
  - 新 tab content script 也 inject → 拿当前 project URL 上报中控
  - 中控混淆"哪个 tab 是工位 X"

  **场景 5：chrome 重启 / chrome update 后 tab 自动 restore**
  - chrome restore 上次的 tab → V2 tab 也 restore → 但 V2 task state 在 chrome.storage.local，page 已不在原 state（restore 是新 page load）
  - content script 重新 inject → 看到 chrome.storage 有 pending task → 试图 resume → 但 page state 不对（restore 后 Flow page 是 fresh load 不是 mid-task）

  **设计稿缺失**：
  - 没定义"工位的 active Flow tab"概念
  - 没说扩展怎么标记"我在哪个 tab 跑任务"
  - 没说 tab 关闭 / 切后台 / restore 的处理路径

- **依据**：
  - chrome 扩展 content script lifecycle（chrome dev docs）
  - chrome 后台 tab throttle 行为（chrome docs - "Background tab freezing"）
  - chrome 109+ Memory Saver 行为
  - V1 没这问题因为 patchright 启的 chrome 只 1 个 tab
- **影响**：
  - 多 Flow tab → 任务派给"错的 tab" → 跑错 project / 漏跑
  - 关 tab 中途 → 中控状态不一致 → silent failure（C-009 / §4.4 兜底但触发条件不全）
  - 切后台 → 任务超时假死
  - chrome restore → 任务从 pending 状态错误 resume
- **建议方向**：
  1. **定义"工位 active tab" 概念**：
     - 扩展 service worker 维护 `active_flow_tab_id` per workstation
     - 任务 dispatch 时 sw 找已存在的 active tab，没有就 chrome.tabs.create 开一个
     - chrome.tabs.onRemoved 监听 tab 关闭 → 任务 mark interrupted + 通知中控
  2. **tab 锁定**：
     - 派 task 时 sw chrome.tabs.update({pinned: true}) 把 tab pin 住（视觉提示"这是工位 X 的工作 tab"）
     - 标记 chrome.tabs.update({title: "[WS_X 工作中] Flow"}) 让操作员一眼可见
  3. **多 Flow tab 时 dispatch 选择**：
     - sw 收到 task_assign → chrome.tabs.query({url: 'https://labs.google/fx/tools/flow/*'}) 列所有 Flow tab
     - 已有 active 工位 tab → 用之
     - 无 → 创建专用 tab（pinned + 特殊 title）
     - 多个 tab → 用最近 chrome.tabs.update 的（last interaction）
  4. **后台 tab throttle 防御**：
     - chrome.tabs.update({active: true}) 在关键步骤（点 Create / 等待 generation）切到前台
     - 或：chrome 启动加 `--disable-background-tab-throttling` flag（侵入性高）
     - 或：用 chrome.alarms 30s + content script onmessage 主动跟 sw 同步状态，绕过 setTimeout throttle
  5. **chrome restore 处理**：
     - sw 启动检测 chrome.storage 有 pending task → 不立即 resume，先验证 page state（content script 检测 Flow page 是否 mid-generation）
     - page state 不匹配 → mark task `interrupted` + 通知中控（不是错误恢复）
  6. **新增 §4.6 多 tab 管理章节**

### C-044 [Major] §13.5 "客户机 V2 不上传任何客户数据" vs cloudflared 隧道用法直接矛盾，GDPR 边界未定

- **针对**：design v0.4 §2.2 + §4.1 cloudflared **保留** + §13.5 GDPR "客户机 V2 不上传任何客户数据（除非用 cloudflared 隧道——operator 主动开关）"
- **问题描述**：
  §13.5 用括号补丁带过"除非 cloudflared"——但 cloudflared 是设计明确**保留**的远程支持渠道（C-017），实际使用频率高，不是边角 case：

  **矛盾 1：声明 vs 实际**
  - §13.5 "不上传任何客户数据"——客户读了认为 V2 私有
  - §2.2 + §4.1 + §4.4.3 / §11 多处保留 cloudflared 作为远程支持核心机制
  - cloudflared 隧道开起来 → 作者从公网访问客户 dashboard → **dashboard 含所有 prompt / SKU / mp4 元数据 / forensic_log**
  - 数据没"上传"但"通过隧道穿透到作者机器" — GDPR 视角同样是"data transfer to processor"

  **矛盾 2：operator 主动开关的 ambiguity**
  - "operator 主动开关"——但 cloudflared 隧道默认开还是关？V1 v0.0.2 是默认开（启动随 Co-Pilot）
  - 默认开 → operator 不主动关 = 长期数据可访问
  - 默认关 → 客户出问题前 operator 没意识 → 出问题时来不及开 → 回到 v0.0.1 时代
  - 设计稿没说默认状态

  **矛盾 3：GDPR DPA（数据处理协议）**
  - 客户跨欧盟使用 V2 → V2 处理客户业务数据（prompt 含产品描述 / 客户名 / SKU）
  - GDPR 要求 DPA：作者作为 processor，客户作为 controller
  - V2 没提 DPA 文档；客户出问题作者远程支持 = ad-hoc 数据访问，无审计日志
  - "V2 私有部署"广告语跟"作者随时可远程支持"功能矛盾

  **矛盾 4：cloudflared 安全模型缺失**
  - cloudflared 隧道用 named tunnel + cloudflare access policy 还是匿名 tunnel？
  - 匿名 tunnel：URL 公开后任何人可访问 dashboard（无 auth）
  - named tunnel + access policy：要 cloudflare 账号 + 客户 IP 白名单 / 邮箱 SSO — 客户端能力上限以下
  - design §4.1 没说哪种

  **矛盾 5：诊断包扩展项**
  - C-017 v0.0.2 建议"诊断包包含扩展端 console 日志"
  - 扩展 console 含 prompt（debug 输出）+ DOM snapshot（screenshot）+ chrome.storage state（task_state）
  - 诊断包客户 zip 给作者 → 作者机器存客户数据 → 数据保留期？
  - §13.5 "诊断包默认本地 30 天后过期" — 客户机本地 30 天，但**邮件发出去后作者机器**没保留期约束

- **依据**：
  - GDPR Article 28（DPA 要求，public）
  - cloudflared 行为（cloudflare docs）
  - §13.5 末尾括号补丁
  - V1 v0.0.2 cloudflared 默认行为
- **影响**：
  - 客户 GDPR 合规出问题：欧盟客户不能用 V2 / 不能远程支持
  - 客户广告"私有"但实际作者可见 → 信任崩
  - 操作员误开 cloudflared 长期不关 → 数据长期可访问
  - 出问题时作者远程支持要求 = 客户合规风险接管
- **建议方向**：
  1. **§13.5 重写明确数据流**：
     - 列每个数据源（prompt / SKU / screenshot / log / mp4）的存放位置 + 流向 + 保留期
     - 区分"客户机 only" vs "经 cloudflared 可访问" vs "邮件诊断包流出"三种状态
  2. **cloudflared 默认关**：
     - V2 默认不启 cloudflared
     - operator 在 dashboard 显式按"开启远程支持"按钮才启 — 启动时 30 分钟自动关闭（防长期挂）
     - 启用时 dashboard 显著横幅 "远程支持已启用，作者可访问，剩余 28 分钟"
  3. **cloudflared 安全升级**：
     - 用 named tunnel + cloudflare zero-trust access policy
     - 客户机 install wizard 配置 access policy（绑定作者邮箱）
     - 启动时 token-based auth（不是匿名 URL）
  4. **诊断包加密**：
     - 客户机生成诊断包用作者公钥加密（GPG / age）
     - 邮件发出仅作者可解
     - 诊断包内容自动脱敏：prompt 截断前 50 字符 / 替换客户名为 `<customer-redacted>`
  5. **DPA 模板**：客户合规要求时提供 DPA（标准 template，spike 阶段写）
  6. **设计稿 §13.5 必须 inline 重写**为 §13.5 "数据流 + 保留期" 章节

### C-045 [Major] V2.x 内部 rollback SOP 缺位，扩展版本 / DB schema 退到上一版的路径未定义

- **针对**：design v0.4 §12.3 V2→V1 回退路径 + §6.1 milestone 6 "v0.1.0 客户切到 V2 双跑对比稳定性"
- **问题描述**：
  §12.3 设计了 V2→V1 一刀切回退（保留 V1 bundle）。但**V2 内部版本演进**（V2.0 → V2.1 → V2.2）的 rollback 完全没设计：

  **缺失 1：扩展版本 rollback**
  - 客户装 V2.0 → 1 周后 V2.1 release → 客户 update → V2.1 出严重 bug → 想退回 V2.0
  - Web Store 上架的扩展 chrome 自动更新，**没有 rollback 机制**——chrome 总是装最新版
  - 不能让客户重新打包 V2.0 unpacked + Web Store 装，两个版本 ext_id 不同（C-050 同样问题）
  - 客户必须等 V2.2 fix 出来 → bug 期间产能停

  **缺失 2：Co-Pilot rollback**
  - Co-Pilot.exe 升级覆盖安装 → 想退到旧版必须保留旧 .exe + 卸 + 装旧
  - 客户能力上限不太行；Win11 服务管理菜单不是普通用户日常工具
  - V1 时代客户已经踩过升级失败 → 操作员手动卸装恢复（memory: project_v1_delivery）

  **缺失 3：DB schema rollback**
  - V2.1 加新字段（比如 task_results 加 `extension_version` 列）
  - V2.0 schema 不认这字段 → 想退 V2.0 必须 ALTER TABLE DROP COLUMN（SQLite 3.35+ 才支持，客户机版本未知）
  - SQLite 不支持 → 必须 rebuild table → 数据丢失风险

  **缺失 4：扩展 manifest 升级 = 不可逆**
  - V2.1 manifest 加 permission（即使 v0.0.2 C-021 要求"只删不加"，实际 V2 演进难免加）
  - chrome 弹"扩展请求新权限" → 操作员同意 → permission 写入 chrome → rollback 到 V2.0 manifest 没那 permission，但 chrome 已记忆"这扩展曾经有过那 permission"
  - rollback 后 chrome 行为不一致

  **缺失 5：双跑期 dispatcher_kind 切换**
  - §12.2 提了 `dispatcher_kind: 'v1' | 'v2'`，但没 `v2.1 / v2.2 / v2.3` — V2 内部多版本共存场景没设计
  - 客户机 5 工位用混合版本（3 个 V2.0 + 2 个 V2.1）测试时调度怎么办

  **缺失 6：rollback 决策权**
  - rollback 需要 Co-Pilot + 扩展同时降级，不能只降一边（协议不兼容）
  - 客户没能力做协调降级

- **依据**：
  - chrome Web Store 不支持版本 rollback（chrome dev docs）
  - SQLite ALTER TABLE 限制（SQLite docs）
  - V1 升级历史踩坑（memory）
- **影响**：
  - V2.x bug 期间客户停产 → V2 release cadence 必须极保守（跟 v0.0.2 C-021 串联）
  - 客户对 V2 信任度比 V1 低（V1 双击 .exe 装 / 卸方便）
  - V2 内部测试时多版本共存场景跑不动
- **建议方向**：
  1. **新增 §12.6 V2 内部 rollback SOP**：
     - 三种 rollback 类型：扩展 only / Co-Pilot only / 同时
     - 每种的步骤 + 风险 + 数据影响
  2. **扩展 rollback 路径**：
     - Web Store 不支持 rollback → 客户必须**临时切 unpacked**装旧版
     - Co-Pilot 内置"扩展紧急回退"按钮：自动 disable Web Store 扩展 + 装 unpacked V2.0 + 提示操作员每个 profile 重 reload
     - V2.x 旧版本 unpacked zip 保留在 Co-Pilot 安装目录（不删）
  3. **Co-Pilot rollback**：
     - install wizard 备份旧 Co-Pilot.exe 到 `backup/v2.0/`
     - 一键 rollback 脚本：停 Co-Pilot → 替换 exe → 启动
  4. **DB schema 兼容性原则**：
     - V2 schema 演进**只加 NULLABLE 列，不删不改 type**
     - rollback 不需要 schema 降级，旧版 ignore 新列
     - 大型 schema 重构走 migration tool（V1 已有），rollback 路径单独验证
  5. **manifest permission 严格管理**：
     - V2.0 manifest 一次性放足所有未来 1 年用得到的 permission
     - V2.1+ permission 只删不加（C-021 持续生效）
     - rollback 不会撞 permission 不一致
  6. **多版本协议兼容**：
     - WS message envelope 含 `protocol_version: 1` (§4.3 已有)
     - V2.1 加新 message type 但保留 v1 message 处理
     - V2.0 扩展 + V2.1 Co-Pilot 仍能通信（forward compatible）
  7. **rollback 测试 plan**：spike Phase 0.5 加"rollback 演练"——V2.0 → V2.1 升级后 rollback 一次

### C-046 [Major] V2 多扩展并发上报 + scheduler 写 + dashboard 读，SQLite WAL 模式承载力未评估

- **针对**：design v0.4 §4.1 "保留 SQLite schema + migration" + §4.4 task lifecycle 双写（扩展端 chrome.storage + 中控端 SQLite）
- **问题描述**：
  V1 单 worker 时 SQLite 写频率低（worker 内部状态 + 周期性 task_progress upsert）。V2 改成多扩展并发 + 频繁状态上报，**SQLite 并发写承载力没评估**：

  **写压力分析**：
  - 5 工位 × 任务每分钟 task_progress 1 次（heartbeat 30s + state transition 多次） = 至少 60 次/分钟 INSERT/UPDATE
  - 5 工位 × 扩展端 console.log 推送（C-017）每分钟可能 50-100 条 → 250-500 条/分钟 forensic_log INSERT
  - 加 task_complete / task_error / mp4 metadata = 高峰 1000+ 写/分钟
  - V1 同样调度但写少：worker 状态在内存，只是阶段性 upsert
  - V2 双写策略放大写压力 1-2 数量级

  **SQLite WAL 模式分析**：
  - WAL allows 1 writer + N readers concurrent — 5 个扩展 + scheduler + dashboard 并发就是 7 写入源
  - WAL 写仍然顺序（writers 排队），高并发下 lock 等待累积
  - WAL 文件持续增长 → 周期性 checkpoint 必要 → checkpoint 期间锁库
  - 长跑 1 周后 WAL 几 GB 是常见

  **dashboard 读压力**：
  - 操作员开 dashboard → 每 5 秒 ajax 拉 task / workstation 状态
  - 6 个工位卡片 + 任务列表 = 多个 query
  - 高频读 + 高频写 → SQLite cache miss 增加

  **设计稿空白**：
  - §4.1 "SQLite schema + migration ✅ 不动" — 但承载力没新评估
  - §4.4 双写策略加重写压力没量化
  - 没有"写排队 / batch write / flush 周期"设计
  - 没有 WAL checkpoint 策略

  **风险情景**：
  - SQLite "database is locked" 错误（WAL 高并发下罕见但有）→ task 状态写失败 → V2 silent failure 重现（v0.0.2 C-019 / V1 fragility 第 25-28 类）
  - 长跑后 WAL 大 → checkpoint 锁库 30s+ → 期间所有 task_progress 阻塞 → 中控 timeout
  - 客户机 SSD IO 上限 → 高频写撞磁盘瓶颈

- **依据**：
  - V1 forensic_log 增长速度（memory: project_known_fragility）
  - SQLite WAL 模式限制（SQLite docs - "Write-Ahead Logging"）
  - V2 设计 §4.4 双写每个 transition 都同步落盘
- **影响**：
  - 长跑后性能退化（写延迟 + checkpoint 抖动）
  - 高峰期 task_progress 丢 → silent failure
  - dashboard 卡顿（读阻塞）
- **建议方向**：
  1. **写压力削减**：
     - task_progress 不是每个 state transition 写 SQLite — 改 batch（5 秒内的 progress 合并写）
     - 扩展端 console.log 客户端 buffer（chrome.storage.local 累积）+ 5 秒 batch 推送中控
     - 只关键状态 transition 立即写：create_pending / create_committed / round_complete / task_complete / task_error
  2. **WAL checkpoint 策略**：
     - 设置 PRAGMA wal_autocheckpoint=1000（默认就是 1000 page，但要确认）
     - dashboard 低峰期（凌晨 3am）跑 PRAGMA wal_checkpoint(TRUNCATE) 强制 checkpoint
     - WAL 文件大小 > 100 MB 告警
  3. **forensic_log 分表**：
     - 按日 partition forensic_log_20260509 / forensic_log_20260510 / ...
     - 30 天后 DROP TABLE 老分区（C-037 串联）
     - 单表大小可控
  4. **SQLite busy_timeout**：
     - PRAGMA busy_timeout=5000 (5 秒) 让并发写有 retry 机会
     - 应用层捕获 OperationalError 自动 retry 3 次
  5. **read replica（V3 预演）**：
     - V2.0 不做，但 schema 设计预留：dashboard 用 read-only connection（PRAGMA query_only=1）
     - V3 上 PostgreSQL 时切 read replica
  6. **spike 验收新增 #11**：模拟 5 工位 × 1 周连续跑，监测 SQLite 写延迟 / WAL 大小 / locked 错误率

### C-047 [Major] Veo / Flow 后端契约变更监控未设计，Google 改 Flow UI / 加 captcha / 改 generation 显示 → V2 突然挂

- **针对**：design v0.4 §6.2 风险登记册 "Google 检测扩展层（未来）低/高" + memory: project_known_fragility（V1 6 轮迭代踩坑都源于 Google 后端契约变化）
- **问题描述**：
  V1 6 轮迭代每一轮都是 Google 改了 Flow UI / 流程导致 V1 fail。memory 已经记录这是结构性风险。V2 设计**完全没有"Google 改 contract → V2 检测 + 响应"机制**：

  **缺失 1：变更监控**
  - V1 没监控只能等客户复现 → 1-3 天后才发现 Google 改了
  - V2 同样问题 + 客户机分散 → 5 个工位有的撞了有的没撞 → 误判"个别工位问题"
  - 设计稿没"金丝雀监控":定期跑健康检查任务证明 Flow contract 没变

  **缺失 2：变更检测信号**
  - Flow UI 改了：button 文本 / DOM 结构 / class name / Veo 后端 API path
  - V1 各种 selector 失败的"silent timeout"才能间接发现
  - V2 改进：扩展端 fail-fast + 截图，但**没有 contract drift 早期信号**

  **缺失 3：自动适配机制**
  - V1 每次 selector drift 都需要发版本（v0.0.4 13 语言 / v0.1.0 操作员切英文）
  - V2 是否能远程下发 selector 配置（不重发版）？设计稿没说
  - 完全靠 V2 release 跟进 = release cadence 必然降低（v0.0.4 C-045 rollback 痛 + v0.0.2 C-021 升级痛）

  **缺失 4：onboarding flow 变化**
  - Google 偶尔在 Flow 流程加新 onboarding step（"欢迎使用新版"弹窗 / cookie consent）
  - V2 扩展不识别 → 卡在 onboarding 不进 main flow
  - V1 v0.1.0 popup-dismiss 处理过同类（memory）但每次新弹窗都要补 selector

  **缺失 5：captcha 挑战**
  - Google 偶尔触发 captcha（特别是 unusual_activity 后）
  - V2 扩展不可能解 captcha（违反 TOS + 技术不可行）
  - 但 V2 应**识别出"撞 captcha"** → mark task `captcha_required` → 通知操作员
  - 设计稿 ErrorType enum 没列 captcha

  **缺失 6：API path / response shape 变化**
  - 假设 §4.2 假设 3 "拦截 fetch / XHR" 在 CSP 下能跑（C-041 验证后），Veo 后端 API path / response shape 一变 V2 就挂
  - 没监控变更
  - 客户机分散 = 多账号撞 → 集中监控才能区分"账号问题"vs"contract drift"

- **依据**：
  - V1 6 轮迭代每轮都源于 Google contract 变化（memory: project_v1_delivery）
  - Veo / Flow 没公开 API 文档 → 契约稳定性纯靠观察
  - V2 ErrorType enum 没含 captcha / contract_drift（design §4.3）
- **影响**：
  - V2 release 后被 Google 改 break → 客户停产几天等 fix
  - 多客户机分散撞同一 contract drift → 误判"个别问题"延迟发现
  - 操作员频繁报问题，作者搞不清"账号 ban"还是"contract 变"
- **建议方向**：
  1. **金丝雀监控任务**：
     - V2 内置每日"healthcheck"任务：1 个固定 SKU × 1 个测试账号 × 简单 prompt
     - 每天凌晨自动跑，结果上报中控
     - 失败 → 中控告警"contract 可能变了"
  2. **客户机汇总监控**：
     - 多客户机 forensic_log 中"unknown error type"频率上升 → 高度怀疑 contract drift
     - dashboard 显示"近 24 小时 unknown error 趋势图"
  3. **远程 selector 配置**：
     - selector 字典放中控 SQLite + 启动时下发扩展（不打包扩展）
     - selector 出错 → 中控更新字典 → 推送扩展 → 不重发版本
     - 设计稿 §4.3 加 `selector_config_update` message
  4. **ErrorType 补 captcha + contract_drift + onboarding_required**：
     - 扩展检测特定 page state → 上报具体类型
     - 中控按类型分流处理（captcha → 操作员介入；contract_drift → 升级 selector；onboarding → 自动 dismiss）
  5. **变更触发的 hot fix flow**：
     - Co-Pilot 内置"selector 字典 hot update" 通道
     - 作者 push 新字典 → cloudflared 隧道传 → 即时生效不用 release
  6. **新增 §16 后端契约监控章节**

### C-048 [Major] 扩展自身 update 期间任务在跑的处理未设计，chrome 平滑切 sw + 旧 sw 持有的 WS 状态 race

- **针对**：design v0.4 §4.4 task lifecycle + §5.3 选项 A Chrome Web Store 自动更新
- **问题描述**：
  Web Store 自动更新行为：chrome 后台下载新版 → 等待时机 → kill 旧 sw → load 新 sw → content script **不**自动重 inject（仍旧版本，直到 page reload）。这导致 V2 状态 race：

  **场景 1：扩展 update 期间任务跑中**
  - V2.0 任务跑到 round 3，chrome 后台下载 V2.1
  - chrome 选时机 swap sw → 旧 sw kill 期间持有的 WS 连接断
  - 新 sw 起 → 但 content script 还是 V2.0 版本（page 没 reload）
  - V2.1 sw + V2.0 content script 协议可能不一致 → 行为乱

  **场景 2：sw 状态丢**
  - 旧 sw kill 时 chrome.storage.local 数据保留（chrome 设计），但内存状态全丢
  - 新 sw 启动 → 读 chrome.storage 拿 task_state → 看似能恢复
  - 但 WS 重新连 → 中控收到 register → 认为 ws 重新 online → 派新 task or resume 旧 task？
  - design §4.4.3 reconnect 策略覆盖了这个，但**协议版本不一致情况没考虑**

  **场景 3：content script 版本 skew**
  - chrome 不会主动 reload page 让 content script 更新 — 用户必须刷新 page
  - 操作员不知道要 reload；继续看现有 tab 跑
  - V2.1 sw + V2.0 content script 跑混合版本几小时
  - 有的 task 用旧 content script 协议（兼容），有的撞 bug

  **场景 4：manifest permission 升级**
  - V2.1 加新 permission → chrome 弹"扩展请求新权限" → 用户必须点允许
  - 用户点允许期间 sw 已切到 V2.1
  - 用户拒 → chrome disable 整个扩展 → 任务全停（C-021 + C-048 串联）

  **场景 5：unpacked update（fallback 路径）**
  - V2 必须支持 unpacked 备份（C-042）
  - unpacked 升级 = 操作员手动 chrome://extensions reload → 期间 sw + content script 同时 kill → page reload 后重新 inject
  - 跑中任务直接断（content script 持有的 page state 丢）
  - V2.0 → V2.1 升级 = 手动操作 = 操作员要选低峰时间

- **依据**：
  - chrome 扩展 update 行为（chrome dev docs - "Updating extensions"）
  - chrome.runtime.onUpdateAvailable / onInstalled lifecycle
  - manifest permission upgrade 行为（C-021）
- **影响**：
  - 任务跑中 chrome 后台 update → silent 跑挂（chunked sw 状态）
  - V2.1 V2.0 协议混跑几小时跑出脏数据
  - permission 升级被用户拒 → 全停
- **建议方向**：
  1. **延迟 update 直到 task idle**：
     - chrome.runtime.onUpdateAvailable 事件 → sw 检查 active task → 有任务跑 → `chrome.runtime.reload()` 不立即调
     - 等所有 task 完成或 idle 1 小时 → 主动 reload sw 装新版
  2. **content script 版本同步**：
     - V2.1 sw 启动时 broadcast 给所有 active tab "请 reload page" 消息
     - tab content script 收到 → location.reload() 同步到新版本
     - 缺点：操作员看到 page 闪一下；优点：版本一致
  3. **协议向前兼容**：
     - V2.1 message schema 必须接受 V2.0 message 形态（不破坏字段）
     - V2.1 sw 在内部识别 V2.0 content script，自动 fallback 旧协议
  4. **permission 不主动升级**：
     - V2.0 manifest 一次性放足（C-021 持续生效）
     - V2.x 升级期间不加 permission → 不触发用户对话框 → 不会被拒
  5. **update 发生时 task 状态保护**：
     - sw onSuspend 事件（chrome 关闭 sw 前 30s 给的 grace）→ 强制 flush chrome.storage.local 写
     - 新 sw 启动检测"上次没正常 shutdown" → mark active task `interrupted` 跟中控对账
  6. **unpacked 升级时机告知**：
     - Co-Pilot 升级助手（v0.0.2 C-021）告诉操作员"建议在 dashboard 全 idle 时点升级"
     - 自动检测 idle 时间 → 大于 30 分钟才允许触发升级
  7. **新增 §4.7 扩展自身 update lifecycle 章节**

## 三、Minor（v0.0.4 新增）

### C-049 [Minor] NTFS 文件名 unicode / 特殊字符 / 260 路径长度限制未考虑

- **针对**：§4.3 文件传输方案 A `filename: "FlowHarvester/output/${date}/${sku}/${creative}/segment_${seg}/${filename}"` + V1 customer SKU 可能含中文 / 特殊字符
- **问题**：
  - NTFS 不允许文件名含 `: < > | / \ ? *` （chrome 自动 escape，但 escape 后名字可能跟预期不符）
  - Windows 路径长度默认 260 字符限制（chrome.downloads 走 chrome 自身限制 + Win API 限制）
  - SKU 中文 / Unicode 字符 → 落盘 OK 但 chrome.downloads.search 返回的 filename encoding 可能跟预期不符
  - V1 customer 如果 SKU 名是 "Customer A's Product (2026/Q1)" → chrome 会 escape 成 `Customer A_s Product _2026_Q1_` → 跟 Co-Pilot DB 期望不符
- **建议**：
  - filename normalize：所有 SKU / creative 字段先过 sanitize（替换 NTFS forbidden chars + 截断 100 字符）
  - 路径长度检查：组装 filename 后 assert 总长度 < 220（留 40 字节 buffer）
  - 中文 SKU 用 base64 / 拼音替代 落盘，DB 保留原文
  - spike #4 验收新增："含中文 / 特殊字符 SKU 的 filename 落盘正确"

### C-050 [Minor] Chrome Web Store ext_id vs unpacked ext_id 不同 = ws_token / Origin 校验跨发布渠道

- **针对**：§13.2.3 WS Origin 校验 "chrome 扩展会发送 chrome-extension://<id> Origin" + §5.3 选项 A vs C
- **问题**：
  - Web Store 上架 ext_id 是 Google 分配（一旦上架不变）
  - unpacked ext_id 是基于扩展目录路径 hash 生成（可重新生成 / 每个客户机不同）
  - C-042 要求支持双渠道 → 客户机可能用 Web Store 也可能用 unpacked → ws_token / Origin 校验逻辑要适配两种 ext_id
  - 设计稿 §13.2.3 假设 ext_id 固定，但实际跨渠道不固定
- **建议**：
  - manifest 加 `key` 字段（base64 公钥）锁定 ext_id（unpacked 也固定 ID）
  - Origin 白名单含 store id + unpacked id 两个
  - install wizard 自动检测当前装的是哪种渠道 → Co-Pilot 校验对应 ext_id

### C-051 [Minor] Google 账号 30 天 session cookie 过期处理未设计

- **针对**：§6.3 "自动化登录 + 凭证管理（Google 反 bot 太严，不做）" + §5.4 多账号管理
- **问题**：
  - Google 账号 session cookie 大约 14-30 天过期（active 使用会续期，闲置不续）
  - V2 专门账号（C-028 选 A）日常**只跑 V2 任务**，没"真实使用历史" → cookie 不续
  - 30 天后扩展跑任务时 Flow page redirect 到登录 → V2 没法解（自动登录 § 6.3 排除）
  - 操作员手动重新登录 5 个账号一次月度任务 → 每次撞 unusual_activity（多次登录失败）
- **建议**：
  - 扩展检测 redirect 到 login → 上报 `login_required` errortype
  - 中控 dashboard 显著告警 "WS_X 需要重新登录"
  - 操作员 onboarding 文档加"账号 cookie 续期"建议：每周登录一次让 cookie 不过期
  - 长期：考虑 Google App Password / refresh token 机制（V3 探索）

### C-052 [Minor] 多语言 prompt 输入 + IME composition 跟 keyboard.type 节奏冲突

- **针对**：§4.2 内部分工 "content/flow_dom.ts ... 输 prompt" + V1 keyboard.type 60-110ms delay（memory: project_known_fragility 第 5 条）
- **问题**：
  - V1 patchright keyboard.type 直接对 chrome 发字符 event，绕过 IME
  - V2 扩展 content script 用 chrome 标准 input event → 受**操作员 chrome IME 状态**影响
  - 中文 IME 开 → input event 含 composition 阶段 → React onChange 收 composition events 跟 final commit
  - prompt 输入"包含中文 SKU 名"时，IME composition 中间状态可能被 React 提前提交
  - V1 keyboard.type 60-110ms delay 也基于 ASCII 假设，中文输入跟节奏冲突
- **建议**：
  - prompt 输入用 `Object.defineProperty` 直接改 textarea value + dispatchEvent('input', {bubbles: true}) 绕过 IME（v0.0.3 C-029 防御 Grammarly 同样思路）
  - 不用 keyboard.type 模拟（content script 没这 API；要用 InputEvent 模拟）
  - 测试矩阵加：操作员中文 IME 开启状态下跑英文 prompt + 跑中文 prompt
  - 必要时 `compositionstart` / `compositionend` 显式 dispatch

### C-053 [Minor] V2 增加可观测性 → console.log 推送中控 → dashboard 信息过载没控制

- **针对**：§4.3 protocol `log` 消息 + §13 cloudflared 远程访问
- **问题**：
  - 5 工位 × 扩展 console.log 频繁 → forensic_log 表数据爆
  - dashboard 显示所有 log 信息过载，操作员看不出关键
  - 重复 error（同一 selector_drift 连发 100 次）淹没真实新问题
  - cloudflared 远程访问时作者也被信息过载
- **建议**：
  - 中控收 log 自动 dedup：相同 (level, message stem) 5 分钟内 first occurrence + count
  - dashboard log 默认只显示 ERROR + WARN，DEBUG / INFO 收起
  - 高频重复 error 自动收敛成"this error 100 次/min" 单行
  - alert 阈值：error rate > 10/min 红色横幅；持续 30min 邮件告警

---

---

## v0.0.4 修订 — 架构约束更新（2026-05-09）

**新约束**：用户明确"V2 扩展不部署/上传 Chrome Web Store"。`extension/` 只走 unpacked 分发。

### A. 受影响 challenges 状态变化

| Challenge | 修订 | 理由 |
|---|---|---|
| **C-042** [Blocker] Chrome Web Store 下架风险 | **OBSOLETE** | 不上 Web Store 就没 Web Store 下架风险 |
| **C-050** [Minor] ext_id 跨渠道 | **简化** | 只 unpacked，manifest 加 `key` 字段固定 ext_id；不需要双渠道适配，但 ws_token / Origin 校验仍要绑该固定 ID |
| **C-045** [Major] V2.x rollback | **缩减** | "Web Store auto-update 不可 rollback" 段 OBSOLETE；Co-Pilot rollback / DB schema rollback / unpacked 旧版保留 仍有效；rollback = 操作员手动 reload 旧 unpacked |
| **C-048** [Major] 扩展 update race | **缩减** | "chrome 后台自动 update 期间 sw + content script 协议混跑" 场景 OBSOLETE；unpacked 手动 reload race + chrome 主版本 update disable unpacked 仍有效 |

### B. 风险加重 challenges（unpacked-only 放大原痛点）

| Challenge | 加重 | 原因 |
|---|---|---|
| **C-008** [Major → **Blocker**] chrome update disable unpacked | 严重度升 | 唯一分发路径靠 unpacked，chrome 主版本 update 后所有客户机 V2 全停摆 = 单点故障；没有 Web Store 备份 |
| **C-021** [Major] manifest 升级摩擦 | 加重 | unpacked 没 chrome auto-update，每次 V2.x release = 操作员手动 5 profile reload；release cadence 极保守也是每月一次 = 客户负担 |
| **C-027** [Blocker] license 模型 | 略加重 | unpacked 扩展明文 JS + 任意拷贝；license 全靠 Co-Pilot 端校验 + 扩展端 license signature 防御薄；machine binding 必做 |

### C. 新增 challenges（unpacked-only 触发）

#### C-054 [Blocker] unpacked 是 V2 唯一分发路径 = chrome 主版本 update 单点故障

- **针对**：design v0.4 §5.3 选项 C + §6.2 风险登记册 + 新约束（无 Web Store fallback）
- **问题描述**：
  之前 v0.0.4 C-042 论证不能 single-bet Web Store 时给出 "unpacked 备份" 假设。新约束反过来：unpacked 是**唯一**分发路径，没有 Web Store 备份。chrome 主版本升级（每 4 周）有时 disable 所有未签名 unpacked 扩展 → 5 工位同时停摆 → 客户产能瞬间归零，没有备份恢复路径。
  - V1 patchright 用固定 chrome binary（patchright 自带 chromium），客户 chrome 主版本升级影响小
  - V2 跑客户日常 chrome → chrome 升级 = V2 也升级 = 高频暴露
  - chrome enterprise GPO 禁 unpacked 的客户机直接装不了
  - chrome 88+ 起 "You are using extensions that are not from the Web Store" 警告 banner 长期存在
  - 如果 chrome 未来加严（彻底禁未签名 unpacked），V2 整个架构崩
- **依据**：
  - chrome stable 4 周 cadence（chrome release schedule）
  - chrome update 行为对 unpacked 不友好（chrome 安全策略）
  - 用户决策不上 Web Store → 没 fallback 渠道
- **影响**：
  - 每 4 周 chrome 主版本 → V2 整盘停摆窗口（可能 1-2 天恢复）
  - 客户企业机 GPO 限制 → 部分客户无法部署
  - chrome 长期政策收紧 → V2 架构生命周期受限
  - 客户问题"为啥 V2 突然不工作了" 答案是"chrome 升级了"——客户体验差
- **建议方向**：
  1. **chrome 升级监控 + 提前告知**：
     - Co-Pilot 每日检查 chrome stable next-version；下周升级前 7 天告警操作员
     - 中控 dashboard 横幅"chrome 即将升级，建议测试机先验证"
  2. **chrome 升级后自动 disable 检测 + Co-Pilot 一键 reload 助手**：
     - Co-Pilot 用 chrome DevTools Protocol 探测扩展状态（need chrome `--remote-debugging-port`）
     - disabled → guided wizard 带操作员每个 profile 重新 reload
     - 升级窗口期间任务自动 mark `paused`，恢复后自动 retry
  3. **客户机 chrome 锁定版本**（chrome enterprise）：
     - 如客户能接受不自动升级 chrome（部分企业策略允许）
     - 锁在 chrome 117+（C-031 兼容下限）某稳定版本
  4. **测试机先升级**：
     - V2 部署架构含 1 台测试机用 chrome beta channel
     - 提前 4 周看升级影响 → 来不来得及发 V2 patch
  5. **替代方案探索**（spike 阶段研究）：
     - **Edge / Brave / Vivaldi 等 chromium-based** 浏览器对 unpacked 是否更宽松
     - chrome enterprise policy `ExtensionInstallSources` 白名单 — 客户机一次配置永久有效，但需要 GPO/registry 改动
     - 自签名 .crx + chrome 命令行 `--load-extension=` flag（操作员每次启动 chrome 需要带 flag）
  6. **新增 G10 立项 gate**："客户机 chrome 升级管控"——客户能否锁版本 / 接受 update 期间停摆
  7. **§6.2 风险登记册"扩展更新困难"**升 **极高/极高**

#### C-055 [Major] unpacked-only 没自动分发渠道 = 扩展 / Co-Pilot 版本一致性管理全靠手工

- **针对**：design v0.4 §13.2.3 ws_token + §4.5.2 setup wizard + §4.3 protocol_version + 新约束
- **问题描述**：
  Web Store 路径下 chrome auto-update 保证扩展跟 Co-Pilot 协议版本同步；unpacked-only 失去这层保障，必须重新设计版本一致性管理：

  **场景 1：协议版本 mismatch**
  - 客户 Co-Pilot 升 v2.1（含新 message field），扩展仍是 v2.0（不识别新 field）
  - WS 通信协议 v0.4 §4.3 标了 `protocol_version: 1`，但 v2 进 v2.1 是否升 protocol 没说
  - 客户机 Co-Pilot v2.1 + 扩展 v2.0 跑混合 → 行为乱

  **场景 2：5 profile 扩展版本不一致**
  - 客户机 5 profile 各自 unpacked 装扩展，每次 V2.x release 操作员要 5 次 reload
  - 操作员可能漏装 1-2 个 profile → 该 profile 仍跑旧版扩展
  - dashboard "5 工位 online" 看不出版本差异 → silent inconsistency

  **场景 3：ws_token 持久化跨 reload**
  - setup wizard 输 ws_token 存 chrome.storage.local
  - unpacked reload 时 chrome 行为：**保留 chrome.storage**（chrome 设计如此），token 不丢
  - 但 chrome 用户手动"清除浏览数据" → chrome.storage 清空 → token 丢 → 操作员要重新 setup wizard
  - 操作员不知道为啥扩展突然 register fail

  **场景 4：升级期间任务跑中**
  - 操作员手动点 reload extension 时正在跑任务
  - sw kill + content script 跟 page 一起 die → 任务丢
  - 没有"等 idle 再升级"提示

  **场景 5：测试机 vs 生产机版本一致性**
  - 作者推 V2.1 release，5 个客户机分批升级（避免全停摆）
  - 期间作者跑 V2.1 测试，客户跑 V2.0 → 同样的客户问题作者没法本地复现

- **依据**：
  - chrome auto-update 不适用 unpacked
  - chrome.storage 行为（reload 不清，手动清除浏览数据清）
  - V1 release 节奏（3 天 6 版）在 V2 unpacked 下不可能
- **影响**：
  - 5 profile 版本不一致 silent failure
  - V2.x release 摩擦极高 → 升级延迟 → bug 修复堆积
  - 客户支持复现困难（版本 skew）
- **建议方向**：
  1. **Co-Pilot 主动版本检查**：
     - 扩展 register 时上报 extension_version + manifest version
     - Co-Pilot 跟自己 version 比对 → mismatch reject + dashboard 红色告警 "WS_X 扩展版本 v2.0 vs Co-Pilot v2.1，请 reload 扩展"
     - mismatch 期间不派 task
  2. **统一 release zip**：
     - V2.x release zip 含 Co-Pilot.exe + extension/ 同 version 同时升
     - 客户必须**同时**升级 Co-Pilot + 全部 5 profile 扩展，部分升级 = 拒绝跑
     - install.bat 自动检测当前 version 并 guide 升级
  3. **升级助手**：
     - Co-Pilot dashboard 加 "升级到 v2.x" 按钮
     - 检测每个 profile 扩展状态 → step-by-step 指导操作员每个 profile reload
     - 自动检测 idle 时间 > 30 分钟才允许升级
  4. **协议版本兼容**：
     - WS message envelope `protocol_version` 字段每次升级递增
     - 扩展 register 上报 `protocol_version` → Co-Pilot 拒绝接受 protocol < min_supported
     - V2.0 扩展 + V2.1 Co-Pilot 协议兼容设计：v2.1 必须接受 v1 protocol message（forward compat）
  5. **chrome.storage 持久化策略**：
     - ws_token 同时存 `chrome.storage.local` + 本地文件 `%APPDATA%\FlowHarvester\extension_token_<profile>` (Co-Pilot 写)
     - 扩展 register 时 chrome.storage 没 token → fetch localhost:8080/extension/token 取 → 自动恢复
     - 操作员清浏览数据后扩展自愈，不用重做 setup wizard
  6. **升级前 idle check**：
     - 操作员点 reload extension 前，扩展 popup 显示 "当前任务 X 个跑中，建议 X 分钟后升级"
     - 强制 reload 时跑中任务 mark `interrupted` 等下次 resume

### D. 仍有效但加备注的 challenges

| Challenge | 备注 |
|---|---|
| C-008 chrome update disable unpacked | 跟 C-054 串联，事实上 C-054 是 C-008 在 unpacked-only 约束下的具体化升级 |
| C-021 manifest 升级摩擦 | "permissions 一次性放足" 比 Web Store 路径更重要——unpacked 升级 permissions 触发的对话框比 Web Store 还烦 |
| C-031 chrome 版本兼容矩阵 | unpacked-only 下 chrome 升级直接破坏分发，G9 chrome 版本审计 + chrome 升级监控更关键 |

---

---

# v0.0.5 — 第五轮 challenge（针对 design v0.5 inline 集成版 + unpacked-only 约束）

design v0.5 已 inline 集成 v0.0.1-v0.0.4 共 53 条 challenges + 修订段 unpacked-only 反转。本轮聚焦 **spike 阶段实操可行性 / 客户参与机制 / chrome enterprise GPO 阻塞 / 测试基础设施 / 多 chrome window / WS 网络层 / cancel 彻底性 / 性能开销 / 运营成本 / i18n / 数据备份**。

## 一、Blocker（v0.0.5 新增）

### C-056 [Blocker] PoC 客户参与机制完全没设计 — 11 个立项 gate 中 5 个需要客户配合，但客户已"V1 v0.1.0 交付完成"，配合意愿 / NDA / 时间窗口 / 收费 / 责任 全部空白

- **针对**：design v0.5 §7.4 立项 gate G1-G10（其中 G1 / G2 / G7 / G8 / G10 涉及客户）+ §7.3 spike 验收 #7（客户机 RAM 测试需要客户）+ memory: project_v1_delivery（客户已"交付"状态）
- **问题描述**：
  V1 v0.1.0 已经"交付完成 + 操作员驱动账号语言切换"——客户跟作者的合同关系停留在 V1 验收。V2 立项需要客户重新参与，但**这个新关系完全没在设计稿讨论**：

  **缺失 1：客户配合的 gate 太多**
  - G1 客户接受 chrome 必须开 + 5 profile 长开 → 访谈
  - G2 客户机 RAM ≥16 GB → 客户机审计
  - G7 unpacked 分发摩擦 → 客户接受手动 reload SOP（约束更新后变 unpacked-only，G7 改为讨论 chrome enterprise GPO + chrome 升级管控）
  - G8 客户使用专门账号 / 日常账号 → 访谈 + 账号策略制定
  - G10 chrome 升级管控 → 客户机 chrome 版本审计 + 升级策略
  - spike #7 客户机 5 profile 并发 RAM 实测 → 客户配合远程或现场跑测试
  - 5 个 gate + 1 个 spike 全部依赖客户配合，作者无法单方面推进

  **缺失 2：客户为啥要配合**
  - V1 已交付 → 客户视角"我付了钱，工具能用就行，为啥要花时间陪你研究 V2"
  - V2 给客户的好处：locale 不需要操作员介入 + anti-bot 改善（待证）+ 调试更好——但**客户当前没痛点**（V1 v0.1.0 操作员切英文已解 locale；anti-bot 强度跟 V1 本质相同 per C-028）
  - 客户配合 V2 PoC = 投入操作员时数 + 客户机 PC 资源 + 账号风险 → 没有正向 ROI
  - 不收费 → 客户没动力；收费 → 商业关系变化未谈

  **缺失 3：NDA / 数据保护**
  - spike 验证 #2 越南语账号跑 Flow → 用客户哪个账号？账号 ban 风险谁担
  - spike #7 客户机 RAM 实测 → 作者远程访问客户机做测试 → 商业秘密 / 客户业务数据可见 → NDA / 数据处理协议必要
  - 设计稿 §13.5 GDPR 章节没提"作者 - 客户"的合规边界（只提 V2 软件本身）

  **缺失 4：时间窗口冲突**
  - spike 5-7 天（design §7.3）— 客户操作员日常跑 V1 任务，没时间陪 spike
  - 客户 5 profile 长跑 V1 不能停（业务需求）→ spike 必须用客户**额外**资源（额外账号 / 额外 PC）
  - 客户没承诺这个

  **缺失 5：失败责任**
  - V2 spike 中如果客户账号被 ban / 客户机崩 → 谁赔
  - V1 是稳定产品出问题作者负责修复；V2 spike 是"实验性投入"，责任未划

  **缺失 6：spike 失败的客户出口**
  - 11 gate 中任何一个 fail（如客户 PC RAM 不够 / chrome 升级不可控 / 账号策略客户拒）→ V2 立项失败
  - 客户已经投入时间陪 PoC，怎么"补偿"？设计稿没说

- **依据**：
  - design §7.4 G1-G10 gate 涉及客户的有 5 个
  - V1 v0.1.0 状态见 memory: project_v1_delivery
  - 客户能力上限见 customer-manual / install-windows
- **影响**：
  - V2 立项无限期 pending（客户不配合 → gate 永远 ⏳）
  - 强行推进 → 客户敷衍参与 → spike 数据不可信 → 立项决策错
  - 客户出账号问题 / 数据问题 → 法律风险归属不清
  - V2 整个项目可能因为"没人陪 PoC"无法启动
- **建议方向**：
  1. **新增 §18 客户协作框架章节**：
     - 谁是 PoC 客户（V1 现有客户中选 1-2 个最配合的，or 新发展 1 个 V2 lead customer）
     - PoC 周期（spike 阶段）+ 客户投入承诺（操作员 X 小时 / 客户机访问 / 账号 N 个）
     - 商业关系：免费 PoC 换早期 V2 折扣 / V2 发展期客户共建权益 / 收费咨询模式三选一
     - NDA + DPA template
  2. **gate 拆分客户依赖度**：
     - 完全独立验证（spike #1 sw / #2 locale / #4 200MB 文件 / #5 PoC ext / #6 Veo idempotency / #8 误配检测）→ 作者自己测试机
     - 必须客户配合（G1-G2-G7-G8-G10 + spike #7）→ 单独 milestone "客户协作期"，不阻塞独立验证
  3. **spike 阶段拆分**：
     - **Phase A（独立）**：作者测试机跑 spike #1/2/4/5/6/8 + 建 V1 baseline → 5-7 天
     - **Phase B（客户协作）**：客户机 PoC 跑 spike #7 + G1/G2/G7/G8/G10 客户访谈 → 1-2 周
     - 两 Phase 异步进行，Phase A 失败也不浪费 Phase B 客户时间
  4. **客户出口预案**：
     - V2 spike 失败 → 作者承担 PoC 时间损失 + 客户继续用 V1
     - V2 spike 成功但客户不愿切 → V2 找新客户首发，V1 老客户保留
  5. **V2 价值再定位**：
     - 不强调"客户切换 V1→V2"，强调"V2 是新客户首发版本，V1 老客户自愿升级"
     - 减少老客户配合压力
  6. **设计稿 §6.1 milestone 必须 inline 加 "客户协作期" 阶段**

### C-057 [Blocker] chrome enterprise GPO / 公司机 IT 政策可能完全禁 unpacked + 没 Web Store fallback = V2 整套部署 dead

- **针对**：design v0.5 §5.3 unpacked-only commit + §6.2 风险登记册 + 客户机部署假设
- **问题描述**：
  约束更新后 V2 单一分发路径是 unpacked。但客户机如果是公司机 / 受 IT 管控的 PC，**公司 GPO 可能彻底禁 unpacked 扩展**：

  **GPO 禁用方式**：
  - `ExtensionInstallBlocklist` = `*` → chrome 拒绝任何非白名单扩展（包括 Web Store + unpacked）
  - `BlockExternalExtensions` → 禁 chrome://extensions 开发者模式
  - `DeveloperToolsAvailability` → 禁开发者工具（含开发者模式扩展加载）
  - `ExtensionInstallSources` 白名单 only → unpacked 装不了
  - `ExtensionAllowedTypes` 不含 "extension" → 禁所有扩展
  - chrome 启动加 `--disable-extensions` flag（GPO 可强制）

  **客户机现实**：
  - V1 客户场景：5 个 Flow 账号 × 一台 Win11 PC（memory: project_v1_delivery）
  - 这台 PC 是个人机还是公司机？memory 没明说，但客户业务是商业的 → 大概率公司机
  - 公司机 IT 政策严的话，V1 patchright 也可能被拦（但 patchright 跑独立 chrome，受影响相对小；V2 跑客户日常 chrome，受影响最直接）
  - 客户公司 IT 不会为了 V2 改全公司 GPO
  - V1 时代客户能装 patchright 不代表 V2 能装扩展——chrome 扩展 GPO 跟 exe 安装权限是两套

  **现有设计 §5.3 缓解措施局限**：
  - 选项 B "Chrome Enterprise Policy" 客户配合度要求高（要懂 GPO / registry 改动）
  - 选项 C unpacked 就是直接撞 GPO
  - 没有 fallback——之前 v0.0.4 C-042 假设 Web Store 是备份，约束更新后这条路堵死
  - 跟 C-008 / C-054 串联：单点故障风险叠加 GPO 阻塞

  **检测时机**：
  - install wizard 没说怎么检测 chrome 是否受 GPO 管控（chrome://policy 显示 GPO 状态，但没扩展可读 chrome://policy）
  - 操作员可能在装到一半才发现"加载已解压"按钮不能点 → 时间已浪费

- **依据**：
  - chrome enterprise policy 完整列表（chrome 官方文档 - Chrome Enterprise）
  - 客户业务是商业用途暗示公司机（memory: project_v1_delivery）
  - V2 设计 §5.3 选项 A 已 OBSOLETE（Web Store 反转）
  - chrome 88+ 后 unpacked 警告对个人机就有，公司机往往直接禁
- **影响**：
  - V2 整套部署在 GPO 受控客户机上**完全失败**——不是部分失败是完全
  - 客户公司 IT 拒改 GPO → V2 在该客户机永远部署不了
  - 客户分布广 → V2 总体部署率可能远低于预期
  - 客户切到 V2 才发现部署不了 → 浪费迁移成本 + 客户信任
- **建议方向**：
  1. **install wizard 必检 GPO 状态**：
     - 启动时调 chrome 命令行 `chrome --disable-extensions=false` + 探测扩展加载是否成功
     - 读 Win11 注册表 `HKLM\SOFTWARE\Policies\Google\Chrome\` 检查相关 GPO 设置
     - 检测到禁 unpacked → install wizard 直接 fail + 给出 GPO 修改指南
  2. **客户公司 IT 协作 SOP**：
     - 提供 GPO 配置模板（客户 IT 一次性配置）
     - 配置项：`ExtensionInstallSources` 白名单 V2 安装路径 + 关 `BlockExternalExtensions`
     - 客户不能改 GPO → V2 不部署该客户
  3. **chrome 命令行 fallback**：
     - V2 用 chrome `--load-extension=` flag 启动（命令行加载扩展）
     - chrome_profile_launcher.py 启动 chrome 时带这个 flag
     - 部分 GPO 可能仍允许命令行 `--load-extension`（GPO 不一定 cover 所有路径）
     - 但操作员开 chrome 必须经 Co-Pilot launcher，不能直接双击 chrome 桌面图标——客户体验差
  4. **替代浏览器**：
     - Edge / Brave / Vivaldi（chromium-based）是否受同一 GPO 管控？很多公司 IT 只 GPO 控 chrome，不控 Edge
     - 但 Edge 自动化不在 V1 / V2 实战范围
     - 升级 G10 包含"客户机可用浏览器审计"
  5. **新增 G11 立项 gate "客户机可装 unpacked 扩展验证"**：
     - 客户机部署前必检 GPO 状态
     - 不可装 → V2 不部署
  6. **设计稿 §5.3 必须 inline 加 GPO 检测 + 失败处理**

## 二、Major（v0.0.5 新增）

### C-058 [Major] 测试基础设施零 — 测试机 / chrome beta channel / Veo 测试账号 / 测试任务集 / 网络环境 全部需要从零搭，spike 阶段时间表没含这部分

- **针对**：design v0.5 §7.3 spike 验收 + §15 chrome 兼容矩阵 + §17 后端契约监控 + design §17 "测试机用 chrome beta channel 提前 4 周看升级影响"
- **问题描述**：
  设计稿多处提到"测试机"作为 spike + chrome 升级监控基础设施，但**测试机怎么搭、怎么维护、谁付钱、用什么账号**全部空白：

  **缺失 1：测试机硬件 / OS / 网络**
  - §15 chrome 版本兼容要求测 chrome 117 / 124 / 130，3 个 chrome 版本 → 3 台 Win11 测试机？还是 1 台多版本切换？
  - chrome 多版本共存技术上可行（用不同 install path）但运维复杂
  - 测试机必须是 Win11（V1/V2 客户都是 Win11） → 不能用 macOS 测
  - 网络：Veo 后端访问可能受地区限制 → 测试机网络环境必须跟客户机相似

  **缺失 2：Veo 测试账号**
  - spike #2 验证 locale 用越南语账号 → 谁开越南账号？账号实名 / 手机号 / 信用卡需要
  - spike #6 验证 idempotency 连续 click Create → 消耗 Veo 配额，要付费 Google 账号
  - spike #7 客户机 RAM 测试要 5 个测试账号
  - 至少 5 个 Veo 测试账号（含越南语 / 中文 / 阿非利卡 / 英文 / 主测） → 月度成本 $50-200/账号 × 5 = $250-1000/月
  - 设计稿没列这部分预算

  **缺失 3：chrome beta channel 测试机**
  - C-054 + design §17 commit "测试机 chrome beta channel 提前 4 周验证升级"
  - chrome beta 比 stable 早 4 周 → V2 必须在 beta 测试机持续跑测试
  - 这个测试机需要永久运行 → 一个独立 PC + 网络 + 监控 + 维护
  - 没说谁负责

  **缺失 4：测试任务集**
  - V1 baseline + V2 验收都需要"固定测试任务集"（design §7.1）
  - 任务集需要：固定 SKU / prompt / asset 图 / 账号映射
  - 谁建？什么时候建？data 在哪存？版本管理？
  - 设计稿 §7.1 提了"docs/v2-baseline.md 含 baseline 数据"——但 baseline 数据集本身没说

  **缺失 5：CI 跑扩展 e2e（v0.0.2 C-026 + v0.0.3 partial）**
  - playwright + chrome --load-extension 是常规方案
  - 但跑 e2e 需要真实 Veo 后端 or mock
  - 真实 Veo：消耗配额 + 不稳定（Google 改 UI）
  - mock Veo：要建 fixture（V1 fragility 35 条 reproducer，C-032 关联）
  - GitHub Actions runner 跑 chrome e2e：网络 / 账号 / 资源限制

  **缺失 6：spike Phase 0.5（C-032）测试 fixture**
  - design §6.1 加了 Phase 0.5 V1 fragility 知识沉淀 1 周
  - 但 35 条 fixture（DOM snapshot + 网络 stub + 时序）人 1 周写得完吗？
  - 每条 fixture 平均 2-4 小时 → 35 × 3h = 100h+ → 1 周 40h 远不够

- **依据**：
  - design §15 / §17 / §6.1 Phase 0.5 提到测试基础设施
  - V1 没专用测试机（memory）
  - GitHub Actions chrome e2e 是已知模式但跑 Google 服务受限
- **影响**：
  - spike Phase A（独立验证）实际启动延后——先要建测试机 / 注册账号 / 写 fixture
  - 测试基础设施成本（hardware / 账号 / 月度运维）没在 V2 商业模型里
  - 没测试基础设施 → spike 验证靠手动 → 数据不可重现 → 决策依据弱
  - 后续 V2.x release cadence 严重受限（每次 release 都靠手动测）
- **建议方向**：
  1. **设计稿新增 §19 测试基础设施**：
     - 列硬件清单（测试机 N 台 / OS / chrome 版本）
     - 测试账号清单（语种 / 配额 / 付费方）
     - CI runner 选型（GitHub Actions / self-hosted）
     - Fixture 库管理（Git LFS / 独立 repo）
  2. **测试机最小集**：
     - 1 台主测试机 chrome stable 117（兼容下限）
     - 1 台 chrome beta channel 永久跑（提前 4 周看升级）
     - 1 台客户机镜像（client-spec PC，Win11 + 8 GB RAM 模拟客户低端机）
     - 总计 3 台 PC + 网络
  3. **测试账号策略**：
     - 主测试账号 1 个（含 Veo 配额）
     - 多语种账号 4 个（越南 / 中文 / 阿非利卡 / 日 — 简化版 i18n 矩阵）
     - 月度成本 $300-500，列入 V2 项目预算
  4. **Fixture 渐进式建立**：
     - Phase 0.5 不可能 1 周写完 35 条
     - 先 priority top 10 fragility（影响最大的）→ 1 周内
     - 剩下 25 条在 V2 实施期间逐步补
  5. **CI 选型**：
     - GitHub Actions + self-hosted runner（在测试机上跑） → 解决 Google 网络限制
     - 或：playwright + mock Veo（fixture 库），不依赖真实后端
  6. **测试基础设施建立必须排在 spike 之前**：
     - 设计稿 §6.1 加 **Phase -1 测试基础设施搭建**（1-2 周）→ Phase 0.5 fragility 沉淀（1 周，部分）→ Phase 0 spike

### C-059 [Major] 多 chrome window 同 profile 行为 — 操作员同 profile 开 2 个 chrome window，扩展在两个 window 都 inject，task_assign 派给哪个未定义

- **针对**：design v0.5 §4.6 多 tab 管理 + §5.1 操作员日常流程 + chrome multi-window 用户行为
- **问题描述**：
  §4.6 设计了"多 tab 管理"——但多 chrome **window** 同 profile 是 chrome 用户日常常见行为，跟多 tab 不同：

  - 操作员开 chrome window A 跑 V2 任务（Profile A）
  - 操作员同时**新开一个 chrome window**（同 Profile A） → chrome 启动后第二个 window 共享同一 profile
  - 两个 window 都加载 labs.google → V2 content script 在两个 window 都 inject
  - sw 不区分 window，认为是同一个工位
  - chrome.tabs.query({url: 'labs.google/*'}) 返回**两个 window 的所有 Flow tab**
  - §4.6 active_flow_tab_id 单一指向，但实际可能两个 window 都有 active tab

  **场景细节**：

  **场景 1：操作员误开第二 window**
  - 客户在 Profile A 跑 V2，需要快速看 Drive 里某个文件
  - 习惯：Ctrl+N 开新 window → chrome 默认在同 profile 开
  - 新 window 加 labs.google 看历史产出 → V2 content script 注入 → 上报 page state 给中控 → 中控混淆

  **场景 2：chrome restore 多 window**
  - chrome 重启 / chrome update 后默认 restore 上次的 window/tab 配置
  - 客户上次开了 2 window，restore 后 2 window 同时打开
  - 多 active_flow_tab_id 出现

  **场景 3：DevTools 是另一个 window 但同 profile**
  - chrome DevTools 弹出独立 window（虽然不是 page，但属于同 profile chrome 进程）
  - 客户技术人员打开 DevTools 看扩展行为 → DevTools window 不影响 inject 但占资源

  **场景 4：incognito window 跟 normal 同 chrome 进程**
  - incognito window 不 inject V2（manifest 默认 not_allowed，C-038 已覆盖）
  - 但客户开 incognito 后 chrome 资源更紧张 → C-007 RAM 评估变化

  **缺失 1：sw 视角缺乏 window 概念**
  - chrome.tabs.query 不返回 window_id 区分（其实 Tab object 有 windowId 字段）
  - design §4.6 active_flow_tab_id 只标 tab，没有 window
  - 5 工位每个 ws 默认 1 个 tab，但多 window 下每个 ws 可能多 tab

  **缺失 2：操作员误用第二 window 的预防**
  - 没有 "lock to single window" 机制
  - 操作员习惯多 window，V2 没引导避免

- **依据**：
  - chrome multi-window 是常见用户行为（chrome dev docs）
  - chrome.tabs.Tab 含 windowId 字段（chrome API）
  - V1 patchright 启 chrome 是单 window 跑 → V2 才有这问题
- **影响**：
  - 多 window 下 sw 行为混乱 → task_assign 派给错的 window 的 tab
  - 操作员误开 window 看历史 → V2 task 中断（content script 重新 inject）
  - chrome restore 多 window → 多 active_flow_tab_id silent inconsistency
- **建议方向**：
  1. **§4.6 加 window 维度**：
     - active_flow_tab_id 升级为 (window_id, tab_id)
     - sw 维护 window_id 锁定的"工位 window"
     - chrome.tabs.create 时显式指定 windowId，避免新建 window
  2. **single-window 策略**：
     - 启动时强制 chrome.windows.create 一个专用 window 给 V2 任务
     - 该 window 标记 alwaysOnTop / pinned
     - 操作员另开 window 看 Drive / 别的网站不影响
  3. **多 window 检测告警**：
     - sw chrome.windows.onCreated 监听新 window
     - 新 window 加载了 Flow URL → 视觉告警操作员"V2 不应在多 window 同时开 Flow"
  4. **chrome restore 时合并 window**：
     - sw 启动检测多 Flow window → 自动 close 非工位 window，保留 1 个
     - 或弹 popup 让操作员选哪个 window 是工位
  5. **incognito + V2 资源监控**：
     - 操作员开 incognito → chrome.windows.onCreated 检测 incognito state → 中控告警 RAM 压力（C-007 串联）

### C-060 [Major] 客户机 WS 网络稳定性未验证 — 公司代理 / DNS / 防火墙 / 杀毒拦 localhost ws / Win11 Defender Network Protection 全没考虑

- **针对**：design v0.5 §4.3 ws://localhost:8080/ws/extension/<ws_id> + §13.2.3 FastAPI 强制 127.0.0.1 bind
- **问题描述**：
  设计稿假设 ws://localhost 通信"零延迟，零网络风险"——但**客户机网络层有多重拦截**：

  **干扰 1：Win11 Defender Network Protection**
  - Defender SmartScreen + Network Protection 检查所有 chrome 出网请求（含本机 ws://localhost）
  - 拦截 unknown unsigned 进程跟外部通信
  - chrome → ws://localhost:8080 是 chrome 到 Co-Pilot 进程的 IPC，但 Defender 视角是"两个进程通信"，可能被拦
  - 特别 Co-Pilot.exe unsigned（C-036 已提）+ chrome 扩展 unpacked → Defender 双重怀疑

  **干扰 2：客户公司代理 + DNS**
  - 客户公司机走公司代理（PAC / SOCKS / HTTP proxy）
  - 公司代理可能强制 chrome 所有出网走代理，**包括 localhost**
  - DNS over HTTPS：chrome 可能用 cloudflare / google DoH 解析 localhost → 得到外部 IP（罕见但可能配置错）
  - 公司代理把 localhost ws 重定向 → V2 通信不到 Co-Pilot

  **干扰 3：第三方杀毒软件**
  - 卡巴斯基 / 360 / 腾讯电脑管家 行为监控
  - 检测到"chrome 扩展跟非浏览器进程通信"可能告警 / 拦截
  - V2 chrome 扩展 → ws://localhost → Co-Pilot.exe 是反常通信模式
  - 杀毒判定为"可疑 → 拦截"概率不低

  **干扰 4：Win11 Hyper-V / WSL 网络隔离**
  - 部分客户机用 WSL 跑工具 → WSL 改了 network namespace
  - localhost 可能 routing 错误（chrome localhost = Win 主机 vs WSL localhost）
  - 罕见但部分客户撞过

  **干扰 5：cloudflared 隧道 vs localhost ws 共存**
  - design §4.1 保留 cloudflared
  - cloudflared 隧道把 localhost:8080 暴露公网
  - chrome 扩展 → ws://localhost:8080 是直连
  - 操作员开 cloudflared 时 → 公网访问 dashboard 通；扩展 ws 通信不变
  - 但如果 cloudflared 配置错（端口冲突 / DNS 错） → 影响 ws

  **干扰 6：Win11 Hosts 文件**
  - 客户机偶尔 hosts 改过（公司管理工具）→ localhost 解析变化
  - V2 假设 localhost = 127.0.0.1，但可能被 hosts 改成别的

- **依据**：
  - Win11 Defender Network Protection 行为（Microsoft docs）
  - chrome DoH 默认行为（chrome 91+）
  - V1 v0.0.2 cloudflared 经验（memory）
- **影响**：
  - 客户机部署 V2 但扩展 register 失败 / WS 间歇断开 → V2 不工作
  - 客户报"为啥 dashboard 看 5 工位 offline" → 排查地狱
  - cloudflared 跟 localhost ws 端口冲突
- **建议方向**：
  1. **install wizard 网络层检测**：
     - 启动 Co-Pilot → 自测 127.0.0.1:8080 listen 成功
     - 用本机 chrome 访问 http://127.0.0.1:8080/health 确认通
     - chrome 扩展首次启动 fetch http://127.0.0.1:8080/health 验通 → 失败 popup 告警 + 提供 troubleshooting checklist
  2. **Win Defender 排除项**：
     - install wizard 提示客户加 Defender 排除：Co-Pilot.exe + V2 安装目录 + chrome.exe（已默认排除）
     - 提供一键 PowerShell 脚本（需要 admin）：`Add-MpPreference -ExclusionPath`
  3. **DoH 关闭引导**：
     - chrome → Settings → Privacy → DNS over HTTPS → "Off" 或 "With current service provider"
     - 文档说明
  4. **杀毒白名单**：
     - 提供主流杀毒（卡巴斯基 / 360）白名单配置说明
     - 客户 IT 协作 SOP
  5. **网络层监控**：
     - 扩展端 WS reconnect 失败 → 上报 onError code → 中控记录失败原因
     - 重复失败 → 提示客户检查代理 / 防火墙 / 杀毒
  6. **设计稿 §13 加新威胁场景 / §5.3 install wizard 加网络层检测**

### C-061 [Major] cancel_task 协议有但 Veo 后端不可取消 = 已点 Create 的配额扣了任务作废 = 客户成本浪费

- **针对**：design v0.5 §4.3 protocol `cancel_task` + §4.4 task lifecycle "create_pending → create_committed"
- **问题描述**：
  设计稿协议 v1 有 cancel_task message，但**没说 cancel 在不同 lifecycle 阶段的实际行为**——Veo 后端 click Create 后不能取消：

  **场景 1：cancel_task 在 lifecycle 不同阶段**

  | 阶段 | cancel 后果 |
  |---|---|
  | pending（中控未派） | OK，扩展没收到，配额未消耗 |
  | assigned（扩展接收但未点 Create） | OK，扩展中止 |
  | uploading（asset 上传中） | OK，扩展中止 |
  | prompt_typed（prompt 输入完未点 Create） | OK，扩展中止 |
  | **create_pending**（Create 已点，等 Veo 后端确认） | ⚠️ Veo 后端可能已收，配额可能已扣 |
  | **create_committed**（Veo 已收 generation request） | ❌ Veo 不可取消，generation 必跑完，配额已扣 |
  | generating（等 mp4 出） | ❌ 不可取消，mp4 仍生成 |
  | downloading | OK，扩展中止下载，但 mp4 在 Flow project page 仍可见 |
  | task_complete / error | N/A |

  **场景 2：客户 / 操作员主动 cancel**
  - 中控 dashboard "取消任务" 按钮 → 派 cancel_task
  - cancel 时机不可控 → 大概率 create_committed 阶段 → Veo 配额已扣
  - 客户付了 Veo 配额钱，最终没拿到 mp4（cancel 后扩展不下载）
  - **客户成本浪费 silent**——dashboard 报 "task cancelled" 但没说"配额已扣"

  **场景 3：中控自动 cancel（C-001 / C-009 sw hibernate retry）**
  - design §4.4.4 防御 Veo 双扣费 — 中控不主动 retry pending → cancel_task 不会自动触发
  - 但操作员主动 cancel 仍是路径

  **场景 4：Flow project 残留**
  - cancel 后 Veo 仍生成的 mp4 在 Flow project page 可见
  - 下次操作员看 page 看到 "未下载的 mp4" → 困惑
  - DB 没记录这个 mp4 → 跟 V1 silent failure 同症状（memory: project_known_fragility）

  **缺失 1：cancel 协议没说阶段约束**
  - protocol v1 cancel_task 是 "fire and forget"
  - 没有"只允许 cancel 在 X 阶段之前"的约束
  - 没有 cancel 后的扩展回报（是否成功 cancel / 是否已 commit / Veo 已扣多少）

  **缺失 2：dashboard UX 不告诉客户配额代价**
  - 客户看到"取消任务"按钮 → 想取消就点 → 没意识到配额已扣
  - 应该显示"该任务已 commit，取消将浪费 N 个 mp4 配额"确认对话框

  **缺失 3：Flow project 残留 mp4 处理**
  - cancel 后扩展应该尽量下载残留 mp4（即使 task cancelled）
  - 或：扩展上报 "残留 mp4: 3 个" → 中控记录"配额已扣但已 cancel"

- **依据**：
  - Veo / Flow 没公开 cancel API（design §2.3）
  - V1 也没主动 cancel 机制（V1 client 操作粒度低）
  - V2 引入 dashboard cancel 按钮是新功能（design §4.3）
- **影响**：
  - 客户配额 silent 浪费（已扣但没产出）
  - dashboard 报告跟 Veo 后端实际状态分歧
  - 残留 mp4 在 Flow project page 没处理
- **建议方向**：
  1. **cancel_task 协议加阶段约束**：
     - 扩展收到 cancel_task → 检查当前 lifecycle 阶段
     - 已 create_committed → reject cancel + 上报 `cancel_rejected: already_committed`
     - 中控 dashboard 显示"无法取消，generation 进行中"
  2. **dashboard cancel UX 加二次确认**：
     - 客户点 cancel → 弹"该任务进度 X% / Veo 配额已扣 Y 个 / 取消后 Y 个 mp4 仍生成但不下载，确认？"
     - 客户明确承担配额代价
  3. **残留 mp4 兜底下载**：
     - cancel 后扩展不立即关 page，等 5 分钟看 Flow page 是否生成残留 mp4
     - 残留 mp4 仍下载（写到 output/cancelled/...）
     - DB 记录"任务 cancel 但配额已用，残留 mp4 N 个"
  4. **配额账户级监控（C-067 关联）**：
     - 中控记每账号每日 Create count
     - cancel 不重置 count（已扣不退）
     - dashboard 显示账号配额使用率
  5. **§4.4 加 cancel 阶段处理图 + §4.3 protocol cancel_task 加 stage check**

### C-062 [Major] 扩展性能开销持续影响客户日常 chrome UX — content script 永驻 + MutationObserver 持续跑 + chrome.alarms 30s 撞客户日常浏览

- **针对**：design v0.5 §4.2 内部分工 content_scripts run_at: document_idle + chrome.alarms 30s + §5.4 多 chrome profile
- **问题描述**：
  V1 patchright 启的 chrome 是独立进程，跟客户日常 chrome 隔离，性能开销不影响日常浏览。V2 设计跑客户日常 chrome → **扩展持续 inject + observe + alarm 影响日常浏览体验**：

  **开销 1：content script 永驻 labs.google/* tabs**
  - manifest content_scripts match `https://labs.google/fx/tools/flow/*`
  - 客户每打开一个 Flow tab → content script inject → 跑 page lifecycle 监听 + DOM observer + state 检测
  - V1 v0.0.4 等价代码 V2 重写 TS 后体积可能 100-300 KB（minified）
  - 每个 Flow tab 开启时 inject 时间 ~50-200ms（初次解析）

  **开销 2：MutationObserver 持续跑**
  - design §4.2 假设 2 用 MutationObserver 监测 DOM 变化定位 page state（C-002 / C-041 后退路）
  - MutationObserver 监听整个 document 子树 → 每个 DOM mutation 触发 callback
  - Flow page React rerender 频率高（state 变化 → DOM diff → mutation events 上百次/秒）
  - callback 内 state 检测 + chrome.runtime.sendMessage → page 主线程 CPU 占用持续 5-15%

  **开销 3：chrome.alarms 30s 唤醒 sw**
  - C-001 防御 sw hibernate 用 chrome.alarms 30s 周期触发
  - sw 唤醒 → JavaScript event loop 跑一段 → CPU spike
  - 5 工位 × 5 sw × 30s alarm = 客户机持续 CPU 抖动
  - 单 sw 唤醒影响小，5 个一起 + 客户日常 tab 切换 → 体感卡

  **开销 4：扩展跑在客户日常 chrome → 影响所有 chrome 性能**
  - chrome 不是只对 labs.google 加载扩展，而是整个 chrome 进程都加载扩展
  - 每个 chrome tab（包括客户日常 Gmail / Drive / 看 b 站）都受扩展性能开销影响（即使 content script 只 match labs.google，sw + 扩展资源仍占）
  - chrome 扩展资源限制：每个扩展 sw 内存 100-200 MB；多扩展并发占用累加

  **开销 5：客户日常 chrome 卡顿 → 操作员归咎 V2**
  - 操作员体感"装了 V2 后 chrome 慢了" → 不愿意用
  - 客户拒绝 V2

  **缺失 1：性能 benchmark**
  - 设计稿没说扩展启动 / inject / alarm / MutationObserver 的可接受性能开销范围
  - spike 阶段没性能验证项

  **缺失 2：极端场景**
  - Flow tab × 5 profile 同时跑任务 = 5 个 MutationObserver 持续跑
  - 客户机 RAM 8 GB（C-007 已论证不达标但实测 fallback N=2-3）→ chrome OOM

- **依据**：
  - V1 patchright 独立 chrome 跟客户日常隔离
  - chrome 扩展 sw + content script 性能开销（chrome dev docs）
  - MutationObserver 性能特性（DOM spec）
- **影响**：
  - 客户日常 chrome 卡顿 → 操作员体验差 → 拒用 V2
  - chrome OOM 导致 V2 任务中断
  - "V2 客户日常 chrome 一等公民" 优势变成劣势
- **建议方向**：
  1. **content script 懒加载**：
     - manifest 不写 content_scripts auto match
     - sw 收到 task_assign 时再用 chrome.scripting.executeScript 主动 inject
     - 任务结束 chrome.scripting.removeCSS / removeListener 清理
     - 客户日常打开 Flow page（不跑任务）→ 不 inject
  2. **MutationObserver 限缩范围**：
     - 不监听 document，只监听 Flow main container（labs.google specific selector）
     - throttle / debounce callback（100ms 一次）
     - 任务结束 disconnect observer
  3. **chrome.alarms 周期动态**：
     - task 跑中 alarm 30s（C-001）
     - task idle 时 alarm 5min（降到 chrome alarms 最低稳态周期 — 实测 chrome 后台 throttle 后 alarms 也不会 < 1min）
     - 减少客户机 CPU 抖动
  4. **性能 benchmark spike 验收**：
     - spike 新增 #12：装 V2 extension on chrome with 客户日常 tab（10 个） → 测 chrome 主线程 CPU / RAM 增量 / page render 帧率
     - 增量 < 5% CPU 才 pass
  5. **客户日常 chrome 不装 V2（C-029 串联）**：
     - 推 customer 用专门 V2 profile（不是日常 profile）
     - V2 profile 只装 V2 扩展不装 Grammarly 等 → 性能干净
     - 损失"日常使用历史"——但 C-028 已论证账号信誉优势薄
  6. **设计稿 §4.2 加 lazy inject + observer 范围限制**

### C-063 [Major] spike 阶段 5-7 天时间表不现实 — 11 gate / 12 spike 验收项 + 测试基础设施搭建 + 客户协作 + Phase 0.5 fragility 沉淀部分都要做

- **针对**：design v0.5 §6.1 milestone "Spike 5-7 天" + §7.3 spike 验收项 1-12 + §7.4 G1-G10 + 修订 G11
- **问题描述**：
  spike 时间表 5-7 天但内容已堆叠到不可执行：

  **统计 spike 阶段需做的事**：
  - **测试基础设施搭建（C-058）**：3 台测试机 + 5 个测试账号 + chrome 多版本 → **5-10 天** 仅搭建
  - **Phase 0.5 V1 fragility 沉淀（C-032）**：35 条 reproducer fixture top 10 priority → 1 周，全部要 2-3 周
  - **spike 独立验证项**：
    - #1 sw hibernate（C-001）→ 1-2 天
    - #2 locale-independent + CSP 兼容（C-002 / C-041）→ 2-3 天
    - #3 V1 baseline（C-004）→ 跑 3 次任务集 × 单次 30-60min = 1.5-3 天
    - #4 200MB 文件传输（C-005 / C-019）→ 1 天
    - #6 Veo idempotency（C-020）→ 1 天
    - #8 误配 ws_id 检测（C-018）→ 0.5 天
    - #9 reconnect storm（C-034）→ 1 天
    - #10 时间偏移（C-034）→ 0.5 天
    - #11 SQLite 1 周长跑（C-046）→ **7 天**
    - #12 性能 benchmark（C-062）→ 1 天
  - **客户协作 spike**（C-056）：
    - #5 客户机 PoC 上架（约束反转后已 OBSOLETE 部分）
    - #7 客户机 5 profile RAM（C-007）→ 客户配合 1-2 天
  - **客户访谈 gate**：G1 / G2 / G7 / G8 / G10 / G11 → 1 周（客户日历窗口）
  - **安全 review**（§13.3）：1-2 天

  **总计**：测试基础设施 5-10 天 + Phase 0.5 1 周 + spike 独立 ~10 天（其中 #11 SQLite 长跑独占 1 周可并行） + 客户协作 1-2 周 = **3-6 周不是 5-7 天**

  **隐含问题**：
  - SQLite 1 周长跑独占 1 周 wall-clock，但做别的事可并行（要测试机够多）
  - Phase 0.5 跟 spike 独立验证有依赖关系（测试 fixture 才能跑独立验证）
  - 客户访谈是异步事件，客户回答慢 1 周以上很正常
  - design §6.1 把 spike 标 "5-7 天"——预算严重低估

  **后果**：
  - 实际跑下来 spike 6 周 → 项目经理 / 客户预期错位
  - 5-7 天硬切 → 项目经理裁掉部分 spike 项 → 风险未验证 → V2 实施期再爆
  - 客户协作时间不留够 → 客户敷衍 → gate 数据不可信

- **依据**：
  - design §7.3 12 项 spike 验收
  - 修订段加 G10 / G11 → 立项 gate 11 个
  - C-058 测试基础设施 / C-032 fragility 沉淀工时估算
- **影响**：
  - spike 时间表跟实际不符 → 项目延期
  - 强行 5-7 天裁项目 → spike 数据不可信 → V2 立项决策错
  - 客户预期被设错（"5-7 天就能定 V2 立项"）
- **建议方向**：
  1. **spike 时间表重排**：
     - **Phase -1 测试基础设施搭建**：1-2 周（先决条件）
     - **Phase 0 客户访谈 + GPO check**：1-2 周（异步，跟 Phase -1 并行）
     - **Phase 0.5 Top-10 fragility 沉淀**：1 周
     - **Phase 1 spike 独立验证**：5-7 天（其中 SQLite 长跑后台 1 周）
     - **Phase 2 spike 客户协作**：1 周（客户机现场 / 远程）
     - 总计 **4-6 周** spike 阶段
  2. **gate 优先级**：
     - 客户访谈 gate（G1/G2/G7/G8/G10/G11）排在 spike 最前面 → 客户拒绝 → spike 直接停
     - 独立验证 gate（G3-G6/G9）在客户访谈通过后开
  3. **spike 失败的 fast fail**：
     - 任何 Blocker gate 失败 → 立即停 spike，不等其他
     - 节省 spike 资源
  4. **设计稿 §6.1 必须 inline 重排**

## 三、Minor（v0.0.5 新增）

### C-064 [Minor] V2 运营成本量化缺位

- **针对**：design v0.5 §6 实施计划完全不含成本估算 + memory: project_v1_delivery V1 客户支持成本无记录
- **问题**：
  - V2 比 V1 多哪些运营成本？测试机 3 台（$3000+）/ Veo 测试账号月度（$500+）/ chrome 升级跟进（每月 1-2 工时）/ 扩展 release 节奏（每月 4-8 工时手动 reload 助手）/ cloudflared 流量（GB 级，免费 tier 够用）
  - V2 客户支持时数 vs V1：每个 V2 客户机 chrome 升级跟进 + extension reload + GPO 配置协助 → 估算 5-10 工时/月/客户
  - 5 客户机 = 25-50 工时/月 = 1-2 工程师全职
  - V2 商业模型支撑得起吗？V1 license 收入对应得起 V1 支持成本，V2 支持成本翻倍但 license 没翻
- **建议**：
  - 设计稿 §18 客户协作框架加成本明细
  - 客户切 V2 对应 license 提价 / 升级 SLA 加价
  - 内部成本审计 + V2 单客户 break-even 分析

### C-065 [Minor] 国际化语言矩阵深挖 — 客户产品多国卖（中英日韩泰阿），SKU/prompt 跨语种 IME / Unicode 输入跨平台测试

- **针对**：v0.0.4 C-052 IME 单条延伸 + 客户业务场景
- **问题**：
  - 客户产品多国卖意味着 SKU / prompt 含多语种文本（中文 SKU + 英文 prompt / 日文 SKU + 中文 prompt 等组合）
  - 各语种 IME 行为差异：中文拼音 / 日文罗马字 / 韩文 IME / 泰文输入 / 阿拉伯 RTL 文字
  - V2 prompt 输入跨语种验证未做（spike #2 只测越南语 UI）
  - Veo 后端处理多语种 prompt 是否一致（API 层不公开，纯黑盒）
- **建议**：
  - i18n 测试矩阵：5 种语言 × 输入 / 显示 / 上传 → 至少跑 spike 后期补
  - 文档加多语种支持声明

### C-066 [Minor] mp4 codec 跨平台兼容（Veo 默认输出 vs 客户 NLE / Win Media Player）

- **针对**：design v0.5 §4.3 文件传输 mp4 + 客户使用场景未提
- **问题**：
  - Veo 默认输出 mp4 codec（h264 / h265 / VP9）—V1 没记录
  - 客户拿 mp4 后用 NLE 软件（Premiere / 剪映 / DaVinci）剪辑
  - 部分 NLE 不直接支持 h265 / VP9，需要 transcode
  - Win Media Player / 系统 thumbnail 也可能 codec 兼容差
  - V1 客户没反馈过这问题（可能 Veo 输出 h264 默认）— V2 一致性未确认
- **建议**：
  - spike 阶段记录 Veo 输出 codec 实际值
  - 客户文档加 codec 说明 + 必要时建议转码脚本

### C-067 [Minor] 操作员误操作 chrome 设置（清浏览数据 / disable 扩展 / 改下载目录 / 关 power management）的预防 + 检测 + 自愈

- **针对**：design v0.5 §4.5 setup wizard + §4.7 扩展 update
- **问题**：
  - 客户清浏览数据 → chrome.storage 清 → ws_token 丢（C-055 已提）
  - 客户 disable 扩展（chrome://extensions toggle）→ V2 全停，dashboard 看 ws offline
  - 客户改 chrome 下载目录 → V2 方案 A 落盘路径不对（C-019 + C-049 串联）
  - 客户关闭 chrome power management 加 flag → 不影响但操作员可能改其他 flag 影响 V2
  - 没有"误操作早期检测"机制
- **建议**：
  - 扩展 first run + 周期性 self-check：
    - chrome.storage.local 完整性（ws_token / setup_complete 标志）
    - chrome.downloads default folder 是否未改
    - chrome.management 自身扩展状态（不可读但可通过 ping 间接判）
  - 中控 dashboard 显示 self-check 结果，红色告警操作员误操作

### C-068 [Minor] Co-Pilot + 扩展 unified release packaging（zip 内含两者，install.bat 自动化）

- **针对**：v0.0.4 修订段 C-055 unified release zip 提议 + design §6.1 milestone 5 客户体验
- **问题**：
  - V1 release 是单一 .exe（PyInstaller bundle），客户双击装
  - V2 release 至少含 Co-Pilot.exe + extension/ + install.bat + GPO 配置脚本（C-057）+ 升级 SOP 文档
  - 如何打包成单一 zip？install.bat 自动化到什么程度？
  - 当前 V1 GitHub Actions release 流程（memory: reference_paths）需要扩展支持 V2 packaging
- **建议**：
  - V2 release zip 结构定义：
    ```
    flow-harvester-v2.x.zip
    ├── Co-Pilot.exe
    ├── extension/  (unpacked)
    ├── install.bat (自动化安装)
    ├── update.bat (升级)
    ├── chrome-policy/ (GPO 模板)
    └── README.txt (中文)
    ```
  - GitHub Actions release workflow 扩展打包

### C-069 [Minor] 数据备份 / 恢复策略缺失 — SQLite WAL + output/ 5GB+ 数据怎么备份？客户机硬盘故障怎么办

- **针对**：design v0.5 §3.1 客户机部署 + §4.1 SQLite + V1 没备份机制
- **问题**：
  - V1 也没显式备份策略（memory）
  - V2 任务结果 mp4 + DB 历史 + forensic_log 累计 5-10 GB
  - 客户机硬盘故障 / Win11 重装 / 病毒加密 → 数据全丢
  - V2 多了"工位 binding / license / setup token" → 重装客户机要重做整个 setup wizard
  - 没有"导入老备份恢复"机制
- **建议**：
  - 备份策略：
    - SQLite 每日 dump 到 `backup/db_YYYYMMDD.sqlite`
    - output/ 保留最近 30 天，可外接 NAS / OneDrive 镜像
    - workstation binding 配置导出 JSON 备份
  - 恢复 SOP：客户机重装 → 跑 install.bat → restore 备份 → workstation 自动 rebind
  - dashboard 加"备份状态"指示

---

---

# v0.0.6 — 第六轮 challenge（防过拟合：收紧到 5 条 high-value）

**本轮策略**：前 5 轮 65 条 + design v0.5 已 inline 集成全部 → 进入 diminishing returns。本轮不补全 v0.0.5 末尾 10 项 list，只挑 high-value 新角度 + 1 条 meta 警告。**显式记录 skip 的项以避免过拟合**。

## 显式 skip 的项（v0.0.5 末尾 10 项中 7 项 skip）

| 项 | skip 理由 |
|---|---|
| Co-Pilot self-update | V1 沿用手动 .exe 替换 OK；扩展 + Co-Pilot 统一 release zip（C-068）已覆盖；自动 self-update 是实施细节，不是架构问题 |
| 扩展协议向后兼容矩阵 | C-055 + protocol_version 字段已设计 forward-compat；具体兼容矩阵是 release engineering 实操，不是设计盲区 |
| 多客户机 fleet 监控 | V3 范围（design §6.3 / §3.2 已排除）；V2 单机 single-customer 不该现在挑战 |
| 客户报障 SLA | 业务 / 合同问题，不是架构 challenge；C-064 运营成本已带过 |
| 扩展开发 DevX（HMR / 调试 / 分支） | 实施细节 — Vite + @crxjs/vite-plugin 自带 HMR；branch 是 dev process |
| 扩展 UI 框架选型（React / Vanilla TS） | 实施细节 — 不影响架构，工程师 spike 时定 |
| V2 release semver | 实施 release engineering policy，不影响架构决策 |
| 任务重试策略细化 schedule | V1 strike + V2 §4.5.3 strike-by-email 已设计；cooldown 时长是 tunable 参数 |
| dashboard 移动端响应式 | minor UX；cloudflared 远程访问场景偶发，不阻塞立项 |
| mp4 codec / i18n 跨语种深挖 | C-065 / C-066 已 minor 标记；spike 阶段实测一次定，不需要更多 challenge |

**保留挑战**：Google TOS 法律风险（v0.0.5 末尾 #8 唯一保留项）+ 4 条新角度。

---

## 0. Meta-challenge

### C-070 [Meta] challenges 已 65 条进入 diminishing returns，建议暂停 challenge cycle 转 Phase -1 实操；产出 "V2 Top 10 Implementation Constraints" 摘要给工程师消化

- **针对**：v0.0.1-v0.0.5 累计 65 条 + design v0.5 600+ 行 + 11 立项 gate + 12 spike 验收
- **问题描述**：
  挑战的边际收益从 v0.0.4 开始递减。证据：
  - v0.0.4 已经出现"Web Store 风险" Blocker，被用户一句话约束直接 OBSOLETE → 表明纸面 challenge 跟客户实际决策脱节
  - v0.0.5 13 条中 C-056/C-058/C-063 三条本质都是"项目管理风险"角度重复（客户协作 / 测试基础设施 / 时间表）
  - design v0.5 §11 change log 索引超 50 行，工程师读完已要 1 小时；新加入实施工程师**消化不了**
  - 65 条 challenges 文档 1500+ 行，再加 v0.0.6/v0.0.7 滑向"完美主义瘫痪"
  - 真实风险只能通过 **Phase -1 测试基础设施搭建 + Phase 0 spike 实操** 验证；继续纸面 challenge 不会再发现 Blocker 级新风险

  **过拟合迹象**：
  - 给 minor 找补（v0.0.5 minor 5 条中数据备份 / mp4 codec / i18n 等是"完整性诉求"非"立项 blocker"）
  - 同主题反复深挖（Locale 已经 v0.0.1 C-002 → v0.0.4 C-041 CSP → v0.0.5 i18n 矩阵 三层叠加，spike 实测会一次性回答）
  - 对架构正文每个段落都尝试找 challenge → 边际成本超边际收益

- **影响**：
  - 团队认知负担过载，决策瘫痪
  - "challenge 文档"变成自我消耗，远离原始目的（验证 V2 设计合理性）
  - 真实问题不会从纸面挖出，只能 Phase -1 + spike 实操反馈
- **建议方向**：
  1. **暂停 challenge cycle**：v0.0.6 之后**不再添加新 challenges 直到 spike Phase 0 出第一份实测数据**
  2. **产出 V2 Top 10 Implementation Constraints 摘要**：
     - 从 65 条中筛 top 10 必读约束（spike 阶段必须验证 + 实施期必须满足）
     - 其他 55 条降级为"参考资料"，工程师按需查
     - 摘要 ≤ 1 页，工程师 5 分钟能读完
  3. **建立"实测反馈优先"原则**：
     - spike 实测发现新问题 → 加 challenges
     - 纯纸面推演的新问题 → 暂存 backlog 不立即写入 challenges
  4. **挑战质量门槛**：
     - 新 challenge 必须能回答"如果不解决，V2 立项 / 实施 / release 哪一步具体 break"
     - 答不出 → 不写
  5. **本轮 v0.0.6 + 后续暂停 challenge** → 进 Phase -1 实操 → 复盘后再决定 v0.0.7 是否启动

## 一、Blocker（v0.0.6 新增）

### C-071 [Blocker] Google TOS / Veo 服务条款合规未审 — 客户 + 作者法律风险归属空白；V2 比 V1 暴露面更大

- **针对**：design v0.5 §13.5 GDPR 数据保护 + design 整体未涉及 Google ToS / Veo TOS 合规
- **问题描述**：
  V1 patchright 自动化 Veo 也存在 TOS 风险，但客户场景规模小没爆。V2 改 chrome 扩展，**TOS 风险面变大且暴露方式变了**：

  **TOS 雷点 1：Google Workspace / 个人账号 TOS**
  - Google 通用 ToS § 5 "If you've been told that you can't access or use any of our Services, please don't try"
  - Google 反自动化条款（automation / scraping / bot）— 对 Veo / Flow 自家 AI 服务尤其严
  - V1 patchright 也违反，但 V1 是后台进程客户视角"工具"；V2 是客户日常 chrome 装的扩展，**操作员 + 客户 + 作者 三方都能看到这是自动化** → Google 主动检测到的概率↑
  - 账号被 ban → 客户业务停（C-028 已论证账号风险半径）
  - 客户 ban 责任：客户违反 Google TOS（不是作者违反）— **客户合规暴露**

  **TOS 雷点 2：Veo 服务专属条款**
  - Veo（labs.google）是 experimental / preview 产品，可能有专属 TOS
  - "research preview" 通常含"non-commercial / personal use"限制
  - 客户用 V2 批量生产视频商用 → 可能违反 Veo TOS
  - 作者作为工具提供方 + 客户作为 end user 两方都暴露

  **TOS 雷点 3：扩展 distribution + 自动化结合**
  - chrome 扩展即使 unpacked，第三方 distribute 给客户使用 → 作者承担产品责任
  - 扩展 inject 到 Google 服务 page 干预其行为 → 可能违反 chrome 扩展行为政策（即使不上 Web Store）
  - Google 可以 chrome client-side 检测扩展 ID + 行为指纹 → 扩展级 ban / 账号级 ban

  **TOS 雷点 4：作者法律风险归属**
  - 客户 license 协议是否含"客户使用工具违反 Google TOS 的责任由客户承担"？V1 license 文档没看到（memory: feature_license 仅校验逻辑）
  - 没有 indemnification clause → 作者可能承担连带责任
  - 客户因 V2 被 Google 起诉 / 业务停摆 → 反过来要求作者赔偿
  - 跨国客户（EU / 美国） → 法律管辖权复杂

  **TOS 雷点 5：作者主动监控义务**
  - V2 比 V1 有更强可观测性（cloudflared 远程访问 / 扩展端日志推送 / forensic_log）
  - 作者"知道"客户在做啥 → 法律视角"作者明知客户违反 TOS 仍提供工具" → 责任加重
  - V1 时代作者"不知道"客户具体在跑什么 → 法律责任更轻

  **缺失：法律审计未做**
  - design 没出现"legal review" / "TOS audit" / "license terms" 章节
  - 客户 license / 服务协议 V1 用的什么模板未审
  - Google TOS / Veo TOS 是否在 V2 立项前过律师 unknown

- **依据**：
  - Google TOS（公开文档）§ 5
  - Veo / labs.google 是 research preview 性质（公开）
  - V1 license 实际条款 memory 没记
  - chrome 扩展政策对 Google 服务自动化的态度（v0.0.4 C-042 已涉及，但 TOS 层面没深挖）
- **影响**：
  - 客户被 Google 起诉 / 账号大量 ban → 客户业务损失 → 反索作者
  - 作者作为产品方法律责任不清 → 一次客户事件可能 wipe out 项目
  - V2 上线后法律风险曝光，比 V1 更明显（扩展 + dashboard + 远程支持都让自动化更"显眼"）
- **建议方向**：
  1. **V2 立项前必做 legal review**：
     - Google TOS 自动化条款逐条解读
     - Veo TOS（如果有专属）单独审
     - chrome 扩展政策 vs unpacked distribute 法律地位
     - 客户管辖权 + 跨国合规（EU GDPR + 美国 + CN）
  2. **客户 license 协议升级**：
     - 加 "Customer is solely responsible for compliance with Google TOS / Veo TOS"
     - 加 indemnification clause（客户违反第三方 TOS 时作者免责）
     - 双语版本（中英）
  3. **风险披露给客户**：
     - V2 onboarding 第一屏：客户必须勾选确认"理解使用 V2 自动化 Veo 可能违反 Google TOS，承担相应责任"
     - 客户内部合规审过才能用
  4. **作者操作 hygiene**：
     - 作者远程支持时不主动看客户具体业务数据（只看 error log / state）
     - 减少"明知"风险
     - 诊断包脱敏 prompt（v0.0.4 C-044 已建议）
  5. **新增 §20 合规 / TOS / 法律风险章节**
  6. **G12 立项 gate "legal review 通过"**

## 二、Major（v0.0.6 新增）

### C-072 [Major] 架构层 stop-loss criteria 未定义 — 11 gate / 12 spike 中多少 fail 算 V2 整体 abort，没设计退出条件 → 沉没成本陷阱

- **针对**：design v0.5 §7.4 立项 gate G1-G11 + §7.3 spike 验收 + §6.1 Phase -1/0/0.5/1+
- **问题描述**：
  设计有 11 个立项 gate + 12 个 spike 验收项，每个都标"⏳ pending"或"pass criteria"。但**"组合失败的 abort 阈值"没定义**：

  **场景 1：部分 fail 怎么决策**
  - spike 跑完 12 项，9 项 pass / 3 项 fail（比如 C-002 locale + C-005 文件传输 + C-007 客户机 RAM 都 fail）
  - 设计稿没说"≥X 项 fail = abort V2"
  - 项目经理凭直觉决定继续还是停 → 沉没成本陷阱（已投入 4-6 周 spike，倾向继续）

  **场景 2：单 Blocker fail 全 abort 还是降级**
  - C-002 locale 假设 fail → 设计 §4.2 已有降级"退回多语言列表"
  - 但有些 Blocker 没降级方案：C-027 license / C-057 GPO 阻塞 / C-071 TOS 法律风险
  - fail 怎么处理 — abort or 部分客户 launch？没说

  **场景 3：客户 gate fail 但技术 gate pass**
  - G1-G2 客户访谈拒（"chrome 必须开" 客户拒）
  - spike 独立验证全 pass（技术上 V2 可行）
  - 怎么办？换客户？做 V2 但找新 lead customer？继续 V1？设计稿没设计

  **场景 4：成本超预期 → abort 还是注资**
  - C-058 测试基础设施成本 / C-064 运营成本 实际跑出来超预算
  - 没"成本超 X 倍 abort" 阈值

  **场景 5：spike 时间超预期**
  - C-063 已论证 spike 实际 4-6 周（不是 5-7 天）
  - spike 跑到第 8 周仍未完，怎么办？
  - 没"超 N 周 abort" 阈值

  **缺失：abort 决策权 + 触发**
  - 谁有权 abort V2？项目经理 / 作者 / 客户？
  - abort 后什么处理：停在哪 / 沉没成本如何记 / 继续 V1 维护

- **依据**：
  - 11 gate × 任意组合 fail 没决策树
  - V1 v0.0.1→v0.1.0 6 轮迭代每轮"修一个看一个"，没全局 stop-loss
  - design §7.4 表格末尾没"组合 fail 决策"段
- **影响**：
  - 投入 spike 时间 + 测试基础设施成本后，因为没 stop-loss 而硬推 V2 成 boondoggle
  - 跟项目沉没成本心理 → 越投入越不舍得停
  - 客户 gate fail 没退路 → V2 在该客户机 dead，但作者继续投入 V2 想"找新客户"→ 商业模型变化
- **建议方向**：
  1. **设计稿 §7.5 新增 stop-loss criteria**：
     - **硬 abort**（任一触发立即停）：
       - G1 / G2 / G7 / G8 / G10 / G11 客户访谈 gate ≥ 2 项 fail
       - C-027 license / C-071 TOS / C-057 GPO 任一 fail
       - 客户 PoC 拒绝合作（C-056）
     - **软 abort**（讨论决定）：
       - spike 验证 12 项中 ≥ 4 项 fail
       - 测试基础设施成本超预算 2 倍
       - spike 时间超 8 周
     - **降级 launch**（V2 部分功能 ship）：
       - 单 Blocker fail 但有降级方案 → 文档说明降级 + 客户接受后 launch
  2. **abort 后处理 SOP**：
     - 沉没成本 write-off 流程
     - V1 客户继续维护承诺
     - 测试基础设施转给 V1 维护用
  3. **决策权明确**：
     - 项目经理 + 作者共同决策（双签）
     - 客户 gate fail 客户有否决权
  4. **里程碑式审查**：
     - Phase -1 / 0 / 0.5 / 1 / 2 每阶段后强制 review，按 stop-loss 判断
     - 不是"做完才 review"
  5. **本轮 challenge 之后**（C-070 meta 暂停）：进 Phase -1 时把 stop-loss 实际触发条件细化

### C-073 [Major] V2 立项后 V1+V2 双轨长期化运维负担未估算 — 客户没动力切（C-056）→ V1 永不退役 → 作者两栈维护破产

- **针对**：design v0.5 §6.1 milestone 6 内测迁移 + §12 V1→V2 迁移策略 + C-056 客户配合度
- **问题描述**：
  设计稿假设"V2 跑稳后客户全切，V1 退役"。但 C-056 已论证**客户当前没痛点 → 没动力切 V2**。这意味着 V2 上线后**V1 仍在跑**：

  **场景 1：客户拒绝切 V2**
  - V1 客户视角：V1 v0.1.0 已稳定，操作员熟练，业务正常跑
  - V2 给客户的好处少（locale 已解 / anti-bot 持平 / 调试更好但客户不直接受益）
  - 客户拒绝 V2 投入 → V1 无限期延寿

  **场景 2：客户部分切 + 部分留**
  - 5 个账号客户切 1 个测 V2，4 个保留 V1（C-056 建议增量切的副作用）
  - 客户机长期跑 V1（4 账号）+ V2（1 账号）双系统
  - dispatcher_kind 字段（design §12.2）支持双跑，但作者要同时维护 2 套软件栈

  **场景 3：双轨维护成本**
  - V1 patchright 仍要跟进（chrome / patchright 升级 / Google contract drift）
  - V2 扩展 + Co-Pilot 同步维护
  - 作者 1 个人维护 2 个产品 → 实际单产品迭代速度减半
  - V1 bug fix 必须 backport + V2 同时 fix → 双倍工时

  **场景 4：客户支持双倍**
  - 客户问问题，作者要先问"你装的是 V1 还是 V2" → 双套排查 SOP
  - cloudflared 双套（V1 v0.0.2 cloudflared + V2 cloudflared，端口 / 配置不同）

  **场景 5：长期 V1 退役决策**
  - 客户永不主动 retire V1 → 作者主动 EOL V1？
  - EOL 后客户被迫切 V2 或换工具
  - 这是商业关系破裂触发器

  **缺失 1：双轨成本估算**
  - design §6.1 没列"V2 上线后 V1 维护"的工时
  - C-064 minor 提运营成本但没具体到双轨

  **缺失 2：V1 EOL 路径**
  - V1 什么时候停止维护？
  - 客户没切 V2 怎么办

  **缺失 3：V1 跟 V2 在同一客户机的边界**
  - dispatcher_kind 双跑技术可行（§12.2）
  - 但客户机 chrome 给 V2 用 + patchright 给 V1 用 = 2 个 chrome 进程组 = RAM 翻倍
  - C-007 5 profile RAM 已紧张，加 V1 patchright = 7-10 GB 占用

- **依据**：
  - C-056 客户没动力切 V2
  - V1 已在 5 个客户机跑（memory: project_v1_delivery）
  - design §12 双跑设计假设客户最终全切，没设计"永久双轨"
- **影响**：
  - 作者维护负担翻倍 → V2 / V1 都迭代慢 → 落后 Google contract drift
  - 客户支持工时翻倍 → 经济模型不成立
  - V1 EOL 强行触发客户关系危机
- **建议方向**：
  1. **V2 立项时 commit V1 EOL 计划**：
     - 设计稿 §12 加 "V1 EOL timeline": V2 release 6 个月后 V1 进入 maintenance only（仅 critical security fix）；12 个月后 V1 EOL 无法启动
     - 客户 V2 切换给 12 个月窗口，过期客户必须切 or 换工具
  2. **V1 EOL 客户激励**：
     - V1→V2 切换提供折扣 / 升级补贴
     - V1 沉没成本部分抵 V2 license
  3. **新客户优先 V2**：
     - V2 release 后新客户只能装 V2，不再有 V1 选项
     - 减少新增 V1 客户机维护
  4. **设计稿 §6.1 milestone 7 加 "V1 EOL 准备"**：
     - V2 上线后 6 / 12 个月里程碑
     - 客户切换率监控 + EOL 决策点
  5. **双轨维护成本算入 C-064**：
     - 双轨 6-12 个月作者 ≥ 50% 时间维护 V1
     - V2 release cadence 必然降低
  6. **V2 商业定位重审**：
     - 如果 V2 是"V1 替代"→ 必须有 EOL 时间表
     - 如果 V2 是"V1 平行新产品"→ 走新客户首发路径，老客户保留 V1 → C-056 商业模型重写

## 三、Minor（v0.0.6 新增）

### C-074 [Minor] Co-Pilot ↔ 扩展启动顺序 race — chrome 已开但 Co-Pilot 还没启完 → 扩展 register fail → 不主动 retry → 操作员困惑

- **针对**：design v0.5 §3.1 Plan A "Co-Pilot 用户进程 + 开机自启" + §4.4 reconnect 指数退避 + §5.1 操作员日常流程
- **问题描述**：
  操作员日常开机流程：
  1. 开机 → Win11 logon → Co-Pilot 自启（如配置）
  2. 操作员打开 chrome（习惯：开机后立刻 Ctrl+T）
  3. 5 profile 扩展尝试 register → ws://localhost:8080/ws/extension/<ws_id>
  4. **如果 Co-Pilot 还在启动**（FastAPI / SQLite migration / cloudflared 起来需要 5-15 秒），扩展 register fail
  5. §4.4 reconnect 指数退避 1s/2s/4s/...，但**首次 register 失败的特殊性没设计**（不是"曾经成功后断开"，是"从来没成功"）
  6. 30 秒后 Co-Pilot 起来 → 扩展可能仍在退避中（已退到 8s/16s）→ 30-60 秒延迟才 register

  **场景**：
  - 操作员看 dashboard 5 工位 offline → 困惑 "我 chrome 都开了为啥还 offline"
  - 30-60 秒后陆续 online → 操作员体验差
  - 严重时 cloudflared 比 Co-Pilot 还慢（V1 实测 cloudflared 起来 30-60 秒），扩展首次 register 期间 Co-Pilot 自身还没听 ws

  **缺失 1：启动健康信号**
  - 扩展 first register 失败应该立刻 retry 1 次（不是指数退避起点 1s）
  - 没有"等 Co-Pilot ready" 的协议握手
- **建议**：
  1. 扩展 first register 失败 → 5 秒内 retry 6 次（不指数退避，保持 5s 间隔）
  2. 6 次仍 fail → 进指数退避（说明 Co-Pilot 真没起，不是慢启动）
  3. Co-Pilot 启动后写 `%APPDATA%\FlowHarvester\copilot_ready` 文件作 readiness signal；扩展 fetch http://localhost:8080/health 200 才 register
  4. 启动顺序文档：操作员习惯先开 Co-Pilot → 等 dashboard 加载完 → 再开 chrome（或操作员等 30 秒后再开 chrome）
  5. install wizard 配置 chrome 启动延迟（Win11 任务计划 Co-Pilot logon + 30s + chrome）

---

# 下一版迭代方向（v0.0.7 暂停）

**C-070 meta 决议**：本轮 v0.0.6 之后**暂停 challenge cycle**，进入 Phase -1 测试基础设施搭建 + Phase 0 spike 实操。

**触发 v0.0.7 启动条件**：
- spike 实操出现 challenges 没覆盖的真实问题
- 客户 PoC 反馈出现新约束（类似 v0.0.4 修订段 unpacked-only）
- 架构层有 breaking 变化
- design 演进到 v0.6+ inline 集成 v0.0.5 + v0.0.6 后

**不触发**（继续暂停）：
- 纯纸面推演的新角度
- 已有 challenges 同主题深挖
- 实施细节问题（DevX / UI 框架 / release engineering）
- minor UX 完善

**待 spike 实测后再决定的关键问题**（暂存 backlog，不写入 challenges）：
- React fiber / MutationObserver / fetch 拦截 在 labs.google CSP 下技术真实可行性（C-041）
- 客户机 chrome 升级实际频率 + V2 升级摩擦真实数据
- Veo 配额 daily 实际上限 + cancel 后实际扣费行为（C-061）
- chrome 扩展长跑 1 周 RAM / CPU 实测增长

**累计 70 条 challenges**（v0.0.1 13 + v0.0.2 13 + v0.0.3 14 + v0.0.4 12 + v0.0.4 修订 2 + v0.0.5 13 + v0.0.6 5）。design 对偶迭代到 v0.6（v0.5 已 inline 集成 v0.0.4 + 修订，v0.6 集成 v0.0.5 + v0.0.6）。
