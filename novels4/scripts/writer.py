#!/usr/bin/env python3
"""
Writer Agent - 内容生成Agent
负责根据大纲生成具体章节内容，保存为txt文件
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"
LOG_FILE = NOVELS_DIR / "logs" / "writer.log"

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


def log(msg: str):
    """记录日志"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    """调用 mmx text chat 生成内容（通过 node 直接运行 mmx-cli）"""
    cmd = [
        "node", MMX_CLI_PATH, "text", "chat",
        "--model", "MiniMax-M2.7-highspeed",
        "--system", system_prompt,
        "--message", user_prompt,
        "--max-tokens", str(max_tokens),
        "--temperature", str(temperature),
        "--stream=false",
        "--quiet"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else "unknown error"
            log(f"[ERROR] mmx call failed (rc={result.returncode}): {err}")
            return ""
        raw = result.stdout.strip()
        try:
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            pass
        if "Response:" in raw:
            json_part = raw.split("Response:")[-1].strip()
            try:
                data = json.loads(json_part)
                return data.get("content", raw)
            except json.JSONDecodeError:
                return json_part
        return raw
    except Exception as e:
        log(f"[ERROR] mmx subprocess exception: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    """加载JSON文件"""
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_chapter(chapter_number: int, retry: int = 0) -> str:
    """生成单个章节"""
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    if chapter_file.exists() and chapter_file.stat().st_size > 1000:
        log(f"[Writer] 第{chapter_number}章已存在，跳过")
        return "exists"

    # 加载数据
    world = load_json(WORLD_FILE)
    outline = load_json(OUTLINE_FILE)
    characters = load_json(CHARACTERS_FILE)

    # 找到当前章节大纲
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    if not chapter_outline:
        log(f"[Writer] 第{chapter_number}章大纲不存在")
        return "no_outline"

    # 找到前一章和后一章的摘要（用于衔接）
    prev_summary = ""
    next_summary = ""
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number - 1:
            prev_summary = ch.get("summary", "")
        if ch.get("chapter_number") == chapter_number + 1:
            next_summary = ch.get("summary", "")

    # 读取上一章末尾（用于衔接）
    prev_ending = ""
    if chapter_number > 1:
        prev_file = CHAPTERS_DIR / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    # 检查是否存在审查报告（用于重写时参考）
    review_file = NOVELS_DIR / "reviews" / f"chapter_{chapter_number:04d}_review.json"
    review_data = None
    is_rewrite = False
    if review_file.exists():
        try:
            review_data = load_json(review_file)
            verdict = review_data.get("verdict", "")
            score = review_data.get("overall_score", 10)
            if verdict == "需重写" or score < 7:
                is_rewrite = True
                log(f"[Writer] 第{chapter_number}章检测到低分审查报告（评分{score}，verdict:{verdict}），将基于建议重写")
        except Exception:
            pass

    # 构建审查建议部分（如果存在）
    review_section = ""
    if is_rewrite and review_data:
        suggestions = review_data.get("suggestions", [])
        continuity_issues = review_data.get("continuity_issues", [])
        strengths = review_data.get("strengths", [])
        weaknesses = review_data.get("weaknesses", [])

        review_section = "\n\n## 编辑审查反馈（请严格参考以下建议重写）\n"

        if strengths:
            review_section += "\n### 原文优点（请保留）\n"
            for s in strengths:
                review_section += f"- {s}\n"

        if weaknesses:
            review_section += "\n### 原文不足（请改进）\n"
            for w in weaknesses:
                review_section += f"- {w}\n"

        if suggestions:
            review_section += "\n### 具体修改建议（必须落实）\n"
            for s in suggestions:
                review_section += f"- {s}\n"

        if continuity_issues and continuity_issues[0] != "与前文不一致之处（如有）":
            review_section += "\n### 连续性问题（必须修正）\n"
            for c in continuity_issues:
                if c and c != "与前文不一致之处（如有）":
                    review_section += f"- {c}\n"

        old_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
        if old_file.exists():
            with open(old_file, "r", encoding="utf-8") as f:
                old_content = f.read()
            review_section += f"\n### 原文参考（前800字）\n{old_content[:800]}\n...\n"

    # 系统提示词
    if is_rewrite:
        system = """你是一位顶尖的中文网络小说作家，同时也是一位资深编辑。
你现在需要对一篇已完成的章节进行**重写**，而不是从零创作。
重写原则：
1. 保留原文的优点和核心情节框架
2. 严格落实验编给出的具体修改建议
3. 修正连续性问题
4. 弥补原文的不足之处
5. 每章约5000字，不能少于4500字或超过5500字
6. 保持角色性格一致性，前后情节衔接自然
7. 文笔要比原文更加流畅、细腻、有张力
8. 主角是外表文弱的书生，但实力深不可测，要保持"扮猪吃虎"的爽感"""
    else:
        system = """你是一位顶尖的中文网络小说作家，擅长创作东方玄幻/武侠爽文，尤其精通"扮猪吃虎"和"儒道流"题材。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑，每章必须有强烈的爽感和阅读钩子。

