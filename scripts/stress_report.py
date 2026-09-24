"""Render a stress-test report.json into a Markdown table (docs/STRESS_RESULTS.md)."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main(report: str, out: str) -> None:
    rows = json.loads(Path(report).read_text(encoding="utf-8"))
    lines = ["# Результаты слепого стресс-теста", "",
             "Сгенерировано `scripts/stress_report.py` из `report.json` (`scripts/stress.py`).", "",
             "| Шаблон | Размер | Паттернов | Разбор, с | Сбаланс. | Визуальный | Аналитич. | Ошибок | Различимость |",
             "|---|---|---|---|---|---|---|---|---|"]
    allv, errs = [], Counter()
    for r in sorted(rows, key=lambda x: x["template"].lower()):
        if "crash" in r:
            lines.append(f"| {r['template']} | — | — | — | падение: {r['crash'][:60]} | | | | |")
            continue
        v = r["variants"]
        allv += v.values()
        for x in v.values():
            errs.update({k: n for k, n in x["by_check"].items()})
        size = "×".join(f"{d}" for d in r.get("size_in", [])) or "—"
        lines.append(f"| {r['template']} | {size} | {r['n_usable']} | {r['analyze_s']} | {v['balanced']['score']} | "
                     f"{v['visual']['score']} | {v['dense']['score']} | {sum(x['errors'] for x in v.values())} | "
                     f"{r['variant_distinctness']} |")
    n = max(len(allv), 1)
    lines += ["", f"Колод: {len(allv)}, средний балл аудита: {sum(x['score'] for x in allv) / n:.1f}, "
                  f"минимальный: {min((x['score'] for x in allv), default=0)}, колод с ошибками: {sum(x['errors'] > 0 for x in allv)}, "
                  f"расхождений числа слайдов: {sum(x['slides'] != x['expected_slides'] for x in allv)}.", "",
              "Находки (замечания и ошибки, без info):", ""]
    lines += [f"- `{k}`: {c}" for k, c in errs.most_common()]
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
