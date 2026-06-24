# Bilingual (EN/ZH) UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an instant English/Chinese language switch covering all GUI text, defaulting to English and remembering the choice.

**Architecture:** A small `i18n` module holds a `Translator` (current language + observers) and a `tr(zh)` lookup keyed by the existing Chinese strings (English values in a `TRANSLATIONS` table; missing key → Chinese passthrough). A `Retranslator` records apply-closures for static widget text so a language change re-applies them live. Dynamic status/error strings call `tr()` at the call site. A status-bar flip button drives `translator.set_language`, persisted to `runtime/ui_settings.json`.

**Tech Stack:** Python 3.13, PySide6, existing `json_store.atomic_write_json`.

## Global Constraints

- Run/test with `py -3.13` (default `python` is 3.14, no PySide6).
- App default language: `"en"`. Test suite pinned to `"zh"` via `tests/conftest.py` autouse fixture so existing Chinese assertions hold unchanged.
- `tr(zh)` returns `zh` unchanged when missing from the table or when language is `"zh"` — never blank.
- Do NOT translate identifiers: axis codes (X1, Y2, R, MicroscopeZ), COM names, units (mm, °C, PWM, pulse), version. Only role/label text.
- Keys in `TRANSLATIONS` are the exact existing Chinese source strings (including `{name}` placeholders for templates).
- Dialogs built on demand call `tr()` once at build time; they do NOT register retranslate closures (avoids stale references to destroyed widgets).
- TDD: failing test → minimal code → green → commit. Commit after each task.

---

### Task 1: i18n core — Translator + tr + TRANSLATIONS

**Files:**
- Create: `src/automatic_transfer_station/i18n.py`
- Test: `tests/test_i18n.py`

**Interfaces:**
- Produces:
  - `class Translator` with: `lang: str` (read via property), `initialize(lang: str) -> None` (set without notify/persist), `set_language(lang: str) -> None` (set + persist-if-saver + notify observers), `tr(zh: str) -> str`, `add_observer(cb: Callable[[], None]) -> None`, `clear_observers() -> None`, `set_saver(saver: Callable[[str], None] | None) -> None`.
  - module singleton `translator = Translator()`; module fn `tr(zh: str) -> str` delegating to `translator.tr`.
  - `TRANSLATIONS: dict[str, str]` (empty for now; seeded in later tasks).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_i18n.py
import unittest


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        from automatic_transfer_station.i18n import Translator
        self.Translator = Translator

    def test_zh_returns_key_unchanged(self):
        t = self.Translator(lang="zh")
        t._table = {"温度控制": "Temperature Control"}
        self.assertEqual("温度控制", t.tr("温度控制"))

    def test_en_returns_translation(self):
        t = self.Translator(lang="en")
        t._table = {"温度控制": "Temperature Control"}
        self.assertEqual("Temperature Control", t.tr("温度控制"))

    def test_missing_key_passthrough(self):
        t = self.Translator(lang="en")
        self.assertEqual("未登记", t.tr("未登记"))

    def test_set_language_notifies_observers(self):
        t = self.Translator(lang="en")
        hits = []
        t.add_observer(lambda: hits.append(t.lang))
        t.set_language("zh")
        self.assertEqual(["zh"], hits)

    def test_initialize_does_not_notify(self):
        t = self.Translator(lang="en")
        hits = []
        t.add_observer(lambda: hits.append(1))
        t.initialize("zh")
        self.assertEqual([], hits)
        self.assertEqual("zh", t.lang)

    def test_saver_called_on_set_language_only(self):
        t = self.Translator(lang="en")
        saved = []
        t.set_saver(saved.append)
        t.initialize("zh")
        self.assertEqual([], saved)
        t.set_language("en")
        self.assertEqual(["en"], saved)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest tests/test_i18n.py -q`
Expected: FAIL (`ModuleNotFoundError: ... i18n`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/automatic_transfer_station/i18n.py
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
            # Still notify on a real change only; ignore no-ops/invalid.
            if lang == self._lang:
                return
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.13 -m pytest tests/test_i18n.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/i18n.py tests/test_i18n.py
git commit -m "feat(i18n): add Translator core and tr() lookup"
```

---

### Task 2: Language persistence (runtime/ui_settings.json)

**Files:**
- Modify: `src/automatic_transfer_station/paths.py` (add `UI_SETTINGS_PATH`)
- Modify: `src/automatic_transfer_station/i18n.py` (add `load_language`, `save_language`)
- Test: `tests/test_i18n.py` (add persistence tests)

