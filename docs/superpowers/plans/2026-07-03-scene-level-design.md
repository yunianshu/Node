# 场景级设计穿透 + 重写轮轻量二轮深化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将大纲从扁平 key_events 升级为结构化 scenes 数组，Writer 按场景兑现并在重写轮加二轮深化，Outline Reviewer/Reviewer 加 scene_density 门禁，Planner sensory_palette 升级为分类物象库。

**Architecture:** scenes 与 key_events 并存（向后兼容）。新增检测函数集中在 outline_quality_gate.py 和 reviewer.py 的本地分析区。Writer 重写轮深化为受相似度约束的轻量二轮调用。所有改动通过 py_compile 验证。

**Tech Stack:** Python 3.12，pathlib，现有 mmx_client/novel_config/outline_quality_gate 工具链。无新增依赖。

**设计文档:** `docs/superpowers/specs/2026-07-03-scene-level-design.md`

---

## 文件结构

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `scripts/core/outline_quality_gate.py` | 新增 `detect_scene_density_issues()` 本地检测 | 追加函数 |
| `scripts/pipeline/planner.py` | sensory_palette 分类、契约校验、prompt 增强 | 修改 |
| `scripts/pipeline/outliner.py` | scenes 字段契约校验、prompt 注入 sensory_palette 分类库 | 修改 |
| `scripts/pipeline/outline_reviewer.py` | scene_design design_gate、注入 scene density issues | 修改 |
| `scripts/pipeline/reviewer.py` | `detect_scene_realization()` 本地检测、降分逻辑 | 修改 |
| `scripts/pipeline/writer.py` | 重写轮二轮深化、deepen_rewrite 开关 | 修改 |

无新增文件。每个任务的改动自包含，可独立 py_compile 验证。

---

### Task 1: Planner sensory_palette 分类增强

**Files:**
- Modify: `scripts/pipeline/planner.py`（`_default_quality_bible`、`_validate_world_data`、`_ensure_world_quality_bible`、generate_world prompt）

- [ ] **Step 1: 修改 `_default_quality_bible` 的 sensory_palette 为分类对象**

在 `scripts/pipeline/planner.py` 的 `_default_quality_bible` 函数中，把 `"sensory_palette": [...]` 列表替换为五类对象。找到现有的：

```python
        "sensory_palette": [
            "优先使用与题材绑定的物象、工具、账目、伤痕、旧物、空间压迫和生活噪声。",
            "少用空泛宏大形容词，避免把氛围写成通用影视预告片。",
        ],
```

替换为：

```python
        "sensory_palette": {
            "indoor": ["工位", "厨房灶台", "楼道灯", "账单", "药柜", "旧沙发", "饭桌", "煤炉"],
            "outdoor": ["街市叫卖", "雨棚滴水", "巷口阴影", "野风", "尘土", "市集摊位"],
            "body": ["指节伤口", "汗渍", "旧疤", "磨白袖口", "冻红的手", "干裂嘴唇", "黑眼圈"],
            "object": ["裂屏手机", "铜钥匙", "旧饭盒", "褪色照片", "磨钝的笔", "缺角瓷碗"],
            "sound_smell": ["药味", "饭香", "钟声", "楼道灯嗡嗡", "铁锈味", "潮气", "远处犬吠"],
        },
```

- [ ] **Step 2: 新增 `_migrate_sensory_palette` 迁移函数**

在 `_default_quality_bible` 函数之后、`_ensure_world_quality_bible` 之前，插入：

```python
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
```

- [ ] **Step 3: 修改 `_validate_world_data` 校验 sensory_palette 分类**

在 `_validate_world_data` 中，找到 quality_bible 校验块（`for field in QUALITY_BIBLE_KEYS:` 循环）。在该循环之后追加：

```python
    palette = quality_bible.get("sensory_palette") if isinstance(quality_bible, dict) else None
    if isinstance(palette, list):
        issues.append("quality_bible.sensory_palette 仍是旧版列表，需迁移为分类对象")
    elif not isinstance(palette, dict):
        issues.append("quality_bible.sensory_palette 缺失或不是对象")
    else:
        for cat in SENSORY_PALETTE_CATEGORIES:
            value = palette.get(cat)
            if not isinstance(value, list) or not any(str(v).strip() for v in value):
                issues.append(f"quality_bible.sensory_palette.{cat} 缺失或为空")
```

