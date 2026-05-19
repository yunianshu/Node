---
name: mmx-cli
description: Use mmx (MiniMax CLI) to generate media content for novel projects — covers, trailers, chapter images, TTS audio, and background music. Also supports general text chat, vision, and web search via MiniMax AI platform.
---

# MiniMax CLI — 小说项目专用 Skill

本 Skill 用于为小说项目生成多媒体内容（封面、预告片、章节配图、语音朗读、背景音乐），同时保留通用 MiniMax CLI 能力。

## 前置条件

```bash
# 安装
npm install -g mmx-cli

# 认证（API key 持久化到 ~/.mmx/config.json）
mmx auth login --api-key sk-xxxxx

# 验证
mmx auth status
```

Region 自动检测，可用 `--region global` 或 `--region cn` 覆盖。

---

## Agent / 非交互式标志

| Flag | 作用 |
|---|---|
| `--non-interactive` | 缺少参数时直接失败，不交互提示 |
| `--quiet` | 静默模式，stdout 为纯数据 |
| `--output json` | 输出 JSON 格式 |
| `--async` | 异步任务（视频生成），立即返回 taskId |
| `--dry-run` | 仅预览请求，不执行 |
| `--yes` | 跳过确认提示 |

---

## 小说项目目录约定

每本小说项目位于 `projects/novelsX/`，多媒体文件存放规则：

```
projects/novelsX/
  media/
    images/          ← 封面 cover.jpg、章节配图 chapter_XXXX.jpg
    videos/          ← 预告片 trailer.mp4
    audio/           ← 章节 TTS 语音 chapter_XXXX.mp3
    music/           ← 背景音乐 chapter_XXXX.mp3
```

封面统一命名为 `media/images/cover.jpg`，阅读器会自动识别。

---

## 小说项目专用命令

### 生成封面

为单本小说生成封面，提示词自动从 `world.json` 的 `title` 字段构建：

```bash
cd projects/novels1

# 基础封面生成
mmx image generate \
  --prompt "东方玄幻仙侠小说封面，书名《长生武道：虚空万界行》，画面中央是一位白衣长发的武道强者，背后有虚空万界的景象，星空、裂缝、不同世界的碎片漂浮在虚空中，整体色调以深蓝和金色为主，大气磅礴，史诗感" \
  --out-dir media/images --out-prefix cover --quiet

# 重命名为标准文件名
mv media/images/cover_001.jpg media/images/cover.jpg
```

**批量生成所有小说封面**（Bash）：

```bash
for d in projects/novels*; do
  [ -d "$d" ] || continue
  title=$(node -e "console.log(JSON.parse(require('fs').readFileSync('$d/world.json')).title || '')" 2>/dev/null)
  [ -z "$title" ] && continue
  mkdir -p "$d/media/images"
  echo "生成封面: $title"
  mmx image generate \
    --prompt "东方玄幻仙侠小说封面，书名《$title》，画面精美，气势恢宏，适合网络小说封面展示" \
    --out-dir "$d/media/images" --out-prefix cover --quiet
  mv "$d/media/images/cover_001.jpg" "$d/media/images/cover.jpg" 2>/dev/null
done
```

**批量生成（Python）**：

```python
import json, subprocess, glob
from pathlib import Path

for world_file in sorted(glob.glob("projects/novels*/world.json")):
    project = Path(world_file).parent
    title = json.load(open(world_file, "r", encoding="utf-8")).get("title", "")
    out_dir = project / "media" / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt = f"东方玄幻仙侠小说封面，书名《{title}》，画面精美，气势恢宏"
    subprocess.run([
        "mmx", "image", "generate",
        "--prompt", prompt,
        "--out-dir", str(out_dir), "--out-prefix", "cover", "--quiet"
    ], check=True)

    # 重命名为标准文件名
    for f in out_dir.glob("cover_*.jpg"):
        f.rename(out_dir / "cover.jpg")
```

---

### 生成预告片视频

