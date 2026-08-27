#!/usr/bin/env python3
"""
Writer Agent - 内容生成Agent
负责根据大纲生成具体章节内容，保存为txt文件
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.json_repair import repair_latin1_gbk_mojibake as _repair_latin1_gbk_mojibake
from core.novel_config import (
    build_origin_fact_directive,
    configure_stdio,
    load_config,
    load_origin_materials,
    resolve_project_dir,
)
from core.workflow_state import load_outline_chapter, review_dir
from core.workflow_state import FORBIDDEN_PHRASES, VALID_ENDINGS, is_valid_chapter_text, read_text_length
from core.edit_diff import (
    EditApplyError,
    apply_text_edits,
    build_edit_prompt,
    check_preserved_ratio,
    parse_edit_ops,
    similarity,
)
from core.ai_flavor_detector import detect_ai_flavor

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""
CANDIDATE_MODE = False
NOVEL_PREMISE = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, WORLD_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG, ORIGIN_MATERIALS, NOVEL_PREMISE
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "writer.log"
    CONFIG = load_config(NOVELS_DIR)
    premise_file = NOVELS_DIR / "premise.txt"
    NOVEL_PREMISE = premise_file.read_text(encoding="utf-8") if premise_file.exists() else ""
    # 限制 origin 素材长度，避免 prompt 过长导致 API 超时（reviewer/outline_reviewer 都有此限制）
    origin_max = int(CONFIG.get("writer", {}).get("origin_max_chars", 1200) or 1200)
    ORIGIN_MATERIALS = load_origin_materials(NOVELS_DIR, max_chars=origin_max)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    try:
        writer_cfg = CONFIG.get("writer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=int(writer_cfg.get("candidate_retries", 0) if CANDIDATE_MODE else writer_cfg.get("max_retries", 3)),
            retry_delay=float(writer_cfg.get("retry_delay", 5.0)),
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="writer",
            timeout=int(writer_cfg.get("candidate_timeout_seconds", 240) if CANDIDATE_MODE else writer_cfg.get("timeout_seconds", 300)),
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


_GENRE_KEYWORDS = {
    "都市悬疑/社会派": ["悬疑", "案件", "调查", "记者", "证据", "真相", "追踪", "警方", "犯罪", "谜", "反转", "都市", "档案", "举报", "旧案"],
    "东方玄幻/仙侠": ["修仙", "武道", "真气", "灵气", "境界", "斗气", "魔法", "飞升", "宗门", "法宝", "神通", "筑基", "金丹", "元婴", "渡劫", "仙人", "神魔", "妖兽", "灵根", "天劫", "修炼", "炼气", "悟道", "儒道", "剑修", "魔教", "仙宫", "圣地"],
    "都市重生/职场": ["重生", "都市", "现代", "职场", "校园", "商战", "创业", "房价", "互联网", "移动互联网", "智能手机", "时代", "金钱", "银行卡", "股票", "投资", "公司", "上班", "打工", "商业", "电商", "地产", "金融", "中年", "青年", "生活", "婚姻", "家庭"],
    "灵异恐怖": ["鬼", "灵异", "恐怖", "诡异", "尸体", "死亡", "诅咒", "惊悚", "阴间", "黄泉", "冥界", "怨灵", "厉鬼", "驱鬼", "驭鬼", "复苏", "僵尸", "邪祟", "阴气", "灵魂"],
    "科幻未来": ["星际", "飞船", "机甲", "基因", "未来", "太空", "人工智能", "AI", "机器人", "量子", "宇宙", "星球", "外星", "末世", "丧尸", "核战", "科技"],
    "历史架空": ["古代", "王朝", "皇帝", "科举", "诸侯", "架空", "宫廷", "权谋", "宦官", "将士", "兵马", "江山", "天下", "登基", "丞相", "郡主", "王爷"],
}


def _infer_genre(world: dict) -> str:
    """根据 world.json 内容推断题材类型，返回中文题材描述。"""
    text = world.get("world_description", "") + " " + world.get("title", "") + " " + str(world.get("power_system", {}))
    scores = {}
    for genre, keywords in _GENRE_KEYWORDS.items():
        score = sum(text.count(kw) for kw in keywords)
        scores[genre] = score
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
    return "网络小说"


def _flatten_project_text(*values, limit: int = 12000) -> str:
    parts: list[str] = []

    def walk(value) -> None:
        if len(" ".join(parts)) >= limit:
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                parts.append(text)

    for value in values:
        walk(value)
    return " ".join(parts)[:limit]


def _payoff_profile(world: dict, chapter_outline: dict | None = None) -> dict:
    text = _flatten_project_text(world, chapter_outline or {}, NOVEL_PREMISE).lower()
    if any(key in text for key in ("悬疑", "案件", "调查", "记者", "证据", "真相", "追踪", "警方", "犯罪", "谜", "都市")):
        return {
            "label": "阅读回报",
            "chain": "期待→压迫/阻碍→线索反转→代价兑现/局势推进",
            "directive": "回报来自主角在压力下的判断、行动、取舍、证据推进或关系破局，不要求战力碾压。",
        }
    if any(key in text for key in ("修仙", "武道", "玄幻", "境界", "灵气", "真气", "宗门", "神通", "法宝", "修炼")):
        return {
            "label": "爽点链",
            "chain": "期待→压制→反转→兑现",
            "directive": "爽点来自主角判断、能力、资源或协作的真实发挥，不能靠巧合或突然觉醒硬赢。",
        }
    return {
        "label": "阅读回报",
        "chain": "期待→阻碍→反转→兑现",
        "directive": "回报来自人物选择、行动、资源或关系变化，按本书题材落地，不套固定升级打脸模板。",
    }


def _character_brief(item: dict) -> str:
    name = str(item.get("name", "")).strip()
    if not name:
        return ""
    fields = []
    for key in (
        "identity",
        "occupation",
        "role",
        "description",
        "motivation",
        "character_arc",
        "relationship_with_protagonist",
        "life_profile",
    ):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            value = "、".join(str(v) for v in value[:3])
        elif isinstance(value, dict):
            value = "；".join(f"{k}:{v}" for k, v in list(value.items())[:3])
        text = str(value or "").strip()
        if text:
            fields.append(text[:140] if key == "life_profile" else text[:80])
    return f"{name}：{'；'.join(fields)[:360]}" if fields else name


def _collect_character_briefs(value, *, limit: int = 10) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def walk(item) -> None:
        if len(result) >= limit:
            return
        if isinstance(item, dict):
            brief = _character_brief(item)
            name = str(item.get("name", "")).strip()
            if brief and name and name not in seen:
                seen.add(name)
                result.append(brief)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return result



def normalize_outline_text(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_latin1_gbk_mojibake(value)
    if isinstance(value, list):
        return [normalize_outline_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_outline_text(item) for key, item in value.items()}
    return value


def _writer_quality_contract() -> str:
    min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    return f"""## 【9分神作契约】正文质量红线（必须满足，否则视为不合格）

### 【让人读下去·最高优先】（评分高 ≠ 读着好看，以下决定读者留存，违反即判"普通"）
- 【开头即刻钩子】第一句/第一段必须直接进入冲突、悬念、动作或一个让人不安的细节。**严禁环境描写开头**（雨夜/风/天气/景色）、**严禁设定解释开头**（介绍世界观/设备/体系/背景）、**严禁回顾或平淡过渡开头**（"过了几天/沈默醒来/那是一个")。理想开头：一上来就是主角正在做一件有张力的事、或撞见一个反常。承接上章时，第一句就要重新绷紧张力，而非平铺。
- 【阅读回报·每章必释放】每章必须有完整的"期待→阻碍/压迫→反转→兑现/推进"。主角必须主动做出关键判断、行动或取舍，禁止整章被动挨打、单纯逃亡、被信息轰炸。读者要感到局势被推进、真相更近、关系改变或主动权发生变化，不能只挖坑不填。
- 【代入主角】贴主角限制性视角，持续呈现主角此刻的欲望、恐惧、盘算与情绪波动，让读者代入"如果是我"。禁止上帝视角旁观式叙述把读者挡在门外。
- 【赌注真实且升级】本章赌注必须具体可感、比上章更高（生存/在乎的人/身份真相/核心目标）。空洞的全书级口号不算赌注。

- 审查目标分必须达到 {min_score:g} 分及以上；低于该分数视为不合格，必须重写。
- 字数必须达到配置要求，建议不少于 {min_words} 字，避免超过 {max_words} 字。
- 必须严格执行本章大纲的核心事件、人物、地点、危机和章末钩子，不得擅自改主线。
- 开头必须自然承接上一章结尾的人物状态、地点、时间和未解决危机，禁止生硬跳转。

### 【悬念密度·强制要求】
- 每章必须包含至少 3 个"让人无法停止阅读"的关键时刻（tension points）。
- 每 800-1200 字必须有一次有效推进：新信息曝光、冲突升级、意外转折、人物关系质变至少一项。
- 禁止连续超过 1500 字没有任何情绪转折或悬念推进的"平铺直叙"。

### 【章末钩子·强制要求】
- 每章结尾必须是以下三种强力钩子之一，且必须在最后 200 字内落地：
  1. 危机升级钩子：主角或核心人物突然陷入更大危险，生存/目标受到直接威胁。
  2. 信息反转钩子：抛出颠覆前文认知的关键信息，让读者产生"原来如此"或"竟然是这样"的震惊。
  3. 情感爆点钩子：人物关系发生剧烈撕裂或质变，让读者对角色命运产生强烈牵挂。