**Interfaces:**
- Consumes: `paths.RUNTIME_DIR`, `json_store.atomic_write_json`.
- Produces:
  - `paths.UI_SETTINGS_PATH: Path` = `RUNTIME_DIR / "ui_settings.json"`.
  - `i18n.load_language(path: Path | None = None, default: str = "en") -> str`
  - `i18n.save_language(lang: str, path: Path | None = None) -> None`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_i18n.py
import tempfile, os, unittest
from pathlib import Path


class LanguagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "ui_settings.json"

    def tearDown(self):
        for p in Path(self.dir).glob("*"):
            try: p.unlink()
            except OSError: pass
        os.rmdir(self.dir)

    def test_default_when_missing(self):
        from automatic_transfer_station.i18n import load_language
        self.assertEqual("en", load_language(self.path))

    def test_round_trip(self):
        from automatic_transfer_station.i18n import load_language, save_language
        save_language("zh", self.path)
        self.assertEqual("zh", load_language(self.path))

    def test_corrupt_file_falls_back_to_default(self):
        from automatic_transfer_station.i18n import load_language
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual("en", load_language(self.path))

    def test_invalid_value_falls_back(self):
        from automatic_transfer_station.i18n import load_language, save_language
        self.path.write_text('{"language": "fr"}', encoding="utf-8")
        self.assertEqual("en", load_language(self.path))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest tests/test_i18n.py::LanguagePersistenceTests -q`
Expected: FAIL (`ImportError: cannot import name 'load_language'`).

- [ ] **Step 3: Write minimal implementation**

Add to `paths.py` (after the `RUNTIME_DIR` definition):

```python
UI_SETTINGS_PATH = RUNTIME_DIR / "ui_settings.json"
```

Add to `i18n.py` (top imports + functions):

```python
import json
from pathlib import Path

from .paths import UI_SETTINGS_PATH
from .json_store import atomic_write_json


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.13 -m pytest tests/test_i18n.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/i18n.py src/automatic_transfer_station/paths.py tests/test_i18n.py
git commit -m "feat(i18n): persist language to runtime/ui_settings.json"
```

---

### Task 3: Retranslator + static-text helpers

**Files:**
- Modify: `src/automatic_transfer_station/i18n.py`
- Test: `tests/test_i18n.py`

**Interfaces:**
- Consumes: `Translator`, `tr`.
- Produces: `class Retranslator(translator: Translator)` with methods that each apply now AND record a re-apply closure:
  - `text(widget, zh)` -> `widget.setText(tr(zh))`
  - `title(widget, zh)` -> uses `setTitle` if present else `setWindowTitle`
  - `tooltip(widget, zh)` -> `widget.setToolTip(tr(zh))`
  - `placeholder(widget, zh)` -> `widget.setPlaceholderText(tr(zh))`
  - `items(combo, zh_list)` -> `combo.setItemText(i, tr(zh_list[i]))` for each, preserving current index
  - `custom(apply_fn)` -> calls `apply_fn()` now and on each change
  - `retranslate()` -> re-run all closures (registered as a translator observer in `__init__`)

- [ ] **Step 1: Write the failing test** (uses fakes, no Qt needed)

```python
# add to tests/test_i18n.py
class RetranslatorTests(unittest.TestCase):
    def _trans(self):
        from automatic_transfer_station.i18n import Translator
        t = Translator(lang="en", table={"温度控制": "Temperature Control"})
        return t

    def test_text_applies_now_and_on_switch(self):
        from automatic_transfer_station.i18n import Retranslator
        t = self._trans()
        r = Retranslator(t)

        class W:
            def __init__(self): self.t = None
            def setText(self, v): self.t = v
        w = W()
        r.text(w, "温度控制")
        self.assertEqual("Temperature Control", w.t)  # applied now (EN)
        t.set_language("zh")
        self.assertEqual("温度控制", w.t)              # live re-applied (ZH)

    def test_items_preserve_index(self):
        from automatic_transfer_station.i18n import Retranslator
        t = self._trans()
        r = Retranslator(t)

        class Combo:
            def __init__(self): self.texts = ["a", "b"]; self.idx = 1
            def setItemText(self, i, v): self.texts[i] = v
            def currentIndex(self): return self.idx
            def count(self): return len(self.texts)
        c = Combo()
        r.items(c, ["温度控制", "温度控制"])
        self.assertEqual(["Temperature Control", "Temperature Control"], c.texts)
        self.assertEqual(1, c.currentIndex())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest tests/test_i18n.py::RetranslatorTests -q`
Expected: FAIL (`ImportError: cannot import name 'Retranslator'`).

- [ ] **Step 3: Write minimal implementation** (append to `i18n.py`)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.13 -m pytest tests/test_i18n.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/i18n.py tests/test_i18n.py
git commit -m "feat(i18n): add Retranslator and static-text helpers"
```

