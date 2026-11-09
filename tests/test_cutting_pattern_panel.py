"""Pattern controls use real geometry settings, with no controllers or recipe I/O."""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QScrollArea

from automatic_transfer_station.cutting_pattern_panel import CuttingPatternPanel
from automatic_transfer_station.i18n import TRANSLATIONS, translator


# Main owns i18n.py. Supply proposed translations in memory so live switching and
# English layout can be tested independently before that integration lands.
TRANSLATION_KEYS = {
    "加工模式": "Pattern mode",
    "单线": "Single line",
    "区域行扫描": "Area lines",
    "区域点阵": "Area dots",
    "行距 (µm)": "Row pitch (µm)",
    "点距 (µm)": "Point pitch (µm)",
    "扫描角度 (°)": "Scan angle (°)",
    "到点稳定时间 (ms)": "Settling time (ms)",
    "曝光窗口 (ms)": "Exposure window (ms)",
    "区域配方": "Area recipe",
    "保存区域配方": "Save area recipe",
    "加载区域配方": "Load area recipe",
    "配方只保存区域和参数；加载后需重新拍照并对齐。":
        "Recipes store only the region and settings. Capture a fresh photo and realign after loading.",
    "单线使用画布上的切割线和路径速度。":
        "Single-line mode uses the drawn line and path speed.",
    "行扫描按单向曝光，回程与预加载保持关光。":
        "Lines expose in one direction. Return travel and preload stay dark.",
    "点阵移动全程关光；FIRE 设置毫秒曝光窗口，不代表单个皮秒脉冲。":
        "All dot travel stays dark. FIRE sets a millisecond exposure window, not a single picosecond pulse.",
    "尚未生成区域预览。": "No area preview yet.",
    "曝光数：{count} · 预计时间：{seconds:.2f} s":
        "Exposures: {count} · Estimated time: {seconds:.2f} s",
}


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def panel(app):
    widget = CuttingPatternPanel()
    widget.resize(340, 650)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def raster_settings(**overrides):
    from automatic_transfer_station.cutting_raster import RasterSettings

    values = dict(mode="dots", row_pitch_um=12.5, point_pitch_um=8.25,
                  angle_deg=-30., speed_um_s=123., settle_ms=75, dwell_ms=15)
    values.update(overrides)
    return RasterSettings(**values)


def test_default_single_line_and_stable_mode_ids(panel):
    assert [panel.mode_combo.itemData(i) for i in range(3)] == ["line", "lines", "dots"]
    assert panel.pattern_mode == "line" and panel.settings(100) is None
    assert (panel.row_pitch_spin.value(), panel.point_pitch_spin.value(), panel.angle_spin.value(),
            panel.settle_spin.value(), panel.dwell_spin.value()) == (10, 10, 0, 50, 10)
    assert not panel.row_pitch_spin.isEnabled() and not panel.dwell_spin.isEnabled()
    assert not panel.save_button.isEnabled() and panel.load_button.isEnabled()
    assert not panel.recipe_section.is_expanded
    assert panel.save_button.isHidden() and not panel.load_button.isHidden()
    assert not panel.description_label.isVisible() and panel.description_label.text()
    assert not panel.summary_label.isVisible() and panel.summary_label.text()


def test_mode_switch_hides_inapplicable_fields_and_labels_without_losing_values(panel, app):
    panel.set_settings(raster_settings())
    panel.recipe_section.set_expanded(True)
    controls = (panel.row_pitch_spin, panel.point_pitch_spin, panel.angle_spin,
                panel.settle_spin, panel.dwell_spin)
    original_values = [spin.value() for spin in controls]
    form = panel.findChild(QFormLayout)
    heights = {}
    for mode, visible in (("dots", (True, True, True, True, True)),
                          ("lines", (True, False, True, False, False)),
                          ("line", (False, False, False, False, False)),
                          ("dots", (True, True, True, True, True))):
        panel.set_mode(mode)
        app.processEvents()
        for spin, expected in zip(controls, visible):
            assert spin.isVisible() == expected
            assert form.labelForField(spin).isVisible() == expected
            assert spin.isEnabled() == expected
        assert panel.description_label.isVisible() == (mode == "dots")
        assert panel.mode_combo.toolTip() == panel.description_label.text()
        assert panel.save_button.isVisible() == (mode != "line")
        assert panel.load_button.isVisible()
        assert [spin.value() for spin in controls] == original_values
        heights[mode] = panel.sizeHint().height()
    assert heights["line"] < heights["lines"] < heights["dots"]


def test_mode_setter_invalidates_once_and_rejects_unknown_mode(panel):
    spy = QSignalSpy(panel.changed)
    panel.set_mode("lines")
    assert panel.pattern_mode == "lines" and spy.count() == 1
    panel.set_mode("lines")
    assert spy.count() == 1
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("dots"))
    assert panel.pattern_mode == "dots" and spy.count() == 2
    with pytest.raises(ValueError):
        panel.set_mode("translated label")
    assert panel.pattern_mode == "dots" and spy.count() == 2


