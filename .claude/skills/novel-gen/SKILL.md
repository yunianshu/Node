---
name: novel-gen
description: 使用 D:/AiProject/Node 当前 scripts 自动化框架生成和维护长篇中文网络小说项目。适用于创建小说项目、运行 Planner/Outliner/Writer/Reviewer/Rewrite/Coordinator、多 Agent 批量生成大纲、断点续传、企业微信进度推送、章节质量门、补齐缺失章节、多媒体生成和阅读器调试。当前标准为 Planner 只生成 world.json 与 characters.json，大纲由 Outliner 生成到 chapters/outline/chapter_XXXX.json，禁止生成或依赖根目录 outline.json。
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
| Planner Parallel | `scripts/pipeline/planner_parallel.py` | 先跑 Planner，再批量启动 Outliner | 单章大纲文件 |
| Writer | `scripts/pipeline/writer.py` | 根据单章大纲生成初稿 | `chapters/draft/chapter_XXXX.txt` |
| Reviewer | `scripts/pipeline/reviewer.py` | 审查初稿并评分 | `chapters/review/chapter_XXXX_review.json` |
| Rewrite | `scripts/pipeline/rewrite_agent.py` | 根据审查意见重写或复制终稿 | `chapters/final/chapter_XXXX.txt` |
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
Outliner / Planner Parallel
   ↓ chapters/outline/chapter_XXXX.json
Writer
   ↓ chapters/draft/chapter_XXXX.txt
Reviewer
   ↓ chapters/review/chapter_XXXX_review.json
Rewrite
   ↓ chapters/final/chapter_XXXX.txt
Push/Reports/Reader
```

阶段门控：
- Planner 只负责 `world.json` 与 `characters.json`。
- 大纲未完成前，不运行 Writer/Reviewer/Rewrite。
- Writer 必须通过 Coordinator 的多子进程/多 Agent 并发执行，按 `coordinator.num_workers` 和当前批次范围拆分启动；只允许在单章调试、故障定位或用户明确要求时直接串行运行 `writer.py`。
- 继续生成长篇项目时，优先运行 `scripts/pipeline/coordinator.py` 断点续传，不手工逐章调用 Writer；若发现只剩少数卡点章节，可由 Coordinator 的补偿队列处理或明确记录为调试性单章重跑。
- Writer 并发数必须来自项目 `config.json` 的 `coordinator.num_workers`，可按配额/失败率动态降低，但不得硬编码固定单进程。
- 初稿未完成的批次，不运行该批 Reviewer。
- 审查报告未全部 `status=completed` 前，不运行 Rewrite。
- Rewrite 最多尝试 5 次；每次内容和评分写到 `chapters/rewrite/chapter_XXXX/attempt_XX.*`。
- 5 次仍不合格时，选择最高分版本写入终稿，并在 `rewrite_meta.json` 记录选择原因。

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

批量并行生成全书大纲：

```powershell
python "scripts/pipeline/planner_parallel.py" --project "D:/AiProject/Node/projects/<book_id>" --workers 20
```

运行完整协调器：

```powershell
python "scripts/pipeline/coordinator.py" --project "D:/AiProject/Node/projects/<book_id>" --planner-parallel
```

指定章节范围：

```powershell
python "scripts/pipeline/coordinator.py" --project "D:/AiProject/Node/projects/<book_id>" --start 1 --end 200 --batch-size 20
```

只重写候选章节：

```powershell
python "scripts/pipeline/rewrite_agent.py" --project "D:/AiProject/Node/projects/<book_id>"
```

查看需重写章节：

```powershell
python "scripts/pipeline/rewrite_agent.py" --project "D:/AiProject/Node/projects/<book_id>" --candidates
```

发送单项目企业微信进度：

```powershell
python "scripts/maintenance/wechat_notify.py" --project "D:/AiProject/Node/projects/<book_id>"
```

启动阅读器：

```powershell
python "scripts/maintenance/reader_server.py" --novels-dir "D:/AiProject/Node/projects" --port 8889
```

## 配置要点

项目配置文件为 `projects/<book_id>/config.json`。常用字段：

```json
{
  "total_chapters": 2000,
  "model": "MiniMax-M2.7-highspeed",
  "mmx_path": "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs",
  "api_qps": 2.0,
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

常用补齐脚本已改为单章大纲模式：
- `scripts/batch/fill_missing_outline.py`
- `scripts/batch/fill_outline.py`
- `scripts/batch/fill_outline_segments.py`
- `scripts/batch/run_outlines_parallel.py`
- `scripts/batch/run_planner_batches.py`

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
python -m unittest discover -s "scripts/tests"
python -m py_compile "scripts/pipeline/planner.py" "scripts/pipeline/outliner.py" "scripts/pipeline/planner_parallel.py" "scripts/pipeline/coordinator.py" "scripts/pipeline/rewrite_agent.py" "scripts/core/workflow_state.py"
```

涉及推送、阅读器或批处理脚本时，追加对应文件到 `py_compile`。

关键回归点：
- `rg "outline\.json" "scripts"` 只应出现“不生成 outline.json”的测试断言，不能出现业务读写。
- Outliner 无 `--outline-file` 时不得生成 `outline.json` 或 `chapters/outline/index.json`。
- Coordinator 大纲完成数必须来自单章大纲文件。
- Rewrite 尝试记录必须保留每次内容和评分。

## 多媒体

多媒体入口：

```powershell
python "scripts/media/generate_cover_and_trailer.py" --project "D:/AiProject/Node/projects/<book_id>"
```

标准输出目录：

```text
media/images/
media/audio/
media/music/
```

视频、音乐配额通常较少，默认谨慎启用。生成前先确认 `config.json` 中对应开关和 MiniMax 配额。