---

### Task 4: Pin tests to ZH (conftest) + load language at startup

**Files:**
- Create: `tests/conftest.py`
- Modify: `src/automatic_transfer_station/run_eth_board_test.py` (load+save wiring in `main`)
- Test: full existing suite stays green.

**Interfaces:**
- Consumes: `i18n.translator`, `i18n.load_language`, `i18n.save_language`.

- [ ] **Step 1: Create the conftest fixture**

```python
# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def _pin_language_zh():
    """Existing assertions expect Chinese; pin the translator to ZH (where tr()
    returns the key unchanged) and reset observers so closures from a prior test's
    destroyed widgets never fire."""
    from automatic_transfer_station.i18n import translator
    translator.clear_observers()
    translator.set_saver(None)
    translator.initialize("zh")
    yield
    translator.clear_observers()
```

- [ ] **Step 2: Wire startup language in `run_eth_board_test.main`**

In `main()`, immediately after `configure_logging()`:

```python
    from .i18n import translator, load_language, save_language
    translator.initialize(load_language())
    translator.set_saver(save_language)
```

- [ ] **Step 3: Run the full suite**

Run: `py -3.13 -m pytest -q`
Expected: PASS (existing count, e.g. 515 + new i18n tests).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py src/automatic_transfer_station/run_eth_board_test.py
git commit -m "test(i18n): pin suite to ZH; load language at startup"
```

---

### Task 5: Status-bar language toggle button + window title

**Files:**
- Modify: `src/automatic_transfer_station/main_window.py`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: `i18n.translator`, `i18n.Retranslator`, `i18n.tr`.
- Produces: `MainWindow._retranslator: Retranslator`; status-bar button registered as `"languageToggle"`; helper `MainWindow._toggle_language()`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_main_window.py (inside MainWindowTests)
    def test_language_toggle_switches_window_title(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        translator.initialize("zh")
        window._retranslator.retranslate()
        self.assertIn("运动", window.windowTitle() + "x" * 0 or "运动控制" )  # ZH title present
        toggle = window.controls["languageToggle"]
        self.assertEqual("EN", toggle.text())  # in ZH, button offers EN
        window._toggle_language()
        self.assertEqual("en", translator.lang)
        self.assertEqual("中文", window.controls["languageToggle"].text())
```

Note: the title assertion is loose; tighten to your real ZH window title set in Step 3.

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest tests/test_main_window.py -k language_toggle -q`
Expected: FAIL (`KeyError: 'languageToggle'`).

- [ ] **Step 3: Implement** (in `MainWindow.__init__`)

Add near the top of `__init__` (after `self.controls = {}`):

```python
        from .i18n import translator, Retranslator, tr
        self._translator = translator
        self._retranslator = Retranslator(translator)
```

Replace the window title line `self.setWindowTitle("EthBoardTest Python")` with a
translated, registered title (add `"运动控制台"` -> `"Transfer Station Control"` to
`TRANSLATIONS` in `i18n.py`):

```python
        self._retranslator.title(self, "运动控制台")
```

Add the toggle button just after the Exit button block:

```python
        lang_toggle = self._register("languageToggle", QPushButton())
        lang_toggle.setCursor(Qt.PointingHandCursor)
        lang_toggle.setStyleSheet(
            "QPushButton{background:#1f6f5b;color:#ffffff;font-weight:800;border:none;"
            "border-radius:5px;padding:3px 12px;margin:0 4px;}"
            "QPushButton:hover{background:#175d4c;}"
        )
        lang_toggle.clicked.connect(self._toggle_language)
        self.statusBar().addPermanentWidget(lang_toggle)
        # Button shows the OTHER language; update label on every change.
        self._retranslator.custom(
            lambda b=lang_toggle: b.setText("中文" if self._translator.lang == "en" else "EN")
        )