- [ ] **Step 4: 修改 `_ensure_world_quality_bible` 调用迁移**

在 `_ensure_world_quality_bible` 函数末尾的 `return changed` 之前，插入迁移调用：

```python
    if _migrate_sensory_palette(world):
        changed = True
```

- [ ] **Step 5: 修改 generate_world prompt 中的 sensory_palette 描述**

在 `generate_world` 的 prompt 模板中，找到：

```
    "sensory_palette": ["本书专属感官词库和物象：气味、声音、旧物、工具、伤痕、账目等"],
```

替换为：

```
    "sensory_palette": {{
      "indoor": ["室内物象：工位、厨房、楼道、账单、药柜等"],
      "outdoor": ["室外物象：街市、野外、雨棚、天气等"],
      "body": ["身体细节：伤口、汗、指节、旧疤、冻红的手等"],
      "object": ["随身物件：旧衣、裂屏手机、钥匙、饭盒、褪色照片等"],
      "sound_smell": ["声音气味：药味、饭香、钟声、楼道灯嗡嗡、铁锈味等"]
    }},
```

- [ ] **Step 6: py_compile 验证**

Run: `python -m py_compile scripts/pipeline/planner.py`
Expected: 无输出（成功）

- [ ] **Step 7: 提交**

```bash
git add scripts/pipeline/planner.py
git commit -m "feat(planner): sensory_palette 升级为五类分类物象库+旧列表自动迁移"
```

---

### Task 2: Outliner scenes 字段契约校验 + sensory_palette 注入

**Files:**
- Modify: `scripts/pipeline/outliner.py`（`REQUIRED_CHAPTER_FIELDS`、`_validate_chapter_outline`、prompt 模板）

- [ ] **Step 1: 把 scenes 加入 REQUIRED_CHAPTER_FIELDS**

在 `scripts/pipeline/outliner.py` 找到 `REQUIRED_CHAPTER_FIELDS` 元组，在 `"main_arc_link",` 之后追加：

```python
    "main_arc_link",
    "scenes",
```

- [ ] **Step 2: 新增 scenes 校验逻辑**

在 `_validate_chapter_outline` 函数末尾（`return issues` 之前）追加 scenes 校验。找到函数最后的：

```python
    try:
        target = int(chapter.get("word_count_target", 0))
        if target < 3000:
            issues.append("word_count_target低于3000")
    except (TypeError, ValueError):
        issues.append("word_count_target必须是数字")

    return issues
```

在 `return issues` 之前插入：

```python
    # scenes 场景级设计校验（向后兼容：旧章节缺 scenes 由 REQUIRED_CHAPTER_FIELDS 已报"缺少字段"，
    # 这里校验字段质量）
    scenes = chapter.get("scenes")
    if isinstance(scenes, str):
        issues.append("scenes 必须是数组，不能是字符串")
    elif not isinstance(scenes, list):
        issues.append("scenes 缺失或不是数组")
    else:
        if len(scenes) < 3 or len(scenes) > 5:
            issues.append("scenes 必须为3-5个场景")
        valid_positions = {"opening", "middle", "climax", "closing"}
        anchors: list[str] = []
        for idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                issues.append(f"scenes[{idx}] 不是对象")
                continue
            for sfield in ("position", "objective", "conflict", "sensory_anchor", "subtext_beat", "exit_hook"):
                sval = str(scene.get(sfield, "")).strip()
                if len(sval) < 4:
                    issues.append(f"scenes[{idx}].{sfield} 过短或为空")
                if _has_placeholder(sval):
                    issues.append(f"scenes[{idx}].{sfield} 是占位文本")
            pos = str(scene.get("position", "")).strip()
            if pos and pos not in valid_positions:
                issues.append(f"scenes[{idx}].position 必须是 {','.join(sorted(valid_positions))} 之一")
            anchor = str(scene.get("sensory_anchor", "")).strip()
            if anchor:
                anchors.append(anchor)
        if len(anchors) != len(set(anchors)):
            issues.append("scenes 的 sensory_anchor 存在重复，每个场景物象必须独立")

```

