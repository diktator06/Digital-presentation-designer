import { useEffect, useState } from 'react'
import { api } from './api'
import Generate from './pages/Generate'
import Templates from './pages/Templates'
import Skills from './pages/Skills'

type Tab = 'generate' | 'templates' | 'skills'

export default function App() {
  // оболочка интерфейса: вкладки и индикаторы подключённых моделей (LLM, VLM, генерация изображений)
  const [tab, setTab] = useState<Tab>('generate')
  const [health, setHealth] = useState<{ llm: string; vlm: string | null; t2i: string | null } | null>(null)

  useEffect(() => {
    // какие модели подключены на сервере (или офлайн-режим)
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [])

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <div style={{ width: 16 }}>
              <i style={{ width: 16 }} />
              <i style={{ width: 10 }} />
              <i style={{ width: 13 }} />
            </div>
          </div>
          DeckSmith
          <span className="muted small" style={{ fontWeight: 500 }}>цифровой дизайнер презентаций</span>
        </div>
        <nav className="tabs">
          <button className={`tab ${tab === 'generate' ? 'active' : ''}`} onClick={() => setTab('generate')}>Генерация</button>
          <button className={`tab ${tab === 'templates' ? 'active' : ''}`} onClick={() => setTab('templates')}>Шаблоны</button>
          <button className={`tab ${tab === 'skills' ? 'active' : ''}`} onClick={() => setTab('skills')}>Скиллы и аудит</button>
        </nav>
        <div className="spacer" />
        {health && (
          <div className="row small">
            <span className="status-pill"><span className={`dot ${health.llm === 'offline' ? 'off' : ''}`} />LLM: {health.llm}</span>
            <span className="status-pill"><span className={`dot ${health.vlm ? '' : 'off'}`} />VLM: {health.vlm ?? 'нет'}</span>
            <span className="status-pill"><span className={`dot ${health.t2i ? '' : 'off'}`} />T2I: {health.t2i ?? 'выкл.'}</span>
          </div>
        )}
      </header>
      <main>
        {tab === 'generate' && <Generate />}
        {tab === 'templates' && <Templates />}
        {tab === 'skills' && <Skills />}
      </main>
    </>
  )
}