```

Add the methods:

```python
    def _toggle_language(self) -> None:
        self._translator.set_language("zh" if self._translator.lang == "en" else "en")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.13 -m pytest tests/test_main_window.py -k language_toggle -q`
Expected: PASS. Then full file: `py -3.13 -m pytest tests/test_main_window.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/main_window.py src/automatic_transfer_station/i18n.py tests/test_main_window.py
git commit -m "feat(i18n): add status-bar language toggle and translated window title"
```

---

### Task 6: Translate temperature_panel.py (end-to-end proof)

**Files:**
- Modify: `src/automatic_transfer_station/temperature_panel.py`
- Modify: `src/automatic_transfer_station/i18n.py` (add this file's strings to `TRANSLATIONS`)
- Test: `tests/test_temperature_panel.py`

**Pattern (apply to every static UI string in the file):**
- Obtain a `Retranslator`. The panel receives `register=` already; add an optional
  `retranslator=None` constructor param and, when `None`, create one bound to the
  global `translator` (`from .i18n import translator, Retranslator, tr`). MainWindow
  passes its own `self._retranslator` so all widgets share one observer.
- `QLabel("温度控制")` building → create the label, then `self._rt.text(label, "温度控制")`.
- `QGroupBox("连接")` → `group = QGroupBox(); self._rt.title(group, "连接")`.
- `QPushButton("开始连接")` → create then `self._rt.text(btn, "开始连接")`.
- combo fixed items → `self._rt.items(combo, ["...", "..."])`.
- tooltips → `self._rt.tooltip(widget, "...")`.
- The heat-warning banner text → `self._rt.text(warning, "⚠ 高温警告：…")`.
- Dynamic status lines (`self._set_status(f"RD105 已连接：{port}")`) → wrap template:
  `self._set_status(tr("RD105 已连接：{port}").format(port=port))`.
- run_state display value: leave the raw value but translate via `tr(run_state)` when
  setting the label, and add the run_state strings (`输出开启`, `输出关闭`, `超温保护`,
  `超温保护-关闭输出失败`, `未知`) to the table. `_output_energized` keeps matching the
  raw ZH value from the controller (do NOT translate before that check).

Add every Chinese source string in this file to `TRANSLATIONS` with its English.
Representative seed entries (extend to cover the whole file):

```python
TRANSLATIONS.update({
    "温度控制": "Temperature Control",
    "连接": "Connection",
    "COM 端口": "COM port",
    "刷新端口": "Refresh ports",
    "开始连接": "Connect",
    "连接状态": "Connection status",
    "等待接入": "Waiting",
    "当前状态": "Current status",
    "当前温度": "Current temp",
    "当前给定": "Current setpoint",
    "目标温度": "Target temp",
    "输出 (PWM)": "Output (PWM)",
    "最大输出占空比": "Max duty",
    "控制状态": "Control status",
    "错误码": "Error code",
    "控制": "Control",
    "快速设温": "Quick set",
    "按速率控温": "Ramp set",
    "控温速率": "Ramp rate",
    "关闭输出": "Stop output",
    "温度曲线": "Temperature curve",
    "等待实时读数": "Waiting for live data",
    "输出开启": "Output ON",
    "输出关闭": "Output OFF",
    "超温保护": "Over-temp protection",
    "超温保护-关闭输出失败": "Over-temp — disable FAILED",
    "未知": "Unknown",
    "⚠ 高温警告：加热时请勿触碰加热台，谨防烫伤；离开或退出前务必关闭输出（ENABLE=0）":
        "⚠ HIGH-TEMP WARNING: do not touch the heater stage while heating; always stop output (ENABLE=0) before leaving or exiting",
    # …add the remaining temperature_panel strings (status templates etc.)…
})
```

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_temperature_panel.py
    def test_temperature_panel_translates_to_english(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel
        from automatic_transfer_station.i18n import translator
        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="", hwid="")],
        )
        translator.set_language("en")
        try:
            self.assertEqual("Temperature Control", controls["tempPanelTitle"].text())
            self.assertEqual("Stop output", controls["tempStopHeating"].text())
        finally:
            translator.initialize("zh")
```

Note: register the title label as `"tempPanelTitle"` in Step 2 so the test can read it.

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest tests/test_temperature_panel.py -k translates_to_english -q`
Expected: FAIL (`KeyError: 'tempPanelTitle'` or Chinese text returned).

- [ ] **Step 3: Apply the pattern across `temperature_panel.py`**

Convert every static string per the Pattern above; register the title label as
`tempPanelTitle`; create/accept the shared `Retranslator`; add all strings to
`TRANSLATIONS`. The fixture pins ZH so existing assertions still see Chinese.

- [ ] **Step 4: Run tests**

Run: `py -3.13 -m pytest tests/test_temperature_panel.py -q`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/temperature_panel.py src/automatic_transfer_station/i18n.py tests/test_temperature_panel.py
git commit -m "feat(i18n): translate temperature panel"
```