- [ ] **Step 3: 在大纲质量契约中新增 scenes 设计要求**

在 `_outline_quality_contract()` 函数中，找到 `### 【内容丰富度·强制要求】` 段。在该段末尾（`- 禁止"抽象概念堆叠"...` 那条之后）追加新段：

```


### 【场景级设计·强制要求】（scenes 数组）
- 必须输出 3-5 个 scene 对象，每个含 position(opening/middle/climax/closing)、objective(本场景主角具体目标)、conflict(阻碍+对手+赌注)、sensory_anchor(本场景专属可触摸物象/气味/声音/身体细节，取自 quality_bible.sensory_palette 对应类别)、subtext_beat(一句潜台词或未说出口的话)、exit_hook(本场景如何推向下一场景的不可逆动作或信息落点)。
- 每个 scene 的 sensory_anchor 必须互不重复，且至少一个 scene 落地本章 human_anchor 的生活压力/关系牵挂。
- sensory_anchor 必须从 world.quality_bible.sensory_palette 五类(indoor/outdoor/body/object/sound_smell)中取材，不能写空泛形容词。
- scenes 与 key_events 互补：key_events 是高层事件链，scenes 是场景级执行蓝图；Writer 会按 scenes 逐场兑现。
```

- [ ] **Step 4: 在 prompt 模板中注入 sensory_palette 分类库**

在 outliner.py 的章节生成 prompt 构建处（搜索 `json.dumps(world` 或 `quality_bible`），找到注入 world 的位置。在 prompt 中世界观注入之后追加 sensory_palette 显式提示。定位 `generate_chapter_outlines` 或主生成函数中构建 prompt 的地方，在 world json 注入后追加：

找到 prompt 中类似 `{json.dumps(world, ensure_ascii=False, indent=2)[:XXXX]}` 的世界观注入行，在其后追加一行：

```python
{sensory_palette_directive}
```

并在 prompt 构建前计算该变量（在 prompt 字符串拼接之前）：

```python
    palette = (world.get("quality_bible") or {}).get("sensory_palette") if isinstance(world, dict) else None
    if isinstance(palette, dict):
        sensory_palette_directive = "## 【感官物象分类库】（scenes.sensory_anchor 必须从以下取材，每场景选一类）\n" + json.dumps(palette, ensure_ascii=False, indent=2)
    else:
        sensory_palette_directive = "## 【感官物象分类库】（quality_bible.sensory_palette 缺失，请自行设计 indoor/outdoor/body/object/sound_smell 五类物象供 scenes 取材）"
```

注：因为 outliner.py 有多个 prompt 构建点（卷纲、单章、修复），只需在单章生成的主 prompt 中注入。如果定位困难，可在 `_story_architecture_context` 返回的 payload 中加入 sensory_palette，让所有 prompt 自然继承——更稳妥。优先用后者：

修改 `_story_architecture_context` 的 payload dict，在 `"volume_plan_policy"` 之后加：

```python
        "sensory_palette": (world.get("quality_bible") or {}).get("sensory_palette") if isinstance(world.get("quality_bible"), dict) else None,
```

- [ ] **Step 5: py_compile 验证**

Run: `python -m py_compile scripts/pipeline/outliner.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add scripts/pipeline/outliner.py
git commit -m "feat(outliner): scenes 场景级字段契约校验+sensory_palette 分类库注入"
```

---

### Task 3: outline_quality_gate 新增 detect_scene_density_issues

**Files:**
- Modify: `scripts/core/outline_quality_gate.py`（追加函数）

- [ ] **Step 1: 新增 detect_scene_density_issues 函数**

在 `scripts/core/outline_quality_gate.py` 末尾追加：

