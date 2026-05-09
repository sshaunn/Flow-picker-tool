# V1 Fragility Regression Suite (V2 对应处理)

**版本**：v0.1（spike Phase 0.5 起草，对应 design v0.4 §16 / C-032）
**日期**：2026-05-09
**状态**：草案，spike Phase 0.5 期间逐条补 reproducer fixture + V2 状态标注

## 用途

V1 worker.flow_playwright 1900 行 = v0.0.1 → v0.1.0 6 轮迭代踩出来的 35 条 fragility 处理。
V2 重写 TS 后，**判定每条 fragility 在 V2 是 fix / 架构层消除 / 新机制处理的标准**必须存在。
端到端测试无法证明每条 fragility 在 V2 是 fix 状态——可能"刚好这次没撞到"。
本文档逐条对应：

- **V2 仍存在**：行为层 fragility，扩展同样会撞 → 必须重新实现（移植 V1 fix 到 TS）
- **V2 架构层消除**：patchright 特有的 fragility（webdriver / CDP fingerprint） → 仍要测，证明真消除
- **V2 用新机制处理**：V1 SQL filter / Python state machine 在 V2 改成扩展 / chrome.storage / WS protocol → 测新机制行为对齐

## 验收

- 每条 fragility 必须有 reproducer fixture（DOM snapshot / 网络 stub / 时序 fixture）
- CI 每次 V2 build 跑 35 条 regression
- **必须 35/35 pass 才能 release**（design v0.4 §7.2 第 10 项）
- regression 失败的 commit 自动 revert

## 35 条 fragility 跟 V2 对应

来源：`memory/project_known_fragility.md`。

### A. Flow 平台行为（12 条）

| # | V1 fragility | V2 状态 | V2 处理 | reproducer fixture |
|---|---|---|---|---|
| 1 | Veo 3.1 Fast 输出无声音 mp4 | V2 仍存在 | 扩展拿到 mp4 不校验音轨；客户文档说明 | `tests/fixtures/v1_dom/veo31_fast_silent.json`（task config + expected mp4） |
| 2 | 马来语 / 小语种 prompt 触发 audio_generation_failed | V2 仍存在 | 扩展端 phrase 检测 + 同 round retry 3 次（移植 V1 retry 逻辑到 TS） | mock Veo 后端 audio_failure response 3 次后成功 |
| 3 | 新 project 默认模型不是 Veo（可能 Nano Banana） | V2 仍存在 | 扩展 mode preset 自动切换；fail 时 fail-fast WS 上报中控 | DOM snapshot of model selector 默认状态 |
| 4 | 同账号 ~12-15 generations 触发 unusual_activity | V2 仍存在（但概率降低） | strike 系统按 email 累计；扩展端检测 phrase + WS 上报 | mock Flow page phrase show "We noticed some unusual activity" |
| 5 | 多账号同 IP 并发立刻全员 ban；必须 stagger 60s/120s | V2 仍存在 + reconnect storm 加剧（C-034） | 中控 stagger_sec 沿用；reconnect 加 jitter；register stagger | 5 ws 同时 register 时序 fixture |
| 6 | Flow 风控不是真"封号"是 sticky flag；strike 5 次后 manual_check | V2 仍存在 | 沿用 V1 strike 状态机（按 email），扩展自动驱动 | mock 5 次连续 unusual_activity → 工位 manual_check |
| 7 | Flow project library 重复入库 | V2 仍存在 | 扩展 reuse 路径（先点已有 thumbnail）— TS 重写 | DOM snapshot of project library |
| 8 | Veo poster image 早出现，mp4 60-90s 后挂；UUID 去重 + early-exit | V2 仍存在 | TS 端 MutationObserver 监听 video src；UUID 去重 | DOM snapshot generation list with poster but no mp4 |
| 9 | prompt-attach 必须点 `<img>` 本身；click strategy `img-direct` 第一优先 | V2 仍存在 | TS 端 element.click() 直接点 img；移植 V1 4 种策略 | DOM snapshot of prompt-attach modal |
| 10 | Stale "Failed" card phrase；`inner_text` 不是 `textContent` | V2 用新机制处理 | TS 端 element.innerText（chrome 内置不同于 patchright） + count 增量识别 | DOM snapshot with stale failed card + new generation |
| 10a | Flow 多种 phrase 状态（service_unavailable / unusual_activity / no_flow_access） | V2 仍存在 | 扩展端按 [config/flow-selectors.yaml] 完整匹配（特别 no_flow_access 必须完整匹配） | DOM snapshots for 3 phrase types |
| 11 | `keyboard.insert_text` 触发 Veo Failed；必须 `keyboard.type` 60-110ms | V2 用新机制处理（更激进） | TS 端 setReactInputValue（Object.defineProperty + dispatchEvent input） — **绕过 Grammarly 等扩展** | mock textarea + Grammarly extension behavior |
| 12 | patchright `page.url` 不跟 SPA 路由；3 路并行检测 | V2 架构层消除 | content script 直接拿 location.href（chrome 不缓存） | SPA navigation timeline fixture |