---

### Task 7: Translate main_window.py static text + dialogs

**Files:**
- Modify: `src/automatic_transfer_station/main_window.py`, `i18n.py`
- Test: `tests/test_main_window.py`

**Pattern:**
- All `QLabel(...)`, `QPushButton(...)`, `QGroupBox(...)`, axis-card titles, the
  microscope section title, the calibration button (`坐标标定`), Exit button (`退出 Exit`
  → register text `退出`), motion/board control buttons, and tooltips → use
  `self._retranslator.text/title/tooltip/items`.
- Dialog builders (calibration, microscope point manager, R-twist, Z-control, exit
  confirm `QMessageBox`): use `tr(...)` directly at build time (one-shot, NOT
  registered). Example: `QMessageBox.question(self, tr("确认退出"), tr("确认退出程序？…"), …)`.
- Status-bar messages (`self.statusBar().showMessage(f"…")`, `self._set_motion_status`,
  safety alarms): wrap templates with `tr(...).format(...)`.
- Add every Chinese string in the file to `TRANSLATIONS`.

- [ ] **Step 1: Write the failing test**

```python
    def test_exit_dialog_text_is_english_when_en(self):
        from automatic_transfer_station import main_window as mw
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        get_app()
        window = MainWindow(controller=RecordingController(),
                            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        translator.set_language("en")
        captured = {}
        def fake_q(parent, title, text, *a, **k):
            captured["title"] = title; captured["text"] = text
            return mw.QMessageBox.No
        original = mw.QMessageBox.question
        mw.QMessageBox.question = staticmethod(fake_q)
        try:
            window._request_exit()
        finally:
            mw.QMessageBox.question = original
            translator.initialize("zh")
        self.assertNotIn("确认", captured["title"])  # English, not Chinese
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3.13 -m pytest tests/test_main_window.py -k exit_dialog_text -q`
Expected: FAIL (title still Chinese).

- [ ] **Step 3: Apply the pattern across `main_window.py`**

Convert static text via `self._retranslator`; wrap dialog + status strings in `tr`;
add strings to `TRANSLATIONS`.

- [ ] **Step 4: Run tests**

Run: `py -3.13 -m pytest tests/test_main_window.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/main_window.py src/automatic_transfer_station/i18n.py tests/test_main_window.py
git commit -m "feat(i18n): translate main window and dialogs"
```

---

### Task 8: Translate objective_panel, gamepad_panel, gamepad_help

**Files:**
- Modify: `objective_panel.py`, `gamepad_panel.py`, `gamepad_help.py`, `i18n.py`
- Test: `tests/test_objective_panel.py`, `tests/test_gamepad_panel.py`, `tests/test_gamepad_help.py`

**Pattern:**
- `objective_panel` and `gamepad_panel`: same static-text pattern via a shared
  `Retranslator` (accept `retranslator=None`, default to one bound to global
  `translator`; MainWindow passes `self._retranslator`).
- `gamepad_help.py` draws labels onto a diagram (QPainter/QPixmap). Wrap each drawn
  label in `tr(...)` at draw time, and re-draw on language change: register a
  `custom(redraw_fn)` closure with the Retranslator so the diagram re-renders on
  switch. Add the diagram label strings (button names, MODE note, axis-role captions)
  to `TRANSLATIONS`.
- Add all Chinese strings from these three files to `TRANSLATIONS`.

