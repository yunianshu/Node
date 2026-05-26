#!/usr/bin/env python3
"""
Rewrite Agent - 重写Agent
根据Reviewer的审核意见，对初稿进行重写
初稿来源: chapters/draft/
重写输出: chapters/final/
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config
from core.push_notifier import push_stage_complete
from core.workflow_state import load_outline_chapter, outline_index_path, review_dir
from core.workflow_state import analyze_chapter_text, is_valid_chapter_text, load_review_status, read_text_length
from core.workflow_state import scan_chapter_status, write_status_file, highest_contiguous, report_path

configure_stdio()

NOVELS_DIR = None
DRAFT_DIR = None
FINAL_DIR = None
REVIEWS_DIR = None
SOURCE_DIR = None  # 打磨模式时从final读取源稿
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
MAX_REWRITE_ATTEMPTS = 5
POLISH_MODE = False


def init_project(project_dir: str | Path, final_mode: bool = False, polish_mode: bool = False) -> None:
    global NOVELS_DIR, DRAFT_DIR, FINAL_DIR, REVIEWS_DIR, SOURCE_DIR, WORLD_FILE, OUTLINE_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG, POLISH_MODE
    NOVELS_DIR = Path(project_dir).resolve()
    DRAFT_DIR = NOVELS_DIR / "chapters" / "draft"
    FINAL_DIR = NOVELS_DIR / "chapters" / "final"
    POLISH_MODE = polish_mode
    if final_mode:
        REVIEWS_DIR = NOVELS_DIR / "chapters" / "review_final"
        SOURCE_DIR = FINAL_DIR  # 打磨终稿，源文件来自final
    else:
        REVIEWS_DIR = review_dir(NOVELS_DIR)
        SOURCE_DIR = DRAFT_DIR
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "rewrite_agent.log"
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
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
            raw_name="rewrite",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        log(f"[ERROR] mmx调用失败: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def rewrite_attempt_dir(chapter_number: int) -> Path:
    return NOVELS_DIR / "chapters" / "rewrite" / f"chapter_{chapter_number:04d}"


def score_rewrite_attempt(content: str) -> dict:
    word_count, grade, passed, issues = analyze_chapter_text(content)
    score = 10.0 if passed else 0.0
    quality = CONFIG.get("quality", {})
    target_words = int(quality.get("min_chapter_words", 5000))
    if not passed:
        if word_count >= target_words:
            score += 5.0
        elif word_count > 0:
            score += min(4.0, word_count / target_words * 4.0)
        if grade == "warn":
            score += 2.0
        score -= min(3.0, len(issues) * 0.5)
    return {
        "word_count": word_count,
        "grade": grade,
        "passed": passed,
        "issues": issues,
        "score": round(max(0.0, min(10.0, score)), 2),
    }


def save_rewrite_attempt(chapter_number: int, attempt: int, content: str, review: dict) -> dict:
    target_dir = rewrite_attempt_dir(chapter_number)
    target_dir.mkdir(parents=True, exist_ok=True)
    text_file = target_dir / f"attempt_{attempt:02d}.txt"
    review_file = target_dir / f"attempt_{attempt:02d}_review.json"
    text_file.write_text(content, encoding="utf-8")
    review_file.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "attempt": attempt,
        "content_file": str(text_file),
        "review_file": str(review_file),
        **review,
    }


def select_best_attempt(attempts: list[dict]) -> dict | None:
    if not attempts:
        return None
    passed = [item for item in attempts if item.get("passed")]
    candidates = passed or attempts
    return max(candidates, key=lambda item: (item.get("score", 0), item.get("word_count", 0)))


def rewrite_chapter(chapter_number: int, retry: int = 0, force: bool = False) -> str:
    source_file = SOURCE_DIR / f"chapter_{chapter_number:04d}.txt"
    final_file = FINAL_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    final_exists, final_words, final_ok = read_text_length(final_file)
    _, _, _, review_ok = load_review_status(review_file)
    if final_exists and final_ok and review_ok and not force and not POLISH_MODE:
        log(f"[Rewrite] 第{chapter_number}章终稿已存在且质量门通过（{final_words}字），跳过")
        return "exists"

    if not source_file.exists():
        log(f"[Rewrite] 第{chapter_number}章源稿不存在，无法重写")
        return "no_draft"

    if not review_file.exists():
        log(f"[Rewrite] 第{chapter_number}章无审查报告，跳过")
        return "no_review"

    review_data = load_json(review_file)
    verdict = review_data.get("verdict", "")
    score = review_data.get("overall_score", 10)
    try:
        score = float(score)
    except (ValueError, TypeError):
        score = 10.0

    # polish模式：只要<8.5就打磨；普通模式：按原有逻辑
    polish_threshold = 9.0 if POLISH_MODE else 7.0
    if verdict != "需重写" and score >= polish_threshold and not POLISH_MODE:
        _, source_words, source_ok = read_text_length(source_file)
        if not source_ok:
            log(f"[Rewrite] 第{chapter_number}章评分{score}但源稿字数不合格（{source_words}字），进入重写")
        else:
            log(f"[Rewrite] 第{chapter_number}章评分{score}无需重写，直接复制源稿到终稿")
            content = source_file.read_text(encoding="utf-8")
            FINAL_DIR.mkdir(parents=True, exist_ok=True)
            with open(final_file, "w", encoding="utf-8") as f:
                f.write(content)
            return "copied"

    world = load_json(WORLD_FILE)
    characters = load_json(CHARACTERS_FILE)

    chapter_outline = load_outline_chapter(NOVELS_DIR, chapter_number)

    if not chapter_outline:
        log(f"[Rewrite] 第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = load_outline_chapter(NOVELS_DIR, chapter_number - 1).get("summary", "")
    next_summary = load_outline_chapter(NOVELS_DIR, chapter_number + 1).get("summary", "")

    prev_ending = ""
    if chapter_number > 1:
        prev_final = FINAL_DIR / f"chapter_{chapter_number-1:04d}.txt"
        prev_source = SOURCE_DIR / f"chapter_{chapter_number-1:04d}.txt"
        prev_file = prev_final if prev_final.exists() else prev_source
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    source_content = source_file.read_text(encoding="utf-8")

    suggestions = review_data.get("suggestions", [])
    continuity_issues = review_data.get("continuity_issues", [])
    strengths = review_data.get("strengths", [])
    weaknesses = review_data.get("weaknesses", [])
    scores = review_data.get("scores", {})

    review_section = "\n\n## 编辑审查反馈（请严格参考以下建议重写）\n"

    if strengths:
        review_section += "\n### 原文优点（请务必保留）\n"
        for s in strengths:
            review_section += f"- {s}\n"

    if weaknesses:
        review_section += "\n### 原文不足（必须改进）\n"
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

    if scores:
        review_section += "\n### 各维度评分\n"
        for dim, score_val in scores.items():
            review_section += f"- {dim}: {score_val}分\n"

    review_section += f"\n### 原文参考（前1000字，了解原有风格）\n{source_content[:1000]}\n...\n"
    review_section += f"\n### 原文参考（结尾500字，保持结尾走向）\n{source_content[-500:] if len(source_content) > 500 else source_content}\n"

    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))

    if POLISH_MODE:
        system = f"""你是一位殿堂级中文网络小说大师，专门负责将优秀章节打磨成**9.0分以上的神作**。