```python
SCENE_REQUIRED_FIELDS = (
    "position", "objective", "conflict", "sensory_anchor", "subtext_beat", "exit_hook",
)
SCENE_VALID_POSITIONS = ("opening", "middle", "climax", "closing")


def detect_scene_density_issues(outline: dict) -> dict | None:
    """本地确定性检测 scenes 数组密度——单章审查的 scene_design 门证据来源。

    检查 scene 数量(3-5)、五字段完整性、position 合法、sensory_anchor 不重复。
    返回 {issues, suggestion} 或 None（无 scenes 字段时，由上层决定是否报缺字段）。
    """
    if not isinstance(outline, dict):
        return None
    scenes = outline.get("scenes")
    if scenes is None:
        return None
    issues: list[str] = []
    if not isinstance(scenes, list):
        return {"issues": ["scenes 不是数组"], "suggestion": "scenes 必须是 3-5 个场景对象数组"}
    if len(scenes) < 3 or len(scenes) > 5:
        issues.append(f"scenes 数量为 {len(scenes)}，应为 3-5 个")
    anchors: list[str] = []
    for idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            issues.append(f"scenes[{idx}] 不是对象")
            continue
        for field in SCENE_REQUIRED_FIELDS:
            val = str(scene.get(field, "")).strip()
            if len(val) < 4:
                issues.append(f"scenes[{idx}].{field} 过短或为空")
        pos = str(scene.get("position", "")).strip()
        if pos and pos not in SCENE_VALID_POSITIONS:
            issues.append(f"scenes[{idx}].position 非法: {pos}")
        anchor = str(scene.get("sensory_anchor", "")).strip()
        if anchor:
            anchors.append(anchor)
    if len(anchors) != len(set(anchors)) and anchors:
        issues.append("scenes 的 sensory_anchor 存在重复")
    if not issues:
        return None
    return {
        "issues": issues[:6],
        "suggestion": "补齐 scenes 数组：3-5 个场景，每个含 position/objective/conflict/sensory_anchor/subtext_beat/exit_hook，sensory_anchor 从 sensory_palette 取材且互不重复",
    }
```

- [ ] **Step 2: py_compile 验证**

Run: `python -m py_compile scripts/core/outline_quality_gate.py`
Expected: 无输出

- [ ] **Step 3: 提交**

```bash
git add scripts/core/outline_quality_gate.py
git commit -m "feat(quality_gate): detect_scene_density_issues 本地场景密度检测"
```

---

### Task 4: Outline Reviewer 集成 scene_design 门禁

**Files:**
- Modify: `scripts/pipeline/outline_reviewer.py`（import、design_gate schema、本地检测注入）

- [ ] **Step 1: import detect_scene_density_issues**

在 `scripts/pipeline/outline_reviewer.py` 顶部的 `from core.outline_quality_gate import (` 块中追加 `detect_scene_density_issues`。找到：

```python
from core.outline_quality_gate import (
    cast_name_set,
    clean_char_name,
    detect_adjacent_event_repetition,
    detect_beat_runs,
    detect_cast_violations,
)
```

替换为：

```python
from core.outline_quality_gate import (
    cast_name_set,
    clean_char_name,
    detect_adjacent_event_repetition,
    detect_beat_runs,
    detect_cast_violations,
    detect_scene_density_issues,
)
```

- [ ] **Step 2: 在 review_outline 中计算 scene density issue**

在 `review_outline` 函数中，找到 `adjacent_repetition_issue = detect_adjacent_event_repetition(prev_outline, outline)` 行，在其后追加：

```python
    scene_density_issue = detect_scene_density_issues(outline)
```

- [ ] **Step 3: prompt 的 design_gates schema 增加 scene_design**

在 outline_reviewer.py 的 prompt JSON 模板中，找到 `"strong_hook"` design_gate 那行：

```python
    "strong_hook": {{"passed": true, "evidence": "章末正在发生的具体危机或反转，60字以内"}}{repair_gate_schema}
```

替换为：

```python
    "strong_hook": {{"passed": true, "evidence": "章末正在发生的具体危机或反转，60字以内"}},
    "scene_design": {{"passed": true, "evidence": "scenes 数组3-5个、五字段齐全、sensory_anchor不重复且取自分类库、human_anchor落进至少一个场景，60字以内"}}{repair_gate_schema}
```

