# 场景级设计穿透 + 重写轮轻量二轮深化

日期：2026-07-03
状态：已确认（Section 1-4 经用户逐节确认）

## 背景与目标

现有流水线已具备 quality_bible、life_profile、human_anchor、伏笔台账、关系/角色/弧线状态追踪、AI 味检测、4 候选竞速和多级门禁。但仍存在内容单薄的根因：

- 大纲以扁平 `key_events` 列表描述全章，Writer 缺少场景级"目标/冲突/感官/潜台词/出口"结构指引，容易写成单线事件链。
- 正文缺少按场景兑现的可量化抓手，丰富度只靠全章级 human_warmth 启发式粗判。
- Planner 的 sensory_palette 是通用列表，Outliner 取材时缺乏分类指引。

本轮优化覆盖 Planner / Outliner / Outline Reviewer / Writer / Reviewer 五个核心阶段，目标：让生成的章节在场景密度、感官落地、潜台词承载和不可逆动作上更稳定、更丰富，同时不显著增加 API 成本。

## 方案：A 场景级设计穿透 + 轻量 B 重写轮深化

### Section 1 — 大纲 scenes 数组（与 key_events 并存）

新增单章大纲字段 `scenes`，3-5 个场景对象，与现有 `key_events` 并存（key_events 保留为高层摘要，向后兼容）。

```json
"scenes": [
  {
    "position": "opening|middle|climax|closing",
    "objective": "主角本场景具体想得到/推进什么",
    "conflict": "阻碍 + 对手 + 赌注",
    "sensory_anchor": "本场景专属的可触摸物象/气味/声音/身体细节（取自 quality_bible.sensory_palette 对应类别）",
    "subtext_beat": "一句潜台词/未说出口的话/欲言又止",
    "exit_hook": "本场景如何推向下一场景（不可逆动作或信息落点）"
  }
]
```

约束：
- 每章 3-5 个 scene。
- `human_anchor` 必须落进至少一个 scene（由 Outline Reviewer 校验）。
- 旧章节缺 scenes 时不报错（向后兼容）；新章节生成必须提供（由 outliner.py 契约校验强制）。

### Section 2 — Writer 重写轮轻量二轮深化

触发时机：仅在重写路径（`is_rewrite=True`，章节未通过 Reviewer）。首轮 4 候选竞速不变，成本不增加。

流程：
1. 第一轮：按现有逻辑基于审查反馈重写 → `content`。
2. 第二轮（深化）：注入 `content` + 本章 `scenes`（每 scene 的 sensory_anchor/subtext_beat/exit_hook）+ `human_anchor` + `quality_bible.sensory_palette` + `local_analysis.ai_flavor_detection`，要求模型只做局部深化：
   - 为每个 scene 补一个可触摸物象（从 sensory_palette 对应类别取材）。
   - 把至少一句对白改成潜台词。
   - 在不可逆动作后加 1-2 个现场反应（余韵）。
   - 针对 ai_flavor_detection 指出的缺失感官/潜台词类别补齐。
   - **禁止改主线、换场景、删事件。**

契约约束（防失控）：
- 第二轮 max_tokens=4096，temperature=0.4。
- 输出必须保留第一轮 ≥70% 字符（用现有 `similarity` 校验），否则丢弃深化结果用第一轮。
- 字数/截断校验复用现有逻辑（超 max_words 走 `_auto_compress`，低于 min_words 回退第一轮）。
- 配置开关 `config.writer.deepen_rewrite`，默认 true；关闭时退回单轮重写。
- 日志记录第二轮耗时和相似度。

### Section 3 — scene_density 门禁

Outline Reviewer（大纲层）：
- 新增 design_gate `scene_design`：检查 scenes 数组存在、3-5 个、每 scene 五字段非空、sensory_anchor 不重复、human_anchor 至少落进一个 scene。
- outliner.py `_validate_chapter_outline`：新章节缺 scenes 或字段不达标列入 issues，触发重生成。
- outline_quality_gate.py 新增 `detect_scene_density_issues(outline)`：本地检测 scene 数量、字段空置率、sensory_anchor 重复率，返回 issues + suggestion，注入 outline_reviewer review_data，强制需修改。

Reviewer（正文层）：
- 新增本地检测 `detect_scene_realization(chapter_content, outline)`：对每 scene 检查正文是否出现 sensory_anchor 关键词（bigram，复用 `_anchor_terms`/`_keyword_hits`）、是否有潜台词标记、是否有 exit_hook 落点。返回每 scene `realized: bool` + 缺失项。
- 非硬门禁（避免 bigram 误杀同义改写）；但 `scene_realization_rate < 0.5` 时降 human_warmth 分并写 weaknesses/suggestions，触发重写轮深化。
- 评分维度 `content_richness` 描述明确：scene 层兑现率纳入考量。

### Section 4 — Planner sensory_palette 分类增强

将 `quality_bible.sensory_palette` 从通用列表升级为分类物象库：

```json
"sensory_palette": {
  "indoor": ["工位", "厨房", "楼道", "账单", "..."],
  "outdoor": ["街市", "野外", "雨棚", "..."],
  "body": ["伤口", "汗", "指节", "旧疤", "..."],
  "object": ["旧衣", "裂屏手机", "钥匙", "饭盒", "..."],
  "sound_smell": ["药味", "饭香", "钟声", "楼道灯嗡嗡", "..."]
}
```

- Planner 生成 prompt 增加分类要求；`_default_quality_bible` 兜底按分类填。
- `_validate_world_data` 校验 sensory_palette 是对象且五类非空；旧文件缺分类时自动迁移。
- Outliner scene `sensory_anchor` 要求从 sensory_palette 对应类别取材，prompt 显式注入分类库。

## 不在本轮范围

- Media Generator：不直接决定正文丰富度（见 audit 文档），不动。
- 整本大纲审查（outline_book_reviewer）：自然受益于 scene 结构（重复检测粒度更细），无需单独改。
- relationship_state / character_state / arc_state：本轮不扩展状态字段。

## 验收标准

- `py_compile` 通过所有改动脚本。
- coordinator.py 能在 `--start N --end M` 小范围跑通单章质量门。
- 新生成大纲含 scenes 数组；新 world.json sensory_palette 为分类对象。
- 旧项目（无 scenes、sensory_palette 为列表）能正常加载，不报契约错误。
- Writer 重写轮在 deepen_rewrite=true 时走两轮，日志可见相似度；deepen_rewrite=false 时退回单轮。

## 风险与缓解

- 大纲 JSON 体积变大 → Outliner prompt 已有 origin 截断，scenes 字段简短（每 scene 5 个短字段），增量可控。
- 第二轮深化改变主线 → 相似度 ≥70% 约束 + "禁止改主线"指令 + 丢弃回退。
- bigram scene_realization 误杀 → 设为非硬门禁，仅降分触发重写。
- 旧项目兼容 → scenes 缺失不报错；sensory_palette 旧格式自动迁移。
