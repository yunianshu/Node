---
name: novel-gen
description: 使用 D:/AiProject/Node 当前 scripts 自动化框架生成和维护长篇中文网络小说项目。适用于创建小说项目、运行 Planner/Outliner/Outline Reviewer/Writer/Reviewer/Coordinator、断点续传、企业微信进度推送、章节质量门和补齐缺失章节。当前标准为 Planner 只生成 world.json 与 characters.json，大纲由 Outliner 生成到 chapters/outline/chapter_XXXX.json，禁止生成或依赖根目录 outline.json。
---

# Novel Gen

在 `D:/AiProject/Node` 中使用当前 `scripts/` 自动化框架处理小说生成任务。优先读取现有代码和项目配置，不沿用旧的 `novels/` 公共框架假设。

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
| Coordinator | `scripts/pipeline/coordinator.py` | 调度全流程、断点续传、质量门、终稿晋级、配额、推送 | `chapters/final/chapter_XXXX.txt`, `reports/progress.json`, 日志 |

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
Outliner
   ↓ chapters/outline/chapter_XXXX.json
Outline Reviewer（大纲审查，门槛 8.5 分）
   ↓ chapters/outline_review/chapter_XXXX_review.json
Writer
   ↓ chapters/draft/chapter_XXXX.txt
Reviewer
   ↓ chapters/review/chapter_XXXX_review.json
Coordinator promote
   ↓ chapters/final/chapter_XXXX.txt
Push/Reports
```

阶段门控：
- Planner 只负责 `world.json` 与 `characters.json`。
- 每章必须按 Outliner -> Outline Reviewer -> Writer -> Reviewer -> final 顺序处理。
- **Outline Reviewer** 在 Writer 之前运行，审查每章大纲质量。评分维度：剧情吸引力、节奏把控、人物动机合理性、爽点设计、伏笔与呼应、场景多样性、力量体系一致性、整体可写性。门槛默认 **8.5 分**。
- 大纲审查不通过时，Outliner 带审查意见重生成同一章；同一轮最多 3 次，失败原因分析最多 3 轮，仍不通过则退出并推送原因。
- 初稿审查不通过时，Writer 带审查意见重生成同一章；同一轮最多 3 次，失败原因分析最多 3 轮，仍不通过则退出并推送原因。
- 只有审查通过的初稿才由 Coordinator 写入 `chapters/final/chapter_XXXX.txt`。
- 继续生成长篇项目时，优先运行 `scripts/pipeline/coordinator.py` 断点续传，不手工逐章调用 Writer。
- 流程代码只能修改 `scripts/`，小说阅读器只能修改 `novels-dashboard/`。

## 常用命令

始终在仓库根目录执行：

```powershell
cd "D:/AiProject/Node"
```

生成基础设定：

```powershell
python "scripts/pipeline/planner.py" --project "D:/AiProject/Node/projects/<book_id>"
```

生成指定范围大纲：

```powershell
python "scripts/pipeline/outliner.py" --project "D:/AiProject/Node/projects/<book_id>" --start 1 --end 100
```

运行完整协调器：

```powershell
python "scripts/pipeline/coordinator.py" --project "D:/AiProject/Node/projects/<book_id>"
```

指定章节范围：

```powershell
python "scripts/pipeline/coordinator.py" --project "D:/AiProject/Node/projects/<book_id>" --start 1 --end 200 --batch-size 20
```

审查单章大纲：

```powershell
python "scripts/pipeline/outline_reviewer.py" --project "D:/AiProject/Node/projects/<book_id>" --start 1 --end 100
```

发送单项目企业微信进度：

```powershell
python "scripts/maintenance/wechat_notify.py" --project "D:/AiProject/Node/projects/<book_id>"
```

阅读器代码位于 `novels-dashboard/`，不要在 `scripts/` 中新增阅读器入口。

## 配置要点

项目配置文件为 `projects/<book_id>/config.json`。常用字段：

```json
{
  "total_chapters": 2000,
  "model": "MiniMax-M3",
  "mmx_path": "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs",
  "api_qps": 5.0,
  "writer": {
    "max_retries": 3,
    "retry_delay": 5.0
  },
  "coordinator": {
    "batch_size": 20,
    "num_workers": 5,
    "review_workers": 2,
    "pause_between_batches": 3.0
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
  }
}
```

不要硬编码项目根路径。脚本应通过 `--project` 或 `NOVEL_PROJECT_DIR` 读取项目目录。

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

## 补齐与维护

补齐缺失章节时优先运行 Coordinator 的断点续传与质量门，不恢复旧 `scripts/batch/`、`scripts/cli/`、`scripts/one_off/` 入口。

补齐大纲时只写：

```text
chapters/outline/chapter_XXXX.json
```

不要写：

```text
outline.json
chapters/outline/index.json
```

## 验证

修改小说自动化代码后至少运行：

```powershell
python -m py_compile "scripts/pipeline/planner.py" "scripts/pipeline/outliner.py" "scripts/pipeline/outline_reviewer.py" "scripts/pipeline/writer.py" "scripts/pipeline/reviewer.py" "scripts/pipeline/coordinator.py" "scripts/maintenance/coordinator_watchdog.py" "scripts/maintenance/wechat_notify.py" "scripts/core/workflow_state.py"
```

涉及推送、阅读器或批处理脚本时，追加对应文件到 `py_compile`。

关键回归点：
- `rg "outline\.json" "scripts"` 只应出现“不生成 outline.json”的测试断言，不能出现业务读写。
- Outliner 无 `--outline-file` 时不得生成 `outline.json` 或 `chapters/outline/index.json`。
- Coordinator 大纲完成数必须来自单章大纲文件。
- Coordinator 必须按单章质量门处理，不允许大纲失败后继续生成下一章。