- [ ] **Step 4: required_gates 元组增加 scene_design**

在 review_outline 中找到：

```python
    required_gates = (
        "core_desire",
        "irreversible_choice",
        "midpoint_reversal",
        "human_warmth",
        "content_richness",
        "strong_hook",
    )
```

替换为：

```python
    required_gates = (
        "core_desire",
        "irreversible_choice",
        "midpoint_reversal",
        "human_warmth",
        "content_richness",
        "strong_hook",
        "scene_design",
    )
```

- [ ] **Step 5: 注入 scene_density_issue 强制需修改**

在 review_outline 中，找到 `if adjacent_repetition_issue:` 块之前，插入 scene density 强制逻辑：

```python
        if scene_density_issue:
            evi = "；".join(scene_density_issue["issues"])
            review_data.setdefault("continuity_issues", [])[:] = [evi]
            review_data.setdefault("weaknesses", [])[:] = [evi]
            review_data.setdefault("suggestions", [])[:] = [scene_density_issue["suggestion"]]
            edits = review_data.setdefault("edits", [])
            if isinstance(edits, list) and not any(
                isinstance(edit, dict) and edit.get("field") == "scenes"
                for edit in edits
            ):
                edits[:] = [{
                    "field": "scenes",
                    "action": "replace",
                    "value": "3-5个场景对象，每个含position/objective/conflict/sensory_anchor/subtext_beat/exit_hook",
                }]
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["scene_density_issue"] = scene_density_issue
```

- [ ] **Step 6: hard_gate_ok 加入 scene_density_issue 条件**

找到：

```python
        hard_gate_ok = (
            design_gate_passed
            and repair_feedback_closed
            and not beat_flat_issues
            and not adjacent_repetition_issue
            and not continuity_hard_failures
            and not (cast_violations["polluted"] or cast_violations["unregistered"])
        )
```

替换为：

```python
        hard_gate_ok = (
            design_gate_passed
            and repair_feedback_closed
            and not beat_flat_issues
            and not adjacent_repetition_issue
            and not scene_density_issue
            and not continuity_hard_failures
            and not (cast_violations["polluted"] or cast_violations["unregistered"])
        )
```

- [ ] **Step 7: py_compile 验证**

Run: `python -m py_compile scripts/pipeline/outline_reviewer.py`
Expected: 无输出

- [ ] **Step 8: 提交**

```bash
git add scripts/pipeline/outline_reviewer.py
git commit -m "feat(outline_reviewer): scene_design design_gate+本地场景密度门禁"
```

---

### Task 5: Reviewer 新增 detect_scene_realization 本地检测

**Files:**
- Modify: `scripts/pipeline/reviewer.py`（新增函数、集成到 local_analysis、降分逻辑）

- [ ] **Step 1: 新增 detect_scene_realization 函数**

在 `scripts/pipeline/reviewer.py` 的 `detect_relationship_obligation` 函数之后追加：

```python
def detect_scene_realization(chapter_content: str, chapter_outline: dict) -> dict:
    """本地检测正文是否兑现大纲 scenes 的感官锚点、潜台词和出口钩子。

    非硬门禁：用 bigram 软匹配避免误杀同义改写。scene_realization_rate < 0.5 时
    由上层降 human_warmth 分并触发重写轮深化。
    """
    if not isinstance(chapter_outline, dict):
        return {"required": False, "rate": None, "scenes": []}
    scenes = chapter_outline.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"required": False, "rate": None, "scenes": []}
    text = chapter_content or ""
    subtext_markers = ("没说", "沉默", "欲言又止", "别告诉", "对不起", "谢谢", "别怕", "算了", "低声", "移开目光")
    realized_count = 0
    scene_results = []
    for idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        anchor = str(scene.get("sensory_anchor", "")).strip()
        anchor_terms = _anchor_terms(anchor)
        anchor_hits = [t for t in anchor_terms if t in text]
        subtext = str(scene.get("subtext_beat", "")).strip()
        subtext_terms = _anchor_terms(subtext)
        subtext_hits = [t for t in subtext_terms if t in text]
        subtext_marker_hits = [m for m in subtext_markers if m in text]
        exit_hook = str(scene.get("exit_hook", "")).strip()
        exit_terms = _anchor_terms(exit_hook)
        exit_hits = [t for t in exit_terms if t in text]
        anchor_ok = len(anchor_hits) >= max(1, min(2, len(anchor_terms) // 3))
        subtext_ok = bool(subtext_hits) or bool(subtext_marker_hits)
        exit_ok = bool(exit_hits)
        realized = anchor_ok and (subtext_ok or exit_ok)
        if realized:
            realized_count += 1
        scene_results.append({
            "index": idx,
            "position": str(scene.get("position", "")),
            "realized": realized,
            "anchor_hits": anchor_hits[:6],
            "subtext_hits": subtext_hits[:4],
            "exit_hits": exit_hits[:4],
        })
    rate = round(realized_count / len(scene_results), 3) if scene_results else 0.0
    return {
        "required": True,
        "rate": rate,
        "realized_count": realized_count,
        "total_scenes": len(scene_results),
        "scenes": scene_results,
        "needs_attention": rate < 0.5,
    }
```

