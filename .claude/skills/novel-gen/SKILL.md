---
name: novel-gen
description: 使用当前仓库的 scripts 自动化框架生成和维护长篇中文网络小说项目。适用于创建小说项目、运行 Planner/Outliner/Writer/Reviewer/Rewrite/Coordinator、多 Agent 批量生成大纲、断点续传、企业微信进度推送、章节质量门、补齐缺失章节、多媒体生成和阅读器调试。当前标准为 Planner 只生成 world.json 与 characters.json，大纲由 Outliner 生成到 chapters/outline/chapter_XXXX.json，禁止生成或依赖根目录 outline.json。
---

# Novel Gen

在当前仓库中使用 `scripts/` 自动化框架处理小说生成任务。优先读取现有代码和项目配置，不沿用旧的 `novels/` 公共框架假设。仓库迁移到新目录后，优先使用 `--project` 或 `NOVEL_PROJECT_DIR` 指向项目目录；未指定时脚本会尝试从当前项目目录、当前仓库唯一 `projects/<book_id>`、或本 skill 所在仓库的唯一项目自动识别。

## 当前标准结构

项目目录位于 `projects/<book_id>/`，标准结构由 `scripts/core/novel_config.py::ensure_project_structure()` 创建：

```text
projects/<book_id>/
├── config.json
├── premise.txt
├── world.json
├── characters.json
├── chapters/
│   ├── outline/
│   │   └── chapter_0001.json
│   ├── outline_review/
│   │   └── chapter_0001_review.json
│   ├── draft/
│   │   └── chapter_0001.txt
│   ├── review/
│   │   └── chapter_0001_review.json
│   └── final/
│       └── chapter_0001.txt
├── logs/
├── media/
│   ├── audio/
│   ├── images/
│   └── music/
├── origin/
└── reports/
```

硬性约束：
- 不在小说根目录生成 `outline.json`。
- 不依赖 `chapters/outline/index.json` 作为主数据源。
- 大纲是“一章一个文件”：`chapters/outline/chapter_XXXX.json`。
- 旧脚本或旧项目如果出现 `outline.json`，只当历史文件处理，不作为新流程输出。

## Agent 职责

| Agent | 入口 | 职责 | 输出 |
|---|---|---|---|
| Planner | `scripts/pipeline/planner.py` | 只生成世界观和角色档案 | `world.json`, `characters.json` |
| Outliner | `scripts/pipeline/outliner.py` | 按范围生成章节大纲 | `chapters/outline/chapter_XXXX.json` |
| Outline Reviewer | `scripts/pipeline/outline_reviewer.py` | 审查单章大纲质量（门槛 **8.5 分**） | `chapters/outline_review/chapter_XXXX_review.json` |
| Writer | `scripts/pipeline/writer.py` | 根据单章大纲生成初稿 | `chapters/draft/chapter_XXXX.txt` |
| Reviewer | `scripts/pipeline/reviewer.py` | 审查初稿并评分 | `chapters/review/chapter_XXXX_review.json` |
| Coordinator | `scripts/pipeline/coordinator.py` | 调度全流程、断点续传、配额、推送 | `reports/progress.json`, 日志 |

核心状态工具在 `scripts/core/workflow_state.py`：
- `load_outline_chapter()` 只从单章大纲文件读取。
- `outline_completed_count()` 统计单章大纲完成数。
- `outlines_complete()` 判断大纲是否全量完成。
- `scan_chapter_status()` 扫描 draft/review/final 状态。

## 标准流程

```text
config/premise
   ↓
Planner
   ↓ world.json + characters.json
Chapter N loop:
  Outline lookahead（默认10章）
     ↓ 先确保第 N 到 N+9 章大纲均已生成并通过审查
  Outliner
     ↓ chapters/outline/chapter_XXXX.json
  Outline Reviewer（大纲审查）
     ↓ 不通过：下一次 Outliner 必须立即带审查意见重生成同一章大纲；通过：进入 Writer
  Writer
     ↓ chapters/draft/chapter_XXXX.txt
  Reviewer
     ↓ 不通过：带审查意见重新生成同一章初稿；通过：写入 final
  Final
     ↓ chapters/final/chapter_XXXX.txt
Push/Reports
```