- 禁止以"平静收尾、总结现状、铺垫过渡"作为章末结尾。每一章结束都必须让读者产生"我必须立刻读下一章"的焦虑感。

### 【反套路·强制要求】
- 禁止套路化战斗描写：禁止"主角遇敌→分析弱点→能力爆发→战胜敌人"的标准升级流模板。
- 禁止套路化解谜描写：禁止"发现问题→查阅资料→恍然大悟→轻松解决"的流水账推演。
- 禁止用专业细节、设定解释、数据参数替代人物情感和剧情张力。专业元素必须服务于"人"的困境，不能成为叙事主体。
- 禁止配角沦为解说员或工具人。每个有台词的配角必须有自己的欲望、恐惧和秘密。

### 【爽点与反差·强制要求】（治"呆板/平淡"，读者最爱的核心爽点，违反即判平庸）
- 【爽点密度】每 2000-3000 字至少落一个明确爽点：主角反制、打脸、破局、能力/身份揭示、压制者吃瘪、信息反转至少其一。禁止整章只有铺垫和危机而没有任何兑现——那是"憋屈"不是"悬念"。
- 【反差爽＝信息差×压抑×爆发】爽点本质是认知落差：先让对手或旁人低估、轻视、挑衅主角（压抑要充分，"扮猪"要演到位、别穿帮），再在众目睽睽下干脆利落地反转打脸（爆发要狠），用围观群众的震惊反应放大爽感。反差越大越爽。
- 【每章回报闭环】每章必须有完整的"期待→阻碍/压迫→反转→兑现/推进"链条，主角主动破局而非被动挨打；胜利要落到具体的人、具体的关系/资源/真相变化上，不能只赢了场面。
- 【好打脸 vs 烂模板】可以写打脸/扮猪吃虎，但要写"好的"——压抑铺垫足、反转有因果、主角凭真实判断/能力/布局赢。被禁止的是"遇敌→分析弱点→能力爆发→秒杀"的无脑碾压流水账，不是打脸本身。

- 【伏笔回收·顿悟兑现】本章若回收前文埋下的伏笔、悬念或反常，必须写出"原来如此"的兑现冲击——具体呼应当初埋设的物件、台词、数字或反常细节，让读者产生顿悟与满足；禁止只用一句旁白交代或一笔带过。兑现缺乏冲击感等于没回收。

### 【情感冲击·强制要求】
- 心理描写必须展现"真实的恐惧、孤独、愤怒或渴望"，禁止空泛的概念分析、术语堆叠或机械化心理流水账。
- 每章至少有一个"刺点"——一个让读者心头一紧的细节：一个反常的动作、一句没说出口的话、一个突然沉默的瞬间。
- 冲突必须触及角色的核心恐惧或核心欲望，不能停留在表层利害计算。

### 【人物辨识度·强制要求】（"人物性格不突出"的直接对策，违反即判平庸）
- 每个有戏份的角色必须有可被读者记住的辨识锚点：一种说话腔调或口头禅、一个反复出现的标志动作或小动作、一种独特的判断偏好或价值取向。**遮住角色名字，仅凭台词或行动也应能认出是谁。**
- 主角必须有鲜明性格锋芒：明确的价值排序、一戳即中的软肋、一句话就能引爆的情绪点；主角的关键抉择要"只有他会这么干"，而非任意主角通用的反应。
- 同一章至少出现一次性格反差：角色在压力下做出与平时人设相反却仍合理的选择（冷静者失控、自私者冒险救人、骄傲者低头、怯懦者硬扛），让人物从扁平变立体。
- 对白要因人而异：不同角色的句长、用词、语气、信息量必须有可分辨差异，禁止所有角色都用同一种"标准书面腔"；每个配角至少有一句"只有他才说得出口"的话。
- 角色选择必须推进其性格弧线：让读者感到他比上一章更接近或更远离某个转变，而非性格原地踏步、只换事件。

### 【因果逻辑·强制要求】（"不符合逻辑"的直接对策，违反即判不合格）
- 每个转折、破局、关键推进都必须可追溯到前文可见的原因——某角色的选择、某条信息、某个物件或某个伏笔。**禁止用"恰好/刚好/凑巧/忽然"替主角解决问题**：巧合可以制造麻烦，但不能解决麻烦。
- 主角的胜利、获救、关键信息必须来自他自己前文铺垫的判断、能力、资源、人际关系或主动布局，禁止天降外援、无铺垫顿悟、临场冒出的新设定。
- 信息传递要因果闭环：角色"知道某事"必须因亲眼所见、被告知、合理推理或已建立的信息渠道；禁止角色凭空掌握他不该知道的信息。
- 动机-行为-结果要自洽：角色做什么由他此刻的欲望、处境、性格和已知信息共同决定；动机不足的行动必须补足心理或处境依据，不能"剧情需要他就去了"。
- 时间/空间/伤情/资源要连续承接：上一章的伤势、用掉的资源、所处地点、约定时间本章必须如实延续，禁止无交代的"伤忽然好了/东西又有了/突然换地方"。
- 新设定/新规则一经引入必须按其自身逻辑产生后果，不能召之即来、挥之即去。

### 【烟火气与人情味·强制要求】
- 每章至少写出一个具体生活压力或人际牵挂：饭钱、房租、病痛、工作、邻里眼光、旧恩旧怨、家人等待、同事帮衬、欠下的人情。它必须参与剧情推进，不能只是背景装饰。
- 每个核心场景至少放入一个能被读者触摸/闻到/听见的生活物件或身体细节，例如凉掉的饭、磨白的袖口、裂屏手机、楼道灯、药味、汗味、指节伤口；细节必须反映人物处境。
- 重要对白要有潜台词：人物可以嘴硬、转移话题、开玩笑、沉默或说反话，禁止把动机和情绪用说明书式台词讲透。
- 胜利、爽点或破局必须带关系回声：有人松一口气、有人更亏欠、有人被伤到、有人改变看法。没有人的反应，事件就没有温度。
- 配角不能只负责递线索、解释设定或当工具。至少一个配角要表现出自己的小算盘、小善意、小恐惧或现实难处。

### 【信息新鲜度·强制要求】
- 每章必须给读者带来至少一个"此前从未出现过的新元素"：新人物、新地点、新规则、新真相、新威胁、新情感关系。
- 禁止整章都在重复已知信息或进行无新意的铺垫。

### 【去AI味·强制要求】（去AI化的核心，违反即判AI腔）
- 【禁短句断句指纹】禁止高频使用“他。没有，哭。”“走。了。结。”“快。滚。”这类用句号硬拆主谓宾/动宾结构的伪沉重写法。短句只能在真正的惊吓、打断、失语处偶尔出现；同一页不得连续堆叠。
- 【句长起伏】长短句要服务情绪变化，而不是机械打点。用动作、对白、段落留白和具体物件制造停顿，禁止通篇句长均匀，也禁止把“1-3字短句”当固定节奏器。
- 【禁套路微表情】禁止"眼中闪过一丝XX""嘴角微微上扬""眉头微皱""心中暗道""心头涌起"等套路化微表情与内心独白。情绪必须用具体动作、对白、环境或身体反应外化（"他把杯子转了三圈" > "他心中涌起波澜"）。
- 【破折号节制】全章破折号"——"不超过3-4处，禁止把它当万能戏剧停顿；改用短句断句或动作承接。
- 【比喻节制】全章核心比喻不超过2-3个，禁止"仿佛/宛如/如同/犹如"的比喻堆叠；优先用直接动作和具象细节。
- 【删冗余副词】删去"缓缓地/微微地/默默地/淡淡地"等软调副词，让动词本身承担动作质感。
- 【展示而非告知】禁止"他感到/她意识到/他明白了"式直接告知情绪，改为可观察的动作与物象。
- 【禁议论文腔】禁止"毫无疑问/众所周知/总而言之/综上所述/不得不说/显而易见"等连接词进入小说正文。
- 【禁对称式章节结尾】禁止每章用“身后……身前……”“朝着某方向迈步”“一步，又一步”“未知的路”这类对称锚点句式收尾。章末钩子必须来自具体现场：一个动作没完成、一句话没说完、一件物品暴露、一个人做出反常反应。
- 【禁概念堆叠】修炼/规则/科技/神秘体系不得只写“某某之核、共鸣、境界、法则、经脉胀痛”。每个抽象概念变化都必须落到至少一个可见后果：身体代价、器物变化、他人误解、环境损伤、生活成本或关系裂痕。
- 【禁打卡地图】新地点不能只是“拿道具/升境界/过副本”的站点。第一次进入地点必须写出当地风土人情、制度规则、生计结构或普通人的日常压力；地图切换必须有因果、代价和不可逆状态变化。
- 【禁散文诗复沓】禁止整章用同一种段落起手、同一种物象聚焦句、同一句作者判断反复变奏。最多允许一处有意识的回环；超过两次就必须打散。小说章节首先要让事件在时间里推进，而不是把一个瞬间切成十几片。
- 【不可逆一步】每章必须让主角或关键人物完成一个可被复述的不可逆动作：签下、撕毁、交出、藏起、公开、背叛、救下、放弃、承认、误伤或暴露。不能只反复强调“他往前挪了一步”，必须让读者看见他具体做了什么、因此失去了什么或改变了谁。
- 【重奏到独步】可以用群像/多声部制造压力，但中后段必须收束到主角自己的判断和行动：让读者能复述“众人如何把路铺到这里，主角又独自迈出了哪一步”。禁止整章只有街坊、配角、制度在推动，主角只是被押着看见。
- 【信息链清晰】证据、药包、账页、信物、钥匙、录音等关键物必须有可追踪路径：谁拿到、如何转交、收信人凭什么理解、风险在哪里、最后落到谁手里。禁止“证据自然传出去/大家都懂了”的跳步。
- 【反派找台阶】反派不能只“脸色一变/恼羞成怒”。当权力露出破绽时，必须让反派立刻寻找程序、威胁、交易、沉默、嫁祸或规则解释来稳住场面；这样张力比单纯发怒更强。
- 【反派意象反照】若反派有标志物（灯笼、戒指、刀、伞、手套、烟、车钥匙等），它不能只当随身道具；至少一次让该物照出/映出/碰到反派不愿面对的旧事、软肋、年轻时选择或当前破绽。读者要从物象看见反派的内层，而不是只看到“平静/变脸”两种状态。
- 【反派裂隙半拍】当旧签押、旧证词、旧物证或熟人指认逼到反派时，必须给一个极短的身体裂隙：目光落下又移开、指节发白、手套按住旧疤、灯笼柄轻响、喉咙动一下、张口又咽回去、呼吸停半拍。随后再让他找台阶稳住场面。禁止只写“猛地站起/脸色一变”。
- 【跨空间咬合】多地点并行时，要用声音、光、脚步、物件到达、传话延迟、时辰变化等桥接时间，避免从A地突然跳到B地让读者脑补整夜发生了什么。
- 【关键道具预埋】章末要使用的关键物、墨印、录音、拓片、钥匙、药包、证词等，必须在前文有一次可见的制作、藏匿、转手或检查动作。可以保留悬念，但不能让道具像作者临时需要才冒出来。
- 【拓印/复制动作】若结尾出现墨印、拓片、录音备份、账页副本等“复制型证据”，前文必须写出复制动作本身：蘸墨、按压、晾干、夹入账页、换袋、藏入夹层等至少一笔。只写“后来拿出墨印”不合格。
- 【情感线回扣】本章前文出现的亲缘旧痕、父亲字迹、旧称呼、手把手教过的动作等情感线索，章末关键动作时必须有一个极小回响：字形重叠、手势重复、旧物触感返回、称呼卡在喉口。不要用解释性心理总结。
- 【结尾余韵反应】章末关键动作落地后，不要立刻截断；必须给1-2个微小现场反应作为余韵：对手的手停住、旁人倒吸气、同伴松开账本、灯光落偏、某个物件发出声音。反应要短，不能写成解释性总结。
- 【禁作者旁注】禁止“读者能看见/这一章往前挪/权力最怕的是/真正让本章立住的是”等评论式句子。把判断交给动作、物件、对白和他人反应，不要替读者写读后感。

