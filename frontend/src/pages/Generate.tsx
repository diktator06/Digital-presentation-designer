import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type ContentPack, type Run, type RunEvent, type TemplateCard, type Variant } from '../api'
import VariantViewer from '../components/VariantViewer'
import { scoreClass } from '../score'

const LAST_RUN = 'decksmith.lastRun'

const PURPOSES = [
  { id: 'feature', label: 'Фича' },
  { id: 'product', label: 'Продукт' },
  { id: 'project', label: 'Проект' },
  { id: 'initiative', label: 'Инициатива' },
]
const STAGES = [
  { id: 'build', label: 'Вёрстка' },
  { id: 'render', label: 'Рендер' },
  { id: 'audit', label: 'Аудит' },
  { id: 'autofix', label: 'Автоисправление' },
  { id: 'export', label: 'Экспорт' },
]
const VARIANTS = ['balanced', 'visual', 'dense']
const VARIANT_RU: Record<string, string> = { balanced: 'Сбалансированный', visual: 'Визуальный', dense: 'Аналитический' }

function fmt(s: number) {
  // секунды -> «м:сс» для таймера генерации
  const m = Math.floor(s / 60)
  const r = Math.floor(s % 60)
  return `${m}:${r.toString().padStart(2, '0')}`
}

export default function Generate() {
  // экран генерации: выбор шаблона и контента, бриф, прогресс трёх вариантов и их превью
  const [templates, setTemplates] = useState<TemplateCard[]>([])
  const [packs, setPacks] = useState<ContentPack[]>([])
  const [tpl, setTpl] = useState<string>('')
  const [pack, setPack] = useState<string>('')
  // контент-пакет, выбранный до включения демо-контента: к нему возвращает повторное нажатие кнопки
  const [packBeforeDemo, setPackBeforeDemo] = useState<string>('')
  const [contentError, setContentError] = useState('')
  const [purpose, setPurpose] = useState('product')
  const [nSlides, setNSlides] = useState(12)
  // бриф пишет пользователь: поле всегда начинается пустым
  const [brief, setBrief] = useState('')
  const [runId, setRunId] = useState<string | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [now, setNow] = useState(Date.now())
  const [open, setOpen] = useState<{ v: Variant; slide: number } | null>(null)
  const [busy, setBusy] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    // шаблоны датасета разбираются в фоне при старте сервера: опрашиваем список, пока он пуст
    const load = () =>
      api.templates().then((t) => {
        setTemplates(t)
        // выбранный шаблон мог быть удалён на вкладке «Шаблоны»: тогда берётся доступный
        const pick = t.find((x) => x.name.includes('tech'))?.id ?? t[0]?.id ?? ''
        setTpl((cur) => (cur && t.some((x) => x.id === cur) ? cur : pick))
      })
    load()
    const iv = setInterval(() => templates.length === 0 && load(), 4000)
    // пакет не выбирается сам: материалы не по теме брифа смешиваются с ним в колоде
    api.content().then(setPacks)
    return () => clearInterval(iv)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templates.length])

  useEffect(() => {
    // сервис хранит запуски: после перезагрузки страницы последний показывается снова (прогресс воспроизводится)
    let id: string | null = null
    try {
      id = localStorage.getItem(LAST_RUN)
    } catch {
      /* хранилище недоступно (приватный режим): начинаем с чистого листа */
    }
    if (!id) return
    api
      .run(id)
      .then((r) => {
        setRun(r)
        setRunId(id)
        // шаблон прошлого запуска выбирается снова, только если он ещё не удалён
        if (r.request?.template_id) api.template(r.request.template_id).then(() => setTpl(r.request.template_id)).catch(() => {})
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    // события прогресса запуска приходят по SSE; по завершении загружаем итог
    if (!runId) return
    setEvents([])
    const es = new EventSource(`/api/runs/${runId}/events`)
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data) as RunEvent
      setEvents((e) => [...e, ev])
      if (ev.stage === 'finished') {
        es.close()
        api.run(runId).then(setRun)
      }
    }
    return () => es.close()
  }, [runId])

  useEffect(() => {
    // таймер от нажатия Enter тикает, пока идёт генерация
    if (!run || run.status !== 'running') return
    const iv = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(iv)
  }, [run])

  const start = async () => {
    // старт генерации по Enter: запоминаем id запуска, чтобы восстановить его после перезагрузки
    if (!tpl || !brief.trim()) return
    const { run_id } = await api.startRun({ template_id: tpl, content_id: pack || null, brief, purpose, n_slides: nSlides })
    try {
      localStorage.setItem(LAST_RUN, run_id)
    } catch {
      /* хранилище недоступно: восстанавливать будет нечего */
    }
    setRun({ id: run_id, status: 'running', started: Date.now() / 1000, template: '', events: [], request: { template_id: tpl, brief, purpose, n_slides: nSlides } })
    setNow(Date.now())
    setRunId(run_id)
    setOpen(null)
  }

  const onKey = (e: React.KeyboardEvent) => {
    // Enter запускает генерацию, Shift+Enter — перенос строки в брифе
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      start()
    }
  }

  const uploadPack = async (files: FileList | null) => {
    // загрузка своего контент-пакета (PDF/DOCX/PPTX/MD/TXT/CSV/XLSX)
    if (!files?.length) return
    setBusy('content')
    setContentError('')
    try {
      const p = await api.uploadContent([...files])
      setPacks((x) => [p, ...x.filter((y) => y.id !== p.id)])
      setPack(p.id)
    } catch (e) {
      setContentError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }
  const demoOn = packs.some((p) => p.id === pack && p.demo)
  const toggleDemo = async () => {
    // переключатель: включает демо-контент из data/content/, повторное нажатие возвращает прежний выбор
    if (demoOn) {
      setPack(packBeforeDemo)
      return
    }
    setBusy('content')
    setContentError('')
    try {
      const p = await api.sampleContent()
      setPackBeforeDemo(pack === p.id ? '' : pack)
      setPacks((x) => [p, ...x.filter((y) => y.id !== p.id)])
      setPack(p.id)
    } catch (e) {
      setContentError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const elapsed = run ? (run.status === 'running' ? (now / 1000 - run.started) : run.elapsed_s ?? 0) : 0
  const planEv = events.find((e) => e.stage === 'plan' && e.status === 'done')
  const stageState = useMemo(() => {
    // последнее завершённое событие каждого этапа каждого варианта (для таблицы прогресса)
    const m: Record<string, RunEvent> = {}
    events.forEach((e) => {
      if (e.variant && e.status === 'done') m[`${e.variant}:${e.stage}`] = e
    })
    return m
  }, [events])

  const replaceVariant = (nv: Variant) => {
    // после исправлений в просмотрщике подменяем вариант новой версией
    setRun((r) => (r ? { ...r, variants: r.variants?.map((v) => (v.name === nv.name ? nv : v)) } : r))
    setOpen((o) => (o ? { ...o, v: nv } : o))
  }

  if (open && run) {
    return <VariantViewer run={run} variant={open.v} initialSlide={open.slide} onBack={() => setOpen(null)} onUpdate={replaceVariant} />
  }

  return (
    <div className="gen">
      <section className="panel pad composer col">
        <h2>Бриф</h2>
        <div className="field">
          <label>Шаблон</label>
          <div className="tpl-pick">
            {templates.map((t) => (
              <button key={t.id} className={tpl === t.id ? 'on' : ''} onClick={() => setTpl(t.id)} title={t.name}>
                {t.thumbnail && <img src={t.thumbnail} alt="" />}
                <span>{t.name}</span>
              </button>
            ))}
          </div>
          {templates.length === 0 && <span className="small muted"><span className="spin" /> шаблоны разбираются… (или загрузите свой на вкладке «Шаблоны»)</span>}
        </div>
        <div className="field">
          <label>Контент-пакет</label>
          <div className="row">
            <select className="grow" value={pack} onChange={(e) => setPack(e.target.value)}>
              <option value="">— только бриф —</option>
              {packs.map((p) => (
                <option key={p.id} value={p.id}>{p.files.join(', ')} · {p.chunks} фрагм.</option>
              ))}
            </select>
            <button className="btn sm" onClick={() => fileRef.current?.click()} disabled={busy === 'content'}>Загрузить</button>
            <input ref={fileRef} type="file" multiple hidden accept=".pdf,.docx,.pptx,.md,.txt,.csv,.xlsx" onChange={(e) => uploadPack(e.target.files)} />
          </div>
          <button
            className={`btn sm toggle ${demoOn ? 'on' : ''}`}
            style={{ alignSelf: 'flex-start' }}
            onClick={toggleDemo}
            disabled={busy === 'content'}
            aria-pressed={demoOn}
            title={demoOn ? 'Выключить демо-контент и вернуть прежний выбор' : 'Взять готовые материалы из папки data/content'}
          >
            {busy === 'content' ? <span className="spin" /> : demoOn ? '✓' : null} Демо-контент (data/content)
          </button>
          {contentError && <span className="badge err" style={{ whiteSpace: 'normal' }}>{contentError}</span>}
        </div>
        <div className="field">
          <label>Назначение</label>
          <div className="seg">
            {PURPOSES.map((p) => (
              <button key={p.id} className={purpose === p.id ? 'on' : ''} onClick={() => setPurpose(p.id)}>{p.label}</button>
            ))}
          </div>
        </div>
        <div className="field">
          <label>Слайдов: {nSlides}</label>
          <input type="range" min={6} max={15} value={nSlides} onChange={(e) => setNSlides(+e.target.value)} />
        </div>
        <div className="field">
          <label>Что рассказать</label>
          <textarea value={brief} onChange={(e) => setBrief(e.target.value)} onKeyDown={onKey} rows={6}
            placeholder="Кратко опишите, о чём презентация" />
        </div>
        <div className="send-row">
          <button className="btn primary" onClick={start} disabled={!tpl || !brief.trim() || run?.status === 'running'}>
            {run?.status === 'running' ? <span className="spin" /> : '↵'} Сгенерировать 3 варианта
          </button>
          <span className="small muted"><span className="kbd">Enter</span> — запуск</span>
        </div>
      </section>

      <section className="col">
        {!run && (
          <div className="panel empty">
            <h3 style={{ marginBottom: 8 }}>Шаблон и контент уже разобраны — осталось нажать Enter</h3>
            <div>Сервис построит структуру, сверстает три варианта по паттернам шаблона, проверит их и выгрузит PPTX, PDF и HTML.</div>
          </div>
        )}
        {run && (
          <div className="panel pad col">
            <div className="row">
              <div className="col" style={{ gap: 2 }}>
                <span className="small muted">от нажатия Enter</span>
                <span className="timer">{fmt(elapsed)}</span>
              </div>
              <div className="spacer" />
              {run.status === 'done' && (
                <span className={`badge ${elapsed <= 300 ? 'ok' : 'err'}`}>{elapsed <= 300 ? 'уложились в 5 минут' : 'дольше 5 минут'}</span>
              )}
              {planEv && <span className="badge blue">план: {planEv.mode === 'llm' ? 'LLM' : 'офлайн'} · {planEv.slides} слайдов</span>}
              {planEv?.materials === 'none' && (
                <span className="badge warn" title="Модель оценила материалы как не относящиеся к теме брифа: их факты на слайды не переносятся">
                  материалы не по теме брифа — колода по брифу
                </span>
              )}
              {planEv?.materials === 'partial' && (
                <span className="badge warn" title="Из материалов взяты только фрагменты по теме брифа">материалы по теме частично</span>
              )}
              {run.manifest && <a className="btn sm" href={run.manifest} target="_blank">manifest.json</a>}
            </div>
            <div className="stages">
              <span className="small muted">Структура</span>
              <div className={`stage-cell ${planEv ? 'done' : 'run'}`} style={{ gridColumn: 'span 3' }}>
                <span>{planEv ? `план готов` : 'LLM строит план колоды…'}</span>
                <span>{planEv?.t != null ? `${planEv.t.toFixed(1)} c` : <span className="spin" />}</span>
              </div>
              <span />
              {VARIANTS.map((v) => (
                <span key={v} className="small" style={{ fontWeight: 600 }}>{VARIANT_RU[v]}</span>
              ))}
              {STAGES.map((s) => (
                <Stage key={s.id} label={s.label} cells={VARIANTS.map((v) => stageState[`${v}:${s.id}`])} started={!!planEv} />
              ))}
            </div>
            {planEv?.titles && (
              <ol className="plan-list">
                {planEv.titles.map((t, i) => <li key={i}>{t}</li>)}
              </ol>
            )}
            {run.status === 'error' && <div className="badge err">{run.error}</div>}
          </div>
        )}
        {run?.variants && (
          <div className="variants">
            {run.variants.map((v) => (
              <div key={v.name} className="panel variant">
                <div className="variant-head row">
                  <div className="col grow" style={{ gap: 2 }}>
                    <h3>{v.title}</h3>
                    {v.description && <span className="small muted">{v.description}</span>}
                    <span className="small muted">{v.pngs.length} слайдов · {v.audit?.issues.length ?? 0} замечаний</span>
                  </div>
                  <div className="col" style={{ alignItems: 'flex-end', gap: 0 }}>
                    <span className={`score ${scoreClass(v.audit?.score)}`}>{v.audit?.score ?? '—'}</span>
                    <span className="small muted">
                      аудит{v.audit_before_fix ? ` (было ${v.audit_before_fix.score})` : ''}
                    </span>
                  </div>
                </div>
                {v.error ? (
                  <div className="pad badge err">{v.error}</div>
                ) : (
                  <div className="thumbs">
                    {v.pngs.map((p, i) => (
                      <img key={p} src={p} alt={`слайд ${i + 1}`} onClick={() => setOpen({ v, slide: i })} />
                    ))}
                  </div>
                )}
                <div className="variant-foot">
                  <button className="btn sm primary" onClick={() => setOpen({ v, slide: 0 })}>Аудит и правки</button>
                  {v.pptx && <a className="btn sm" href={v.pptx} download>PPTX</a>}
                  {v.pdf && <a className="btn sm" href={v.pdf} download>PDF</a>}
                  {v.html && <a className="btn sm" href={v.html} target="_blank">HTML</a>}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function Stage({ label, cells, started }: { label: string; cells: (RunEvent | undefined)[]; started: boolean }) {
  // строка таблицы прогресса: этап и его состояние в каждом из трёх вариантов
  return (
    <>
      <span className="small muted">{label}</span>
      {cells.map((c, i) => (
        <div key={i} className={`stage-cell ${c ? 'done' : started ? 'run' : ''}`}>
          <span>{c?.score != null ? `балл ${c.score}` : c ? 'готово' : ''}</span>
          <span>{c?.t != null ? `${c.t.toFixed(1)} c` : ''}</span>
        </div>
      ))}
    </>
  )
}
