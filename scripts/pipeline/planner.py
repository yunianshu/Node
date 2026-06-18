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
import sys
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import load_config, load_origin_materials, resolve_project_dir

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
        NOVEL_PREMISE = f"""《长生武道：虚空万界行》是《长生武道：从五禽养生拳开始》的续作/后传。

前作结局回顾：
主角苏长空，从黑铁山庄一个孱弱少年起步，修炼五禽养生拳、龟息真定功、天蚕神功等长生武学，靠"寿命增长则天赋无限提升"的金手指一步步崛起。历经百年，他达到了前无古人的"魂界"境界——在识海中开辟天地、演化世界，独立于天地之外。他斩杀了祸乱天地的大反派天魔神，拯救了人族。此时他100岁，寿命10000年，潜能值1000点。
苏长空的同伴包括：华善（古圣，生命之道，治愈大师，鹤发童颜的老者）、姬雪潇（神凰转世，掌握虚无之道，冰晶火焰构成的神凰本体）、战无双等古圣强者。他们炼化了天道碎片，原本无法离开故土天地，但苏长空的魂界可以收容他们，带他们一起离开。

续写设定：
苏长空带着华善、姬雪潇等同伴，离开了故乡天地，进入了无尽虚空。无尽虚空是一片浩瀚的黑暗空间，其中漂浮着无数"天地"（世界），每个天地都有独立的天道法则和修炼体系。虚空中存在着各种各样的文明、种族和强者，还有危险的虚空生物、虚空风暴等。
苏长空的魂界是一个还在成长中的独立世界，他需要在探索中不断壮大魂界。魂界境之上还有更高境界：界主境（完全掌控一方天地）、虚空境（在虚空中自由穿行，不惧虚空风暴）、混沌境（超越虚空，触及宇宙本源）。
全书共{total}章，每章约5000字。风格延续前作的热血、升级、长生武道流，融合虚空万界、异界探索、文明碰撞等元素。"""


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
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
            raw_name="planner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def generate_world():
    if WORLD_FILE.exists():
        print(f"[Planner] world.json 已存在，跳过生成")
        return

    system = """你是一位顶级东方玄幻/武侠/修仙世界观架构师。
你需要根据用户提供的故事 premise，构建一个完整、详细、有深度的世界观。
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
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.4)
    if not content:
        print("[Planner] 世界观生成失败")
        return

    try:
        world_data = json.loads(content)
        with open(WORLD_FILE, "w", encoding="utf-8") as f:
            json.dump(world_data, f, ensure_ascii=False, indent=2)
        print(f"[Planner] 世界观已保存到 {WORLD_FILE}")
    except json.JSONDecodeError as e:
        print(f"[Planner] JSON解析失败: {e}")
        # 状态机式引号修复
        from core.json_repair import fix_inner_quotes
        fixed = fix_inner_quotes(content)
        try:
            start = fixed.index("{")
            end = fixed.rindex("}") + 1
            world_data = json.loads(fixed[start:end])
            with open(WORLD_FILE, "w", encoding="utf-8") as f:
                json.dump(world_data, f, ensure_ascii=False, indent=2)
            print(f"[Planner] 世界观已保存（经过修复）")
        except Exception as e2:
            print(f"[Planner] 修复失败: {e2}")
            with open(WORLD_FILE.with_suffix(".raw"), "w", encoding="utf-8") as f:
                f.write(content)


def generate_characters():
    if CHARACTERS_FILE.exists():
        print(f"[Planner] characters.json 已存在，跳过生成")
        return

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise 设计主要角色。
输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

请输出包含 protagonist、companions、new_characters、antagonists 的JSON结构。

要求：
1. 主角设计要符合 premise 中的描述，有完整的成长路径设计
2. 同伴角色要有血有肉，与主角有真实的情感羁绊
3. 新角色至少设计8个重要角色，涵盖同伴、导师、对手等类型
4. 可以有红颜知己或暧昧角色，但不要太滥
5. 反派要有层次，设计至少3个层级的反派（小反派、中BOSS、最终BOSS）
6. 如果 origin/ 中存在角色、前作、背景或风格素材，必须优先参考并保持一致
7. 必须输出合法JSON"""

    print("[Planner] 正在生成角色档案...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.4)
    if not content:
        print("[Planner] 角色档案生成失败")
        return

    try:
        chars_data = json.loads(content)
        with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(chars_data, f, ensure_ascii=False, indent=2)
        print(f"[Planner] 角色档案已保存到 {CHARACTERS_FILE}")
    except json.JSONDecodeError as e:
        print(f"[Planner] JSON解析失败: {e}")
        from core.json_repair import fix_inner_quotes
        fixed = fix_inner_quotes(content)
        try:
            start = fixed.index("{")
            end = fixed.rindex("}") + 1
            chars_data = json.loads(fixed[start:end])
            with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
                json.dump(chars_data, f, ensure_ascii=False, indent=2)
            print(f"[Planner] 角色档案已保存（经过修复）")
        except Exception as e2:
            print(f"[Planner] 修复失败: {e2}")


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
            "电影级构图，强烈故事感，东方幻想/武侠质感，主角居中，背景展现核心世界观，"
            "高细节，商业出版封面，避免现代广告字样和水印。"
        ),
        "video_prompt": (
            f"根据小说《{title}》世界观制作15秒电影感概念预告片：{visual_core}。"
            "镜头从世界核心地貌推进到主角背影，再展现力量体系与主要冲突，史诗感，"
            "动态光影，东方幻想氛围，无字幕，无水印。"
        ),
        "song_prompt": (
            f"为中文网络小说《{title}》创作主题曲，贴合世界观：{world_desc[:300]}。"
            "情绪从孤独起步到热血崛起，适合小说宣传视频和阅读氛围。"
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default="",
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="兼容参数，Planner不再生成大纲")
    parser.add_argument("--end", type=int, default=0, help="兼容参数，Planner不再生成大纲")
    parser.add_argument("--world-only", action="store_true", help="兼容参数，Planner默认只生成世界观和角色")
    parser.add_argument("--outline-file", type=str, default="", help="兼容参数；Planner 不生成大纲")
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

    generate_world()
    generate_characters()
    generate_media_prompts()

    # 媒体资产生成：提示词就绪后立即生成封面/视频/主题歌。
    # 放在 planner 末尾，使无论用 coordinator 还是 _gen_serial.py 等任意编排，
    # 只要跑过 planner，媒体都会生成（config.media.enabled=false 时跳过）。
    if not generate_media_assets():
        print("[Planner] 媒体资产生成未完成（详见 logs/media_generator.log）")

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
