# ExecPlan：按小说生成流程重组脚本目录

## Goal

将 `scripts/` 的工作流实现按实际执行顺序组织，让开发者能从目录直接读出“规划 → 大纲 → 写作 → 审稿 → 编排”的链路；所有运行入口、后台守护、恢复建议和文档同步到新路径，且现有命令在迁移期继续可用。

## Non-goals

- 不修改生成策略、质量门禁、模型配置、项目正文或 `projects/` 下任何用户数据。
- 不删除旧路径入口；删除兼容入口属于破坏性变更，需在迁移稳定后单独确认。
- 不触碰当前无关的 `.arts/settings.json` 修改。
- 不重写历史设计文档中用于描述“当时路径”的内容，仅更新当前 README、AGENTS 和可执行运行提示。

## Open Questions

- 默认采用“新目录为正式实现 + `scripts/pipeline/`、`scripts/maintenance/` 保留薄兼容入口”的安全迁移。若你希望直接删除旧入口、强制所有调用方立即切换，请明确说明；那会是不可兼容的文件删除操作。

## Proposed Changes

### 目标目录

```text
scripts/
  core/                         # 跨阶段配置、LLM、状态、质量与文本工具
  workflow/
    step_01_planning/            # planner.py、media_generator.py
    step_02_outlining/           # outliner.py、outline_reviewer.py、outline_gate.py
    step_03_writing/             # writer.py
    step_04_reviewing/           # reviewer.py、polisher.py、draft_gate.py
    orchestration/               # coordinator.py
  operations/                    # 后台 lane、watchdog、审计、回填、通知、导出与本地工具
  pipeline/                      # 旧 CLI 路径兼容入口，不承载业务实现
  maintenance/                   # 旧 CLI 路径兼容入口，不承载业务实现
  tests/
```

### 正式实现迁移

- 将 `scripts/pipeline/` 的 10 个工作流模块迁入对应 `scripts/workflow/` 子包；新路径中的模块保留实际实现。
- 将 `scripts/maintenance/` 的运行时脚本迁入 `scripts/operations/`，按 `lanes/`、`watchdogs/`、`audits/`、`tools/` 分类；归档/非运行脚本保持原样或由清单明确处理。
- 新建必要的 `__init__.py`，并修正移动后 `TOOLS_ROOT` 推导、`sys.path` 注入、包内 import、Coordinator 的子脚本注册表。

### 兼容与调用方更新

- 将原 `pipeline/*.py`、`maintenance/*.py` 替换为仅转调新模块 `main()` 的薄入口，保留原 CLI 参数与退出码。
- 将 `_gen_serial.py`、看门狗、恢复/审计建议命令、测试 import、README、AGENTS 的当前路径替换为正式新路径。
- 历史文档不改写；所有实际执行路径不再依赖兼容入口。

## Step-by-step Implementation

1. 建立 `workflow/`、`operations/` 及其子包，按目标树移动正式实现；对当前未提交质量门禁修改采用纯路径迁移，不覆盖或回滚内容。
2. 更新每个移动模块的根路径推导、跨模块 import、Coordinator 脚本表以及在源码中硬编码的命令建议。
3. 新建旧路径的薄兼容入口，并为每个入口保留原有 `python scripts/...` 调用方式。
4. 更新当前 AGENTS、README、自动串行脚本、运行时看门狗、lane、审计/回填脚本和测试 import，使新路径成为唯一正式依赖。
5. 用全量 `py_compile`、各正式主入口 `--help`、各旧兼容入口 `--help` 验证模块可导入和 CLI 参数转发；针对 Coordinator 做不触发生成的 `--help` 验证。
6. 搜索运行时代码与当前文档，确认不再残留对旧实现目录的依赖；将迁移映射、验证结果和回滚方式写回本计划。

## Tests & Acceptance Criteria

- 所有正式工作流模块位于 `scripts/workflow/` 的正确阶段目录，所有运营脚本位于 `scripts/operations/` 分类目录。
- `scripts/pipeline/` 与 `scripts/maintenance/` 仅保留兼容入口；不包含工作流业务实现。
- `scripts/core/` 保持为跨阶段共享模块，`scripts/tests/` 不随生产代码迁移。
- `_gen_serial.py`、Coordinator、watchdog、lane、审计/回填命令和当前 README/AGENTS 使用新路径。
- 正式入口和旧入口的 `--help` 都返回成功；全量 `py_compile` 与现有质量门禁隔离测试通过。
- 不创建、删除或改写 `projects/` 内任何产物；不覆盖当前未提交的质量门禁改动和 `.arts/settings.json`。

## Risks & Rollback

- 风险：移动后 `__file__` 层级变化，导致 `scripts` 不在 `sys.path`、子进程找不到脚本或 Windows 路径失效。通过每个入口 `--help` 与 Coordinator 注册表检查控制。
- 风险：旧路径被外部任务调用。以薄兼容入口保留 CLI 合约，不在本次删除旧路径。
- 风险：未提交的质量门禁修改与移动操作交织。实施时只移动已核对的文件内容，不执行 reset、checkout 或覆盖写回。
- 回滚：恢复正式模块至原目录，并将兼容入口替换为原实现；不涉及项目数据。

## 确认前约束

本计划确认前，不移动、删除或重命名业务文件，不修改 import、文档或运行命令；仅保留本计划和只读诊断结果。