阶段门控：
- Planner 只负责 `world.json` 与 `characters.json`。
- 长篇续跑时允许三个 lane 同时运行：大纲 lane 负责 Outliner/Outline Reviewer，初稿 lane 负责 Writer/Reviewer，推送 lane 负责企业微信进度。大纲或正文 lane 启动时必须先确保独立企业微信推送进程 `scripts/maintenance/wechat_pusher_lane.py` 已运行。推送进程是旁路 subagent，只读取 `reports/progress.json`、章节产物与审查产物统计进度，不调用模型、不参与大纲/正文质量门、不持有生成链路锁；`logs/wechat_pusher.lock` 用于跨进程单例，避免重复推送。
- 大纲/正文 lane 启动时必须自动确保对应只读监控 subagent 已运行：`scripts/maintenance/gate_watchdog.py --mode outline --interval 300` 检查大纲审查卡章，`scripts/maintenance/gate_watchdog.py --mode draft --interval 300` 检查初稿审查卡章。完整 Coordinator 启动时必须确保两个 watchdog 都运行。它们只读取进程、日志和章节状态，写入 `logs/outline_gate_watchdog.log` / `logs/draft_gate_watchdog.log`，发现 lane 停止或同一章节连续无推进时推送企业微信告警；`logs/outline_gate_watchdog.lock` / `logs/draft_gate_watchdog.lock` 用于跨进程单例，避免重复启动。
- Coordinator 按正文单章推进，但写第 N 章正文前，必须先维护大纲提前窗口：默认确保第 N 到第 N+9 章大纲都已生成并通过大纲审查；窗口大小由 `coordinator.outline_lookahead_chapters` 或 CLI `--outline-lookahead` 控制。
- **Outliner 单章模式必须生成完整大纲**。单章大纲必须包含 `chapter_number/title/summary/characters_involved/location/mood/key_events/foreshadowing/power_progression/word_count_target`。其中 `summary` 必须是具体剧情摘要，`key_events` 至少 3 条，`foreshadowing` 与 `power_progression` 不得缺失或为空；不能把“200字详细摘要”“章节标题”“事件1”等模板占位词写入文件。结构不完整时 Outliner 应非零退出且不得落盘。
- **生成端必须携带压缩质量标准**。Outliner/Writer 每次生成或重生成都应携带一段短质量契约，而不是完整审查 Prompt：Outliner 需包含大纲门槛分、前后章衔接、独立冲突、summary/key_events/foreshadowing/power_progression 硬要求；Writer 需包含正文门槛分、字数、承接、节奏推进、角色动机、章末钩子和禁止水文等硬要求。审查意见仍是重生成时的最高优先级输入，完整评分细则主要保留给 Reviewer。
- **审查反馈必须压缩后再传给生成端**。多轮失败时只保留最高分、最近一轮 verdict/score、前 6 条核心失败原因、前 6 条必要修正和少量最新建议；不要把完整历史 reviews、长 failure_analysis 或大段 origin 全量塞入 Outliner/Writer。单章大纲输出也要控制长度：`key_events` 建议 5-7 条、每条不超过 90 字，优先保证 JSON 闭合和必填字段完整。
- **Outline Reviewer** 紧跟 Outliner。大纲每轮最多生成/审查 3 次；每一次大纲审查未通过、Outliner 生成失败、JSON 解析失败或结构字段不完整后，Coordinator 都必须立即写入 `logs/outline_feedback_chXXXX_roundN.json` 并在下一次 Outliner 调用中传入 `--review-feedback`，不能等到下一轮才带反馈。3 次仍不通过时进入下一轮原因调整；最多 3 轮；仍不通过则停止全流程并推送企业微信错误。
- 当 `outline_race.enabled=true` 时，大纲质量门必须使用候选并行赛马：同一章并发启动多个 Outliner 候选，每个候选写入 `logs/outline_candidates/chXXXX/roundR_attemptA/candidate_NN.json`，对应 Outline Reviewer 写入同目录候选审查文件；任一候选达到 `outline_reviewer.min_score` 后，Coordinator 立即发布该候选到正式 `chapters/outline/chapter_XXXX.json` 与 `chapters/outline_review/chapter_XXXX_review.json`，并停止同章其他候选进程。所有候选均失败时，汇总最佳分数与失败原因写入下一轮 `--review-feedback`。候选流程不得让多个 Outliner/Reviewer 直接抢写正式大纲文件。
- 如果大纲审查意见明确指出“与后章重叠/重复/冲突”，默认按“后章有问题”处理：Coordinator 先删除并重写后章大纲，再回头重审当前章，避免把边界错算到前章。
- **Writer/Reviewer** 紧跟已过审大纲执行。`scripts/maintenance/draft_lane.py` 可用 `--workers` 并发生成初稿，但每章启动前必须确认本章大纲已通过；并发必须受 `--continuity-window` 约束，避免后文无限越过前文。Writer 必须读取前后章节大纲、前一章结尾和审查反馈来处理章节承接；Reviewer 写共享进度文件时应避免并发写冲突。初稿重写次数按 `coordinator.draft_attempts_per_round × coordinator.draft_analysis_rounds` 执行；不通过时 Writer 必须带着上一轮 Reviewer 审查意见重写同一章。耗尽配置次数仍不通过时，Coordinator/draft lane 汇总原因并停止全流程或当前 lane，推送企业微信错误。
- 初稿审查通过后，Coordinator 将合格初稿写入 `chapters/final/chapter_XXXX.txt`。普通流程不再依赖全局 rewrite 队列作为主路径。
- `outline_reviewer.min_score` 控制大纲审查门槛，`reviewer.min_score` 控制初稿审查门槛。不要在代码或手工命令中硬编码分数。
- 当用户要求“最终 8.5 评分”或同等质量优先目标时，项目配置应将 `outline_reviewer.min_score` 与 `reviewer.min_score` 都设为 8.5；大纲/正文重生成次数可提高到 5 轮×5 次。正文 lane 遇到 `需修改`、`需重写` 或低于门槛时必须继续带审查意见重写，不能第一次失败就停止。
- 继续生成长篇项目时，优先运行 `scripts/pipeline/coordinator.py` 断点续传，不手工逐章调用 Writer/Reviewer，除非是在定位单章故障。