```bash
cd projects/novels1

mmx video generate \
  --prompt "制作一段东方玄幻小说的预告片视频。画面展现主角从平凡少年成长为万界最强者的历程，包含修炼、战斗、探索异世界等场景，节奏紧凑，充满史诗感和热血感，时长约30秒" \
  --download media/videos/trailer.mp4 --quiet
```

视频生成是异步任务，默认会轮询直到完成。如需非阻塞：

```bash
# 获取 taskId
TASK=$(mmx video generate --prompt "..." --async --quiet | jq -r '.taskId')
# 查询状态
mmx video task get --task-id "$TASK" --output json
# 下载
mmx video download --task-id "$TASK" --out trailer.mp4
```

---

### 生成章节配图

```bash
cd projects/novels1

# 为第 1 章生成配图
mmx image generate \
  --prompt "东方玄幻仙侠场景，一座悬浮在云端的古老仙山，仙鹤环绕，灵气缭绕，水墨画风与CG结合" \
  --out-dir media/images --out-prefix chapter_0001 --quiet
```

---

### 生成章节 TTS 语音

```bash
cd projects/novels1

# 为章节文本生成语音朗读
mmx speech synthesize \
  --text-file chapters/draft/chapter_0001.txt \
  --out media/audio/chapter_0001.mp3 \
  --quiet
```

---

### 生成背景音乐

```bash
cd projects/novels1

# 生成章节背景音乐（纯音乐）
mmx music generate \
  --prompt "古风仙侠战斗场景，紧张激烈的管弦乐，配合中国传统乐器如古筝、笛子、鼓" \
  --instrumental \
  --out media/music/chapter_0001.mp3 \
  --quiet
```

---

## 通用命令参考

### text chat

Chat completion，默认模型 `MiniMax-M2.7`。

```bash
mmx text chat --message "user:Hello" --output json --quiet

# 多轮对话
mmx text chat \
  --system "You are a coding assistant." \
  --message "user:Write fizzbuzz in Python" \
  --output json
```

| Flag | 类型 | 说明 |
|---|---|---|
| `--message <text>` | string, **required**, 可重复 | 消息文本，前缀 `role:` 设置角色 |
| `--messages-file <path>` | string | JSON 消息数组文件，`-&#8203;` 表示 stdin |
| `--system <text>` | string | System prompt |
| `--model <model>` | string | 模型 ID |
| `--max-tokens <n>` | number | 最大 token 数（默认 4096） |
| `--temperature <n>` | number | 温度（0.0, 1.0] |
| `--stream` | boolean | 流式输出 |

---

### image generate

图像生成，模型 `image-01`。

```bash
mmx image generate --prompt "A cat in a spacesuit" --output json --quiet

# 批量下载
mmx image generate --prompt "Logo" --n 3 --out-dir ./gen/ --quiet
```

| Flag | 类型 | 说明 |
|---|---|---|
| `--prompt <text>` | string, **required** | 图像描述 |
| `--aspect-ratio <ratio>` | string | `16:9`, `1:1` 等 |
| `--n <count>` | number | 生成数量（默认 1） |
| `--width` / `--height` | number | 尺寸 512–2048，8 的倍数 |
| `--out-dir <dir>` | string | 下载目录 |
| `--out-prefix <prefix>` | string | 文件名前缀（默认 `image`） |
| `--response-format` | string | `url`（默认）或 `base64` |

---

### video generate

视频生成，默认模型 `MiniMax-Hailuo-2.3`，异步任务。

```bash
# 阻塞等待完成
mmx video generate --prompt "Ocean waves." --download ocean.mp4 --quiet

# 非阻塞，获取 taskId
mmx video generate --prompt "A robot." --async --quiet
```

| Flag | 类型 | 说明 |
|---|---|---|
| `--prompt <text>` | string, **required** | 视频描述 |
| `--first-frame <path>` | string | 首帧图片 |
| `--download <path>` | string | 保存路径 |
| `--async` / `--no-wait` | boolean | 立即返回 taskId |
| `--poll-interval <seconds>` | number | 轮询间隔（默认 5） |

