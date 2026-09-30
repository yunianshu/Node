# 我的武侠梦

基于 `scripts/` 自动化框架生成的中文长篇网络小说项目。多 Agent 流水线:**Planner 建世界观 → Outliner 逐章大纲(大纲门)→ Writer 逐章正文(正文门)→ 整本终审**。全程由 `coordinator.py` 串行协调,跨章一致性靠状态快照 + 伏笔/关系台账维持。

> 本仓库**不依赖任何 skill 封装**。生成引擎是 `scripts/` 下的独立 Python 程序,直接用命令行驱动即可。质量阈值与流程开关全部在 `projects/<book_id>/config.json`,不硬编码;流程/目录的事实来源是代码本身,而非任何外部文档。

---

## 一、目录结构

每个项目位于 `projects/<book_id>/`,由 `scripts/core/novel_config.py::ensure_project_structure()` 创建:

```text
projects/<book_id>/
├── config.json                          # 总章数/模型/各门阈值/webhook
├── premise.txt                          # 故事前提/设定种子
├── origin/                              # 高优先级参考素材(facts/style/旧文)
│
├── world.json              [Planner]    # 世界观 + media_prompts
├── characters.json         [Planner]    # 角色档案(life_profile/语言指纹/弧线)
├── volume_outline.json     [Outliner]   # 分卷规划(volumes[])
│
├── chapters/
│   ├── outline/chapter_XXXX.json        # 单章大纲(一章一文件,禁止根目录 outline.json)
│   ├── outline_review/chapter_XXXX_review.json  # 大纲审查(分数+设计门五项)
│   ├── draft/chapter_XXXX.txt           # 初稿(+ _best 兜底 + _polish_N 候选)
│   ├── review/chapter_XXXX_review.json  # 初稿审查(16维分+文本 edits)
│   ├── final/chapter_XXXX.txt           # 终稿(promote 产出)
│   ├── character_states/chapter_XXXX.json   # 角色快照 → 注入下章 writer
│   ├── arc_states/chapter_XXXX.json         # 成长弧线阶段
│   └── relationship_states/chapter_XXXX.json# 关系欠账(亏欠/误会/承诺/照料)
│
├── media/                               # 封面/世界观视频/主题曲
│   ├── images/  videos/  music/  audio/
│
├── reports/
│   ├── progress.json                    # 断点续传进度(由产物扫描刷新)
│   ├── summary_report.json              # 全书总结
│   ├── foreshadowing_ledger.json        # 伏笔台账(planted/claimed/resolved/dangling)
│   ├── outline_memory.json              # 远程压缩记忆(里程碑+近8章)
│   ├── book_review/                     # 整本终审 + repair_manifest
│   └── story_flow_audit.json            # 确定性流程体检
│
└── logs/
    ├── coordinator.log
    ├── outline_candidates/  draft_candidates/   # 历史候选目录(已停用,仅存档)
    ├── outline_feedback_*.json  draft_feedback_*.json  # 失败反馈 → --review-feedback
    └── *watchdog.{log,lock}             # 守护进程单例
```

**硬约束:** 不生成根目录 `outline.json`;大纲一律 `chapters/outline/chapter_XXXX.json`(一章一文件);完成数来自单章文件扫描,不信任 `progress.json`。

---

## 二、生成流程

```text
config/premise → Planner → world.json + characters.json (+ media)
                          ↓
   逐章正文循环 run_serial_quality_workflow(第 N 章):
     ensure_outline_lookahead  → 先让 N..N+9 章大纲过门
     大纲门  process_outline_gate   → outline + outline_review(不达标→意见定点修订)
     正文门  process_draft_gate     → draft + review (+ polisher) → final(不达标→原稿+意见局部修订)
     G14 终稿 AI硬门复检（失败立即停止）
     状态抽取(三路并行)            → character/arc/relationship states
   全书末章 → G16 结尾收束检查(伏笔回收率 + 弧线终点)
                          ↓
   整本终审 book_reviewer(segment 10章 → volume 50章 → final)
     不达标 → 关系线/烟火气修复回灌大纲 → 清正文 → 断点续跑重写
                          ↓
   总结报告 + story_flow_audit + 微信推送「完成」
```

### 大纲门(`scripts/pipeline/outline_gate.py`)
- 轮次与每轮尝试数由配置决定（默认 `2 轮 × 1 次`）;每次 `outliner 修订 → outline_reviewer 审查`
- **意见修订**:不达标时保留原大纲,反馈写 `logs/outline_feedback_*.json`,下次带 `--review-feedback` 做定点修订(字段级 edits 优先,模型修订兜底),不推倒重生成;累计 ≥8 次启用 `--rescue`
- 三层质量门:首轮初筛(分 < screening_score 或设计门不过直接停)→ 多轮投票(中位分 + 票数)
- **设计硬门**:core_desire / scene_design；允许日常、过渡、单场景和安静收尾，反转/烟火气/强钩子不再是每章配额