### 【人物称呼一致性·强制要求】（world_consistency 主因，违反即判设定矛盾）
- **全书出场角色已在前期锁定于 characters.json（闭环角色）**。正文只能使用其中已登记的角色；严禁临时生造新的命名角色。若剧情确实需要新角色，必须先在 characters.json 登记（name+aliases+role）再使用——这是防止人物漂移的根本约束。
- 每个角色有唯一规范名（见角色信息）。正文优先用规范名指代。
- 若用职务/称谓/别名称呼（如"周社长""老林"），首次出现必须与规范名绑定（如"周德茂——当年报社的周社长"），此后同一角色不得再冒出未绑定的新称呼。
- 严禁让两个不同角色共用同一称呼或姓氏+职务组合；严禁同一角色在不同章节换用互不关联的名字。
- 本章大纲 characters_involved 中列出的角色，必须在本章实际登场且其规范名（或已绑定称呼）至少出现一次，不得只提其名却不出场、或出场却换了陌生称呼。

### 【基础禁令】
- 不得用设定解释替代剧情现场；世界观信息必须通过行动、对话、发现或冲突呈现。
- 人物动机和说话方式必须符合既有设定，不能OOC。
- 爽点必须来自主角判断、能力、资源或协作的实际发挥，不能靠巧合硬赢。
- 不得水文、重复段落、空泛心理独白、元叙述或输出“本章完”等非正文信息。"""


def _deai_rewrite_directives(detection: dict) -> str:
    """根据 C1 检测结果生成针对性去AI味指令。无问题返回空串。"""
    issues = detection.get("issues", []) if isinstance(detection, dict) else []
    if not issues:
        return ""
    parts = ["## 【去AI味专项重写】（本次重写的首要目标，优先于其他修改）"]
    by_type = {}
    for it in issues:
        by_type.setdefault(it.get("type"), it)
    for t, it in by_type.items():
        loc = it.get("paragraph", "")
        loc_str = f"（定位：{loc}）" if loc and loc != "未定位" else ""
        if t == "parallel_sentiment":
            parts.append(f"- 打破排比工整{loc_str}：把'不仅…而且…更…'改为长短句交替，"
                         "用一个具体动作或物象替代抒情（'他把杯子转了三圈' > '他心中涌起波澜'）")
        elif t == "summary_ending":
            parts.append("- 章末禁止总结性收尾：最后一句必须是未解决的问题、突然的威胁或未说出口的话，"
                         "禁止'故事才刚刚开始'式元叙述")
        elif t == "short_sentence_fragmentation":
            parts.append("- 删除短句断句指纹：不要再写'他。没有。哭。'这类句号硬拆；把情绪改成动作、沉默、对白错位或物件反应")
        elif t == "symmetric_anchor_ending":
            parts.append("- 重写章末最后300字：禁止'身后困境/身前方向/一步又一步'对称模板，改成具体现场钩子")
        elif t == "abstract_concept_pileup":
            parts.append("- 把抽象概念落地：减少'道意/本源/法则/共鸣/境界'堆叠，改写为身体代价、器物变化、旁人反应或现实成本")
        elif t == "formal_refrain_stagnation":
            parts.append("- 打散重复段式：删除同一开头/同一物象聚焦句的连续变奏，改成按时间推进的行动链")
        elif t == "repeated_authorial_judgment":
            parts.append("- 删除重复作者判断句：不要反复告诉读者'这不是升级秘境/这是一条窄路'，改用一次不可逆动作证明")
        elif t == "authorial_aside":
            parts.append("- 删除作者旁注式总结：不要写'读者能看见/这一章往前挪/权力最怕的是'，改为动作、证物转移、对白潜台词和旁人反应")
        elif t == "static_lyrical_scene":
            parts.append("- 静态散文诗化过重：增加目标受阻、人物行动、关系反应、信息变化和不可逆后果")
        elif t == "adjective_pileup":
            parts.append("- 删除'璀璨/磅礴/恐怖'等空泛形容词，每个形容词替换为具体感官细节"
                         "（'空气冷得像含着铁片' > '空气无比冰冷'）")
        elif t == "low_variance":
            parts.append("- 段落长度必须有变化：穿插1-2句短段落制造停顿，再用长段落铺陈")
        elif t == "telling_not_showing":
            parts.append("- 用动作和物象替代'他感到/他意识到'式直接告知情绪")
        elif t == "meta_narration":
            parts.append("- 删除一切生成痕迹/元叙述词")
        elif t == "simile_overuse":
            parts.append("- 精简比喻：全章核心比喻不超过2-3个，删去'仿佛/宛如/如同'的堆叠，"
                         "改用直接动作与具象细节（'风把门摔上' > '风仿佛一只无形的手'）")
        elif t == "micro_expression_cliche":
            parts.append("- 删除套路化微表情/内心独白（'眼中闪过一丝/嘴角微微上扬/心中暗道'），"
                         "用具体动作、对白或环境外化情绪，禁止用'闪过一丝XX'交代心理")
        elif t == "soft_adverb_overuse":
            parts.append("- 删去冗余软调副词'缓缓/微微/默默/淡淡'，让动词本身承担质感")
        elif t == "reflexive_cliche":
            parts.append("- 减少'不由得/忍不住/情不自禁'，直接写动作，省略心理过渡")
        elif t == "essay_connective":
            parts.append("- 删除议论文连接词'毫无疑问/众所周知/总而言之/不得不说'，让叙事本身推进")
        elif t == "temporal_filler":
            parts.append("- 减少'顿时/霎时间/刹那间'，用动作节奏本身制造紧迫感")
        elif t == "dash_overuse":
            parts.append("- 大幅减少破折号'——'，每章不超过3-4处；改用短句断句或动作承接")
        elif t == "sentence_uniformity":
            parts.append("- 句子长短太均匀：调整段落和动作节奏，但禁止高频1-3字断句，避免形成AI短句指纹")
    return "\n".join(parts)


def _load_9star_examples(chapter_number: int, min_score: float = 9.0, max_examples: int = 2) -> str:
    """加载此前评分 >= min_score 的章节作为9分范本参考。"""
    rd = review_dir(NOVELS_DIR)
    if not rd.exists():
        return ""
    examples = []
    for f in sorted(rd.glob("chapter_*_review.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            score = float(data.get("overall_score", 0))
        except Exception as exc:
            continue
        if score < min_score:
            continue
        m = re.search(r"chapter_(\d+)_review", f.name)
        if not m:
            continue
        ex_num = int(m.group(1))
        if ex_num >= chapter_number:
            continue
        final_file = NOVELS_DIR / "chapters" / "final" / f"chapter_{ex_num:04d}.txt"
        draft_file = NOVELS_DIR / "chapters" / "draft" / f"chapter_{ex_num:04d}.txt"
        src = final_file if final_file.exists() else draft_file
        if not src.exists():
            continue
        text = src.read_text("utf-8")
        head = text[:1200]
        tail = text[-600:] if len(text) > 1800 else ""
        snippet = head + ("\n...\n" if tail else "") + tail
        examples.append((ex_num, score, snippet))
        if len(examples) >= max_examples:
            break
    if not examples:
        return ""
    parts = ["## 9分神作范本参考（仅学习其节奏、悬念与情感写法，不得抄袭剧情）\n"]
    for num, score, snippet in examples:
        parts.append(f"### 第{num}章（评分 {score} 分）节选\n{snippet}\n")
    return "\n".join(parts)


def _load_feedback_as_review(feedback_file: Path, chapter_number: int) -> dict:
    try:
        payload = load_json(feedback_file)
    except Exception as exc:
        return {}
    item = payload.get(str(chapter_number), payload) if isinstance(payload, dict) else {}
    if not isinstance(item, dict):
        return {}
    reviews = item.get("reviews", [])
    analysis = item.get("failure_analysis", {})
    selected: dict = {}
    candidates: list[tuple[int, float, dict]] = []
    if isinstance(reviews, list):
        for index, review in enumerate(reviews):
            if not isinstance(review, dict):
                continue
            status = str(review.get("status", "")).strip().lower()
            if status and status != "completed":
                continue
            try:
                score = float(review.get("overall_score"))
            except (TypeError, ValueError):
                continue
            candidates.append((index, score, review))
    if candidates:
        selected = max(candidates, key=lambda item: (item[1], item[0]))[2]
    elif isinstance(reviews, list):
        selected = next((review for review in reversed(reviews) if isinstance(review, dict)), {})
    if not isinstance(analysis, dict):
        analysis = {}
    def first_list(value):
        return value[:1] if isinstance(value, list) else []

    def compressed_suggestions() -> list[str]:
        repairs = [
            str(v).strip()
            for v in analysis.get("targeted_repairs", [])
            if str(v).strip()
        ][:3]
        if len(repairs) > 1:
            joined = "；".join(f"{idx}. {text}" for idx, text in enumerate(repairs, 1))
            return [f"本轮重写必须同时完成这些硬修复项：{joined}。"]
        if repairs:
            return repairs
        return first_list(analysis.get("adjustments", selected.get("suggestions", [])))

    return {
        "status": "completed",
        "verdict": "需重写",
        "overall_score": analysis.get("best_score", selected.get("overall_score", 0)),
        "strengths": first_list(selected.get("strengths", [])),
        "weaknesses": first_list(analysis.get("likely_reasons", selected.get("weaknesses", []))),
        "suggestions": compressed_suggestions(),
        "continuity_issues": first_list(selected.get("continuity_issues", [])),
        "summary": selected.get("summary", ""),
        "edits": first_list(selected.get("edits", [])),
        "local_analysis": selected.get("local_analysis", {}),
        "raw_response": selected.get("raw_response", ""),
        "candidate_file": selected.get("candidate_file", ""),
    }


def _feedback_base_text_path(review_data: dict, chapter_number: int) -> Path:
    official = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    candidate_value = str(review_data.get("candidate_file", "") or "").strip()
    if not candidate_value:
        return official
    try:
        candidate = Path(candidate_value).resolve()
        project = NOVELS_DIR.resolve()
        if candidate.is_file() and candidate.is_relative_to(project):
            return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    return official


def _auto_compress(content: str, chapter_number: int, max_words: int, target_min: int, chapter_outline: dict) -> str:
    """如果章节超过 max_words，自动调用压缩 agent 精简内容。"""
    word_count = len(content)
    if word_count <= max_words:
        return content

    key_events = chapter_outline.get("key_events", [])
    if isinstance(key_events, str):
        key_events = [key_events]
    key_events_text = "\n".join(f"{i+1}. {str(ev)}" for i, ev in enumerate(key_events) if str(ev).strip())
    chapter_hook = str(chapter_outline.get("chapter_hook", "")).strip()

    compress_system = """你是一位资深小说编辑，专门负责将超过字数限制的章节压缩到合格范围。
