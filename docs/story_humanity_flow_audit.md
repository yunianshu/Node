# 小说生成流程与“烟火气”优化审计

目标：让自动化流程不只生成结构完整、节奏合格的章节，而是稳定产出有生活压力、关系牵挂、潜台词和人物温度的故事。

## 当前流程

1. Planner 读取 `premise.txt`、`origin/` 和配置，生成 `world.json`、`characters.json`，并补齐媒体提示词。
2. Outliner 生成卷纲和单章大纲，单章文件落到 `chapters/outline/chapter_XXXX.json`。
3. Outline Reviewer 审查单章大纲，输出 `chapters/outline_review/chapter_XXXX_review.json`，并通过本地聚合门禁。
4. Outline Book Reviewer 做整本大纲层审查，检查缺章、重复、衔接、伏笔和终局闭环。
5. Foreshadowing Audit 检查伏笔闭环，防止只埋不收。
6. Writer 按已过审大纲、前章结尾、角色状态、成长弧线、关系欠账和反馈生成正文。
7. Reviewer 审查正文，Draft Gate 根据评分、本地字数/重复/截断检查和候选赛马结果发布 `final`。
8. Coordinator 抽取角色状态、成长弧线和关系欠账，刷新进度，整本完成后触发 Book Reviewer 终审。
9. Story Flow Audit 可在任意时间只读扫描项目；Coordinator 在正常完成或终审失败诊断路径也会自动生成当前范围的人情味流程证据和缺口。

## 阶段矩阵

