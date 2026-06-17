#!/usr/bin/env python3
"""小说生成实时进度看板。

启动方式：
    python scripts/maintenance/progress_dashboard.py --project projects/<book_id> --port 8080

功能：
- 实时显示项目整体进度
- 章节网格状态（大纲/大纲审/初稿/审查/终稿）
- 最近评分与失败章节
- 尾部日志实时推送
- 预计剩余时间
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
import uvicorn

TOOLS_ROOT = Path(__file__).resolve().parents[2]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
if str(TOOLS_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT / "scripts"))

from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import (
    outline_dir,
    outline_review_dir,
    review_dir,
    scan_chapter_status,
)

app = FastAPI(title="Novel Gen Dashboard")
PROJECT_DIR: Path | None = None
TOTAL_CHAPTERS: int = 0
TITLE: str = "小说生成看板"


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{title}}</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; --muted:#94a3b8; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444; --info:#3b82f6; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--text); }
  header { padding:1rem 1.5rem; background:#0b1220; border-bottom:1px solid #334155; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:1rem; }
  h1 { margin:0; font-size:1.4rem; }
  .subtitle { color:var(--muted); font-size:.85rem; }
  main { padding:1.5rem; display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:1.5rem; }
  .card { background:var(--panel); border-radius:.75rem; padding:1rem; border:1px solid #334155; }
  .card h2 { margin:0 0 .75rem 0; font-size:1rem; color:#cbd5e1; }
  .metric { display:flex; justify-content:space-between; padding:.5rem 0; border-bottom:1px solid #334155; }
  .metric:last-child { border-bottom:none; }
  .metric .value { font-weight:600; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(2.2rem, 1fr)); gap:.35rem; }
  .cell { aspect-ratio:1; display:flex; align-items:center; justify-content:center; border-radius:.35rem; font-size:.7rem; font-weight:700; cursor:default; }
  .cell.draft { background:#334155; color:var(--muted); }
  .cell.outline_ok { background:#14532d; color:var(--ok); }
  .cell.draft_ok { background:#1e3a8a; color:#93c5fd; }
  .cell.review_ok { background:var(--ok); color:#052e16; }
  .cell.fail { background:var(--err); color:#fff; }
  .legend { display:flex; flex-wrap:wrap; gap:.75rem; margin-top:.75rem; font-size:.75rem; color:var(--muted); }
  .legend span { display:flex; align-items:center; gap:.3rem; }
  .dot { width:.7rem; height:.7rem; border-radius:.15rem; }
  #logs { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:.78rem; line-height:1.5; max-height:24rem; overflow:auto; white-space:pre-wrap; color:#cbd5e1; }
  .log-line { border-bottom:1px solid #334155; padding:.15rem 0; }
  .status { display:inline-flex; align-items:center; gap:.4rem; padding:.25rem .6rem; border-radius:999px; font-size:.8rem; background:#334155; }
  .status.running { background:#14532d; color:var(--ok); }
  .status.idle { background:#451a03; color:var(--warn); }
</style>
</head>
<body>
<header>
  <div>
    <h1>📖 {{title}}</h1>
    <div class="subtitle">项目: {{project_dir}} | 总章节: {{total_chapters}} | <span id="conn" class="status idle">连接中...</span></div>
  </div>
  <div class="subtitle" id="clock"></div>
</header>
<main>
  <div class="card">
    <h2>整体进度</h2>
    <div id="metrics"></div>
  </div>
  <div class="card">
    <h2>最近评分 / 失败</h2>
    <div id="recent"></div>
  </div>
  <div class="card" style="grid-column:1/-1;">
    <h2>章节状态网格</h2>
    <div id="grid" class="grid"></div>
    <div class="legend">
      <span><div class="dot" style="background:#334155"></div>无/草稿</span>
      <span><div class="dot" style="background:#14532d"></div>大纲过审</span>
      <span><div class="dot" style="background:#1e3a8a"></div>初稿完成</span>
      <span><div class="dot" style="background:#22c55e"></div>审查通过</span>
      <span><div class="dot" style="background:#ef4444"></div>失败/需重写</span>
    </div>
  </div>
  <div class="card" style="grid-column:1/-1;">
    <h2>实时日志</h2>
    <div id="logs">等待日志...</div>
  </div>
</main>
<script>
const total = {{total_chapters}};
const evtSource = new EventSource('/events');
const logsEl = document.getElementById('logs');
const metricsEl = document.getElementById('metrics');
const gridEl = document.getElementById('grid');
const recentEl = document.getElementById('recent');
const connEl = document.getElementById('conn');

function pad(n){ return String(n).padStart(4,'0'); }
function fmtPct(a,b){ return b ? (a/b*100).toFixed(1)+'%' : '0%'; }

function render(state){
  const p = state.progress;
  const m = [
    ['📋 大纲完成', p.outline_done+'/'+total, fmtPct(p.outline_done,total)],
    ['🔍 大纲审查通过', p.outline_review_ok+'/'+total, fmtPct(p.outline_review_ok,total)],
    ['✍ 初稿完成', p.draft_done+'/'+total, fmtPct(p.draft_done,total)],
    ['🔍 初稿审查通过', p.review_ok+'/'+total, fmtPct(p.review_ok,total)],
    ['📤 终稿完成', p.final_done+'/'+total, fmtPct(p.final_done,total)],
    ['⏱ 预计剩余', p.eta, ''],
    ['🤖 活跃进程', p.active_processes, ''],
  ];
  metricsEl.innerHTML = m.map(([k,v,pct]) => `<div class="metric"><span>${k}</span><span class="value">${v}${pct?' <span style="color:var(--muted);font-weight:400">'+pct+'</span>':''}</span></div>`).join('');

  gridEl.innerHTML = '';
  for(let i=1;i<=total;i++){
    const st = state.chapters[i] || {};
    const el = document.createElement('div');
    el.className = 'cell ' + (st.review_ok?'review_ok':st.draft_ok?'draft_ok':st.outline_review_ok?'outline_ok':st.fail?'fail':'draft');
    el.textContent = i;
    el.title = `第${i}章\n` + JSON.stringify(st, null, 2);
    gridEl.appendChild(el);
  }

  const recent = (state.recent_scores || []).slice(0, 10);
  recentEl.innerHTML = recent.length ? recent.map(r => `<div class="metric"><span>第${r.chapter}章</span><span class="value" style="color:${r.score>=9?'var(--ok)':r.score>=8?'#93c5fd':'var(--warn)'}"}>${r.score} ${r.verdict}</span></div>`).join('')
    : '<div style="color:var(--muted)">暂无评分数据</div>';
}

evtSource.onmessage = (e) => {
  try {
    const data = JSON.parse(e.data);
    if(data.type === 'state') render(data.payload);
    if(data.type === 'log'){
      const line = document.createElement('div');
      line.className = 'log-line';
      line.textContent = data.text;
      logsEl.appendChild(line);
      logsEl.scrollTop = logsEl.scrollHeight;
      while(logsEl.children.length > 300) logsEl.removeChild(logsEl.firstChild);
    }
    connEl.textContent = '实时连接';
    connEl.className = 'status running';
  } catch(err) { console.error(err); }
};
evtSource.onerror = () => {
  connEl.textContent = '连接断开';
  connEl.className = 'status idle';
};

setInterval(() => {
  document.getElementById('clock').textContent = new Date().toLocaleString('zh-CN');
}, 1000);
</script>
</body>
</html>
"""