你的任务是删减冗余，保留精华。禁止改变核心剧情、禁止删除关键事件、禁止削弱章末钩子。
你尤其擅长：删除重复描写、压缩过长的测试/列举/解释段落、把学术论文式对话改写成紧张的对峙。"""

    target = min(max(target_min + 1000, 7000), max_words - 500)
    for attempt in range(3):
        current_count = len(content)
        if current_count <= max_words:
            break

        compress_prompt = f"""以下第{chapter_number}章字数过多（{current_count}字），需要压缩到 {target} 字左右（绝对不要超过 {max_words} 字）。

## 本章必须保留的关键事件
{key_events_text}

## 本章章末钩子（最后200字必须保留）
{chapter_hook}

## 压缩原则
1. 保留所有关键事件和情绪转折点，不能省略大纲规定的任何事件。
2. 删除重复的心理描写、重复的环境渲染、重复的身体感受。
3. 对于AI测试/挑战/列举类场景，最多展示3-4个具体例子，其余用"后面还有数十道类似的题目"等方式概括。
4. 删除大段技术参数、算法解释、设定说明，只保留对情节至关重要的信息。
5. 保留所有对话中的潜台词和情感张力，但删除解释性插话。
6. 章末钩子最后200字必须完整保留，不能削弱。
7. 压缩后仍然是流畅的小说正文，不是大纲或摘要。

请直接输出压缩后的正文，不要输出任何解释、分析或元信息：

{content}"""

        log(f"[Writer] 第{chapter_number}章字数过多（{current_count}字），启动第{attempt+1}次压缩...")
        compressed = call_mmx(compress_system, compress_prompt, max_tokens=8192, temperature=0.3)
        if compressed:
            compressed = compressed.strip()
            if compressed.startswith("```"):
                lines = compressed.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                compressed = "\n".join(lines).strip()
            if len(compressed) >= target_min and len(compressed) <= max_words:
                log(f"[Writer] 第{chapter_number}章压缩成功（{len(compressed)}字）")
                return compressed
            if len(compressed) < len(content):
                content = compressed
                log(f"[Writer] 第{chapter_number}章压缩后仍为{len(content)}字，继续压缩...")
            else:
                log(f"[Writer] 第{chapter_number}章压缩未减少字数，停止压缩")
                break
        else:
            log(f"[Writer] 第{chapter_number}章压缩调用失败")
            break

    return content


def _apply_incremental_edits(
    chapter_number: int,
    old_text: str,
    review_data: dict,
    min_words: int,
    max_words: int,
) -> str | None:
    """尝试用模型输出 diff ops 并 apply 到旧文本。失败返回 None，由调用方回退全文重写。"""
    system, prompt = build_edit_prompt(old_text, review_data, chapter_number, min_words, max_words)
    log(f"[Writer] 第{chapter_number}章尝试增量编辑...")
    start_time = time.time()
    try:
        raw = call_mmx(system, prompt, max_tokens=8192, temperature=0.3)
    except Exception as e:
        log(f"[Writer] 增量编辑调用失败: {e}")
        return None
    elapsed = time.time() - start_time
    log(f"[Writer] 增量编辑 API 调用耗时 {elapsed:.1f}s")
    if not raw:
        log("[Writer] 增量编辑返回空")
        return None

    try:
        edits = parse_edit_ops(raw)
    except EditApplyError as e:
        log(f"[Writer] 增量编辑解析失败: {e}")
        return None
    edits = edits[:1]

    if not edits:
        log("[Writer] 模型未输出有效 edits，回退到全文重写")
        return None

    log(f"[Writer] 获得 {len(edits)} 条 edit ops: {[e.get('type') for e in edits]}")
    try:
        new_text, applied_log = apply_text_edits(old_text, edits)
    except EditApplyError as e:
        log(f"[Writer] 增量编辑 apply 失败: {e}")
        return None

    preserved_ratio = similarity(old_text, new_text)
    log(f"[Writer] 增量编辑后文本相似度: {preserved_ratio:.2%}")

    # 如果 edits 很多且文本变化过大，说明模型没有遵守局部修改，回退
    if preserved_ratio < 0.45 and len(edits) <= 2:
        log("[Writer] 增量编辑后文本变化过大，疑似全文重写，回退")
        return None

    # 字数校验
    word_count = len(new_text)
    if word_count < min_words:
        log(f"[Writer] 增量编辑后字数不足 ({word_count} < {min_words})，回退")
        return None
    if word_count > max_words:
        log(f"[Writer] 增量编辑后字数超出 ({word_count} > {max_words})，尝试压缩")
        # 简单截断到最大字数附近（这里只做一个安全网，压缩逻辑后续可接入 _auto_compress）
        new_text = new_text[:max_words]
        # 找到最后一个完整句子
        for end in (".", "!", "?", "。", "！", "？", "；"):
            idx = new_text.rfind(end)
            if idx > max_words * 0.85:
                new_text = new_text[: idx + 1]
                break

    log(f"[Writer] 第{chapter_number}章增量编辑成功，{len(old_text)} -> {len(new_text)} 字")
    return new_text


def _deepen_rewrite(
    chapter_number: int,
    first_pass: str,
    chapter_outline: dict,
    world: dict,
    ai_flavor_detection: dict,
) -> str:
    """重写轮轻量二轮深化：局部补感官物象、潜台词、余韵，禁止改主线。

    受 similarity >= 0.70 约束，不达标则返回 first_pass（丢弃深化结果）。
    """
    scenes = chapter_outline.get("scenes") if isinstance(chapter_outline, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return first_pass
    palette = (world.get("quality_bible") or {}).get("sensory_palette") if isinstance(world, dict) else None
    human_anchor = str(chapter_outline.get("human_anchor", "")).strip() if isinstance(chapter_outline, dict) else ""

    scenes_text = "\n".join(
        f"- 场景{s.get('position', '')}：物象={s.get('sensory_anchor', '')}；潜台词={s.get('subtext_beat', '')}；出口={s.get('exit_hook', '')}"
        for s in scenes if isinstance(s, dict)
    )
    palette_text = json.dumps(palette, ensure_ascii=False) if isinstance(palette, dict) else "（quality_bible.sensory_palette 缺失，自行选用室内/室外/身体/物件/声音气味物象）"
    ai_issues = ai_flavor_detection.get("issues", []) if isinstance(ai_flavor_detection, dict) else []
    ai_hint = ""
    if ai_issues:
        ai_hint = "\n## 本地AI味检测指出的缺失（针对性补齐）\n" + "\n".join(
            f"- {it.get('type', '')}: {it.get('paragraph', '')}" for it in ai_issues[:4] if isinstance(it, dict)
        )

    system = (
        "你是一位资深小说编辑，只做局部深化，绝不改主线、不换场景、不删事件。"
        "你的任务是把已有正文在感官、潜台词和余韵上补厚，让它更像9分神作。"
    )
    prompt = f"""以下第{chapter_number}章正文已通过第一轮重写，请在**不改变主线、场景、事件和人物动作**的前提下，做局部深化：

