"""Экспорт дизайн-системы шаблона: токены W3C (JSON), CSS-переменные, палитра SVG, README."""
import io
import json
import re
import zipfile
from xml.etree import ElementTree

from decksmith.export.design_system import EMU_PER_PX, design_system_zip, design_tokens, tokens_css
from decksmith.parsing.template_parser import analyze_template

HEX = re.compile(r"^#[0-9a-f]{6}$")


def _tokens(node, path=""):
    """Все токены дерева DTCG: (путь, токен) для узлов с $value."""
    if isinstance(node, dict):
        if "$value" in node:
            yield path, node
        for k, v in node.items():
            if not k.startswith("$"):
                yield from _tokens(v, f"{path}.{k}" if path else k)


def test_design_tokens_match_the_template_profile(unknown_template):
    """Токены совпадают с профилем шаблона: цвета — корректный hex, размеры — пиксели при 96 dpi."""
    prof = analyze_template(unknown_template)
    t = prof.tokens
    tok = design_tokens(prof)

    assert tok["color"]["accent"]["$value"] == f"#{t.accent_hex.lower()}"
    assert tok["color"]["background"]["$value"] == f"#{t.background_hex.lower()}"
    assert len(tok["color"]["palette"]) == len(t.palette)
    assert tok["font"]["family"]["heading"]["$value"][0] == t.heading_font
    assert tok["font"]["size"]["title"]["$extensions"]["com.decksmith"]["pt"] == t.type_scale.title
    assert tok["layout"]["slide"]["width"]["$value"] == f"{t.slide_w / EMU_PER_PX:g}px"

    # каждый токен типизирован, цвета и размеры в допустимом виде; имена групп без символов DTCG
    for path, node in _tokens(tok):
        assert node["$type"] in ("color", "fontFamily", "dimension", "number"), path
        assert not re.search(r"[{}.]", path.split(".")[-1]), path
        if node["$type"] == "color":
            assert HEX.match(node["$value"]), (path, node["$value"])
        if node["$type"] == "dimension":
            assert re.match(r"^-?\d+(\.\d+)?px$", node["$value"]), (path, node["$value"])

    css = tokens_css(prof)
    assert css.count("{") == css.count("}") == 1 and ":root {" in css
    assert f"--ds-color-accent: #{t.accent_hex.lower()};" in css
    assert f"--ds-font-heading: \"{t.heading_font}\", sans-serif;" in css


def test_design_system_archive_is_complete(unknown_template):
    """Архив содержит четыре файла, JSON читается, SVG — корректный XML, имя файла без спецсимволов."""
    prof = analyze_template(unknown_template)
    name, data = design_system_zip(prof)
    assert name.endswith("_design-system.zip") and "/" not in name
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == ["README.md", "palette.svg", "tokens.css", "tokens.json"]
    assert json.loads(z.read("tokens.json"))["color"]["palette"]
    ElementTree.fromstring(z.read("palette.svg"))
    assert prof.name in z.read("README.md").decode("utf-8")
