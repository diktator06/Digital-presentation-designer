"""Детерминированный выбор паттернов: DeckPlan × TemplateProfile × Variant -> решения.

Каждое решение несёт строку-обоснование, чтобы UI/защита могли показать, *почему*
выбран конкретный слайд шаблона. Одинаковые входные данные всегда дают одинаковый результат.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from PIL import Image

from decksmith.core.config import ROOT
from decksmith.core.models import Box, DeckPlan, Pattern, PatternKind, SlideLayout, SlideSpec, SlotRole, TemplateProfile
from decksmith.layout.room import clear_title_box, owned_height
from decksmith.layout.textfit import fits
from decksmith.parsing.ooxml import color_distance, rgb_to_hex
from decksmith.parsing.template_parser import occupancy_grid

K = PatternKind

COMPAT: dict[PatternKind, dict[PatternKind, float]] = {
    K.title: {K.title: 1.0},
    K.section: {K.section: 1.0, K.title: 0.45},
    K.agenda: {K.agenda: 1.0, K.steps: 0.55, K.cards: 0.45},
    K.text: {K.text: 1.0, K.two_column: 0.75, K.image_text: 0.55},
    K.two_column: {K.two_column: 1.0, K.text: 0.7, K.cards: 0.6},
    K.cards: {K.cards: 1.0, K.steps: 0.55, K.stats: 0.25},
    K.steps: {K.steps: 1.0, K.cards: 0.7},
    K.stats: {K.stats: 1.0, K.cards: 0.5},
    K.table: {K.table: 1.0},
    K.chart: {K.chart: 1.0},
    K.image_text: {K.image_text: 1.0, K.text: 0.5},
    K.quote: {K.quote: 1.0, K.section: 0.3},
    K.team: {K.team: 1.0, K.cards: 0.5},
    K.contacts: {K.contacts: 1.0, K.thanks: 0.8},
    K.thanks: {K.thanks: 1.0, K.contacts: 0.7, K.section: 0.35, K.title: 0.4},
}
ITEM_KINDS = {K.cards, K.steps, K.stats, K.agenda, K.team}
# содержательные слайды, у которых подзаголовок или одна фраза могут быть всем текстом слайда
LEAD_KINDS = {K.text, K.image_text, K.two_column}


@dataclass
class Variant:
    name: str
    title: str
    description: str
    kind_bias: dict[str, float]
    text_density: float = 1.0
    max_bullets: int = 5
    represent: dict[str, list[str]] = field(default_factory=dict)
    drop_sections: bool = False
    exec_summary_first: bool = False
    images: int = 1
    prefer_icons: bool = False
    tone: str = "auto"


@lru_cache(maxsize=4)
def load_variants(path: str | None = None) -> dict[str, Variant]:
    """Стратегии вариантов из config/variants.yaml."""
    p = Path(path) if path else ROOT / "config" / "variants.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return {k: Variant(name=k, **v) for k, v in data.items()}


def spec_items_count(spec: SlideSpec) -> int:
    """Число элементов, которые покажет слайд."""
    if spec.items:
        return len(spec.items)
    if spec.intent in ITEM_KINDS and spec.bullets:
        return len(spec.bullets)
    return 0


def spec_chars(spec: SlideSpec) -> int:
    """Объём текста слайда в символах."""
    n = len(spec.subtitle) + len(spec.message)
    n += sum(len(b) for b in spec.bullets)
    n += sum(len(i.title) + len(i.text) + len(i.value) for i in spec.items)
    return n


@lru_cache(maxsize=256)
def _cell_colors(png: str) -> tuple[tuple[str, ...], ...]:
    """Цвета клеток сетки 64×36 рендера пустого макета."""
    im = Image.open(png).convert("RGB").resize((64, 36))
    px = im.load()
    return tuple(tuple(rgb_to_hex(px[x, y]) for x in range(64)) for y in range(36))


def _layout_art(profile: TemplateProfile, p: Pattern) -> float:
    """Доля области под заголовком, которую макет занимает собственной графикой (панель под объект,
    орнамент) и которую не закрывает ни один слот паттерна. Фоном считается цвет, на котором стоит текст
    слотов: у макета «половина белая, половина тёмная» пустой остаётся та половина, где текста нет.
    """
    li = p.layout_index
    if li is None or not 0 <= li < len(profile.layouts):
        return 0.0
    lay, t = profile.layouts[li], profile.tokens
    if not lay.thumbnail or not Path(lay.thumbnail).exists():
        return 0.0
    cells = _cell_colors(lay.thumbnail)
    below = lay.title_box.b if lay.title_box else (t.title_box.b if t.title_box else int(0.2 * t.slide_h))
    m = t.margins
    x0, x1 = int(m.left / t.slide_w * 64), int((t.slide_w - m.right) / t.slide_w * 64)
    y0, y1 = int(below / t.slide_h * 36), int((t.slide_h - m.bottom) / t.slide_h * 36)
    region = [(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    if not region:
        return 0.0

    def slot_at(x: int, y: int, kinds=("text", "picture", "table", "chart")) -> bool:
        """Клетка внутри рамки какого-либо слота паттерна."""
        cx, cy = (x + 0.5) / 64 * t.slide_w, (y + 0.5) / 36 * t.slide_h
        return any(sl.kind in kinds and sl.box.x <= cx <= sl.box.r and sl.box.y <= cy <= sl.box.b for sl in p.slots)

    under_text = [cells[y][x] for y in range(36) for x in range(64) if slot_at(x, y, ("text",))]
    ref = max(set(under_text), key=under_text.count) if under_text else (lay.background_hex or t.background_hex)
    if under_text and sum(color_distance(c, ref) > 120 for c in under_text) > 0.3 * len(under_text):
        return 0.0  # пёстрый фон (фото, текстура) и под текстом: это фон, а не пустая панель
    art = [(x, y) for x, y in region if not slot_at(x, y) and color_distance(cells[y][x], ref) > 120]
    return len(art) / len(region)


def _layout_under_items(profile: TemplateProfile, p: Pattern) -> bool:
    """Макет сам рисует что-то под элементами повторителя (плитки, номера, иконки): спрятанный элемент
    оставил бы от себя пустую плитку макета.
    """
    li = p.layout_index
    if li is None or not 0 <= li < len(profile.layouts):
        return False
    lay, t = profile.layouts[li], profile.tokens
    if not lay.thumbnail or not Path(lay.thumbnail).exists():
        return False
    edges = _edge_cells(lay.thumbnail, lay.background_hex or t.background_hex)
    inside = [edges[y][x] for y in range(36) for x in range(64)
              if any(b.x <= (x + 0.5) / 64 * t.slide_w <= b.r and b.y <= (y + 0.5) / 36 * t.slide_h <= b.b
                     for b in p.repeaters[0].item_boxes)]
    # у плиток, номеров и иконок есть контуры, у градиентов и свечений фона — нет
    return bool(inside) and sum(inside) > 0.05 * len(inside)


@lru_cache(maxsize=256)
def _edge_cells(png: str, bg_hex: str) -> tuple[tuple[bool, ...], ...]:
    """Клетки 64×36 рендера пустого макета, в которых есть контуры нарисованного."""
    return tuple(tuple(row) for row in occupancy_grid(Path(png), bg_hex, 64, 36, edges_only=True))


def _removable(p: Pattern) -> bool:
    """Лишние элементы повторителя можно удалить без дыр: ряд или колонка сдвигаются по исходной длине."""
    # в сетке удалённые элементы оставляют пустой ряд, а подложки рядов и нумерация карточек часто
    # нарисованы в макете и не удаляются вместе с элементами
    return bool(p.repeaters) and p.repeaters[0].direction in ("row", "column")


@dataclass
class Candidate:
    score: float
    mode: str
    pattern: Pattern | None
    keep_items: int | None
    why: list[str]


def _row_length(p: Pattern) -> int:
    """Число элементов в первой строке повторителя, разложенного сеткой (0 для одной строки или колонки)."""
    boxes = p.repeaters[0].item_boxes if p.repeaters else []
    if len(boxes) < 4:
        return 0
    top = min(b.y for b in boxes)
    first = [b for b in boxes if b.y - top < 0.3 * b.h]
    return len(first) if 1 < len(first) < len(boxes) else 0


@lru_cache(maxsize=4096)
def _title_ratio(title: str, font: str, size: float, bold: bool, w: int, h: int, caps: bool = False,
                 tracking: float = 0.0) -> float:
    """Наибольшая из долей 100/85/70/60 % от кегля, при которой заголовок помещается в рамку (0, если ни
    одна).
    """
    return next((r for r in (1.0, 0.85, 0.7, 0.6) if fits([title], font, size * r, w, h, bold, caps, tracking)), 0.0)


def score_pattern(p: Pattern, spec: SlideSpec, variant: Variant, used: dict[str, int], prev: str | None,
                  has_image: bool = False, slide_area: int = 0, layout_photo: float = 0.0,
                  layout_uses: int = 0, title_clear: Box | None = None, layout_art: float = 0.0,
                  hide_ok: bool = True) -> Candidate | None:
    compat = COMPAT.get(spec.intent, {spec.intent: 1.0})
    w = compat.get(p.kind)
    if w is None or p.kind == K.guide or p.score_hint < 0.2:
        return None
    if spec.intent != K.quote and not any(s.role == SlotRole.title for s in p.slots):
        return None  # у каждого слайда, кроме цитаты, должен быть виден заголовок
    if p.kind == K.image_text and not has_image:
        return None  # пустая рамка под фото (или чужое фото) хуже другого макета
    why = [f"{spec.intent.value}->{p.kind.value} x{w:.2f}"]
    s = 3.0 * w
    need = spec_items_count(spec)
    keep = None
    if p.kind in ITEM_KINDS:
        if need == 0:
            return None
        if p.n_items == need:
            s += 1.2
            why.append(f"items {need}=={p.n_items}")
        elif p.n_items > need and _removable(p) and hide_ok and p.n_items - need <= 2 and need >= 2 and p.source == "slide":
            # (паттерны из одних макетов не могут прятать элементы: рендереры всё равно рисуют пустые рамки
            # макета)
            s += 0.2
            keep = need
            why.append(f"items {need}<{p.n_items} (hide {p.n_items - need})")
            cols = _row_length(p)
            if cols and need > cols and need % cols == 1:
                s -= 1.0  # 4 карточки в сетке из 3 колонок: три в ряд и одна отдельно ниже
                why.append(f"lone item in the last row ({need} in {cols} columns)")
        else:
            return None
    elif need > 0 and spec.intent in ITEM_KINDS:
        s -= 0.8  # элементы превратились бы в сплошной текст
    # пункты встают только в рамки основного текста или в элементы повторителя: без них они бы потерялись
    frames = {sl.role for sl in p.slots if sl.kind == "text" and sl.item_index is None}
    if spec.bullets and not p.repeaters and SlotRole.body not in frames:
        return None
    # слайд, текст которого — подзаголовок или одна фраза, тоже теряет его без рамки подзаголовка или текста
    if (spec.intent in LEAD_KINDS and not spec.bullets and not spec.items and (spec.subtitle or spec.message)
            and not p.repeaters and not frames & {SlotRole.body, SlotRole.subtitle}):
        return None
    # структура элементов: заголовкам+текстам нужны либо два слота, либо составная рамка
    if p.repeaters and spec.items:
        roles = set(p.repeaters[0].slot_roles)
        comp = any(sl.para_roles for sl in p.slots if sl.item_index == 0)
        has_titles = any(i.title for i in spec.items)
        has_texts = any(i.text for i in spec.items)
        if has_titles and has_texts and not comp and not (SlotRole.item_title in roles and SlotRole.item_text in roles):
            s -= 0.6
            why.append("packs title+text")
        if spec.intent == K.stats and SlotRole.number not in roles and not comp:
            s -= 1.0
            why.append("no number slot")
        first = [sl for sl in p.slots if sl.item_index == 0 and sl.kind == "text"]
        # заголовки элементов намного длиннее слота заголовка шаблона были бы уменьшены до нечитаемости
        caps = [sl.max_chars for sl in first if sl.role == SlotRole.item_title and not sl.para_roles and sl.max_chars]
        longest = max(len(i.title) for i in spec.items)
        if caps and longest > 1.3 * max(caps):
            s -= min(2.0, (longest / max(caps) - 1.3) * 1.5)
            why.append(f"item titles {longest}>{max(caps)} chars")
        # крупные слоты на элемент, которые этот контент оставляет пустыми (подписи значений без значений,
        # ...)
        has_values = any(i.value for i in spec.items)
        empty = [sl for sl in first if not sl.para_roles and (
            (sl.role == SlotRole.label and not has_values) or (sl.role == SlotRole.item_text and not has_texts
                                                               and any(x.role == SlotRole.item_title for x in first)))]
        total = sum(sl.box.area for sl in first) or 1
        share = sum(sl.box.area for sl in empty) / total
        if share > 0.3:
            s -= 1.5 * share
            why.append(f"item slots left empty {share:.0%}")
        # карточки с заголовком и телом (две рамки или одна рамка с двумя стилями) для элементов, которые
        # несут одну строку (заголовок или текст, который тогда уходит в заголовок): каждое тело остаётся
        # пустым
        if (comp or SlotRole.item_title in roles and SlotRole.item_text in roles) and not (has_titles and has_texts):
            s -= 2.0
            why.append("card bodies left empty")
    # заголовок длиннее, чем в примере: свободная часть рамки заголовка (до фоновой графики,
    # над подзаголовком, начинающимся внутри неё) вмещает его только более мелким кеглем,
    # чем на остальных слайдах колоды
    t_slot = next((sl for sl in p.slots if sl.role == SlotRole.title and sl.item_index is None and sl.kind == "text"), None)
    if t_slot is not None and t_slot.style.size and spec.title and spec.intent not in (K.quote,):
        tb = clear_title_box(t_slot.box, title_clear) or t_slot.box
        h = tb.h
        if spec.subtitle or spec.message:
            h = min(h, owned_height(t_slot, [sl for sl in p.slots if sl.kind == "text" and sl.item_index is None
                                             and sl.role in (SlotRole.subtitle, SlotRole.body)]))
        r = _title_ratio(spec.title, t_slot.style.font or "Arial", t_slot.style.size, t_slot.style.bold, tb.w, h,
                         t_slot.style.caps, t_slot.style.tracking)
        if r < 1.0:
            s -= {0.85: 0.4, 0.7: 0.9, 0.6: 1.4}.get(r, 2.0)
            why.append(f"title at {r:.0%} of its size" if r else "title does not fit")
    # свободные слоты, которые спецификация не может заполнить, после удаления оставляют дыры
    fillable = {SlotRole.title, SlotRole.table, SlotRole.chart, SlotRole.icon, SlotRole.image, SlotRole.decor}
    if spec.subtitle or spec.message:
        fillable |= {SlotRole.subtitle, SlotRole.body}
    if spec.bullets or spec.quote:
        fillable.add(SlotRole.body)
    if spec.intent == K.quote and spec.quote_author:
        fillable |= {SlotRole.caption, SlotRole.label, SlotRole.person}
    holes = [sl for sl in p.slots if sl.item_index is None and sl.kind == "text" and sl.role not in fillable]
    # вводная фраза разделителя / финального слайда (призыв к действию в питче) должна оставаться видимой:
    # без рамки подзаголовка она занимает свободную рамку вроде подписи, без неё — потерялась бы
    if (spec.subtitle or spec.message) and spec.intent in (K.section, K.thanks) and not any(
            sl.kind == "text" and sl.item_index is None and sl.role in (SlotRole.subtitle, SlotRole.body) for sl in p.slots):
        spare = sorted([sl for sl in holes if sl.role in (SlotRole.person, SlotRole.caption, SlotRole.label)], key=lambda sl: -sl.box.area)
        if spare:
            holes.remove(spare[0])
            s -= 0.5  # рамка подписи — более скромное место для неё, чем подзаголовок
            why.append("lead line in a caption frame")
        else:
            s -= 2.0
            why.append("no frame for the lead line")
    if holes:
        s -= 0.25 * len(holes)
        why.append(f"{len(holes)} unfilled")
    # слоты, оставленные пустыми на окрашенных плейсхолдерах макета, в некоторых рендерерах видны как пустые
    # карточки
    painted = [sl for sl in p.slots if sl.painted]
    if painted and slide_area:
        n_items = keep if keep is not None else (need if p.kind in ITEM_KINDS else 0)
        has_values = any(i.value for i in spec.items)
        has_texts = any(i.text for i in spec.items) or bool(spec.bullets)

        def unused(sl) -> bool:
            """Слот останется пустым при этом содержании."""
            if sl.kind == "picture":
                return not has_image
            if sl.item_index is not None:
                return (sl.item_index >= n_items or (sl.role == SlotRole.label and not has_values)
                        or (sl.role == SlotRole.item_text and not has_texts))
            return sl.role not in fillable

        share = sum(sl.box.area for sl in painted if unused(sl)) / slide_area
        if share > 0.01:
            s -= min(3.0, 12.0 * share)
            why.append(f"empty painted frames {share:.0%}")
    # макет, у которого большую часть места под заголовком занимает собственная графика (панель под объект,
    # орнамент), без изображения оставляет на содержательном слайде пустую область
    if (layout_art > 0.45 and p.source == "layout" and spec.intent not in (K.title, K.section, K.thanks, K.quote)
            and not (has_image and any(sl.kind == "picture" for sl in p.slots))):
        s -= 1.5
        why.append(f"layout art {layout_art:.0%} of the content area")
    # фото, вшитые в макет, видны на каждом слайде на его основе, о чём бы он ни был:
    # уместно на обложке или разделителе, не по теме рядом с содержанием и никогда дважды в колоде
    if layout_photo > 0.15 and spec.intent not in (K.title, K.section, K.thanks):
        s -= 1.0 + 2.0 * layout_uses
        why.append(f"layout photos {layout_photo:.0%}" + (f", used x{layout_uses}" if layout_uses else ""))
    # рамки под фото без изображения остаются пустыми (и удаляются): освободившаяся область — тоже дыра
    if not has_image and slide_area:
        pic = sum(sl.box.area for sl in p.slots if sl.kind == "picture" and sl.role == SlotRole.image
                  and sl.box.area > 0.04 * slide_area)
        if pic:
            share = min(pic / slide_area, 1.0)
            s -= min(3.0, 10.0 * share)
            why.append(f"empty photo area {share:.0%}")
    # вместимость текста против объёма контента
    chars = spec_chars(spec) * variant.text_density
    cap = max(p.text_capacity, 1)
    if chars:
        r = chars / cap
        if r > 1.1:
            s -= min(2.5, (r - 1.1) * 2.0)
            why.append(f"tight {r:.1f}x")
        elif r < 0.15 and p.kind not in (K.title, K.section, K.thanks, K.quote):
            s -= 0.4
            why.append("sparse")
    bias = variant.kind_bias.get(p.kind.value, 1.0)
    s += math.log(max(bias, 0.05)) * 1.5
    s += 2.0 * (p.score_hint - 1.0)
    n_used = used.get(p.id, 0)
    if n_used:
        s -= 0.9 * n_used
        why.append(f"reused x{n_used}")
    if prev == p.id:
        s -= 1.5
    if p.source == "layout":
        s -= 0.15
    return Candidate(score=s, mode="clone", pattern=p, keep_items=keep, why=why)


def compose_candidates(spec: SlideSpec, variant: Variant, has_image: bool = False) -> list[Candidate]:
    out = []
    rep = variant.represent
    data_pref = rep.get("data", ["chart", "table", "kpi"])
    item_pref = rep.get("items", ["cards", "steps", "bullets"])

    def pref_bonus(lst, name):
        """Бонус за место способа отрисовки в списке предпочтений варианта."""
        return (len(lst) - lst.index(name)) * 0.35 if name in lst else 0.0

    if spec.chart and spec.chart.series:
        data = [Candidate(3.2 + pref_bonus(data_pref, "chart"), "compose:chart", None, None, ["native chart from data"])]
        if len(spec.chart.categories) <= 7:
            data.append(Candidate(2.6 + pref_bonus(data_pref, "table"), "compose:table", None, None, ["chart data as table"]))
        if len(spec.chart.series) == 1 and len(spec.chart.categories) <= 4:
            data.append(Candidate(2.5 + pref_bonus(data_pref, "kpi"), "compose:kpi", None, None, ["few values as KPI tiles"]))
        # побеждает первое доступное в варианте представление рядов данных (его идентичность)
        first = next((c for name in data_pref for c in data if c.mode == f"compose:{name}"), None)
        if first is not None:
            first.score += 1.0
            first.why.append("variant's data view")
        out += data
    if spec.table and spec.table.rows:
        out.append(Candidate(3.3 + pref_bonus(data_pref, "table"), "compose:table", None, None, ["native table"]))
    if spec.items:
        n = len(spec.items)
        if spec.intent == K.steps or spec.intent == K.agenda:
            out.append(Candidate(2.4 + pref_bonus(item_pref, "steps"), "compose:process", None, None, [f"{n}-step process diagram"]))
        valued = sum(bool(i.value) for i in spec.items)
        # плитки KPI — это числа; без значений заголовок встал бы вместо числа крупным кеглем
        if valued * 2 >= n and (spec.intent == K.stats or valued == n):
            bonus = 1.2 if spec.intent == K.stats and n <= 4 else 0.0
            out.append(Candidate(2.3 + bonus + pref_bonus(data_pref, "kpi"), "compose:kpi", None, None, ["value tiles"]))
        out.append(Candidate(2.0 + pref_bonus(item_pref, "cards"), "compose:cards", None, None, [f"{n} icon cards"]))
        out.append(Candidate(1.6 + pref_bonus(item_pref, "bullets"), "compose:bullets", None, None, ["items as bullet list"]))
    if spec.bullets and not spec.items:
        out.append(Candidate(1.9 + pref_bonus(item_pref, "bullets"), "compose:bullets", None, None, ["bullet list"]))
    if spec.intent == K.image_text and variant.images and has_image:
        out.append(Candidate(2.2, "compose:image", None, None, ["picture + text"]))
    if spec.intent == K.quote and spec.quote:
        out.append(Candidate(2.6, "compose:quote", None, None, ["large quote in template type"]))
    if spec.intent in (K.section, K.thanks, K.title):
        out.append(Candidate(1.0, "compose:section", None, None, ["divider composed in template type"]))
    return out


def _chart_compatible(p: Pattern, spec: SlideSpec) -> bool:
    """Нативная диаграмма из примера переиспользуется только для того же семейства диаграмм."""
    if p.kind != K.chart:
        return True
    if not spec.chart:
        return False
    kinds = " ".join(t for t in p.tags if t.startswith("chart:")).upper()
    fam = {"bar": ("BAR", "COLUMN"), "column": ("BAR", "COLUMN"), "line": ("LINE",), "pie": ("PIE", "DOUGHNUT"),
           "doughnut": ("PIE", "DOUGHNUT")}[spec.chart.type]
    return any(f in kinds for f in fam)


def _key(c: "Candidate") -> str:
    """Ключ кандидата для сравнения выборов вариантов."""
    return c.pattern.id if c.pattern else c.mode


def select_layouts(plan: DeckPlan, profile: TemplateProfile, variant: Variant,
                   avoid: dict[str, set[str]] | None = None, images: set[str] | None = None) -> list[SlideLayout]:
    """`avoid` — выборы, уже сделанные другими вариантами для каждого слайда: предпочитается почти равноценная
    альтернатива, чтобы три варианта заметно отличались, оставаясь в рамках шаблона.
    """
    patterns = profile.usable_patterns()
    used: dict[str, int] = {}
    # слайды, уже стоящие на макетах с вшитыми фото (коллажи часто повторяются в разных макетах)
    photo_uses = 0
    prev: str | None = None
    out: list[SlideLayout] = []
    avoid = avoid or {}
    images = images or set()
    art: dict[str, float] = {}  # доля графики макета по паттернам (считается один раз на выбор)
    hide: dict[str, bool] = {}  # можно ли прятать лишние элементы повторителя
    for spec in plan.slides:
        has_image = spec.id in images
        cands: list[Candidate] = []
        for p in patterns:
            li = p.layout_index if p.layout_index is not None and 0 <= p.layout_index < len(profile.layouts) else None
            if p.id not in art:
                art[p.id] = _layout_art(profile, p) if p.source == "layout" else 0.0
                hide[p.id] = not (p.repeaters and _layout_under_items(profile, p))
            c = score_pattern(p, spec, variant, used, prev, has_image, profile.tokens.slide_w * profile.tokens.slide_h,
                              profile.layouts[li].photo_share if li is not None else 0.0, photo_uses,
                              profile.layouts[li].title_clear if li is not None else None,
                              art[p.id], hide[p.id])
            if c:
                cands.append(c)
        comp = compose_candidates(spec, variant, has_image)
        # структурированные данные всегда рисуются нативно (диаграммы/таблицы из данных)
        if spec.chart or spec.table:
            cands = [c for c in cands if c.pattern and c.pattern.kind in (K.table, K.chart) and "shape_table" not in c.pattern.tags
                     and "shape_chart" not in c.pattern.tags and _chart_compatible(c.pattern, spec)]
        cands += comp
        # одна брендовая обложка
        taken = avoid.get(spec.id, set()) if spec.intent not in (K.title, K.thanks) else set()
        if taken and len(cands) > 1:
            for c in cands:
                if (spec.chart or spec.table) and c.pattern is None:
                    # способ отрисовки данных — собственное предпочтение каждого варианта (represent.data)
                    continue
                if _key(c) in taken:
                    c.score -= 1.3
                    c.why.append("taken by another variant")
        if not cands:
            cands = [Candidate(0.0, "compose:bullets", None, None, ["no compatible pattern: composed list"])]
        cands.sort(key=lambda c: (-c.score, c.pattern.id if c.pattern else c.mode))
        best = cands[0]
        alt = ", ".join(f"{(c.pattern.id if c.pattern else c.mode)}={c.score:.2f}" for c in cands[1:3])
        rationale = f"{best.mode}:{best.pattern.id if best.pattern else ''} score={best.score:.2f} [{'; '.join(best.why)}] alt: {alt}"
        if best.pattern:
            used[best.pattern.id] = used.get(best.pattern.id, 0) + 1
            bl = best.pattern.layout_index
            if bl is not None and 0 <= bl < len(profile.layouts) and profile.layouts[bl].photo_share > 0.15 \
                    and spec.intent not in (K.title, K.section, K.thanks):
                photo_uses += 1
            prev = best.pattern.id
            out.append(SlideLayout(spec_id=spec.id, mode="clone", pattern_id=best.pattern.id, keep_items=best.keep_items, rationale=rationale))
        else:
            prev = best.mode
            out.append(SlideLayout(spec_id=spec.id, mode="compose", compose_kind=best.mode.split(":", 1)[1], rationale=rationale))
    return out
