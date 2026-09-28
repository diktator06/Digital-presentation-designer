"""Извлечение композиционных паттернов.

Шаблон рассматривается как *библиотека примеров композиций*. Для каждого
слайда-примера определяются:
  * слот заголовка,
  * повторители: наборы визуально одинаковых элементов (карточки, шаги, плитки KPI),
    найденные кластеризацией текстовых элементов по (кеглю, насыщенности, ширине)
    и проверкой их расположения (строка / колонка / сетка),
  * оставшиеся свободные слоты (подзаголовок, тело, подписи, картинки, таблицы),
  * вид паттерна (title / section / cards / steps / stats / ...)
    с понятным человеку обоснованием, чтобы решение можно было проверить.

Здесь нет ничего специфичного для трёх выданных шаблонов: правила работают
только с геометрией, типографикой и общими ключевыми словами RU/EN.
"""
from __future__ import annotations

import re
from collections import defaultdict

from decksmith.core.models import Box, Pattern, PatternKind, Repeater, Slot, SlotRole, TextStyle
from decksmith.parsing.elements import Element, is_numberish

EMU_PT = 12700

KW = {
    "thanks": r"спасибо|благодар|thank|q\s*&\s*a|вопросы\??$|questions|end slide|closing|финальн",
    "agenda": r"содержани|оглавлени|agenda|contents|повестк|план презентации|table of contents",
    "quote": r"цитат|quote|«.{40,}",
    "section": r"раздел|разделител|section|divider|chapter|перебивк",
    "title": r"титул|обложк|cover|title slide|название\s*презентации|тема презентации|^title$",
    "team": r"имя фамилия|команд|team|спикер|speaker|визитк",
    "contacts": r"qr|call to action|контакт|contact|ссылк|связаться",
    "timeline": r"таймлайн|timeline|этап|stage|roadmap|дорожн|шаг|step|ганта|gantt",
    "chart": r"диаграмм|график|chart|гистограм",
    "table": r"таблиц|table",
}


# Текст, обращённый к тому, кто заполняет шаблон (а не образец содержания): «используй слайд 7 для
# оформления», «обязательный блок», «this template». Простые подсказки-заглушки («вставьте текст») сюда НЕ
# входят: это обычное содержание слайдов-примеров, и оно всё равно заменяется.
INSTRUCTION_RE = re.compile(
    r"(используй(?:те)?\b|для оформления|эт(?:от|ом|ому|им) шаблон\w*|шаблон(?:ом|а)? презентации|обязательный блок|"
    r"не забудь(?:те)?\b|привет,|use this (?:slide|layout|template)|this template|how to use)", re.I)
MEDIA_FILLER = re.compile(r"вставить|вставьте|insert|qr[- ]?(code|код)?$|^qr|фото$|photo|логотип|logo|иллюстрац", re.I)


def _kw(name: str, text: str) -> bool:
    """Текст содержит ключевое слово группы `name` (RU/EN)."""
    return re.search(KW[name], text, re.IGNORECASE) is not None


def _pt(emu: int) -> float:
    """EMU -> пункты."""
    return emu / EMU_PT


def estimate_capacity(box: Box, size_pt: float, avail_h: int | None = None) -> tuple[int, int]:
    """(max_chars, max_lines) для текстовой рамки по средней ширине глифа."""
    size = max(size_pt or 12.0, 4.0)
    w_pt = max(_pt(box.w) - 14.4, size)  # внутренние отступы слева/справа по умолчанию 0.1"
    h_pt = _pt(avail_h if avail_h is not None else box.h) - 7.2
    cpl = max(int(w_pt / (size * 0.55)), 1)
    lines = max(int(h_pt / (size * 1.2)), 1)
    return int(cpl * lines * 0.92), lines


# ----------------------------------------------------------------------------
# Поиск повторителей
# ----------------------------------------------------------------------------
def _signature_groups(texts: list[Element], slide_w: int) -> list[list[Element]]:
    groups: list[list[Element]] = []
    for t in texts:
        placed = False
        for g in groups:
            ref = g[0]
            if abs(ref.font_size - t.font_size) > 0.6:
                continue
            if (ref.style and t.style) and ref.style.bold != t.style.bold:
                continue
            if abs(ref.box.w - t.box.w) > max(0.08 * max(ref.box.w, t.box.w), 0.01 * slide_w):
                continue
            if ref.placeholder and t.placeholder and max(ref.box.h, t.box.h) > 2 * max(min(ref.box.h, t.box.h), 1):
                # рамки плейсхолдеров фиксированы: полоса заголовка и высокое тело — не один и тот же элемент
                continue
            g.append(t)
            placed = True
            break
        if not placed:
            groups.append([t])
    return groups