你的任务不是从零创作，而是在现有终稿基础上进行**极致精修**，让每一个字、每一句话都达到经典级水准。

当前章节评分{score}分，目标是提升到**9.0分以上**。你必须让所有维度都达到顶尖水准。

9.0分神作的九大黄金标准（缺一不可）：

1. **文笔质感（writing_quality）——如诗如画**
   - 长短句精妙交错，读起来有音乐般的韵律感
   - 修辞信手拈来且不落俗套：比喻要新奇、拟人要传神、通感要惊艳
   - 动词做到"一字千金"：同样的动作，你的描写让读者过目难忘
   - 每个场景至少有两三处让人拍案叫绝的描写，读者会忍不住截图分享

2. **场景沉浸感（scene_description）——身临其境**
   - 五感全开且自然融合：不是罗列感官，而是让读者通过角色的身体感受世界
   - 环境是活的：不仅参与叙事，还能预示命运、映射心境、推动转折
   - 空间镜头感：远景铺陈气势→中景展开冲突→近景刻画细节→特写定格灵魂
   - 每处场景都要有"记忆点"，读者闭着眼能回想起画面

3. **人物立体度（character_consistency）——栩栩如生**
   - 每个角色有独一无二的"语言指纹"：换一个人说不出同样的话
   - 配角有自己的秘密、欲望和小算盘，读者会好奇他们的故事
   - 主角内心层次分明：表层反应→深层动机→潜意识冲突→价值观裂变
   - 人物成长有迹可循，不是突然顿悟，而是量变到质变的积累