def test_line_raster_omits_dot_parameters_and_returns_real_engine_type(panel):
    from automatic_transfer_station.cutting_raster import RasterSettings

    panel.set_mode("lines")
    settings = panel.settings(91.25)
    assert isinstance(settings, RasterSettings)
    assert settings.mode == "lines" and settings.speed_um_s == 91.25
    assert settings.row_pitch_um == 10 and settings.angle_deg == 0
    assert settings.point_pitch_um is None
    assert settings.settle_ms == settings.dwell_ms == 0
    assert panel.row_pitch_spin.isEnabled() and panel.angle_spin.isEnabled()
    assert all(not spin.isEnabled() for spin in (panel.point_pitch_spin, panel.settle_spin, panel.dwell_spin))


def test_dots_settings_include_integer_ms_envelope_and_dark_travel_hint(panel):
    panel.set_mode("dots")
    settings = panel.settings(100)
    assert settings.point_pitch_um == settings.row_pitch_um == 10
    assert settings.settle_ms == 50 and settings.dwell_ms == 10
    assert type(settings.settle_ms) is int and type(settings.dwell_ms) is int
    assert "FIRE" in panel.description_label.text() and "皮秒" in panel.description_label.text()
    assert panel.description_label.isVisible()
    assert panel.point_pitch_spin.isEnabled() and panel.settle_spin.isEnabled() and panel.dwell_spin.isEnabled()


@pytest.mark.parametrize("speed", [0, -1, math.nan, math.inf])
def test_invalid_speed_is_rejected_by_engine(panel, speed):
    panel.set_mode("dots")
    with pytest.raises(ValueError):
        panel.settings(speed)


def test_settings_load_updates_atomically_and_emits_one_complete_change(panel):
    settings = raster_settings()
    snapshots = []
    panel.changed.connect(lambda: snapshots.append(panel.settings(settings.speed_um_s)))
    panel.set_settings(settings)
    assert snapshots == [settings]
    assert panel.pattern_mode == "dots"
    assert panel.settings(settings.speed_um_s) == settings
    panel.set_settings(None)
    assert panel.pattern_mode == "line" and snapshots[-1] is None


def test_loading_lines_keeps_dot_controls_inactive_and_cannot_leak_old_dwell(panel):
    panel.set_settings(raster_settings())
    lines = raster_settings(mode="lines", point_pitch_um=None, settle_ms=0, dwell_ms=0)
    panel.set_settings(lines)
    assert panel.settings(lines.speed_um_s) == lines
    assert not panel.dwell_spin.isEnabled()
    panel.set_mode("dots")
    assert panel.settings(100).dwell_ms == 15


@pytest.mark.parametrize("values", [{"row_pitch_um": 20000.}, {"point_pitch_um": .00001},
                                    {"angle_deg": 12.34567}])
def test_settings_outside_ui_range_fail_without_partial_load(panel, values):
    panel.set_settings(raster_settings())
    before = panel.settings(123)
    spy = QSignalSpy(panel.changed)
    with pytest.raises(ValueError):
        panel.set_settings(raster_settings(**values))
    assert panel.settings(123) == before and spy.count() == 0


@pytest.mark.parametrize("angle", [-45., -180., -.0001])
def test_negative_angle_recipe_json_roundtrip_keeps_equivalent_direction(panel, angle):
    from automatic_transfer_station.cutting_geometry import BacklashSettings, CalibrationContext, ROI
    from automatic_transfer_station.cutting_raster import RasterRecipe

    panel.set_mode("dots")
    panel.angle_spin.setValue(angle)
    settings = panel.settings(100.)
    recipe = RasterRecipe(CalibrationContext("fake", "20X", 400, 200, 1), ROI(0, 0, 100, 100),
                          ((10, 10), (80, 10), (80, 70), (10, 70)), settings, BacklashSettings())
    loaded = RasterRecipe.from_json(recipe.to_json())
    panel.angle_spin.setValue(0.)
    panel.set_settings(loaded.settings)
    assert panel.settings(100.) == settings
    assert panel.angle_spin.value() == pytest.approx(angle if angle > -180. else 180.)


def test_field_changes_clear_stale_summary_and_emit_changed(panel):
    panel.set_mode("dots")
    panel.set_summary(25, 12.75)
    assert panel.summary_label.isVisible()
    spy = QSignalSpy(panel.changed)
    for spin in (panel.row_pitch_spin, panel.point_pitch_spin, panel.angle_spin,
                 panel.settle_spin, panel.dwell_spin):
        spin.setValue(spin.value() + 1)
    assert spy.count() == 5
    assert panel.summary_label.text() == "尚未生成区域预览。"
    assert not panel.summary_label.isVisible()