| 阶段 | 入口 | 主要输入 | 主要输出 | 烟火气/人情味抓手 | 失败回路 |
|---|---|---|---|---|---|
| 项目配置 | `novel_config.py` | `config.json`、环境变量、项目目录 | 标准目录、默认质量阈值、终审修复开关 | 创建 `relationship_states` 等状态目录，默认启用终审关系/烟火气修复 | 配置缺失时回落默认值；路径解析失败直接停止 |
| Planner | `planner.py` | `premise.txt`、`origin/`、配置 | `world.json`、`characters.json` | `life_profile` 记录家庭、谋生压力、旧债、软肋、日常动作和不愿说出口的话 | 已有合格文件则跳过；旧角色档案缺 `life_profile` 会被契约拦住并提示 backfill；缺媒体提示词时可补齐 |
| Media | `media_generator.py` | `world.json.media_prompts`、角色设定 | 封面、视频、主题歌资产 | 媒体不直接决定人情味，但可承接世界观物件和氛围 | 生成失败阻断媒体阶段，不改正文链路 |
| Outliner | `outliner.py` | 世界观、角色、卷纲、前后章事实、审查反馈 | `chapters/outline/chapter_XXXX.json` | `human_anchor` 把生活压力、关系牵挂、潜台词或物件写进大纲 | 结构缺失不落盘；失败反馈压缩后重试 |
| Outline Reviewer | `outline_reviewer.py` | 单章大纲、角色/世界观、修复反馈 | `chapters/outline_review/chapter_XXXX_review.json` | `human_warmth` 设计门；`repair_feedback_checks` 验收整本反馈 | 未过硬门槛时不得判通过；字段级 `edits` 回灌 |
| Outline Gate | `outline_gate.py` | 大纲、单章审查、整本反馈 | 通过的大纲/审查，或反馈文件 | 把整本问题转成 `outline_book_feedback`、`book_relationship_feedback`、`book_human_warmth_feedback` | 候选赛马或单章重试；重写大纲时清理正文产物 |
| Outline Book Review | `outline_book_reviewer.py` | 全量单章大纲、逐章审查 | `reports/outline_book_review/final_outline_review.json` | 在正文前检查结构、重复、伏笔和终局闭环，避免后期只靠正文补救 | 未过则定向修后章，最早章保留事实锚点 |
| Foreshadowing Audit | `foreshadowing_audit.py` | 全量大纲和伏笔台账 | `reports/foreshadowing_ledger.json` | 防止“只埋不收”，让人物承诺和旧债有兑现空间 | critical dangling 会阻断正文阶段 |
| Writer | `writer.py` | 已过审大纲、前章结尾、角色状态、成长弧线、关系欠账、反馈 | `chapters/draft/chapter_XXXX.txt` | 注入 `life_profile`、`human_anchor`、`relationship_state`，要求写出具体物件、潜台词和关系回声 | 正文不通过时带 Reviewer 反馈重写同章 |
| Reviewer | `reviewer.py` | 草稿、单章大纲、角色设定、origin | `chapters/review/chapter_XXXX_review.json` | `human_warmth` 评分、本地 `human_warmth_detection` 和 `relationship_obligation_detection`；未兑现锚点或关系任务不得通过 | 本地硬门槛失败会降级 verdict/score |
| Draft Gate | `draft_gate.py` | 草稿、正文审查、本地质量门 | `chapters/final/chapter_XXXX.txt` | 只有通过质量门的正文进入 final，保障下一章读取的是合格事实 | 赛马候选或重写；章节严格串行 |
| Post Chapter State | `coordinator.py` | final 正文、角色设定 | `character_states`、`arc_states`、`relationship_states` | 抽取照料、亏欠、误会、承诺、未说出口的话，供下一章使用 | 抽取异常只记录，不阻断已过审正文 |
| Book Reviewer | `book_reviewer.py` | final 全文、逐章审查、关系状态、伏笔台账、节奏信号 | `reports/book_review/*` | 检查关系欠账轨迹、`human_warmth_streak`、终审 `relationship_repair_targets` | 未达终审分会生成修复反馈和 `repair_manifest.json` |
| Legacy Backfill | `backfill_humanity_fields.py` | 旧 `characters.json`、旧单章大纲 | 补齐后的 `life_profile`、`human_anchor` | 让旧项目进入新质量门，不因缺字段天然失败 | 默认 dry-run；`--apply --backup` 才写用户数据；可用 `--invalidate-outline-reviews` 强制重审；输出 `suggested_actions` 区分重跑风险，并按配置和已有产物标记推荐动作；`--run-recommended-action` 默认最多自动执行 50 章且只允许 `review_outline_only`，配置级完整质量门放行会检查目标章是否已有正文产物，CLI 显式 allow 可覆盖 |
| Portable Check | `portable_check.py` | 项目目录、配置、关键产物 | 迁移预检日志 | 检查 `life_profile`、`human_anchor`、`relationship_states` 目录、旧聚合大纲文件和现有 `story_flow_audit.json` 状态/新鲜度，提前发现旧项目未进入新质量门 | 只给 WARN/FAIL、backfill 建议命令、旧文件迁移提示和 story flow audit 建议命令，不改写用户内容 |
| Story Flow Audit | `story_flow_audit.py` / `coordinator.py` | 当前项目产物、审查 JSON、关系状态、终审报告 | `reports/story_flow_audit.json` | 汇总 `life_profile`、`human_anchor`、人情味门、关系任务、origin/facts 和终审修复清单证据 | 只读扫描，不调用模型；可手动运行，也会由 Coordinator 收尾自动生成；输出 `overall_status`、`stage_statuses`、确定性缺口和建议命令，供 dashboard 或人工复盘 |
| WeChat Progress | `wechat_notify.py` / `wechat_pusher_lane.py` | 章节产物、审查分数、`story_flow_audit.json` | 企业微信进度消息 | 当 story flow audit 已生成且非 ok 或早于关键输入产物时，在常规进度里暴露流程审计缺口 | 只读摘要，不触发重跑；缺失报告交给 Portable Check 或 Coordinator 收尾生成 |

## 证据产物

