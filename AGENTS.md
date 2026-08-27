# Repository Guidelines

## Project Structure & Module Organization

This repository contains a streamlined novel-generation automation stack.

- `scripts/core/`: shared utilities for config loading, generic multi-provider LLM calls (`llm_client.py`, OpenAI-compatible; review agents must use a different model than draft generators), media CLI access (`mmx_cli.py`), push notifications, JSON repair, and workflow state.
- `scripts/pipeline/`: main generation agents: `planner.py`, `media_generator.py`, `outliner.py`, `outline_reviewer.py`, `writer.py`, `reviewer.py`, and `coordinator.py`.
- `scripts/maintenance/`: operational helpers, including `coordinator_watchdog.py` and `wechat_notify.py`.
- `projects/`: generated novel projects and content. Treat chapter files, outlines, reviews, finals, media assets, and project configs as user data.
- Novel media belongs under `projects/<book_id>/media/`: covers and visual assets in `images/`, worldbuilding-based videos in `videos/`, audio in `audio/`, and music in `music/`. Planner must write media prompts to `world.json.media_prompts`; `media_generator.py` turns them into cover, video, and theme song assets.
- `projects/<book_id>/origin/`: source/reference material. When files exist, generation and review code must treat them as high-priority inputs.
- `doc/`: documentation and workflow notes.
- `novels-dashboard/`: dashboard/frontend assets, if used separately.

## Build, Test, and Development Commands

Run commands from the repository root:

```powershell
python -m py_compile "scripts/pipeline/planner.py" "scripts/pipeline/media_generator.py" "scripts/pipeline/outliner.py" "scripts/pipeline/outline_reviewer.py" "scripts/pipeline/coordinator.py" "scripts/pipeline/writer.py" "scripts/pipeline/reviewer.py" "scripts/core/json_repair.py" "scripts/core/llm_client.py" "scripts/core/review_ai_client.py" "scripts/core/mmx_cli.py" "scripts/core/novel_config.py" "scripts/core/push_notifier.py" "scripts/core/workflow_state.py" "scripts/core/outline_quality_gate.py" "scripts/core/foreshadowing_ledger.py" "scripts/core/outline_memory.py" "scripts/core/character_state.py" "scripts/core/arc_state.py" "scripts/core/relationship_state.py" "scripts/maintenance/coordinator_watchdog.py" "scripts/maintenance/wechat_notify.py" "scripts/maintenance/outline_lane.py" "scripts/maintenance/draft_lane.py" "scripts/maintenance/wechat_pusher_lane.py" "scripts/maintenance/gate_watchdog.py" "scripts/maintenance/portable_check.py" "scripts/maintenance/backfill_humanity_fields.py" "scripts/maintenance/story_flow_audit.py" "scripts/maintenance/book_reviewer.py"
```

Validate syntax for the active Python workflow.

```powershell
python "scripts/pipeline/coordinator.py" --project "D:/AiProject/Node/projects/<book_id>"
```

Run the full single-chapter quality-gated workflow.

```powershell
python "scripts/maintenance/wechat_notify.py" --project "D:/AiProject/Node/projects/<book_id>"
```

Send a progress notification.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code, 4-space indentation, and `pathlib.Path` for filesystem paths. Keep CLI arguments explicit and project-relative through `--project` or `NOVEL_PROJECT_DIR`; do not hardcode local project paths except in examples. Use `chapter_XXXX` naming for chapter artifacts.

## Change Boundaries

Workflow and generation logic must be changed only under `scripts/`. Novel reader or dashboard changes must be made only under `novels-dashboard/`. Do not implement workflow behavior in the dashboard, and do not place reader UI code under `scripts/`.

## Testing Guidelines

There is currently no retained test suite in `scripts/`. Before changes, run `py_compile` on all active scripts. For behavior changes, test against a small chapter range with `coordinator.py --start N --end M`.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commits style: `feat(scope): ...`, `fix(scope): ...`, or `chore: ...`. PRs should describe workflow impact, list validation commands, and call out any changes to `projects/` content or Enterprise WeChat configuration.

## Security & Configuration Tips

Store Enterprise WeChat webhook URLs in `projects/<book_id>/config.json` or `NOVEL_WEBHOOK_URL`. Do not hardcode secrets in shared scripts. Avoid deleting or rewriting `projects/` content unless explicitly requested.