### B. Locale 层（5 条）

| # | V1 fragility | V2 状态 | V2 处理 | reproducer fixture |
|---|---|---|---|---|
| 13 | Flow UI 完全跟随 Google 账号偏好语言 | V2 仍存在 | spike #2 验证扩展能否 locale-independent；失败退多语言列表 + fail-fast | DOM snapshot 越南语 / 中文 / EN |
| 14 | PROJECT_URL_RE 必须接受 optional locale 段 | V2 仍存在 | 中控 protocol.flow_project_url 字段沿用 regex；扩展自己识别 URL | URL fixture set with locale segments |
| 15 | Text-based selector 必须多语言列表化（13 语言）+ fast-fail iteration + dump visible buttons | V2 仍存在 | TS 端移植 13 语言 selector list + chrome.tabs.captureVisibleTab 截图上报 | 13 个 DOM snapshot per language |
| 16 | Accept-Language header rewrite 实测无效 | V2 架构层消除 | 扩展不需要改 header（chrome 自带正确 Accept-Language） | N/A |
| 17 | v0.1.0 操作员驱动账号语言切换是终极解 | V2 仍存在但简化 | 扩展自带账号管理页 + 复用 myaccount.google.com/language?hl=en 流程 | DOM snapshot myaccount language page |

### C. 调度层（7 条）

| # | V1 fragility | V2 状态 | V2 处理 | reproducer fixture |
|---|---|---|---|---|
| 18 | Daemon workstation 列表 stale；每 pass 重读 DB | V2 仍存在 | scheduler/daemon.py 沿用 | unit test |
| 19 | Task retry_waiting 卡 N/N 永远不变 failed | V2 仍存在 | claim 守卫 + auto-resume cap 3 次沿用 | sqlite fixture |
| 20 | claim 必须过滤 flow_project_url（v0.0.3） | V2 用新机制处理 | claim filter 仍 SQL；V2 register 时校验 expected_email + bound_profile_id_hash | sqlite fixture with empty flow_project_url ws |
| 21 | resume 时 `generation_round_count` + `MAX(generation_round)` 权威源 | V2 仍存在 | 中控逻辑沿用；扩展 task_resume 协议同步状态 | task lifecycle fixture |
| 22 | 进程重启孤儿任务自愈 | V2 仍存在 | reset_zombie_state_on_startup 沿用；V2 加 chrome.storage 二次确认 | startup fixture |
| 23 | Captcha / login_required 夜间无人值守 | V2 仍存在 | 扩展上报 captcha；中控按 captcha_action 处理 | DOM snapshot captcha modal |
| 24 | Strike 不会自动清零 | V2 仍存在但目标改 email | strike 按 email 累计（C-018） + 触发后 cooldown 24-72h（C-028）；连续 5 次 disable | unit test |

### D. Silent failure 防护（4 条）

