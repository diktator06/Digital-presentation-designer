"""Разбор шаблонов: три шаблона датасета + шаблон, который система никогда не видела."""
import pytest

from decksmith.core.models import PatternKind
from decksmith.parsing.ooxml import contrast_ratio
from decksmith.parsing.tokens import saturation

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("name", ["vk_tech", "vk_workspace", "vk_education"])
def test_dataset_templates_decomposed(profiles, name):
    p = profiles[name]
    kinds = {x.kind for x in p.usable_patterns()}
    # композиционные паттерны, нужные любой бизнес-колоде
    assert PatternKind.title in kinds
    assert PatternKind.cards in kinds
    assert kinds & {PatternKind.steps, PatternKind.agenda}
    assert kinds & {PatternKind.thanks, PatternKind.contacts}
    t = p.tokens
    assert saturation(t.accent_hex) > 0.35, "accent must be a brand colour, not grey"
    assert contrast_ratio(t.text_hex, t.background_hex) >= 4.5
    assert t.type_scale.title > t.type_scale.body
    assert p.canvas_layouts, "a clean layout for natively composed slides"
    assert any(f.available for f in t.fonts), "template font resolved for rendering/metrics"


def test_repeaters_have_regular_items(profiles):
    """Паттерны карточек дают N одинаковых элементов с одинаковой композицией слотов."""
    p = profiles["vk_education"]
    cards = [x for x in p.usable_patterns() if x.kind == PatternKind.cards and x.n_items == 4 and "irregular_items" not in x.tags]
    assert cards
    rep = cards[0].repeaters[0]
    assert len(rep.item_boxes) == 4 and len(rep.item_shape_ids) == 4


def test_guide_slides_are_not_reused(profiles):
    """Библиотеки иконок / образцы кода — документация шаблона, а не макеты."""
    for p in profiles.values():
        for g in [x for x in p.patterns if x.kind == PatternKind.guide]:
            assert g not in p.usable_patterns()


def test_dark_and_light_detected(profiles):
    """Тёмный и светлый шаблоны определяются правильно."""
    assert profiles["vk_workspace"].tokens.dark_background is True
    assert profiles["vk_education"].tokens.dark_background is False


def test_unknown_template(unknown_template):
    """На финальной защите используется шаблон, которого никто не видел: разбор не должен опираться на
    имена/фикстуры.
    """
    from decksmith.parsing.template_parser import analyze_template

    p = analyze_template(unknown_template)
    kinds = {x.kind for x in p.usable_patterns()}
    assert PatternKind.title in kinds
    assert PatternKind.cards in kinds  # 3 нарисованные вручную карточки находятся как повторитель
    card = next(x for x in p.usable_patterns() if x.kind == PatternKind.cards)
    assert card.n_items == 3
    assert p.tokens.body_font