**查询任务**：

```bash
mmx video task get --task-id <id> --output json
mmx video download --task-id <id> --out <path>
```

---

### speech synthesize

文本转语音，默认模型 `speech-2.8-hd`，最多 10k 字符。

```bash
mmx speech synthesize --text "Hello world" --out hello.mp3 --quiet

# 从文件读取
mmx speech synthesize --text-file chapter.txt --out chapter.mp3 --quiet

# 带字幕
mmx speech synthesize --text "Hello" --subtitles --out hello.mp3
```

| Flag | 类型 | 说明 |
|---|---|---|
| `--text <text>` | string | 要合成的文本 |
| `--text-file <path>` | string | 从文件读取文本 |
| `--voice <id>` | string | 音色 ID |
| `--speed` / `--volume` / `--pitch` | number | 速度/音量/音调 |
| `--format <fmt>` | string | 音频格式（默认 mp3） |
| `--out <path>` | string | 输出文件路径 |
| `--subtitles` | boolean | 同时生成 `.srt` 字幕文件 |

---

### music generate

音乐生成，**模型 `music-2.6-free`**，API key 用户无限制，RPM = 3。

```bash
# 纯音乐
mmx music generate --prompt "Cinematic orchestral, building tension" --instrumental --out bgm.mp3 --quiet

# 带歌词
mmx music generate --prompt "Upbeat pop" --lyrics "La la la..." --out song.mp3 --quiet

# 自动填词
mmx music generate --prompt "Upbeat pop about summer" --lyrics-optimizer --out summer.mp3 --quiet
```

| Flag | 类型 | 说明 |
|---|---|---|
| `--prompt <text>` | string | 音乐风格描述 |
| `--lyrics <text>` | string | 歌词（含结构标签） |
| `--lyrics-optimizer` | boolean | 自动从 prompt 生成歌词 |
| `--instrumental` | boolean | 纯音乐 |
| `--vocals <text>` | string | 人声风格 |
| `--genre` / `--mood` / `--instruments` | string | 流派/情绪/乐器 |
| `--bpm <number>` | number | BPM |
| `--out <path>` | string | 输出文件 |

---

### music cover

翻唱，**模型 `music-cover-free`**，无限制，RPM = 3。

```bash
mmx music cover \
  --prompt "Indie folk, acoustic guitar, warm male vocal" \
  --audio-file original.mp3 \
  --out cover.mp3 --quiet
```

---

### vision describe

图像理解（VLM）。

```bash
mmx vision describe --image photo.jpg --prompt "What breed?" --output json
```

---

### search query

网页搜索。

```bash
mmx search query --q "MiniMax AI" --output json --quiet
```

---

### quota show

查看配额用量。

```bash
mmx quota show --output json
```

---

## 管道与链式用法

```bash
# 生成封面 URL → 用 VLM 描述
URL=$(mmx image generate --prompt "A sunset" --quiet)
mmx vision describe --image "$URL" --quiet

# 异步视频工作流
TASK=$(mmx video generate --prompt "A robot" --async --quiet | jq -r '.taskId')
mmx video task get --task-id "$TASK" --output json
mmx video download --task-id "$TASK" --out robot.mp4
```

---

## 配置优先级

CLI flags → 环境变量 → `~/.mmx/config.json` → 默认值。

```bash
# 持久化配置
mmx config set --key region --value cn
mmx config show

# 环境变量
export MINIMAX_API_KEY=sk-xxxxx
export MINIMAX_REGION=cn

# 设置默认模型
mmx config set --key default-text-model --value MiniMax-M2.7-highspeed
mmx config set --key default-speech-model --value speech-2.8-hd
mmx config set --key default-video-model --value MiniMax-Hailuo-2.3
mmx config set --key default-music-model --value music-2.6
```

---

## 退出码

| Code | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 通用错误 |
| 2 | 用法错误 |
| 3 | 认证错误 |
| 4 | 配额超限 |
| 5 | 超时 |
| 10 | 内容过滤触发 |
