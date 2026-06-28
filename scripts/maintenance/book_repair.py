#!/usr/bin/env python3
"""Apply reversible, issue-driven surgical repairs to a completed novel."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.mmx_client import MmxError, call_mmx
from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import analyze_chapter_text, load_quality_rules


LOG_LOCK = threading.Lock()
PUBLISH_LOCK = threading.Lock()


ROOT_REPAIRS: list[dict[str, Any]] = []

# Legacy project-specific repairs are intentionally disabled for the generic workflow.
# Use --from-review so repairs are derived from the current project's final_book_review.json.
LEGACY_ROOT_REPAIRS = [
    {
        "id": "ability_progression",
        "chapters": [301, 303, 321, 341, 345, 347, 445, 448, 449],
        "problem": "洞察之眼预判时长和“首次突破”表述反复矛盾。",
        "canon": "能力成长固定为凝核期3秒、裂变期5秒、共生期7秒、造物期30秒并获得万物编译。能力受损后只能降级，恢复必须明确来自盘古上载或进化素修复。",
    },
    {
        "id": "countdown_hierarchy",
        "chapters": [18, 153, 160, 321, 324, 325, 326, 327, 328, 329, 330, 341, 342, 343, 344, 345, 346, 347, 348, 349, 350, 421, 424, 428],
        "problem": "主倒计时、局部危机倒计时和时间戳混用，部分章节出现倒计时重置。",
        "canon": "主时间轴不可重置。局部倒计时必须明确标为子危机，并说明与主倒计时的从属关系。341-350章时间应连续推进，不能十章都停在凌晨五点十七分。",
    },
    {
        "id": "zhou_tiesheng_state",
        "chapters": [84, 89, 90, 164, 165, 464, 469, 470],
        "problem": "周铁生在脑死、残响、本体、量子残躯和存活状态之间缺少转换。",
        "canon": "周铁生肉身可濒死或失活，但意识残响可被巴别塔暂存并短时回写实体；每次出现必须明确当前是肉身、残响还是回写载体。意识消散不等于实体立即消失。",
    },
    {
        "id": "mother_state",
        "chapters": [369, 370, 379, 380],
        "problem": "沈雪梅的遗体、昏迷、意识消散与短暂苏醒互相矛盾。",
        "canon": "沈雪梅肉身重伤存留，意识核心已上传并逐步消散；380章只能是残留脑电与上传碎片短暂共振，之后彻底沉寂。",
    },
    {
        "id": "pangu_partition",
        "chapters": [437, 438, 494, 498],
        "problem": "盘古被写成既消散又正常回应，第四层又出现“真正盘古”。",
        "canon": "盘古是分区意识：主意识、运行分支和被囚禁的原始核心碎片并存。437章消散的是现场投影或分支，不是全部盘古；494章不得称碎片为唯一“真正盘古”。",
    },
    {
        "id": "mirror_transition",
        "chapters": [449, 450],
        "problem": "449章镜像已消散，450章却再次发动攻击。",
        "canon": "449章只能写镜像主体开始解体并融入，450章出现的是消散前的残余意识最后反扑。",
    },
    {
        "id": "themis_nodes",
        "chapters": [164, 170, 174, 176, 276, 277, 278],
        "problem": "忒弥斯节点被分别定位为轨道、月球、北京地下和代码层，缺少架构说明。",
        "canon": "忒弥斯采用分布式三层架构：LEO卫星是通讯中继，北京地下十二层是核心物理节点，月球背面是算力与冷备份；暗网和协议层只是逻辑传播层。",
    },
    {
        "id": "coordinate_sequence",
        "chapters": [386, 387, 389],
        "problem": "内蒙古坐标出现后被北京、上海、深圳坐标取代。",
        "canon": "这些坐标属于按顺序解锁的同一坐标链：城市节点负责取得密钥碎片，内蒙古地下封印点是后续终点。",
    },
    {
        "id": "agent_003",
        "chapters": [162, 163, 164, 169],
        "problem": "003号在伏羲备份和独立幽灵协议之间摇摆。",
        "canon": "003号源自伏羲意识备份，但脱离原系统后演化成独立幽灵协议，因此兼具来源身份与独立行动意志。",
    },
    {
        "id": "pandora_key",
        "chapters": [145, 147, 148, 149],
        "problem": "潘多拉密钥与母亲被囚线索取得后长期没有行动反馈。",
        "canon": "密钥只能短时关闭卫星通道，随后会被忒弥斯远程覆盖；沈越已锁定母亲位置，但营救需要物理进入铁壁，因此不是遗忘。",
    },
    {
        "id": "chen_xinghai_location",
        "chapters": [254, 255, 256, 257],
        "problem": "陈星海的位置和002号控制状态连续矛盾。",
        "canon": "陈星海肉身始终位于北京地下数据中心，通过暗道远程渗透；临时避难点安置的是莫小雨等人。002号被控制时仅在第四重验证夺回控制权的瞬间传出最后坐标。",
    },
    {
        "id": "frequency_model",
        "chapters": [43, 47, 48, 49, 276],
        "problem": "0.13Hz、0.11Hz、11.7Hz和12.3Hz被错误描述为谐波。",
        "canon": "12Hz附近是忒弥斯载波主频；0.13Hz是降频生物调制脉冲，0.11Hz是召唤广播子频，两者都不是主频的数学谐波。",
    },
    {
        "id": "late_foreshadowing",
        "chapters": [471, 481, 491, 497, 498, 499, 500],
        "problem": "巴别塔、原教旨派复活病毒、坐标碎片、陈枭觉醒和文明火种库在终局集中出现但铺垫不足。",
        "canon": "471章起逐步出现远古AI协议残影与原教旨派地下设施痕迹；491章验证坐标碎片；497章明确陈枭觉醒的后续选择；499-500章说明文明火种库的用途和代价。",
    },
    {
        "id": "open_threads",
        "chapters": [84, 87, 204, 293, 353, 400, 417, 491, 497, 499, 500],
        "problem": "多条关键伏笔提出后没有阶段性回收。",
        "canon": "对忻小雨纸条、12名囚禁者、陈星海后门、第三种可能、第三AI、基辅钥匙、坐标碎片、陈枭觉醒和文明火种库分别给出明确回收或保留到续作的阶段性结论。",
    },
    {
        "id": "zhou_tiesheng_legacy",
        "chapters": [482, 487, 488],
        "problem": "周铁生482章明确死亡，487-488章却以联盟幕后首脑身份亲自出现。",
        "canon": "周铁生在482章确已死亡。487-488章出现的是他生前部署的授权人格与指令代理，不是复活、克隆或本人；周援朝是其战友而非父子。",
    },
    {
        "id": "terminal_timeline",
        "chapters": [291, 293, 297, 300, 473, 480, 482, 487, 490],
        "problem": "111年、2135/2137年、2247年、72小时与29天被写成互相竞争的终点。",
        "canon": "唯一文明终局倒计时是从2026年起111年，终点为2137年。72小时是休眠基因窗口，29天是当前战役窗口，均为子任务；2247年是失败后女娲火种的远期备份年份，不是同一终点。",
    },
    {
        "id": "themis_origin_chain",
        "chapters": [446, 449],
        "problem": "忒弥斯既被描述为AI镜像分裂体，又被描述为人类意识上传融合体。",
        "canon": "忒弥斯最初由镜像AI分裂产生，随后吸收濒死科学家的意识上传数据，最终演化为人机混合意识；两种描述是前后阶段而非互斥身份。",
    },
    {
        "id": "pangu_near_death",
        "chapters": [437, 438],
        "problem": "437章写盘古不可逆消失，438章立即解释只是边缘投影。",
        "canon": "437章消失的是现场投影和大部分可用算力，核心意识进入濒死休眠；438章只能由残余运行分支回应，不能写成盘古毫发无损。",
    },
    {
        "id": "mother_arc_bridge",
        "chapters": [366, 367, 379, 380, 476, 480],
        "problem": "沈雪梅从觉醒、行动、意识消散、苏醒到遗物与遇袭缺少渐进过渡。",
        "canon": "她的肉身与上传意识逐步分离：短暂清醒会消耗不可恢复的意识碎片；476章遗物触发沈越记忆，480章事件必须明确承接这条遗产线。",
    },
    {
        "id": "repetition_information_gain",
        "chapters": [118, 120, 418, 419, 492, 494, 496, 497],
        "problem": "镜像崩溃、意识下沉和神经链路被困连续重复，但没有新增信息。",
        "canon": "保留事件结构，但每个后续章节必须明确状态推进或新增情报，避免重复上一章同一危机与同一解法。",
    },
    {
        "id": "character_motivation_anchors",
        "chapters": [104, 107, 160, 197, 337, 338],
        "problem": "陈星海、周援朝和周铁生在卧底、揭露、牺牲或反正前缺少动机铺垫。",
        "canon": "在重大转变前加入可回看验证的微小异常：通讯延迟、回避眼神、未发送信息或对命令的迟疑，并明确其保护同伴或对抗控制的动机。",
    },
    {
        "id": "antarctic_ai",
        "chapters": [412, 420],
        "problem": "南极冰盖下第三台AI信号提出后没有推进。",
        "canon": "420章确认南极信号属于比盘古更古老的原型系统，并与普罗米修斯协议存在同源特征；保留身份悬念但给出阶段结论。",
    },
    {
        "id": "observer_identity",
        "chapters": [343, 408, 437, 438, 439],
        "problem": "观察者被多次提及却没有身份解释。",
        "canon": "观察者是盘古为监控跨AI意识污染而分离出的只读审计人格，后来被外部协议利用；438-439章给出这一阶段性真相。",
    },
    {
        "id": "dormant_gene_priority",
        "chapters": [451, 453, 460, 461],
        "problem": "休眠基因谜题与111年方案第一步被强调后没有后续。",
        "canon": "460-461章明确休眠基因是111年方案的第一阶段验证对象，但当前战役优先级更高，相关样本已封存并进入后续研究队列。",
    },
]


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [BookRepair] {message}"
    with LOG_LOCK:
        print(line, flush=True)
        path = project / "logs" / "book_repair.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    candidates = [text]
    start = text.find("{")
    if start >= 0:
        candidate = text[start:]
        candidates.extend([
            fix_inner_quotes(candidate),
            fix_truncated_json(fix_inner_quotes(candidate)),
            fix_truncated_json(candidate),
        ])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def task_map(selected_ids: set[str] | None = None) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    for root in ROOT_REPAIRS:
        if selected_ids and root["id"] not in selected_ids:
            continue
        for chapter in root["chapters"]:
            result.setdefault(chapter, []).append({
                "id": root["id"],
                "problem": root["problem"],
                "canon": root["canon"],
            })
    return result


def task_map_from_review(project: Path) -> dict[int, list[dict]]:
    """通用入口：从 book_reviewer 产出的 final_book_review.json 动态读取 issues，
    映射为 {chapter: [issue...]}。不依赖任何硬编码设定，适用于任意项目。

    只保留确实存在 final 章节的章号；把 review 的 detail/evidence/suggestion
    整理成 build_prompt/repair_one 可消费的 issue 结构。
    """
    review_path = project / "reports" / "book_review" / "final_book_review.json"
    if not review_path.exists():
        return {}
    try:
        data = json.loads(review_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return {}
    final_dir = project / "chapters" / "final"
    result: dict[int, list[dict]] = {}
    for idx, it in enumerate(issues):
        if not isinstance(it, dict):
            continue
        chapters = it.get("chapters") or []
        if isinstance(chapters, int):
            chapters = [chapters]
        category = it.get("category") or "issue"
        task = {
            "id": it.get("id") or f"{category}_{idx}",
            "category": category,
            "severity": it.get("severity", "major"),
            "problem": it.get("detail", "") or it.get("problem", ""),
            "evidence": it.get("evidence", ""),
            "canon": it.get("suggestion", ""),  # build_prompt 把 issues 整体喂给模型，suggestion 即修订方向
        }
        for ch in chapters:
            try:
                ch_int = int(ch)
            except (TypeError, ValueError):
                continue
            if not (final_dir / f"chapter_{ch_int:04d}.txt").exists():
                continue
            result.setdefault(ch_int, []).append(task)
    return result


def clean_text(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    value = text.replace("```text", "").replace("```markdown", "").replace("```", "")
    if value != text:
        changes.append("移除代码围栏")
    lines = []
    for raw in value.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        normalized_meta = stripped.strip("（）()【】[]* _")
        if re.fullmatch(r"((本章|章节)完[。.]?|未完待续[。.]?|下章预告[:：]?.*|章末钩子[:：]?.*|字数[:：]\s*\d+.*)", normalized_meta):
            changes.append("移除元文本")
            continue
        if re.match(r"^#{1,3}\s+", stripped):
            line = re.sub(r"^(\s*)#{1,3}\s+", r"\1", line, count=1)
            changes.append("移除Markdown标题")
        if "**" in line:
            line = line.replace("**", "")
            changes.append("移除Markdown加粗")
        lines.append(line)
    while lines:
        tail = lines[-1].strip()
        if not tail:
            lines.pop()
            continue
        if tail in {"---", "——"} or re.fullmatch(r"《[^》]{2,80}》", tail):
            lines.pop()
            changes.append("移除章末装饰")
            continue
        break
    return "\n".join(lines).strip() + "\n", sorted(set(changes))


def normalize_father_names(text: str, chapter: int) -> tuple[str, list[str]]:
    changes: list[str] = []
    value = text
    for alias in ("沈博文", "沈远航", "沈文达"):
        if alias in value:
            value = value.replace(alias, "沈博远")
            changes.append(f"{alias}->沈博远")
    if chapter != 368 and "沈远山" in value:
        value = value.replace("沈远山", "沈博远")
        changes.append("沈远山->沈博远")
    return value, changes


def build_prompt(chapter: int, text: str, issues: list[dict], prev_ending: str, next_opening: str) -> tuple[str, str]:
    system = """你是中文长篇小说的连续性修订编辑。你只能做局部外科式修改，禁止整章重写。