## 本章场景蓝图（每个场景补一个可触摸物象 + 一句潜台词 + 出口余韵）
{scenes_text}

## 感官物象分类库（物象从对应类别取材）
{palette_text}

## 本章烟火气锚点
{human_anchor or "（无）"}
{ai_hint}

## 深化规则（严格遵守）
1. 为每个场景补一个可触摸物象（气味、声音、身体细节或旧物），必须反映人物处境，不得堆砌风景。
2. 把至少一句对白改成潜台词（嘴硬、转移话题、欲言又止、说反话），禁止把动机说透。
3. 在不可逆动作之后加1-2个现场反应作为余韵（对手停顿、旁人吸气、灯光偏移、物件声响）。
4. 针对 AI 味检测指出的缺失类别定点补齐。
5. **禁止改主线、禁止换场景、禁止删事件、禁止大段重写**。改动总量不得超过全文30%。
6. 保持字数在原有 ±10% 范围内。

直接输出深化后的完整正文，不要任何解释：

{first_pass}"""

    log(f"[Writer] 第{chapter_number}章重写轮启动二轮深化...")
    start = time.time()
    deepened = call_mmx(system, prompt, max_tokens=4096, temperature=0.4)
    elapsed = time.time() - start
    if not deepened or not deepened.strip():
        log(f"[Writer] 第{chapter_number}章二轮深化返回空，保留第一轮（耗时{elapsed:.1f}s）")
        return first_pass
    deepened = deepened.strip()
    if deepened.startswith("```"):
        lines = deepened.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        deepened = "\n".join(lines).strip()
    sim = similarity(first_pass, deepened)
    log(f"[Writer] 第{chapter_number}章二轮深化相似度 {sim:.2%}（耗时{elapsed:.1f}s）")
    if sim < 0.70:
        log(f"[Writer] 第{chapter_number}章二轮深化相似度过低({sim:.2%}<70%)，丢弃保留第一轮")
        return first_pass
    return deepened


def generate_chapter(
    chapter_number: int,
    retry: int = 0,
    output_file: str | Path | None = None,
    review_feedback: str | Path | None = None,
) -> str:
    chapter_file = Path(output_file) if output_file else CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    review_file = Path(review_feedback) if review_feedback else review_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}_review.json"
    review_data = None
    is_rewrite = False
    if review_file.exists():
        try:
            if review_feedback:
                review_data = _load_feedback_as_review(review_file, chapter_number)
            else:
                review_data = load_json(review_file)
            status = review_data.get("status", "")
            verdict = review_data.get("verdict", "")
            score = review_data.get("overall_score", 10)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            min_review_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
            if status != "completed" or verdict in {"需重写", "需修改"} or score < min_review_score:
                is_rewrite = True
                log(f"[Writer] 第{chapter_number}章检测到未通过审查报告（status:{status}，评分{score}，verdict:{verdict}），将基于建议重写")
        except Exception as exc:
            pass

    exists, existing_words, existing_ok = read_text_length(chapter_file)
    if exists and existing_ok and not is_rewrite:
        log(f"[Writer] 第{chapter_number}章已存在且字数合格（{existing_words}字），跳过")
        return "exists"
    if exists and is_rewrite:
        log(f"[Writer] 第{chapter_number}章已有初稿但审查未通过，基于审查意见重新生成")
    elif exists:
        log(f"[Writer] 第{chapter_number}章已存在但字数不合格（{existing_words}字），重新生成")

    world = load_json(WORLD_FILE)
    characters = load_json(CHARACTERS_FILE)

    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))

    chapter_outline = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number))

    if not chapter_outline:
        log(f"[Writer] 第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number - 1)).get("summary", "")
    next_summary = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number + 1)).get("summary", "")

    prev_ending = ""
    if chapter_number > 1:
        prev_file = CHAPTERS_DIR / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    # 人物状态追踪：注入上一章结束时的角色状态快照，消除跨章矛盾
    character_state_directive = ""
    try:
        from core.character_state import format_state_for_prompt, latest_state_before
        prev_states = latest_state_before(NOVELS_DIR, chapter_number)
        character_state_directive = format_state_for_prompt(prev_states)
    except Exception as _cse:
        log(f"[Writer] 角色状态加载异常（忽略）: {_cse}")

    # 人物成长弧线追踪：注入主角当前弧线阶段，确保成长不倒退
    arc_state_directive = ""
    try:
        from core.arc_state import format_arc_for_prompt, latest_arc_before
        prev_arc = latest_arc_before(NOVELS_DIR, chapter_number)
        arc_state_directive = format_arc_for_prompt(prev_arc)
    except Exception as _ase:
        log(f"[Writer] 弧线状态加载异常（忽略）: {_ase}")

    # 关系欠账追踪：注入上一章结束时仍在发酵的人情债、误会、承诺和未说出口的话
    relationship_state_directive = ""
    relationship_obligation_directive = ""
    try:
        from core.relationship_state import (
            format_relationships_for_prompt,
            latest_relationships_before,
            relationship_obligation_for_prompt,
        )
        prev_relationships = latest_relationships_before(NOVELS_DIR, chapter_number)
        relationship_state_directive = format_relationships_for_prompt(prev_relationships)
        relationship_obligation_directive = relationship_obligation_for_prompt(prev_relationships)
    except Exception as _rse:
        log(f"[Writer] 关系欠账加载异常（忽略）: {_rse}")

    # 伏笔闭环：注入"本章应回收"的前期伏笔，强制在正文兑现 payoff（治伏笔悬空根因）
    foreshadowing_directive = ""
    try:
        from core.foreshadowing_ledger import rebuild_from_outlines, dangling_threads
        _ledger = rebuild_from_outlines(NOVELS_DIR)
        _due = [
            t for t in dangling_threads(_ledger, as_of_chapter=chapter_number)
            if int(t.get("must_resolve_by", 0) or 0) <= chapter_number
            and int(t.get("planted_at", 0) or 0) < chapter_number
        ]
        if _due:
            _lines = ["## 【本章必须回收的伏笔】（前期埋下、已到回收点，必须在正文兑现 payoff，不得继续悬空）"]
            for t in _due[:6]:
                _setup = str(t.get("setup", "")).strip().replace("\n", " ")[:120]
                _lines.append(f"- 第{t.get('planted_at')}章埋设：{_setup}")
            _lines.append(
                "请在正文自然兑现这些伏笔的 payoff（揭示/呼应/反转），让读者感到「原来如此」且符合前期铺垫；"
                "若无法在本章全部回收，至少兑现最关键的1-2条，其余明确为后续回收，绝不能无声丢弃。"
            )
            foreshadowing_directive = "\n".join(_lines) + "\n"
    except Exception as _fse:
        log(f"[Writer] 伏笔台账加载异常（忽略）: {_fse}")

    review_section = ""
    if is_rewrite and review_data:
        def one(value):
            if isinstance(value, list):
                return value[:1]
            return [value] if str(value or "").strip() else []

        suggestions = one(review_data.get("suggestions", []))
        continuity_issues = one(review_data.get("continuity_issues", []))
        strengths = one(review_data.get("strengths", []))
        weaknesses = one(review_data.get("weaknesses", []))
        raw_response = str(review_data.get("raw_response", "") or "").strip()

        review_section = "\n\n## 编辑审查反馈（本轮只处理以下唯一建议）\n"

        if strengths:
            review_section += "\n### 原文优点（请保留）\n"
            for s in strengths:
                review_section += f"- {s}\n"

        if weaknesses:
            review_section += "\n### 原文不足（请改进）\n"
            for w in weaknesses:
                review_section += f"- {w}\n"

        if suggestions:
            review_section += "\n### 具体修改建议（本轮只落实这一条）\n"
            for s in suggestions:
                review_section += f"- {s}\n"

        if continuity_issues and continuity_issues[0] != "与前文不一致之处（如有）":
            review_section += "\n### 连续性问题（必须修正）\n"
            for c in continuity_issues:
                if c and c != "与前文不一致之处（如有）":
                    review_section += f"- {c}\n"

        if raw_response and not (suggestions or continuity_issues or weaknesses):
            review_section += "\n### 原始审查反馈（解析失败时也必须参考）\n"
            review_section += raw_response[:3000] + "\n"

        old_file = _feedback_base_text_path(review_data, chapter_number)
        old_content = ""
        if old_file.exists():
            with open(old_file, "r", encoding="utf-8") as f:
                old_content = f.read()
            review_section += f"\n### 原文参考（前800字）\n{old_content[:800]}\n...\n"

        # === 方案B：增量编辑优先 ===
        # 如果 reviewer 给出了 edits，先尝试定点修改；失败再回退全文重写
        edits = review_data.get("edits") if isinstance(review_data, dict) else None
        if old_content and edits:
            incremental_text = _apply_incremental_edits(
                chapter_number, old_content, review_data, min_words, max_words
            )
            if incremental_text is not None:
                # 清理禁用短语并保存
                for phrase in FORBIDDEN_PHRASES:
                    incremental_text = incremental_text.replace(phrase, "")
                # 处理截断
                if incremental_text and not incremental_text.endswith(VALID_ENDINGS):
                    paragraphs = incremental_text.split("\n\n")
                    if len(paragraphs) > 1 and not paragraphs[-1].strip().endswith(VALID_ENDINGS):
                        incremental_text = "\n\n".join(paragraphs[:-1]).strip()
                    if incremental_text and not incremental_text.endswith(VALID_ENDINGS):
                        last_valid = max(
                            (incremental_text.rfind(end) for end in VALID_ENDINGS if end in incremental_text),
                            default=-1,
                        )
                        if last_valid > len(incremental_text) * 0.9:
                            incremental_text = incremental_text[: last_valid + 1].strip()

                # 去AI味复检：若重写后 ai_flavor 反而下降且原文不算太差，回退保留原文
                if old_content:
                    try:
                        old_detect = detect_ai_flavor(old_content, project=NOVELS_DIR)
                        new_detect = detect_ai_flavor(incremental_text, project=NOVELS_DIR)
                        if (new_detect["ai_flavor_score"] < old_detect["ai_flavor_score"]
                                and old_detect["ai_flavor_score"] >= 6.0):
                            log(f"[Writer] 第{chapter_number}章去AI味复检：重写后 "
                                f"{new_detect['ai_flavor_score']} < 原文 {old_detect['ai_flavor_score']}，回退保留原文")
                            incremental_text = old_content
                    except Exception as _de:
                        log(f"[Writer] 去AI味复检异常（忽略）: {_de}")

                word_count = len(incremental_text)
                CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
                with open(chapter_file, "w", encoding="utf-8") as f:
                    f.write(incremental_text)
                log(f"[Writer] 第{chapter_number}章通过增量编辑保存（{word_count}字）")
                return "success"
            log(f"[Writer] 第{chapter_number}章增量编辑失败，回退到全文重写")

    # 提取主角名和故事设定
    # 优先从 characters.json 的 protagonist.name 读取，其次从 premise.txt 提取
    protagonist_name = "主角"
    protagonist_data = characters.get("protagonist", {})
    if isinstance(protagonist_data, dict) and protagonist_data.get("name"):
        protagonist_name = protagonist_data["name"]
    else:
        premise_file = NOVELS_DIR / "premise.txt"
        if premise_file.exists():
            premise_text = premise_file.read_text(encoding="utf-8")[:2000]
            import re
            m = re.search(r"主角(\S+?)(?:本|是|穿越|重生|携带|得到|拥有|来到|乃|为)", premise_text)
            if m:
                protagonist_name = m.group(1)

    world_desc = world.get("world_description", "")[:300]
    power_system = world.get("power_system", {})
    power_system_desc = power_system.get("description", "")[:200]

    key_characters = _collect_character_briefs(characters, limit=10)
    if not key_characters:
        for f in world.get("factions", [])[:3]:
            fname = f.get("name", "")
            fdesc = f.get("description", "")
            if fname and fdesc:
                key_characters.append(f"{fname}：{fdesc[:80]}")

    key_chars_text = "\n".join(f"- {kc}" for kc in key_characters) if key_characters else "（暂无详细角色设定）"

    # G8: 注入语言指纹（文风锚定，防AI塑料感和风格漂移）
    lang_fp = characters.get("language_fingerprint", {})
    lang_fp_text = ""
    if lang_fp:
        parts = []
        if lang_fp.get("prose_style"):
            parts.append(f"文风基调：{lang_fp['prose_style']}")
        if lang_fp.get("signature_metaphors"):
            parts.append(f"标志性意象：{'、'.join(lang_fp['signature_metaphors'])}")
        if lang_fp.get("forbidden_expressions"):
            parts.append(f"避免表达：{'、'.join(lang_fp['forbidden_expressions'])}")
        if parts:
            lang_fp_text = "## 【语言指纹】（本章必须严格遵循以下文风，不得漂移）\n" + "\n".join(f"- {p}" for p in parts)

    system_label = "核心规则/能力体系" if power_system_desc else "核心规则/现实约束"
    story_context = f"""主角：{protagonist_name}