## 常用命令

优先在仓库根目录执行；项目迁移后把 `<repo>` 替换成新仓库路径：

```powershell
cd "<repo>"
```

迁移或接手项目前先运行自检：

```powershell
python "scripts/maintenance/portable_check.py" --project "projects/<book_id>"
```

生成基础设定：

```powershell
python "scripts/pipeline/planner.py" --project "projects/<book_id>"
```

生成指定范围大纲：

```powershell
python "scripts/pipeline/outliner.py" --project "projects/<book_id>" --start 1 --end 100
```

运行完整协调器：

```powershell
python "scripts/pipeline/coordinator.py" --project "projects/<book_id>"
```

指定章节范围：

```powershell
python "scripts/pipeline/coordinator.py" --project "projects/<book_id>" --start 1 --end 200 --batch-size 20
```

审查单章大纲：

```powershell
python "scripts/pipeline/outline_reviewer.py" --project "projects/<book_id>" --start 1 --end 100
```

发送单项目企业微信进度：

```powershell
python "scripts/maintenance/wechat_notify.py" --project "projects/<book_id>"
```

## 配置要点

项目配置文件为 `projects/<book_id>/config.json`。常用字段：

```json
{
  "total_chapters": 2000,
  "model": "MiniMax-M3-highspeed",
  "mmx_path": "mmx",
  "api_qps": 5.0,
  "writer": {
    "max_retries": 3,
    "retry_delay": 5.0
  },
  "coordinator": {
    "batch_size": 20,
    "num_workers": 4,
    "review_workers": 2,
    "pause_between_batches": 3.0,
    "push_interval_seconds": 120,
    "outline_lookahead_chapters": 10,
    "outline_attempts_per_round": 3,
    "outline_analysis_rounds": 3,
    "draft_attempts_per_round": 3,
    "draft_analysis_rounds": 3
  },
  "outline_race": {
    "enabled": true,
    "candidates": 3,
    "max_workers": 3,
    "stop_on_first_pass": true
  },
  "outline_reviewer": {
    "max_tokens": 4096,
    "temperature": 0.3,
    "min_score": 8.5
  },
  "quality": {
    "min_chapter_words": 5000,
    "max_chapter_words": 12000,
    "hard_fail_min_chapter_words": 3000,
    "title_required": false
  },
  "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>"
}
```

