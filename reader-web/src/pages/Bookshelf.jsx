import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchBooks } from '../api.js'

const PROGRESS_KEY = 'reader_progress' // bookId -> {chapter, ts}

function getProgress() {
  try { return JSON.parse(localStorage.getItem(PROGRESS_KEY) || '{}') } catch { return {} }
}

export default function Bookshelf() {
  const [books, setBooks] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const nav = useNavigate()

  useEffect(() => {
    fetchBooks()
      .then((b) => { setBooks(Array.isArray(b) ? b : []); setLoading(false) })
      .catch((e) => { setErr(String(e)); setLoading(false) })
  }, [])

  const progress = getProgress()

  return (
    <div className="bookshelf">
      <header className="shelf-header">
        <h1>📚 书库</h1>
        <span className="sub">{books.length} 本</span>
      </header>
      {loading && <p className="hint">加载中…</p>}
      {err && <p className="hint err">加载失败：{err}（确认 reader_server 在 :8889 运行）</p>}
      <div className="grid">
        {books.map((b) => {
          const p = progress[b.id]
          return (
            <div key={b.id} className="book-card" onClick={() => nav(`/read/${b.id}/${(p && p.chapter) || 1}`)}>
              <div className="cover">
                {b.cover ? <img src={b.cover} alt="" /> : <span className="cover-ph">📖</span>}
              </div>
              <div className="info">
                <div className="title">{b.title || b.id}</div>
                <div className="meta">{b.chapter_count || 0} 章 · {Math.round((b.word_count || 0) / 10000)} 万字</div>
                {p && <div className="prog">读到第 {p.chapter} 章</div>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
