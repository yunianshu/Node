#!/usr/bin/env python3
"""
多本小说阅读器 - 简易HTTP服务器
扫描 novels/ 下的所有小说子目录
"""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

NOVELS_ROOT = Path("D:/AiProject/Node/novels2")
NOVELS_PARENT = NOVELS_ROOT.parent
PORT = 8888

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小说阅读器</title>
<style>
:root {
  --bg: #f5f0e8;
  --text: #333;
  --sidebar-bg: #e8e0d4;
  --sidebar-text: #444;
  --border: #d0c8b8;
  --accent: #8b4513;
  --hover: #d4c8b0;
  --font-size: 20px;
  --line-height: 1.8;
}
.theme-dark {
  --bg: #1a1a2e;
  --text: #e0e0e0;
  --sidebar-bg: #16213e;
  --sidebar-text: #ccc;
  --border: #0f3460;
  --accent: #e94560;
  --hover: #2a2a4a;
}
.theme-green {
  --bg: #c7edcc;
  --text: #2e4a3e;
  --sidebar-bg: #a8d5b5;
  --sidebar-text: #2e4a3e;
  --border: #8fc9a0;
  --accent: #1a5f2a;
  --hover: #9bcfa8;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: var(--font-size);
  line-height: var(--line-height);
  overflow: hidden;
  height: 100vh;
}
#app { height: 100vh; }

/* ========== 书库页面 ========== */
#library-page {
  height: 100vh;
  overflow-y: auto;
  padding: 40px 60px;
}
#library-page .header {
  text-align: center;
  margin-bottom: 40px;
}
#library-page .header h1 {
  font-size: 36px;
  color: var(--accent);
  margin-bottom: 8px;
}
#library-page .header p {
  font-size: 16px;
  color: var(--sidebar-text);
}
.book-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 24px;
  max-width: 1200px;
  margin: 0 auto;
}
.book-card {
  background: var(--sidebar-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
  position: relative;
}
.book-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.15);
}
.book-card .book-icon {
  width: 64px;
  height: 80px;
  background: var(--accent);
  border-radius: 4px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 28px;
  font-weight: bold;
}
.book-card h3 {
  font-size: 20px;
  color: var(--accent);
  margin-bottom: 10px;
  word-break: break-all;
}
.book-card .meta {
  font-size: 14px;
  color: var(--sidebar-text);
  line-height: 1.6;
}
.book-card .meta span {
  display: inline-block;
  margin-right: 12px;
}

/* ========== 阅读页面 ========== */
#reader-page {
  display: none;
  height: 100vh;
}
#reader-page.show { display: flex; }

/* 侧边栏 */
#sidebar {
  width: 320px;
  background: var(--sidebar-bg);
  color: var(--sidebar-text);
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  transition: transform 0.3s;
}
#sidebar.hidden { transform: translateX(-100%); width: 0; border: none; }
#sidebar-header {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
#sidebar-header .book-title {
  font-size: 15px;
  font-weight: bold;
  color: var(--accent);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
#sidebar-search {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
}
#sidebar-search input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
}
#chapter-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}
.chapter-item {
  padding: 10px 20px;
  cursor: pointer;
  font-size: 15px;
  border-left: 3px solid transparent;
  transition: background 0.2s;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chapter-item:hover { background: var(--hover); }
.chapter-item.active {
  background: var(--hover);
  border-left-color: var(--accent);
  font-weight: bold;
}
.chapter-item .ch-num { color: var(--accent); margin-right: 8px; }

/* 主内容区 */
#main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#toolbar {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--sidebar-bg);
  flex-shrink: 0;
}
#toolbar-left, #toolbar-right { display: flex; align-items: center; gap: 8px; }
.btn {
  padding: 6px 14px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  transition: all 0.2s;
}
.btn:hover { background: var(--hover); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
#progress-text { font-size: 14px; color: var(--accent); }

#content-area {
  flex: 1;
  overflow-y: auto;
  padding: 40px 60px;
  max-width: 900px;
  margin: 0 auto;
  width: 100%;
}
#chapter-title {
  font-size: 28px;
  font-weight: bold;
  text-align: center;
  margin-bottom: 30px;
  color: var(--accent);
}
#chapter-body { text-indent: 2em; text-align: justify; }
#chapter-body p { margin-bottom: 1em; }