- [ ] **Step 2: 集成到 review_chapter 的 local_analysis**

在 `review_chapter` 中找到：

```python
    local_analysis["relationship_obligation_detection"] = detect_relationship_obligation(
        chapter_content, chapter_number
    )
```

在其后追加：

```python
    local_analysis["scene_realization_detection"] = detect_scene_realization(
        chapter_content, chapter_outline
    )
```

- [ ] **Step 3: 降分逻辑——scene_realization_rate < 0.5 加入 hard_gate_reasons**

在 review_chapter 的 hard_gate_reasons 构建块中（`relationship_obligation` 检测之后），追加：

```python
        scene_realization = local_analysis.get("scene_realization_detection") or {}
        if (
            isinstance(scene_realization, dict)
            and scene_realization.get("required") is True
            and scene_realization.get("needs_attention") is True
        ):
            rate = scene_realization.get("rate")
            hard_gate_reasons.append(
                f"本地场景兑现率过低({rate})：scenes 的 sensory_anchor/subtext_beat/exit_hook 未充分落地正文"
            )
```

- [ ] **Step 4: prompt 审查清单增加 scene 兑现项**

在 reviewer.py 的审查清单（`【9分神作核心审查清单】`）末尾追加一条。找到最后一条清单项（编号 26），在其后加：

```
27. 本章是否按大纲 scenes 逐场兑现：每个场景的 sensory_anchor（可触摸物象）、subtext_beat（潜台词）、exit_hook（出口钩子）是否在正文中落地？兑现率过低视为内容单薄。
```

并把要求区第 16 条后追加（在 origin_fact_reference_detection 那条之后）：

```
17. 若 local_analysis.scene_realization_detection.needs_attention=true，verdict 不得为"通过"，必须在 weaknesses/suggestions 中指出哪些 scene 未兑现，并在 edits 中定点补 sensory_anchor 或潜台词对白。
```

- [ ] **Step 5: py_compile 验证**

Run: `python -m py_compile scripts/pipeline/reviewer.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add scripts/pipeline/reviewer.py
git commit -m "feat(reviewer): detect_scene_realization 本地场景兑现检测+降分门禁"
```

---

### Task 6: Writer 重写轮二轮深化

**Files:**
- Modify: `scripts/pipeline/writer.py`（新增 `_deepen_rewrite` 函数、集成到 generate_chapter 重写路径）

- [ ] **Step 1: 新增 `_deepen_rewrite` 函数**

在 `scripts/pipeline/writer.py` 的 `_apply_incremental_edits` 函数之后追加：

```python
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
        f"- 场景{s.get('position','')}：物象={s.get('sensory_anchor','')}；潜台词={s.get('subtext_beat','')}；出口={s.get('exit_hook','')}"
        for s in scenes if isinstance(s, dict)
    )
    palette_text = json.dumps(palette, ensure_ascii=False) if isinstance(palette, dict) else "（quality_bible.sensory_palette 缺失，自行选用室内/室外/身体/物件/声音气味物象）"
    ai_issues = ai_flavor_detection.get("issues", []) if isinstance(ai_flavor_detection, dict) else []
    ai_hint = ""
    if ai_issues:
        ai_hint = "\n## 本地AI味检测指出的缺失（针对性补齐）\n" + "\n".join(
            f"- {it.get('type','')}: {it.get('paragraph','')}" for it in ai_issues[:4] if isinstance(it, dict)
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
```