世界观：{world_desc[:200]}
{system_label}：{power_system_desc[:150] or "按世界观、题材和人物关系推进，不强加修炼或升级体系。"}
关键势力/角色：
{key_chars_text}"""

    # 9分范本参考：注入此前高分章节作为风格锚定（限制1篇避免 prompt 膨胀）
    examples_section = _load_9star_examples(chapter_number, max_examples=1)

    genre = _infer_genre(world)
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")
    target_min = max(min_words + 500, 5500)

    # 从大纲提取关键事件和章末钩子，强制 writer 按节点执行
    key_events = chapter_outline.get("key_events", [])
    if isinstance(key_events, str):
        key_events = [key_events]
    key_events_text = "\n".join(f"{i+1}. {str(ev)}" for i, ev in enumerate(key_events) if str(ev).strip())
    chapter_hook = str(chapter_outline.get("chapter_hook", "")).strip()
    emotional_arc = str(chapter_outline.get("emotional_arc", "")).strip()
    tension_points = chapter_outline.get("tension_points", [])
    if isinstance(tension_points, str):
        tension_points = [tension_points]
    tension_text = "\n".join(f"- {str(tp)}" for tp in tension_points if str(tp).strip())

    # 救猫咪结构 + 网文增强维度：目标赌注/爽点链/结构功能/对抗力量。
    # 旧大纲可能缺这些字段，用空字符串兜底，正文 prompt 中对空值做条件渲染。
    chapter_goal = str(chapter_outline.get("chapter_goal", "")).strip()
    payoff_design = str(chapter_outline.get("payoff_design", "")).strip()
    human_anchor = str(chapter_outline.get("human_anchor", "")).strip()
    story_beat = str(chapter_outline.get("story_beat", "")).strip()
    main_antagonist = str(chapter_outline.get("main_antagonist", "")).strip()
    content_layers = chapter_outline.get("content_layers", [])
    if isinstance(content_layers, str):
        content_layers = [content_layers]
    content_layers_text = "\n".join(
        f"  - {str(item).strip()}"
        for item in content_layers
        if str(item).strip()
    )

    # 结构功能执行指令：把大纲的结构维度翻译成 Writer 必须落实的写作要求。
    # 旧大纲缺这些字段时整段省略，不影响正文生成（向后兼容）。
    beat_guidance = {
        "catalyst": "本章是催化事件：必须打破主角的日常，制造一个不可忽视的起点危机。",
        "midpoint": "本章是中点：必须让赌注升级，主角从被动应对转为主动出击，常伴随重大信息揭示。",
        "all_is_lost": "本章是谷底：主角必须跌入最低点，关键损失/背叛/失败发生，制造全章压抑。",
        "finale": "本章是高潮：主角用此前积累的认知与实力兑现主线承诺，解决核心冲突。",
        "dark_night": "本章是灵魂暗夜：主角在谷底反思，必须展现真实内心挣扎，为顿悟铺垫。",
    }
    beat_hint = beat_guidance.get(story_beat, "")
    structure_parts = []
    if beat_hint:
        structure_parts.append(f"- 【结构定位·{story_beat}】{beat_hint}")
    if chapter_goal:
        structure_parts.append(f"- 【章节目标与赌注】{chapter_goal}（必须在正文中落实目标的推进或受挫，让读者感受到赌注的分量）")
    payoff_profile = _payoff_profile(world, chapter_outline)
    if payoff_design:
        structure_parts.append(
            f"- 【{payoff_profile['label']}】{payoff_design}"
            f"（按「{payoff_profile['chain']}」落地；{payoff_profile['directive']}）"
        )
    if human_anchor:
        structure_parts.append(
            f"- 【烟火气锚点】{human_anchor}"
            "（必须写进正文现场：用生活压力、关系牵挂、潜台词或具体物件推动剧情，不得只在心理旁白里解释）"
        )
    if content_layers_text:
        structure_parts.append(
            "- 【内容层次】正文必须逐层兑现以下设计，不能只写单线事件：\n"
            f"{content_layers_text}\n"
            "  每一层至少用一个现场动作、对话潜台词、具体物件、制度压力或关系结果落地。"
        )
    if main_antagonist:
        structure_parts.append(f"- 【主要对抗】{main_antagonist}（必须塑造对抗力量的具体威胁，让读者感受到压力，而非抽象的「敌人」）")
    structure_directive = "\n".join(structure_parts) if structure_parts else "（本章大纲未提供结构功能/目标赌注/爽点链字段，按既有大纲执行即可）"
    fact_directive = build_origin_fact_directive(ORIGIN_MATERIALS, limit=8)

    if is_rewrite:
        system = (f"""你是一位追求9分神作的顶尖中文网络小说作家，同时也是一位冷酷的资深编辑。
