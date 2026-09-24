import { useEffect, useMemo, useState } from 'react'
import { api, CAT_RU, KIND_RU, SEV_RU, type AuditIssue, type Run, type Variant } from '../api'
import { scoreClass } from '../pages/Generate'

type Props = { run: Run; variant: Variant; initialSlide: number; onBack: () => void; onUpdate: (v: Variant) => void }

export default function VariantViewer({ run, variant, initialSlide, onBack, onUpdate }: Props) {
  const [slide, setSlide] = useState(initialSlide)
  const [dims, setDims] = useState<{ w: number; h: number }>({ w: 12192000, h: 6858000 })
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [focus, setFocus] = useState<string | null>(null)
  const [onlySlide, setOnlySlide] = useState(true)
  const [showInfo, setShowInfo] = useState(false)
  const [fixing, setFixing] = useState(false)

  useEffect(() => {
    api.template(run.request.template_id).then((t) => setDims({ w: t.tokens.slide_w, h: t.tokens.slide_h }))
  }, [run.request.template_id])

  const issues = variant.audit?.issues ?? []
  const perSlide = useMemo(() => {
    const m: Record<number, AuditIssue[]> = {}
    issues.forEach((i) => (m[i.slide] ||= []).push(i))
    return m
  }, [issues])
  const visible = issues.filter((i) => (!onlySlide || i.slide === slide) && (showInfo || i.severity !== 'info'))
  const groups = useMemo(() => {
    const g: Record<string, AuditIssue[]> = {}
    visible.forEach((i) => (g[i.category] ||= []).push(i))
    return g
  }, [visible])
  const meta = variant.slides[slide]
  const slideIssues = (perSlide[slide] ?? []).filter((i) => showInfo || i.severity !== 'info')

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const selectAllFixable = () => setSelected(new Set(issues.filter((i) => i.fixable && i.severity !== 'info').map((i) => i.id)))

  const doFix = async () => {
    setFixing(true)
    try {
      const nv = await api.fix(run.id, variant.name, [...selected])
      onUpdate(nv)
      setSelected(new Set())
    } finally {
      setFixing(false)
    }
  }

  const pct = (v: number, total: number) => `${(v / total) * 100}%`
  const nDet = issues.filter((i) => i.deterministic).length

  return (
    <div className="col">
      <div className="row">
        <button className="btn" onClick={onBack}>← К вариантам</button>
        <h2>{variant.title}</h2>
        <span className={`score ${scoreClass(variant.audit?.score)}`}>{variant.audit?.score}</span>
        <span className="muted small">
          {issues.length} замечаний: {nDet} детерминированных, {issues.length - nDet} контекстуальных · версия {variant.version}
        </span>
        <div className="spacer" />
        {variant.last_fix && <span className="badge ok">исправлено {variant.last_fix.applied.length} за {variant.last_fix.seconds} c</span>}
        {variant.pptx && <a className="btn sm" href={variant.pptx} download>PPTX</a>}
        {variant.pdf && <a className="btn sm" href={variant.pdf} download>PDF</a>}
        {variant.html && <a className="btn sm" href={variant.html} target="_blank">HTML</a>}
      </div>
      <div className="viewer">
        <div className="rail">
          {variant.pngs.map((p, i) => {
            const n = (perSlide[i] ?? []).filter((x) => x.severity !== 'info').length
            return (
              <button key={p} className={i === slide ? 'on' : ''} onClick={() => setSlide(i)}>
                <img src={p} alt="" />
                {n > 0 && <span className={`badge cnt ${perSlide[i]?.some((x) => x.severity === 'error') ? 'err' : 'warn'}`}>{n}</span>}
              </button>
            )
          })}
        </div>
        <div className="stage-wrap">
          <div className="slide-stage">
            <div className="slide-frame">
              <img src={variant.pngs[slide]} alt="" />
              {slideIssues.flatMap((iss) =>
                iss.boxes.map((b, k) => (
                  <div
                    key={iss.id + k}
                    className={`hl ${iss.severity} ${focus === iss.id ? 'focus' : ''}`}
                    style={{ left: pct(b.x, dims.w), top: pct(b.y, dims.h), width: pct(b.w, dims.w), height: pct(b.h, dims.h) }}
                  />
                )),
              )}
            </div>
          </div>
          {meta && (
            <div className="why">
              <b>Слайд {slide + 1}</b> · {meta.mode === 'clone' ? `паттерн шаблона ${meta.pattern}` : `нативная композиция: ${KIND_RU[meta.pattern] ?? meta.pattern}`} · тип{' '}
              {KIND_RU[meta.kind] ?? meta.kind}
              {'\n'}почему: {meta.rationale}
            </div>
          )}
        </div>
        <div className="panel issues">
          <div className="pad col" style={{ gap: 8, borderBottom: '1px solid var(--line)' }}>
            <div className="row">
              <h3>Аудит</h3>
              <div className="spacer" />
              <div className="seg">
                <button className={onlySlide ? 'on' : ''} onClick={() => setOnlySlide(true)}>Этот слайд</button>
                <button className={!onlySlide ? 'on' : ''} onClick={() => setOnlySlide(false)}>Вся колода</button>
              </div>
            </div>
            <div className="row small">
              <label className="row" style={{ gap: 6 }}>
                <input type="checkbox" checked={showInfo} onChange={(e) => setShowInfo(e.target.checked)} /> показать инфо
              </label>
              <div className="spacer" />
              <button className="btn sm" onClick={selectAllFixable}>Выбрать исправимые</button>
              <button className="btn sm primary" disabled={!selected.size || fixing} onClick={doFix}>
                {fixing ? <span className="spin" /> : null} Исправить ({selected.size})
              </button>
            </div>
          </div>
          {visible.length === 0 && <div className="empty">Замечаний нет</div>}
          {Object.entries(groups).map(([cat, list]) => (
            <div key={cat}>
              <div className="group-title">{CAT_RU[cat] ?? cat} · {list.length}</div>
              {list.map((i) => (
                <div
                  key={i.id}
                  className={`issue ${selected.has(i.id) ? 'sel' : ''}`}
                  onMouseEnter={() => setFocus(i.id)}
                  onMouseLeave={() => setFocus(null)}
                  onClick={() => setSlide(i.slide)}
                >
                  <input
                    type="checkbox"
                    disabled={!i.fixable}
                    checked={selected.has(i.id)}
                    onChange={() => toggle(i.id)}
                    onClick={(e) => e.stopPropagation()}
                    title={i.fixable ? `исправить: ${i.fix}` : 'автоисправления нет'}
                  />
                  <div>
                    <div className="msg">{i.message}</div>
                    <div className="meta">
                      <span className={`badge ${i.severity === 'error' ? 'err' : i.severity === 'warning' ? 'warn' : ''}`}>{SEV_RU[i.severity]}</span>
                      <span className={`badge ${i.deterministic ? 'det' : 'ctx'}`}>{i.deterministic ? 'детерминированная' : 'контекстуальная (VLM)'}</span>
                      <span className="badge">{i.check}</span>
                      {!onlySlide && <span className="badge">слайд {i.slide + 1}</span>}
                      {i.fix && <span className="badge blue">→ {i.fix}</span>}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
