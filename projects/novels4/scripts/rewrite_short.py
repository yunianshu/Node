#!/usr/bin/env python3
"""重新生成字数不足的章节"""
import concurrent.futures
import json
import subprocess
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


def call_mmx(system_prompt, user_prompt, max_tokens=8192, temperature=0.7):
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
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
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


def rewrite_chapter(chapter_number):
    """重写单个章节"""
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    # 备份旧版本
    if chapter_file.exists():
        backup = chapter_file.with_suffix(".txt.bak2")
        try:
            chapter_file.rename(backup)
        except Exception:
            pass

    # 加载数据
    world = json.load(open(NOVELS_DIR / "world.json", encoding="utf-8"))
    outline = json.load(open(NOVELS_DIR / "outline.json", encoding="utf-8"))
    characters = json.load(open(NOVELS_DIR / "characters.json", encoding="utf-8"))

    # 找到当前章节大纲
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    if not chapter_outline:
        print(f"[ERROR] 第{chapter_number}章大纲不存在")
        return False

    # 找到前一章和后一章的摘要
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

    # 针对特殊章节调整策略
    is_short_outline = chapter_number == 1936
    is_long_prone = chapter_number == 283

    if is_short_outline:
        system = """你是一位顶尖的中文网络小说作家，擅长创作长篇玄幻小说。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
注意保持角色性格一致性，前后情节衔接自然。

【字数强制执行规则 - 最高优先级】
- 本章字数必须达到4500字以上，这是绝对硬性要求，不可违反！
- 你之前多次未能达到字数要求，这次请务必写够4500字。
- 本章大纲有4个关键事件，每个事件至少写1200字以上。
- 事件之间要增加过渡段落，描写角色感受和周围环境变化，每个过渡至少300字。
- 增加角色之间的互动对话，每个对话都要完整展开，不要简单带过。
- 环境描写要充分，描述周围景色、氛围、光线、声音、气味等细节。
- 心理描写要深入详尽，展现角色的思考过程、犹豫、决心、情绪波动，每次心理活动至少200字。
- 战斗或探索场面要分阶段详细描写：起手、交锋、变化、高潮、结果。
- 如果字数不够，增加一段回忆、侧面描写或环境渲染来补足。
- 最终输出前请自检字数，字数不达标必须补充内容，直到满足4500字为止。"""
    elif is_long_prone:
        system = """你是一位顶尖的中文网络小说作家，擅长创作长篇玄幻小说。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
注意保持角色性格一致性，前后情节衔接自然。

【字数强制执行规则 - 最高优先级】
- 本章字数必须在4500-5500字之间，绝对不能超过5500字。
- 你之前总是写得过长，这次请严格控制篇幅。
- 每个情节点描写要精炼，不要过度扩展。
- 对话要简洁有力，不要冗长。
- 环境描写适度，不要大段渲染。
- 心理描写要点到为止，不要长篇内心独白。
- 聚焦核心情节，删减次要细节。
- 最终输出前请自检字数，超过5500字必须精简。"""
    else:
        system = """你是一位顶尖的中文网络小说作家，擅长创作长篇玄幻小说。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
注意保持角色性格一致性，前后情节衔接自然。

【字数强制执行规则 - 最高优先级】
- 本章字数必须在4500-5500字之间，这是绝对硬性要求，不可违反。
- 如果大纲内容较少，必须扩展环境描写、角色心理活动、对话细节、动作描写来充实篇幅。
- 增加角色之间的互动对话，每个对话都要完整展开，不要简单带过。
- 增加场景转换时的过渡描写，描述周围环境、氛围、天气变化等。
- 战斗场面要分阶段详细描写：起手、交锋、变化、高潮、结果，每个阶段都要有动作细节和角色反应。
- 心理描写要深入，展现角色的思考过程、犹豫、决心、情绪波动。
- 如果字数接近下限，请增加一段回忆、侧面描写或环境渲染来补足。
- 最终输出前请自检字数，字数不达标必须补充内容。"""

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
{next_summary}

## 写作要求
1. 本章字数必须在4500-5500字之间，这是最重要的硬性要求
2. 严格按照大纲核心事件展开，不要遗漏任何情节点
3. 开头要自然衔接前一章，结尾要留悬念或引出下一章
4. 对话要符合角色性格，推动情节发展，对话要详细具体
5. 场景描写要生动细致，让读者有画面感，不要一笔带过
6. 战斗/冲突场面要紧张刺激，有层次感，详细描写动作和过程
7. 心理描写要细腻，展现主角内心变化，增加内心独白
8. 不要流水账，要有起伏和转折
9. 不要输出章节标题，直接从正文开始
10. 不要输出任何元信息，只输出正文
11. 本章大纲有4个关键事件，每个事件至少写1200字。事件之间增加过渡段落（每个至少300字）。充分展开对话、环境描写和心理描写。最终字数必须达到4500字以上。

请开始写作："""

    print(f"[Rewrite] 正在生成第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=12000, temperature=0.8)

    if not content:
        print(f"[ERROR] 第{chapter_number}章生成失败")
        return False

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
    print(f"[Rewrite] 第{chapter_number}章已保存（{word_count}字）")
    return True


def main():
    short_chapters = [1936]
    print(f"需要重写的章节: {len(short_chapters)} 个")
    print(short_chapters)

    completed = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(rewrite_chapter, ch): ch for ch in short_chapters}
        for future in concurrent.futures.as_completed(futures):
            ch = futures[future]
            try:
                success = future.result()
                if success:
                    completed += 1
                else:
                    failed.append(ch)
            except Exception as exc:
                print(f"[ERROR] 第{ch}章异常: {exc}")
                failed.append(ch)

    print(f"[Rewrite] 完成: 成功{completed}章, 失败{len(failed)}章")
    if failed:
        print(f"失败: {failed}")


if __name__ == "__main__":
    main()
