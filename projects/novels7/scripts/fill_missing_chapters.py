#!/usr/bin/env python3
"""
补全缺失的前25章大纲
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


def extract_json(content: str) -> dict:
    """从 LLM 输出中提取 JSON"""
    import re
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()

    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)
    content = re.sub(r"[\u200b-\u200f\ufeff]", "", content)
    content = re.sub(r",(\s*[}\]])", r"\1", content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except (json.JSONDecodeError, ValueError):
        pass

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


def _summarize_world(world_data: dict) -> str:
    """通用世界观摘要 - 适配任意JSON结构"""
    import json
    # 直接序列化整个JSON，限制长度
    raw = json.dumps(world_data, ensure_ascii=False, indent=2)
    if len(raw) > 4000:
        raw = raw[:4000] + "\n...（截断）"
    return raw


def _summarize_characters(chars_data: dict) -> str:
    """通用角色摘要 - 适配任意JSON结构"""
    import json
    raw = json.dumps(chars_data, ensure_ascii=False, indent=2)
    if len(raw) > 3000:
        raw = raw[:3000] + "\n...（截断）"
    return raw


def main():
    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)
    logger = get_logger("FillMissing", config.logs_dir)

    client = LLMClient(
        mmx_path=config.mmx_path,
        model=config.model,
        qps=config.api_qps,
        log_dir=str(config.logs_dir)
    )

    # 读取世界观和角色
    with open(config.world_file, "r", encoding="utf-8") as f:
        world_data = json.load(f)
    with open(config.characters_file, "r", encoding="utf-8") as f:
        chars_data = json.load(f)

    # 读取现有大纲
    with open(config.outline_file, "r", encoding="utf-8") as f:
        outline = json.load(f)

    world_summary = _summarize_world(world_data)
    chars_summary = _summarize_characters(chars_data)

    planner_prompt = ""
    prompt_file = config.prompts_dir / "planner.txt"
    if prompt_file.exists():
        with open(prompt_file, "r", encoding="utf-8") as f:
            planner_prompt = f.read()

    system = planner_prompt + "\n\n请生成章节大纲。输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"

    # 读取第26章作为后续衔接参考
    next_chapters = outline.get("chapters", [])
    next_context = ""
    if next_chapters:
        first_few = next_chapters[:3]
        next_context = "\n后续章节摘要（用于衔接）：\n"
        for ch in first_few:
            next_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

    logger.info("正在补全第1-25章大纲...")
    user = f"""请根据以下设定，生成第1章到第25章的详细大纲（凡尘篇，主角凡间经历）。

世界观设定：
{world_summary}

角色设定：
{chars_summary}

{next_context}

请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": 1,
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
1. 第1-25章为凡尘篇，讲述主角在凡间的悲惨经历，确立逆仙之志
2. 主角出身极度卑微，遭受欺凌但隐忍不发
3. 铺垫重要人物出场（导师、女主、首个反派）
4. 情节要有起伏，黑暗压抑中偶尔有温情
5. 必须输出合法JSON，总共25个章节对象"""

    content = client.call(system_prompt=system, user_prompt=user, max_tokens=16384, temperature=0.5)
    if not content:
        logger.error("第1-25章大纲生成失败")
        return 1

    try:
        batch_outline = extract_json(content)
        new_chapters = batch_outline.get("chapters", [])
        if len(new_chapters) != 25:
            logger.warning(f"只生成了 {len(new_chapters)} 章，期望25章")

        # 合并到outline.json开头
        existing = outline.get("chapters", [])
        outline["chapters"] = new_chapters + existing

        with open(config.outline_file, "w", encoding="utf-8") as f:
            json.dump(outline, f, ensure_ascii=False, indent=2)

        logger.info(f"第1-25章大纲已补全，共 {len(new_chapters)} 章，outline.json 总计 {len(outline['chapters'])} 章")
        return 0
    except Exception as e:
        logger.error(f"解析失败: {e}")
        raw_file = config.outline_file.parent / "outline_batch_0001_fix.raw"
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(content)
        return 1


if __name__ == "__main__":
    sys.exit(main())
