import { useEffect, useRef, useState } from 'react'
import { api, KIND_RU, type TemplateCard, type TemplateDetail } from '../api'

export default function Templates() {
  // вкладка шаблонов: список, загрузка нового и декомпозиция слайдов-примеров
  const [list, setList] = useState<TemplateCard[]>([])
  const [sel, setSel] = useState<TemplateDetail | null>(null)
  const [overlay, setOverlay] = useState(true)
  const [kind, setKind] = useState<string>('all')
  const [uploading, setUploading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [opening, setOpening] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [error, setError] = useState('')
  const ref = useRef<HTMLInputElement>(null)

  useEffect(() => {
    api.templates().then(setList).finally(() => setLoading(false))
  }, [])

  const openTemplate = (id: string) => {
    // полный профиль шаблона: токены, паттерны и их слоты
    setOpening(id)
    api.template(id).then(setSel).finally(() => setOpening(null))
  }

  const upload = async (f: File | undefined) => {
    // новый шаблон разбирается на сервере сразу и открывается
    if (!f) return
    setUploading(true)
    try {
      const card = await api.uploadTemplate(f)
      setList((l) => [card, ...l.filter((x) => x.id !== card.id)])
      setSel(await api.template(card.id))
    } finally {
      setUploading(false)
    }
  }

  const remove = async (id: string, name: string) => {
    // удаление с подтверждением: шаблон убирается из списка и с сервера (разбор, превью, загруженный файл)
    const ok = window.confirm(
      `Удалить шаблон «${name}»?\n\nОн пропадёт из списка и будет удалён с сервера вместе с разбором и ` +
        'загруженным файлом — и после перезагрузки не вернётся. Уже созданные презентации останутся.',
    )
    if (!ok) return
    setDeleting(id)
    setError('')
    try {
      await api.deleteTemplate(id)
      setList((l) => l.filter((x) => x.id !== id))
      setSel((cur) => (cur && cur.id === id ? null : cur))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(null)
    }
  }

  if (sel) {
    const s = sel.summary
    const kinds = Object.keys(s.patterns)
    const pats = sel.patterns.filter((p) => p.source === 'slide' && (kind === 'all' || p.kind === kind))
    return (
      <div className="col">
        <div className="row">
          <button className="btn" onClick={() => setSel(null)}>← Все шаблоны</button>
          <h2>{sel.name}</h2>
          <span className="muted small">{s.slide_size_in[0]}″×{s.slide_size_in[1]}″ · {s.n_layouts} макетов · {s.n_patterns} паттернов</span>
          <div className="spacer" />
          {error && <span className="badge err">{error}</span>}
          <a
            className="btn sm primary"
            href={api.designSystemUrl(sel.id)}
            download
            title="Палитра, шрифты, кегли и поля шаблона: токены W3C (JSON), CSS-переменные, палитра SVG"
          >
            ⬇ Скачать дизайн-систему
          </a>
          <button className="btn sm danger" disabled={deleting === sel.id} onClick={() => remove(sel.id, sel.name)}>
            {deleting === sel.id ? <span className="spin" /> : null} Удалить шаблон
          </button>
        </div>
        <div className="tokens">
          <div className="panel pad col">
            <h3>Палитра</h3>
            <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
              {s.palette.map((c) => <div key={c} className="swatch" style={{ background: `#${c}` }} title={`#${c}`} />)}
            </div>
            <div className="small muted">акцент <b style={{ color: `#${s.accent}` }}>#{s.accent}</b> · фон #{s.background} · текст #{s.text}</div>
            <div className="row" style={{ gap: 4 }}>
              <span className="small muted">диаграммы</span>
              {s.chart_colors.map((c) => <div key={c} className="swatch" style={{ background: `#${c}`, width: 16, height: 16 }} />)}
            </div>
          </div>
          <div className="panel pad col">
            <h3>Типографика</h3>
            <div className="small">заголовки: <b>{s.fonts.heading}</b> · текст: <b>{s.fonts.body}</b></div>
            <div className="small muted">
              {s.fonts.all.map(([f, ok]) => (
                <span key={f} className={`badge ${ok ? 'ok' : 'warn'}`} style={{ marginRight: 4 }}>{f}{ok ? '' : ' (замена)'}</span>
              ))}
            </div>
            <div className="small">шкала: {s.type_scale.sizes.slice(0, 12).join(' · ')} pt</div>
            <div className="small muted">title {s.type_scale.title} · body {s.type_scale.body} · caption {s.type_scale.caption} · KPI {s.type_scale.number}</div>
          </div>
          <div className="panel pad col">
            <h3>Сетка и поля</h3>
            <div className="small">поля: ← {s.margins_in.left}″ · ↑ {s.margins_in.top}″ · → {s.margins_in.right}″ · ↓ {s.margins_in.bottom}″</div>
            <div className="small">фон контент-слайдов: {s.dark ? 'тёмный' : 'светлый'}</div>
            <div className="small">холсты для нативной вёрстки: {Object.entries(s.canvas_layouts).map(([k, v]) => `${k}: «${v}»`).join(', ')}</div>
            <div className="small muted">бренд-элементов: {s.brand_elements} · иконок в библиотеке: {s.icons}</div>
          </div>
          <div className="panel pad col">
            <h3>Паттерны</h3>
            <div className="row" style={{ flexWrap: 'wrap', gap: 4 }}>
              {Object.entries(s.patterns).map(([k, n]) => <span key={k} className="badge blue">{KIND_RU[k] ?? k}: {n}</span>)}
            </div>
            {s.warnings.map((w) => <div key={w} className="small badge warn">{w}</div>)}
          </div>
        </div>
        <div className="row">
          <h3>Декомпозиция примеров слайдов</h3>
          <div className="seg">
            <button className={kind === 'all' ? 'on' : ''} onClick={() => setKind('all')}>Все</button>
            {kinds.map((k) => <button key={k} className={kind === k ? 'on' : ''} onClick={() => setKind(k)}>{KIND_RU[k] ?? k}</button>)}
          </div>
          <div className="spacer" />
          <label className="row small"><input type="checkbox" checked={overlay} onChange={(e) => setOverlay(e.target.checked)} /> слоты поверх слайда</label>
        </div>
        <div className="pattern-grid">
          {pats.map((p) => (
            <div key={p.id} className="panel pattern">
              <img src={overlay ? p.overlay : p.thumbnail ?? ''} alt={p.id} loading="lazy" />
              <div className="p-meta">
                <div className="row">
                  <b>{p.id}</b>
                  <span className={`badge ${p.kind === 'guide' ? '' : 'blue'}`}>{KIND_RU[p.kind] ?? p.kind}{p.n_items ? ` × ${p.n_items}` : ''}</span>
                  {p.tags.map((t) => <span key={t} className="badge">{t}</span>)}
                  <div className="spacer" />
                  <span className="small muted">prior {p.score_hint.toFixed(2)}</span>
                </div>
                <span className="small muted">{p.reason} · макет «{p.layout_name}» · {p.slots.length} слотов</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="col">
      <div className="row">
        <h2>Шаблоны</h2>
        <span className="muted">шаблон читается как набор правил: токены, типографика, сетка и композиционные паттерны</span>
        <div className="spacer" />
        {error && <span className="badge err">{error}</span>}
      </div>
      <div className="tpl-grid">
        <div className="drop panel" onClick={() => ref.current?.click()} style={{ display: 'grid', placeItems: 'center', minHeight: 220 }}>
          {uploading ? (
            <span><span className="spin" /> разбираем шаблон… (рендер, CV, паттерны)</span>
          ) : (
            <span>+ Загрузить новый шаблон (.pptx, .potx, .ppt, .odp)<br /><span className="small">неизвестный шаблон разбирается автоматически</span></span>
          )}
          <input ref={ref} type="file" accept=".pptx,.potx,.pptm,.ppt,.odp,.otp" hidden onChange={(e) => upload(e.target.files?.[0])} />
        </div>
        {loading && (
          <div className="panel" style={{ display: 'grid', placeItems: 'center', minHeight: 220 }}>
            <span><span className="spin" /> загружаем шаблоны…</span>
          </div>
        )}
        {list.map((t) => (
          <div key={t.id} className="panel tpl-card" onClick={() => !opening && openTemplate(t.id)}>
            {t.thumbnail && <img src={t.thumbnail} alt="" />}
            <div className="pad col" style={{ gap: 6 }}>
              <div className="row">
                <h3 className="tpl-name" title={t.name}>{t.name}</h3>
                <div className="spacer" />
                {opening === t.id ? <span className="small"><span className="spin" /> открываем…</span> : <span className="badge blue">{t.n_patterns} паттернов</span>}
                <button
                  className="btn sm danger"
                  title="Удалить шаблон с сайта и с сервера"
                  disabled={deleting === t.id}
                  onClick={(e) => {
                    // кнопка на карточке не должна открывать сам шаблон
                    e.stopPropagation()
                    remove(t.id, t.name)
                  }}
                >
                  {deleting === t.id ? <span className="spin" /> : 'Удалить'}
                </button>
              </div>
              <div className="row" style={{ gap: 4 }}>{t.palette.slice(0, 10).map((c) => <div key={c} className="swatch" style={{ background: `#${c}`, width: 16, height: 16 }} />)}</div>
              <span className="small muted">{t.fonts.heading} / {t.fonts.body} · {t.dark ? 'тёмный' : 'светлый'} · {t.slide_size_in[0]}″×{t.slide_size_in[1]}″</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
