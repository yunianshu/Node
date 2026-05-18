import React from 'react'
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Projects from './pages/Projects'
import Chapters from './pages/Chapters'
import Config from './pages/Config'
import Quota from './pages/Quota'
import Multimedia from './pages/Multimedia'
import Logs from './pages/Logs'

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Projects />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/chapters" element={<Chapters />} />
        <Route path="/config" element={<Config />} />
        <Route path="/quota" element={<Quota />} />
        <Route path="/multimedia" element={<Multimedia />} />
        <Route path="/logs" element={<Logs />} />
      </Routes>
    </Layout>
  )
}

export default App
