#!/usr/bin/env python3
"""
Planner Agent - 框架总领Agent
负责生成和维护小说世界观、角色档案
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import subprocess
import sys
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import load_config, load_origin_materials, resolve_project_dir
from core.outline_quality_gate import clean_char_name

NOVELS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
CONFIG = None
NOVEL_PREMISE = ""
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, WORLD_FILE, CHARACTERS_FILE, CONFIG, NOVEL_PREMISE, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    CONFIG = load_config(NOVELS_DIR)
    ORIGIN_MATERIALS = load_origin_materials(NOVELS_DIR)
    total = CONFIG["total_chapters"]

    premise_file = NOVELS_DIR / "premise.txt"
    if premise_file.exists():
        NOVEL_PREMISE = premise_file.read_text(encoding="utf-8").replace("{total_chapters}", str(total))
    else:
        NOVEL_PREMISE = (
            f"请根据项目配置、origin/参考素材和用户后续补充，规划一部长篇中文网络小说，"
            f"全书共{total}章。题材、主角、世界规则、叙事风格必须从项目资料中推导；"
            "资料不足时生成通用但可扩展的原创设定，不得套用任何固定旧项目。"
        )


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    try:
        cfg = CONFIG.get("planner", {})
        fallback = CONFIG.get("writer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=cfg.get("max_tokens", max_tokens),
            temperature=cfg.get("temperature", temperature),
            retries=cfg.get("max_retries", cfg.get("retries", fallback.get("max_retries", 3))),
            retry_delay=cfg.get("retry_delay", fallback.get("retry_delay", 5.0)),
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="planner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def _parse_json_response(content: str) -> dict | None:
    text = str(content or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    variants = [text]
    try:
        from core.json_repair import fix_inner_quotes, fix_truncated_json
        variants.extend([
            fix_inner_quotes(text),
            fix_truncated_json(text),
            fix_truncated_json(fix_inner_quotes(text)),
        ])
    except Exception:
        pass
    for variant in variants:
        try:
            value = json.loads(variant)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validate_world_data(world: dict) -> list[str]:
    issues = []
    for field in ("title", "world_description", "overall_arc"):
        if not str(world.get(field, "")).strip():
            issues.append(f"{field} 缺失")
    if len(str(world.get("world_description", "")).strip()) < 120:
        issues.append("world_description 过短，至少120字")
    if len(str(world.get("overall_arc", "")).strip()) < 120:
        issues.append("overall_arc 过短，至少120字")
    three_act = world.get("three_act_structure")
    if not isinstance(three_act, dict):
        issues.append("three_act_structure 缺失或不是对象")
    else:
        for act in ("act1", "act2", "act3"):
            if not str(three_act.get(act, "")).strip():
                issues.append(f"three_act_structure.{act} 缺失")
    if not isinstance(world.get("power_system"), dict):
        issues.append("power_system 缺失或不是对象")
    if not isinstance(world.get("key_locations"), list) or not world["key_locations"]:
        issues.append("key_locations 缺失或为空")
    if not isinstance(world.get("factions"), list) or not world["factions"]:
        issues.append("factions 缺失或为空")
    if not isinstance(world.get("rules"), list) or len(world["rules"]) < 3:
        issues.append("rules 至少需要3条")
    if not isinstance(world.get("themes"), list) or len(world["themes"]) < 2:
        issues.append("themes 至少需要2条")
    world_building = world.get("world_building")
    if not isinstance(world_building, dict):
        issues.append("world_building 缺失或不是对象")
    else:
        for field in ("economy", "politics", "geography", "history", "culture"):
            if not str(world_building.get(field, "")).strip():
                issues.append(f"world_building.{field} 缺失")
    return issues


def _validate_characters_data(characters: dict) -> list[str]:
    issues: list[str] = []
    protagonist = characters.get("protagonist")
    if not isinstance(protagonist, dict):
        issues.append("protagonist 缺失或不是对象")
    names: list[str] = []

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            if "name" in value:
                raw = str(value.get("name", "")).strip()
                cleaned = clean_char_name(raw)
                if not raw:
                    issues.append(f"{path}.name 为空")
                elif raw != cleaned:
                    issues.append(
                        f"{path}.name 不是干净规范名：{raw}；"
                        "括号说明或“个人信息”后缀必须移到 aliases/description"
                    )
                else:
                    names.append(cleaned)
                aliases = value.get("aliases")
                if aliases is None:
                    issues.append(f"{path}.aliases 缺失")
                elif not isinstance(aliases, list):
                    issues.append(f"{path}.aliases 必须是数组")
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(characters, "characters")
    if len(names) < 4:
        issues.append("显式登记角色少于4个")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        issues.append("规范名重复：" + "、".join(duplicates))
    if isinstance(protagonist, dict) and not protagonist.get("character_arc"):
        issues.append("protagonist.character_arc 缺失")
    return list(dict.fromkeys(issues))


def _generate_json_with_contract(
    system: str,
    prompt: str,
    *,
    label: str,
    validator,
    max_tokens: int = 8192,
) -> dict | None:
    semantic_retries = max(
        0,
        int(CONFIG.get("planner", {}).get("semantic_retries", 1) or 0),
    )
    retry_hint = ""
    last_content = ""
    for attempt in range(semantic_retries + 1):
        last_content = call_mmx(system, prompt + retry_hint, max_tokens=max_tokens, temperature=0.3)
        data = _parse_json_response(last_content)
        issues = validator(data) if isinstance(data, dict) else ["响应不是完整JSON对象"]
        if isinstance(data, dict) and not issues:
            return data
        if attempt < semantic_retries:
            print(
                f"[Planner] {label}契约不完整，原任务重试 "
                f"{attempt + 1}/{semantic_retries}: {'; '.join(issues[:8])}"
            )
            retry_hint = (
                "\n\n## 上次输出无效，本次必须纠正\n"
                + "；".join(issues[:8])
                + "\n请从头返回完整合法JSON，不得省略必填字段。"
            )
    raw_file = NOVELS_DIR / "logs" / f"planner_{label}.raw"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text(last_content, encoding="utf-8")
    print(f"[Planner] {label}生成失败，原始响应已保存到 {raw_file}")
    return None


def generate_world() -> bool:
    if WORLD_FILE.exists():
        try:
            world_data = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[Planner] world.json 无法读取: {exc}")
            return False
        issues = _validate_world_data(world_data) if isinstance(world_data, dict) else ["根节点不是对象"]
        if issues:
            print(f"[Planner] world.json 契约不合格: {'; '.join(issues[:8])}")
            return False
        print("[Planner] world.json 已存在且契约合格，跳过生成")
        return True

    system = """你是一位顶级长篇小说世界观架构师。