你现在需要对一篇接近9分但未达标的章节进行**局部精修**，而不是推倒重来。
你擅长创作{genre}，但你的标准不是"合格"，而是"惊艳"。

## 精修原则（必须遵守）
1. **保留优点，只改问题**：原文中 reviewer 没有批评的部分（特别是高张力的对话、成功的悬念场景、有效的刺点）必须保留，不得因为重写而丢失。
2. **只落实本轮唯一修改建议**：只针对 reviewer 给出的唯一 weakness/suggestion/edit 做局部修改，修改后在心中自检"这一条建议是否已被解决"。
3. **禁止为改而改**：不要改变原文中本已有效的部分，不要为了"创新"而破坏原有节奏。
4. **局部手术，整体保留**：大多数问题只需要修改几个段落、几句对话或一个场景结尾，不需要全文重写。
5. **修正连续性问题**
6. 正文字数必须不少于{min_words}字，建议写到{target_min}-8000字，严格不得超过{max_words}字
7. 保持角色性格一致性
8. 主角是{protagonist_name}，请参考故事设定保持角色一致性
9. 核心目标：精修后的章节必须让第一次读的人产生"我必须立刻知道下一章发生了什么"的冲动

## 精修操作指南
- 如果 reviewer 说"某段落太长"，直接删减该段落至合适长度，不要重写其他部分。
- 如果 reviewer 说"某角色符号化"，给该角色增加一个动作、一句潜台词或一个反常反应，不要重写整个场景。
- 如果 reviewer 说"章末钩子弱"，只修改最后200字，保留前文。
- 如果 reviewer 说"技术细节过多"，删除技术解释，替换为人物反应。
- 如果 reviewer 说"中段缺乏小高潮"，在中段插入一个短小的冲突或转折场景，不要打乱整体结构。"""
        + "\n\n"
        + _deai_rewrite_directives(
            (review_data.get("local_analysis", {}) or {}).get("ai_flavor_detection", {})
            if isinstance(review_data, dict) else {}
        )
        )
    else:
        system = f"""你是一位追求9分神作的顶尖中文网络小说作家，擅长创作{genre}。
你的标准不是"写出一章合格的内容"，而是"写出一章让人欲罢不能的神作"。
你的文笔锋利如刀，对话充满潜台词，场景描写让人身临其境，节奏像过山车一样让人喘不过气。
你痛恨套路，痛恨水文，痛恨用技术细节或设定解释来填充篇幅。
你相信真正的好小说每一章都必须回答一个问题："读者为什么必须继续读下去？"
正文字数必须不少于{min_words}字，建议写到{target_min}-8000字；低于{min_words}字会被系统拒绝。
注意保持角色性格一致性，前后情节衔接自然。
但"衔接自然"不等于"平淡过渡"——衔接处也要有张力、有悬念、有未知。
主角是{protagonist_name}，请参考故事设定保持角色一致性。"""

    prompt = f"""请根据以下信息，写出第{chapter_number}章《{chapter_outline.get('title', '未命名')}》的完整内容。

## 🚨 正文质量红线（系统会自动检测，违反直接判不合格；交稿前必须逐条自检）
- 【禁短句断句AI指纹】禁止用句号硬拆主谓宾/动宾来制造伪沉重——如“他。没有。哭。”“走。了。结。”“快。滚。”这类一律禁止。短句只能用于真正的惊吓、打断、失语瞬间，且同一页不得连续堆叠1-3字短句；长短句必须自然起伏，靠动作、对白、物件反应、段落留白制造停顿，而不是靠句号机械打点。
- 【角色必须到齐】本章大纲 characters_involved 里列出的**每一个角色**（包括主角之外的任何配角），都必须在正文里实际登场——规范名或已绑定称呼至少出现一次，且有动作/对白/反应，不得只挂个名字、不得遗漏任何一个。交稿前把名单逐个核对一遍。
- 【技术/专业机制要可感，而非符号】“禁止专业细节堆砌”禁的是**脱离人物处境的设定说明书**（大段参数、背景解释）；但当某个技术/专业机制本身就是本章的核心赌注或临场危机时（如一次入侵、一个报错、一笔转账、一段代码、一次对齐检测），**必须写出它的可感细节**（端口号、协议特征、数据包大小、报错码、进度条、手感和阻力、身体的紧张反应），让读者“摸得到”，不能停在“同一漏洞”“被攻破了”这类概念/符号层面。
- 【关系任务必须兑现】若本章被注入了【关系任务/关系义务/关系状态】清单，**必须逐条在正文兑现**：任务对象角色要充分出场并有实质互动，关系压力（亏欠/误解/裂痕/试探）必须在本章推进或解决，且正文要自然回声任务里的关键词与触发点；只提一次名字或不推进关系，会被本地关系门拦截判不合格。
- 【输出前最终自检】通读全章：①有没有句号硬拆的短句指纹？有就合并或改成动作/沉默/对白错位；②characters_involved 里每个角色是否都已实际出场？漏的补一段其在场反应；③本章核心危机若依赖某个技术机制，是否已写出可感细节而非符号？④有没有脱离人物处境的设定堆砌？有就换成处境与压力；⑤若有关系任务清单，是否每条都已兑现（对象出场+压力推进+关键词回声）？

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

{fact_directive}
## 本章大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 本章【必须严格执行的关键事件清单】
以下是大纲中规定的关键事件，你必须按顺序、按时间点全部写入正文，不能遗漏、不能跳过、不能过度发挥成无关内容：
{key_events_text}

## 本章【情绪曲线】
{emotional_arc}

## 本章【张力节点】
{tension_text}

## 本章【章末钩子——最后200字必须落在这里】
{chapter_hook}

## 本章【结构功能 / 目标赌注 / 爽点链 / 对抗力量】
{structure_directive}
## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}
{character_state_directive}
{arc_state_directive}
{relationship_state_directive}
{relationship_obligation_directive}
{lang_fp_text}
{foreshadowing_directive}
## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 故事设定
{story_context}

{_writer_quality_contract()}

{examples_section}