4. **对话质量（dialogue_quality）——句句带锋**
   - 对话像高手过招：表面客套，实则交锋；每句话都在试探、博弈、施压
   - 同一信息由不同角色传达，用词、语气、潜台词完全不同
   - 信息绝不通过说教传递，全靠行动、冲突、选择和后果呈现
   - 关键时刻的对白是"减法艺术"：越简短，越有力，越让人回味

5. **情感共鸣（emotional_impact）——直击灵魂**
   - 情感从不直接命名：不说"他很悲伤"，而是写他"盯着那碗已经凉透的面，筷子悬在半空，最终轻轻放下"
   - 情绪曲线有完整的生命：萌芽→压抑→酝酿→临界点→爆发→余震→沉淀
   - 让读者为角色笑、为角色哭、为角色恨、为角色不甘——产生真正的情感投资

6. **节奏掌控（pacing）——张弛如呼吸**
   - 高潮场面：短句如鼓点，每段一个冲击，让读者屏住呼吸翻页
   - 抒情段落：长句如流水，细腻到毛孔，让读者舍不得翻页
   - 整章的节奏像一首曲子：有前奏、有高潮、有回落、有华彩尾声
   - 信息密度精准：每句话要么推进剧情，要么深化人物，要么营造氛围

7. **冲突层次（plot_coherence）——多维绞杀**
   - 表层：人与人的直接对抗，刀光剑影、唇枪舌剑
   - 中层：人与环境的博弈，规则压迫、命运捉弄、时代洪流
   - 深层：人与自我的撕裂，欲望与道德的拔河、理想与现实的撕扯
   - 三层冲突在同一章中交织缠绕，互相催化，让 tension 指数级上升

8. **悬念与钩子（hook）——欲罢不能**
   - 章节结尾的钩子要像 cliff 边的手指：读者必须看下一章才能安心
   - 悬念类型多样：未解之谜、突发反转、情感危机、伏笔炸开、角色命运悬而未决
   - 不仅是"发生了什么"，更是"为什么会这样""接下来怎么办"

9. **主题深度（theme）——余韵悠长**
   - 好故事讲"事"，神作讲"道"：人性、命运、选择、代价
   - 主题不是喊口号，而是藏在情节、对话、象征、细节里，让读者自己悟出来
   - 读完后回味无穷，忍不住思考"如果是我，我会怎么选"

