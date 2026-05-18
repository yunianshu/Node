---
name: novel-gen
description: 使用多Agent系统自动生成长篇网络小说（1000-2000章）。包含统一框架（Planner、Writer、Reviewer、RewriteAgent、Coordinator）、多媒体生成（封面/配图/语音/音乐/视频）、配置驱动、断点续传、配额监控。novels5+ 使用 novels/ 公共框架 + config.json 配置驱动。
---

# 小说生成系统 — Agent Skill Guide

使用多Agent系统自动生成长篇中文网络小说。

**项目演进：**
- `novels2` (剑来风格)：独立脚本，2000章完成，评分8.19
- `novels3` (武道通神)：40进程并行，1997/2000章，评分7.77，限流严重
- `novels4`：独立脚本完成
- `novels5` (长生武道)：首个使用 `novels/` 统一框架，2000章完成
- `novels6+`：**推荐**使用统一框架 + 配置驱动

---

## 系统架构 (novels5+)

```
D:/AiProject/Node/
├── novels/                    # 公共框架（所有项目共享）
│   ├── core/                  # 核心模块
│   │   ├── config.py          # 统一配置管理 (NovelConfig)
│   │   ├── llm_client.py      # LLM调用 + 令牌桶限流 + JSON容错解析
│   │   ├── logger.py          # 多模块独立日志
│   │   ├── notifier.py        # 企业微信Webhook推送
│   │   └── quota.py           # MiniMax配额监控
│   ├── agents/                # Agent基类
│   │   └── base.py            # BaseAgent抽象基类
│   ├── multimedia/            # 多媒体生成器
│   │   ├── image_generator.py # 封面 + 章节配图 (mmx image generate)
│   │   ├── video_generator.py # 预告片 + 短视频 (mmx video generate)
│   │   ├── speech_generator.py# 章节语音朗读 (mmx speech synthesize)
│   │   └── music_generator.py # 主题曲 + 背景音乐 (mmx music generate)
│   ├── coordinator.py         # 统一主控
│   └── default_prompts/       # 默认Prompt模板
│       ├── planner.txt
│       ├── writer.txt
│       ├── reviewer.txt
│       └── rewrite.txt
│
└── projects/
    └── novelsX/               # 单个小说项目
        ├── config.json        # 项目级配置（唯一需要修改的文件）
        ├── prompts/           # 项目级Prompt覆盖（可选）
        ├── world.json         # 世界观
        ├── outline.json       # 2000章大纲
        ├── characters.json    # 角色档案
        ├── chapters/          # 章节（兼容旧结构）
        │   ├── draft/         # 初稿
        │   └── final/         # 终稿
        ├── reviews/           # 审查报告
        ├── images/            # 封面 + 配图
        ├── videos/            # 预告片 + 视频
        ├── audio/             # 语音朗读
        ├── music/             # 音乐
        ├── lyrics/            # 歌词
        ├── logs/              # 运行日志
        └── output/            # 框架输出目录
            ├── chapters/{draft,final}/
            ├── reviews/
            ├── progress.json
            └── summary_report.json
```

| Agent | 职责 | 输出 |
|-------|------|------|
| **Planner** | 生成世界观、2000章大纲、角色档案 | `world.json` + `outline.json` + `characters.json` |
| **Writer** | 按大纲生成章节初稿 (~5000字/章) | `chapters/draft/chapter_XXXX.txt` |
| **Reviewer** | 对初稿评分审查，标记需重写 | `reviews/chapter_XXXX_review.json` |
| **RewriteAgent** | 根据评审意见重写低分章节为终稿 | `chapters/final/chapter_XXXX.txt` |
| **Coordinator** | 调度批次、配额监控、断点续传、多媒体异步调度 | 日志 + 进度 + 多媒体 |

---

## 快速开始 (novels5+)

### 1. 创建项目

```bash
mkdir -p projects/novels6/{prompts,images,videos,audio,music,lyrics,logs,output/chapters/{draft,final},output/reviews}
```

### 2. 编写 config.json

```json
{
  "title": "新书名称",
  "project_dir": "D:/AiProject/Node/projects/novels6",
  "total_chapters": 2000,
  "model": "MiniMax-M2.7-highspeed",
  "mmx_path": "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs",
  "api_qps": 2.0,
  "writer": {
    "max_tokens": 8192,
    "temperature": 0.7,
    "context_chapters": 3,
    "max_retries": 3,
    "retry_delay": 5.0
  },
  "reviewer": {
    "max_tokens": 4096,
    "temperature": 0.3,
    "min_score": 7.0,
    "rewrite_threshold": 6.5,
    "dimensions": ["文笔", "剧情", "人设", "连贯性"]
  },
  "planner": {
    "parallel_agents": 60,
    "segments": 60,
    "max_tokens": 8192,
    "temperature": 0.7
  },
  "coordinator": {
    "batch_size": 20,
    "num_workers": 5,
    "wechat_webhook": "",
    "push_interval_seconds": 120,
    "auto_fill_missing": true,
    "auto_rewrite": true,
    "pause_between_batches": 3.0
  },
  "multimedia": {
    "enabled": true,
    "images": {
      "enabled": true,
      "model": "image-01",
      "aspect_ratio": "16:9",
      "generate_cover": true,
      "generate_chapter_images": true
    },
    "videos": {
      "enabled": false,
      "model": "MiniMax-Hailuo-2.3",
      "generate_trailer": true,
      "generate_chapter_videos": false,
      "trailer_chapters": [1, 500, 1000, 1500, 2000],
      "duration": "6s",
      "resolution": "768p"
    },
    "audio": {
      "enabled": false,
      "model": "speech-2.8-hd",
      "voice": "Chinese_Mandarin_Gentle_Youth",
      "generate_chapter_audio": true,
      "speed": 1.0
    },
    "music": {
      "enabled": false,
      "model": "music-2.6",
      "generate_chapter_music": false,
      "generate_theme_music": true,
      "genre": "dark folk",
      "mood": "mysterious, tense",
      "vocals": "male, deep, emotional",
      "instruments": "acoustic guitar, cello, piano",
      "tempo": "slow"
    }
  }
}
```