### 正文门(`scripts/pipeline/draft_gate.py`)
- 轮次与每轮尝试数由配置决定（默认 `2 轮 × 1 次`）;每次 `writer 修订 → reviewer 评分`
- **意见修订**:只应用审查定位范围内的精确 JSON 补丁，默认修改量不超过原稿15%；失败保留原稿并报告，不回退全文重写
- **分数门槛**:1–3 章用 `golden_chapter_min_score`(默认 9.0),其余 `quality.review_min_score`(默认 8.5)
- **Polisher**:默认关闭；已有项目显式开启时也只接受精确定位、限制修改量的补丁
- **本地硬门**:字数 / 角色在场。AI指定指纹与低于7分检测硬阻断；人情味、关系、场景与origin词匹配保留观察提示；Reviewer阅读全文，事实冲突仍需修订
- 全程保存 `_best` 历史最高分稿,恢复后必须重审，不能按历史分数直接放行；通过全部质量门才 promote 到 `final/`

---

新版写作约束、已有配置兼容与验证方式见 [小说生成与局部修订调整](docs/narrative-revision.md)。已有正文与报告不会自动重写。

## 三、直接驱动命令

> 不经过任何 skill,直接用 `scripts/` 命令。在仓库根目录执行。

```bash
# 完整生成 / 断点续传(默认10章大纲提前窗口 + 整本终审)
python scripts/pipeline/coordinator.py --project "projects/<book_id>"

# 指定章节范围
python scripts/pipeline/coordinator.py --project "projects/<book_id>" --start N --end M

# 单步定位故障(绕开协调器)
python scripts/pipeline/planner.py          --project "projects/<book_id>"            # 世界+角色
python scripts/pipeline/outliner.py         --project "projects/<book_id>" --start N --end M
python scripts/pipeline/outline_reviewer.py --project "projects/<book_id>" --start N --end M
python scripts/pipeline/writer.py           --project "projects/<book_id>" --chapter N
python scripts/pipeline/reviewer.py         --project "projects/<book_id>" --chapter N
python scripts/maintenance/book_reviewer.py         --project "projects/<book_id>"   # 整本终审

# 接手/迁移前自检
python scripts/maintenance/portable_check.py --project "projects/<book_id>"
```

常用 `coordinator.py` 开关:`--skip-planner`(复用世界/角色)、`--outline-lookahead`(大纲提前窗口，默认10章)、`--force-book-review`(忽略终审缓存)、`--skip-book-review`(仅调试)。

改完代码必跑语法校验:
```bash
python -m py_compile scripts/pipeline/planner.py scripts/pipeline/outliner.py scripts/pipeline/outline_reviewer.py scripts/pipeline/coordinator.py scripts/pipeline/writer.py scripts/pipeline/reviewer.py scripts/core/novel_config.py scripts/core/workflow_state.py
```

---

## 四、配置要点(`projects/<book_id>/config.json`)

```json
{
  "total_chapters": 2000,
  "llm": { "provider": "deepseek", "model": "deepseek-chat" },
  "mmx_path": "mmx",
  "coordinator": {
    "outline_lookahead_chapters": 10,
    "outline_attempts_per_round": 1,
    "outline_analysis_rounds": 2,
    "draft_attempts_per_round": 1,
    "draft_analysis_rounds": 2,
    "push_interval_seconds": 120
  },
  "outline_reviewer": { "provider": "glm", "model": "glm-4.6", "min_score": 8.5 },
  "reviewer":         { "provider": "glm", "model": "glm-4.6", "min_score": 8.5, "golden_chapter_min_score": 9.0 },
  "polisher":         { "enabled": false, "threshold": 8.0 },
  "writer":           { "use_scored_examples": false },
  "revision":         { "max_changed_ratio": 0.15 },
  "book_reviewer":    { "enabled": true, "min_score": 8.5, "required_on_finish": true },
  "quality": { "review_min_score": 8.5, "min_chapter_words": 5000, "max_chapter_words": 12000 },
  "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>"
}
```

- 阈值开关一律走 config,**不在代码或命令里硬编码分数**。
- webhook 优先写 `config.json`,也可用 `NOVEL_WEBHOOK_URL` 临时覆盖。
- 项目路径用 `--project` 或 `NOVEL_PROJECT_DIR`,不硬编码本地路径。

---

## 五、跨章一致性机制(整本评分主战场)