| 产物 | 说明 | 用途 |
|---|---|---|
| `characters.json.life_profile` | 角色的家庭/生计/旧债/软肋/日常动作 | Planner 到 Outliner/Writer 的人情味素材 |
| `chapters/outline/chapter_XXXX.json.human_anchor` | 单章生活压力、关系牵挂、潜台词或物件锚点 | Writer 必须现场化兑现，Reviewer 本地检测会核对 |
| `chapters/outline_review/chapter_XXXX_review.json.design_gates.human_warmth` | 单章大纲人情味设计门 | 防止事件链大纲进入正文阶段 |
| `chapters/review/chapter_XXXX_review.json.local_analysis.human_warmth_detection` | 正文是否兑现 `human_anchor` 和基础人情味元素 | 未通过时正文不得判“通过” |
| `chapters/review/chapter_XXXX_review.json.local_analysis.relationship_obligation_detection` | 正文是否兑现上一章延续来的关系硬任务 | 未通过时正文不得判“通过”，并回写定向修复反馈 |
| `chapters/review/chapter_XXXX_review.json.local_analysis.origin_fact_reference_detection` | 正文是否命中并兑现 `origin/facts` 中的事实素材关键词与事实短句 | 区分事实遵守、只借用名词和风格模仿，供 Reviewer 和后续门禁调参 |
| `chapters/relationship_states/chapter_XXXX.json` | 每章关系欠账、误会、承诺、照料、未说出口的话 | 下一章 Writer 注入，整本终审分析关系轨迹 |
| `reports/book_review/local_full_scan.json` | 全书本地扫描、重复、质量门、`human_warmth_streak`、最有人味/最空泛章节样例 | 终审上下文、烟火气修复目标和人工调参参照 |
| `reports/book_review/final_book_review.json` | 整本终审结论、维度分、关系修复目标 | 发布门禁和关系线回灌来源 |
| `logs/book_relationship_feedback_chXXXX_roundN.json` | 关系线终审问题转成单章大纲反馈 | 复用 `process_outline_gate` 定向修大纲 |
| `logs/book_human_warmth_feedback_chXXXX_roundN.json` | 连续缺烟火气问题转成单章大纲反馈 | 要求目标章补物件、问句对白、配角主动选择 |
| `logs/text_artifact_cleanup.jsonl` | 大纲重写或终审修复前清理 draft/review/final/状态文件的逐次记录 | 证明哪些正文派生产物实际被删除、哪些本来不存在 |
| `reports/book_review/repair_manifest.json` | 终审失败后的修复清单、目标章、续跑起点、反馈文件、清理产物、复核命令和验证结果 | 人工复盘、dashboard 消费和修复后验证 |
| `portable_check.py` 迁移预检输出 | 目录可写、依赖、webhook、旧项目人情味字段缺口、历史 `outline.json`/`index.json`、现有 `story_flow_audit.json` 状态和是否过期 | 接手项目时先发现是否需要运行 backfill、人工迁移旧大纲或补跑只读流程审计 |
| `reports/story_flow_audit.json` | 当前范围各阶段证据数量、`overall_status`、`stage_statuses`、缺口、建议命令 | 不跑模型即可判断项目是否已经进入人情味质量链路，便于 dashboard 或人工审计；其 mtime 会和关键输入产物比较，识别报告是否落后于后续变更 |
| 企业微信进度消息中的“审计”行 | `story_flow_audit.json.overall_status`、新鲜度与最多 3 个非 ok 阶段 label | 长篇运行时避免只看章节数，及时发现人情味/关系/origin facts 证据链已出现缺口，或审计报告已经早于后续章节产物 |

## 失败回路

1. 单章大纲失败：`process_outline_gate` 写 `outline_feedback_chXXXX_roundN.json`，下一次 Outliner 带 `--review-feedback` 重写。
2. 整本大纲失败：`repair_outline_book_review` 把跨章问题分配到较晚章节，写 `outline_book_feedback_chXXXX_roundN.json`，再走单章大纲门。
3. 伏笔审计失败：`foreshadowing_audit.py` 阻断正文阶段，必要时对大纲补丁回收伏笔。
4. 正文失败：Draft Gate 汇总 Reviewer 反馈，同章 Writer 重写；章节之间不并发越过。
5. 整本终审关系失败：`relationship_repair_targets` 转成 `book_relationship_feedback_chXXXX_roundN.json`，清理目标章正文产物，断点续跑重写。
6. 整本终审烟火气失败：`human_warmth_streak` 转成 `book_human_warmth_feedback_chXXXX_roundN.json`，清理目标章正文产物，断点续跑重写。
7. 终审自动修复留痕：`repair_manifest.json` 记录所有目标章和 `resume_from_chapter`。

## 已有质量抓手

- 结构：`story_beat`、卷纲转折点对齐、连续注水 beat 检测。
- 连续性：前后章事实锚点、角色状态、成长弧线、人物称呼闭环。
- 人情连续性：`relationship_state` 抽取信任、亏欠、误会、承诺、照料行为和未说出口的话，并在下一章写作时注入。
- 伏笔：大纲层 `claimed` 与正文层 `resolved` 分离，Writer 回验后再真正回收。
- 去 AI 味：本地 `ai_flavor_detector` 检查排比、空泛形容词、套路微表情和总结式结尾。
- 整本质量：分段、卷级、终审上下文接入关键章节原文、伏笔台账和节奏重复信号。

