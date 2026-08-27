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
from core.json_repair import strip_json_markdown as _strip_json_markdown
from core.novel_config import load_config, load_origin_materials, resolve_project_dir
from core.outline_quality_gate import clean_char_name

NOVELS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
CONFIG = None
NOVEL_PREMISE = ""
ORIGIN_MATERIALS = ""

LIFE_PROFILE_KEYS = (
    "family_ties",
    "livelihood_pressure",
    "old_debts",
    "soft_spot",
    "daily_habits",
    "relationship_taboo",
)

QUALITY_BIBLE_KEYS = (
    "conflict_engine",
    "content_density_rules",
    "scene_variety",
    "sensory_palette",
    "emotional_promises",
    "taboo_cliches",
)


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
    except Exception as exc:
        pass
    for variant in variants:
        try:
            value = json.loads(variant)
        except Exception as exc:
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
    quality_bible = world.get("quality_bible")
    if not isinstance(quality_bible, dict):
        issues.append("quality_bible 缺失或不是对象")
    else:
        for field in QUALITY_BIBLE_KEYS:
            value = quality_bible.get(field)
            if isinstance(value, list):
                if not any(str(item).strip() for item in value):
                    issues.append(f"quality_bible.{field} 缺失或为空")
            elif not str(value or "").strip():
                issues.append(f"quality_bible.{field} 缺失")
        palette = quality_bible.get("sensory_palette")
        if isinstance(palette, list):
            issues.append("quality_bible.sensory_palette 仍是旧版列表，需迁移为分类对象")
        elif not isinstance(palette, dict):
            issues.append("quality_bible.sensory_palette 缺失或不是对象")
        else:
            for cat in SENSORY_PALETTE_CATEGORIES:
                cat_value = palette.get(cat)
                if not isinstance(cat_value, list) or not any(str(v).strip() for v in cat_value):
                    issues.append(f"quality_bible.sensory_palette.{cat} 缺失或为空")
    return issues