精修铁律：
- **保留神来之笔**：原文的高光段落、金句、绝妙设计必须保留并放大
- **攻克每一分**：针对Reviewer扣分的维度重点突破，不留短板
- **零容忍截断**：必须完整输出全章，出现[中间部分...]等标记是严重失职
- **字数硬指标**：严格控制在{min_words}-{max_words}字，绝对不可低于{warn_min}字
- **风格如指纹**：保持原有文风的一致性，精修后应该像原作者的"超频版"""
    else:
        system = f"""你是一位顶尖的中文网络小说作家，同时也是资深编辑。
你现在需要对一篇初稿进行**重写升级**，而不是从零创作。
你的目标是让重写后的章节达到**8.5分以上的优秀标准**。

优秀网文的八大核心标准（必须全部达到）：
1. **文笔流畅细腻**：句式有变化，长短句交错，行文节奏感强，避免口语化和重复表达
2. **场景画面感**：用五官感受描写场景（视觉、听觉、嗅觉、触觉、味觉），让读者身临其境
3. **人物立体鲜活**：每个角色有独特的说话方式、思维方式、行为特征，不是脸谱化工具人
4. **对话生动自然**：对话推动情节，符合角色身份，有潜台词和情绪层次，不要干巴巴交代信息
5. **情感共鸣强**：主角的情绪变化有层次，让读者能产生代入感和情绪波动
6. **节奏张弛有度**：紧张场面紧凑激烈，过渡段落舒缓自然，有起承转合的韵律
7. **冲突有层次感**：不是简单的打来打去，而是心理冲突、利益冲突、价值观冲突交织
8. **悬念和钩子**：每章结尾留有余味，让读者想继续看下一章

重写原则：
- 保留原文的优点和核心情节框架，不要完全推倒重来
- 严格落实验编给出的具体修改建议，每一条都要落实
- 修正连续性问题和逻辑漏洞
- 每章尽量控制在{min_words}-{max_words}字之间
- 保持角色性格一致性，前后情节衔接自然
- 重写后的内容必须是原文的**全面提升版**，不能只是小修小补"""

    task_verb = "深度精修" if POLISH_MODE else "重写"
    prompt = f"""请根据以下信息，{task_verb}第{chapter_number}章《{chapter_outline.get('title', '未命名')}》。

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## 本章大纲（必须严格遵循）
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 写作要求
1. 本章字数尽量控制在{min_words}-{max_words}字之间，绝对不可低于{warn_min}字
2. 严格按照大纲核心事件展开，不要遗漏任何情节点
3. 开头要自然衔接前一章，结尾要留悬念或引出下一章
4. 对话要符合角色性格，推动情节发展，对话要详细具体
5. 场景描写要生动细致，让读者有画面感，不要一笔带过
6. 冲突/对抗场面要紧张刺激，有层次感
7. 心理描写要细腻，展现主角内心变化
8. 不要流水账，要有起伏和转折
9. 不要输出章节标题，直接从正文开始
10. 不要输出任何元信息，只输出正文

请开始重写："""

    attempts = []
    for attempt in range(1, MAX_REWRITE_ATTEMPTS + 1):
        log(f"[Rewrite] 正在{task_verb}第{chapter_number}章（第{attempt}/{MAX_REWRITE_ATTEMPTS}次，源稿{len(source_content)}字，评分{score}）...")
        temp = 0.75 if POLISH_MODE else 0.85
        content = call_mmx(system, prompt, max_tokens=12000, temperature=temp)

        if not content:
            log(f"[Rewrite] 第{chapter_number}章第{attempt}次重写失败，无返回内容")
            time.sleep(CONFIG["writer"]["retry_delay"])
            continue

        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()

        word_count = len(content)

        if word_count < warn_min:
            log(f"[Rewrite] 第{chapter_number}章第{attempt}次字数不足({word_count}字)，尝试补充...")
            supplement_prompt = f"""以下是一章小说的内容，但字数只有{word_count}字，需要扩展到至少{warn_min}字。
请在保持原有情节和风格的基础上，通过以下方式扩充：
1. 增加环境描写的细节
2. 扩展对话内容，让对话更完整
3. 增加角色的心理活动和内心独白
4. 增加场景转换的过渡描写
5. 增加侧面描写和氛围渲染

请直接在原文基础上扩充，输出完整的{warn_min}字以上版本：

{content}"""
            supplement = call_mmx(system, supplement_prompt, max_tokens=12000, temperature=0.7)
            if supplement and len(supplement) > word_count:
                content = supplement.strip()
                word_count = len(content)
                log(f"[Rewrite] 第{chapter_number}章第{attempt}次扩充后{word_count}字")

        attempt_review = score_rewrite_attempt(content)
        attempts.append(save_rewrite_attempt(chapter_number, attempt, content, attempt_review))
        log(
            f"[Rewrite] 第{chapter_number}章第{attempt}次评分{attempt_review['score']}，"
            f"字数{attempt_review['word_count']}，通过={attempt_review['passed']}"
        )
        if attempt_review["passed"]:
            break
        time.sleep(CONFIG["writer"]["retry_delay"])

    best = select_best_attempt(attempts)
    if not best:
        log(f"[Rewrite] 第{chapter_number}章重写失败，{MAX_REWRITE_ATTEMPTS}次均无有效内容")
        return "failed"

    selected_content = Path(best["content_file"]).read_text(encoding="utf-8")
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    final_file.write_text(selected_content, encoding="utf-8")

    meta = {
        "chapter": chapter_number,
        "max_attempts": MAX_REWRITE_ATTEMPTS,
        "selected_attempt": best["attempt"],
        "selected_reason": "quality_passed" if best.get("passed") else "best_score_fallback",
        "attempts": attempts,
    }
    meta_file = rewrite_attempt_dir(chapter_number) / "rewrite_meta.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    log(
        f"[Rewrite] 第{chapter_number}章终稿已保存（选用第{best['attempt']}次，"
        f"评分{best.get('score')}，{best.get('word_count')}字） -> {final_file}"
    )
    return "success" if best.get("passed") else "best_effort"