【9分神作标准——你必须达到这个水平】
1. 对话不是"他说""我说"的流水账，而是有潜台词、有交锋、有韵味的语言艺术
2. 场景描写不是"天很蓝、风很大"的废话，而是用五感渲染氛围，让读者身临其境
3. 战斗场面不是"他一拳打出，敌人倒了"的简单描述，而是有层次、有张力、有画面感的电影镜头
4. 心理描写不是"他很生气"的直接陈述，而是通过细节、动作、环境折射内心
5. 爽点不是简单打脸，而是铺垫充分、反转精妙、余韵悠长的情感释放

核心写作要求（必须严格执行）：
1. 每章必须包含至少2个"爽点"——可以是反转、打脸、震惊、扮猪吃虎被揭穿等，爽点之间要有递进
2. 主角外表必须是温润儒雅的书生形象，说话带书卷气（善用典故、诗词、对仗），但出手时武道霸气碾压全场
3. 对话必须推动情节，不能废话。配角对话要体现性格，主角对话要体现"腹有诗书气自华"
4. 战斗/冲突场面必须分层递进：试探→压制→反转→全场震惊→主角云淡风轻离开
5. 配角反应链必须详细：从不屑→惊讶→震惊→恐惧→敬畏，让读者产生代入爽感
6. 每章结尾必须留钩子：悬念、新敌人出现、身份即将暴露、更大的危机酝酿
7. 场景描写要有画面感，环境细节服务于氛围营造
8. 心理描写要细腻，展现主角"表面平静、内心洞察一切"的智者形象
9. 每章约5000字，不能少于4500字或超过5500字
10. 保持角色性格一致性，前后情节衔接自然

【高级写作技巧（必须运用）】
- 对话技巧：主角说话要少而精，每句话都要有信息量或韵味。善用沉默、停顿、反问。配角说话要体现阶层和性格差异。
- 场景技巧：用"特写镜头"写法——先聚焦一个细节（一片落叶、一滴血、一个眼神），再拉远到全景，制造画面层次感。
- 战斗技巧：不要直接写"他很厉害"，要写"旁观者看到的不可思议"。通过他人反应来衬托主角强大。
- 节奏技巧：紧张与舒缓交替。一场大战后，要有一段安静的回味或对话，让读者喘息，再进入下一波高潮。
- 书卷气技巧：主角说话引用经典但要自然，不是掉书袋。可以用"古人云""诗经云"等自然引入，然后话锋一转，露出锋芒。

文风要求：书卷气与武道霸气并存，既有"谈笑间樯橹灰飞烟灭"的儒雅，又有"一拳破万法"的霸气。"""

    prompt = f"""请根据以下信息，写出第{chapter_number}章《{chapter_outline.get('title', '未命名')}》的完整内容。

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## 本章大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 写作要求
1. 本章约5000字，严格按照大纲核心事件展开
2. 开头要自然衔接前一章，结尾要留悬念或引出下一章
3. 对话要符合角色性格，推动情节发展
4. 场景描写要生动，让读者有画面感
5. 战斗/冲突场面要紧张刺激，有层次感，体现"书生武道"的独特气质
6. 心理描写要细腻，展现主角内心变化
7. 要体现"外表文弱、实力通神"的反差感，有扮猪吃虎的爽点
8. 主角说话要有书卷气，引经据典，但出手毫不留情
9. 不要流水账，要有起伏和转折
10. 不要输出章节标题，直接从正文开始
11. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

请开始写作："""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)

    if not content:
        if retry < 3:
            log(f"[Writer] 第{chapter_number}章生成失败，重试({retry+1}/3)...")
            time.sleep(5)
            return generate_chapter(chapter_number, retry + 1)
        log(f"[Writer] 第{chapter_number}章生成失败，已达最大重试次数")
        return "failed"

    # 清理内容
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 保存
    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(chapter_file, "w", encoding="utf-8") as f:
        f.write(content)

    word_count = len(content)
    log(f"[Writer] 第{chapter_number}章已保存（{word_count}字） -> {chapter_file}")
    return "success"


def main():
    print("=" * 60)
    print("Writer Agent 启动")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只生成某一章")
    args = parser.parse_args()

    if args.chapter > 0:
        generate_chapter(args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = generate_chapter(ch)
            if result == "failed":
                log(f"[Writer] 第{ch}章生成失败，暂停")
                break
            time.sleep(0)

    log("[Writer] 全部完成")


if __name__ == "__main__":
    main()
