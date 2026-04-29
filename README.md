# Flow Picker Tool

Flow Picker Tool 是一个面向 Google Flow 视频生成的最小可用执行系统草案。它以 Segment 为基本调度单位，多个 Segment 通过 `creative_id` 聚合归属于同一套脚本；只接管「已验证源素材（首帧图或衔接素材）+ 已验证视频 Prompt + 目标候选数量 `target_count`」之后的高重复执行环节：自动派发任务、打开 Flow、上传源素材、粘贴 Prompt、循环触发生成直到 `downloaded_count >= target_count`、下载结果、按 `日期/SKU/Creative/Segment` 归档文件，并在 unusual activity、登录异常、验证码或页面异常出现时暂停对应工位，避免整批任务停摆。

## 当前阶段

当前仓库处于项目初始化阶段，已准备：

- README 项目入口
- `.gitignore` 初始规则
- `docs/` 项目文档拆分

## MVP 边界

本项目只做：

> 已验证源素材 + 已验证 Segment 视频 Prompt + `target_count` -> Flow 按 Segment 循环生成候选视频 -> 下载落盘 -> 异常熔断 -> 第二天按 creative 聚合收菜

本阶段不做 Prompt 生成、首帧图生成、首帧图筛选、视频质量判断、自动剪辑、验证码绕过，也不承诺规避 Flow 的风控。任务最终未达 `target_count` 进入 `failed` 时，已落盘候选不被回滚，日报独立列出「未达标但有产出」段。

## 第一版技术栈

| 模块 | 选型 |
|---|---|
| 语言 | Python |
| 浏览器自动化 | Playwright |
| 数据库 | SQLite |
| 任务导入 | CSV |
| 配置 | YAML |
| 结果存储 | 本地文件夹 |
| 运行方式 | CLI |
| 日志 | 本地 log 文件 |

## 文档索引

- [项目范围](docs/product-scope.md)
- [客户需求与 MVP 验收](docs/requirements.md)
- [客户流程适配与冲突检查](docs/customer-workflow-fit.md)
- [系统架构](docs/architecture.md)
- [执行流程与调度规则](docs/workflow-and-scheduling.md)
- [数据结构与文件目录](docs/data-and-storage.md)
- [日报与人工验收](docs/operations-and-reports.md)
- [版本计划与交付清单](docs/roadmap.md)
- [MVP 开发计划与任务拆分](docs/development-plan.md)
- [输入与配置模板](docs/templates.md)

## 目标目录草图

```text
flow-harvester/
├── input/
│   ├── tasks.csv
│   └── images/
│       ├── stroller_001_creative_001_A.png
│       └── stroller_001_creative_001_B.png
├── output/
│   └── 2026-04-28/
│       ├── stroller_001/
│       │   └── stroller_001_creative_001/
│       │       ├── segment_A/
│       │       │   ├── T001_round_01_seq_01.mp4
│       │       │   ├── T001_round_01_seq_02.mp4
│       │       │   └── screenshots/
│       │       ├── segment_B/
│       │       └── creative_summary.md
│       └── daily_report.md
├── profiles/
│   ├── workstation_A/
│   ├── workstation_B/
│   └── workstation_C/
├── logs/
│   ├── scheduler.log
│   ├── worker_WS_A.log
│   └── errors.log
├── config/
│   ├── workstations.yaml
│   └── settings.yaml
├── app/
│   ├── scheduler/
│   ├── worker/
│   ├── db/
│   ├── reports/
│   └── utils/
├── docs/
└── README.md
```

输出文件命名：`{task_id}_round_{generation_round}_seq_{sequence_no}.mp4`，便于回溯每条候选所属的生成轮次。

## 核心验收结果

第二天需要能直接查看：

- 按 `日期/SKU/Creative/Segment` 归档的成功生成并下载的视频
- 失败任务列表（`failed`）
- 下载失败任务列表（`download_failed`）
- 「未达标但有产出」任务列表（`status IN ('failed', 'download_failed')` 且 `0 < downloaded_count < target_count`）
- 每个任务的 `downloaded_count / target_count` 进度
- 每个 `creative_id` 下各 Segment 的聚合视图
- 每个工位的执行状态（含 `cooldown` 到期时间）
- unusual activity 触发记录
- 错误截图（带 `generation_round` 标注）
- 每日执行报告
