#!/usr/bin/env python3
"""
多本小说阅读器 - 简易HTTP服务器 (多媒体增强版)
扫描指定目录下的所有小说子目录，支持视频、音乐、图片、语音
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from core.workflow_state import list_outline_chapters, outline_dir

DEFAULT_NOVELS_PARENT_DIR = Path(__file__).resolve().parents[2] / "projects"
NOVELS_PARENT = None
PORT = 8889

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
  overflow: hidden;
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
  position: relative;
}
.book-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.15);
}
.book-cover {
  width: 100%;
  height: 160px;
  object-fit: cover;
  background: var(--hover);
  display: block;
}
.book-cover-placeholder {
  width: 100%;
  height: 160px;
  background: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 48px;
  font-weight: bold;
}
.book-card .card-body {
  padding: 16px 20px 20px;
}
.book-card h3 {
  font-size: 20px;
  color: var(--accent);
  margin-bottom: 8px;
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
.book-card .media-tags {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.media-tag {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--bg);
  color: var(--accent);
  border: 1px solid var(--border);
}

/* ========== 视频弹窗 ========== */
#video-modal {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.8);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 200;
}
#video-modal.show { display: flex; }
#video-modal .video-box {
  position: relative;
  width: 90%;
  max-width: 800px;
  background: #000;
  border-radius: 8px;
  overflow: hidden;
}
#video-modal video {
  width: 100%;
  display: block;
}
#video-modal .close-video {
  position: absolute;
  top: 8px;
  right: 8px;
  background: rgba(0,0,0,0.6);
  color: #fff;
  border: none;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  font-size: 20px;
  cursor: pointer;
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
.btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
#progress-text { font-size: 14px; color: var(--accent); }

#content-area {
  flex: 1;
  overflow-y: auto;
  padding: 30px 60px;
  max-width: 900px;
  margin: 0 auto;
  width: 100%;
}
#chapter-image {
  width: 100%;
  max-height: 320px;
  object-fit: cover;
  border-radius: 8px;
  margin-bottom: 24px;
  display: none;
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

/* 歌词面板 */
#lyrics-panel {
  position: fixed;
  bottom: 64px;
  right: 20px;
  width: 320px;
  max-height: 300px;
  overflow-y: auto;
  background: var(--sidebar-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.15);
  display: none;
  z-index: 100;
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
}
#lyrics-panel.show { display: block; }
#lyrics-panel .lyrics-title {
  font-weight: bold;
  color: var(--accent);
  margin-bottom: 8px;
  font-size: 13px;
}

/* ========== 章节大纲 ========== */
.outline-wrap {
  margin-top: 40px;
  padding: 24px;
  background: var(--sidebar-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.outline-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.outline-title { font-size: 18px; font-weight: bold; color: var(--accent); }
.outline-section { margin-bottom: 16px; }
.outline-section:last-child { margin-bottom: 0; }
.outline-section h4 { font-size: 14px; color: var(--accent); margin-bottom: 6px; text-indent: 0; }
.outline-section p { text-indent: 0; font-size: 14px; line-height: 1.7; text-align: left; margin-bottom: 4px; }
.outline-section ul { padding-left: 20px; }
.outline-section li { text-indent: 0; font-size: 14px; line-height: 1.7; text-align: left; margin-bottom: 4px; }
.outline-section .ol-label { font-weight: bold; color: var(--accent); }
.outline-scene { background: var(--bg); border-left: 3px solid var(--accent); padding: 10px 14px; margin: 8px 0; border-radius: 4px; }
.outline-scene .scene-pos { font-size: 12px; color: var(--accent); font-weight: bold; margin-bottom: 6px; }

/* 底部导航 */
#bottom-nav {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 20px;
  padding: 12px 16px;
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

/* 音频控制条 */
#audio-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 16px;
  border-top: 1px solid var(--border);
  background: var(--sidebar-bg);
  flex-shrink: 0;
  font-size: 14px;
}
#audio-bar .audio-label { color: var(--accent); font-size: 12px; min-width: 48px; }
#audio-bar input[type="range"] { flex: 1; }
#audio-bar .time-display { font-size: 12px; color: var(--sidebar-text); min-width: 80px; text-align: center; }

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
  #audio-bar { flex-wrap: wrap; }
  #lyrics-panel { width: calc(100% - 40px); right: 20px; }
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
          <button class="btn" id="btn-music" title="背景音乐">音乐</button>
          <button class="btn" id="btn-lyrics" title="歌词">歌词</button>
          <button class="btn" id="btn-outline" title="本章大纲">大纲</button>
          <button class="btn" id="btn-tts" title="语音朗读">朗读</button>
          <button class="btn" id="btn-settings">设置</button>
        </div>
      </div>

      <div id="content-area">
        <img id="chapter-image" alt="章节配图">
        <div id="chapter-title">加载中...</div>
        <div id="chapter-body">正在加载章节内容...</div>
        <div id="chapter-outline" class="outline-wrap" style="display:none;">
          <div class="outline-header">
            <span class="outline-title">本章大纲</span>
            <button class="btn" id="btn-outline-close" style="padding:2px 10px;font-size:12px;">收起</button>
          </div>
          <div id="outline-body"></div>
        </div>
      </div>

      <div id="lyrics-panel">
        <div class="lyrics-title">歌词</div>
        <div id="lyrics-content">暂无歌词</div>
      </div>

      <div id="audio-bar">
        <span class="audio-label">朗读</span>
        <input type="range" id="tts-progress" min="0" max="100" value="0">
        <span class="time-display" id="tts-time">00:00 / 00:00</span>
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

<!-- 视频弹窗 -->
<div id="video-modal">
  <div class="video-box">
    <button class="close-video" onclick="closeVideo()">&times;</button>
    <video id="trailer-video" controls></video>
  </div>
</div>

<!-- 设置面板 -->
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
  <div class="setting-row">
    <label>朗读音量</label>
    <input type="range" id="tts-volume" min="0" max="100" value="80">
  </div>
  <div class="setting-row">
    <label>音乐音量</label>
    <input type="range" id="music-volume" min="0" max="100" value="50">
  </div>
  <div class="setting-row" style="justify-content:center;">
    <button class="btn" id="close-settings">关闭</button>
  </div>
</div>

<script>
let books = [];
let currentBook = null;
let currentBookObj = null;
let chapters = [];
let currentChapter = 1;
let totalChapters = 0;
let currentMedia = {};

let musicAudio = new Audio();
musicAudio.loop = true;
let ttsAudio = new Audio();
let isMusicPlaying = false;
let isTtsPlaying = false;

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
  if (s.ttsVolume !== undefined) {
    ttsAudio.volume = s.ttsVolume / 100;
    document.getElementById('tts-volume').value = s.ttsVolume;
  }
  if (s.musicVolume !== undefined) {
    musicAudio.volume = s.musicVolume / 100;
    document.getElementById('music-volume').value = s.musicVolume;
  }
}
function saveSettings() {
  localStorage.setItem('reader_settings', JSON.stringify({
    theme: document.body.className.replace('theme-', '') || 'day',
    fontSize: parseInt(document.getElementById('font-size').value),
    lineHeight: parseFloat(document.getElementById('line-height').value),
    ttsVolume: parseInt(document.getElementById('tts-volume').value),
    musicVolume: parseInt(document.getElementById('music-volume').value)
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
    const coverHtml = book.cover
      ? '<img class="book-cover" src="' + book.cover + '" alt="封面" loading="lazy">'
      : '<div class="book-cover-placeholder">' + book.title.charAt(0).toUpperCase() + '</div>';
    let tags = '';
    if (book.has_video) tags += '<span class="media-tag">视频</span>';
    if (book.has_music) tags += '<span class="media-tag">音乐</span>';
    if (book.has_audio) tags += '<span class="media-tag">朗读</span>';
    if (book.has_images) tags += '<span class="media-tag">配图</span>';
    card.innerHTML =
      coverHtml +
      '<div class="card-body">' +
      '<h3>' + book.title + '</h3>' +
      '<div class="meta">' +
        '<span>' + book.chapter_count + '章</span>' +
        '<span>' + (book.word_count / 10000).toFixed(1) + '万字</span>' +
      '</div>' +
      '<div class="media-tags">' + tags + '</div>' +
      '</div>';
    card.onclick = () => { selectBook(book.id); };
    // 视频按钮（如果有预告片）
    if (book.trailer) {
      const playBtn = document.createElement('button');
      playBtn.className = 'btn';
      playBtn.textContent = '预告片';
      playBtn.style.cssText = 'position:absolute;top:8px;right:8px;font-size:12px;padding:4px 10px;background:rgba(0,0,0,0.6);color:#fff;border:none;';
      playBtn.onclick = (e) => { e.stopPropagation(); openVideo(book.trailer); };
      card.querySelector('.card-body').appendChild(playBtn);
    }
    list.appendChild(card);
  });
}

function openVideo(src) {
  const video = document.getElementById('trailer-video');
  video.src = src;
  document.getElementById('video-modal').classList.add('show');
  video.play();
}
function closeVideo() {
  const video = document.getElementById('trailer-video');
  video.pause();
  video.src = '';
  document.getElementById('video-modal').classList.remove('show');
}

async function selectBook(bookId) {
  currentBook = bookId;
  currentBookObj = books.find(b => b.id === bookId);
  localStorage.setItem('reader_book', bookId);
  currentChapter = parseInt(localStorage.getItem('reader_book_' + bookId + '_chapter') || '1');

  document.getElementById('library-page').style.display = 'none';
  document.getElementById('reader-page').classList.add('show');

  document.getElementById('current-book-title').textContent = currentBookObj ? currentBookObj.title : bookId;

  await loadChapterList(bookId);
}

function backToLibrary() {
  musicAudio.pause();
  ttsAudio.pause();
  document.getElementById('reader-page').classList.remove('show');
  document.getElementById('library-page').style.display = 'block';
  currentBook = null;
  currentBookObj = null;
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

  // 加载章节内容和媒体
  try {
    const res = await fetch('/api/book/' + encodeURIComponent(currentBook) + '/chapter/' + n);
    const data = await res.json();
    const paragraphs = data.content.split(/\\n{2,}/).filter(p => p.trim());
    document.getElementById('chapter-body').innerHTML = paragraphs.map(p =>
      '<p>' + p.trim().replace(/\\n/g, '<br>') + '</p>').join('');
    document.getElementById('content-area').scrollTop = 0;

    // 处理媒体
    currentMedia = data.media || {};
    renderMedia();
    renderOutline(data.outline);
  } catch (e) {
    document.getElementById('chapter-body').textContent = '加载失败: ' + e.message;
    currentMedia = {};
    renderMedia();
  }

  document.getElementById('prev-chapter').disabled = n <= 1;
  document.getElementById('next-chapter').disabled = n >= totalChapters;
  document.getElementById('bottom-prev').disabled = n <= 1;
  document.getElementById('bottom-next').disabled = n >= totalChapters;
}

function renderMedia() {
  // 章节配图
  const imgEl = document.getElementById('chapter-image');
  if (currentMedia.image) {
    imgEl.src = currentMedia.image;
    imgEl.style.display = 'block';
  } else {
    imgEl.style.display = 'none';
    imgEl.src = '';
  }

  // 背景音乐
  const musicBtn = document.getElementById('btn-music');
  if (currentMedia.music) {
    musicBtn.style.display = '';
    if (musicAudio.src !== currentMedia.music) {
      musicAudio.src = currentMedia.music;
      if (isMusicPlaying) musicAudio.play().catch(()=>{});
    }
  } else {
    musicBtn.style.display = 'none';
    musicAudio.pause();
    musicAudio.src = '';
    isMusicPlaying = false;
  }
  updateMusicBtn();

  // TTS 语音
  const ttsBtn = document.getElementById('btn-tts');
  const audioBar = document.getElementById('audio-bar');
  if (currentMedia.audio) {
    ttsBtn.style.display = '';
    audioBar.style.display = 'flex';
    if (ttsAudio.src !== currentMedia.audio) {
      ttsAudio.src = currentMedia.audio;
      isTtsPlaying = false;
    }
  } else {
    ttsBtn.style.display = 'none';
    audioBar.style.display = 'none';
    ttsAudio.pause();
    ttsAudio.src = '';
    isTtsPlaying = false;
  }
  updateTtsBtn();

  // 歌词
  const lyricsPanel = document.getElementById('lyrics-panel');
  const lyricsContent = document.getElementById('lyrics-content');
  const lyricsBtn = document.getElementById('btn-lyrics');
  if (currentMedia.lyrics) {
    lyricsBtn.style.display = '';
    lyricsContent.textContent = currentMedia.lyrics;
  } else {
    lyricsBtn.style.display = 'none';
    lyricsPanel.classList.remove('show');
    lyricsContent.textContent = '暂无歌词';
  }
}

function toggleMusic() {
  if (!currentMedia.music) return;
  if (isMusicPlaying) {
    musicAudio.pause();
    isMusicPlaying = false;
  } else {
    musicAudio.play().catch(e => console.log('音乐播放失败', e));
    isMusicPlaying = true;
  }
  updateMusicBtn();
}
function updateMusicBtn() {
  const btn = document.getElementById('btn-music');
  btn.textContent = isMusicPlaying ? '暂停' : '音乐';
  btn.classList.toggle('active', isMusicPlaying);
}

function toggleTts() {
  if (!currentMedia.audio) return;
  if (isTtsPlaying) {
    ttsAudio.pause();
    isTtsPlaying = false;
  } else {
    ttsAudio.play().catch(e => console.log('朗读播放失败', e));
    isTtsPlaying = true;
  }
  updateTtsBtn();
}
function updateTtsBtn() {
  const btn = document.getElementById('btn-tts');
  btn.textContent = isTtsPlaying ? '暂停' : '朗读';
  btn.classList.toggle('active', isTtsPlaying);
}

function toggleLyrics() {
  document.getElementById('lyrics-panel').classList.toggle('show');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function renderOutline(outline) {
  const body = document.getElementById('outline-body');
  if (!outline || typeof outline !== 'object' || Object.keys(outline).length === 0) {
    body.innerHTML = '<p style="text-indent:0;color:var(--sidebar-text);">本章无大纲数据</p>';
    return;
  }
  const sections = [];
  const field = (label, val) => {
    if (val !== undefined && val !== null && String(val).trim()) {
      sections.push('<div class="outline-section"><h4>' + label + '</h4><p>' + escapeHtml(String(val)) + '</p></div>');
    }
  };
  const listField = (label, arr) => {
    if (Array.isArray(arr) && arr.length) {
      const items = arr.map(it => '<li>' + escapeHtml(String(it)) + '</li>').join('');
      sections.push('<div class="outline-section"><h4>' + label + '</h4><ul>' + items + '</ul></div>');
    }
  };
  field('本章概要', outline.summary);
  field('章节目标', outline.chapter_goal);
  listField('关键事件', outline.key_events);
  listField('出场人物', outline.characters_involved);
  field('地点', outline.location);
  field('时间', outline.time_progression);
  field('情绪基调', outline.mood);
  field('情绪弧线', outline.emotional_arc);
  listField('张力点', outline.tension_points);
  field('伏笔', outline.foreshadowing);
  field('章节钩子', outline.chapter_hook);
  field('兑现设计', outline.payoff_design);
  field('人性锚点', outline.human_anchor);
  listField('内容层次', outline.content_layers);
  field('主要对手', outline.main_antagonist);
  field('主线关联', outline.main_arc_link);
  if (Array.isArray(outline.scenes) && outline.scenes.length) {
    const pair = (l, v) => (v !== undefined && v !== null && String(v).trim()) ? '<p><span class="ol-label">' + l + '</span>' + escapeHtml(String(v)) + '</p>' : '';
    const scenesHtml = outline.scenes.map(sc => {
      const parts = [];
      if (sc.position) parts.push('<div class="scene-pos">' + escapeHtml(sc.position) + '</div>');
      parts.push(pair('目标：', sc.objective));
      parts.push(pair('冲突：', sc.conflict));
      parts.push(pair('感官锚：', sc.sensory_anchor));
      parts.push(pair('潜台词：', sc.subtext_beat));
      parts.push(pair('退场钩：', sc.exit_hook));
      return '<div class="outline-scene">' + parts.join('') + '</div>';
    }).join('');
    sections.push('<div class="outline-section"><h4>场景设计</h4>' + scenesHtml + '</div>');
  }
  body.innerHTML = sections.join('') || '<p style="text-indent:0;color:var(--sidebar-text);">本章无大纲数据</p>';
}
function toggleOutline() {
  const wrap = document.getElementById('chapter-outline');
  const btn = document.getElementById('btn-outline');
  const shown = wrap.style.display === 'block';
  wrap.style.display = shown ? 'none' : 'block';
  btn.classList.toggle('active', !shown);
  if (!shown) wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// TTS 进度更新
ttsAudio.ontimeupdate = () => {
  if (!ttsAudio.duration) return;
  const pct = (ttsAudio.currentTime / ttsAudio.duration) * 100;
  document.getElementById('tts-progress').value = pct;
  document.getElementById('tts-time').textContent =
    formatTime(ttsAudio.currentTime) + ' / ' + formatTime(ttsAudio.duration);
};
ttsAudio.onended = () => { isTtsPlaying = false; updateTtsBtn(); };
ttsAudio.onerror = () => { isTtsPlaying = false; updateTtsBtn(); };

function formatTime(s) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
}

document.getElementById('tts-progress').oninput = (e) => {
  if (ttsAudio.duration) {
    ttsAudio.currentTime = (e.target.value / 100) * ttsAudio.duration;
  }
};

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

document.getElementById('btn-music').onclick = toggleMusic;
document.getElementById('btn-tts').onclick = toggleTts;
document.getElementById('btn-lyrics').onclick = toggleLyrics;
document.getElementById('btn-outline').onclick = toggleOutline;
document.getElementById('btn-outline-close').onclick = () => {
  document.getElementById('chapter-outline').style.display = 'none';
  document.getElementById('btn-outline').classList.remove('active');
};

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
document.getElementById('tts-volume').oninput = (e) => {
  ttsAudio.volume = e.target.value / 100;
  saveSettings();
};
document.getElementById('music-volume').oninput = (e) => {
  musicAudio.volume = e.target.value / 100;
  saveSettings();
};

document.onkeydown = (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (!currentBook) return;
  if (e.key === 'ArrowLeft') prevChapter();
  if (e.key === 'ArrowRight') nextChapter();
  if (e.key === 'Escape') {
    document.getElementById('settings-panel').classList.remove('show');
    document.getElementById('lyrics-panel').classList.remove('show');
    document.getElementById('chapter-outline').style.display = 'none';
    document.getElementById('btn-outline').classList.remove('active');
    closeVideo();
  }
};

loadSettings();
loadBooks();

// 如果有上次阅读的小说，自动恢复
const lastBook = localStorage.getItem('reader_book');
if (lastBook) {
  setTimeout(() => {
    const book = books.find(b => b.id === lastBook);
    if (book) selectBook(lastBook);
  }, 500);
}
</script>
</body>
</html>
"""