| # | V1 fragility | V2 状态 | V2 处理 | reproducer fixture |
|---|---|---|---|---|
| 25 | download_candidate 写文件 try/except + size verify | V2 用新机制处理 | chrome.downloads API 内置错误处理；onChanged 拿最终 filename + size 校验 | mock chrome.downloads error scenarios |
| 26 | loop._download_round catch all | V2 用新机制处理 | extension_dispatcher.py 等待 task_complete with timeout；任何异常 → save_error_snapshot | extension dispatcher unit test |
| 27 | runner.multi exc_info=True | V2 用新机制处理 | extension_dispatcher 错误日志带堆栈 | unit test |
| 28 | login_session 三路 except 全 log + 30s forensic deep dump | V2 用新机制处理 | 扩展自管登录态；options 页绑定 wizard；失败时扩展端 chrome.tabs.captureVisibleTab + WS log push | options page fixture |

### E. PyInstaller bundle / runtime 陷阱（7 条）

| # | V1 fragility | V2 状态 | V2 处理 | reproducer fixture |
|---|---|---|---|---|
| 29 | `_MEIPASS` 在 onedir 模式下指向 `_internal/` | V2 仍存在 | sys._MEIPASS 沿用；V2 不需要带 patchright（包小） | bundle integration test |
| 30 | 空目录不能放 datas | V2 仍存在 | spec 沿用 | bundle integration test |
| 31 | 运行时找文件别 chdir；多候选搜路径 | V2 仍存在 | _find_bundled_yaml 沿用 | runtime path test |
| 32 | patchright 子进程 Chrome 残留 | V2 架构层消除 | chrome 不是我们启的（Plan A）；Plan B 仍需 profile_check 移植 | N/A (Plan A) / chrome cleanup fixture (Plan B) |
| 33 | logger 双倍记录 | V2 仍存在 | _setup_file_logging 沿用 | logger unit test |
| 34 | Bundled exe 启动崩；console=True + crash handler | V2 仍存在 | spec 沿用；V2 加 install wizard 检测 | bundle launch test |
| 35 | cloudflared 隧道 SRV DNS 被拦；`--protocol http2` | V2 仍存在 | cloudflared 沿用（C-017 v0.3 反转保留） | cloudflared launch test |

## fixture 库目录约定

```
tests/fixtures/v1_dom/
├── flow_phrase_unusual_activity.html       # #4, #6
├── flow_phrase_no_flow_access.html         # #10a
├── flow_phrase_service_unavailable.html    # #10a
├── flow_project_library.html               # #7
├── flow_generation_with_poster_only.html   # #8
├── flow_prompt_attach_modal.html           # #9
├── flow_stale_failed_card.html             # #10
├── flow_locale_vi.html                     # #13, #15
├── flow_locale_zh_CN.html                  # #13, #15
├── flow_locale_en_US.html                  # #13, #15
├── myaccount_language_page.html            # #17
├── flow_captcha_modal.html                 # #23
└── ...

tests/fixtures/v1_network/
├── veo_audio_failure_response.json         # #2
├── veo_unusual_activity_response.json      # #4
└── ...

tests/fixtures/v1_timing/
├── reconnect_storm_5ws.json                # C-034
├── sw_hibernate_30s.json                   # C-001
└── ...
```

## CI 集成

```yaml
# .github/workflows/v2-regression.yml (待 V2 spike 后建)
name: V2 Fragility Regression

on:
  push:
    branches: [main, v2-*]
  pull_request:

jobs:
  regression:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: cd extension && npm ci && npm run test:regression
      - run: pytest tests/regression -v --tb=short
      # 必须 35/35 pass
      - run: |
          PASS=$(grep -c "PASS" regression-report.txt)
          if [ "$PASS" -ne 35 ]; then exit 1; fi
```

## 状态标注汇总

- **V2 仍存在**：22 条（行为层 fragility 都要移植）
- **V2 架构层消除**：3 条（#12 SPA URL / #16 Accept-Language / #32 patchright Chrome 残留）
- **V2 用新机制处理**：10 条（DOM 直读 / chrome API / WS protocol 替代）

**结论**：V2 重写不能省掉 V1 fragility 处理。**60% 行为层 fragility 必须 1:1 移植**，否则 V2 release 会复发 V1 已修 bug → 客户失去信任。

## 后续

- spike Phase 0.5（1 周）：本文档每条补 fixture
- spike 阶段：开始 V2 实现 + 跑 fixture
- V2.0 release：35/35 pass 才可发版
- V2.x 升级：每次新版必须 35/35 pass
