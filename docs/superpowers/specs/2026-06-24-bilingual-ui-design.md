# Bilingual (English / Chinese) UI — Design

Date: 2026-06-24
Status: Approved (pending spec review)

## Goal

Make the whole GUI switchable between English and Chinese at runtime. Default to
English on first run; remember the user's choice across runs. The switch is
instant (no restart). Scope is **all UI text**: panel titles, buttons, labels,
group titles, combo items, dialog titles, tooltips, the gamepad help diagram, axis
role text, and the dynamic status-bar / error messages (RD105 and motion card).

## Non-goals

- Translating log files, the audit log, or developer-facing exceptions that never
  reach the GUI. (Only user-visible strings are in scope.)
- A general plural/gender/locale framework. Two languages, simple string lookup.
- Translating identifiers: axis codes (X1, Y2, R, MicroscopeZ), COM port names,
  units (mm, °C, PWM, pulse), and version string stay verbatim in both languages.
  Only the *role* text around them is translated (e.g. 整体左右 → "Overall X").

## Architecture

### `i18n.py` — translation core (new module)

A single `Translator` instance owns:

- `lang`: `"en"` or `"zh"` (current language).
- `tr(zh: str) -> str`: returns `TRANSLATIONS[zh]` when `lang == "en"`, else returns
  `zh` unchanged. A key missing from the table returns `zh` unchanged (never blank),
  so an un-translated string degrades to Chinese rather than breaking.
- `set_language(lang)`: sets `lang`, persists it (see Persistence), and invokes every
  registered observer so the live UI re-renders.
- `add_observer(callable)`: register a zero-arg callback fired on every language
  change. The retranslation registry (below) registers one observer.

The translation source of truth is `TRANSLATIONS: dict[str, str]` mapping the
**existing Chinese source string** → its English. Keying by the Chinese string that
already exists in the code keeps call sites readable (`tr("温度控制")`) and avoids a
parallel key scheme. The Chinese "value" is implicit (it is the key).

A module-level singleton `translator` and a convenience `tr = translator.tr` are
exported; `tr` is imported wherever strings are emitted.

### Two string paths

1. **Static widget text** — applied once at build time and re-applied on switch.
   Small helpers register both the apply-now and the re-apply-on-change:
   - `tr_text(widget, zh)`   -> `widget.setText(tr(zh))`
   - `tr_title(widget, zh)`  -> `setWindowTitle` / `QGroupBox.setTitle`
   - `tr_tooltip(widget, zh)`-> `widget.setToolTip(tr(zh))`
   - `tr_items(combo, [zh…])`-> re-fills combo item texts (preserving index)
   - `tr_placeholder(widget, zh)` where relevant
   Each helper applies immediately and appends an apply-closure to a `Retranslator`
   registry. `set_language` fires the registry, which re-runs every closure — this is
   what makes the switch instant without rebuilding the window.

2. **Dynamic messages** — status-bar lines and error templates, many containing a
   runtime value. Wrapped at the call site: `tr("RD105 已连接：{port}").format(port=port)`.
   These are **not** retro-translated when the language flips (a transient line clears
   within ~1 s); only newly generated messages use the new language. Templates live in
   `TRANSLATIONS` with their `{name}` placeholders intact on both sides.

### Retranslator registry

A `Retranslator` collects apply-closures from the static helpers and exposes
`retranslate()` (re-run all closures). It is registered as a `translator` observer.
The existing central widget registry (`MainWindow.controls`, populated through the
`register=self._register` callback that every panel already receives) is the natural
home; panels accept the same `tr`/registry hooks they already accept for `register`.

### Dialogs created on demand

Calibration, point-manager, R-twist, Z-control, etc. build their strings each time
they open, so they render in the current language at open time. A dialog open during
a language flip is not live-updated (acceptable — close/reopen picks up the change).
This keeps the registry from accumulating stale references to destroyed dialogs.

## Toggle UI + persistence

- A flip **button** in the status bar, next to the Exit button (consistent with the
  recently added Exit). Its label shows the *other* language ("中文" while in English,
  "EN" while in Chinese). Clicking flips the language via `translator.set_language`.
- Persistence: `runtime/ui_settings.json` via the existing `JsonStore`, key
  `"language"`. Loaded at startup *before* the UI is built so the first paint is in the
  saved language. Absent / invalid file → default `"en"`.

## Error handling

- Missing translation key → returns the Chinese source (logged once at debug, never
  crashes, never blank).
- Corrupt / unreadable `ui_settings.json` → fall back to default `"en"`, surface
  nothing intrusive (consistent with how other runtime JSON stores degrade).
- `tr_items` must preserve the selected index across a re-fill so switching language
  never silently changes a user's selection.

## Testing

- `Translator`: EN and ZH lookup; missing-key passthrough; observer fired on
  `set_language`; settings round-trip (save → reload yields the same language);
  corrupt-settings → default.
- Live switch (integration): build `MainWindow`, flip to ZH, assert representative
  widgets read Chinese (e.g. temperature panel title `温度控制`, Exit dialog, axis
  card titles); flip to EN, assert English. Assert a combo keeps its selected index
  across the flip.
- Table sanity: no empty English values in `TRANSLATIONS`.
- All existing 515 tests stay green. Because the *app* default is now English but
  the existing suite asserts Chinese text, a shared test fixture forces the translator
  to **ZH** for the test session (in `tests/conftest.py` / `get_app`), without writing
  the settings file. In ZH, `tr(zh)` returns the Chinese key unchanged, so existing
  Chinese assertions (`温度控制`, `输出开启`, status templates, …) keep passing with no
  per-test edits. Only the new i18n tests opt into EN / exercise switching. The app's
  real EN default is covered by an explicit test that loads default settings.

## Rollout

Large but mechanical (~400 strings). Executed as a written plan, file-by-file, tests
gating each step, in this order:

1. `i18n.py` core + `Retranslator` + persistence + the status-bar toggle, with the
   table seeded by `temperature_panel` strings (proves the mechanism end-to-end).
2. `main_window.py` static text + dialogs.
3. `objective_panel.py`, `gamepad_panel.py`, `gamepad_help.py`.
4. Axis role text (`axis_calibration` labels / stage-map roles).
5. Dynamic status / error messages (`rd105_controller`, `gas_controller`,
   `main_window` status lines).

Each step adds its strings to `TRANSLATIONS` and switches its call sites to `tr`.