你需要根据用户提供的故事 premise、项目配置和原始素材，构建一个完整、详细、有深度且题材匹配的世界观。
输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"""

    prompt = f"""请根据以下故事设定，构建完整的世界观JSON：

{NOVEL_PREMISE}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

请输出以下JSON结构：
{{
  "title": "小说标题",
  "subtitle": "副标题",
  "world_name": "世界名称",
  "world_description": "世界整体描述（500字）",
  "power_system": {{
    "name": "修炼体系名称",
    "description": "修炼体系描述",
    "levels": [
      {{"name": "等级1", "description": "描述"}},
      ...
    ]
  }},
  "factions": [
    {{"name": "势力名称", "description": "描述", "alignment": "正/邪/中"}}
  ],
  "key_locations": [
    {{"name": "地点名称", "description": "描述", "significance": "重要性"}}
  ],
  "rules": ["世界规则1", "世界规则2"],
  "themes": ["主题1", "主题2", "主题3"],
  "world_building": {{
    "economy": "经济系统：货币体系、贸易路线、核心资源的产出与消耗机制",
    "politics": "政治体系：主要阵营的权力结构、政体类型、势力博弈关系",
    "religion": "宗教信仰：神系/信仰体系（如有）、信仰机制、神凡关系",
    "races": "种族关系：主要种族及其天赋差异、种族矛盾或联盟、混血规则（如有）",
    "tech_tree": "力量/科技树：主干分支、等级划分、解锁条件与代价",
    "geography": "地理设定：大陆/区域划分、标志性地理特征、地图层次",
    "history": "历史纪元：编年史框架、转折性大事件、纪元命名规则",
    "culture": "文化风俗：社会习俗、语言特色、核心价值观"
  }},
  "overall_arc": "整体故事弧线描述（300字）",
  "three_act_structure": {{
    "act1": "第一幕描述",
    "act2": "第二幕描述",
    "act3": "第三幕描述"
  }}
}}