## 关键缺口

- 大纲容易只写“事件链”，缺少人物为什么在乎这件事。
- 正文容易把冲突写成宏大危机，缺少饭钱、病痛、工作、家庭、邻里、体面等具体生活压力。
- 配角容易工具化，只负责递线索、解释设定或制造阻碍。
- 对白容易说明书化，把动机、背景和情绪一次说透，缺少潜台词。
- 爽点容易只有结果，没有人的反应、亏欠、误解、关系变化或回声。

## 本轮落地优化

- `planner.py`：新生成的 `characters.json` 角色档案增加 `life_profile`，记录家庭/谋生压力/旧债/软肋/日常习惯/不愿说出口的真话。
- `planner.py`：`characters.json` 本地契约新增 `life_profile` 完整性检查；模型漏填或旧文件缺字段时不再静默跳过，而是提示先运行 `backfill_humanity_fields.py`。
- `outliner.py`：在单章大纲契约中新增“烟火气与人情味”要求，强制设计生活压力、关系取舍、潜台词和生活物件。
- `outliner.py`：新增单章必填字段 `human_anchor`，把本章具体生活压力、关系牵挂、潜台词或生活物件结构化写入大纲。
- `outliner.py` / `writer.py`：角色摘要注入 `life_profile`，避免 Planner 生成的生活关系资料在章节生成时丢失。
- `outline_reviewer.py`：新增 `human_warmth` 设计门，进入正文前检查大纲是否具备人情味支点。
- `outline_reviewer.py`：审查清单点名 `human_anchor`，未通过时可用字段级 `edits` 定点修复。
- `outline_quality_gate.py`：把 `human_warmth` 纳入本地聚合硬门禁，不再只停留在提示词层。
- `writer.py`：正文契约要求每章写出具体生活压力、可触摸细节、潜台词对白、关系回声和非工具化配角。
- `writer.py`：把大纲 `human_anchor` 转成正文执行指令，要求写进现场而不是只用心理旁白解释。
- `relationship_state.py`：新增跨章关系欠账快照，记录谁欠谁、谁误会谁、谁照顾过谁、谁没把真话说出口。
- `relationship_state.py`：新增本地合并策略；当前章未覆盖的上一章未解决关系欠账会自动延续，已解决项可用 `resolved=true` 关闭。
- `relationship_state.py` / `writer.py`：从未解决关系欠账中挑选一条最高压力关系，转成“本章必须兑现的关系任务”，要求正文写出潜台词对白、照料/回避/补偿动作和关系变化结果。
- `coordinator.py` / `writer.py`：正文通过后并行抽取关系欠账，下一章写作前注入，要求冲突和爽点产生关系回声。
- `reviewer.py`：正文评分新增 `human_warmth` 维度，并把生活压力、潜台词、人的反应纳入核心审查清单。
- `reviewer.py`：新增本地 `human_warmth_detection`，检查正文是否兑现 `human_anchor`、是否有生活压力/关系牵挂/生活物件/足够互动；未通过时不得判“通过”。
- `reviewer.py`：新增本地 `relationship_obligation_detection`，复用上一章关系欠账选择器，检查本章是否写出关系对象、未解决压力、潜台词对白、照料/回避/补偿动作和关系变化结果；未通过时不得判“通过”。
- `reviewer.py`：新增本地 `origin_fact_reference_detection`，从 `origin/facts` 分组提取事实关键词和事实短句并统计正文命中率；会标记 `nominal_only_hit`，识别正文只借用人名/地名但没有兑现事实句的情况；当前为非阻断证据，用于判断章节是在遵守事实素材，还是只模仿了风格样本。
- `coordinator.py` / `draft_gate.py`：初稿失败反馈会读取 `human_warmth_detection`，把未兑现 `human_anchor`、缺生活压力、缺关系牵挂、缺生活物件或互动对白不足转成 Writer 下一轮的 `targeted_repairs`。
- `coordinator.py` / `draft_gate.py`：初稿失败反馈会读取 `relationship_obligation_detection`，把未兑现的关系硬任务转成 Writer 下一轮的 `targeted_repairs`。
- `coordinator.py` / `draft_gate.py`：初稿失败反馈会读取 `origin_fact_reference_detection.needs_attention`，把未命中 `origin/facts` 或只命中名词但没兑现事实短句的问题转成 Writer 下一轮的 `targeted_repairs`，要求至少让一个事实素材中的人物、地点、旧事或物件进入正文现场；若有 `missing_fact_clauses_sample`，优先要求 Writer 兑现具体事实短句。
- `writer.py`：读取失败反馈时会把最多 3 条 `targeted_repairs` 压成一条“必须同时完成”的修改建议，避免 human warmth、关系任务、origin/facts 同时失败时只有第一条进入下一轮 Prompt。
- `novel_config.py` / `outliner.py` / `writer.py`：新增 `build_origin_fact_directive()`，从 `origin/facts` 抽取少量事实线索和事实短句；Outliner 必须在 `key_events`、`summary` 或 `human_anchor` 中落地 1-2 条，Writer 必须把至少 1 条事实线索写成正文现场中的人物、地点、旧事、物件或关系动作。
- `book_reviewer.py`：整本终审接入 `relationship_states`，生成“人物温度与关系欠账轨迹”摘要，要求 emotional_resonance 评分引用关系欠账、人物反应和章节证据；本地扫描会输出 `trajectory_status_counts`、`relationship_trajectory_issues` 和 `sample_trajectories`，识别同一关系是否长期 carried_over、缺少照料/摊牌/收束动作。
- `book_reviewer.py`：终审 Markdown 报告新增“关系欠账与人物温度”章节，暴露关键关系是否只有欠账没有回声或收束。
- `book_reviewer.py`：终审 JSON 新增 `relationship_repair_targets`，把长期未收束的关系欠账转成目标章节、问题证据和验收标准；模型漏填时由 `relationship_states.long_open_threads` 与 `relationship_trajectory_issues` 自动兜底生成。
- `book_reviewer.py`：终审 Markdown 报告新增“关系线修复目标”和“本地关系轨迹问题”章节，方便人工或后续脚本把关系线问题回灌到大纲修复。
- `outline_gate.py` / `coordinator.py`：整本终审未通过时，可把 `relationship_repair_targets` 写成 `book_relationship_feedback_chXXXX_roundN.json`，复用 `process_outline_gate` 的整本反馈硬门槛修复目标章节大纲。
- `coordinator.py`：关系线终审修复会清理目标章节的 draft/review/final 与角色/成长/关系状态派生产物，断点续跑时重新写正文，避免只修大纲不刷新正文。
- `coordinator.py`：正文派生产物清理会追加写入 `logs/text_artifact_cleanup.jsonl`，记录章节、原因、每个 draft/review/final/状态文件是否存在、是否删除和失败原因；清理 review 与 relationship_states 时会保留人情味检测和关系欠账摘要作为修复前基线，`repair_manifest.json` 会引用该日志路径。
- `book_reviewer.py`：整本本地扫描新增 `human_warmth_streak`，检测连续多章缺少生活物件、问句对白或配角主动选择的趋势，并把“烟火气连续性本地扫描”注入终审上下文。
- `book_reviewer.py`：整本本地扫描新增 `human_warmth_exemplars`，按生活物件、问句对白、配角主动选择和互动密度给出“最有人味章节/最空泛章节”样例，附短摘录，并写入终审上下文与 Markdown 报告。
- `backfill_humanity_fields.py`：新增旧项目迁移脚本，默认 dry-run；可用 `--apply --backup` 为已有 `characters.json` 补 `life_profile`，为旧单章大纲补 `human_anchor`；可加 `--invalidate-outline-reviews` 删除被修改章节的旧大纲审查，强制重新过审；输出 `suggested_commands` 和 `suggested_actions`，给出最小章节范围的 Coordinator/Outline Reviewer 重跑命令、用途、风险差异、预计影响产物和预期输出，并根据 outline-first 配置与目标章节是否已有 draft/review/final 标记推荐动作；显式追加 `--run-recommended-action` 时，会执行 `recommended=true` 的动作并在结果中记录 `executed_action`，但默认 `--max-run-chapters=50` 会阻断过大范围，且默认只允许 `review_outline_only`；完整质量门可用 CLI `--allow-run-action rerun_quality_gate` 本次显式放行，也可通过 `config.json.backfill.allow_rerun_quality_gate_without_text_artifacts=true` 在目标章没有 draft/review/final 时条件放行；若配置级长期放行 `run_allowed_actions=["rerun_quality_gate"]`，有正文产物时仍会被拦截，除非显式设置 `allow_rerun_quality_gate_with_text_artifacts=true`。
- `portable_check.py`：迁移自检新增 `character_states`、`arc_states`、`relationship_states` 可写检查，并扫描旧项目是否缺 `life_profile` 或 `human_anchor`；发现缺口时给出带最小章节范围的 `backfill_humanity_fields.py` 建议命令；若发现旧 `outline.json` 或 `chapters/outline/index.json`，提示当前流程不会读取，避免误以为聚合大纲仍是事实源；同时读取现有 `reports/story_flow_audit.json`，缺失、不可解析、`overall_status` 非 ok 或报告早于关键输入产物时给出补跑只读审计命令。
- `novel_config.py`：`load_origin_materials()` 会按路径/文件名把 `origin/` 素材分成 facts/general/style 三组；`origin/facts|setting|world|characters|timeline|canon` 作为事实素材最高优先级，`origin/style|voice|samples|prose` 只约束语气、节奏和描写质感，避免风格样本覆盖设定事实。
- `outline_gate.py` / `coordinator.py`：整本终审未通过时，可把 `human_warmth_streak` 写成 `book_human_warmth_feedback_chXXXX_roundN.json`，分配到连续段后章，要求补生活物件、问句对白和配角主动选择，并触发目标章正文重写。
- `coordinator.py`：终审失败若生成关系线或烟火气修复目标，会写入 `reports/book_review/repair_manifest.json`，记录目标章节、终审分、建议续跑起点、反馈文件、清理产物、续跑后预期产物和复核命令；同时保存修复前 `local_full_scan.human_warmth_streak` 基线，方便人工复盘、dashboard 消费或修复后验证。
- `coordinator.py`：整本终审复核后会回写既有 `repair_manifest.json`，根据目标章 final/review、`relationship_states`、`human_warmth_detection` 和整本终审分生成 `verification.verified=true/false`、逐章验证结果；逐章结果包含修复前/后的 `human_warmth` 与 `relationship_state` 对照，并按人物关系 `pair` 输出欠账、误会、承诺、照料行为、后续压力和 resolved 的字段级 diff；整本结果包含 `book_scan_evidence.human_warmth_streaks` 的修复前/后趋势对照；若复核失败，会把失败检查转成 `verification.next_repair_candidates`，并为带章节号的候选写出 `book_verification_feedback_chXXXX_round1.json`，给下一轮精确修复或 dashboard 消费。
- `coordinator.py`：`book_reviewer.auto_resume_after_repair=true` 时，终审修复目标回灌并清理正文产物后，会按 `repair_manifest.resume_from_chapter/resume_to_chapter` 自动重跑正文质量门并强制复审整本；默认 `resume_to_chapter = 最晚目标章 + auto_resume_followup_chapters(默认3)`，避免从最早目标章一路跑到全书 end，若确需旧行为可显式配置 `auto_resume_to_total_chapters=true`；尝试记录写入 `repair_manifest.auto_resume_attempts`。
- `coordinator.py`：`book_reviewer.auto_retry_verification_feedback=true` 时，若自动续跑后的整本复核仍失败且 manifest 已生成 `book_verification_feedback_chXXXX_round1.json`，Coordinator 会逐章应用这些反馈重过大纲门、清理目标章正文产物、按同样的受限范围重跑正文质量门并强制复审整本；`max_verification_retry_cycles` 限制循环次数，尝试记录写入 `repair_manifest.verification_retry_attempts`，默认关闭。
- `story_flow_audit.py`：新增只读流程证据审计脚本，扫描 `life_profile`、`human_anchor`、大纲人情味门、正文 `human_warmth_detection`、`relationship_obligation_detection`、`origin_fact_reference_detection`、`relationship_states` 与整本终审修复清单，写出 `reports/story_flow_audit.json`、`overall_status`、逐阶段 `stage_statuses` 和建议命令；`gaps` 会吸收所有非 ok 阶段 label，避免出现 `overall_status=fail` 但缺口为空。
- `coordinator.py`：总结报告生成后会自动运行 `story_flow_audit.py`；终审失败的诊断路径也会生成同一份只读审计报告，失败不阻断主流程。
- `push_notifier.py` / `wechat_notify.py` / `wechat_pusher_lane.py`：企业微信进度推送会读取现有 `story_flow_audit.json`；当 `overall_status` 非 ok 时追加一行审计摘要，列出最多 3 个非 ok 阶段标签；当报告早于关键输入产物时提示 stale，让运维通知不只显示章节数，也显示流程证据缺口或审计报告过期。