| 机制 | 文件 | 作用 |
|------|------|------|
| 伏笔台账 | `reports/foreshadowing_ledger.json` | planted→claimed(大纲标收)→resolved(正文真兑现)→dangling;防「大纲写回收、正文没写」假回收 |
| 角色状态 | `chapters/character_states/` | 跨章不矛盾(生死/位置/伤势/已知信息) |
| 成长弧线 | `chapters/arc_states/` | 主角六阶段,只能推进不倒退 |
| 关系欠账 | `chapters/relationship_states/` | 记录跨章关系状态,按当前场景相关性使用,不要求每章推进 |
| 远程记忆 | `reports/outline_memory.json` | 防 outliner 主线漂移 |

正文过门后三路并行抽取(character/arc/relationship)快照,注入下一章 writer。

---

## 六、守护进程

Coordinator 启动时自动拉起三个独立旁路进程(只读,不参与质量门、不持生成锁):

- `scripts/maintenance/wechat_pusher_lane.py` — 周期推送企业微信进度(单例锁 `logs/wechat_pusher.lock`)
- `scripts/maintenance/gate_watchdog.py --mode outline` — 监控大纲卡章
- `scripts/maintenance/gate_watchdog.py --mode draft` — 监控正文卡章

长篇续跑也支持三 lane 并行:`outline_lane`(大纲)、`draft_lane`(初稿)、`wechat_pusher_lane`(推送)。

## 发布契约与旧入口兼容

- 文本模型使用各 Agent 的 `provider/model`（如 `writer.provider/model`、`reviewer.provider/model`），共享 `llm` 仅作回退；mmx 仅负责媒体。加载配置仍校验生成端与审查端的 `(provider, model)` 不得相同。
- 默认大纲提前窗口为10章；默认大纲与正文各为 `2轮 × 1次`。已有项目显式配置优先；默认不启用赛马。
- Reviewer 仅当报告 `status=completed` 返回0，包括有效低分、`需修改/需重写`；API失败、解析失败、无正文、契约无效（含 `invalid_review_salvaged`）返回非零。批量中任何一章执行失败即非零；进程成功不代表质量放行，高分也不会将修订结论改成通过。
- 报告 `content_sha256` 绑定实际送审正文快照（UTF-8，保留换行），模型返回后不重新读取正文计算。正文变化或旧报告无摘要必须重审；发布核对最终正文摘要一致。
- 恢复历史最高分稿后必须强制重审，包括 Polisher失败、评分回退与重试耗尽。只有 completed、通过、章节阈值、报告质量门、本地硬门与摘要一致才允许发布；抢救出的无效报告不得成为发布依据。
- Reviewer 和终稿共用 AI硬门：低于7分拒绝，7.0且无硬门指纹保持通过语义；指定指纹无论高分仍拒绝，包含 parallel_sentiment、summary_ending、meta_narration 和原有七类指纹。检测异常或分数缺失/非法均拒绝。
- 写入 final 前检查正文 AI硬门，失败不覆盖已有终稿；落盘后复检失败立即停止，不抽取跨章状态、不推进完成进度、不推送完成通知。Coordinator 生成、质量门或整本终审失败均返回非零。
- 根目录 `_gen_serial.py` 仅转发 `scripts/maintenance/gen_serial.py`，该脚本通过真实子进程运行 Coordinator；watchdog 每次重启也走此路径并保留失败退出码。watchdog 的 `--once` 表示监督一次完整子进程执行；不会结束后假报成功，也不再按 final 文件数判定质量通过。
- 旧 `--start/--end/--skip-planner` 转发；start/end为0使用 Coordinator 默认范围，从第1章检查并续跑。`--max-rounds` 映射正文分析轮数（每轮尝试数仍读配置）。`--candidates/--workers` 仅接受1并明确提示；其它值、显式 `--timeout`、非0 `--time-limit` 明确拒绝。超时应配置各 Agent 的 `timeout_seconds`，不能静默变更旧语义。
- 测试项目可设置 `coordinator.background_monitors_enabled=false` 禁止旁路监控进程，并清空 webhook、关闭 media；正式项目默认保留监控。

本地验证（Python 3.12+，先确认标准库可加载；Windows 设置 `$env:PYTHONPATH="<repo>/scripts"`）：

```powershell
python -c "import sys, encodings; print(sys.version)"
python -m pytest tests scripts/tests -q
python -m compileall -q scripts
python scripts/pipeline/coordinator.py --help
python _gen_serial.py --help
python scripts/maintenance/gen_serial_watchdog.py --help
```

CLI固定响应测试包含复制隔离项目的单章冒烟，验证实际子进程、HTTP模型通道、失败退出码与终稿绑定；它不代表真实模型质量验证。真实模型冒烟只能在复制测试项目运行，记录实际 provider/model、退出码、报告状态与终稿结果；缺凭据时明确记录未完成。
