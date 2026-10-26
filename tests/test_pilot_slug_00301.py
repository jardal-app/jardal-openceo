"""Piloto sintetico 00301c: slugify puro + tests (tier C, sin secretos)."""

import re


def pilot_slug(text: str) -> str:
    """Minusculas, no-alfanumerico a '-', colapsar repetidos, strip '-'.

    Sintetico solo-test: conserva tildes a proposito; no usar para
    URLs/filenames/identificadores (contrato ASCII-only en
    agents/dev/tools.py:slugify y research/export.py:31).
    """
    mapped = "".join(c if c.isalnum() else "-" for c in text.lower())
    return re.sub(r"-+", "-", mapped).strip("-")


def test_pilot_slug_basic():
    assert pilot_slug("Hello World!") == "hello-world"
    assert pilot_slug("Foo---Bar__Baz") == "foo-bar-baz"


def test_pilot_slug_empty():
    assert pilot_slug("") == ""
    assert pilot_slug("---") == ""


def test_pilot_slug_unicode():
    assert pilot_slug("Canción Árbol") == "canción-árbol"