要求：
1. 修炼体系必须严格遵循 premise 中描述的体系，不要擅自添加或修改境界名称
2. 地点要有层次感和探索价值，从底层到高层逐步展开
3. 势力设计要符合主角的底层起步设定
4. 整体架构要支撑{CONFIG['total_chapters']}章的篇幅
5. 如果 origin/ 中存在素材，必须优先吸收其中的设定、人物、风格和限制，不能与其冲突
6. 必须输出合法的JSON，不要任何注释或额外文本"""

    print("[Planner] 正在生成世界观...")
    world_data = _generate_json_with_contract(
        system,
        prompt,
        label="world",
        validator=_validate_world_data,
        max_tokens=8192,
    )
    if world_data is None:
        return False
    WORLD_FILE.write_text(json.dumps(world_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Planner] 世界观已保存到 {WORLD_FILE}")
    return True


def generate_characters() -> bool:
    if CHARACTERS_FILE.exists():
        try:
            chars_data = json.loads(CHARACTERS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[Planner] characters.json 无法读取: {exc}")
            return False
        issues = _validate_characters_data(chars_data) if isinstance(chars_data, dict) else ["根节点不是对象"]
        if issues:
            print(f"[Planner] characters.json 契约不合格: {'; '.join(issues[:8])}")
            return False
        print("[Planner] characters.json 已存在且契约合格，跳过生成")
        return True

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise 设计主要角色。
输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

请严格输出以下JSON结构，各角色对象都必须保留 name、aliases 等公共字段：
{{
  "protagonist": {{
    "name": "唯一规范名",
    "aliases": ["简称或尊称"],
    "identity": "身份",
    "description": "人物简介",
    "motivation": "核心动机",
    "character_arc": "起点状态→触发事件→成长方向",
    "language_fingerprint": {{
      "speaking_style": "说话风格",
      "signature_words": ["高频用词1", "高频用词2"],
      "tone": "语气基调"
    }}
  }},
  "companions": [
    {{
      "name": "唯一规范名",
      "aliases": [],
      "identity": "身份",
      "description": "人物简介",
      "motivation": "核心动机",
      "arc": "起点状态→触发事件→成长方向",
      "relationship_with_protagonist": "羁绊类型",
      "language_fingerprint": {{
        "speaking_style": "说话风格",
        "signature_words": ["高频用词1", "高频用词2"],
        "tone": "语气基调"
      }}
    }}
  ],
  "new_characters": [],
  "antagonists": [
    {{
      "name": "唯一规范名",
      "aliases": [],
      "tier": 1,
      "arc": "反派弧线",
      "motivation": "核心动机",
      "charm_point": "魅力点或共情点",
      "language_fingerprint": {{
        "speaking_style": "说话风格",
        "signature_words": ["高频用词1", "高频用词2"],
        "tone": "语气基调"
      }}
    }}
  ],
  "language_fingerprint": {{
    "prose_style": "全书文风基调",
    "signature_metaphors": ["标志性意象1", "标志性意象2"],
    "forbidden_expressions": ["禁用AI味表达1", "禁用AI味表达2"]
  }}
}}

要求：
1. 主角设计要符合 premise 中的描述，有完整的成长路径设计
2. 同伴角色要有血有肉，与主角有真实的情感羁绊
3. 新角色至少设计8个重要角色，涵盖同伴、导师、对手等类型
4. 可以有红颜知己或暧昧角色，但不要太滥
5. 反派要有层次，设计至少3个层级的反派（小反派、中BOSS、最终BOSS），每个反派标注 tier（1/2/3）、arc（弧线方向）、motivation（核心动机）、charm_point（魅力点/共情点）
6. 为每个重要配角（至少3个）设计 arc 字段：起点状态→触发事件→成长/转变方向，标注与主角的羁绊类型（师徒/战友/对手/暧昧等）
7. 如果 origin/ 中存在角色、前作、背景或风格素材，必须优先参考并保持一致
8. 每个角色的 name 必须是唯一、干净的规范名；括号说明、身份说明和“个人信息”等后缀必须放入 aliases 或 description
9. 每个角色必须提供 aliases 数组，没有别名时使用空数组；正文可能使用的简称、尊称、曾用名都在此登记
10. 必须输出合法JSON
11. 为每个主要角色设计"语言指纹"：包含 speaking_style（说话风格：话多/话少/句式特征）、signature_words（口头禅/高频用词2-3个）、tone（语气基调：冷峻/热忱/阴鸷/洒脱等）
12. 为整部小说设计 language_fingerprint：包含 prose_style（文风基调：如白描/华丽/简洁有力）、signature_metaphors（标志性比喻意象2-3个）、forbidden_expressions（应避免的AI味表达）"""

    print("[Planner] 正在生成角色档案...")
    chars_data = _generate_json_with_contract(
        system,
        prompt,
        label="characters",
        validator=_validate_characters_data,
        max_tokens=8192,
    )
    if chars_data is None:
        return False
    CHARACTERS_FILE.write_text(json.dumps(chars_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Planner] 角色档案已保存到 {CHARACTERS_FILE}")
    return True


def _strip_json_markdown(content: str) -> str:
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _default_media_prompts(world: dict, characters: dict) -> dict:
    title = world.get("title", NOVELS_DIR.name)
    world_desc = world.get("world_description", "")
    protagonist = characters.get("protagonist", {})
    protagonist_name = protagonist.get("name", "主角") if isinstance(protagonist, dict) else "主角"
    visual_core = f"《{title}》，{world_desc[:300]}，主角{protagonist_name}"
    return {
        "cover_prompt": (
            f"中文网络小说封面，书名《{title}》，{visual_core}。"
            "电影级构图，强烈故事感，视觉风格必须贴合本书题材和时代背景，主角居中，背景展现核心世界观，"
            "高细节，商业出版封面，避免现代广告字样和水印。"
        ),
        "video_prompt": (
            f"根据小说《{title}》世界观制作15秒电影感概念预告片：{visual_core}。"
            "镜头从核心场景推进到主角背影，再展现关键规则、人物关系与主要冲突，"
            "动态光影，题材氛围鲜明，无字幕，无水印。"
        ),
        "song_prompt": (
            f"为中文网络小说《{title}》创作主题曲，贴合世界观：{world_desc[:300]}。"
            "情绪从困境起步到关键抉择和阶段性爆发，适合小说宣传视频和阅读氛围。"
        ),
        "song_lyrics": (
            f"[Verse]\n长夜里踏过风霜，{protagonist_name}回望旧山河\n"
            "一念未熄燃成火，照见万界的辽阔\n\n"
            "[Chorus]\n向天穹，向长风，向命数之外再相逢\n"
            "以此身破云海，写下一卷不朽的梦\n"
        ),
    }


def _write_media_prompt_files(media_prompts: dict) -> None:
    files = {
        NOVELS_DIR / "media" / "images" / "cover_prompt.md": media_prompts.get("cover_prompt", ""),
        NOVELS_DIR / "media" / "videos" / "world_video_prompt.md": media_prompts.get("video_prompt", ""),
        NOVELS_DIR / "media" / "music" / "theme_song_prompt.md": media_prompts.get("song_prompt", ""),
        NOVELS_DIR / "media" / "music" / "theme_song_lyrics.md": media_prompts.get("song_lyrics", ""),
    }
    for path, text in files.items():
        if not text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text).strip() + "\n", encoding="utf-8")