def _default_quality_bible(world: dict) -> dict:
    title = str(world.get("title") or world.get("world_name") or "本书").strip()
    themes = "、".join(str(item) for item in world.get("themes", [])[:3]) if isinstance(world.get("themes"), list) else ""
    factions = "、".join(
        str(item.get("name", ""))
        for item in world.get("factions", [])[:4]
        if isinstance(item, dict) and item.get("name")
    )
    return {
        "conflict_engine": (
            f"《{title}》每章冲突必须来自人物欲望、现实压力、势力博弈与主线秘密的交叉，"
            f"不能只靠随机敌人或设定解释推进。核心势力压力：{factions or '按已登记势力递进'}。"
        ),
        "content_density_rules": [
            "每章至少有一个新信息、一个关系变化、一个具体代价或一个旧伏笔回收。",
            "设定信息必须通过行动、交易、调查、对抗或人物取舍呈现。",
            "连续两章不得使用同一种核心动作链作为主要阅读回报。",
        ],
        "scene_variety": [
            "场景轮换要体现阶层、职业、地理或制度差异，避免所有冲突都发生在同类空间。",
            "每个重要场景必须有一个可触摸的物件、气味、声音或身体细节服务人物处境。",
        ],
        "sensory_palette": {
            "indoor": ["工位", "厨房灶台", "楼道灯", "账单", "药柜", "旧沙发", "饭桌", "煤炉"],
            "outdoor": ["街市叫卖", "雨棚滴水", "巷口阴影", "野风", "尘土", "市集摊位"],
            "body": ["指节伤口", "汗渍", "旧疤", "磨白袖口", "冻红的手", "干裂嘴唇", "黑眼圈"],
            "object": ["裂屏手机", "铜钥匙", "旧饭盒", "褪色照片", "磨钝的笔", "缺角瓷碗"],
            "sound_smell": ["药味", "饭香", "钟声", "楼道灯嗡嗡", "铁锈味", "潮气", "远处犬吠"],
        },
        "emotional_promises": [
            f"围绕{themes or '核心主题'}制造选择代价：人物越接近目标，越要暴露亏欠、软肋或关系裂痕。",
            "胜利必须留下关系回声：感激、误解、亏欠、伤害、沉默或新的恐惧。",
        ],
        "taboo_cliches": [
            "禁止连续使用遇敌-分析-爆发-取胜的单线模板。",
            "禁止用长篇设定说明替代现场冲突。",
            "禁止让配角只负责递线索、解释规则或衬托主角。",
            "禁止高频使用1-3字短句断句制造伪沉重感。",
            "禁止每章用身后/身前、迈步、未知的路等对称式安全锁收尾。",
            "禁止用抽象概念堆叠替代具象生活细节和可见后果。",
            "禁止把地图当成拿道具、升境界、过副本的清单；地点必须有风土人情、制度和生计差异。",
            "禁止整章散文诗式复沓：同一段式、同一物象聚焦句、同一句作者判断不得反复变奏替代事件推进。",
            "每章必须有一个可复述的不可逆动作，证明人物真正向前推进，而不是只营造氛围。",
            "群像章节必须从多声部压力收束到主角独立行动；不能让主角只旁观配角推进。",
            "关键证据、信物和信息必须有可追踪传递链：起点、转交、接收者理解、风险和最终用途。",
            "反派暴露破绽后必须有冷处理策略：规矩、程序、威胁、交易、嫁祸或沉默，而不是只发怒。",
            "跨地点和跨时间叙事必须用声音、光、脚步、物件到达、传话延迟或身体状态咬合。",
            "禁止作者旁注式总结：不要替读者写'这一章真正往前挪'、'权力最怕的是'等读后感。",
            "反派标志物或贯穿意象必须至少一次反照其旧事、软肋、亏欠或破绽，不能只做随身道具。",
            "旧签押、旧证词或旧物证逼到反派时，必须出现一个半拍身体裂隙，再接冷处理策略。",
            "章末关键证据、拓印、录音、钥匙或信物必须在前文预埋制作、藏匿、转交或瞥见动作。",
            "墨印、拓片、副本、录音备份等复制型证据必须提前写出制作动作。",
            "亲缘、父辈、旧痕或手势线索必须在章末关键动作中有微小回扣，形成情感闭合。",
            "章节最后的关键动作之后必须留下短现场反应形成余韵，而不是动作一落就硬切。",
        ],
    }


SENSORY_PALETTE_CATEGORIES = ("indoor", "outdoor", "body", "object", "sound_smell")


def _migrate_sensory_palette(world: dict) -> bool:
    """把旧版列表型 sensory_palette 迁移为五类对象；已分类则补齐缺失类别。"""
    bible = world.get("quality_bible")
    if not isinstance(bible, dict):
        return False
    palette = bible.get("sensory_palette")
    defaults = _default_quality_bible(world)["sensory_palette"]
    if isinstance(palette, list):
        # 旧列表迁移：整体塞进 indoor + object，其余用默认补齐
        items = [str(item).strip() for item in palette if str(item).strip()]
        bible["sensory_palette"] = {
            "indoor": items[:4] if items else defaults["indoor"],
            "outdoor": defaults["outdoor"],
            "body": defaults["body"],
            "object": items[4:8] if len(items) > 4 else defaults["object"],
            "sound_smell": defaults["sound_smell"],
        }
        return True
    if not isinstance(palette, dict):
        bible["sensory_palette"] = defaults
        return True
    changed = False
    for cat in SENSORY_PALETTE_CATEGORIES:
        value = palette.get(cat)
        if not isinstance(value, list) or not any(str(v).strip() for v in value):
            palette[cat] = defaults[cat]
            changed = True
    return changed


def _ensure_world_quality_bible(world: dict) -> bool:
    if not isinstance(world.get("quality_bible"), dict):
        world["quality_bible"] = _default_quality_bible(world)
        return True
    changed = False
    defaults = _default_quality_bible(world)
    for key in QUALITY_BIBLE_KEYS:
        value = world["quality_bible"].get(key)
        missing = (
            not any(str(item).strip() for item in value)
            if isinstance(value, list)
            else not str(value or "").strip()
        )
        if missing:
            world["quality_bible"][key] = defaults[key]
            changed = True
    if _migrate_sensory_palette(world):
        changed = True
    return changed