## 后续优化点

1. Dashboard/阅读器可展示关系欠账轨迹，辅助人工复核人物温度是否断线。
2. 可让 dashboard 读取 `story_flow_audit.json` 与 `repair_manifest.json`，展示“证据缺口→终审发现问题→回灌大纲→等待正文重写”的链路状态。
3. 可把 `origin/facts` 的事实短句检测升级为更深的语义事实匹配，识别同义改写、反事实偏移和“表面命中多个词但因果关系被改写”的情况。
4. 可把关系轨迹扫描从字段变化升级为更深的语义同义识别，避免“换词复述同一笔欠账”被误判为新状态。

## 优化分层

生成前：
- 旧项目迁移后应使用 `--invalidate-outline-reviews` 或手动重跑单章大纲门，否则旧 outline review 可能早于 `human_anchor` 补丁。
- Planner 的 `life_profile` 已有基础本地契约，后续可继续加强语义检测，识别更隐蔽的空泛人物关系和模板化生活压力。
- `origin/` 已支持 facts/general/style 分组，Outliner/Writer 会在生成前显式注入短事实线索和事实短句并要求章节落地，Reviewer 会输出 `origin_fact_reference_detection` 并能回灌 Writer 定向修复；后续可把事实短句命中升级为语义事实一致性检测。