## 写作要求（9分神作执行清单）
1. 【字数红线】本章必须不少于{min_words}字，**严格不得超过{max_words}字**。建议写到{target_min}-8000字。超过{max_words}字视为不合格，必须删减。低于{min_words}字不得结束，必须继续补充。
2. 【关键事件红线】必须严格按照上方【必须严格执行的关键事件清单】逐条写入正文，不能遗漏、不能跳过、不能替换时间点。每个关键事件都必须有主角的现场参与和情感反应。
3. 【专业细节禁令】禁止大段参数、数值、算法、术语、设定或背景解释；任何专业信息都只能服务于人物处境和剧情压力。读者要的是"{protagonist_name}此刻必须作出什么选择"，不是概念说明书。
4. 【开头】必须自然衔接前一章，但衔接的第一句话就要有张力——禁止以"过了几天""{protagonist_name}醒来"等平淡方式开头。理想开头：直接切入一个正在进行的动作、一个突然发生的事件、或一个让人不安的细节
5. 【对话】要符合角色性格，推动情节发展，且每段重要对话必须包含至少一层潜台词（言外之意）。禁止长篇解释性对话，禁止配角当解说员
6. 【场景】描写要生动，但重点不是"画面感"，而是"氛围感"——让读者感到压抑、紧迫、诡异或震撼，而不是"看清了这个地方长什么样"
7. 【冲突】必须触及角色核心恐惧或核心欲望，不能停留在表层利害计算。冲突的输赢不重要，重要的是冲突过程中暴露了什么秘密、改变了什么关系
8. 【心理】描写要展现真实的情感波动（恐惧、愤怒、孤独、渴望、自我怀疑），严禁术语化、分析化或机械化的心理流水账。主角首先是具体的人——他会害怕、会冲动、会后悔，也会为了目标付出代价
9. 【感官冲击】必须有至少一个"主角本人直面威胁"的近距离刺点，不能所有危险都发生在监控屏幕或远处。
10. 【阅读回报】必须来自主角在极端压力下的判断、抉择、行动或牺牲，不能靠巧合、升级或突然觉醒硬赢。最顶级的回报是"主角明知道会付出代价，还是做了最正确的选择"
11. 【配角】要有各自的欲望、恐惧和秘密，不是纯背景板。即使是只出现一次的龙套，也要让读者感觉到"这个人有自己的故事"
12. 【内容层次】必须兑现大纲的 content_layers；外部事件之外，还要让关系、生活压力、秘密代价、制度规则或世界细节至少一层真正改变人物处境
13. 【叙事推进】禁止把一个瞬间写成十几段意象变奏。每3-5段必须发生新的行动、阻碍、选择、反应或信息变化；本章至少完成一个不可逆动作，让读者能说清“{protagonist_name}这一章具体往前挪了哪一步”
14. 【从重奏到独步】若本章有群像/多线协作，前中段可以写众人如何递火、藏证、遮掩或施压；中后段必须落回{protagonist_name}的独立判断和动作，让他亲手承担一次不可逆后果，不能只让配角把路铺完。
15. 【证据链/信息链】关键物与关键信息必须写清“起点→转交→误读或风险→抵达→被使用”。每次转交至少有一个具体动作或接收者反应，禁止用一句“消息传开了/证据送到了”跳过。
16. 【反派应对】反派被逼出破绽后，必须马上有更冷的应对：搬出规矩、扣字眼、拖程序、找替罪羊、威胁旁人或提出交易。不要只写“脸色一变/怒了”。
17. 【反派意象反照】若反派有标志物或反复出现的随身物，至少让它在一处反照反派内层：照到旧签押、裂口漏光落在证物上、手套遮住疤、戒指敲到旧案文书等。物象必须揭示他害怕、亏欠或试图掩盖的东西。
18. 【反派裂隙半拍】旧证据逼到反派时，写一个半拍身体反应再写他的冷处理；可用喉咙动、张口又咽回、呼吸停拍、指节发白等，让他像被旧案缠住的人，不只是压迫符号。
19. 【关键道具预埋】章末要使用的关键道具/证据，前文必须有制作、拓印、藏入、转手或被人看见的一笔；结尾出现时读者应能回想起“原来她刚才留了这一手”。
20. 【复制证据制作】墨印/拓片/副本/录音备份必须写出制作动作，哪怕只是一句“她用裂碗底蘸墨，在矿牌边缘按了一下”。
21. 【情感线回扣】前文亲缘旧痕或父辈线索，章末关键动作要用字形、手势、触感或旧称呼回扣一次，让情感线闭合，不要只写事件推进。
22. 【转场咬合】跨地点或跨时间转场必须有桥：脚步声、灯火、钟声、传话延迟、物件到达、天气后果、伤口变化等。读者不能被迫脑补关键行动链。
23. 【结尾余韵】最后关键动作之后必须留1-2个短反应承接力量：反派如何停顿/找台阶，旁人如何吸气或退半步，同伴的手如何松开或攥紧，标志物的光如何偏移。禁止用作者总结替代余韵。
24. 如果 origin/ 中存在素材，必须参考其中的原始设定、人物关系、历史事件、语气风格和限制，不能与其冲突；若存在 origin/facts，正文必须至少让 1 条事实线索变成现场中的人物、地点、旧事、物件或关系动作
25. 【节奏】禁止流水账。每800-1200字必须有一次有效推进。章节中段必须有一个"小高潮"或"小反转"，不能把所有爆点都堆在结尾
26. 【信息】每章必须给读者带来至少一个"此前从未出现过的新元素"，禁止整章重复已知信息
27. 不要输出章节标题，直接从正文开始
28. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

## 最终硬性要求
输出正文必须不少于{min_words}字，严格不得超过{max_words}字。生成结束前请自行检查篇幅：
- 如果低于{min_words}字，必须继续补充符合大纲的行动、冲突、对话和场景细节。
- 如果超过{max_words}字，必须删减冗余描写、重复叙述和技术解释，保留核心事件和情感张力。
生成结束前，请额外自检以下5个问题并确保答案为"是"：
- 本章最后200字是否精准落在上方【章末钩子】上，且让人心跳加速？
- 本章是否包含上方【关键事件清单】中的每一个事件？
- 本章是否至少有一个"主角本人直面威胁"的近距离刺点？
- 本章是否至少有三个有效的情绪转折（参考上方【情绪曲线】）？
- 本章是否有一个不可逆动作，而不是只靠反复意象和作者判断制造重量？
- 本章是否把群像压力收束成{protagonist_name}的一次独立行动？
- 本章关键证据/信息的传递链是否清楚，没有跳步？
- 章末关键道具是否在前文预埋过，且最后动作后是否有现场反应形成余韵？
- 反派标志物是否至少一次反照出他的内层或破绽，而不只是随身道具？
- 旧证据逼到反派时，是否有半拍身体裂隙再接冷处理？
- 亲缘/父辈/旧痕线索是否在章末关键动作中有微小回扣？
- 如果我是第一次读这本书的读者，读完这章后会不会立刻想打开下一章？

请开始写作："""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    start_time = time.time()
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)
    elapsed = time.time() - start_time
    log(f"[Writer] 第{chapter_number}章生成 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[Writer] 第{chapter_number}章收到空响应")
        max_retry = CONFIG["writer"].get("max_retries", 5)
        if retry < max_retry:
            log(f"[Writer] 第{chapter_number}章生成失败，重试({retry+1}/{max_retry})...")
            time.sleep(CONFIG["writer"]["retry_delay"])
            return generate_chapter(chapter_number, retry + 1, output_file, review_feedback)
        log(f"[Writer] 第{chapter_number}章生成失败，已达最大重试次数")
        return "failed"

    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 清理禁用短语
    for phrase in FORBIDDEN_PHRASES:
        content = content.replace(phrase, "")

    # 处理截断：如果文本没有以有效标点结尾，移除最后一个不完整的段落/句子
    if content and not content.endswith(VALID_ENDINGS):
        # 尝试找到最后一个完整段落结尾
        paragraphs = content.split("\n\n")
        if len(paragraphs) > 1 and not paragraphs[-1].strip().endswith(VALID_ENDINGS):
            content = "\n\n".join(paragraphs[:-1]).strip()
        # 如果还是不完整，找到最后一个有效标点位置
        if content and not content.endswith(VALID_ENDINGS):
            last_valid = max(
                (content.rfind(end) for end in VALID_ENDINGS if end in content),
                default=-1,
            )
            if last_valid > len(content) * 0.9:  # 只截掉最后不到10%的不完整内容
                content = content[: last_valid + 1].strip()

    # 自动压缩：如果超过 max_words，调用压缩 agent
    content = _auto_compress(content, chapter_number, max_words, target_min, chapter_outline)

    # 重写轮轻量二轮深化（仅在重写路径且配置开启时）
    deepen_enabled = bool(CONFIG.get("writer", {}).get("deepen_rewrite", True))
    if is_rewrite and deepen_enabled and content:
        try:
            ai_detection = {}
            if isinstance(review_data, dict):
                ai_detection = (review_data.get("local_analysis") or {}).get("ai_flavor_detection") or {}
            content = _deepen_rewrite(chapter_number, content, chapter_outline, world, ai_detection)
            # 深化后可能再次超长，复用压缩
            content = _auto_compress(content, chapter_number, max_words, target_min, chapter_outline)
        except Exception as _de:
            log(f"[Writer] 第{chapter_number}章二轮深化异常（保留第一轮）: {_de}")

    word_count = len(content)
    if not is_valid_chapter_text(content):
        log(f"[Writer] 第{chapter_number}章字数异常（{word_count}字），但仍保存")
    else:
        log(f"[Writer] 第{chapter_number}章字数合格（{word_count}字）")

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(chapter_file, "w", encoding="utf-8") as f:
        f.write(content)

    log(f"[Writer] 第{chapter_number}章已保存 -> {chapter_file}")

    # 伏笔回收回验：本章大纲声称回收(claimed)的伏笔，正文是否真的兑现。
    # 通过 bigram 重叠粗判，通过则台账升 resolved，否则降回 planted 并打 unresolved_in_text。
    # 这是治"大纲写了[收]但正文没写"假回收的关键，纯本地计算不耗 LLM。
    try:
        from core.foreshadowing_ledger import load_ledger, save_ledger, verify_resolution_in_text
        _ledger = load_ledger(NOVELS_DIR)
        result = verify_resolution_in_text(_ledger, chapter_number, content)
        if result["changed"]:
            save_ledger(NOVELS_DIR, _ledger)
            if result["verified"]:
                log(f"[Writer] 第{chapter_number}章伏笔回验通过：{result['verified']}")
            if result["failed"]:
                log(
                    f"[Writer] ⚠️ 第{chapter_number}章伏笔回验失败（正文未兑现）：{result['failed']}，"
                    "台账已降回 planted 并标记 unresolved_in_text"
                )
    except Exception as _fre:
        log(f"[Writer] 伏笔回收回验异常（忽略）: {_fre}")

    return "success"


def main():
    global CANDIDATE_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只生成某一章")
    parser.add_argument("--output-file", type=str, default="", help="候选模式：写入指定初稿文件，而非正式 draft 目录")
    parser.add_argument("--review-feedback", type=str, default="", help="重写时读取汇总审查反馈")
    args = parser.parse_args()
    CANDIDATE_MODE = bool(args.output_file)

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Writer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    failed_chapters = []
    if args.chapter > 0:
        result = generate_chapter(args.chapter, output_file=args.output_file or None, review_feedback=args.review_feedback or None)
        if result == "failed":
            failed_chapters.append(args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = generate_chapter(ch)
            if result == "failed":
                log(f"[Writer] 第{ch}章生成失败，记录并继续")
                failed_chapters.append(ch)
            time.sleep(2)

    if failed_chapters:
        log(f"[Writer] 以下章节生成失败: {failed_chapters}")
        sys.exit(1)
    else:
        log("[Writer] 全部完成")


if __name__ == "__main__":
    main()