def _refresh_status(start: int, end: int) -> None:
    """扫描处理过的章节，增量更新 chapter_status.json 和 progress.json"""
    try:
        log(f"[Rewrite] 刷新状态文件 ({start}-{end})...")
        statuses = scan_chapter_status(NOVELS_DIR, start, end, use_cache=False)
        write_status_file(NOVELS_DIR, statuses.values())

        progress_file = report_path(NOVELS_DIR, "progress.json")
        if progress_file.exists():
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        else:
            progress = {
                "planner_done": True,
                "last_generated_chapter": 0,
                "last_reviewed_chapter": 0,
                "failed_chapters": [],
                "rewrite_queue": [],
            }

        all_statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"], use_cache=False)
        progress["last_reviewed_chapter"] = highest_contiguous(all_statuses, 1, "review_ok")

        # 清理 rewrite_queue：移除已有终稿的章节
        queue = set(progress.get("rewrite_queue", []))
        for ch in range(start, end + 1):
            key = f"{ch:04d}"
            s = all_statuses.get(ch)
            if s and s.final_ok and ch in queue:
                queue.discard(ch)
        progress["rewrite_queue"] = sorted(queue)

        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        log(f"[Rewrite] 状态刷新完成，last_reviewed_chapter={progress['last_reviewed_chapter']}, rewrite_queue={len(progress['rewrite_queue'])}")
    except Exception as e:
        log(f"[Rewrite] 状态刷新失败: {e}")


def _get_book_title() -> str:
    title = "本小说"
    if WORLD_FILE.exists():
        try:
            with open(WORLD_FILE, "r", encoding="utf-8") as f:
                title = json.load(f).get("title", title)
        except Exception:
            pass
    return title


def get_rewrite_candidates(threshold: float = 7.0, polish_mode: bool = False) -> list:
    candidates = []
    if not REVIEWS_DIR.exists():
        return candidates

    for review_file in REVIEWS_DIR.glob("chapter_*_review.json"):
        try:
            num = int(review_file.stem.split("_")[1])
            review = load_json(review_file)
            verdict = review.get("verdict", "")
            score = review.get("overall_score", 10)
            try:
                score = float(score)
            except (ValueError, TypeError):
                score = 10.0
            # polish模式：所有<8.5的章节都需要打磨；普通模式：按threshold筛选
            effective_threshold = 8.5 if polish_mode else threshold
            if verdict == "需重写" or score < effective_threshold:
                candidates.append((num, score, verdict))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[0])
    return candidates


