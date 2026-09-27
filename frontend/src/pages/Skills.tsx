import { useEffect, useState } from 'react'
import { api, CAT_RU } from '../api'

type Data = Awaited<ReturnType<typeof api.skills>>

export default function Skills() {
  // вкладка скиллов: версии промптов/агентов и каталог проверок аудита
  const [d, setD] = useState<Data | null>(null)
  useEffect(() => {
    api.skills().then(setD)
  }, [])
  if (!d) return <div className="empty"><span className="spin" /></div>
  const det = d.audit_checks.filter((c) => c.deterministic)
  const ctx = d.audit_checks.filter((c) => !c.deterministic)
  return (
    <div className="col">
      <div className="row"><h2>Скиллы, агенты и проверки</h2><span className="muted">промпты и конфиги лежат отдельными версионируемыми файлами в skills/</span></div>
      <div className="row" style={{ alignItems: 'stretch' }}>
        <div className="panel pad grow">
          <h3 style={{ marginBottom: 8 }}>Скиллы</h3>
          <table className="plain">
            <thead><tr><th>скилл</th><th>активная версия</th><th>все версии</th></tr></thead>
            <tbody>
              {Object.entries(d.versions.skills).map(([k, v]) => (
                <tr key={k}><td><b>{k}</b></td><td><span className="badge blue">{v.active}</span></td><td className="muted">{v.versions.join(', ')}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel pad" style={{ width: 380 }}>
          <h3 style={{ marginBottom: 8 }}>Агенты (воркфлоу)</h3>
          <table className="plain">
            <tbody>
              {Object.entries(d.versions.agents).map(([k, v]) => (
                <tr key={k}><td><b>{k}</b></td><td><span className="badge blue">{v.active}</span></td><td className="muted">{v.versions.join(', ')}</td></tr>
              ))}
            </tbody>
          </table>
          <p className="small muted">Каждый запуск пишет manifest.json: версии и sha256 всех скиллов, модели, тайминги и обоснование выбора макета для каждого слайда.</p>
        </div>
      </div>
      <div className="row" style={{ alignItems: 'stretch' }}>
        <div className="panel pad grow">
          <h3 style={{ marginBottom: 8 }}>Детерминированные проверки · {det.length}</h3>
          <p className="small muted">Одинаковый результат на одном и том же слайде: координаты, размеры, коды цветов, ссылки на макеты, пиксели рендера.</p>
          <table className="plain">
            <thead><tr><th>группа</th><th>проверка</th><th>id</th><th>исправление</th></tr></thead>
            <tbody>
              {det.map((c) => (
                <tr key={c.id}><td>{CAT_RU[c.category]}</td><td>{c.title}</td><td className="muted small">{c.id}</td><td>{c.fixer ? <span className="badge blue">{c.fixer}</span> : '—'}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel pad grow">
          <h3 style={{ marginBottom: 8 }}>Контекстуальные проверки (VLM) · {ctx.length}</h3>
          <p className="small muted">Касаются смысла: модель смотрит на картинку слайда и отвечает «да/нет». Могут отличаться между запусками — поэтому не применяются без выбора пользователя.</p>
          <table className="plain">
            <thead><tr><th>проверка</th><th>id</th><th>исправление</th></tr></thead>
            <tbody>
              {ctx.map((c) => (
                <tr key={c.id}><td>{c.title}</td><td className="muted small">{c.id}</td><td>{c.fixer ? <span className="badge ctx">{c.fixer}</span> : '—'}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