def _life_profile_issues(value, path: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(value, dict):
        return [f"{path}.life_profile 缺失或不是对象"]
    vague_markers = ("复杂过去", "背负责任", "说不清", "难以言说", "某个人", "某件事")
    for key in LIFE_PROFILE_KEYS:
        field_path = f"{path}.life_profile.{key}"
        item = value.get(key)
        if key == "daily_habits":
            if not isinstance(item, list) or not any(str(habit).strip() for habit in item):
                issues.append(f"{field_path} 缺失或为空")
            continue
        text = str(item or "").strip()
        if not text:
            issues.append(f"{field_path} 缺失")
        elif len(text) < 8:
            issues.append(f"{field_path} 过短")
        elif any(marker in text for marker in vague_markers):
            issues.append(f"{field_path} 仍偏空泛")
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
                issues.extend(_life_profile_issues(value.get("life_profile"), path))
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
        changed = _ensure_world_quality_bible(world_data) if isinstance(world_data, dict) else False
        if changed:
            WORLD_FILE.write_text(json.dumps(world_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[Planner] world.json 已补齐 quality_bible 质量圣经")
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
  }},
  "quality_bible": {{
    "conflict_engine": "本书最稳定的冲突发动机：人物欲望、现实压力、势力博弈、主线秘密如何相互咬合",
    "content_density_rules": ["每章内容密度规则：新信息/关系变化/代价/伏笔回收至少一项", "设定必须通过现场行动呈现"],
    "scene_variety": ["场景多样性原则：不同阶层、职业、地理或制度空间如何轮换", "避免重复场景的具体禁令"],
    "sensory_palette": {{
      "indoor": ["室内物象：工位、厨房、楼道、账单、药柜等"],
      "outdoor": ["室外物象：街市、野外、雨棚、天气等"],
      "body": ["身体细节：伤口、汗、指节、旧疤、冻红的手等"],
      "object": ["随身物件：旧衣、裂屏手机、钥匙、饭盒、褪色照片等"],
      "sound_smell": ["声音气味：药味、饭香、钟声、楼道灯嗡嗡、铁锈味等"]
    }},
    "emotional_promises": ["本书承诺给读者的情绪回报：爽感、压迫、温情、悔恨、秘密揭开等"],
    "taboo_cliches": ["本书必须避开的套路桥段或AI味表达：短句断句指纹、对称式收尾、抽象概念堆叠、打卡地图、散文诗式复沓等"]
  }}
}}