def generate_media_prompts():
    if not WORLD_FILE.exists() or not CHARACTERS_FILE.exists():
        print("[Planner] world.json 或 characters.json 不存在，跳过媒体提示词生成")
        return

    world = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
    characters = json.loads(CHARACTERS_FILE.read_text(encoding="utf-8"))
    existing = world.get("media_prompts")
    if isinstance(existing, dict) and all(existing.get(key) for key in ("cover_prompt", "video_prompt", "song_prompt", "song_lyrics")):
        _write_media_prompt_files(existing)
        print("[Planner] 媒体提示词已存在，已同步提示词文件")
        return

    system = """你是一位小说视觉与音乐宣发总监。
你需要根据 world.json 和 characters.json，为本书生成封面图、世界观视频、主题曲的高质量生成提示词。
输出必须是合法JSON，不要包含markdown代码块。"""

    prompt = f"""请根据以下小说设定生成媒体提示词，必须符合本书世界观、主角气质和商业网络小说宣发风格。

## world.json
{json.dumps(world, ensure_ascii=False, indent=2)[:4000]}

## characters.json
{json.dumps(characters, ensure_ascii=False, indent=2)[:3000]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

请输出以下JSON结构：
{{
  "cover_prompt": "用于生成一张小说封面图片的详细中文提示词，必须包含画面主体、构图、氛围、色彩、核心世界观元素、禁用水印和现代广告字样",
  "video_prompt": "用于生成一个15秒左右世界观概念视频的详细中文提示词，必须包含镜头运动、场景变化、主角意象、核心冲突、视觉风格、禁用字幕和水印",
  "song_prompt": "用于生成一首本书主题歌的风格提示词，必须包含曲风、情绪、乐器、节奏、用途和世界观氛围",
  "song_lyrics": "中文歌词，带 [Verse] [Chorus] 等结构标签，避免直接照搬已有歌词"
}}

要求：
1. 三类提示词都必须服务于同一本书，不能泛泛而谈
2. cover 必须适合放入 media/images/ 作为小说封面
3. video 必须根据 world.json 的世界观生成，适合放入 media/videos/
4. song 必须是本书主题歌，适合放入 media/music/
5. 如果 origin/ 有素材，必须参考其风格和设定
6. 必须输出合法JSON"""

    print("[Planner] 正在生成媒体提示词...")
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.5)
    media_prompts = None
    if content:
        try:
            media_prompts = json.loads(_strip_json_markdown(content))
        except Exception as exc:
            print(f"[Planner] 媒体提示词JSON解析失败: {exc}")
    if not isinstance(media_prompts, dict):
        media_prompts = _default_media_prompts(world, characters)

    world["media_prompts"] = {
        "cover_prompt": str(media_prompts.get("cover_prompt", "")).strip(),
        "video_prompt": str(media_prompts.get("video_prompt", "")).strip(),
        "song_prompt": str(media_prompts.get("song_prompt", "")).strip(),
        "song_lyrics": str(media_prompts.get("song_lyrics", "")).strip(),
    }
    WORLD_FILE.write_text(json.dumps(world, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_media_prompt_files(world["media_prompts"])
    print("[Planner] 媒体提示词已写入 world.json 和 media/ 提示词文件")


def generate_media_assets() -> bool:
    """复用 media_generator 生成封面/视频/主题歌。

    config.media.enabled 默认 True；为 False 时跳过。返回是否成功（或已跳过）。
    单独 import 以避免循环依赖：media_generator 不依赖 planner。
    """
    if not CONFIG or not CONFIG.get("media", {}).get("enabled", True):
        print("[Planner] media.enabled=false，跳过媒体资产生成")
        return True
    try:
        from pipeline import media_generator as mg
    except Exception as exc:
        print(f"[Planner] 无法加载 media_generator: {exc}")
        return False
    # 复用 planner 已初始化的项目上下文
    mg.init_project(NOVELS_DIR)
    try:
        return mg.generate_media()
    except Exception as exc:
        print(f"[Planner] 媒体资产生成异常: {exc}")
        return False


def generate_volume_outline():
    """兼容旧参数；卷纲统一由 Outliner 生成和校验。"""
    outliner = Path(__file__).with_name("outliner.py")
    command = [
        sys.executable,
        str(outliner),
        "--project",
        str(NOVELS_DIR),
        "--volume-only",
    ]
    print("[Planner] 分卷规划委托给 Outliner...")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"[Planner] Outliner 分卷规划失败: rc={result.returncode}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default="",
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="兼容参数，Planner不再生成大纲")
    parser.add_argument("--end", type=int, default=0, help="兼容参数，Planner不再生成大纲")
    parser.add_argument("--world-only", action="store_true", help="兼容参数，Planner默认只生成世界观和角色")
    parser.add_argument("--outline-file", type=str, default="", help="兼容参数；Planner 不生成大纲")
    parser.add_argument("--with-media-prompts", action="store_true", help="显式补齐 world.json.media_prompts")
    parser.add_argument("--with-volume-outline", action="store_true", help="显式生成 volume_outline.json（兼容旧流程）")
    parser.add_argument("--with-media-assets", action="store_true", help="显式调用 media_generator 生成媒体资产")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Planner Agent 启动 - 仅生成世界观和角色档案")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)

    world_ok = generate_world()
    characters_ok = generate_characters()
    if not (world_ok and characters_ok):
        print("[Planner] 基础资料契约校验失败，阻断后续流程")
        sys.exit(1)

    if args.with_media_prompts:
        generate_media_prompts()
    if args.with_volume_outline:
        if not generate_volume_outline():
            sys.exit(1)
    if args.with_media_assets:
        if not generate_media_assets():
            print("[Planner] 媒体资产生成未完成（详见 logs/media_generator.log）")

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
