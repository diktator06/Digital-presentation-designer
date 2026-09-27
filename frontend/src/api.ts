// Типизированный клиент API DeckSmith.

export type Box = { x: number; y: number; w: number; h: number }

export type TemplateCard = {
  id: string
  name: string
  slide_size_in: [number, number]
  patterns: Record<string, number>
  fonts: { heading: string; body: string; all: [string, boolean][] }
  type_scale: { sizes: number[]; title: number; subtitle: number; body: number; caption: number; number: number }
  accent: string
  chart_colors: string[]
  background: string
  dark: boolean
  text: string
  palette: string[]
  margins_in: Record<string, number>
  canvas_layouts: Record<string, string>
  brand_elements: number
  icons: number
  warnings: string[]
  thumbnail: string | null
  n_patterns: number
  n_layouts: number
  analysis_s?: number
}

export type Slot = { id: string; role: string; box: Box; kind: string; item_index: number | null; sample_text: string; max_chars: number }
export type Pattern = {
  id: string
  source: 'slide' | 'layout'
  slide_index: number | null
  layout_name: string
  kind: string
  n_items: number
  slots: Slot[]
  score_hint: number
  tags: string[]
  reason: string
  thumbnail: string | null
  overlay: string
}
export type TemplateDetail = { id: string; name: string; patterns: Pattern[]; summary: TemplateCard; tokens: { slide_w: number; slide_h: number } }

export type ContentPack = { id: string; files: string[]; chunks: number; tables: number; numbers: number; language: string }

export type AuditIssue = {
  id: string
  check: string
  category: string
  deterministic: boolean
  severity: 'error' | 'warning' | 'info'
  slide: number
  message: string
  boxes: Box[]
  fixable: boolean
  fix: string | null
  data: Record<string, unknown>
}
export type AuditReport = { slide_count: number; issues: AuditIssue[]; checks_run: string[]; score: number; duration_s: number }

export type SlideMeta = { spec: string; mode: string; pattern: string; source: number | null; kind: string; rationale: string }
export type Variant = {
  name: string
  title: string
  description?: string
  error: string
  pptx: string | null
  pdf: string | null
  html: string | null
  pngs: string[]
  audit: AuditReport | null
  audit_before_fix: { score: number; issues: number } | null
  slides: SlideMeta[]
  timings: Record<string, number>
  version: number
  last_fix?: { applied: string[]; skipped: string[]; seconds: number }
}
export type RunEvent = { stage: string; status?: string; variant?: string; t?: number; score?: number; issues?: number; mode?: string; slides?: number; titles?: string[]; message?: string; ts: number }
export type Run = {
  id: string
  status: 'running' | 'done' | 'error'
  template: string
  started: number
  elapsed_s?: number
  plan?: { title: string; slides: { id: string; intent: string; title: string }[] }
  plan_mode?: string
  timings?: Record<string, number>
  variants?: Variant[]
  events: RunEvent[]
  error?: string
  manifest?: string
  request: { template_id: string; brief: string; purpose: string; n_slides: number }
}

async function j<T>(r: Response): Promise<T> {
  // ответ сервера -> JSON; при ошибке HTTP — исключение с текстом ответа
  if (!r.ok) throw new Error((await r.text()) || r.statusText)
  return r.json() as Promise<T>
}

export const api = {
  health: () => fetch('/api/health').then(j<{ ok: boolean; llm: string; vlm: string | null; t2i: string | null; templates: number }>),
  templates: () => fetch('/api/templates').then(j<TemplateCard[]>),
  template: (id: string) => fetch(`/api/templates/${id}`).then(j<TemplateDetail>),
  uploadTemplate: (f: File) => {
    // шаблон разбирается на сервере сразу после загрузки
    const fd = new FormData()
    fd.append('file', f)
    return fetch('/api/templates', { method: 'POST', body: fd }).then(j<TemplateCard>)
  },
  content: () => fetch('/api/content').then(j<ContentPack[]>),
  uploadContent: (files: File[]) => {
    // контент-пакет: несколько файлов одним запросом
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return fetch('/api/content', { method: 'POST', body: fd }).then(j<ContentPack>)
  },
  sampleContent: () => fetch('/api/content/sample', { method: 'POST' }).then(j<ContentPack>),
  startRun: (body: { template_id: string; content_id: string | null; brief: string; purpose: string; n_slides: number }) =>
    fetch('/api/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j<{ run_id: string }>),
  run: (id: string) => fetch(`/api/runs/${id}`).then(j<Run>),
  runs: () => fetch('/api/runs').then(j<Run[]>),
  fix: (runId: string, variant: string, issueIds: string[]) =>
    fetch(`/api/runs/${runId}/variants/${variant}/fix`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue_ids: issueIds }),
    }).then(j<Variant>),
  skills: () =>
    fetch('/api/skills').then(
      j<{
        versions: { skills: Record<string, { active: string; versions: string[] }>; agents: Record<string, { active: string; versions: string[] }> }
        audit_checks: { id: string; category: string; title: string; deterministic: boolean; fixer: string | null }[]
      }>,
    ),
}

export const KIND_RU: Record<string, string> = {
  title: 'Титул', section: 'Раздел', agenda: 'Содержание', text: 'Текст', two_column: 'Две колонки', cards: 'Карточки',
  steps: 'Шаги', stats: 'Показатели', table: 'Таблица', chart: 'Диаграмма', image_text: 'Изображение', quote: 'Цитата',
  team: 'Команда', contacts: 'Контакты', thanks: 'Финал', free: 'Свободный', guide: 'Служебный',
  kpi: 'KPI', process: 'Процесс', bullets: 'Список', image: 'Изображение',
}
export const CAT_RU: Record<string, string> = { layout: 'Вёрстка', template: 'Шаблон', density: 'Плотность', integrity: 'Целостность', content: 'Контент' }
export const SEV_RU: Record<string, string> = { error: 'ошибка', warning: 'замечание', info: 'инфо' }