def test_clear_summary_is_public_and_does_not_reinvalidate_plan(panel):
    panel.set_mode("dots")
    panel.set_summary(25, 12.75)
    spy = QSignalSpy(panel.changed)
    panel.clear_summary()
    assert panel.summary_label.text() == "尚未生成区域预览。"
    assert not panel.summary_label.isVisible()
    assert panel.pattern_mode == "dots" and spy.count() == 0


def test_busy_locks_user_controls_and_file_requests_without_losing_settings(panel):
    panel.set_settings(raster_settings())
    panel.recipe_section.set_expanded(True)
    before = panel.settings(123)
    save, load, changed = QSignalSpy(panel.save_requested), QSignalSpy(panel.load_requested), QSignalSpy(panel.changed)
    panel.set_busy(True)
    for control in (panel.mode_combo, panel.row_pitch_spin, panel.point_pitch_spin, panel.angle_spin,
                    panel.settle_spin, panel.dwell_spin, panel.save_button, panel.load_button):
        assert not control.isEnabled()
        assert control.isVisible()
    QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(panel.load_button, Qt.MouseButton.LeftButton)
    assert save.count() == load.count() == changed.count() == 0
    assert panel.settings(123) == before
    panel.set_busy(False)
    QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(panel.load_button, Qt.MouseButton.LeftButton)
    assert save.count() == load.count() == 1 and changed.count() == 0


def test_recipe_disclosure_keeps_requests_available_without_invalidating_settings(panel, app):
    changed = QSignalSpy(panel.changed)
    save, load = QSignalSpy(panel.save_requested), QSignalSpy(panel.load_requested)
    QTest.mouseClick(panel.recipe_section.toggle_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert panel.load_button.isVisible() and not panel.save_button.isVisible()
    QTest.mouseClick(panel.load_button, Qt.MouseButton.LeftButton)
    assert load.count() == 1 and changed.count() == 0
    panel.set_settings(raster_settings())
    QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    assert save.count() == 1
    before = panel.settings(123)
    for _ in range(2):
        QTest.mouseClick(panel.recipe_section.toggle_button, Qt.MouseButton.LeftButton)
    assert panel.recipe_section.is_expanded and panel.settings(123) == before
    assert changed.count() == 1


@pytest.mark.parametrize("count,duration", [(-1, 1), (True, 1), (1, -1), (1, math.nan), (1, math.inf)])
def test_invalid_summary_does_not_replace_previous(panel, count, duration):
    panel.set_summary(20, 1.25)
    previous = panel.summary_label.text()
    with pytest.raises(ValueError):
        panel.set_summary(count, duration)
    assert panel.summary_label.text() == previous


@pytest.mark.parametrize("width", [320, 340])
def test_live_language_switch_and_compact_scroll_layout(panel, app, monkeypatch, width):
    for key, text in TRANSLATION_KEYS.items():
        if key not in TRANSLATIONS:
            monkeypatch.setitem(TRANSLATIONS, key, text)
    panel.set_mode("dots")
    panel.recipe_section.set_expanded(True)
    panel.set_summary(10000, 1234.56)
    changed = QSignalSpy(panel.changed)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    scroll.resize(width, 240)
    scroll.show()
    for language in ("en", "zh"):
        translator.set_language(language)
        app.processEvents()
        assert panel.pattern_mode == "dots" and changed.count() == 0
        assert "10000" in panel.summary_label.text() and "1234.56" in panel.summary_label.text()
        assert panel.summary_label.isVisible() and panel.description_label.isVisible()
        assert panel.mode_combo.toolTip() == panel.description_label.text()
        assert scroll.horizontalScrollBar().maximum() == 0
        assert panel.width() <= scroll.viewport().width() <= width
        for label in panel.findChildren(QLabel):
            if not label.isVisible():
                continue
            assert label.width() > 0 and label.height() >= label.fontMetrics().height()
            assert label.height() >= label.heightForWidth(label.width())
        for button in (panel.save_button, panel.load_button):
            assert button.width() >= button.minimumSizeHint().width()
        assert not panel.save_button.geometry().intersects(panel.load_button.geometry())
        form = panel.findChild(QFormLayout)
        assert form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapLongRows
        for spin in (panel.row_pitch_spin, panel.point_pitch_spin, panel.angle_spin,
                     panel.settle_spin, panel.dwell_spin):
            assert spin.minimumSizeHint().width() <= spin.width() <= 160
            label = form.labelForField(spin)
            assert not label.geometry().intersects(spin.geometry())
        if width == 340:
            assert form.labelForField(panel.row_pitch_spin).y() == panel.row_pitch_spin.y()
        if language == "en":
            assert panel.save_button.text() == "Save area recipe"
            assert panel.recipe_section.toggle_button.text() == "Area recipe"
            assert "millisecond" in panel.description_label.text()
        else:
            assert panel.save_button.text() == "保存区域配方"
    scroll.takeWidget()
    scroll.close()