不要硬编码项目根路径。脚本应通过 `--project` 或 `NOVEL_PROJECT_DIR` 读取项目目录；未指定时可在项目目录或单项目仓库根目录运行，让脚本自动识别。`mmx_path` 支持 `.mjs/.js` 文件路径，也支持 PATH 上的 `mmx`/`mmx-cli` 命令；迁移到新机器时可用 `NOVEL_MMX_PATH` 或 `MMX_PATH` 临时覆盖。

新建或接手项目时，必须先确认企业微信推送地址：
- 优先写入项目 `config.json` 的 `webhook_url`。
- 也可用环境变量 `NOVEL_WEBHOOK_URL` 临时覆盖。
- 不要把 webhook 写死到通用脚本中。
- 配置后可用 `python "scripts/maintenance/wechat_notify.py" --project "projects/<book_id>"` 验证推送。

## 进度与推送

统一企业微信格式由 `scripts/core/push_notifier.py::build_progress_message()` 生成：

```text
📖 《书名》生成进度 (YYYY-MM-DD HH:MM:SS)
━━━━━━━━━━━━━━━━━━━━
📋 大纲: 1450/2000 章
📋 大纲审: 1450/2000 章
✍ 初稿: 415/2000 章
📝 字数: 2,547,051
🔍 审查: 0/2000 章
📤 终稿: 0/2000 章
🤖 活跃进程: 49
━━━━━━━━━━━━━━━━━━━━
```

不要恢复旧格式：
- `《书名》进度更新`
- `Coordinator已启动，正在生成中..`
- 只显示四行进度且无字数/活跃进程的格式

## 验证

修改小说自动化代码后至少运行：

```powershell
python -m py_compile "scripts/pipeline/planner.py" "scripts/pipeline/outliner.py" "scripts/pipeline/outline_reviewer.py" "scripts/pipeline/coordinator.py" "scripts/pipeline/writer.py" "scripts/pipeline/reviewer.py" "scripts/core/json_repair.py" "scripts/core/mmx_client.py" "scripts/core/novel_config.py" "scripts/core/push_notifier.py" "scripts/core/workflow_state.py" "scripts/maintenance/coordinator_watchdog.py" "scripts/maintenance/wechat_notify.py" "scripts/maintenance/outline_lane.py" "scripts/maintenance/draft_lane.py" "scripts/maintenance/wechat_pusher_lane.py" "scripts/maintenance/gate_watchdog.py" "scripts/maintenance/portable_check.py"
```

涉及推送或守护脚本时，追加对应文件到 `py_compile`。

关键回归点：
- `rg "outline\.json" "scripts"` 不能出现业务读写。
- Outliner 无 `--outline-file` 时不得生成 `outline.json` 或 `chapters/outline/index.json`。
- Coordinator 大纲完成数必须来自单章大纲文件。
- Coordinator 写正文前必须维护默认 10 章大纲提前窗口，可用 `--outline-lookahead` 临时覆盖。
- 大纲重生成必须从第一次失败后的下一次 attempt 起立即带 `--review-feedback`，不能前三次裸跑。
- 单章 Outliner 输出必须通过结构校验：`key_events` 不得为空，`foreshadowing`/`power_progression` 不得缺失，模板占位文本不得落盘。
- 若审查反馈明确指向后章内容重叠，优先修复后章，再重审当前章，不要把该类边界问题误判成当前章的硬失败。
- 初稿通过 Reviewer 后由 Coordinator 直接写入 final，不再依赖全局 Rewrite 队列。
