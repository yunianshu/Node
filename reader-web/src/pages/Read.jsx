import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { fetchChapters, fetchChapter } from '../api.js'

const PROGRESS_KEY = 'reader_progress'
const FONT_KEY = 'reader_fontsize'
const THEME_KEY = 'reader_theme'

function saveProgress(bookId, chapter) {
  try {
    const p = JSON.parse(localStorage.getItem(PROGRESS_KEY) || '{}')
    p[bookId] = { chapter, ts: Date.now() }
    localStorage.setItem(PROGRESS_KEY, JSON.stringify(p))
  } catch {}
}

export default function Read() {
  const { bookId, chapter } = useParams()
  const nav = useNavigate()
  const ch = parseInt(chapter || 1, 10)

  const [chapters, setChapters] = useState([])
  const [content, setContent] = useState('')
  const [title, setTitle] = useState('')
  const [loading, setLoading] = useState(true)
  const [showToc, setShowToc] = useState(false)
  const [fontSize, setFontSize] = useState(parseInt(localStorage.getItem(FONT_KEY) || 20, 10))
  const [theme, setTheme] = useState(localStorage.getItem(THEME_KEY) || 'light')
  const topRef = useRef(null)

  const total = chapters.length

  useEffect(() => { fetchChapters(bookId).then(setChapters).catch(() => {}) }, [bookId])

  useEffect(() => {
    setLoading(true); setContent(''); setTitle('')
    fetchChapter(bookId, ch)
      .then((text) => {
        setContent(text || '（本章无内容）')
        const c = (chapters.find((x) => x.number === ch) || {})
        setTitle(c.title || '')
        setLoading(false)
        saveProgress(bookId, ch)
        topRef.current?.scrollIntoView()
      })
      .catch(() => { setContent('加载失败'); setLoading(false) })
    // eslint-disable-next-line
  }, [bookId, ch])

  useEffect(() => { localStorage.setItem(FONT_KEY, fontSize) }, [fontSize])
  useEffect(() => { localStorage.setItem(THEME_KEY, theme) }, [theme])

  const go = (n) => {
    if (n >= 1 && (total === 0 || n <= total)) nav(`/read/${bookId}/${n}`)
  }

  const paragraphs = content.split(/\n+/).filter((s) => s.trim())

  return (
    <div className={`reader ${theme}`}>
      <div className="topbar">
        <button className="btn" onClick={() => nav('/')}>← 书库</button>
        <span className="ch-title">第 {ch} 章{title ? ` · ${title}` : ''}</span>
        <div className="tools">
          <button className="btn" onClick={() => setFontSize((f) => Math.max(12, f - 1))}>A-</button>
          <button className="btn" onClick={() => setFontSize((f) => Math.min(32, f + 1))}>A+</button>
          <button className="btn" onClick={() => setTheme((t) => (t === 'light' ? 'sepia' : t === 'sepia' ? 'dark' : 'light'))}>主题</button>
          <button className="btn" onClick={() => setShowToc((s) => !s)}>目录</button>
        </div>
      </div>

      {showToc && (
        <div className="toc">
          <div className="toc-head">目录（共 {total} 章）<button className="btn sm" onClick={() => setShowToc(false)}>关闭</button></div>
          <div className="toc-list">
            {chapters.map((c) => (
              <div key={c.number} className={`toc-item ${c.number === ch ? 'cur' : ''}`}
                onClick={() => { nav(`/read/${bookId}/${c.number}`); setShowToc(false) }}>
                {c.number}. {c.title || ''}
              </div>
            ))}
          </div>
        </div>
      )}

      <div ref={topRef} />
      <div className="content" style={{ fontSize }}>
        {loading ? <p className="hint">加载中…</p> : paragraphs.map((p, i) => <p key={i}>{p}</p>)}
      </div>

      <div className="pagenav">
        <button className="btn" disabled={ch <= 1} onClick={() => go(ch - 1)}>← 上一章</button>
        <span className="pageinfo">{ch} / {total || '?'}</span>
        <button className="btn" disabled={total > 0 && ch >= total} onClick={() => go(ch + 1)}>下一章 →</button>
      </div>
    </div>
  )
}