### 3. 一键启动

```bash
python -m novels.coordinator --config D:/AiProject/Node/projects/novels6/config.json
```

参数：
| 参数 | 说明 |
|------|------|
| `--skip-planner` | 跳过Planner（已有大纲时） |
| `--skip-review` | 跳过Reviewer |
| `--rewrite-only` | 只运行重写队列 |

---

## 核心配置项详解

### api_qps (限流速率)
- `2.0` = 每秒最多2次API调用
- 遇到 2062 限流错误时自动指数退避（5s -> 10s -> 20s）
- 配额不足时自动降速到 0.5

### batch_size / num_workers
- `batch_size=20`: 每批处理20章
- `num_workers=5`: 每批5个并行Worker
- **推荐**: 20/5（novels3 用 40/60 导致大量 2062 限流失败）

### reviewer.rewrite_threshold
- 低于此分数触发重写（默认 6.5）
- novels2 平均评分 8.19，novels3 仅 7.77，建议 novels6+ 提高 Prompt 质量要求

---

## 多媒体生成

多媒体在文本生成后**后台异步**调度，不阻塞主流程。

| 类型 | 目录 | 配额 | 策略 |
|------|------|------|------|
| 封面 | `images/cover.jpg` | image-01: 200/天 | 小说完成后一次性 |
| 章节配图 | `images/chapter_XXXX.jpg` | image-01: 200/天 | 每章1张，2000章需10天 |
| 预告片 | `videos/trailer.mp4` | Hailuo-2.3: 3/天 | 只在关键章节 |
| 语音朗读 | `audio/chapter_XXXX.mp3` | speech-hd: 19000/天 | 配额充裕，可全量 |
| 主题曲 | `music/theme.mp3` | music-2.6: 100/天 | 先歌词后音乐 |
| 背景音乐 | `music/chapter_XXXX.mp3` | music-2.6: 100/天 | 100章/天 |

**注意**: 视频和音乐配额极少，默认关闭，建议只开启 `generate_trailer` 和 `generate_theme_music`。

### 单独生成多媒体（novels5已完成的文本）

```bash
# 生成封面和预告片
cd D:/AiProject/Node/projects/novels6
python scripts/generate_cover_and_trailer.py
```

或直接用框架模块：
```python
from novels.core.config import NovelConfig
from novels.multimedia.image_generator import ImageGenerator
from novels.multimedia.music_generator import MusicGenerator

config = NovelConfig.load('novels5/config.json')

# 生成封面
img = ImageGenerator(config)
img.generate_cover({"title": "长生武道"})

# 批量生成章节配图
for i in range(1, 2001):
    img.generate(i)

# 生成主题曲
music = MusicGenerator(config)
music.generate_theme({"title": "长生武道"})
```

---

## 项目演进对比

| 维度 | novels2 | novels3 | novels5+ |
|------|---------|---------|----------|
| 架构 | 独立脚本 | 40进程并行 | 统一框架+配置驱动 |
| 完成度 | 2000/2000 | 1997/2000 | 2000/2000 |
| 平均评分 | 8.19 | 7.77 | - |
| writer失败 | 253次(重试耗尽) | 3次 | 接近0(令牌桶限流) |
| JSON解析失败 | 43次 | 71次 | 0(自动清洗) |
| 限流处理 | 等待5分钟 | 等待5分钟 | 指数退避+动态降速 |
| 补全 | fill_missing | fill_draft+fill_reviews | 内置auto_fill_missing |
| 多媒体 | 音乐(62/2000) | 无 | 框架内置异步调度 |
| 代码复用 | 无 | 复制粘贴 | novels/公共框架 |

---

## 旧项目启动方式 (novels2/3/4)

```bash
cd D:/AiProject/Node/novels2/scripts
python coordinator.py --batch-size 40 --start 1 --end 2000
```

```bash
cd D:/AiProject/Node/novels4/scripts
python coordinator.py --batch-size 40 --start 1 --end 2000
```

---

## 阅读器

```bash
python D:/AiProject/Node/novels/scripts/reader_server.py
# 打开 http://localhost:8888
```

支持：书库首页、章节导航、搜索、主题切换、字体调节、键盘翻页、进度保存。

---

## 常见问题

### API配额不足
```bash
mmx quota show --output json
```
Coordinator 自动等待并重试，无需手动处理。

### 缺失章节
`auto_fill_missing=true` 自动检测并补全，补全时使用更低并发避免再次触发限流。

### JSON解析失败
框架内置 `_sanitize_json()` 自动清洗控制字符、修复尾部逗号、处理反斜杠转义。

### 断点续传
Coordinator 自动读取 `progress.json`，中断后重新运行即可从断点继续。

---

## 模型配置

默认使用 `MiniMax-M2.7-highspeed`。框架中统一配置在 `config.json` 的 `model` 字段。

---

## 前置要求

```bash
# MiniMax API
mmx auth login --api-key sk-xxxxx

# mmx-cli
npm install -g mmx-cli

# Python 3.x（仅标准库）
```
