export function scoreClass(s?: number | null) {
  // цвет бейджа балла аудита: зелёный от 85, жёлтый от 65, иначе красный
  if (s == null) return ''
  return s >= 85 ? 'good' : s >= 65 ? 'mid' : 'bad'
}