/* 底部导航 */
#bottom-nav {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 20px;
  padding: 16px;
  border-top: 1px solid var(--border);
  background: var(--sidebar-bg);
  flex-shrink: 0;
}
#goto-input {
  width: 80px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  text-align: center;
}

/* 设置面板 */
#settings-panel {
  position: fixed;
  top: 60px;
  right: 20px;
  width: 260px;
  background: var(--sidebar-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.15);
  display: none;
  z-index: 100;
}
#settings-panel.show { display: block; }
.setting-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.setting-row label { font-size: 14px; }
.setting-row input[type="range"] { width: 120px; }
.theme-btns { display: flex; gap: 6px; }
.theme-btn {
  width: 32px; height: 32px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
}
.theme-btn.active { border-color: var(--accent); }
.theme-btn.day { background: #f5f0e8; }
.theme-btn.dark { background: #1a1a2e; }
.theme-btn.green { background: #c7edcc; }

/* 滚动条 */
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

@media (max-width: 768px) {
  #sidebar { position: fixed; left: 0; top: 0; height: 100vh; z-index: 50; }
  #content-area { padding: 20px; }
  #toolbar { padding: 0 10px; }
  #library-page { padding: 20px; }
}
</style>
</head>
<body>
<div id="app">
  <!-- 书库页面 -->
  <div id="library-page">
    <div class="header">
      <h1>我的书库</h1>
      <p>选择一本小说开始阅读</p>
    </div>
    <div id="book-list" class="book-grid">
      <div style="text-align:center;color:var(--sidebar-text);padding:40px;">正在加载书库...</div>
    </div>
  </div>

  <!-- 阅读页面 -->
  <div id="reader-page">
    <aside id="sidebar">
      <div id="sidebar-header">
        <span class="book-title" id="current-book-title">未选择</span>
        <button class="btn" id="btn-back" style="padding:4px 10px;font-size:12px;">返回</button>
      </div>
      <div id="sidebar-search"><input type="text" id="search-input" placeholder="搜索章节标题..."></div>
      <div id="chapter-list"></div>
    </aside>

    <main id="main">
      <div id="toolbar">
        <div id="toolbar-left">
          <button class="btn" id="toggle-sidebar">目录</button>
          <button class="btn" id="prev-chapter">上一章</button>
          <span id="progress-text">第 -/- 章</span>
          <button class="btn" id="next-chapter">下一章</button>
        </div>
        <div id="toolbar-right">
          <button class="btn" id="btn-settings">设置</button>
        </div>
      </div>

      <div id="content-area">
        <div id="chapter-title">加载中...</div>
        <div id="chapter-body">正在加载章节内容...</div>
      </div>

      <div id="bottom-nav">
        <button class="btn" id="bottom-prev">上一章</button>
        <span>跳转到</span>
        <input type="number" id="goto-input" min="1" placeholder="章">
        <button class="btn" id="btn-goto">跳转</button>
        <button class="btn" id="bottom-next">下一章</button>
      </div>
    </main>
  </div>
</div>

<div id="settings-panel">
  <div class="setting-row">
    <label>主题</label>
    <div class="theme-btns">
      <div class="theme-btn day active" data-theme="day" title="白天"></div>
      <div class="theme-btn dark" data-theme="dark" title="夜间"></div>
      <div class="theme-btn green" data-theme="green" title="护眼"></div>
    </div>
  </div>
  <div class="setting-row">
    <label>字体大小</label>
    <input type="range" id="font-size" min="14" max="32" value="20">
  </div>
  <div class="setting-row">
    <label>行间距</label>
    <input type="range" id="line-height" min="1.2" max="3" step="0.1" value="1.8">
  </div>
  <div class="setting-row" style="justify-content:center;">
    <button class="btn" id="close-settings">关闭</button>
  </div>
</div>

<script>
let books = [];
let currentBook = null;
let chapters = [];
let currentChapter = 1;
let totalChapters = 0;

function loadSettings() {
  const s = JSON.parse(localStorage.getItem('reader_settings') || '{}');
  if (s.theme) setTheme(s.theme);
  if (s.fontSize) {
    document.documentElement.style.setProperty('--font-size', s.fontSize + 'px');
    document.getElementById('font-size').value = s.fontSize;
  }
  if (s.lineHeight) {
    document.documentElement.style.setProperty('--line-height', s.lineHeight);
    document.getElementById('line-height').value = s.lineHeight;
  }
}
function saveSettings() {
  localStorage.setItem('reader_settings', JSON.stringify({
    theme: document.body.className.replace('theme-', '') || 'day',
    fontSize: parseInt(document.getElementById('font-size').value),
    lineHeight: parseFloat(document.getElementById('line-height').value)
  }));
}
function setTheme(t) {
  document.body.className = 'theme-' + t;
  document.querySelectorAll('.theme-btn').forEach(b => b.classList.toggle('active', b.dataset.theme === t));
}

// 加载小说列表
async function loadBooks() {
  const res = await fetch('/api/books');
  books = await res.json();
  renderBookList();
}

function renderBookList() {
  const list = document.getElementById('book-list');
  if (books.length === 0) {
    list.innerHTML = '<div style="text-align:center;color:var(--sidebar-text);padding:40px;">暂无小说，请确保 novels/ 目录下有小说文件夹</div>';
    return;
  }
  list.innerHTML = '';
  books.forEach(book => {
    const card = document.createElement('div');
    card.className = 'book-card';
    const firstChar = book.title.charAt(0).toUpperCase();
    card.innerHTML =
      '<div class="book-icon">' + firstChar + '</div>' +
      '<h3>' + book.title + '</h3>' +
      '<div class="meta">' +
        '<span>' + book.chapter_count + '章</span>' +
        '<span>' + (book.word_count / 10000).toFixed(1) + '万字</span>' +
      '</div>';
    card.onclick = () => { selectBook(book.id); };
    list.appendChild(card);
  });
}

async function selectBook(bookId) {
  currentBook = bookId;
  localStorage.setItem('reader_book', bookId);
  currentChapter = parseInt(localStorage.getItem('reader_book_' + bookId + '_chapter') || '1');

  // 切换到阅读页面
  document.getElementById('library-page').style.display = 'none';
  document.getElementById('reader-page').classList.add('show');

  const book = books.find(b => b.id === bookId);
  document.getElementById('current-book-title').textContent = book ? book.title : bookId;

  await loadChapterList(bookId);
}

function backToLibrary() {
  document.getElementById('reader-page').classList.remove('show');
  document.getElementById('library-page').style.display = 'block';
  currentBook = null;
}

async function loadChapterList(bookId) {
  const res = await fetch('/api/book/' + encodeURIComponent(bookId) + '/chapters');
  chapters = await res.json();
  totalChapters = chapters.length;
  renderChapterList();
  if (currentChapter > totalChapters) currentChapter = 1;
  loadChapter(currentChapter);
}

function renderChapterList(filter) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = '';
  chapters.forEach(ch => {
    if (filter && !ch.title.includes(filter) && !String(ch.number).includes(filter)) return;
    const div = document.createElement('div');
    div.className = 'chapter-item' + (ch.number === currentChapter ? ' active' : '');
    div.innerHTML = '<span class="ch-num">第' + ch.number + '章</span>' + ch.title;
    div.onclick = () => {
      loadChapter(ch.number);
      if (window.innerWidth < 768) toggleSidebar();
    };
    list.appendChild(div);
  });
}

async function loadChapter(n) {
  if (n < 1 || n > totalChapters) return;
  currentChapter = n;
  document.getElementById('chapter-title').textContent = '第' + n + '章 ' + (chapters[n-1]?.title || '');
  document.getElementById('chapter-body').textContent = '加载中...';
  document.getElementById('progress-text').textContent = '第 ' + n + '/' + totalChapters + ' 章';
  localStorage.setItem('reader_book_' + currentBook + '_chapter', n);

  document.querySelectorAll('.chapter-item').forEach(el => {
    const num = parseInt(el.querySelector('.ch-num').textContent.replace(/[^0-9]/g, ''));
    el.classList.toggle('active', num === n);
  });
  const active = document.querySelector('.chapter-item.active');
  if (active) active.scrollIntoView({ block: 'center' });

  try {
    const res = await fetch('/api/book/' + encodeURIComponent(currentBook) + '/chapter/' + n);
    const data = await res.json();
    const paragraphs = data.content.split(/\\n{2,}/).filter(p => p.trim());
    document.getElementById('chapter-body').innerHTML = paragraphs.map(p =>
      '<p>' + p.trim().replace(/\\n/g, '<br>') + '</p>').join('');
    document.getElementById('content-area').scrollTop = 0;
  } catch (e) {
    document.getElementById('chapter-body').textContent = '加载失败: ' + e.message;
  }

  document.getElementById('prev-chapter').disabled = n <= 1;
  document.getElementById('next-chapter').disabled = n >= totalChapters;
  document.getElementById('bottom-prev').disabled = n <= 1;
  document.getElementById('bottom-next').disabled = n >= totalChapters;
}

function prevChapter() { loadChapter(currentChapter - 1); }
function nextChapter() { loadChapter(currentChapter + 1); }
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('hidden'); }

// 事件绑定
document.getElementById('btn-back').onclick = backToLibrary;
document.getElementById('toggle-sidebar').onclick = toggleSidebar;
document.getElementById('prev-chapter').onclick = prevChapter;
document.getElementById('next-chapter').onclick = nextChapter;
document.getElementById('bottom-prev').onclick = prevChapter;
document.getElementById('bottom-next').onclick = nextChapter;
document.getElementById('btn-goto').onclick = () => {
  const n = parseInt(document.getElementById('goto-input').value);
  if (n) loadChapter(n);
};
document.getElementById('goto-input').onkeydown = (e) => {
  if (e.key === 'Enter') document.getElementById('btn-goto').click();
};
document.getElementById('search-input').oninput = (e) => renderChapterList(e.target.value);

document.getElementById('btn-settings').onclick = () => {
  document.getElementById('settings-panel').classList.toggle('show');
};
document.getElementById('close-settings').onclick = () => document.getElementById('settings-panel').classList.remove('show');
document.querySelectorAll('.theme-btn').forEach(btn => {
  btn.onclick = () => { setTheme(btn.dataset.theme); saveSettings(); };
});
document.getElementById('font-size').oninput = (e) => {
  document.documentElement.style.setProperty('--font-size', e.target.value + 'px');
  saveSettings();
};
document.getElementById('line-height').oninput = (e) => {
  document.documentElement.style.setProperty('--line-height', e.target.value);
  saveSettings();
};

document.onkeydown = (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (!currentBook) return;
  if (e.key === 'ArrowLeft') prevChapter();
  if (e.key === 'ArrowRight') nextChapter();
  if (e.key === 'Escape') document.getElementById('settings-panel').classList.remove('show');
};

loadSettings();
loadBooks();

// 如果有上次阅读的小说，自动恢复
const lastBook = localStorage.getItem('reader_book');
if (lastBook) {
  // 等待书库加载完成后再选择
  setTimeout(() => {
    const book = books.find(b => b.id === lastBook);
    if (book) selectBook(lastBook);
  }, 500);
}
</script>
</body>
</html>
"""


def discover_books(parent_dir):
    """扫描目录下所有小说"""
    books = []
    for entry in Path(parent_dir).iterdir():
        if not entry.is_dir():
            continue
        if entry.name in ('audio', 'logs', 'scripts', 'node_modules'):
            continue
        outline_file = entry / "outline.json"
        chapters_dir = entry / "chapters"
        if not outline_file.exists() or not chapters_dir.exists():
            continue
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                outline = json.load(f)
            chapter_count = len(outline.get("chapters", []))
            total_words = 0
            # 优先统计 final/ 目录（有内容的），其次 draft/，最后 chapters/（兼容旧数据）
            for src_dir in (entry / "chapters" / "final", entry / "chapters" / "draft", chapters_dir):
                if src_dir.exists():
                    txt_files = list(src_dir.glob("chapter_*.txt"))
                    if txt_files:
                        for cf in txt_files:
                            if cf.stat().st_size > 100:
                                total_words += len(cf.read_text(encoding="utf-8"))
                        break  # 找到第一个有文件的目录就停止
            book_title = entry.name
            # 尝试从outline中找到书名
            first_ch = outline.get("chapters", [{}])[0]
            if first_ch and "series_title" in first_ch:
                book_title = first_ch["series_title"]
            books.append({
                "id": entry.name,
                "title": book_title,
                "path": str(entry),
                "chapter_count": chapter_count,
                "word_count": total_words,
            })
        except Exception:
            continue
    return sorted(books, key=lambda x: x["id"])


class ReaderHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/books":
            self.send_json(discover_books(NOVELS_PARENT))
            return

        if path.startswith("/api/book/"):
            parts = path.split("/")
            if len(parts) >= 4:
                book_id = unquote(parts[3])
                book_dir = NOVELS_PARENT / book_id
                outline_file = book_dir / "outline.json"
                chapters_dir = book_dir / "chapters"

                if not outline_file.exists():
                    self.send_json({"error": "Book not found"})
                    return

                if len(parts) >= 5 and parts[4] == "chapters":
                    self.send_json(self.get_chapters(outline_file))
                    return

                if len(parts) >= 6 and parts[4] == "chapter":
                    try:
                        num = int(parts[5])
                        self.send_json(self.get_chapter(chapters_dir, num))
                    except ValueError:
                        self.send_error(400, "Invalid chapter number")
                    return

        self.send_error(404)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def get_chapters(self, outline_file):
        results = []
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                outline = json.load(f)
        except Exception:
            return results
        for ch in outline.get("chapters", []):
            num = ch.get("chapter_number", 0)
            title = ch.get("title", "")
            results.append({"number": num, "title": title})
        return results

    def get_chapter(self, chapters_dir, num):
        # 优先顺序: final/ > draft/ > chapters/ (兼容旧数据)
        final_file = chapters_dir / "final" / f"chapter_{num:04d}.txt"
        draft_file = chapters_dir / "draft" / f"chapter_{num:04d}.txt"
        old_file = chapters_dir / f"chapter_{num:04d}.txt"

        for chapter_file in (final_file, draft_file, old_file):
            if chapter_file.exists():
                content = chapter_file.read_text(encoding="utf-8")
                return {"number": num, "content": content, "word_count": len(content)}
        return {"number": num, "content": "", "word_count": 0, "error": "章节不存在"}


def main():
    server = HTTPServer(("0.0.0.0", PORT), ReaderHandler)
    print("=" * 50)
    print("  多本小说阅读器已启动")
    print(f"  请在浏览器打开: http://localhost:{PORT}")
    print("  按 Ctrl+C 停止")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")


if __name__ == "__main__":
    main()