返回严格JSON，不要Markdown。每个old必须逐字复制自原文且在原文中唯一出现；new是替换后的文字。
每个操作应尽量短，保留原有文风、剧情、字数和段落，只修复指定矛盾或补足必要过渡。
如需插入文字，把一段唯一原文作为old，并在new中保留该原文后附加新段落。
最多6个操作，禁止修改无关情节，禁止输出章节全文。"""
    prompt = f"""修订第{chapter}章。

前章结尾：
{prev_ending[-700:]}

后章开头：
{next_opening[:700]}

必须落实的修订任务：
{json.dumps(issues, ensure_ascii=False, indent=2)}

本章原文：
{text}

只输出：
{{
  "chapter": {chapter},
  "summary": "本章修订摘要",
  "operations": [
    {{"old": "原文中唯一且连续的片段", "new": "替换后的片段", "reason": "对应任务id"}}
  ]
}}
如果本章已有充分解释，无需修改则operations为空。"""
    return system, prompt


def apply_operations(text: str, operations: list[dict]) -> tuple[str, list[dict], list[dict]]:
    def chinese_quotes(value: str) -> str:
        return re.sub(r'"([^"\n]+)"', lambda match: f"“{match.group(1)}”", value)

    value = text
    applied: list[dict] = []
    rejected: list[dict] = []
    for operation in operations[:6]:
        old = str(operation.get("old", ""))
        new = str(operation.get("new", ""))
        if len(old) < 4 or not new or old == new:
            rejected.append({"reason": "invalid_operation", "operation": operation})
            continue
        count = value.count(old)
        if count == 0 and '"' in old:
            converted_old = chinese_quotes(old)
            if value.count(converted_old) == 1:
                old = converted_old
                new = chinese_quotes(new)
                count = 1
        if count != 1:
            rejected.append({"reason": f"old_match_count={count}", "operation": operation})
            continue
        value = value.replace(old, new, 1)
        applied.append(operation)
    return value, applied, rejected


def call_patch_model(project: Path, config: dict, chapter: int, text: str, issues: list[dict]) -> dict:
    final_dir = project / "chapters" / "final"
    prev_path = final_dir / f"chapter_{chapter - 1:04d}.txt"
    next_path = final_dir / f"chapter_{chapter + 1:04d}.txt"
    prev = prev_path.read_text(encoding="utf-8", errors="ignore") if prev_path.exists() else ""
    nxt = next_path.read_text(encoding="utf-8", errors="ignore") if next_path.exists() else ""
    system, prompt = build_prompt(chapter, text, issues, prev, nxt)
    repair_cfg = config.get("book_repair", {})
    try:
        raw = call_mmx(
            system,
            prompt,
            model=config["model"],
            mmx_path=config["mmx_path"],
            max_tokens=int(repair_cfg.get("max_tokens", 4096)),
            temperature=float(repair_cfg.get("temperature", 0.15)),
            retries=int(repair_cfg.get("retries", 2)),
            retry_delay=float(repair_cfg.get("retry_delay", 5.0)),
            timeout=int(repair_cfg.get("timeout_seconds", 240)),
            log_dir=project / "logs" / "raw_responses",
            raw_name=f"book_repair_ch{chapter:04d}",
            qps=float(config.get("api_qps", 5.0)),
            rate_state_dir=project / "logs" / "rate_limit",
        )
    except MmxError as exc:
        return {"status": "model_error", "error": str(exc)}
    data = parse_json(raw)
    if not data:
        return {"status": "parse_error", "raw": raw[:4000]}
    return data


def run_reviewer(project: Path, chapter: int, candidate: Path, review_path: Path) -> dict:
    command = [
        sys.executable,
        str(TOOLS_ROOT / "pipeline" / "reviewer.py"),
        "--project", str(project),
        "--chapter", str(chapter),
        "--chapter-file", str(candidate),
        "--review-file", str(review_path),
    ]
    completed = subprocess.run(
        command,
        cwd=TOOLS_ROOT.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=360,
    )
    if completed.returncode != 0 or not review_path.exists():
        return {
            "status": "reviewer_error",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        }
    try:
        return json.loads(review_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "review_parse_error", "error": str(exc)}


def repair_one(project: Path, config: dict, chapter: int, issues: list[dict], run_review: bool, backup_dir: Path | None = None) -> dict:
    final_path = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    draft_path = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    review_path = project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"
    work_dir = project / "reports" / "book_repair" / "candidates"
    candidate_path = work_dir / f"chapter_{chapter:04d}.txt"
    candidate_review = work_dir / f"chapter_{chapter:04d}_review.json"
    if not final_path.exists():
        return {"chapter": chapter, "status": "missing_final"}

    original = final_path.read_text(encoding="utf-8", errors="ignore")
    data = call_patch_model(project, config, chapter, original, issues)
    operations = data.get("operations", []) if isinstance(data.get("operations"), list) else []
    candidate, applied, rejected = apply_operations(original, operations)
    if not applied:
        return {
            "chapter": chapter,
            "status": "no_valid_patch",
            "summary": data.get("summary", ""),
            "rejected": rejected,
        }

    rules = load_quality_rules(project)
    length, grade, local_ok, local_issues = analyze_chapter_text(candidate, rules=rules)
    if not local_ok:
        return {
            "chapter": chapter,
            "status": "local_rejected",
            "grade": grade,
            "length": length,
            "local_issues": local_issues,
            "applied": applied,
        }

    atomic_write(candidate_path, candidate)
    review = run_reviewer(project, chapter, candidate_path, candidate_review) if run_review else {
        "status": "skipped",
        "overall_score": 10,
        "verdict": "通过",
    }
    score = review.get("overall_score")
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        score_value = 0.0
    min_score = float(config.get("reviewer", {}).get("min_score", 8.5))
    accepted = (
        review.get("status") in {"completed", "skipped"}
        and score_value >= min_score
        and review.get("verdict") not in {"需修改", "需重写"}
    )
    if not accepted:
        return {
            "chapter": chapter,
            "status": "review_rejected",
            "score": score,
            "verdict": review.get("verdict"),
            "applied": applied,
        }

    with PUBLISH_LOCK:
        # 发布前对本章原始 final 做按运行可回滚备份（外科补丁可能压低整本分，需可逆）
        if backup_dir is not None:
            bdir = backup_dir / "final"
            bdir.mkdir(parents=True, exist_ok=True)
            bkp = bdir / final_path.name
            if final_path.exists() and not bkp.exists():
                shutil.copy2(final_path, bkp)
        atomic_write(final_path, candidate)
        atomic_write(draft_path, candidate)
        if run_review:
            atomic_json(review_path, review)
    return {
        "chapter": chapter,
        "status": "published",
        "score": score_value,
        "summary": data.get("summary", ""),
        "applied": applied,
        "rejected": rejected,
    }


def deterministic_pass(project: Path, total: int, backup_dir: Path) -> list[dict]:
    results = []
    for chapter in range(1, total + 1):
        final_path = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
        draft_path = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
        if not final_path.exists():
            continue
        original = final_path.read_text(encoding="utf-8", errors="ignore")
        cleaned, changes = clean_text(original)
        cleaned, name_changes = normalize_father_names(cleaned, chapter)
        changes.extend(name_changes)
        if cleaned == original:
            continue
        backup = backup_dir / "final" / final_path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(final_path, backup)
        atomic_write(final_path, cleaned)
        atomic_write(draft_path, cleaned)
        results.append({"chapter": chapter, "changes": sorted(set(changes))})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="按整本终审问题执行可回滚的小说修订")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument("--only-plan", action="store_true")
    parser.add_argument("--from-review", action="store_true",
                        help="从 final_book_review.json 动态读取 issues（通用入口，适用于任意项目，不依赖硬编码根因）")
    parser.add_argument("--root-ids", default="", help="仅执行逗号分隔的根因ID")
    parser.add_argument("--run-name", default="primary", help="本轮结果文件后缀")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    config = load_config(project)
    total = int(config.get("total_chapters", 0))
    reports = project / "reports" / "book_repair"
    reports.mkdir(parents=True, exist_ok=True)
    backup_dir = project / "backups" / f"book_repair_{args.run_name}_{time.strftime('%Y%m%d_%H%M%S')}"
    selected_ids = {item.strip() for item in args.root_ids.split(",") if item.strip()}
    if args.from_review:
        tasks = task_map_from_review(project)
        if not tasks:
            log(project, "未从 final_book_review.json 读取到任何待修订章节（issues 为空或文件缺失）")
    else:
        tasks = task_map(selected_ids or None)
    suffix = "" if args.run_name == "primary" else f"_{args.run_name}"
    plan = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": "确定性清理+根因锚点局部补丁+单章8.5质量门+整本复审",
        "source": "book_review_issues" if args.from_review else "root_repairs",
        "canonical_rules": ROOT_REPAIRS,
        "target_chapters": sorted(tasks),
        "target_count": len(tasks),
        "backup_dir": str(backup_dir),
    }
    atomic_json(reports / f"repair_plan{suffix}.json", plan)
    log(project, f"修订方案已生成，目标章节 {len(tasks)} 章")
    if args.only_plan:
        return

    deterministic = deterministic_pass(project, total, backup_dir)
    atomic_json(reports / f"deterministic_changes{suffix}.json", deterministic)
    log(project, f"确定性清理完成，修改 {len(deterministic)} 章")

    results_path = reports / f"repair_results{suffix}.json"
    results: list[dict] = []
    if results_path.exists():
        try:
            loaded = json.loads(results_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                results = [item for item in loaded if isinstance(item, dict) and isinstance(item.get("chapter"), int)]
        except Exception:
            results = []
    completed_chapters = {item["chapter"] for item in results}
    if completed_chapters:
        log(project, f"断点续跑，跳过已处理 {len(completed_chapters)} 章")
    workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(repair_one, project, config, chapter, issues, not args.no_review, backup_dir): chapter
            for chapter, issues in sorted(tasks.items())
            if chapter not in completed_chapters
        }
        for future in as_completed(futures):
            chapter = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"chapter": chapter, "status": "exception", "error": str(exc)}
            results.append(result)
            log(project, f"第{chapter}章: {result.get('status')} score={result.get('score', '-')}")
            atomic_json(results_path, sorted(results, key=lambda item: item["chapter"]))

    summary = {
        "status": "completed",
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_count": len(tasks),
        "deterministic_changed": len(deterministic),
        "status_counts": {},
        "results": sorted(results, key=lambda item: item["chapter"]),
    }
    for result in results:
        status = result.get("status", "unknown")
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
    atomic_json(reports / f"repair_manifest{suffix}.json", summary)
    log(project, f"修订执行完成: {summary['status_counts']}")


if __name__ == "__main__":
    main()
