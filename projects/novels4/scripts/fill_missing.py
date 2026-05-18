#!/usr/bin/env python3
"""补全缺失的章节文件"""
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
        print(f"[ERROR] {result.stderr}")
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


def generate_chapter(chapter_number):
    """生成单个缺失章节"""
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    if chapter_file.exists() and chapter_file.stat().st_size > 1000:
        print(f"第{chapter_number}章已存在，跳过")
        return True

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

    system = """你是一位顶尖的中文网络小说作家，擅长创作长篇玄幻小说。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
你尤其擅长描写战斗场面、心理活动和世界观展现。
每章必须约5000字，不能少于4500字或超过5500字。
注意保持角色性格一致性，前后情节衔接自然。"""

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
1. 本章约5000字，严格按照大纲核心事件展开
2. 开头要自然衔接前一章，结尾要留悬念或引出下一章
3. 对话要符合角色性格，推动情节发展
4. 场景描写要生动，让读者有画面感
5. 战斗/冲突场面要紧张刺激，有层次感
6. 心理描写要细腻，展现主角内心变化
7. 不要流水账，要有起伏和转折
8. 不要输出章节标题，直接从正文开始
9. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

请开始写作："""

    print(f"[Fill] 正在生成第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)

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
    print(f"[Fill] 第{chapter_number}章已保存（{word_count}字）")
    return True


def main():
    # 找出缺失的章节
    all_nums = set(range(1, 2001))
    existing = set()
    for c in CHAPTERS_DIR.glob("chapter_*.txt"):
        if c.stat().st_size > 1000:
            try:
                existing.add(int(c.stem.split("_")[1]))
            except (ValueError, IndexError):
                pass

    missing = sorted(all_nums - existing)
    print(f"缺失章节: {len(missing)} 个")
    if missing:
        print(f"范围: 第{min(missing)}-{max(missing)}章")
    else:
        print("无缺失")
        return

    # 并行20个Agent生成
    completed = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(generate_chapter, ch): ch for ch in missing}
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

    print(f"[Fill] 补全完成: 成功{completed}章, 失败{len(failed)}章")
    if failed:
        print(f"失败章节: {failed}")


if __name__ == "__main__":
    main()