要求：
1. 修炼体系必须严格遵循 premise 中描述的体系，不要擅自添加或修改境界名称
2. 地点要有层次感和探索价值，从底层到高层逐步展开
3. 势力设计要符合主角的底层起步设定
4. 整体架构要支撑{CONFIG['total_chapters']}章的篇幅
5. 如果 origin/ 中存在素材，必须优先吸收其中的设定、人物、风格和限制，不能与其冲突
6. quality_bible 必须可直接指导 Outliner/Writer：写清冲突发动机、内容密度、场景轮换、感官物象、情绪承诺和禁用套路，不能写空泛口号
7. 必须输出合法的JSON，不要任何注释或额外文本"""

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
    _ensure_world_quality_bible(world_data)
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
            if any("life_profile" in issue for issue in issues):
                print(
                    "[Planner] 旧角色档案缺少人情味字段，建议先运行: "
                    f'python "scripts/maintenance/backfill_humanity_fields.py" --project "{NOVELS_DIR}" --apply --backup'
                )
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
    "life_profile": {{
      "family_ties": "家庭、亲缘或替代性亲密关系",
      "livelihood_pressure": "谋生压力：工作、钱、债务、身份成本或现实困境",
      "old_debts": "欠下的人情、旧恩旧怨或无法偿还的亏欠",
      "soft_spot": "最容易被触动的软肋：具体到某个人、物件或场景",
      "daily_habits": ["日常习惯或小动作1", "日常习惯或小动作2"],
      "relationship_taboo": "最不愿说出口的话或最怕被看穿的关系真相"
    }},
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
      "life_profile": {{
        "family_ties": "家庭、亲缘或替代性亲密关系",
        "livelihood_pressure": "谋生压力或现实难处",
        "old_debts": "与主角或他人的旧恩旧怨/亏欠",
        "soft_spot": "最能显出人味的软肋或牵挂",
        "daily_habits": ["日常习惯或小动作1", "日常习惯或小动作2"],
        "relationship_taboo": "不愿说出口的真话"
      }},
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
      "life_profile": {{
        "family_ties": "其仍在乎或曾经在乎的人际关系",
        "livelihood_pressure": "现实压力、权力成本或生存困境",
        "old_debts": "推动其变坏/执念的旧恩旧怨",
        "soft_spot": "让反派显出人性裂缝的软肋",
        "daily_habits": ["日常习惯或小动作1", "日常习惯或小动作2"],
        "relationship_taboo": "其绝不愿承认的真相"
      }},
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
  }},
  "relationship_matrix": [
    {{
      "pair": ["角色A", "角色B"],
      "surface_relation": "表面关系",
      "hidden_debt": "未说出口的亏欠、秘密或误解",
      "pressure_trigger": "什么事件会让关系恶化或质变",
      "payoff_direction": "后续如何提供情感回报或撕裂"
    }}
  ],
  "casting_plan": {{
    "early_arc_roles": ["前50章必须承担叙事功能的角色及用途"],
    "mid_arc_roles": ["中段负责制造反转、关系压力或世界扩展的角色"],
    "late_arc_roles": ["后段负责终局兑现、背叛、牺牲或真相揭开的角色"]
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
12. 为每个主要角色设计 life_profile；它不是背景百科，而是后续章节制造烟火气、人情味和潜台词的素材库
13. life_profile 必须具体，禁止写“有复杂过去”“背负责任”等空泛句；要落到谁、哪笔账、哪件旧物、哪种日常动作
14. 为整部小说设计 language_fingerprint：包含 prose_style（文风基调：如白描/华丽/简洁有力）、signature_metaphors（标志性比喻意象2-3个）、forbidden_expressions（应避免的AI味表达）
15. relationship_matrix 必须列出至少6组核心人物关系，每组都有 hidden_debt、pressure_trigger 和 payoff_direction，供 Outliner/Writer 制造关系推进
16. casting_plan 必须说明早中后期角色承担的叙事功能，避免人物出场后闲置或工具化"""

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



def _default_media_prompts(world: dict, characters: dict) -> dict:
    title = world.get("title", NOVELS_DIR.name)
    world_desc = world.get("world_description", "")
    protagonist = characters.get("protagonist", {})
    protagonist_name = protagonist.get("name", "主角") if isinstance(protagonist, dict) else "主角"
    visual_core = f"《{title}》，{world_desc[:300]}，主角{protagonist_name}"
    return {
        "cover_prompt": (
            f"中文网络小说封面，书名《{title}》，{visual_core}。"
            "电影级构图，强烈故事感，视觉风格必须贴合本书题材和时代背景；主角居中但不要摆拍，"
            "背景必须出现一个能识别本书核心矛盾的具体物件/地点/势力符号。高细节，商业出版封面，"
            "避免现代广告字样、水印、纯氛围剪影和通用玄幻光效。"
        ),
        "video_prompt": (
            f"根据小说《{title}》世界观制作15秒电影感概念预告片：{visual_core}。"
            "镜头必须包含三段：生活/现实压力细节、核心规则或势力压迫、主角做出选择的瞬间；"
            "从可触摸的物件推进到主角行动，再展现主要冲突。动态光影，题材氛围鲜明，无字幕，无水印，"
            "禁止只有空镜、云海、火焰或抽象能量。"
        ),
        "song_prompt": (
            f"为中文网络小说《{title}》创作主题曲，贴合世界观：{world_desc[:300]}。"
            "情绪从困境起步到关系牵挂、关键抉择和阶段性爆发；旋律要有记忆点，"
            "副歌体现主角的代价与不认命，适合小说宣传视频和阅读氛围。"
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
