"""Shared-camera cutting workspace with explicit commissioning for emission."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QMainWindow, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .camera_image import qimage_from_frame
from .cutting_canvas import CuttingCanvas
from .cutting_geometry import (
    BacklashSettings, CalibrationContext, CalibrationSample, CuttingCalibration,
    ROI, fit_calibration, plan_line,
)
from .cutting_session import SharedCuttingFrames, capture_snapshot, DarkCuttingRunner, same_positions
from .cutting_live import LiveQualification, LiveCuttingRunner, plan_live_line
from .cutting_raster import RasterPlan, RasterRecipe, plan_raster
from .cutting_raster_runner import RasterRunner, build_raster_route
from .cutting_pattern_panel import CuttingPatternPanel
from .cutting_widgets import CuttingDisclosure
from .cutting_live_view import CuttingLiveView
from .laser_panel import LaserPanel
from .i18n import Retranslator, tr, translator
from .json_store import atomic_write_json
from .paths import RUNTIME_DIR


# Retain windows/signals until daemon workers exit; closing never deletes a live
# QThread, nor does a Python worker call a widget or invoke a GUI callback.
_WORKING_WINDOWS: set = set()
_FIXED_AXES = (1, 2, 3, 4, 7, 8, 9)


def _read_fixed_axis_positions(value):
    """Strict JSON provenance: all non-sample axes, in raw planner pulses."""
    if (not isinstance(value, dict) or set(value) != {str(axis) for axis in _FIXED_AXES}
            or any(isinstance(pulse, bool) or not isinstance(pulse, (int, float))
                   or not math.isfinite(pulse) for pulse in value.values())):
        raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
    return tuple((axis, float(value[str(axis)])) for axis in _FIXED_AXES)


def _fixed_axis_positions(snapshot):
    positions = dict(snapshot.positions)
    return _read_fixed_axis_positions({str(axis): positions.get(axis) for axis in _FIXED_AXES})


def _fixed_axes_match(reference, snapshot):
    return reference is not None and same_positions(reference, _fixed_axis_positions(snapshot), tolerance=0.5)


class _WorkerSignals(QObject):
    finished = Signal(int, object, object)
    checked = Signal(object, object)
    progress = Signal(int, int, int)


class CuttingWindow(QMainWindow):
    """No I/O on construction; hardware operations run on daemon workers.

    ``start_guard`` is an optional GUI-thread callable; False or an exception
    prevents capture/qualification/run. ``prepare_for_close`` never blocks: it
    requests cancellation and returns False until workers have actually exited.
    Call it again (or observe busy_changed) before destroying the owning window.
    A timeout leaves the window retained and closed to new work, never declares
    an unconfirmed hardware operation finished.

    Calibration conservatively holds axes 1/2/3/4/7/8/9 fixed within 0.5 raw
    planner pulse of the ORIGINAL reference; only sample axes 5/6 may move.
    This includes MicroscopeZ (axis 3), since focus/magnification may change.
    Calibration JSON requires ``fixed_axis_positions`` (string axis keys to
    raw pulse values). Missing legacy provenance is never inferred on load.
    """

    busy_changed = Signal(bool)

    def __init__(self, controller, camera_panel, parent=None,
                 storage_dir=RUNTIME_DIR / "laser_cutting", *, laser_controller=None,
                 laser_port_lister=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self.camera_panel = camera_panel
        self.storage_dir = Path(storage_dir)
        self.start_guard = None
        self.frames = SharedCuttingFrames()
        self.snapshot = None
        self.calibration = None
        self.plan = None
        self.qualification = None
        self._live_route = None
        self._raster_route = None
        self.samples = []  # (snapshot, CalibrationSample), one landmark per still
        self._laser_pixel = None
        self._calibration_context = None
        self._calibration_configuration = None
        self._calibration_source_revision = None
        self._calibration_fixed_positions = None
        self._latest_context = None
        self._live_source_identity = self._identity()
        self._thread = None
        self._probe_thread = None
        self._completion = None
        self._check_completion = None
        self._generation = 0
        self._cancel = threading.Event()
        self._runner = None
        self._failed_runner = None
        self._job_kind = None
        self._job_done = None
        self._closing = False
        self._close_when_idle = False
        self._close_deadline = None
        self._confirmation = None
        self._signals = _WorkerSignals()  # no QWidget parent; safe until worker exit
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._signals.checked.connect(self._on_checked, Qt.ConnectionType.QueuedConnection)
        self._signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._rt = Retranslator(translator)
        self.setObjectName("cuttingWindow")
        self._rt.title(self, "激光切割工作台")
        self.resize(1180, 780)
        self.setMinimumSize(800, 560)
        self.laser_panel = LaserPanel(laser_controller, port_lister=laser_port_lister)
        self._build_ui()
        self._rt.custom(self._refresh_sidebar_status)
        self.laser_panel.emergency_requested.connect(self.abort)
        self.laser_panel.status_changed.connect(self._laser_status_changed)
        self.laser_panel.busy_changed.connect(self._sync_controls)
        self.camera_panel.raw_frame_ready.connect(self._receive_frame)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._status("打开共享相机并开始预览，然后拍摄新的定位照片。")
        self._sync_controls()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    @property
    def interlocked(self) -> bool:
        return self._failed_runner is not None

    @property
    def calibration_fixed_axis_positions(self):
        """Immutable (axis, raw pulse) reference for samples/fit, or None."""
        return self._calibration_fixed_positions

    def _label(self, text):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._rt.text(label, text)
        return label

    def _button(self, layout, name, text, callback):
        button = QPushButton()
        button.setObjectName(name)
        self._rt.text(button, text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        setattr(self, name, button)
        return button

    def _group(self, layout, text):
        group = QGroupBox()
        self._rt.title(group, text)
        inner = QVBoxLayout(group)
        layout.addWidget(group)
        return inner

    def _spin(self, form, name, label, maximum, value, minimum=0.0):
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setDecimals(4)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        form.addRow(self._label(label), spin)
        setattr(self, name, spin)
        return spin

    def _build_ui(self):
        self.canvas = CuttingCanvas()
        self.canvas.set_wheel_zoom_enabled(True)
        self.canvas.roi_selected.connect(self._roi_selected)
        self.canvas.line_selected.connect(self._line_selected)
        self.canvas.point_selected.connect(self._point_selected)
        self.canvas.polygon_selected.connect(self._line_selected)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self._label("原始图像（未翻转）· 仅 X3 / Y3"))
        self.image_tabs = QTabWidget()
        self.image_tabs.setObjectName("cuttingImageTabs")
        self.live_view = CuttingLiveView()
        self.image_tabs.addTab(self.canvas, "")
        self.image_tabs.addTab(self.live_view, "")
        self._rt.custom(lambda: self.image_tabs.setTabText(0, tr("定位与路径")))
        self._rt.custom(lambda: self.image_tabs.setTabText(1, tr("实时图像")))
        # Construction never opens a device or starts acquisition. User actions
        # and job launch may start the existing shared camera preview.
        self.image_tabs.setCurrentWidget(self.live_view)
        self.image_tabs.currentChanged.connect(self._image_page_changed)
        left_layout.addWidget(self.image_tabs, 1)
        left_layout.addWidget(self.status_label)

        # Daily operations and occasional setup are separate pages. Execution
        # and STOP remain outside both pages and their scroll areas.
        sidebar = QWidget()
        sidebar.setMinimumWidth(382)
        sidebar.setMaximumWidth(440)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 4, 0, 0)
        self.connection_summary = self._label("")
        self.connection_summary.setObjectName("cuttingConnectionSummary")
        self.connection_summary.setStyleSheet("color: #536271; padding: 2px 8px;")
        sidebar_layout.addWidget(self.connection_summary)
        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.setObjectName("cuttingSidebarTabs")
        sidebar_layout.addWidget(self.sidebar_tabs, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setMinimumWidth(360)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        camera = self._group(layout, "1 · 拍照")
        camera_row = QHBoxLayout()
        camera.addLayout(camera_row)
        self._button(camera_row, "open_camera_button", "打开相机窗口", self.open_camera)
        self._button(camera_row, "capture_button", "拍摄定位照片", self.capture)
        self.image_options = CuttingDisclosure("更多图像选项")
        camera.addWidget(self.image_options)
        image_options = self.image_options.content_layout
        row = QHBoxLayout()
        image_options.addLayout(row)
        self._button(row, "preview_button", "开始预览", self.start_preview)
        self._button(row, "live_button", "返回实时图像", self.show_live)
        self._button(image_options, "save_button", "保存原图、ROI 和元数据", self.save_snapshot)
        self.crop_check = QCheckBox()
        self._rt.text(self.crop_check, "预览 ROI")
        self._rt.tooltip(self.crop_check, "仅预览 ROI（保留原图坐标）")
        self.crop_check.toggled.connect(self.canvas.set_crop_preview)
        row = QHBoxLayout()
        image_options.addLayout(row)
        row.addWidget(self.crop_check)
        self._button(row, "fit_view_button", "适应窗口", lambda: self.canvas.set_view_zoom(1.0))

        path = self._group(layout, "2 · 路径与参数")
        # Preserve stable tool IDs and one source of canvas mode transitions.
        # The visible tools are task-specific buttons instead of a mixed menu
        # containing daily drawing and commissioning actions together.
        self.mode_combo = QComboBox(self)
        for mode in ("none", "roi", "line", "feature", "laser", "polygon"):
            self.mode_combo.addItem("", mode)
        self._rt.items(self.mode_combo, ["查看", "选择 ROI", "绘制切割线", "记录同一特征", "设置激光参考点", "绘制烧蚀区域"])
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.mode_combo.hide()
        self.drawing_buttons = {}
        tools = QHBoxLayout()
        path.addLayout(tools)
        for name, title, mode in (("view_tool", "查看", "none"), ("roi_tool", "选择 ROI", "roi")):
            button = self._button(tools, name, title, lambda checked=False, m=mode: self._select_canvas_mode(m))
            button.setCheckable(True)
            self.drawing_buttons[mode] = button
        self._button(tools, "draw_tool", "绘制路径", self._draw_current_pattern).setCheckable(True)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        path.addLayout(form)
        self._spin(form, "speed_spin", "路径速度 (µm/s)", 5000, 100, 0.0001).valueChanged.connect(self._invalidate_plan)
        self.pattern_panel = CuttingPatternPanel()
        self.pattern_panel.changed.connect(self._pattern_changed)
        self.pattern_panel.save_requested.connect(self.save_recipe)
        self.pattern_panel.load_requested.connect(self.load_recipe)
        path.addWidget(self.pattern_panel)
        self._button(path, "roi_area_button", "用 ROI 作为烧蚀区域", self.use_roi_polygon)
        help_section = CuttingDisclosure("绘图帮助")
        path.addWidget(help_section)
        for text in ("拖动选择 ROI 或线段；特征和激光参考点分别点击。",
                     "区域：依次点击顶点，Enter 或双击闭合，Backspace 撤销，Esc 取消。",
                     "虚线：关光移动与回差预加载；实线：切割路径。"):
            help_section.content_layout.addWidget(self._label(text))
        layout.addStretch(1)
        self.scroll.setWidget(panel)
        self.sidebar_tabs.addTab(self.scroll, "")

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        setup = QWidget()
        setup_layout = QVBoxLayout(setup)
        setup_layout.setContentsMargins(8, 6, 8, 6)
        setup_layout.addWidget(self._label("首次使用或硬件改变时，在这里完成设置。"))
        self.calibration_section = CuttingDisclosure("像素与位移标定")
        setup_layout.addWidget(self.calibration_section)
        calibration = self.calibration_section.content_layout
        calibration.addWidget(self._label("在至少三个不共线的 X3/Y3 位置拍照，每次点击同一个固定特征；另行设置激光参考点。"))
        row = QHBoxLayout()
        calibration.addLayout(row)
        for name, title, mode in (("feature_tool", "记录同一特征", "feature"),
                                  ("laser_point_tool", "设置激光参考点", "laser")):
            button = self._button(row, name, title, lambda checked=False, m=mode: self._select_canvas_mode(m))
            button.setCheckable(True)
            self.drawing_buttons[mode] = button
        self.sample_list = QListWidget()
        self.sample_list.setMaximumHeight(112)
        calibration.addWidget(self.sample_list)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        calibration.addLayout(form)
        self._spin(form, "max_error_spin", "允许最大误差 (µm)", 10000, 1.0)
        self._button(calibration, "fit_button", "拟合并保存标定", self.fit)
        self._button(calibration, "load_button", "加载匹配标定", self.load_calibration)
        self._button(calibration, "reset_button", "重新记录标定", self.reset_calibration)
        self.backlash_section = CuttingDisclosure("回程差补偿")
        setup_layout.addWidget(self.backlash_section)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.backlash_section.content_layout.addLayout(form)
        for name, label, maximum, value, minimum in (
            ("x_backlash_spin", "X3 实测回差 (µm)", 10000, 0, 0),
            ("y_backlash_spin", "Y3 实测回差 (µm)", 10000, 0, 0),
            ("x_percent_spin", "X3 补偿比例 (%)", 100, 100, 0),
            ("y_percent_spin", "Y3 补偿比例 (%)", 100, 100, 0),
        ):
            self._spin(form, name, label, maximum, value, minimum).valueChanged.connect(self._invalidate_plan)
        self.motion_section = CuttingDisclosure("运动与激光设备")
        setup_layout.addWidget(self.motion_section)
        laser = self.motion_section.content_layout
        self._button(laser, "qualify_button", "确认插补调试完成", self.qualify_controller)
        laser.addWidget(self.laser_panel)
        self._button(laser, "export_qualification_button", "导出硬件调试记录模板", self.export_qualification_template)
        self._button(laser, "load_qualification_button", "加载并确认硬件调试记录", self.load_qualification)
        self.live_status_label = self._label("真实出光需要独立关光联锁、运动卡触发和时序验证。")
        laser.addWidget(self.live_status_label)
        setup_layout.addStretch(1)
        self.settings_scroll.setWidget(setup)
        self.sidebar_tabs.addTab(self.settings_scroll, "")
        self._rt.custom(lambda: self.sidebar_tabs.setTabText(0, tr("加工")))
        self._rt.custom(lambda: self.sidebar_tabs.setTabText(1, tr("设置")))
        self.sidebar_tabs.currentChanged.connect(self._sidebar_page_changed)

        run = self._group(sidebar_layout, "3 · 预览与执行")
        self.readiness_label = self._label("")
        self.readiness_label.setObjectName("cuttingReadiness")
        run.addWidget(self.readiness_label)
        self._button(run, "plan_button", "生成路径预览", self.make_plan)
        row = QHBoxLayout()
        run.addLayout(row)
        self._button(row, "run_button", "暗运行", self.request_run)
        self._button(row, "live_run_button", "执行激光切割", self.request_live_run)
        self.capture_button.setStyleSheet("QPushButton:enabled { background:#1f6f5b; color:white; padding:5px; }")
        self.plan_button.setStyleSheet("QPushButton:enabled { background:#1f6f5b; color:white; padding:5px; }")
        self._button(left_layout, "abort_button", "取消 / 停止", self.abort)
        self.abort_button.setStyleSheet("QPushButton { background: #a52525; color: white; padding: 8px; font-weight: bold; }")
        self._button(left_layout, "retry_cleanup_button", "重试关光与停止", self.retry_cleanup)
        self._abort_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._abort_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._abort_shortcut.activated.connect(self.abort)
        self.splitter = QSplitter()
        self.splitter.addWidget(left)
        self.splitter.addWidget(sidebar)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([820, 340])
        self.setCentralWidget(self.splitter)

    def _select_canvas_mode(self, mode):
        if self.mode_combo.isEnabled():
            self.image_tabs.setCurrentWidget(self.canvas)
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
        self._sync_drawing_buttons()

    def _draw_current_pattern(self):
        self._select_canvas_mode("line" if self.pattern_panel.pattern_mode == "line" else "polygon")

    def _sidebar_page_changed(self, index):
        # A hidden calibration tool must not keep collecting points after the
        # operator returns to the daily cutting page.
        if index == 0 and self.mode_combo.currentData() in {"feature", "laser"}:
            self._select_canvas_mode("none")

    def _sync_drawing_buttons(self):
        mode = self.mode_combo.currentData()
        editable = self.mode_combo.isEnabled()
        for code, button in self.drawing_buttons.items():
            button.setEnabled(editable)
            button.setChecked(editable and mode == code)
        self.draw_tool.setEnabled(editable)
        self.draw_tool.setChecked(editable and mode in {"line", "polygon"})

    def _refresh_sidebar_status(self):
        # Presentation only: never read lock-taking controller properties here.
        # Motion qualification is checked by the existing execution worker.
        camera = getattr(self.camera_panel, "controller", None)
        camera_ready = getattr(camera, "current_device", None) is not None
        status = self.laser_panel.status
        laser_state = getattr(getattr(status, "state", None), "name", "DISCONNECTED")
        state_key = {"DISCONNECTED": "未连接", "BOOT_HOLD": "待启动确认",
                     "DISARMED": "已连接", "ARMED": "待命", "FOLLOW": "作业中",
                     "FIRING": "作业中", "FAULT": "故障", "UNKNOWN": "状态未知"}.get(laser_state, "状态未知")
        self.connection_summary.setText(tr("相机：{camera} · 标定：{calibration}\n激光：{laser}").format(
            camera=tr("已连接" if camera_ready else "未连接"),
            calibration=tr("已就绪" if self.calibration is not None else "未就绪"), laser=tr(state_key)))
        if self.interlocked:
            reason = "停止状态未确认，请检查设备连接并重试停止。"
        elif self.busy:
            reason = "作业进行中；可随时停止。"
        elif laser_state in {"FAULT", "UNKNOWN"}:
            reason = "激光状态异常，请在设置中检查设备。"
        elif not camera_ready:
            reason = "请先连接相机。"
        elif not getattr(self.controller, "is_open", False):
            reason = "请先打开运动控制。"
        elif self.snapshot is None:
            reason = "拍照后选择 ROI 和切割路径。"
        elif self.calibration is None:
            reason = "请在设置中完成或加载标定。"
        elif self.plan is None:
            reason = "绘制路径后生成预览。"
        elif laser_state == "DISCONNECTED":
            reason = "激光未连接；空跑前请检查运动设置。"
        elif not self.live_run_button.isEnabled():
            reason = "出光条件未满足，详情见设置。"
        else:
            reason = "路径已就绪，请先关光空跑。"
        self.readiness_label.setText(tr(reason))

    def _status(self, text, **values):
        self._rt.message(self.status_label, text, **values)

    def _error(self, error):
        self._status("操作失败：{error}", error=str(error))

    def _identity(self):
        camera = getattr(self.camera_panel, "controller", None)
        device = getattr(camera, "current_device", None)
        return (getattr(device, "serial_number", ""),
                getattr(self.camera_panel, "_current_objective", ""),
                getattr(self.camera_panel, "cutting_reference_revision", 0))

    def _optics_match(self, snapshot):
        serial, objective, revision = self._identity()
        return (serial == snapshot.context.camera_serial and objective == snapshot.context.objective
                and revision == snapshot.envelope.source_revision
                and (self._latest_context is None or self._latest_context == snapshot.context))

    @Slot(object)
    def _receive_frame(self, frame):
        if self._closing:
            return
        # Malformed image metadata is not evidence that optics changed. Show
        # the monitor error without publishing it into reference validation.
        if not self.live_view.valid_image(frame):
            self._sync_live_source()
            self.live_view.set_frame(frame)
            return
        try:
            serial, objective, revision = self._identity()
            if not serial:
                return
            # Prefer acquisition-time context to delivery-time GUI values.
            provenance = frame.metadata.get("capture_context")
            if isinstance(provenance, dict):
                if (provenance.get("camera_serial", serial) != serial
                        or provenance.get("objective", objective) != objective
                        or provenance.get("source_revision", revision) != revision):
                    return
            context = CalibrationContext(serial, objective, frame.width, frame.height, frame.binning)
            self._latest_context = context
            self.frames.publish(frame, objective, serial, source_revision=revision)
            self._check_optics()
            self._sync_live_source()
            # Live pixels never overwrite the reference image or its geometry,
            # even while a line or area worker owns the motion controller.
            self.live_view.set_frame(frame)
        except (ValueError, TypeError, AttributeError) as error:
            self._error(error)

    def _check_optics(self):
        if self.snapshot is not None and not self._optics_match(self.snapshot):
            self._invalidate_snapshot("相机或物镜已改变，请重新拍摄定位照片。")
            self.reset_calibration()
            self.abort()
        if self._calibration_context is not None:
            serial, objective, revision = self._identity()
            context = self._calibration_context
            if (serial != context.camera_serial or objective != context.objective
                    or revision != self._calibration_source_revision
                    or (self._latest_context is not None and self._latest_context != context)):
                self.reset_calibration()

    def _sync_controls(self, *_):
        idle = not self.busy and not self._closing and not self.interlocked
        still = idle and self.snapshot is not None
        for button in (self.capture_button, self.qualify_button):
            button.setEnabled(idle)
        for button in (self.preview_button, self.live_button):
            button.setEnabled(not self._closing)
        self.open_camera_button.setEnabled(not self._closing)
        self.mode_combo.setEnabled(still)
        self._sync_drawing_buttons()
        self.canvas.set_editable(still)
        self.save_button.setEnabled(still and self.canvas.roi is not None)
        self.crop_check.setEnabled(still and self.canvas.roi is not None)
        self.roi_area_button.setEnabled(still and self.canvas.roi is not None)
        self.roi_area_button.setVisible(self.pattern_panel.pattern_mode != "line")
        self.fit_button.setEnabled(still and len(self.samples) >= 3 and self._laser_pixel is not None)
        self.load_button.setEnabled(still)
        self.reset_button.setEnabled(idle)
        self.plan_button.setEnabled(still and self.calibration is not None
                                    and self.canvas.roi is not None and (
                                        self.canvas.line is not None if self.pattern_panel.pattern_mode == "line"
                                        else self.canvas.polygon is not None))
        self.pattern_panel.set_busy(not idle)
        self.pattern_panel.save_button.setEnabled(still and self.pattern_panel.pattern_mode != "line"
                                                  and self.canvas.polygon is not None)
        self.pattern_panel.load_button.setEnabled(still)
        self.run_button.setEnabled(still and self.plan is not None)
        self.live_run_button.setEnabled(still and self.plan is not None and self._live_route is not None
                                        and self.qualification is not None and self.laser_panel.startup_confirmed
                                        and not self.laser_panel.busy)
        self.export_qualification_button.setEnabled(idle and not self.laser_panel.busy)
        self.load_qualification_button.setEnabled(idle and not self.laser_panel.busy)
        self.abort_button.setEnabled(True)
        self.retry_cleanup_button.setVisible(self.interlocked)
        self.retry_cleanup_button.setEnabled(self.interlocked and not self.busy and not self._closing
                                             and not self.laser_panel.busy)
        for spin in (self.max_error_spin, self.x_backlash_spin, self.y_backlash_spin,
                     self.x_percent_spin, self.y_percent_spin, self.speed_spin):
            spin.setEnabled(idle)
        self._refresh_sidebar_status()

    def _guard(self):
        if self.busy or self._closing or self.laser_panel.busy:
            return False
        if self.interlocked:
            self._status("停止状态未确认，请检查设备连接并重试停止。")
            return False
        try:
            if self.start_guard is not None and self.start_guard() is False:
                self._status("其他操作尚未结束，无法开始。")
                return False
            return True
        except Exception as error:
            self._error(error)
            return False

    def _launch(self, kind, work, done, *, cancel=None):
        if self.busy or self._closing:
            return False
        self._generation += 1
        generation = self._generation
        signals = self._signals
        self._cancel = cancel if cancel is not None else threading.Event()
        self._job_done, self._job_kind = done, kind
        self._completion = None
        cancel_event = self._cancel

        def execute():
            result = error = None
            try:
                if cancel_event.is_set():
                    raise RuntimeError("Operation cancelled before worker start")
                result = work()
            except Exception as exc:
                error = exc
            signals.finished.emit(generation, result, error)

        self._thread = threading.Thread(target=execute, name=f"ATSCutting-{kind}", daemon=True)
        self.laser_panel.set_job_busy(True)
        _WORKING_WINDOWS.add(self)
        self._sync_controls()
        self.busy_changed.emit(True)  # Main locks competing GUI before work starts.
        if kind in {"run", "live-run", "raster-run"}:
            self.show_live()
        self._thread.start()
        return True

    @Slot(int, object, object)
    def _on_finished(self, generation, result, error):
        if generation == self._generation:
            self._completion = result, error
            self._drain_workers()

    @Slot(object, object)
    def _on_checked(self, snapshot, error):
        self._check_completion = snapshot, error
        self._drain_workers()

    @Slot(int, int, int)
    def _on_progress(self, generation, step, total):
        if generation == self._generation and self.busy and not self._cancel.is_set():
            self._status("切割进度：{step}/{total}" if self._job_kind == "live-run"
                         else "暗运行进度：{step}/{total}", step=step, total=total)

    def _drain_workers(self):
        if self._probe_thread is not None and not self._probe_thread.is_alive() and self._check_completion:
            snapshot, error = self._check_completion
            self._probe_thread = self._check_completion = None
            if error is not None and self.snapshot is snapshot and not self.busy:
                self._invalidate_snapshot("运动状态已改变，请重新拍摄定位照片。")
        if self._thread is not None and not self._thread.is_alive() and self._completion is not None:
            result, error = self._completion
            done, kind = self._job_done, self._job_kind
            cancelled = self._cancel.is_set()
            self._thread = self._completion = self._job_done = self._job_kind = None
            if getattr(self._runner, "cleanup_required", False) is True:
                self._failed_runner = self._runner
            self._runner = None
            self.laser_panel.set_job_busy(False)
            if kind in {"run", "live-run", "raster-run"}:
                self._invalidate_snapshot("运行结束后需重新拍摄定位照片。")
            if error is not None:
                self._error(error)
            elif cancelled:
                self._status("操作已取消。")
            elif not self._closing:
                try:
                    done(result)
                except Exception as exc:
                    self._error(exc)
            self._sync_controls()
            self.busy_changed.emit(self.interlocked)
        if self._thread is None and self._probe_thread is None and not self.laser_panel.busy:
            _WORKING_WINDOWS.discard(self)
            if self._closing and self._close_when_idle:
                self._close_when_idle = False
                self.close()

    def _tick(self):
        self._drain_workers()
        if self._closing:
            if self._close_deadline is not None and time.monotonic() >= self._close_deadline:
                self._close_deadline = None
                if self._thread is not None or self._probe_thread is not None:
                    self._status("停止尚未确认，窗口保留并等待任务退出。")
            return
        self._check_optics()
        self._refresh_live_view()
        if self.snapshot is not None and not self.busy and self._probe_thread is None:
            snapshot, controller, signals = self.snapshot, self.controller, self._signals

            def check():
                error = None
                try:
                    snapshot.validate(controller, snapshot.context)
                except Exception as exc:
                    error = exc
                signals.checked.emit(snapshot, error)

            self._probe_thread = threading.Thread(target=check, name="ATSCuttingReferenceCheck", daemon=True)
            _WORKING_WINDOWS.add(self)
            self._probe_thread.start()

    def open_camera(self):
        window = self.camera_panel.window()
        window.show()
        window.raise_()
        window.activateWindow()

    def start_preview(self):
        self.show_live()

    def show_live(self):
        if self._closing:
            return
        if self.image_tabs.currentWidget() is self.live_view:
            self._image_page_changed(1)
        else:
            self.image_tabs.setCurrentWidget(self.live_view)

    def _image_page_changed(self, _index):
        if self.image_tabs.currentWidget() is self.live_view and not self._closing:
            # CameraPanel.start_preview is idempotent and starts its existing
            # acquisition thread; it does not reconnect or alter camera settings.
            try:
                if not getattr(self.camera_panel, "_closing", False):
                    self.camera_panel.start_preview()
            except Exception as error:
                self._status("实时预览无法启动：{error}", error=str(error))
            self._refresh_live_view()

    def _sync_live_source(self):
        identity = self._identity()
        if identity != self._live_source_identity:
            self.live_view.reset()
            self._live_source_identity = identity
        return bool(identity[0]) and not getattr(self.camera_panel, "_closing", False)

    def _refresh_live_view(self):
        self.live_view.refresh(self._sync_live_source())

    def capture(self):
        if not self._guard():
            return
        controller, frames = self.controller, self.frames
        fresh_frame = getattr(self.camera_panel, "capture_cutting_reference", None)
        cancel = threading.Event()
        identity = self._identity()
        self._status("正在拍摄新的定位照片…")

        def done(snapshot):
            # A fresh acquisition is authoritative; the last delivered preview
            # may have different dimensions after changing mode while stopped.
            self._latest_context = snapshot.context
            if identity != self._identity() or not self._optics_match(snapshot):
                raise ValueError(tr("相机或物镜已改变，请重新拍摄定位照片。"))
            self._accept_snapshot(snapshot)

        self._launch("capture", lambda: capture_snapshot(controller, frames, cancel,
                     fresh_frame=fresh_frame), done, cancel=cancel)

    def _accept_snapshot(self, snapshot):
        fixed_changed = (
            self.calibration is not None or self.samples or self._calibration_fixed_positions is not None
        ) and not _fixed_axes_match(self._calibration_fixed_positions, snapshot)
        if fixed_changed:
            self.reset_calibration()
        self.snapshot = snapshot
        self.plan = None
        self._live_route = None
        self._raster_route = None
        self.canvas.set_image(qimage_from_frame(snapshot.envelope.frame))
        self.image_tabs.setCurrentWidget(self.canvas)
        self.canvas.clear_annotations()
        self.crop_check.setChecked(False)
        self.canvas.set_laser_point(self._laser_pixel)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData("roi"))
        self.canvas.set_mode("roi")
        self._status("固定轴位置缺失或已改变，请重新标定。" if fixed_changed
                     else "定位照片已就绪；请选择 ROI、特征或线段。")
        self._sync_controls()

    def _invalidate_snapshot(self, reason):
        self.snapshot = None
        self._invalidate_plan()
        self.canvas.set_mode("none")
        if self._confirmation is not None:
            self._confirmation.reject()
        self._status(reason)
        self._sync_controls()

    def _invalidate_plan(self, *_):
        self.plan = None
        self._live_route = None
        self._raster_route = None
        self.canvas.set_path_preview([])
        self.canvas.set_exposure_points([])
        self.pattern_panel.clear_summary()
        self._sync_controls()

    def _mode_changed(self, *_):
        mode = self.mode_combo.currentData() if self.snapshot is not None else "none"
        self.canvas.set_mode(mode)
        if self.snapshot is not None:
            self.image_tabs.setCurrentWidget(self.canvas)
        if mode == "line" and self.pattern_panel.pattern_mode != "line":
            self.pattern_panel.set_mode("line")
        elif mode == "polygon" and self.pattern_panel.pattern_mode == "line":
            self.pattern_panel.set_mode("lines")
        self._sync_drawing_buttons()

    def _pattern_changed(self):
        self._invalidate_plan()
        if self.pattern_panel.pattern_mode == "line":
            self.canvas.set_polygon(None)
        else:
            self.canvas.set_line(None)
        if self.snapshot is not None:
            mode = "line" if self.pattern_panel.pattern_mode == "line" else "polygon"
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))

    def use_roi_polygon(self):
        if self.busy or self.snapshot is None or self.canvas.roi is None:
            return
        x, y, width, height = self.canvas.roi
        right, bottom = math.nextafter(x + width, x), math.nextafter(y + height, y)
        self.canvas.set_polygon(((x, y), (right, y), (right, bottom), (x, bottom)))
        if self.pattern_panel.pattern_mode == "line":
            self.pattern_panel.set_mode("lines")
        self._invalidate_plan()

    def _roi_selected(self, roi):
        self._invalidate_plan()
        self._sync_controls()

    def _line_selected(self, line):
        self._invalidate_plan()
        self._sync_controls()

    def _point_selected(self, point):
        if self.busy or self.snapshot is None or not self._optics_match(self.snapshot):
            return
        snapshot = self.snapshot
        if (self._calibration_fixed_positions is not None
                and not _fixed_axes_match(self._calibration_fixed_positions, snapshot)):
            self.reset_calibration()
            self._status("固定轴位置缺失或已改变，请重新标定。")
            return
        if self.mode_combo.currentData() == "laser":
            if self._calibration_fixed_positions is None:
                self._calibration_fixed_positions = _fixed_axis_positions(snapshot)
            self._calibration_context = snapshot.context
            self._calibration_configuration = snapshot.axis_configuration
            self._calibration_source_revision = snapshot.envelope.source_revision
            self._laser_pixel = point
            self.calibration = None
            self._invalidate_plan()
        elif self.mode_combo.currentData() == "feature":
            if self.samples and (self.samples[0][0].context != snapshot.context
                                 or self.samples[0][0].axis_configuration != snapshot.axis_configuration
                                 or self.samples[0][0].envelope.source_revision != snapshot.envelope.source_revision):
                self.reset_calibration()
            if self._calibration_fixed_positions is None:
                self._calibration_fixed_positions = _fixed_axis_positions(snapshot)
            if any(not _fixed_axes_match(self._calibration_fixed_positions, saved)
                   for saved, _ in self.samples):
                self.reset_calibration()
                self._status("固定轴位置缺失或已改变，请重新标定。")
                return
            self.samples = [(s, sample) for s, sample in self.samples if s is not snapshot]
            self.samples.append((snapshot, CalibrationSample(snapshot.stage_um, point)))
            self._calibration_context = snapshot.context
            self._calibration_configuration = snapshot.axis_configuration
            self._calibration_source_revision = snapshot.envelope.source_revision
            self.calibration = None
            self._invalidate_plan()
            self._refresh_samples()
        self._sync_controls()

    def _refresh_samples(self):
        self.sample_list.clear()
        for index, (_, sample) in enumerate(self.samples, 1):
            x, y = sample.stage_um
            px, py = sample.pixel
            self.sample_list.addItem(f"{index}: X3 {x:.3f}, Y3 {y:.3f} µm\n    ({px:.3f}, {py:.3f}) px")

    def reset_calibration(self):
        self.samples = []
        self.calibration = None
        self._laser_pixel = None
        self._calibration_context = self._calibration_configuration = self._calibration_source_revision = None
        self._calibration_fixed_positions = None
        self.canvas.set_laser_point(None)
        self._refresh_samples()
        self._invalidate_plan()

    def fit(self):
        if self.busy or self.snapshot is None or not self._optics_match(self.snapshot):
            return
        snapshot, laser = self.snapshot, self._laser_pixel
        records = tuple(self.samples)
        fixed_positions = self._calibration_fixed_positions
        samples = tuple(sample for _, sample in self.samples)
        threshold = self.max_error_spin.value()
        path = self.storage_dir / "calibration.json"

        def work():
            if (not _fixed_axes_match(fixed_positions, snapshot)
                    or any(not _fixed_axes_match(fixed_positions, saved) for saved, _ in records)):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            calibration = fit_calibration(samples, laser, snapshot.context, threshold)
            payload = {"schema_version": 1, "calibration": calibration.to_dict(),
                       "axis_configuration": snapshot.axis_configuration,
                       "fixed_axis_positions": {str(axis): pulse for axis, pulse in fixed_positions},
                       "samples": [{"stage_um": sample.stage_um, "pixel": sample.pixel} for sample in samples]}
            atomic_write_json(path, payload)
            return calibration

        self._launch("fit", work, lambda result: self._accept_calibration(result, snapshot, fixed_positions))

    def _accept_calibration(self, calibration, snapshot, fixed_positions=None):
        if not self._optics_match(snapshot):
            raise ValueError(tr("相机或物镜已改变，请重新拍摄定位照片。"))
        calibration.require_context(snapshot.context)
        if fixed_positions is None:
            fixed_positions = _fixed_axis_positions(snapshot)
        if not _fixed_axes_match(fixed_positions, snapshot):
            raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
        self.calibration = calibration
        self._calibration_context = snapshot.context
        self._calibration_configuration = snapshot.axis_configuration
        self._calibration_source_revision = snapshot.envelope.source_revision
        self._calibration_fixed_positions = fixed_positions
        self._laser_pixel = calibration.laser_pixel
        self.canvas.set_laser_point(self._laser_pixel)
        self._invalidate_plan()
        self._status("标定已就绪，最大残差 {error:.4f} µm。", error=calibration.max_residual_um)

    def load_calibration(self):
        if self.busy or self.snapshot is None:
            return
        snapshot, path = self.snapshot, self.storage_dir / "calibration.json"

        def work():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or payload.get("axis_configuration") != snapshot.axis_configuration:
                raise ValueError(tr("标定的轴配置不匹配，请重新标定。"))
            fixed_positions = _read_fixed_axis_positions(payload.get("fixed_axis_positions"))
            if not _fixed_axes_match(fixed_positions, snapshot):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            calibration = CuttingCalibration.from_dict(payload["calibration"])
            calibration.require_context(snapshot.context)
            return calibration, fixed_positions

        self._launch("load", work, lambda result: self._accept_calibration(result[0], snapshot, result[1]))

    def save_snapshot(self):
        if self.busy or self.snapshot is None or self.canvas.roi is None:
            return
        snapshot, roi = self.snapshot, self.canvas.roi
        folder = self.storage_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:10])

        def work():
            image = qimage_from_frame(snapshot.envelope.frame)
            x, y, width, height = roi
            left, top = math.floor(x), math.floor(y)
            right, bottom = math.ceil(x + width), math.ceil(y + height)
            crop = image.copy(left, top, right - left, bottom - top)
            folder.mkdir(parents=True, exist_ok=False)
            if not image.save(str(folder / "full.png")) or not crop.save(str(folder / "roi.png")):
                raise OSError(tr("图像保存失败。"))
            atomic_write_json(folder / "metadata.json", {
                "schema_version": 1, "context": snapshot.context.to_dict(),
                "captured_at_monotonic": snapshot.envelope.captured_at,
                "source_revision": snapshot.envelope.source_revision,
                "motion_revision": snapshot.motion_revision, "stage_um": snapshot.stage_um,
                "raw_axis_positions": dict(snapshot.positions),
                "axis_configuration": snapshot.axis_configuration,
                "roi_full_frame_pixels": roi, "crop_pixel_bounds": [left, top, right, bottom],
                "full_image": "full.png", "roi_image": "roi.png",
            })
            return folder

        self._launch("save", work, lambda folder: self._status("图像及元数据已保存：{path}", path=str(folder)))

    def make_plan(self):
        if self.busy or self.snapshot is None:
            return
        self.image_tabs.setCurrentWidget(self.canvas)
        try:
            if not self._optics_match(self.snapshot):
                raise ValueError(tr("相机或物镜已改变，请重新拍摄定位照片。"))
            if self.calibration is None or self._calibration_configuration != self.snapshot.axis_configuration:
                raise ValueError(tr("请先完成匹配标定。"))
            if not _fixed_axes_match(self._calibration_fixed_positions, self.snapshot):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            self.calibration.require_context(self.snapshot.context)
            if self.canvas.roi is None:
                raise ValueError(tr("请先选择 ROI 和切割线。"))
            backlash = BacklashSettings(self.x_backlash_spin.value(), self.y_backlash_spin.value(),
                                        self.x_percent_spin.value(), self.y_percent_spin.value())
            if self.pattern_panel.pattern_mode != "line":
                if not self.canvas.polygon:
                    raise ValueError(tr("请先绘制闭合区域。"))
                self._make_raster_plan(backlash)
                return
            if self.canvas.line is None:
                raise ValueError(tr("请先选择 ROI 和切割线。"))
            plan = plan_line(*self.canvas.line, self.snapshot.stage_um, self.calibration,
                             ROI(*self.canvas.roi), backlash, self.speed_spin.value())
            self.canvas.set_path_preview([
                (self.calibration.pixel_for_stage(s.start_um, self.snapshot.stage_um),
                 self.calibration.pixel_for_stage(s.end_um, self.snapshot.stage_um), s.cut)
                for s in plan.segments
            ])
            self.plan = plan
            self._refresh_live_route()
            self._status("路径预览已生成；运行前仍需检查定位照片与运动状态。")
        except Exception as error:
            self._invalidate_plan()
            self._error(error)
        self._sync_controls()

    @staticmethod
    def _configuration_proxy(snapshot):
        from .axis_calibration import AxisCalibration
        return SimpleNamespace(axis_calibrations={int(k): AxisCalibration(**v)
            for k, v in json.loads(snapshot.axis_configuration).items()})

    def _make_raster_plan(self, backlash):
        if not self._guard():
            return
        snapshot, calibration = self.snapshot, self.calibration
        polygon, roi = tuple(self.canvas.polygon), ROI(*self.canvas.roi)
        settings = self.pattern_panel.settings(self.speed_spin.value())
        qualification, status = self.qualification, self.laser_panel.status
        controller, proxy = self.controller, self._configuration_proxy(snapshot)
        self._invalidate_plan()
        def work():
            snapshot.validate(controller, snapshot.context)
            plan = plan_raster(polygon, snapshot.stage_um, calibration, roi, backlash, settings)
            base_route = build_raster_route(plan, controller=proxy)
            live_route, unavailable = None, None
            if qualification is not None:
                try:
                    if qualification.axis_configuration != snapshot.axis_configuration:
                        raise ValueError("Commissioning configuration changed")
                    qualification.require_laser(status)
                    live_route = build_raster_route(plan, qualification=qualification,
                        controller=proxy, watchdog_ms=status.watchdog_ms)
                except Exception as error:
                    unavailable = str(error)
            return plan, base_route, live_route, unavailable
        def done(result):
            if snapshot is not self.snapshot or calibration != self.calibration or not self._optics_match(snapshot):
                raise ValueError(tr("定位照片或路径已改变，请重新预览并确认。"))
            self.plan, self._raster_route, self._live_route, unavailable = result
            route = self._live_route or self._raster_route
            self._paint_raster_route(route)
            self.pattern_panel.set_summary(self.plan.exposure_count, route.estimated_duration_s)
            self._status("区域路径已生成：{count} 次加工，估计 {seconds:.1f} 秒。", count=self.plan.exposure_count,
                         seconds=route.estimated_duration_s)
            if unavailable:
                self._rt.message(self.live_status_label, "暂不可出光：{error}", error=unavailable)
            self._sync_controls()
        self._launch("raster-plan", work, done)

    def _paint_raster_route(self, route):
        convert = lambda point: self.calibration.pixel_for_stage(point, self.snapshot.stage_um)
        self.canvas.set_path_preview([(convert(a), convert(b), cut) for a, b, cut in route.preview_segments])
        self.canvas.set_exposure_points([convert(point) for point in route.exposure_points])

    def save_recipe(self):
        if not self._guard() or self.snapshot is None:
            return
        try:
            if self.pattern_panel.pattern_mode == "line" or not self.canvas.polygon or self.canvas.roi is None:
                raise ValueError(tr("请先绘制区域并选择扫描模式。"))
            recipe = RasterRecipe(self.snapshot.context, ROI(*self.canvas.roi), tuple(self.canvas.polygon),
                self.pattern_panel.settings(self.speed_spin.value()),
                BacklashSettings(self.x_backlash_spin.value(), self.y_backlash_spin.value(),
                                 self.x_percent_spin.value(), self.y_percent_spin.value()))
        except Exception as error:
            self._error(error)
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("保存加工配方"), "cutting-recipe.json", "JSON (*.json)")
        if filename:
            self._launch("recipe-save", lambda: atomic_write_json(Path(filename), recipe.to_dict()),
                         lambda _: self._status("加工配方已保存：{path}", path=filename))

    def load_recipe(self):
        if not self._guard() or self.snapshot is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("加载加工配方"), "", "JSON (*.json)")
        if filename:
            self._load_recipe_path(Path(filename))

    def _load_recipe_path(self, path):
        if not self._guard() or self.snapshot is None:
            return
        snapshot = self.snapshot
        self._invalidate_plan()
        def work():
            recipe = RasterRecipe.from_json(path.read_text(encoding="utf-8"))
            if recipe.context != snapshot.context:
                raise ValueError(tr("配方的相机或物镜与当前定位照片不匹配。"))
            return recipe
        def done(recipe):
            if snapshot is not self.snapshot or not self._optics_match(snapshot):
                raise ValueError(tr("定位照片或路径已改变，请重新预览并确认。"))
            values = [(self.speed_spin, recipe.settings.speed_um_s),
                      (self.x_backlash_spin, recipe.backlash.x_um), (self.y_backlash_spin, recipe.backlash.y_um),
                      (self.x_percent_spin, recipe.backlash.x_percent), (self.y_percent_spin, recipe.backlash.y_percent)]
            for spin, value in values:
                if (not spin.minimum() <= value <= spin.maximum()
                        or not math.isclose(value, round(value, spin.decimals()), rel_tol=0, abs_tol=1e-10)):
                    raise ValueError("Recipe parameter exceeds UI range or precision")
            recipe.roi.validate_for(snapshot.context)
            polygon = self.canvas.validate_polygon(recipe.polygon_pixels)
            self.pattern_panel.set_settings(recipe.settings)
            for spin, value in values:
                spin.setValue(value)
            roi = recipe.roi
            self.canvas.set_roi((roi.x, roi.y, roi.width, roi.height))
            self.canvas.set_polygon(polygon)
            self._invalidate_plan()
            self._status("配方仅恢复图案和参数；请在新照片上重新对齐样品，再生成预览。")
        self._launch("recipe-load", work, done)

    def _laser_status_changed(self, status):
        if self._job_kind == "live-run" and status.state.name in {"FAULT", "UNKNOWN", "DISCONNECTED", "BOOT_HOLD"}:
            self.abort()
        if not self.busy:
            self._refresh_live_route()
        self._sync_controls()

    def _refresh_live_route(self):
        previous = self._live_route
        self._live_route = None
        if self.plan is None or self.snapshot is None or self.qualification is None:
            return
        try:
            if self.qualification.axis_configuration != self.snapshot.axis_configuration:
                raise ValueError("Commissioning does not match the captured axis configuration")
            status = self.laser_panel.status
            self.qualification.require_laser(status)
            if isinstance(self.plan, RasterPlan):
                # Large routes are generated once on a worker, never from the
                # 250 ms status-monitor callback. New readiness needs re-plan.
                self._live_route = previous
                if previous is None:
                    self._rt.message(self.live_status_label, "请重新生成区域路径以核对出光条件。")
                return
            # UI preview uses immutable captured configuration, never a DLL lock.
            axes = json.loads(self.snapshot.axis_configuration)
            proxy = SimpleNamespace(axis_calibrations={int(k): SimpleNamespace(**v) for k, v in axes.items()})
            route = plan_live_line(self.plan, self.qualification, proxy, status.watchdog_ms)
            points = route.endpoints
            self.canvas.set_path_preview([
                (self.calibration.pixel_for_stage(a, self.snapshot.stage_um),
                 self.calibration.pixel_for_stage(b, self.snapshot.stage_um), index == 3)
                for index, (a, b) in enumerate(zip(points, points[1:]))
            ])
            self._live_route = route
            self._rt.message(self.live_status_label, "已包含关光加减速段；执行前将再次核对联锁和定位状态。")
        except Exception as error:
            if previous is not None:
                self._invalidate_plan()
            self._rt.message(self.live_status_label, "暂不可出光：{error}", error=str(error))

    def export_qualification_template(self):
        if not self._guard():
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("导出硬件调试记录模板"), "laser-commissioning.json", "JSON (*.json)")
        if filename:
            self._launch("commission-template", lambda: atomic_write_json(Path(filename), LiveQualification.template(self.controller)),
                         lambda _: self._status("图像及元数据已保存：{path}", path=filename))

    def load_qualification(self):
        if not self._guard():
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("加载并确认硬件调试记录"), "", "JSON (*.json)")
        if filename:
            self._confirm("确认本次连接的硬件调试", "已实测独立关光联锁、断线关光、运动卡输出极性和触发延迟；记录与当前接线一致。",
                          lambda: self._load_qualification(Path(filename)))

    def _load_qualification(self, path):
        if not self._guard():
            return
        controller = self.controller
        self.qualification = None
        self._invalidate_snapshot("硬件设置已改变，请重新拍摄定位照片。")
        def work():
            from .cutting_session import axis_configuration
            previous = getattr(controller, "qualified_cutting_gate", None)
            if previous is not None:
                controller.configure_cutting_gate(previous, qualified=False)
            qualification = LiveQualification.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if not all((qualification.independent_inhibit_verified, qualification.board_io_verified, qualification.timing_verified)):
                raise ValueError("Incomplete commissioning record")
            if qualification.axis_configuration != axis_configuration(controller) or not controller.cutting_qualified:
                raise ValueError("Motion qualification or axis configuration does not match")
            controller.configure_cutting_gate(qualification.output, qualified=True)
            qualification.require_qualified(controller)
            return qualification
        def done(qualification):
            self.qualification = qualification
            self._invalidate_snapshot("插补确认后请重新拍摄定位照片。")
        self._launch("commission-load", work, done)

    def request_live_run(self):
        if not self._guard() or self.plan is None or self.snapshot is None or self._live_route is None:
            return
        snapshot, plan, qualification = self.snapshot, self.plan, self.qualification
        self._confirm("确认激光切割", "本次操作将真实出光。已核对样品、路径、独立联锁和启动状态，并完成同路径关光空跑。",
                      lambda: self._run_live(snapshot, plan, qualification))

    def _reference_guard(self, snapshot, calibration):
        panel, fixed_positions = self.camera_panel, self._calibration_fixed_positions
        def guard():
            camera = getattr(panel, "controller", None)
            device = getattr(camera, "current_device", None)
            if (getattr(panel, "cutting_reference_revision", 0) != snapshot.envelope.source_revision
                    or getattr(panel, "_current_objective", "") != snapshot.context.objective
                    or getattr(device, "serial_number", "") != snapshot.context.camera_serial
                    or self.calibration != calibration
                    or self._calibration_fixed_positions != fixed_positions
                    or (self._latest_context is not None and self._latest_context != snapshot.context)):
                raise ValueError("Camera reference or calibration changed during cutting")
        return guard

    def _run_live(self, snapshot, plan, qualification):
        if isinstance(plan, RasterPlan):
            self._run_raster(snapshot, plan, qualification)
            return
        if not self._guard():
            return
        try:
            if (snapshot is not self.snapshot or plan is not self.plan or qualification is not self.qualification
                    or not self._optics_match(snapshot) or not self.laser_panel.startup_confirmed):
                raise ValueError(tr("定位照片或路径已改变，请重新预览并确认。"))
            if not _fixed_axes_match(self._calibration_fixed_positions, snapshot):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            plan.validate_for(self.calibration, snapshot.context, snapshot.stage_um)
        except Exception as error:
            self._error(error)
            return
        runner = LiveCuttingRunner(self.controller, self.laser_panel.controller)
        self._runner = runner
        calibration, guard = self.calibration, self._reference_guard(snapshot, self.calibration)
        generation, signals = self._generation + 1, self._signals
        def live_guard():
            guard()
            if qualification is not self.qualification or not self.laser_panel.startup_confirmed:
                raise ValueError("Laser startup or commissioning state changed")
        self._status("正在执行激光切割；停止键始终可用。")
        self._launch("live-run", lambda: runner.run(plan, snapshot, snapshot.context, qualification,
                     calibration=calibration, startup_confirmed=True, reference_guard=live_guard,
                     progress=lambda step, total: signals.progress.emit(generation, step, total)),
                     lambda _: self._status("切割完成；请重新拍摄定位照片。"), cancel=runner.cancel)

    def _confirm(self, title, statement, accepted):
        if self._confirmation is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(400)
        self._rt.title(dialog, title)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._label(statement))
        check = QCheckBox()
        check.setObjectName("confirmationCheck")
        self._rt.text(check, "我已完成上述检查")
        layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._rt.text(buttons.button(QDialogButtonBox.StandardButton.Ok), "确认")
        self._rt.text(buttons.button(QDialogButtonBox.StandardButton.Cancel), "取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        check.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._confirmation = dialog

        def finished(result):
            self._confirmation = None
            if result == QDialog.DialogCode.Accepted and check.isChecked() and not self._closing:
                accepted()
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    def qualify_controller(self):
        if not self._guard():
            return
        controller = self.controller

        def check_connection():
            if not controller.is_open:
                raise ValueError(tr("运动控制未打开，无法确认插补调试。"))
            return controller.motion_revision

        def confirm_connection(revision):
            self._confirm("确认本次连接的插补调试", "已在关激光条件下验证 X3/Y3 插补、方向、单位、限位和停止",
                          lambda: self._qualify(controller, revision))

        self._launch("qualification-check", check_connection, confirm_connection)

    def _qualify(self, controller, revision):
        if not self._guard():
            return
        self._invalidate_snapshot("插补确认后请重新拍摄定位照片。")

        def qualify():
            if controller is not self.controller or not controller.is_open or controller.motion_revision != revision:
                raise ValueError(tr("控制器状态已改变，请重新确认插补调试。"))
            controller.set_cutting_qualified(True)

        self._launch("qualify", qualify,
                     lambda _: self._status("本次连接已确认插补调试；复位、重连或轴配置变化后需重新确认。"))

    def request_run(self):
        if not self._guard() or self.plan is None or self.snapshot is None:
            return
        snapshot, plan = self.snapshot, self.plan
        self._confirm("每次暗运行前确认", "本次运行前，我已将激光电源关闭或物理遮挡光路。软件不验证实际出光状态。",
                      lambda: self._run(snapshot, plan))

    def _run(self, snapshot, plan):
        if isinstance(plan, RasterPlan):
            self._run_raster(snapshot, plan)
            return
        if not self._guard():
            return
        try:
            if snapshot is not self.snapshot or plan is not self.plan or not self._optics_match(snapshot):
                raise ValueError(tr("定位照片或路径已改变，请重新预览并确认。"))
            if not _fixed_axes_match(self._calibration_fixed_positions, snapshot):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            plan.validate_for(self.calibration, snapshot.context, snapshot.stage_um)
        except Exception as error:
            self._error(error)
            return
        runner = DarkCuttingRunner(self.controller)
        self._runner = runner
        rehearsal_path = self._live_route.endpoints if self._live_route is not None else None
        generation, signals = self._generation + 1, self._signals
        calibration = self.calibration
        reference_guard = self._reference_guard(snapshot, calibration)

        self._status("正在暗运行；激光必须保持关闭或遮挡。")
        laser = self.laser_panel.controller
        def dark_run():
            if laser is not None and laser.connected:
                status = laser.off()
                if status.state.name != "DISARMED" or status.comp_high is not False:
                    raise ValueError("Laser OFF acknowledgement unconfirmed")
            runner.run(plan, snapshot, snapshot.context,
                       lambda step, total: signals.progress.emit(generation, step, total),
                       calibration=calibration, reference_guard=reference_guard, path_endpoints=rehearsal_path)
        self._launch("run", dark_run,
                     lambda _: self._status("暗运行完成；请重新拍摄定位照片。"), cancel=runner.cancel)

    def _run_raster(self, snapshot, plan, qualification=None):
        if not self._guard():
            return
        live = qualification is not None
        try:
            if snapshot is not self.snapshot or plan is not self.plan or not self._optics_match(snapshot):
                raise ValueError(tr("定位照片或路径已改变，请重新预览并确认。"))
            if not _fixed_axes_match(self._calibration_fixed_positions, snapshot):
                raise ValueError(tr("固定轴位置缺失或已改变，请重新标定。"))
            if live and (qualification is not self.qualification or not self.laser_panel.startup_confirmed):
                raise ValueError("Laser startup or commissioning changed")
            route = self._live_route if live else (self._live_route or self._raster_route)
            if route is None:
                raise ValueError(tr("请重新生成区域路径以核对出光条件。"))
        except Exception as error:
            self._error(error)
            return
        runner = RasterRunner(self.controller, self.laser_panel.controller)
        self._runner = runner
        calibration, guard = self.calibration, self._reference_guard(snapshot, self.calibration)
        generation, signals = self._generation + 1, self._signals
        def reference_guard():
            guard()
            if live and (qualification is not self.qualification or not self.laser_panel.startup_confirmed):
                raise ValueError("Laser startup or commissioning changed")
        self._status("正在执行激光切割；停止键始终可用。" if live
                     else "正在暗运行；激光必须保持关闭或遮挡。")
        self._launch("live-run" if live else "run", lambda: runner.run(plan, snapshot, snapshot.context,
                     progress=lambda step, total: signals.progress.emit(generation, step, total),
                     calibration=calibration, route=route, qualification=qualification,
                     startup_confirmed=live, reference_guard=reference_guard),
                     lambda _: self._status("切割完成；请重新拍摄定位照片。" if live
                                            else "暗运行完成；请重新拍摄定位照片。"), cancel=runner.cancel)

    def abort(self):
        if not self._closing:
            self.laser_panel.emergency_off()
        if self._confirmation is not None:
            self._confirmation.reject()
        if self.busy:
            self._cancel.set()
            if self._runner is not None:
                self._runner.abort()
            self._status("正在取消，等待任务退出…")

    def retry_cleanup(self):
        if self.busy or self._closing or self.laser_panel.busy or self._failed_runner is None:
            return
        runner = self._failed_runner
        self._runner = runner
        def done(_):
            if runner.cleanup_required:
                raise ValueError("Cleanup remains unconfirmed")
            self._failed_runner = None
            self._invalidate_snapshot("运行结束后需重新拍摄定位照片。")
        self._launch("retry-cleanup", runner.retry_cleanup, done)

    def prepare_for_close(self, timeout_ms=5000) -> bool:
        self._closing = True
        self.live_view.reset()
        self.frames.close()
        self.abort()
        laser_done = self.laser_panel.prepare_for_close(timeout_ms)
        self._drain_workers()
        if self._thread is not None or self._probe_thread is not None or not laser_done:
            if self._close_deadline is None:
                self._close_deadline = time.monotonic() + max(0, timeout_ms) / 1000
            self._sync_controls()
            return False
        self._sync_controls()
        return True

    def closeEvent(self, event):  # noqa: N802
        if not self.prepare_for_close():
            self._close_when_idle = True
            event.ignore()
            return
        self._timer.stop()
        event.accept()

    def showEvent(self, event):  # noqa: N802
        if self._closing and not self.busy and self._probe_thread is None and self.laser_panel.reopen():
            self._closing = False
            self._close_deadline = None
            self.frames = SharedCuttingFrames()
            self._timer.start()
            self._check_optics()
            self._sync_controls()
        super().showEvent(event)
