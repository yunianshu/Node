#!/usr/bin/env python3
"""
Planner Agent - 配置驱动版
根据项目 config.json 和 prompts/planner.txt 生成世界观、角色、大纲
"""
import json
import sys
from pathlib import Path

# 将 novels 框架加入路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


def load_prompt(config: NovelConfig, name: str) -> str:
    """加载项目级 prompt，若不存在则使用默认"""
    project_prompt = config.prompts_dir / f"{name}.txt"
    if project_prompt.exists():
        with open(project_prompt, "r", encoding="utf-8") as f:
            return f.read()
    default_prompt = config.path.parent.parent / "novels" / "default_prompts" / f"{name}.txt"
    if default_prompt.exists():
        with open(default_prompt, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_json(content: str) -> dict:
    """从 LLM 输出中提取 JSON，自动清洗控制字符，支持多对象合并"""
    import re
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()

    # 清洗控制字符（保留 \n \r \t）
    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)
    # 去除零宽字符
    content = re.sub(r"[\u200b-\u200f\ufeff]", "", content)
    # 修复对象/数组尾部逗号
    content = re.sub(r",(\s*[}\]])", r"\1", content)

    # 尝试解析为单个JSON对象
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试提取最外层 {}
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except (json.JSONDecodeError, ValueError):
        pass

    # 处理多个独立JSON对象的情况：尝试找到所有顶层对象并合并
    decoder = json.JSONDecoder()
    idx = 0
    merged = {}
    found = False
    while idx < len(content):
        try:
            obj, end = decoder.raw_decode(content, idx)
            if isinstance(obj, dict):
                merged.update(obj)
                found = True
            idx = end
        except json.JSONDecodeError:
            idx += 1

    if found:
        return merged

    raise json.JSONDecodeError("无法解析JSON", content, 0)


def generate_world(client: LLMClient, prompt: str, output_file: Path, logger) -> bool:
    if output_file.exists():
        logger.info(f"{output_file.name} 已存在，跳过")
        return True

    logger.info("正在生成世界观...")
    system = prompt + "\n\n请先输出 world.json 的内容。输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"
    user = f"请为小说《凡尘逆仙》设计完整的世界观设定。这是一部类似《仙逆》风格的修真小说，主角从凡尘起步，逆天改命，追求大道。全书共2000章。"

    content = client.call(system_prompt=system, user_prompt=user, max_tokens=16384, temperature=0.5)
    if not content:
        logger.error("世界观生成失败")
        return False

    try:
        data = extract_json(content)
        save_json(output_file, data)
        logger.info(f"世界观已保存到 {output_file}")
        return True
    except Exception as e:
        logger.error(f"世界观解析失败: {e}")
        raw_file = output_file.with_suffix(".raw")
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(content)
        return False


def generate_characters(client: LLMClient, prompt: str, world_data: dict, output_file: Path, logger) -> bool:
    if output_file.exists():
        logger.info(f"{output_file.name} 已存在，跳过")
        return True

    logger.info("正在生成角色档案...")
    system = prompt + "\n\n请先输出 characters.json 的内容。输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"
    world_json = json.dumps(world_data, ensure_ascii=False, indent=2)
    user = f"请根据以下世界观，为小说《凡尘逆仙》设计完整的角色档案。\n\n世界观设定：\n{world_json}\n\n要求：\n1. 主角出身卑微，性格坚毅沉默、杀伐果断\n2. 设计至少2位重要女性角色（情感线要虐）\n3. 设计至少3个层级的反派（有智商、有动机）\n4. 设计至少5位重要配角（导师、挚友、对手等）\n5. 角色要有成长弧线和深度"

    content = client.call(system_prompt=system, user_prompt=user, max_tokens=16384, temperature=0.5)
    if not content:
        logger.error("角色档案生成失败")
        return False

    try:
        data = extract_json(content)
        save_json(output_file, data)
        logger.info(f"角色档案已保存到 {output_file}")
        return True
    except Exception as e:
        logger.error(f"角色档案解析失败: {e}")
        raw_file = output_file.with_suffix(".raw")
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(content)
        return False


