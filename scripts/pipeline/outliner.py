#!/usr/bin/env python3
"""
Outliner Agent - 单章大纲生成Agent
负责按章节范围生成大纲，并拆分为 chapters/outline/chapter_XXXX.json。
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import load_config
from core.workflow_state import list_outline_chapters, write_outline_chapters

NOVELS_DIR = None
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
CONFIG = None
NOVEL_PREMISE = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, WORLD_FILE, OUTLINE_FILE, CHARACTERS_FILE, CONFIG, NOVEL_PREMISE
    NOVELS_DIR = Path(project_dir).resolve()
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = None
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    CONFIG = load_config(NOVELS_DIR)
    total = CONFIG["total_chapters"]

    premise_file = NOVELS_DIR / "premise.txt"
    if premise_file.exists():
        NOVEL_PREMISE = premise_file.read_text(encoding="utf-8").replace("{total_chapters}", str(total))
    else:
        NOVEL_PREMISE = f"请围绕既有世界观和角色档案，规划一部长篇小说，全书共{total}章。"


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.5) -> str:
    try:
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="outliner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _strip_json_markdown(content: str) -> str:
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _fix_inner_quotes(text: str) -> str:
    """状态机：识别 JSON 字符串边界，将字符串内未转义的 " 替换为单引号。"""
    out = []
    i = 0
    in_string = False
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
            else:
                out.append(ch)
        else:
            if ch == "\\":
                out.append(ch)
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                next_ch = text[j] if j < n else ""
                if next_ch in (":", ",", "}", "]", ""):
                    in_string = False
                    out.append(ch)
                else:
                    out.append("'")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _safe_parse_outline(text: str) -> dict | None:
    """尝试多种方式解析大纲 JSON。"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 修复内部引号后再解析
    try:
        return json.loads(_fix_inner_quotes(text))
    except Exception:
        pass
    # 3. 截断到最后一个完整的 }
    for end_marker in ('"\n    }\n  ]\n}', '"\n    }\n  ]', '"\n    }', '"\n}'):
        idx = text.rfind(end_marker)
        if idx != -1:
            # 找到包裹的右大括号
            end = text.find("}", idx) + 1
            candidate = text[:end]
            try:
                return json.loads(candidate)
            except Exception:
                pass
            try:
                return json.loads(_fix_inner_quotes(candidate))
            except Exception:
                pass
    return None


def generate_outline_range(start: int, end: int, outline_file: Path = None, fill_gaps: bool = False, chapter: int = None, review_feedback: Path = None):
    batch_size = 15
    output_file = outline_file

    world = _load_json(WORLD_FILE)
    characters = _load_json(CHARACTERS_FILE)
    
    # 精简世界观设定，避免请求过大导致 API 超时
    world_summary = {
        "title": world.get("title", ""),
        "world_name": world.get("world_name", ""),
        "world_description": world.get("world_description", "")[:800],
        "power_system": {
            "name": world.get("power_system", {}).get("name", ""),
            "description": world.get("power_system", {}).get("description", "")[:500],
        },
    }
    world_json = json.dumps(world_summary, ensure_ascii=False, indent=2)
    
    # 精简角色设定，只保留主角和关键角色
    chars_summary = {"_meta": characters.get("_meta", {})}
    protagonist = characters.get("protagonist", {})
    chars_summary["protagonist"] = {
        "name": protagonist.get("name", ""),
        "identity": protagonist.get("identity", ""),
        "growth_path": protagonist.get("growth_path", []),
        "signature_ability": protagonist.get("signature_ability", ""),
    }
    chars_summary["companions"] = [
        {"name": c.get("name"), "identity": c.get("identity"), "role": c.get("role")}
        for c in characters.get("companions", [])[:3]
    ]
    chars_json = json.dumps(chars_summary, ensure_ascii=False, indent=2)

    # 读取审查意见
    review_feedback_data = {}
    if review_feedback and review_feedback.exists():
        try:
            review_feedback_data = json.loads(review_feedback.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Outliner] 审查意见文件读取失败: {e}")

    # 单章模式：强制只生成指定章节
    if chapter is not None:
        start = chapter
        end = chapter

    outline = {"chapters": []}
    last_chapter = 0
    if output_file and output_file.exists():
        try:
            outline = json.loads(output_file.read_text(encoding="utf-8"))
            if outline.get("chapters"):
                last_chapter = max(ch.get("chapter_number", 0) for ch in outline["chapters"])
        except Exception:
            outline = {"chapters": []}
    elif output_file is None:
        outline = {"chapters": list_outline_chapters(NOVELS_DIR)}
        covered = {
            item.get("chapter_number")
            for item in outline["chapters"]
            if start <= item.get("chapter_number", 0) <= end
        }
        if not fill_gaps and not chapter and len(covered) == end - start + 1:
            print(f"[Outliner] 单章大纲范围{start}-{end}已覆盖，跳过")
            return

    if output_file and last_chapter >= end and not fill_gaps and not chapter:
        print(f"[Outliner] 大纲已生成到第{last_chapter}章，范围{start}-{end}已覆盖，跳过")
        write_outline_chapters(NOVELS_DIR, outline)
        return

    # 计算实际需要生成的章节范围
    if fill_gaps or chapter:
        existing_chapters = {item.get("chapter_number") for item in outline.get("chapters", [])}
        missing = [ch for ch in range(start, end + 1) if ch not in existing_chapters]
        if not missing:
            print(f"[Outliner] 范围内无缺失章节，跳过")
            return
        # 将缺失章节分组为连续的批次
        batch_ranges = []
        batch_s = missing[0]
        batch_e = missing[0]
        for ch in missing[1:]:
            if ch == batch_e + 1:
                batch_e = ch
            else:
                batch_ranges.append((batch_s, batch_e))
                batch_s = ch
                batch_e = ch
        batch_ranges.append((batch_s, batch_e))
    else:
        actual_start = max(start, last_chapter + 1) if output_file else start
        batch_ranges = [(s, min(s + batch_size - 1, end)) for s in range(actual_start, end + 1, batch_size)]

    system = """你是一位顶级东方玄幻/武侠/修仙小说大纲设计师。