# 扩展名到 MIME 类型的映射
MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".txt": "text/plain; charset=utf-8",
    ".lrc": "text/plain; charset=utf-8",
}

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def load_book_title(book_dir):
    """从 world.json 读取书名，缺失时回退到目录名"""
    w = book_dir / "world.json"
    if w.exists():
        try:
            d = json.loads(w.read_text(encoding="utf-8"))
            title = d.get("title")
            if title and str(title).strip():
                return str(title).strip()
        except Exception as exc:
            pass
    return book_dir.name


def find_cover_url(media_dir, book_name):
    """查找封面图 URL，兼容 media/cover.* 与 media/images/cover*.*"""
    for ext in IMAGE_EXTS:
        if (media_dir / f"cover{ext}").exists():
            return f"/media/{book_name}/cover{ext}"
    images_dir = media_dir / "images"
    if images_dir.exists():
        for ext in IMAGE_EXTS:
            if (images_dir / f"cover{ext}").exists():
                return f"/media/{book_name}/images/cover{ext}"
        for p in sorted(images_dir.glob("cover*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                return f"/media/{book_name}/images/{p.name}"
    return None


def find_chapter_image_url(media_dir, book_name, num):
    """查找章节配图 URL，兼容 images/chapter_XXXX.* 与 images/chapters/chapter_XXXX*.*"""
    images_dir = media_dir / "images"
    if not images_dir.exists():
        return None
    for ext in IMAGE_EXTS:
        p = images_dir / f"chapter_{num:04d}{ext}"
        if p.exists():
            return f"/media/{book_name}/images/chapter_{num:04d}{ext}"
    chapters_dir = images_dir / "chapters"
    if chapters_dir.exists():
        for p in sorted(chapters_dir.glob(f"chapter_{num:04d}*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                return f"/media/{book_name}/images/chapters/{p.name}"
    return None


def discover_books(parent_dir):
    """扫描目录下所有小说，同时检测多媒体资源"""
    books = []
    for entry in Path(parent_dir).iterdir():
        if not entry.is_dir():
            continue
        if entry.name in ("audio", "logs", "scripts", "node_modules"):
            continue
        chapters_dir = entry / "chapters"
        outline_files = list(outline_dir(entry).glob("chapter_*.json"))
        if not outline_files or not chapters_dir.exists():
            continue
        try:
            chapter_count = len(outline_files)
            total_words = 0
            # 优先统计 final/ 目录，其次 draft/，最后 chapters/
            for src_dir in (entry / "chapters" / "final", entry / "chapters" / "draft", chapters_dir):
                if src_dir.exists():
                    txt_files = list(src_dir.glob("chapter_*.txt"))
                    if txt_files:
                        for cf in txt_files:
                            sz = cf.stat().st_size
                            if sz > 100:
                                total_words += sz // 3  # UTF-8 中文约3字节/字
                        break

            book_title = load_book_title(entry)

            media_dir = entry / "media"
            cover_path = None
            trailer_path = None
            has_video = False
            has_music = False
            has_audio = False
            has_images = False

            if media_dir.exists():
                # 封面
                cover_path = find_cover_url(media_dir, entry.name)
                # 预告片
                for ext in (".mp4", ".webm", ".mov"):
                    trailer = media_dir / f"trailer{ext}"
                    if trailer.exists():
                        trailer_path = f"/media/{entry.name}/trailer{ext}"
                        has_video = True
                        break
                # 子目录检测
                if (media_dir / "music").exists() and list((media_dir / "music").glob("*")):
                    has_music = True
                if (media_dir / "audio").exists() and list((media_dir / "audio").glob("*")):
                    has_audio = True
                if (media_dir / "images").exists() and list((media_dir / "images").glob("*")):
                    has_images = True
                # 如果没有 trailer 但 video 目录有文件也算
                if not has_video and (media_dir / "video").exists():
                    videos = list((media_dir / "video").glob("*"))
                    if videos:
                        has_video = True

            books.append({
                "id": entry.name,
                "title": book_title,
                "path": str(entry),
                "chapter_count": chapter_count,
                "word_count": total_words,
                "cover": cover_path,
                "trailer": trailer_path,
                "has_video": has_video,
                "has_music": has_music,
                "has_audio": has_audio,
                "has_images": has_images,
            })
        except Exception as exc:
            continue
    return sorted(books, key=lambda x: x["id"])


def make_handler(parent_dir, port):
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
                self.send_json(discover_books(parent_dir))
                return

            # 静态媒体文件服务 /media/{book_id}/{type}/{filename}
            if path.startswith("/media/"):
                self.serve_media(path)
                return

            if path.startswith("/api/book/"):
                parts = path.split("/")
                if len(parts) >= 4:
                    book_id = unquote(parts[3])
                    book_dir = Path(parent_dir) / book_id
                    chapters_dir = book_dir / "chapters"
                    outlines = list_outline_chapters(book_dir)

                    if not outlines:
                        self.send_json({"error": "Book not found"})
                        return

                    if len(parts) >= 5 and parts[4] == "chapters":
                        self.send_json(self.get_chapters(book_dir))
                        return

                    if len(parts) >= 6 and parts[4] == "chapter":
                        try:
                            num = int(parts[5])
                            self.send_json(self.get_chapter(book_dir, chapters_dir, num))
                        except ValueError:
                            self.send_error(400, "Invalid chapter number")
                        return

            self.send_error(404)

        def serve_media(self, path):
            # /media/{book_id}/{相对路径...}  支持任意层级子目录
            parts = path.split("/")
            if len(parts) < 4:
                self.send_error(400)
                return
            segs = [unquote(p) for p in parts[2:]]
            # 安全检查：防止目录遍历与空段
            if any(s in ("..", ".", "") for s in segs):
                self.send_error(403)
                return
            book_id = segs[0]
            file_path = Path(parent_dir) / book_id / "media"
            for seg in segs[1:]:
                file_path = file_path / seg
            if not file_path.is_file():
                self.send_error(404)
                return

            ext = file_path.suffix.lower()
            mime = MIME_MAP.get(ext, "application/octet-stream")

            try:
                with open(file_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500)

        def send_json(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def get_chapters(self, book_dir):
            results = []
            for ch in list_outline_chapters(book_dir):
                num = ch.get("chapter_number", 0)
                title = ch.get("title", "")
                results.append({"number": num, "title": title})
            return results

        def get_chapter(self, book_dir, chapters_dir, num):
            # 优先顺序: final/ > draft/ > chapters/
            final_file = chapters_dir / "final" / f"chapter_{num:04d}.txt"
            draft_file = chapters_dir / "draft" / f"chapter_{num:04d}.txt"
            old_file = chapters_dir / f"chapter_{num:04d}.txt"

            content = ""
            for chapter_file in (final_file, draft_file, old_file):
                if chapter_file.exists():
                    content = chapter_file.read_text(encoding="utf-8")
                    break

            # 收集该章节的媒体资源
            media = {}
            media_dir = book_dir / "media"
            if media_dir.exists():
                # 配图
                img_url = find_chapter_image_url(media_dir, book_dir.name, num)
                if img_url:
                    media["image"] = img_url
                # 背景音乐
                for ext in (".mp3", ".wav", ".ogg", ".m4a"):
                    music = media_dir / "music" / f"chapter_{num:04d}{ext}"
                    if music.exists():
                        media["music"] = f"/media/{book_dir.name}/music/chapter_{num:04d}{ext}"
                        break
                # TTS 语音
                for ext in (".mp3", ".wav", ".ogg", ".m4a"):
                    audio = media_dir / "audio" / f"chapter_{num:04d}{ext}"
                    if audio.exists():
                        media["audio"] = f"/media/{book_dir.name}/audio/chapter_{num:04d}{ext}"
                        break
                # 歌词
                for ext in (".txt", ".lrc"):
                    lyrics = media_dir / "lyrics" / f"chapter_{num:04d}{ext}"
                    if lyrics.exists():
                        media["lyrics"] = lyrics.read_text(encoding="utf-8")
                        break

            # 大纲数据
            outline = {}
            outline_file = outline_dir(book_dir) / f"chapter_{num:04d}.json"
            if outline_file.exists():
                try:
                    outline = json.loads(outline_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    outline = {}

            return {
                "number": num,
                "content": content,
                "word_count": len(content),
                "media": media,
                "outline": outline,
            }

    return ReaderHandler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--novels-dir", "-d", type=str,
                        default=os.getenv("NOVELS_PARENT_DIR", str(DEFAULT_NOVELS_PARENT_DIR)),
                        help="小说父目录（包含多个小说项目）")
    parser.add_argument("--port", type=int, default=8889, help="服务端口")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    parent_dir = Path(args.novels_dir).resolve()
    port = args.port

    if not parent_dir.exists():
        print(f"错误: 目录不存在 {parent_dir}")
        sys.exit(1)

    Handler = make_handler(parent_dir, port)
    server = HTTPServer(("0.0.0.0", port), Handler)
    print("=" * 50)
    print("  多本小说阅读器已启动 (多媒体增强版)")
    print(f"  扫描目录: {parent_dir}")
    print(f"  请在浏览器打开: http://localhost:{port}")
    print("  媒体文件请放在每本小说的 media/ 目录下")
    print("  按 Ctrl+C 停止")
    print("=" * 50)
    if args.open_browser:
        t = threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}"))
        t.daemon = True
        t.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")


if __name__ == "__main__":
    main()