- [ ] **Step 1: Write a failing test per panel** (mirror Task 6's English test: set EN, assert a known label/button reads English). Example for objective:

```python
    def test_objective_panel_translates_to_english(self):
        from automatic_transfer_station.objective_panel import ObjectivePanel
        from automatic_transfer_station.i18n import translator
        get_app()
        controls = {}
        panel = ObjectivePanel(<existing test ctor args>,
                               register=lambda n, w: controls.setdefault(n, w))
        translator.set_language("en")
        try:
            self.assertEqual("<English of a known label>", controls["<knownName>"].text())
        finally:
            translator.initialize("zh")
```

- [ ] **Step 2: Run to verify each fails.**

Run: `py -3.13 -m pytest tests/test_objective_panel.py tests/test_gamepad_panel.py tests/test_gamepad_help.py -k translate -q`
Expected: FAIL.

- [ ] **Step 3: Apply the pattern across the three files** and extend `TRANSLATIONS`.

- [ ] **Step 4: Run tests**

Run: `py -3.13 -m pytest tests/test_objective_panel.py tests/test_gamepad_panel.py tests/test_gamepad_help.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/automatic_transfer_station/objective_panel.py src/automatic_transfer_station/gamepad_panel.py src/automatic_transfer_station/gamepad_help.py src/automatic_transfer_station/i18n.py tests/test_objective_panel.py tests/test_gamepad_panel.py tests/test_gamepad_help.py
git commit -m "feat(i18n): translate objective, gamepad, and gamepad-help"
```

---

### Task 9: Translate axis role text + dynamic controller messages

**Files:**
- Modify: `main_window.py` (axis-card role text passed to `_build_axis_card` / stage
  hotspot roles), `rd105_controller.py`, `gas_controller.py`, `i18n.py`
- Test: `tests/test_main_window.py`, `tests/test_rd105_controller.py`, `tests/test_gas_controller.py`

**Pattern:**
- Axis role strings (整体左右, 吸附台左右, 显微镜上下位移, …) are passed as plain strings to
  builders. Translate at the point they become widget text (in `_build_axis_card`
  /`_add_stage_hotspot`/stage banner): wrap with `self._retranslator.text(...)` for the
  card subtitle label and register the stage-hotspot tooltip via `tr`.
- Controller user-facing messages: these are returned/stored as `last_error` /
  `safety_alarm` strings consumed by the GUI status bar. Wrap them with `tr(...)` at
  the **GUI display point** (where the status bar shows them) rather than inside the
  controller, so the controller stays UI-agnostic and its own tests (asserting ZH,
  pinned by conftest) stay green. Add the message templates to `TRANSLATIONS`.
- For messages built with values inside the controller (e.g.
  `f"{label} target {coord} … outside …"`), translate the stable template at display
  and pass values via `.format(...)`; where that is impractical, add the full produced
  ZH string family to the table and translate the displayed string by lookup
  (passthrough if unmatched).

- [ ] **Step 1: Write the failing test**

```python
    def test_axis_role_text_translates(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        get_app()
        window = MainWindow(controller=RecordingController(),
                            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        translator.set_language("en")
        try:
            self.assertEqual("<English role>", window.controls["<axis card subtitle name>"].text())
        finally:
            translator.initialize("zh")
```

- [ ] **Step 2: Run to verify it fails.**

Run: `py -3.13 -m pytest tests/test_main_window.py -k axis_role_text -q`
Expected: FAIL.

- [ ] **Step 3: Apply the pattern**; extend `TRANSLATIONS` with role text + controller message templates.

- [ ] **Step 4: Run tests**

Run: `py -3.13 -m pytest -q`
Expected: PASS (whole suite).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(i18n): translate axis roles and dynamic status/error messages"
```

---

### Task 10: Table sanity test + EN-default test + full sweep

**Files:**
- Test: `tests/test_i18n.py`
- Modify: `i18n.py` only if gaps found.

- [ ] **Step 1: Add sanity + default tests**

```python
class TableAndDefaultTests(unittest.TestCase):
    def test_no_empty_english_values(self):
        from automatic_transfer_station.i18n import TRANSLATIONS
        empties = [k for k, v in TRANSLATIONS.items() if not str(v).strip()]
        self.assertEqual([], empties)

    def test_app_default_language_is_english(self):
        from automatic_transfer_station.i18n import load_language
        import tempfile
        from pathlib import Path
        missing = Path(tempfile.mkdtemp()) / "nope.json"
        self.assertEqual("en", load_language(missing))
```

- [ ] **Step 2: Run the full suite**

Run: `py -3.13 -m pytest -q`
Expected: PASS.

- [ ] **Step 3: Manual smoke**

Run: `py -3.13 .\run_transfer_station.py --smoke-exit` → exit 0.
Launch the GUI, click the toggle, confirm every panel flips EN↔ZH live, and the
choice persists across a restart (check `runtime/ui_settings.json`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_i18n.py
git commit -m "test(i18n): table sanity and EN-default coverage"
```

---

## Self-Review notes

- Spec coverage: core (T1), persistence (T2), live retranslate (T3), default+test pin
  (T4), toggle+title (T5), all panels (T6–T8), axis roles + dynamic messages (T9),
  sanity/default (T10). All spec sections mapped.
- Identifiers (axis codes, COM, units, version) are never passed to `tr`, per Global
  Constraints — reviewers must check each conversion leaves them verbatim.
- Controller messages are translated at the GUI display point so controller unit tests
  (ZH-pinned) stay valid; this matches the spec's static/dynamic split.