- [ ] **Step 2: 在 generate_chapter 重写路径末尾集成深化调用**

在 `generate_chapter` 函数中，找到首轮重写成功保存之前的位置。定位到自动压缩之后、保存之前：

```python
    # 自动压缩：如果超过 max_words，调用压缩 agent
    content = _auto_compress(content, chapter_number, max_words, target_min, chapter_outline)
```

在 `_auto_compress` 调用之后、`word_count = len(content)` 之前，插入深化逻辑：

```python
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

```

- [ ] **Step 3: py_compile 验证**

Run: `python -m py_compile scripts/pipeline/writer.py`
Expected: 无输出

- [ ] **Step 4: 提交**

```bash
git add scripts/pipeline/writer.py
git commit -m "feat(writer): 重写轮轻量二轮深化(感官/潜台词/余韵)+相似度约束+deepen_rewrite开关"
```

---

### Task 7: 全量 py_compile + 集成验证

**Files:** 无修改，仅验证

- [ ] **Step 1: 全量 py_compile（按 AGENTS.md 命令）**

Run（在仓库根目录）:
```
python -m py_compile "scripts/pipeline/planner.py" "scripts/pipeline/outliner.py" "scripts/pipeline/outline_reviewer.py" "scripts/pipeline/writer.py" "scripts/pipeline/reviewer.py" "scripts/core/outline_quality_gate.py"
```
Expected: 无输出（全部通过）

- [ ] **Step 2: 确认未破坏其他脚本**

Run:
```
python -m py_compile "scripts/pipeline/coordinator.py" "scripts/pipeline/media_generator.py" "scripts/core/novel_config.py" "scripts/maintenance/book_reviewer.py" "scripts/maintenance/outline_book_reviewer.py"
```
Expected: 无输出

- [ ] **Step 3: 验证旧项目兼容（无 scenes 的大纲不报错）**

人工检查：确认 `_validate_chapter_outline` 中 scenes 校验位于字段缺失校验之后，旧章节会因 `REQUIRED_CHAPTER_FIELDS` 报"缺少字段 scenes"，但这是新章节生成时的强制；旧项目已生成的章节不会被重新校验（outliner 只在新写时校验）。Reviewer 的 detect_scene_realization 在 scenes 缺失时返回 required=False，不影响旧章节审查。

确认逻辑无误即可，无需运行完整 coordinator（需要 API 配置）。

- [ ] **Step 4: 最终提交（如有遗漏）**

```bash
git status
# 若有未提交改动则提交；否则跳过
```

---

## Self-Review

**Spec coverage:**
- Section 1 (scenes 数组) → Task 2 (契约校验 + prompt)
- Section 2 (Writer 二轮深化) → Task 6
- Section 3 (scene_density 门禁: Outline Reviewer + Reviewer) → Task 3+4 (大纲层) + Task 5 (正文层)
- Section 4 (Planner sensory_palette 分类) → Task 1
- 验收标准 (py_compile, 旧项目兼容) → Task 7
- Media/整本审查不动 → 明确不在范围，无需任务

**Placeholder scan:** 所有步骤含完整代码或确切命令，无 TBD。

**Type consistency:** `detect_scene_density_issues` 返回 `{issues, suggestion}`（Task 3 定义，Task 4 消费一致）；`detect_scene_realization` 返回 `{required, rate, needs_attention, scenes}`（Task 5 定义+消费一致）；`_deepen_rewrite` 签名（Task 6 定义+调用一致）；`SCENE_REQUIRED_FIELDS`/`SCENE_VALID_POSITIONS` 命名一致；`SENSORY_PALETTE_CATEGORIES` 在 Task 1 定义并在校验/迁移中复用。

**覆盖完整，可交付执行。**
