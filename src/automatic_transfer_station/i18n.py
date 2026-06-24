from __future__ import annotations

from typing import Callable


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