def _summarize_world(world_data: dict) -> str:
    """提取世界观精简摘要，避免命令行过长"""
    lines = []
    lines.append(f"小说名称：{world_data.get('title', '凡尘逆仙')}")
    lines.append(f"世界描述：{world_data.get('world_description', world_data.get('description', ''))[:300]}")

    # 修炼体系
    power = world_data.get('power_system', {})
    if power:
        lines.append(f"\n修炼体系：{power.get('name', '')}")
        levels = power.get('levels', [])
        if levels:
            level_names = [l.get('name', '') for l in levels[:15]]
            lines.append(f"境界：{' -> '.join(level_names)}")

    # 主要势力
    factions = world_data.get('factions', [])
    if isinstance(factions, dict):
        lines.append(f"\n主要势力：")
        for key, val in list(factions.items())[:4]:
            if isinstance(val, list):
                for f in val[:3]:
                    lines.append(f"  - {f.get('name', '')}（{key}）：{f.get('description', '')[:80]}")
            elif isinstance(val, dict):
                lines.append(f"  - {val.get('name', '')}（{key}）：{val.get('description', '')[:80]}")
    elif isinstance(factions, list) and factions:
        lines.append(f"\n主要势力：")
        for f in factions[:8]:
            lines.append(f"  - {f.get('name', '')}：{f.get('description', '')[:80]}")

    # 关键地点
    locations = world_data.get('key_locations', [])
    if not locations:
        locations = world_data.get('geography', {}).get('major_regions', [])
    if locations:
        lines.append(f"\n关键地点：")
        for loc in locations[:8]:
            lines.append(f"  - {loc.get('name', '')}：{loc.get('description', loc.get('significance', ''))[:80]}")

    # 核心主题
    themes = world_data.get('themes', [])
    if themes:
        lines.append(f"\n核心主题：{', '.join(themes[:5])}")

    result = "\n".join(lines)
    # 如果精简结果为空（字段不匹配），直接返回JSON截断版
    if not result.strip() or result.strip() == '小说名称：凡尘逆仙':
        raw = json.dumps(world_data, ensure_ascii=False, indent=2)
        return raw[:4000] + "\n...（截断）" if len(raw) > 4000 else raw
    return result


def _summarize_characters(chars_data: dict) -> str:
    """提取角色精简摘要 - 通用适配"""
    raw = json.dumps(chars_data, ensure_ascii=False, indent=2)
    if len(raw) > 3000:
        return raw[:3000] + "\n...（截断）"
    return raw