生成中：
- Writer 已从 `relationship_state` 中选取一条未解决关系作为本章硬性任务；后续可让 Draft Gate 对这条任务做更精确的文本兑现检测。
- Draft Gate 已把 `human_warmth_detection` 和 `relationship_obligation_detection` 的失败类型转成更精确的 Writer 反馈；后续可继续把这些反馈与最终正文 diff 绑定，判断修复是否真的改善。
- 关系线和烟火气终审修复已有默认关闭的自动续跑开关；启用时默认按目标章到少量跟随章节的受限范围续跑，并在 `repair_manifest.resume_range_basis` 中记录依据，避免长篇项目反复大范围重写。

生成后：
- Book Reviewer/Coordinator 已能把修复前清理快照、重写后状态、关系 `pair_diffs` 和整本 `human_warmth_streak` 前后趋势写入 `repair_manifest.json.verification`；Book Reviewer 已能输出跨章 `relationship_trajectory_issues`，后续可继续做更细的语义同义 diff。
- `repair_manifest.json` 已记录每个目标章的反馈文件路径、清理产物列表、机器可判定的 `verified=true/false`、验证失败时的 `next_repair_candidates` 和已生成的下一轮反馈文件；默认关闭的 `auto_retry_verification_feedback` 可将这些反馈文件接入受限续跑。
- 终审报告已增加“最有人味章节/最空泛章节”样例和短摘录；后续可让人工标注样例是否准确，反向校准本地信号权重。

可视化复盘：
- Dashboard 可读取 `relationship_states` 画出人物关系欠账时间线。
- Dashboard 可读取 `repair_manifest.json` 标出待重写章节、原因、反馈文件和重写后状态。
- 阅读器可为人工审稿增加“烟火气信号”标注：生活物件、问句对白、配角主动选择、关系回声。

## 判断标准

一章合格的“人情味”不是多写抒情，而是读者能回答：

- 主角这章怕失去谁、欠了谁、想保护谁，或者怕谁失望？
- 这场冲突落到现实处境后，会影响哪顿饭、哪笔钱、哪段关系、哪点尊严？
- 有没有一句话表面上说的是别的，实际是在求助、告别、试探或认输？
- 事件结束后，谁松了一口气，谁更亏欠，谁改变了看法，谁被伤到了？
