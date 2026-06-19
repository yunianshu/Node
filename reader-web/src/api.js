// reader_server.py (http://localhost:8889) 的 API 封装；vite proxy 已把 /api /media 转发过去
export const fetchBooks = () =>
  fetch('/api/books').then((r) => r.json())

export const fetchChapters = (bookId) =>
  fetch(`/api/book/${encodeURIComponent(bookId)}/chapters`).then((r) => r.json())

export const fetchChapter = (bookId, n) =>
  fetch(`/api/book/${encodeURIComponent(bookId)}/chapter/${n}`)
    .then((r) => r.json())
    .then((d) => (d && d.content) || '')
