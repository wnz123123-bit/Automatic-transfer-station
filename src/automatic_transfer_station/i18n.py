from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .paths import UI_SETTINGS_PATH
from .json_store import atomic_write_json


TRANSLATIONS: dict[str, str] = {}


class Translator:
    def __init__(self, lang: str = "en", table: dict[str, str] | None = None):
        self._lang = lang if lang in ("en", "zh") else "en"
        self._table = TRANSLATIONS if table is None else table
        self._observers: list[Callable[[], None]] = []
        self._saver: Callable[[str], None] | None = None

    @property
    def lang(self) -> str:
        return self._lang

    def tr(self, zh: str) -> str:
        if self._lang == "zh":
            return zh
        return self._table.get(zh, zh)

    def initialize(self, lang: str) -> None:
        if lang in ("en", "zh"):
            self._lang = lang

    def set_language(self, lang: str) -> None:
        if lang not in ("en", "zh") or lang == self._lang:
            return
        self._lang = lang
        if self._saver is not None:
            self._saver(lang)
        for cb in list(self._observers):
            cb()

    def add_observer(self, cb: Callable[[], None]) -> None:
        self._observers.append(cb)

    def clear_observers(self) -> None:
        self._observers.clear()

    def set_saver(self, saver: Callable[[str], None] | None) -> None:
        self._saver = saver


translator = Translator()


def tr(zh: str) -> str:
    return translator.tr(zh)


def load_language(path: Path | None = None, default: str = "en") -> str:
    path = UI_SETTINGS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        lang = data.get("language")
    except (OSError, ValueError, AttributeError):
        return default
    return lang if lang in ("en", "zh") else default


def save_language(lang: str, path: Path | None = None) -> None:
    if lang not in ("en", "zh"):
        return
    atomic_write_json(UI_SETTINGS_PATH if path is None else path, {"language": lang})


class Retranslator:
    def __init__(self, translator: "Translator"):
        self._translator = translator
        self._apply: list[Callable[[], None]] = []
        translator.add_observer(self.retranslate)

    def _add(self, fn: Callable[[], None]) -> None:
        fn()
        self._apply.append(fn)

    def text(self, widget, zh: str) -> None:
        self._add(lambda: widget.setText(self._translator.tr(zh)))

    def title(self, widget, zh: str) -> None:
        setter = getattr(widget, "setTitle", None) or widget.setWindowTitle
        self._add(lambda: setter(self._translator.tr(zh)))

    def tooltip(self, widget, zh: str) -> None:
        self._add(lambda: widget.setToolTip(self._translator.tr(zh)))

    def placeholder(self, widget, zh: str) -> None:
        self._add(lambda: widget.setPlaceholderText(self._translator.tr(zh)))

    def items(self, combo, zh_list: list[str]) -> None:
        def apply():
            for i, zh in enumerate(zh_list):
                combo.setItemText(i, self._translator.tr(zh))
        self._add(apply)

    def custom(self, apply_fn: Callable[[], None]) -> None:
        self._add(apply_fn)

    def retranslate(self) -> None:
        for fn in self._apply:
            fn()