def generate_outline(client: LLMClient, prompt: str, world_data: dict, chars_data: dict,
                     output_file: Path, logger, batch_size: int = 50,
                     range_start: int = 1, range_end: int = 2000) -> bool:
    outline = {"chapters": []}
    last_chapter = range_start - 1

    # 优先从 outline_chapters/ 目录检查单章文件
    outline_dir = output_file.parent / "outline_chapters"
    outline_dir.mkdir(parents=True, exist_ok=True)

    existing_nums = []
    if outline_dir.exists():
        for f in outline_dir.glob("chapter_*.json"):
            try:
                num = int(f.stem.split("_")[1])
                if range_start <= num <= range_end:
                    existing_nums.append(num)
            except (ValueError, IndexError):
                pass

    if existing_nums:
        last_chapter = max(existing_nums)
        logger.info(f"检测到已有单章大纲，共 {len(existing_nums)} 章，从第 {last_chapter + 1} 章继续")

    if last_chapter >= range_end:
        logger.info("大纲已完整，跳过")
        return True

    world_summary = _summarize_world(world_data)
    chars_summary = _summarize_characters(chars_data)
    system = prompt + "\n\n请分批生成章节大纲。每批输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"

    for start in range(last_chapter + 1, range_end + 1, batch_size):
        end = min(start + batch_size - 1, range_end)
        logger.info(f"正在生成第 {start}-{end} 章大纲...")

        prev_context = ""
        if outline.get("chapters"):
            prev_chapters = outline["chapters"][-3:]
            prev_context = "\n前一批最后几章摘要（用于衔接）：\n"
            for ch in prev_chapters:
                prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

        user = f"""请根据以下设定，生成第{start}章到第{end}章的详细大纲。

世界观设定：
{world_summary}

角色设定：
{chars_summary}

{prev_context}

请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {start},
      "title": "章节标题",
      "summary": "核心事件摘要（150-250字）",
      "characters_involved": ["角色名1", "角色名2"],
      "location": "场景地点",
      "mood": "情感基调（紧张/温馨/悲壮/激昂/压抑/苍凉等）",
      "key_events": ["事件1", "事件2"],
      "foreshadowing": "埋下的伏笔",
      "power_progression": "实力变化说明",
      "word_count_target": 5000
    }},
    ...
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷，黑暗压抑中偶尔有温情
3. 主角实力逐步提升，但每次都付出代价
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 必须输出合法JSON，总共{end - start + 1}个章节对象"""

        content = client.call(system_prompt=system, user_prompt=user, max_tokens=8192, temperature=0.5)
        if not content:
            logger.error(f"第 {start}-{end} 章大纲生成失败")
            continue

        try:
            batch_outline = extract_json(content)
            new_chapters = batch_outline.get("chapters", [])
            # 每章保存为单独文件
            saved_count = 0
            for ch in new_chapters:
                ch_num = ch.get("chapter_number", 0)
                if range_start <= ch_num <= range_end:
                    ch_file = outline_dir / f"chapter_{ch_num:04d}.json"
                    save_json(ch_file, ch)
                    saved_count += 1
            logger.info(f"第 {start}-{end} 章大纲已生成（{len(new_chapters)}章，保存{saved_count}章）")
        except Exception as e:
            logger.error(f"第 {start}-{end} 章解析失败: {e}")
            raw_file = output_file.parent / f"outline_batch_{start:04d}.raw"
            with open(raw_file, "w", encoding="utf-8") as f:
                f.write(content)

    # 统计当前范围内实际生成的章节数
    actual_in_range = 0
    if outline_dir.exists():
        actual_in_range = sum(1 for f in outline_dir.glob("chapter_*.json")
                              if range_start <= int(f.stem.split("_")[1]) <= range_end)
    logger.info(f"大纲已保存到 {outline_dir}，范围内共 {actual_in_range} 章")
    expected_in_range = range_end - range_start + 1
    return actual_in_range >= expected_in_range


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="起始章节（默认1）")
    parser.add_argument("--end", type=int, default=2000, help="结束章节（默认2000）")
    parser.add_argument("--batch-size", type=int, default=50, help="每批章节数（默认50）")
    parser.add_argument("--output", type=str, default="", help="输出文件路径（默认使用config.outline_file）")
    parser.add_argument("--qps", type=float, default=None, help="覆盖API QPS限制（默认使用config.api_qps）")
    args = parser.parse_args()

    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)
    logger = get_logger("Planner", config.logs_dir)

    logger.info("=" * 60)
    logger.info(f"Planner Agent 启动 | 范围: {args.start}-{args.end} | 批次: {args.batch_size}")
    logger.info("=" * 60)

    qps = args.qps if args.qps is not None else config.api_qps
    client = LLMClient(
        mmx_path=config.mmx_path,
        model=config.model,
        qps=qps,
        log_dir=str(config.logs_dir)
    )
    planner_prompt = load_prompt(config, "planner")

    # 确保输出目录存在
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # 生成世界观（只在处理第1段时生成）
    if args.start == 1:
        if not generate_world(client, planner_prompt, config.world_file, logger):
            logger.error("世界观生成失败，Planner 终止")
            return 1

    # 读取世界观
    world_data = {}
    if config.world_file.exists():
        with open(config.world_file, "r", encoding="utf-8") as f:
            world_data = json.load(f)

    # 生成角色（只在处理第1段时生成）
    if args.start == 1:
        if not generate_characters(client, planner_prompt, world_data, config.characters_file, logger):
            logger.error("角色档案生成失败，Planner 终止")
            return 1

    # 读取角色
    chars_data = {}
    if config.characters_file.exists():
        with open(config.characters_file, "r", encoding="utf-8") as f:
            chars_data = json.load(f)

    # 确定输出文件
    if args.output:
        output_file = Path(args.output)
    else:
        output_file = config.outline_file

    # 生成大纲
    if not generate_outline(client, planner_prompt, world_data, chars_data, output_file, logger,
                            batch_size=args.batch_size, range_start=args.start, range_end=args.end):
        logger.error("大纲生成未完成")
        return 1

    logger.info("Planner 全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
