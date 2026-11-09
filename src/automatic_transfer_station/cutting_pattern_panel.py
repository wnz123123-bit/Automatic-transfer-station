"""Pattern parameters only; geometry, recipe files and execution belong to callers."""

from __future__ import annotations

import math
from numbers import Real
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QPushButton,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from .cutting_widgets import CuttingDisclosure
from .i18n import Retranslator, translator

if TYPE_CHECKING:
    from .cutting_raster import RasterSettings


class CuttingPatternPanel(QWidget):
    """No files, controllers or hardware; all modes use stable combo item data.

    ``changed`` invalidates the caller's plan. A settings load emits it once,
    after every control is updated. ``settings`` returns None for single-line
    mode; area modes return the engine's validated immutable RasterSettings.
    Busy locks user controls; programmatic setters remain available.
    """

    changed = Signal()
    save_requested = Signal()
    load_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cuttingPatternPanel")
        self._rt = Retranslator(translator)
        self._busy = False
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._form = QFormLayout()
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setSpacing(6)
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("patternModeCombo")
        self.mode_combo.setMaximumWidth(340)
        for mode in ("line", "lines", "dots"):
            self.mode_combo.addItem("", mode)
        self._rt.items(self.mode_combo, ["单线", "区域行扫描", "区域点阵"])
        self._rt.custom(lambda: self.mode_combo.setAccessibleName(translator.tr("加工模式")))
        layout.addWidget(self.mode_combo)
        layout.addLayout(self._form)
        for name, label, minimum, maximum, value in (
            ("row_pitch_spin", "行距 (µm)", .0001, 10000., 10.),
            ("point_pitch_spin", "点距 (µm)", .0001, 10000., 10.),
            ("angle_spin", "扫描角度 (°)", -180., 180., 0.),
        ):
            spin = QDoubleSpinBox()
            spin.setDecimals(4)
            spin.setRange(minimum, maximum)
            spin.setValue(value)
            self._add_spin(self._form, name, label, spin)
        for name, label, minimum, value in (
            ("settle_spin", "到点稳定时间 (ms)", 0, 50),
            ("dwell_spin", "曝光窗口 (ms)", 1, 10),
        ):
            spin = QSpinBox()
            spin.setRange(minimum, 60000)
            spin.setValue(value)
            self._add_spin(self._form, name, label, spin)

        self.description_label = self._label()
        layout.addWidget(self.description_label)
        self.summary_label = self._label()
        layout.addWidget(self.summary_label)
        self._clear_summary()
        self.recipe_section = CuttingDisclosure("区域配方")
        layout.addWidget(self.recipe_section)
        buttons = QFormLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        buttons.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.recipe_section.content_layout.addLayout(buttons)
        self.save_button = QPushButton()
        self.load_button = QPushButton()
        for button, key, signal in (
            (self.save_button, "保存区域配方", self.save_requested),
            (self.load_button, "加载区域配方", self.load_requested),
        ):
            self._rt.text(button, key)
            self._rt.tooltip(button, "配方只保存区域和参数；加载后需重新拍照并对齐。")
            button.clicked.connect(signal.emit)
        buttons.addRow(self.save_button, self.load_button)
        layout.addStretch(1)
        self.mode_combo.currentIndexChanged.connect(self._controls_changed)
        self._sync_controls()
        self._rt.custom(lambda: self.mode_combo.setToolTip(self.description_label.text()))

    def _label(self, key=None):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if key is not None:
            self._rt.text(label, key)
        return label

    def _add_spin(self, form, name, label, spin):
        spin.setObjectName(name)
        spin.setMaximumWidth(160)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._controls_changed)
        field_label = self._label(label)
        field_label.setFixedWidth(140)
        form.addRow(field_label, spin)
        setattr(self, name, spin)

    @property
    def pattern_mode(self) -> str:
        return self.mode_combo.currentData()

    def set_mode(self, mode: str) -> None:
        """Select line/lines/dots, emitting changed only when the mode changes."""
        if mode not in ("line", "lines", "dots"):
            raise ValueError("Unsupported cutting pattern mode")
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))

    def settings(self, speed_um_s: float) -> RasterSettings | None:
        if self.pattern_mode == "line":
            return None
        from .cutting_raster import RasterSettings

        dots = self.pattern_mode == "dots"
        return RasterSettings(
            mode=self.pattern_mode,
            row_pitch_um=self.row_pitch_spin.value(),
            point_pitch_um=self.point_pitch_spin.value() if dots else None,
            angle_deg=self.angle_spin.value(),
            speed_um_s=speed_um_s,
            settle_ms=self.settle_spin.value() if dots else 0,
            dwell_ms=self.dwell_spin.value() if dots else 0,
        )

    def set_settings(self, settings: RasterSettings | None) -> None:
        """Load all parameters atomically; refuse silent rounding or clamping."""
        if settings is None:
            self.set_mode("line")
            return
        from .cutting_raster import RasterSettings

        if not isinstance(settings, RasterSettings) or settings.mode not in ("lines", "dots"):
            raise ValueError("Expected RasterSettings for lines or dots")
        # Geometry serializes [0,360); the signed UI represents the same angle.
        angle = settings.angle_deg - 360. if settings.angle_deg > 180. else settings.angle_deg
        values = [(self.row_pitch_spin, settings.row_pitch_um), (self.angle_spin, angle)]
        if settings.mode == "dots":
            values += [(self.point_pitch_spin, settings.point_pitch_um),
                       (self.settle_spin, settings.settle_ms), (self.dwell_spin, settings.dwell_ms)]
        for spin, value in values:
            if (isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
                    or not spin.minimum() <= value <= spin.maximum()):
                raise ValueError("Recipe parameter is outside the supported UI range")
            decimals = spin.decimals() if isinstance(spin, QDoubleSpinBox) else 0
            if not math.isclose(value, round(value, decimals), rel_tol=0., abs_tol=1e-10):
                raise ValueError("Recipe parameter exceeds UI precision")
        self._updating = True
        try:
            self.set_mode(settings.mode)
            for spin, value in values:
                spin.setValue(value)
        finally:
            self._updating = False
        self._controls_changed()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self):
        mode, idle = self.pattern_mode, not self._busy
        self.mode_combo.setEnabled(idle)
        for spins, applicable in (
            ((self.row_pitch_spin, self.angle_spin), mode != "line"),
            ((self.point_pitch_spin, self.settle_spin, self.dwell_spin), mode == "dots"),
        ):
            for spin in spins:
                # Hide the entire form row, including its label and spacing.
                self._form.setRowVisible(spin, applicable)
                spin.setEnabled(idle and applicable)
        self.save_button.setEnabled(idle and mode != "line")
        self.save_button.setVisible(mode != "line")
        self.load_button.setEnabled(idle)
        key = {
            "line": "单线使用画布上的切割线和路径速度。",
            "lines": "行扫描按单向曝光，回程与预加载保持关光。",
            "dots": "点阵移动全程关光；FIRE 设置毫秒曝光窗口，不代表单个皮秒脉冲。",
        }[mode]
        self._rt.message(self.description_label, key)
        self.description_label.setVisible(mode == "dots")
        self.mode_combo.setToolTip(self.description_label.text())

    def _clear_summary(self):
        self._rt.message(self.summary_label, "尚未生成区域预览。")
        self.summary_label.hide()

    def clear_summary(self) -> None:
        """Clear a stale preview count without changing settings or emitting changed."""
        self._clear_summary()

    def _controls_changed(self, *_):
        if self._updating:
            return
        self._sync_controls()
        self._clear_summary()
        self.changed.emit()

    def set_summary(self, exposure_count: int, estimated_duration_s: float) -> None:
        if (type(exposure_count) is not int or exposure_count < 0
                or isinstance(estimated_duration_s, bool) or not isinstance(estimated_duration_s, Real)
                or not math.isfinite(estimated_duration_s) or estimated_duration_s < 0):
            raise ValueError("Preview summary requires nonnegative finite values")
        self._rt.message(self.summary_label, "曝光数：{count} · 预计时间：{seconds:.2f} s",
                         count=exposure_count, seconds=estimated_duration_s)
        self.summary_label.show()