def main():
    parser = argparse.ArgumentParser(description="根据Reviewer意见重写章节")
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只重写某一章")
    parser.add_argument("--candidates", action="store_true", help="只列出需要重写的章节")
    parser.add_argument("--workers", type=int, default=0, help="并行重写Agent数量")
    parser.add_argument("--all", action="store_true", help="处理所有章节（含直接复制）")
    parser.add_argument("--threshold", type=float, default=7.0, help="低于此分数的章节将被重写（默认7.0）")
    parser.add_argument("--force", action="store_true", help="强制重写，即使终稿已存在")
    parser.add_argument("--final", action="store_true", help="终稿打磨模式：读取final目录作为源稿，使用review_final作为审查依据")
    parser.add_argument("--polish", action="store_true", help="精品打磨模式：使用8.5+高标准prompt精修")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project, final_mode=args.final, polish_mode=args.polish)
    end = args.end or CONFIG["total_chapters"]
    workers = args.workers or CONFIG["coordinator"]["num_workers"]

    print("=" * 60)
    print("Rewrite Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if args.candidates:
        candidates = get_rewrite_candidates(threshold=args.threshold, polish_mode=args.polish)
        mode_label = "打磨" if args.polish else "重写"
        effective_th = 8.5 if args.polish else args.threshold
        print(f"需要{mode_label}的章节: {len(candidates)} 个 (阈值: {effective_th})")
        for num, score, verdict in candidates:
            print(f"  第{num}章: 评分{score} [{verdict}]")
        return

    title = _get_book_title()

    if args.chapter > 0:
        result = rewrite_chapter(args.chapter, force=args.force)
        _refresh_status(args.chapter, args.chapter)
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿",
            start_chapter=args.chapter, end_chapter=args.chapter,
            processed=1 if result in ("success", "best_effort", "copied", "exists") else 0,
            failed=1 if result == "failed" else 0,
        )
        return

    if args.all:
        chapters_to_process = []
        for ch in range(args.start, end + 1):
            review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
            if review_file.exists():
                review = load_json(review_file)
                score = review.get("overall_score", 10)
                try:
                    score = float(score)
                except (ValueError, TypeError):
                    score = 10.0
                verdict = review.get("verdict", "")
                effective_threshold = 8.5 if args.polish else args.threshold
                if verdict == "需重写" or score < effective_threshold:
                    chapters_to_process.append((ch, "rewrite"))
                else:
                    chapters_to_process.append((ch, "copy"))
            else:
                source_file = SOURCE_DIR / f"chapter_{ch:04d}.txt"
                if source_file.exists():
                    chapters_to_process.append((ch, "copy"))

        mode_label = "打磨" if args.polish else "重写"
        print(f"共需处理 {len(chapters_to_process)} 章（{mode_label} + 复制）")

        completed = 0
        failed = []

        def process_single(args_tuple):
            ch, action = args_tuple
            if action == "copy":
                source_f = SOURCE_DIR / f"chapter_{ch:04d}.txt"
                final_file = FINAL_DIR / f"chapter_{ch:04d}.txt"
                if source_f.exists() and (not final_file.exists() or final_file.stat().st_size < 1000):
                    content = source_f.read_text(encoding="utf-8")
                    FINAL_DIR.mkdir(parents=True, exist_ok=True)
                    with open(final_file, "w", encoding="utf-8") as f:
                        f.write(content)
                    log(f"[Rewrite] 第{ch}章直接复制到终稿（{len(content)}字）")
                    return ch, "copied"
                return ch, "exists"
            else:
                result = rewrite_chapter(ch, force=args.force)
                return ch, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_single, item): item for item in chapters_to_process}
            for future in concurrent.futures.as_completed(futures):
                ch, result = future.result()
                if result in ("success", "best_effort", "copied"):
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
                print(f"进度: {completed}/{len(chapters_to_process)} 完成, 失败: {len(failed)}")

        log(f"[Rewrite] 全部完成: 成功{completed}章, 失败{len(failed)}章")
        if failed:
            log(f"失败章节: {failed}")
        _refresh_status(args.start, end)
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿打磨" if args.polish else "终稿",
            start_chapter=args.start, end_chapter=end,
            processed=completed, failed=len(failed),
        )
        return

    candidates = get_rewrite_candidates(polish_mode=args.polish)
    print(f"需要{mode_label}的章节: {len(candidates)} 个")
    for num, score, verdict in candidates:
        print(f"  第{num}章: 评分{score} [{verdict}]")

    if not candidates:
        print("没有需要重写的章节")
        return

    completed = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(rewrite_chapter, num, force=args.force): num for num, _, _ in candidates}
        for future in concurrent.futures.as_completed(futures):
            ch = futures[future]
            try:
                result = future.result()
                if result in ("success", "best_effort"):
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
            except Exception as exc:
                log(f"[ERROR] 第{ch}章异常: {exc}")
                failed.append(ch)

    log(f"[Rewrite] 完成: 成功{completed}章, 失败{len(failed)}章")
    if failed:
        log(f"失败: {failed}")

    # 刷新状态：使用 candidates 的章节范围
    if candidates:
        ch_nums = [num for num, _, _ in candidates]
        _refresh_status(min(ch_nums), max(ch_nums))
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿精品打磨" if args.polish else "终稿重写",
            start_chapter=min(ch_nums), end_chapter=max(ch_nums),
            processed=completed, failed=len(failed),
        )


if __name__ == "__main__":
    main()