def _arrangement(members: list[Element], slide_w: int, slide_h: int) -> str | None:
    """Строка / колонка / сетка, если элементы расположены регулярно и не перекрываются."""
    boxes = [m.box for m in members]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            inter = boxes[i].intersection(boxes[j])
            if inter > 0.2 * min(boxes[i].area, boxes[j].area):
                return None
    ytol, xtol = 0.04 * slide_h, 0.03 * slide_w
    rows = _cluster([b.y for b in boxes], ytol)
    cols = _cluster([b.x for b in boxes], xtol)
    n = len(boxes)
    if len(rows) == 1 and len(cols) == n:
        return "row"
    if len(cols) == 1 and len(rows) == n:
        return "column"
    if len(rows) >= 2 and len(cols) >= 2 and len(rows) * len(cols) >= n >= max(len(rows), len(cols)):
        # настоящая решётка: в каждой строке (кроме, может быть, последней) одинаковое число ячеек
        counts = [len(r) for r in rows]
        if len(set(counts[:-1])) <= 1 and counts[-1] <= counts[0]:
            return "grid"
        return None
    # строки со сдвигом (например, зигзагообразные таймлайны): принимаем, если позиции x различны
    if len(cols) == n and n >= 3:
        return "row"
    return None


def _cluster(values: list[float], tol: float) -> list[list[float]]:
    """Группирует близкие значения (разница соседних не больше tol)."""
    out: list[list[float]] = []
    for v in sorted(values):
        if out and abs(out[-1][-1] - v) <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _find_title(elements: list[Element], slide_h: int) -> Element | None:
    phs = [e for e in elements if e.kind == "text" and e.is_title_ph and not e.brand]
    if phs:
        return sorted(phs, key=lambda e: (-(len(e.text) > 0), e.box.y))[0]
    texts = [e for e in elements if e.kind == "text" and e.text and not e.brand and not is_numberish(e.text) and len(e.text) <= 140]
    if not texts:
        return None

    def dominant(cands: list[Element], ratio: float) -> Element | None:
        """Самый крупный текст, если он заметно (в `ratio` раз) крупнее остальных."""
        if not cands:
            return None
        best = max(cands, key=lambda e: (e.font_size, -e.box.y))
        others = sorted(e.font_size for e in texts if e is not best)
        if others and best.font_size < ratio * others[len(others) // 2]:
            return None
        return best

    # 1) самый крупный текст в верхней полосе (содержательные слайды), 2) явно доминирующий текст где угодно
    # (на обложках / разделителях заголовок часто стоит в середине или нижней половине)
    return dominant([e for e in texts if e.box.y < 0.35 * slide_h], 1.15) or dominant(texts, 1.3)


def detect_repeaters(elements: list[Element], title: Element | None, slide_w: int, slide_h: int) -> list[tuple[Repeater, dict]]:
    texts = [e for e in elements if e.kind == "text" and e is not title and (e.text or e.placeholder in ("BODY", "OBJECT"))
             and not MEDIA_FILLER.search(e.text) and not e.brand]
    groups = _signature_groups(texts, slide_w)
    reps: list[tuple[list[Element], str]] = []
    for g in groups:
        if 2 <= len(g) <= 12:
            arr = _arrangement(g, slide_w, slide_h)
            if arr:
                reps.append((g, arr))
            else:
                # нерегулярная группа: оставляем её наибольшее выровненное подмножество (чистую строку или
                # колонку)
                cols = defaultdict(list)
                rows = defaultdict(list)
                for e in g:
                    cols[round(e.box.x / (0.03 * slide_w))].append(e)
                    rows[round(e.box.y / (0.04 * slide_h))].append(e)
                best = max(list(cols.values()) + list(rows.values()), key=len)
                if len(best) >= 2:
                    arr = _arrangement(best, slide_w, slide_h)
                    if arr:
                        reps.append((best, arr))
    # повторители из иконок/картинок (например, иконки над тезисами)
    if not reps:
        return []
    reps.sort(key=lambda r: (-len(r[0]), -r[0][0].font_size, r[0][0].box.y))
    primary, arr = reps[0]
    anchors = sorted(primary, key=lambda e: (round(e.box.y / (0.05 * slide_h)), e.box.x))
    n = len(anchors)
    region = anchors[0].box
    for a in anchors[1:]:
        region = region.union(a.box)
    # расстояние между опорами -> радиус привязки
    xs = sorted({a.box.cx for a in anchors})
    ys = sorted({a.box.cy for a in anchors})
    dx = min((b - a for a, b in zip(xs, xs[1:]) if b - a > 0.02 * slide_w), default=slide_w)
    dy = min((b - a for a, b in zip(ys, ys[1:]) if b - a > 0.02 * slide_h), default=slide_h)

    assigned: dict[int, list[Element]] = defaultdict(list)
    anchor_ids = {id(a) for a in anchors}
    for e in elements:
        if e is title or id(e) in anchor_ids or e.kind == "group" or e.brand:
            continue
        if e.box.area > 0.5 * slide_w * slide_h:  # фоны во весь слайд
            continue
        # фоны карточек: декор, содержащий ровно одну опору
        containing = [i for i, a in enumerate(anchors) if e.box.contains(a.box, tol=int(0.005 * slide_w))]
        if len(containing) == 1 and e.kind in ("decor", "picture"):
            assigned[containing[0]].append(e)
            continue
        if len(containing) > 1:
            continue  # общий контейнер (панель за всеми карточками)
        # ближайшая опора в пределах половины шага; по оси без шага (одна колонка или
        # строка элементов) решает зазор между рамками, а не размер слайда
        best, best_d = None, None
        for i, a in enumerate(anchors):
            ddx = abs(e.box.cx - a.box.cx)
            ddy = abs(e.box.cy - a.box.cy)
            gx = max(0, e.box.x - a.box.r, a.box.x - e.box.r)
            gy = max(0, e.box.y - a.box.b, a.box.y - e.box.b)
            near_x = gx <= 0.08 * slide_w if dx >= slide_w else ddx <= max(dx * 0.55, a.box.w * 0.6)
            near_y = gy <= 0.28 * slide_h if dy >= slide_h else ddy <= max(dy * 0.6, 0.28 * slide_h if arr == "row" else dy * 0.6)
            if near_x and near_y:
                d = ddx + ddy
                if best_d is None or d < best_d:
                    best, best_d = i, d
        if best is not None and e.box.w < 1.5 * max(dx, anchors[best].box.w * 1.2):
            assigned[best].append(e)
    rep = Repeater(
        id="r0",
        n_items=n,
        direction=arr,  # type: ignore[arg-type]
        item_boxes=[],
        item_shape_ids=[],
    )
    item_texts: list[list[Element]] = []
    for i, a in enumerate(anchors):
        members = [a] + assigned.get(i, [])
        bb = members[0].box
        for m in members[1:]:
            bb = bb.union(m.box)
        rep.item_boxes.append(bb)
        rep.item_shape_ids.append([m.shape_id for m in members])
        item_texts.append([m for m in members if m.kind == "text"])
    return [(rep, {"anchors": anchors, "item_texts": item_texts, "assigned": assigned})]


# ----------------------------------------------------------------------------
# Построение слотов
# ----------------------------------------------------------------------------
def _style(e: Element) -> TextStyle:
    """Стиль текста слота из эффективного стиля элемента."""
    if not e.style:
        return TextStyle()
    return TextStyle(font=e.style.font, size=e.style.size, bold=e.style.bold, color_hex=e.style.color,
                     caps=e.style.caps, tracking=e.style.tracking)


def _avail_height(e: Element, elements: list[Element], slide_h: int, bottom_margin: int) -> int:
    """Для автоматически растущих рамок: место по вертикали до следующего элемента ниже."""
    if not e.autofit:
        return e.box.h
    limit = slide_h - bottom_margin
    for o in elements:
        if o is e or o.kind == "group" or o.box.y <= e.box.y + e.box.h * 0.5:
            continue
        if o.box.contains(e.box):
            limit = min(limit, o.box.b)
            continue
        overlap_x = min(o.box.r, e.box.r) - max(o.box.x, e.box.x)
        if overlap_x > 0.3 * e.box.w:
            limit = min(limit, o.box.y)
    return max(e.box.h, limit - e.box.y)


def _style_differs(a, b) -> bool:
    """Стили абзацев заметно различаются (кегль, насыщенность или цвет)."""
    return abs((a.size or 0) - (b.size or 0)) >= 1.0 or a.bold != b.bold or (a.color or "") != (b.color or "")


def composite_roles(e: Element, in_item: bool) -> list[SlotRole]:
    """Роли по абзацам для текстовых рамок с несколькими стилями, [] для однородных."""
    if len(e.paragraphs) < 2 or len(e.para_styles) < 2 or not _style_differs(e.para_styles[0], e.para_styles[1]):
        return []
    first = e.paragraphs[0]
    head = SlotRole.number if is_numberish(first) else (SlotRole.item_title if in_item else SlotRole.subtitle)
    rest = SlotRole.item_text if in_item else SlotRole.body
    return [head] + [rest] * (len(e.paragraphs) - 1)


def _text_role_for_item(t: Element, siblings: list[Element]) -> SlotRole:
    comp = composite_roles(t, True)
    if comp:
        return comp[0]
    if is_numberish(t.text) and len(t.text) <= 8:
        return SlotRole.number
    sizes = sorted({s.font_size for s in siblings}, reverse=True)
    if len(siblings) == 1:
        return SlotRole.item_text if len(t.text) > 25 or t.font_size < 13 else SlotRole.item_title
    if t.font_size == sizes[0] or (t.style and t.style.bold and t.font_size >= sizes[-1]):
        # самый крупный (или жирный) и короткий -> заголовок элемента
        if len(t.text) <= 40:
            return SlotRole.item_title
    if re.search(r"текст|описани|пояснени|text|description|lorem", t.text, re.I):
        return SlotRole.item_text
    if len(t.text) <= 14 and t.font_size <= sizes[-1]:
        return SlotRole.label
    return SlotRole.item_text


COVER_KINDS = {PatternKind.title, PatternKind.section, PatternKind.thanks, PatternKind.contacts, PatternKind.quote}


def _cover_cleanup(slots: list[Slot], repeaters: list[Repeater], title_slot: Slot | None, slide_h: int) -> list[Repeater]:
    """У обложек/разделителей нет элементов: слоты повторителей получают свободные роли."""
    for s in slots:
        if s.item_index is None:
            continue
        s.item_index = None
        if s.kind != "text":
            continue
        below_title = title_slot is not None and 0 <= s.box.y - title_slot.box.b < 0.15 * slide_h
        if re.search(r"имя|name|фамили", s.sample_text, re.I):
            s.role = SlotRole.person
        elif below_title and s.role in (SlotRole.item_title, SlotRole.item_text):
            s.role = SlotRole.subtitle
        else:
            s.role = SlotRole.caption
    return []


def _containers(slots: list[Slot], elements: list[Element], slide_w: int, slide_h: int) -> dict[str, list[int]]:
    """Декоративные фигуры, которые существуют только как обрамление слота (карточка за текстом, рамка фото
    рядом с именем спикера). Удаляются вместе с неиспользованным слотом.
    """
    out: dict[str, list[int]] = {}
    area = slide_w * slide_h
    decor = [e for e in elements if e.kind == "decor" and e.box.area < 0.25 * area]
    for s in slots:
        if s.role == SlotRole.title:
            continue
        ids = []
        for d in decor:
            if d.box.contains(s.box, tol=int(0.004 * slide_w)):
                ids.append(d.shape_id)
            elif s.role in (SlotRole.person, SlotRole.caption, SlotRole.label, SlotRole.decor):
                # рамка фото / круг аватара рядом с текстом
                v_overlap = min(d.box.b, s.box.b) - max(d.box.y, s.box.y)
                gap = s.box.x - d.box.r
                if v_overlap > 0.3 * min(d.box.h, s.box.h) and 0 <= gap < 0.6 * max(d.box.w, s.box.h) and d.box.w < 0.12 * slide_w:
                    ids.append(d.shape_id)
        if ids or s.role == SlotRole.decor:
            out[s.id] = ids
    return out


def _stacked_frames(slots: list[Slot]) -> None:
    """Рамка размером с карточку, содержащая следующую текстовую рамку того же элемента (рамка заголовка на
    всю карточку, рамка тела внутри неё), владеет только местом над этой рамкой: её рамка и вместимость
    обрезаются там, поэтому длинный заголовок не заходит на тело.
    """
    groups: dict[int, list[Slot]] = {}
    for s in slots:
        if s.kind == "text" and s.item_index is not None and not s.para_roles:
            groups.setdefault(s.item_index, []).append(s)
    for group in groups.values():
        for a in group:
            below = [b for b in group if b is not a and a.box.y < b.box.y < a.box.b
                     and min(a.box.r, b.box.r) - max(a.box.x, b.box.x) > 0.5 * min(a.box.w, b.box.w)]
            if not below:
                continue
            h = min(b.box.y for b in below) - a.box.y
            if h > 0:
                a.box = Box(x=a.box.x, y=a.box.y, w=a.box.w, h=h)
                a.max_chars, a.max_lines = estimate_capacity(a.box, a.style.size or 12.0)
                a.autofit = False


def _backdrops(slots: list[Slot], elements: list[Element], slide_w: int, slide_h: int) -> dict[str, int]:
    """Залитая плашка за заголовком или подзаголовком, подогнанная под слова примера (подложка, на которой
    начинается текст, уже текстовой рамки). Сборщик подгоняет её под новый текст, иначе более длинный
    заголовок выходит за плашку.
    """
    out: dict[str, int] = {}
    tol = int(0.02 * slide_w)
    for s in slots:
        if s.kind != "text" or s.item_index is not None or s.role not in (SlotRole.title, SlotRole.subtitle):
            continue
        best = None
        for d in elements:
            if d.kind != "decor" or not d.fill_hex or d.text.strip():
                continue
            b = d.box
            if b.w >= 0.9 * s.box.w or not 0.6 * s.box.h <= b.h <= 3 * s.box.h:
                continue  # полосы и карточки во всю ширину — не плашки
            if min(b.b, s.box.b) - max(b.y, s.box.y) < 0.8 * min(b.h, s.box.h):
                continue  # должна накрывать полосу текста
            if not (s.box.x - 3 * tol <= b.x <= s.box.x + tol and b.r > s.box.x + 2 * tol):
                continue  # должна начинаться там, где начинается текст
            if best is None or b.area < best.box.area:
                best = d
        if best is not None:
            out[s.id] = best.shape_id
    return out


def build_pattern(
    pid: str,
    elements: list[Element],
    *,
    slide_index: int | None,
    layout_index: int | None,
    layout_name: str,
    slide_w: int,
    slide_h: int,
    bottom_margin: int,
    dark: bool,
    source: str = "slide",
) -> Pattern:
    title = _find_title(elements, slide_h)
    reps = detect_repeaters(elements, title, slide_w, slide_h)
    slots: list[Slot] = []
    in_items: dict[int, tuple[int, SlotRole]] = {}
    repeaters: list[Repeater] = []
    if reps:
        rep, info = reps[0]
        roles_per_item: list[list[SlotRole]] = []
        for i, texts in enumerate(info["item_texts"]):
            roles = []
            for t in sorted(texts, key=lambda e: (e.box.y, e.box.x)):
                role = _text_role_for_item(t, texts)
                in_items[t.shape_id] = (i, role)
                roles.append(role)
            roles_per_item.append(roles)
        # слоты картинок внутри элементов (иконки / фото)
        for i, members in info["assigned"].items():
            for m in members:
                if m.kind == "picture":
                    in_items[m.shape_id] = (i, SlotRole.icon if m.is_icon else SlotRole.image)
        rep.slot_roles = roles_per_item[0] if roles_per_item else []
        repeaters.append(rep)

    title_bottom = title.box.b if title else int(0.18 * slide_h)
    for e in elements:
        if e.kind in ("group", "line") or e.brand:
            continue  # брендовые тексты (колонтитулы, номера страниц, повторяющиеся пометки) не трогаются
        sid = f"{pid}.s{e.shape_id}"
        if title is not None and e is title:
            mc, ml = estimate_capacity(e.box, e.font_size, _avail_height(e, elements, slide_h, bottom_margin))
            slots.append(
                Slot(id=sid, role=SlotRole.title, box=e.box, shape_id=e.shape_id, shape_path=e.path, placeholder_idx=e.placeholder_idx,
                     sample_text=e.text, style=_style(e), max_chars=max(mc, 30), max_lines=ml)
            )
            continue
        if e.kind == "text":
            if e.is_mono:
                continue
            item = in_items.get(e.shape_id)
            if MEDIA_FILLER.search(e.text or ""):
                idx, role = None, SlotRole.decor
            elif item is not None:
                idx, role = item
            else:
                idx = None
                big_content_ph = e.placeholder in ("BODY", "OBJECT") and e.box.area > 0.12 * slide_w * slide_h
                if big_content_ph:
                    # крупный плейсхолдер контента — тело, даже если стоит сразу под заголовком
                    role = SlotRole.body
                elif e.placeholder in ("SUBTITLE",) or (
                    title is not None and e.box.y >= title.box.y and e.box.y - title_bottom < 0.08 * slide_h
                    and e.font_size < title.font_size and len(e.paragraphs) <= 2 and e.box.x <= title.box.x + 0.05 * slide_w
                ):
                    role = SlotRole.subtitle
                elif re.search(r"имя\s*фамилия|name\s*surname|спикер", e.text, re.I):
                    role = SlotRole.person
                elif is_numberish(e.text):
                    role = SlotRole.number
                elif len(e.paragraphs) >= 2 or e.placeholder in ("BODY", "OBJECT") or e.box.area > 0.06 * slide_w * slide_h:
                    role = SlotRole.body
                elif e.font_size and title and e.font_size >= 0.9 * title.font_size:
                    role = SlotRole.subtitle
                else:
                    role = SlotRole.label if len(e.text) <= 30 else SlotRole.caption
            mc, ml = estimate_capacity(e.box, e.font_size, _avail_height(e, elements, slide_h, bottom_margin))
            para_roles = composite_roles(e, idx is not None)
            slots.append(
                Slot(id=sid, role=role, box=e.box, shape_id=e.shape_id, shape_path=e.path, placeholder_idx=e.placeholder_idx,
                     sample_text=e.text[:200], style=_style(e), max_chars=mc, max_lines=ml, item_index=idx,
                     paragraphs=max(len(e.paragraphs), 1), para_roles=para_roles, autofit=e.autofit,
                     para_styles=[TextStyle(font=ps.font, size=ps.size, bold=ps.bold, color_hex=ps.color) for ps in e.para_styles]
                     if para_roles else [])
            )
        elif e.kind == "picture":
            item = in_items.get(e.shape_id)
            big = e.box.area > 0.08 * slide_w * slide_h
            if e.placeholder == "PICTURE" or big or item is not None:
                role = item[1] if item else (SlotRole.image if (big or e.placeholder == "PICTURE") else SlotRole.icon)
                slots.append(Slot(id=sid, role=role, kind="picture", box=e.box, shape_id=e.shape_id, shape_path=e.path,
                                  placeholder_idx=e.placeholder_idx, item_index=item[0] if item else None))
        elif e.kind == "table":
            slots.append(Slot(id=sid, role=SlotRole.table, kind="table", box=e.box, shape_id=e.shape_id, shape_path=e.path))
        elif e.kind == "chart":
            slots.append(Slot(id=sid, role=SlotRole.chart, kind="chart", box=e.box, shape_id=e.shape_id, shape_path=e.path))

    kind, reason, tags, score = classify(elements, slots, repeaters, title, layout_name, slide_index, slide_w, slide_h)
    if repeaters:
        # у элементов должна быть одна композиция слотов; иначе пример — разовый коллаж
        per_item: dict[int, list[str]] = {}
        for s in slots:
            if s.item_index is not None and s.kind == "text":
                per_item.setdefault(s.item_index, []).append(s.role.value)
        sigs = {tuple(sorted(v)) for v in per_item.values()}
        dup_roles = any(len(v) != len(set(v)) and v.count("item_title") > 1 for v in per_item.values())
        if len(sigs) > 1 or dup_roles:
            score *= 0.45
            tags = tags + ["irregular_items"]
            reason += "; irregular items"
    tags = tags + [f"chart:{e.chart_type}" for e in elements if e.kind == "chart" and e.chart_type]
    graphics = {e.graphic for e in elements if e.graphic}
    if graphics & {"smartart", "ole"}:
        # содержимое SmartArt / OLE нельзя перезаполнить -> в колоду утёк бы текст шаблона
        score *= 0.25
        tags = tags + sorted(graphics)
        reason += f"; contains {', '.join(sorted(graphics))}"
    if kind in COVER_KINDS and repeaters:
        title_slot = next((s for s in slots if s.role == SlotRole.title), None)
        repeaters = _cover_cleanup(slots, repeaters, title_slot, slide_h)
    _stacked_frames(slots)
    containers = _containers(slots, elements, slide_w, slide_h)
    backdrops = _backdrops(slots, elements, slide_w, slide_h) if source == "slide" else {}
    text_cap = sum(s.max_chars for s in slots if s.kind == "text" and s.role != SlotRole.title)
    covered = sum(min(e.box.area, slide_w * slide_h) for e in elements if e.kind in ("text", "picture", "table", "chart"))
    return Pattern(
        id=pid,
        source=source,  # type: ignore[arg-type]
        slide_index=slide_index,
        layout_index=layout_index,
        layout_name=layout_name,
        kind=kind,
        n_items=repeaters[0].n_items if repeaters else 0,
        slots=slots,
        repeaters=repeaters,
        containers=containers,
        backdrops=backdrops,
        dark=dark,
        text_capacity=text_cap,
        has_picture_slot=any(s.role == SlotRole.image for s in slots),
        fill_ratio=round(min(covered / (slide_w * slide_h), 1.0), 3),
        score_hint=score,
        tags=tags,
        reason=reason,
    )


# ----------------------------------------------------------------------------
# Классификация
# ----------------------------------------------------------------------------
def classify(elements, slots, repeaters, title, layout_name, slide_index, slide_w, slide_h):
    texts = [e for e in elements if e.kind == "text" and e.text]
    pics = [e for e in elements if e.kind == "picture"]
    all_text = " ".join(e.text for e in texts)
    lname = (layout_name or "").lower()
    title_text = title.text if title else ""
    tags: list[str] = []
    area = slide_w * slide_h

    # --- документация шаблона / библиотеки ресурсов -------------------------------
    small_pics = [p for p in pics if p.is_icon]
    if len(pics) >= 30 and len(small_pics) >= 0.7 * len(pics):
        return PatternKind.guide, f"icon library: {len(small_pics)} small pictures", ["icons"], 0.0
    if len(pics) >= 10 and len(texts) < 0.5 * len(pics) and all(p.box.area < 0.03 * area for p in pics):
        # стена логотипов / лист ресурсов: его картинки — образцы, которые не должны попасть в колоду
        return PatternKind.guide, f"asset sheet: {len(pics)} small pictures, {len(texts)} texts", ["assets"], 0.0
    mono = [e for e in texts if e.is_mono and re.search(r"[{};:]", e.text)]
    if mono:
        return PatternKind.guide, "code sample block (monospace)", ["code"], 0.0
    swatches = [e for e in elements if e.kind == "decor" and e.fill_hex and e.box.w < 0.12 * slide_w and abs(e.box.w - e.box.h) < 0.02 * slide_w]
    if len({s.fill_hex for s in swatches}) >= 6 and re.search(r"цвет|color|палитр|шрифт|font", all_text, re.I):
        return PatternKind.guide, "palette/typography specimen", ["palette"], 0.0
    codes = set(re.findall(r"(?:#|\bHEX\s*#?|\bRGB\s*)([0-9A-Fa-f]{6})\b", all_text))
    if len(codes) >= 3:
        # руководство по стилю: слайд документирует фирменные цвета (образцы любой формы + их коды)
        return PatternKind.guide, f"style guide: {len(codes)} colour codes", ["palette"], 0.0
    # инструкции, обращённые к пользователю шаблона («используй слайд 7», «replace this text»)
    instr = set(m.lower() for m in INSTRUCTION_RE.findall(all_text))
    if len(instr) >= 2 and len(all_text) > 80:
        return PatternKind.guide, f"instructions to the template user ({', '.join(sorted(instr)[:3])})", ["instructions"], 0.0

    n_items = repeaters[0].n_items if repeaters else 0
    has_table = any(s.role == SlotRole.table for s in slots)
    has_chart = any(s.role == SlotRole.chart for s in slots)
    big_pic = [s for s in slots if s.role == SlotRole.image and s.box.area > 0.12 * area]
    title_size = title.font_size if title else 0
    body_slots = [s for s in slots if s.role == SlotRole.body]
    n_text = len([s for s in slots if s.kind == "text"])
    key = f"{lname} {title_text} {all_text}".lower()

    if has_table:
        return PatternKind.table, "native table present", tags, 1.0
    if has_chart:
        return PatternKind.chart, "native chart present", tags, 1.0
    person_slots = [s for s in slots if s.role == SlotRole.person]
    if len(person_slots) >= 3 or (n_items >= 3 and _kw("team", key) and re.search(r"имя|name", all_text, re.I)):
        return PatternKind.team, "several person name slots", tags, 0.6
    if _kw("team", lname) and n_items < 2:
        return PatternKind.team, "person card layout (визитка/speaker)", tags, 0.5
    # таблицы / диаграммы, нарисованные простыми фигурами: переиспользование хрупкое -> низкий приоритет,
    # вместо этого компоновщик строит нативные таблицы/диаграммы в стиле шаблона
    grid_texts = [e for e in texts if e is not title and len(e.text) <= 40]
    card_grid = bool(repeaters) and repeaters[0].direction in ("grid", "row") and n_items < 9
    if len(grid_texts) >= 12 and not card_grid:
        rows = _cluster([e.box.cy for e in grid_texts], 0.02 * slide_h)
        cols = _cluster([e.box.x for e in grid_texts], 0.02 * slide_w)
        dense_rows = [sum(r) / len(r) for r in rows if len(r) >= 3]
        dense_cols = [c for c in cols if len(c) >= 3]
        gaps = [b - a for a, b in zip(dense_rows, dense_rows[1:])]
        regular = False
        if len(gaps) >= 3:
            mean = sum(gaps) / len(gaps)
            cv = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean if mean else 1
            regular = cv < 0.35
        if len(dense_rows) >= 4 and len(dense_cols) >= 3 and regular:
            return PatternKind.table, f"shape-drawn table {len(dense_rows)}x{len(dense_cols)}", ["shape_table"], 0.15
    num_labels = [e for e in texts if is_numberish(e.text) and e.font_size < 20]
    if len(num_labels) >= 6 and (_kw("chart", key) or len(num_labels) >= 0.4 * len(texts)):
        return PatternKind.chart, f"shape-drawn chart ({len(num_labels)} value labels)", ["shape_chart"], 0.15
    if _kw("thanks", title_text) or (_kw("thanks", lname) and n_items == 0):
        return PatternKind.thanks, "thanks/Q&A keywords", tags, 1.0
    if _kw("agenda", f"{lname} {title_text}"):
        return PatternKind.agenda, "agenda keywords", tags, 1.0
    if _kw("quote", lname) or (texts and texts[0].text.startswith(("«", '"', "“")) and len(texts[0].text) > 60):
        return PatternKind.quote, "quote layout/marks", tags, 0.9
    if _kw("contacts", f"{lname} {title_text}") and n_items == 0:
        return PatternKind.contacts, "call-to-action / QR keywords", tags, 0.8

    few = n_text <= 4 and n_items == 0 and not has_table
    opening = slide_index is not None and slide_index <= 2
    if title is not None and title.placeholder == "CENTER_TITLE" and n_items == 0 and n_text <= 4 and not big_pic:
        return PatternKind.title, "centred-title placeholder (cover layout)", tags, 1.0
    if title is not None and n_text <= 5 and not has_table and (opening or slide_index is None) and _kw("title", f"{lname} {title_text}"):
        return PatternKind.title, "title-slide layout name / cover keywords", tags, 1.0
    if few and (slide_index == 0 or _kw("title", f"{lname} {title_text}")) and title is not None:
        if opening or person_slots or "назван" in title_text.lower():
            return PatternKind.title, "opening slide with large title and few elements", tags, 0.85
    if few and (_kw("section", key) or (title is not None and title_size >= 30 and not body_slots and not big_pic)):
        return PatternKind.section, "large title with no content blocks", tags, 1.0
    if few and title is not None and slide_index is not None and slide_index <= 1:
        return PatternKind.title, "first slide", tags, 0.8

    if n_items >= 2:
        rep = repeaters[0]
        roles = rep.slot_roles
        lines = [e for e in elements if e.kind == "line"]
        numbers_seq = SlotRole.number in roles
        if _kw("timeline", key) or (numbers_seq and lines) or (lines and rep.direction == "row" and n_items >= 3):
            return PatternKind.steps, f"{n_items} items with sequence markers/connectors", ["timeline"], 1.0
        if roles and roles[0] == SlotRole.number and SlotRole.item_title not in roles:
            big = max((s.style.size or 0) for s in slots if s.role == SlotRole.number) if any(s.role == SlotRole.number for s in slots) else 0
            if big >= 24:
                return PatternKind.stats, f"{n_items} big-number tiles", tags, 1.0
        if numbers_seq:
            return PatternKind.steps, f"{n_items} numbered items", tags, 1.0
        if _kw("agenda", key):
            return PatternKind.agenda, "repeated list items with agenda keywords", tags, 1.0
        return PatternKind.cards, f"{n_items} repeated items ({rep.direction})", tags, 1.0
    numbers = [s for s in slots if s.role == SlotRole.number and (s.style.size or 0) >= 28]
    if numbers:
        return PatternKind.stats, "large numeric callouts", tags, 0.9
    if big_pic:
        tags.append("chart_like" if _kw("chart", key) else "photo")
        return PatternKind.image_text, "large picture slot with text", tags, 0.7 if _kw("chart", key) else 0.9
    if len(body_slots) >= 2:
        return PatternKind.two_column, "two body text blocks", tags, 1.0
    if body_slots or n_text >= 2:
        return PatternKind.text, "title + body text", tags, 1.0
    if title is not None and n_text <= 1:
        return PatternKind.section, "title only", tags, 0.6
    return PatternKind.free, "no dominant structure", tags, 0.3
