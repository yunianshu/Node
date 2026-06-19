import { Routes, Route, Navigate } from 'react-router-dom'
import Bookshelf from './pages/Bookshelf.jsx'
import Read from './pages/Read.jsx'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Bookshelf />} />
      <Route path="/read/:bookId" element={<Read />} />
      <Route path="/read/:bookId/:chapter" element={<Read />} />
      <Route path="*" element={<Navigate to="/" />} />
    </Routes>
  )
}