def _load_progress(project_dir: Path) -> dict:
    progress_file = project_dir / "reports" / "progress.json"
    if progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_world_title(project_dir: Path) -> str:
    world_file = project_dir / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                return json.load(f).get("title", "本小说")
        except Exception:
            pass
    return "本小说"


def _count_files(directory: Path, pattern: str = "*") -> int:
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


def _latest_log_lines(project_dir: Path, n: int = 50) -> list[str]:
    log_dir = project_dir / "logs"
    if not log_dir.exists():
        return []
    logs = sorted(
        [p for p in log_dir.rglob("*.log") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not logs:
        return []
    try:
        with open(logs[0], "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip() for line in lines[-n:]]
    except Exception:
        return []


def build_state(project_dir: Path, total: int) -> dict:
    statuses = scan_chapter_status(project_dir, 1, total, use_cache=False)
    outline_done = _count_files(outline_dir(project_dir), "chapter_*.json")
    outline_review_ok = sum(
        1 for s in statuses.values()
        if s.outline_review_ok or s.outline_review_status == "ok"
    )
    draft_done = _count_files(project_dir / "chapters" / "draft", "chapter_*.txt")
    review_ok = sum(1 for s in statuses.values() if s.review_ok)
    final_done = _count_files(project_dir / "chapters" / "final", "chapter_*.txt")

    recent_scores = []
    for ch, s in statuses.items():
        score = s.review_score
        if score is not None:
            try:
                recent_scores.append({
                    "chapter": ch,
                    "score": float(score),
                    "verdict": s.review_status or "",
                })
            except Exception:
                pass
    recent_scores.sort(key=lambda x: x["chapter"], reverse=True)

    # 估算剩余时间（非常粗略：基于最近完成速度）
    progress = _load_progress(project_dir)
    eta = "N/A"
    active = 0
    try:
        # 从 progress.json 中读取时间戳如果有的话
        ts = progress.get("last_update_time")
        if ts:
            pass
    except Exception:
        pass

    return {
        "progress": {
            "outline_done": outline_done,
            "outline_review_ok": outline_review_ok,
            "draft_done": draft_done,
            "review_ok": review_ok,
            "final_done": final_done,
            "eta": eta,
            "active_processes": active,
        },
        "chapters": {
            str(ch): {
                "outline_review_ok": s.outline_review_ok,
                "outline_review_status": s.outline_review_status,
                "outline_review_score": s.outline_review_score,
                "draft_ok": s.draft_ok,
                "draft_words": s.draft_words,
                "review_ok": s.review_ok,
                "review_score": s.review_score,
                "review_status": s.review_status,
                "final_ok": s.final_ok,
                "final_words": s.final_words,
                "failed_reason": s.failed_reason,
            }
            for ch, s in statuses.items()
        },
        "recent_scores": recent_scores,
        "logs": _latest_log_lines(project_dir),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    if PROJECT_DIR is None:
        return HTMLResponse("未指定项目", status_code=500)
    html = HTML_PAGE.replace("{{title}}", TITLE)
    html = html.replace("{{project_dir}}", str(PROJECT_DIR))
    html = html.replace("{{total_chapters}}", str(TOTAL_CHAPTERS))
    return HTMLResponse(html)


@app.get("/api/state")
def api_state():
    if PROJECT_DIR is None:
        return JSONResponse({"error": "未指定项目"}, status_code=500)
    return build_state(PROJECT_DIR, TOTAL_CHAPTERS)


@app.get("/events")
def events():
    if PROJECT_DIR is None:
        return JSONResponse({"error": "未指定项目"}, status_code=500)

    async def event_generator():
        last_state = None
        last_log_hash = ""
        while True:
            state = build_state(PROJECT_DIR, TOTAL_CHAPTERS)
            state_json = json.dumps(state, ensure_ascii=False)
            if state_json != last_state:
                yield f"data: {json.dumps({'type':'state','payload':state}, ensure_ascii=False)}\n\n"
                last_state = state_json

            log_hash = "\n".join(state["logs"])
            if log_hash != last_log_hash:
                # 只发送新增的日志行
                yield f"data: {json.dumps({'type':'log','text':'[刷新日志]'}, ensure_ascii=False)}\n\n"
                for line in state["logs"][-20:]:
                    yield f"data: {json.dumps({'type':'log','text':line}, ensure_ascii=False)}\n\n"
                last_log_hash = log_hash

            await __import__("asyncio").sleep(2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def main():
    global PROJECT_DIR, TOTAL_CHAPTERS, TITLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    PROJECT_DIR = project
    config = load_config(project)
    TOTAL_CHAPTERS = int(config.get("total_chapters", 0))
    TITLE = _load_world_title(project) or "小说生成看板"

    print(f"=" * 60)
    print(f"小说生成看板启动")
    print(f"项目: {project}")
    print(f"访问: http://{args.host}:{args.port}")
    print(f"=" * 60)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