你需要设计详细的大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
严格按照 premise 中描述的故事设定和主角设定来设计大纲。
输出必须是合法的JSON格式。"""

    for batch_start, batch_end in batch_ranges:
        print(f"[Outliner] 正在生成第 {batch_start}-{batch_end} 章大纲...")

        prev_context = ""
        if outline.get("chapters"):
            # 找到 batch_start 之前最多3章作为上下文
            prev_candidates = [ch for ch in outline["chapters"] if ch.get("chapter_number", 0) < batch_start]
            prev_candidates.sort(key=lambda x: x.get("chapter_number", 0))
            prev_chapters = prev_candidates[-3:]
            if prev_chapters:
                prev_context = "\n前一批最后几章摘要（用于衔接）：\n"
                for ch in prev_chapters:
                    prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

        review_section = ""
        if review_feedback_data:
            # 只提取当前批次章节的审查意见，避免请求过大
            batch_feedback = {
                k: v for k, v in review_feedback_data.items()
                if v.get("chapter") and batch_start <= v.get("chapter", 0) <= batch_end
            }
            if batch_feedback:
                review_section = f"""\n上一轮大纲审查反馈（请特别注意并改进以下问题）：\n{json.dumps(batch_feedback, ensure_ascii=False, indent=2)}\n"""

        prompt = f"""请根据以下世界观和角色设定，生成第{batch_start}章到第{batch_end}章的详细大纲。

世界观设定：
{world_json}

角色设定：
{chars_json}

故事前提：{NOVEL_PREMISE}

{prev_context}
{review_section}
请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "章节标题",
      "summary": "核心事件摘要（150-250字）",
      "characters_involved": ["角色名1", "角色名2"],
      "location": "场景地点",
      "mood": "情感基调",
      "key_events": ["事件1", "事件2"],
      "foreshadowing": "埋下的伏笔",
      "power_progression": "实力变化说明",
      "word_count_target": 5000
    }}
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷，有扮猪吃虎的爽点
3. 主角的实力和技能要逐步成长，保持升级爽感
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 要有强敌轻视主角，结果被主角以积累的实力碾压的爽文桥段
6. 探索不同场景时要展现环境差异和世界多样性
7. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象"""

        content = call_mmx(system, prompt, max_tokens=8192, temperature=0.5)
        if not content:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲生成失败")
            continue

        try:
            stripped = _strip_json_markdown(content)
            # 先做 mojibake 修复
            try:
                from core.json_repair import repair_latin1_gbk_mojibake
                stripped = repair_latin1_gbk_mojibake(stripped)
            except Exception:
                pass
            # 修复被 max_tokens 截断的 JSON
            try:
                from core.json_repair import fix_truncated_json
                stripped = fix_truncated_json(stripped)
            except Exception:
                pass
            batch_outline = _safe_parse_outline(stripped)
            if batch_outline is None:
                raise ValueError("所有 JSON 解析策略均失败")
            new_chapters = batch_outline.get("chapters", [])
            outline["chapters"].extend(new_chapters)
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲已生成（{len(new_chapters)}章）")
            if output_file:
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
            skip_existing = fill_gaps or (chapter is not None)
            write_outline_chapters(NOVELS_DIR, {"chapters": new_chapters}, skip_existing=skip_existing)
        except Exception as e:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章解析失败: {e}")
            raw_file = NOVELS_DIR / "logs" / f"outline_batch_{batch_start:04d}.raw"
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            raw_file.write_text(content, encoding="utf-8")

    print(f"[Outliner] 大纲范围 {start}-{end} 已完成，共 {len(outline['chapters'])} 章")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""), help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--outline-file", type=str, default="", help="指定大纲索引输出文件路径（用于并行生成）")
    parser.add_argument("--fill-gaps", action="store_true", help="只生成缺失的章节，跳过已存在的")
    parser.add_argument("--chapter", type=int, default=0, help="只生成指定单章的大纲")
    parser.add_argument("--review-feedback", type=str, default="", help="大纲审查意见JSON文件路径，用于指导改进")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)
    end = args.end or CONFIG["total_chapters"]

    print("=" * 60)
    if args.chapter:
        print(f"Outliner Agent 启动 - 单章: 第{args.chapter}章")
    elif args.fill_gaps:
        print(f"Outliner Agent 启动 - 填补空缺: 第{args.start}章到第{end}章")
    else:
        print(f"Outliner Agent 启动 - 范围: 第{args.start}章到第{end}章")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    outline_file = Path(args.outline_file) if args.outline_file else None
    generate_outline_range(
        args.start, end, outline_file,
        fill_gaps=args.fill_gaps,
        chapter=args.chapter if args.chapter > 0 else None,
        review_feedback=Path(args.review_feedback) if args.review_feedback else None,
    )

    print("[Outliner] 全部完成")


if __name__ == "__main__":
    main()
