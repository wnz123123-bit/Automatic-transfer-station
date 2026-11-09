from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import replace
from datetime import datetime
import json
import logging
import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QDeadlineTimer, QElapsedTimer, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .camera_controller import (
    CameraAcquisitionSettings,
    CameraCapabilities,
    DEFAULT_CAMERA_GRAB_TIMEOUT_MS,
    DEFAULT_CAMERA_SERIAL,
    BaslerCameraController,
    CameraError,
    CameraFrame,
)
from .camera_image import draw_measurement_overlay, draw_scale_bar, flip_frame, qimage_from_frame
from .camera_measurement import (
    MeasurementObject,
    evaluate_measurement,
    measurement_sidecar_payload,
    normalized_points,
)
from .camera_settings import (
    DEFAULT_FILENAME_TEMPLATE,
    DEFAULT_OBJECTIVE,
    OBJECTIVE_LABELS,
    CameraSettings,
    CameraSettingsStore,
    ScaleCalibrationStore,
    build_snapshot_path,
)
from .paths import CAMERA_SCALE_CALIBRATION_PATH, CAMERA_SETTINGS_PATH, CAMERA_SNAPSHOT_DIR


_DETACHED_CAMERA_THREADS: list[QThread] = []
PREVIEW_STOP_TIMEOUT_MS = DEFAULT_CAMERA_GRAB_TIMEOUT_MS + 1000
DEFAULT_CAMERA_PREVIEW_FPS = 60
DEFAULT_CAMERA_PREVIEW_INTERVAL_MS = 1000.0 / DEFAULT_CAMERA_PREVIEW_FPS
DEFAULT_CAMERA_RENDER_FPS = 30
DEFAULT_CAMERA_RENDER_INTERVAL_MS = 1000.0 / DEFAULT_CAMERA_RENDER_FPS
DEFAULT_CAMERA_EXPOSURE_MS = 10.0
_IMAGE_ZOOM_STEP = 1.25
_MIN_IMAGE_ZOOM = 0.25
_MAX_IMAGE_ZOOM = 8.0


class CameraTaskThread(QThread):
    succeeded = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self, kind: str, action: Callable[[], object], parent=None):
        super().__init__(parent)
        self._kind = kind
        self._action = action

    def run(self) -> None:
        try:
            result = self._action()
        except Exception as exc:  # noqa: BLE001 - normalize worker failures
            self.failed.emit(self._kind, str(exc))
            return
        self.succeeded.emit(self._kind, result)


class ThreadedCameraExecutor(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._threads: set[CameraTaskThread] = set()
        self._detached_threads: list[CameraTaskThread] = []

    def submit(self, kind, action, on_done, on_error) -> None:
        thread = CameraTaskThread(kind, action, self)
        self._threads.add(thread)
        thread.succeeded.connect(on_done)
        thread.failed.connect(on_error)
        thread.finished.connect(lambda t=thread: self._cleanup(t))
        thread.start()

    def _cleanup(self, thread: CameraTaskThread) -> None:
        self._threads.discard(thread)
        if thread in self._detached_threads:
            self._detached_threads.remove(thread)
        if thread in _DETACHED_CAMERA_THREADS:
            _DETACHED_CAMERA_THREADS.remove(thread)
        thread.deleteLater()

    def wait_for_idle(self, timeout_ms: int = 5000) -> None:
        deadline = QDeadlineTimer(max(0, int(timeout_ms)))
        while True:
            pending = [thread for thread in list(self._threads) if thread.isRunning()]
            if not pending:
                return
            remaining = deadline.remainingTime()
            if remaining == 0:
                return
            for thread in pending:
                thread.wait(remaining if remaining > 0 else 0)
                if deadline.hasExpired():
                    return

    def is_busy(self) -> bool:
        return any(thread.isRunning() for thread in self._threads)

    def detach_running_threads(self) -> None:
        for thread in list(self._threads):
            if not thread.isRunning() or thread in self._detached_threads:
                continue
            for signal in (thread.succeeded, thread.failed, thread.finished):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            try:
                thread.setParent(None)
            except Exception:
                pass
            try:
                thread.quit()
            except Exception:
                pass
            self._detached_threads.append(thread)
            _DETACHED_CAMERA_THREADS.append(thread)


class InlineCameraExecutor:
    def submit(self, kind, action, on_done, on_error) -> None:
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001
            on_error(kind, str(exc))
            return
        on_done(kind, result)

    def wait_for_idle(self, timeout_ms: int = 5000) -> None:
        return None

    def is_busy(self) -> bool:
        return False


class CameraPreviewThread(QThread):
    frame_ready = Signal(object, float)
    failed = Signal(str)

    def __init__(
        self,
        controller,
        parent=None,
        timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS,
        frame_interval_ms: int = DEFAULT_CAMERA_PREVIEW_INTERVAL_MS,
        emit_interval_ms: float = DEFAULT_CAMERA_RENDER_INTERVAL_MS,
        context_provider=None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.timeout_ms = int(timeout_ms)
        self.frame_interval_ms = max(0.0, float(frame_interval_ms))
        self.emit_interval_ms = max(0.0, float(emit_interval_ms))
        self._stop_requested = False
        self.context_provider = context_provider

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            self.controller.start_preview()
            preview_timer = QElapsedTimer()
            preview_timer.start()
            last_frame_ms: int | None = None
            last_emit_ms: int | None = None
            while not self._stop_requested:
                frame_timer = QElapsedTimer()
                frame_timer.start()
                capture_started = time.monotonic()
                capture_context = self.context_provider() if self.context_provider else None
                frame = self.controller.grab_one(timeout_ms=self.timeout_ms)
                if self.context_provider and capture_context != self.context_provider():
                    continue
                frame = replace(frame, metadata={
                    **frame.metadata,
                    "capture_started_monotonic": capture_started,
                    "capture_finished_monotonic": time.monotonic(),
                    "capture_context": capture_context,
                })
                current_ms = int(preview_timer.elapsed())
                fps = self._frame_rate(last_frame_ms, current_ms)
                last_frame_ms = current_ms
                if self._should_emit_frame(last_emit_ms, current_ms):
                    last_emit_ms = current_ms
                    self.frame_ready.emit(frame, fps)
                self._wait_for_next_frame(frame_timer)
        except Exception as exc:  # noqa: BLE001
            if not self._stop_requested:
                self.failed.emit(str(exc))
        finally:
            try:
                self.controller.stop_preview()
            except Exception as exc:  # noqa: BLE001
                if not self._stop_requested:
                    self.failed.emit(str(exc))

    def _frame_rate(self, previous_ms: int | None, current_ms: int) -> float:
        if previous_ms is None:
            return 0.0
        elapsed_ms = max(1, current_ms - previous_ms)
        return 1000.0 / elapsed_ms

    def _should_emit_frame(self, previous_emit_ms: int | None, current_ms: int) -> bool:
        if previous_emit_ms is None:
            return True
        return current_ms - previous_emit_ms >= self.emit_interval_ms

    def _wait_for_next_frame(self, frame_timer: QElapsedTimer) -> None:
        remaining_ms = self.frame_interval_ms - float(frame_timer.elapsed())
        while remaining_ms > 0 and not self._stop_requested:
            sleep_ms = max(1, min(int(remaining_ms), 20))
            self.msleep(sleep_ms)
            remaining_ms = self.frame_interval_ms - float(frame_timer.elapsed())


class CameraImageLabel(QLabel):
    image_clicked = Signal(float, float)
    image_hovered = Signal(float, float)
    zoom_delta_requested = Signal(int)

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._source_width = 0
        self._source_height = 0
        self.setMouseTracking(True)

    def set_source_size(self, width: int, height: int) -> None:
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))

    def source_size(self) -> tuple[int, int]:
        return self._source_width, self._source_height

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        point = self._source_point_from_event(event)
        if point is not None:
            self.image_clicked.emit(point[0], point[1])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        point = self._source_point_from_event(event)
        if point is not None:
            self.image_hovered.emit(point[0], point[1])
        super().mouseMoveEvent(event)

    def _source_point_from_event(self, event) -> tuple[float, float] | None:
        pixmap = self.pixmap()
        if (
            pixmap is not None
            and not pixmap.isNull()
            and self._source_width > 0
            and self._source_height > 0
        ):
            position = event.position() if hasattr(event, "position") else event.pos()
            display_width = pixmap.width()
            display_height = pixmap.height()
            offset_x = (self.width() - display_width) / 2.0
            offset_y = (self.height() - display_height) / 2.0
            x = position.x() - offset_x
            y = position.y() - offset_y
            if 0 <= x <= display_width and 0 <= y <= display_height:
                source_x = x * self._source_width / max(1, display_width)
                source_y = y * self._source_height / max(1, display_height)
                return source_x, source_y
        return None

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        if delta:
            self.zoom_delta_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class CameraPanel(QWidget):
    raw_frame_ready = Signal(object)

    def __init__(
        self,
        register: Callable[[str, object], object] | None = None,
        controller_factory: Callable[[], object] | None = None,
        task_executor: object | None = None,
        snapshot_dir: str | Path = CAMERA_SNAPSHOT_DIR,
        settings_path: str | Path = CAMERA_SETTINGS_PATH,
        calibration_path: str | Path = CAMERA_SCALE_CALIBRATION_PATH,
    ):
        super().__init__()
        self._external_register = register
        self.controls: dict[str, object] = {}
        self.controller_factory = controller_factory or BaslerCameraController
        self.controller = None
        self.settings_store = CameraSettingsStore(settings_path, default_save_dir=snapshot_dir)
        self.settings = self.settings_store.load()
        self.snapshot_dir = Path(self.settings.save_dir)
        self.scale_store = ScaleCalibrationStore(calibration_path)
        self.scale_calibrations = self.scale_store.load()
        self._task_executor = task_executor if task_executor is not None else ThreadedCameraExecutor(self)
        self._preview_thread: CameraPreviewThread | None = None
        self._detached_preview_threads: list[CameraPreviewThread] = []
        self._displayed_frame: CameraFrame | None = None
        self._last_frame: CameraFrame | None = None
        self._pending_frame: CameraFrame | None = None
        self._preview_fps: float | None = None
        self._frame_render_scheduled = False
        self._render_clock = QElapsedTimer()
        self._render_clock.start()
        self._last_render_ms: int | None = None
        self._image_zoom = 1.0
        self._measurements: list[MeasurementObject] = []
        self._active_measurement_points: list[tuple[float, float]] = []
        self._active_measurement_preview_point: tuple[float, float] | None = None
        self._measurement_mode: str | None = None
        self._next_measurement_id = 1
        self._overlay_refresh_scheduled = False
        self._capabilities: CameraCapabilities | None = None
        self._current_objective = self.settings.objective or DEFAULT_OBJECTIVE
        self.cutting_reference_revision = 0
        self._calibration_active = False
        self._calibration_known_um = 100.0
        self._calibration_points: list[tuple[float, float]] = []
        self._scale_dialog: QDialog | None = None
        self._closing = False
        self._close_result: Future[bool] | None = None
        from .i18n import translator, Retranslator, tr
        self._tr = tr
        self._rt = Retranslator(translator)

        self.setObjectName("cameraPanel")
        self._build_ui()
        self._sync_connection_state(False)
        self.connect_camera()

    def _register(self, name: str, widget):
        widget.setObjectName(name)
        self.controls[name] = widget
        if self._external_register is not None:
            self._external_register(name, widget)
        return widget

    def _controller(self):
        if self.controller is None:
            self.controller = self.controller_factory()
        return self.controller

    def _translated_label(self, source: str) -> QLabel:
        label = QLabel()
        self._rt.text(label, source)
        return label

    def _build_ui(self) -> None:
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        workspace = self._register("cameraWorkspace", QWidget())
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(10)
        root.addWidget(workspace)

        preview_pane = self._register("cameraPreviewPane", QFrame())
        preview_pane.setFrameShape(QFrame.StyledPanel)
        preview_layout = QVBoxLayout(preview_pane)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        preview_layout.setSpacing(8)

        header = QGridLayout()
        title = self._register("cameraTitleLabel", QLabel())
        self._rt.text(title, "相机预览")
        title.setStyleSheet("color:#d8e2ee;font-size:16px;font-weight:800;")
        header.addWidget(title, 0, 0, 1, 2)
        fps_label = self._register("cameraFpsLabel", QLabel(self._preview_fps_text()))
        fps_label.setProperty("readout", True)
        fps_label.setWordWrap(True)
        header.addWidget(fps_label, 1, 0)
        zoom_label = self._register("cameraZoomLabel", QLabel(self._image_zoom_text()))
        zoom_label.setProperty("readout", True)
        zoom_label.setWordWrap(True)
        self._rt.tooltip(zoom_label, "鼠标滚轮缩放预览图像")
        header.addWidget(zoom_label, 1, 1)
        device_label = self._register("cameraDeviceLabel", QLabel())
        self._rt.message(device_label, "相机: 未连接")
        device_label.setProperty("readout", True)
        device_label.setWordWrap(True)
        header.addWidget(device_label, 2, 0, 1, 2)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(1, 1)
        preview_layout.addLayout(header)

        image = self._register("cameraImageLabel", CameraImageLabel(self._tr("无图像")))
        image.setAlignment(Qt.AlignCenter)
        image.setMinimumSize(280, 240)
        image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        image.setFrameShape(QFrame.NoFrame)
        image.setStyleSheet(
            "QLabel#cameraImageLabel{background:#071018;color:#d8e2ee;border:1px solid #253546;"
            "border-radius:6px;font-weight:700;}"
        )
        image.image_clicked.connect(self._handle_image_click)
        image.image_hovered.connect(self._handle_image_hover)
        image.zoom_delta_requested.connect(self._adjust_image_zoom)
        preview_layout.addWidget(image, 1)

        overlay = self._register("cameraOverlayStatusLabel", QLabel())
        overlay.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        overlay.setWordWrap(True)
        overlay.setStyleSheet("color:#b6c6d8;font-weight:700;")
        preview_layout.addWidget(overlay)

        status = self._register("cameraStatusLabel", QLabel(self._tr("Basler 相机未连接")))
        status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        status.setWordWrap(True)
        status.setStyleSheet("color:#7dd3fc;font-weight:700;")
        preview_layout.addWidget(status)

        control_scroll = self._register("cameraControlScroll", QScrollArea())
        control_scroll.setWidgetResizable(True)
        control_scroll.setFrameShape(QFrame.NoFrame)
        control_scroll.setMinimumWidth(360)
        control_scroll.setMaximumWidth(420)
        control_pane = self._register("cameraControlPane", QWidget())
        control_layout = QVBoxLayout(control_pane)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(8)
        control_scroll.setWidget(control_pane)

        acquire_group = self._register("cameraAcquireGroup", QGroupBox())
        self._rt.title(acquire_group, "采集")
        acquire_layout = QGridLayout(acquire_group)
        preview = self._register("cameraPreview", QPushButton())
        snapshot = self._register("cameraSnapshot", QPushButton())
        self._rt.custom(lambda: preview.setText(self._tr("停止预览" if self._preview_thread else "预览")))
        self._rt.text(snapshot, "拍照")
        preview.clicked.connect(self.toggle_preview)
        snapshot.clicked.connect(self.take_snapshot)
        acquire_layout.addWidget(preview, 0, 0)
        acquire_layout.addWidget(snapshot, 0, 1)
        control_layout.addWidget(acquire_group)

        save_group = self._register("cameraSaveGroup", QGroupBox())
        self._rt.title(save_group, "保存")
        save_layout = QGridLayout(save_group)
        save_dir = self._register("cameraSaveDirEdit", QLineEdit(str(self.snapshot_dir)))
        browse = self._register("cameraBrowseSaveDir", QPushButton())
        self._rt.text(browse, "浏览")
        template = self._register("cameraSaveTemplateEdit", QLineEdit(self.settings.filename_template or DEFAULT_FILENAME_TEMPLATE))
        fmt = self._register("cameraFormatCombo", QComboBox())
        fmt.addItems(["TIFF", "PNG", "JPG"])
        fmt.setCurrentText(self.settings.image_format if self.settings.image_format in {"TIFF", "PNG", "JPG"} else "TIFF")
        quality = self._register("cameraJpegQualitySpin", QSpinBox())
        quality.setRange(1, 100)
        quality.setValue(int(self.settings.jpeg_quality))
        browse.clicked.connect(self.browse_save_dir)
        save_layout.addWidget(self._translated_label("目录"), 0, 0)
        save_layout.addWidget(save_dir, 0, 1)
        save_layout.addWidget(browse, 0, 2)
        save_layout.addWidget(self._translated_label("文件名"), 1, 0)
        save_layout.addWidget(template, 1, 1, 1, 2)
        save_layout.addWidget(self._translated_label("格式"), 2, 0)
        save_layout.addWidget(fmt, 2, 1)
        save_layout.addWidget(quality, 2, 2)
        control_layout.addWidget(save_group)

        quality_group = self._register("cameraQualityGroup", QGroupBox())
        self._rt.title(quality_group, "清晰度")
        quality_layout = QFormLayout(quality_group)
        live_resolution = self._register("cameraLiveResolutionCombo", QComboBox())
        snap_resolution = self._register("cameraSnapResolutionCombo", QComboBox())
        self._populate_resolution_combos()
        live_resolution.currentTextChanged.connect(lambda _text: self._clear_measurements_for_coordinate_change())
        live_resolution.currentTextChanged.connect(self._invalidate_cutting_reference)
        quality_layout.addRow(self._translated_label("Live 像素"), live_resolution)
        quality_layout.addRow(self._translated_label("Snap 像素"), snap_resolution)
        control_layout.addWidget(quality_group)

        exposure_group = self._register("cameraExposureGroup", QGroupBox())
        self._rt.title(exposure_group, "曝光/增益")
        exposure_layout = QGridLayout(exposure_group)
        exposure_auto = self._register("cameraExposureAuto", QCheckBox())
        self._rt.text(exposure_auto, "自动曝光")
        exposure_auto.setChecked(False)
        exposure_spin = self._register("cameraExposureSpin", QDoubleSpinBox())
        exposure_spin.setRange(0.01, 100000.0)
        exposure_spin.setDecimals(2)
        exposure_spin.setValue(DEFAULT_CAMERA_EXPOSURE_MS)
        exposure_spin.setSuffix(" ms")
        exposure_spin.setEnabled(True)
        gain_auto = self._register("cameraGainAuto", QCheckBox())
        self._rt.text(gain_auto, "自动增益")
        gain_auto.setChecked(True)
        gain_spin = self._register("cameraGainSpin", QDoubleSpinBox())
        gain_spin.setRange(0.0, 48.0)
        gain_spin.setDecimals(2)
        gain_spin.setValue(0.0)
        gain_spin.setEnabled(False)
        gamma_spin = self._register("cameraGammaSpin", QDoubleSpinBox())
        gamma_spin.setRange(0.25, 2.0)
        gamma_spin.setDecimals(2)
        gamma_spin.setSingleStep(0.05)
        gamma_spin.setValue(float(self.settings.gamma))
        sharpness_spin = self._register("cameraSharpnessSpin", QDoubleSpinBox())
        sharpness_spin.setRange(0.0, 1.0)
        sharpness_spin.setDecimals(2)
        sharpness_spin.setSingleStep(0.05)
        sharpness_spin.setValue(float(self.settings.sharpness))
        exposure_auto.toggled.connect(self._on_exposure_auto_changed)
        exposure_spin.editingFinished.connect(self.apply_exposure)
        gain_auto.toggled.connect(self._on_gain_auto_changed)
        gain_spin.editingFinished.connect(self.apply_gain)
        gamma_spin.editingFinished.connect(self.apply_image_adjustment)
        sharpness_spin.editingFinished.connect(self.apply_image_adjustment)
        exposure_layout.addWidget(exposure_auto, 0, 0)
        exposure_layout.addWidget(exposure_spin, 0, 1)
        exposure_layout.addWidget(gain_auto, 1, 0)
        exposure_layout.addWidget(gain_spin, 1, 1)
        exposure_layout.addWidget(QLabel("Gamma"), 2, 0)
        exposure_layout.addWidget(gamma_spin, 2, 1)
        exposure_layout.addWidget(self._translated_label("锐化"), 3, 0)
        exposure_layout.addWidget(sharpness_spin, 3, 1)
        control_layout.addWidget(exposure_group)

        scale_group = self._register("cameraScaleGroup", QGroupBox())
        self._rt.title(scale_group, "比例尺")
        scale_layout = QGridLayout(scale_group)
        objective_combo = self._register("cameraObjectiveCombo", QComboBox())
        objective_combo.addItems(list(OBJECTIVE_LABELS))
        objective_combo.setCurrentText(self._current_objective)
        calibrate = self._register("cameraOpenScaleCalibration", QPushButton())
        self._rt.text(calibrate, "标定...")
        scale_status = self._register("cameraScaleStatusLabel", QLabel(""))
        scale_status.setWordWrap(True)
        objective_combo.currentTextChanged.connect(self.set_objective)
        calibrate.clicked.connect(self.open_scale_calibration_dialog)
        scale_layout.addWidget(self._translated_label("倍率"), 0, 0)
        scale_layout.addWidget(objective_combo, 0, 1)
        scale_layout.addWidget(calibrate, 1, 0, 1, 2)
        scale_layout.addWidget(scale_status, 2, 0, 1, 2)
        control_layout.addWidget(scale_group)

        display_group = self._register("cameraDisplayGroup", QGroupBox())
        self._rt.title(display_group, "显示")
        display_layout = QVBoxLayout(display_group)
        flip_x = self._register("cameraFlipHorizontal", QCheckBox())
        self._rt.text(flip_x, "水平翻转")
        flip_y = self._register("cameraFlipVertical", QCheckBox())
        self._rt.text(flip_y, "垂直翻转")
        flip_x.setChecked(bool(self.settings.flip_horizontal))
        flip_y.setChecked(bool(self.settings.flip_vertical))
        flip_x.toggled.connect(lambda _checked: self._on_display_transform_changed())
        flip_y.toggled.connect(lambda _checked: self._on_display_transform_changed())
        display_layout.addWidget(flip_x)
        display_layout.addWidget(flip_y)

        measurement_group = self._register("cameraMeasurementGroup", QGroupBox())
        self._rt.title(measurement_group, "测量")
        measurement_layout = QGridLayout(measurement_group)
        crosshair = self._register("cameraCrosshairToggle", QCheckBox())
        self._rt.text(crosshair, "中心十字线")
        crosshair.setChecked(False)
        line_button = self._register("cameraMeasureLine", QPushButton())
        self._rt.text(line_button, "直线距离")
        polygon_button = self._register("cameraMeasurePolygon", QPushButton())
        self._rt.text(polygon_button, "面积")
        finish_polygon = self._register("cameraFinishPolygon", QPushButton())
        self._rt.text(finish_polygon, "完成面积")
        undo_measurement = self._register("cameraUndoMeasurement", QPushButton())
        self._rt.text(undo_measurement, "撤销")
        clear_measurements = self._register("cameraClearMeasurements", QPushButton())
        self._rt.text(clear_measurements, "清空")
        include_annotations = self._register("cameraIncludeAnnotations", QCheckBox())
        self._rt.text(include_annotations, "截图包含标注")
        include_annotations.setChecked(True)
        measurement_status = self._register("cameraMeasurementStatusLabel", QLabel())
        measurement_status.setWordWrap(True)
        line_button.setCheckable(True)
        polygon_button.setCheckable(True)
        crosshair.toggled.connect(lambda _checked: self._on_crosshair_toggled())
        line_button.clicked.connect(lambda: self._set_measurement_mode("line"))
        polygon_button.clicked.connect(lambda: self._set_measurement_mode("polygon"))
        finish_polygon.clicked.connect(self.finish_polygon_measurement)
        undo_measurement.clicked.connect(self.undo_measurement)
        clear_measurements.clicked.connect(self.clear_measurements)
        measurement_layout.addWidget(crosshair, 0, 0, 1, 2)
        measurement_layout.addWidget(line_button, 1, 0)
        measurement_layout.addWidget(polygon_button, 1, 1)
        measurement_layout.addWidget(finish_polygon, 2, 0)
        measurement_layout.addWidget(undo_measurement, 2, 1)
        measurement_layout.addWidget(clear_measurements, 3, 0)
        measurement_layout.addWidget(include_annotations, 4, 0, 1, 2)
        measurement_layout.addWidget(measurement_status, 5, 0, 1, 2)
        control_layout.addWidget(measurement_group)

        results_group = self._register("cameraMeasurementResultsGroup", QGroupBox())
        self._rt.title(results_group, "测量结果")
        results_layout = QVBoxLayout(results_group)
        results_table = self._register("cameraMeasurementTable", QTableWidget(0, 4))
        self._rt.custom(lambda: results_table.setHorizontalHeaderLabels(
            [self._tr(key) for key in ("ID", "类型", "结果", "备注")]
        ))
        self._rt.custom(results_table.resizeColumnsToContents)
        results_table.setSelectionBehavior(QTableWidget.SelectRows)
        results_table.setSelectionMode(QTableWidget.SingleSelection)
        results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        results_table.setMinimumHeight(120)
        results_table.horizontalHeader().setStretchLastSection(True)
        delete_measurement = self._register("cameraDeleteMeasurement", QPushButton())
        self._rt.text(delete_measurement, "删除选中")
        delete_measurement.clicked.connect(self.delete_selected_measurement)
        results_layout.addWidget(results_table)
        results_layout.addWidget(delete_measurement)
        control_layout.addWidget(results_group)

        control_layout.addWidget(display_group)
        control_layout.addStretch(1)

        workspace_layout.addWidget(preview_pane, 1)
        workspace_layout.addWidget(control_scroll)

        self.setStyleSheet(
            """
            #cameraPanel {
                background: #0e1720;
                font-weight: 700;
            }
            #cameraPreviewPane {
                background: #0b1220;
                border: 1px solid #243447;
                border-radius: 8px;
            }
            #cameraControlPane QGroupBox {
                border: 1px solid #d9e2ec;
                border-radius: 8px;
                margin-top: 9px;
                padding: 8px;
                background: #ffffff;
                color: #223044;
                font-weight: 800;
            }
            #cameraControlPane QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            #cameraPanel QPushButton {
                min-height: 28px;
                border: 1px solid #cbd6e2;
                border-radius: 6px;
                background: #edf2f7;
                color: #223044;
                font-weight: 800;
                padding: 3px 8px;
            }
            #cameraPanel QPushButton:pressed {
                background: #d3deea;
                border-color: #90a4ba;
                padding-top: 4px;
                padding-bottom: 2px;
            }
            #cameraPanel QPushButton#cameraPreview,
            #cameraPanel QPushButton#cameraSnapshot {
                background: #1f6f5b;
                border-color: #1f6f5b;
                color: #ffffff;
            }
            #cameraPanel QLabel[readout="true"] {
                color: #d8e2ee;
                background: #172338;
                border-radius: 6px;
                padding: 5px 7px;
                font-weight: 700;
            }
            """
        )
        self._set_measurement_status("测量: 就绪")
        self._rt.custom(self._refresh_readout_language)

    def _refresh_readout_language(self) -> None:
        self._set_preview_fps(self._preview_fps)
        self._update_image_zoom_label()
        self._update_scale_status()
        if self._displayed_frame is None:
            self.controls["cameraImageLabel"].setText(self._tr("无图像"))
        table = self.controls["cameraMeasurementTable"]
        for row, measurement in enumerate(self._measurements):
            item = table.item(row, 3)
            if item is not None:
                item.setText(self._measurement_note(measurement))

    def _on_exposure_auto_changed(self, checked: bool) -> None:
        self.controls["cameraExposureSpin"].setEnabled(not checked)
        if self._is_connected():
            self.apply_exposure()

    def _on_gain_auto_changed(self, checked: bool) -> None:
        self.controls["cameraGainSpin"].setEnabled(not checked)
        if self._is_connected():
            self.apply_gain()

    def browse_save_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, self._tr("选择保存目录"), self.controls["cameraSaveDirEdit"].text())
        if directory:
            self.controls["cameraSaveDirEdit"].setText(directory)
            self._save_settings()

    def create_scale_calibration_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setObjectName("cameraScaleCalibrationDialog")
        self._rt.title(dialog, "比例尺标定")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form = QFormLayout()
        objective = QLabel(self._current_objective)
        objective.setObjectName("cameraCalibrationObjectiveLabel")
        length_spin = self._register("cameraCalibrationLengthSpin", QDoubleSpinBox())
        length_spin.setRange(0.001, 1000000.0)
        length_spin.setDecimals(3)
        length_spin.setValue(float(self._calibration_known_um))
        length_spin.setSuffix(" um")
        form.addRow(self._translated_label("当前倍率"), objective)
        form.addRow(self._translated_label("已知长度"), length_spin)
        layout.addLayout(form)

        start = self._register("cameraStartScaleCalibration", QPushButton())
        self._rt.text(start, "开始两点标定")
        close = QPushButton()
        self._rt.text(close, "关闭")
        start.clicked.connect(lambda: self._start_scale_calibration_from_dialog(dialog))
        close.clicked.connect(dialog.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(start)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        return dialog

    def open_scale_calibration_dialog(self) -> None:
        dialog = self.create_scale_calibration_dialog()
        self._scale_dialog = dialog
        dialog.finished.connect(lambda _result, d=dialog: self._clear_scale_dialog(d))
        dialog.open()

    def _clear_scale_dialog(self, dialog: QDialog) -> None:
        if self._scale_dialog is dialog:
            self._scale_dialog = None

    def _start_scale_calibration_from_dialog(self, dialog: QDialog) -> None:
        self.start_scale_calibration()
        dialog.accept()

    def connect_camera(self) -> None:
        if self._is_connected():
            return
        self.cutting_reference_revision += 1

        self._set_status("正在连接相机...")
        self._sync_connection_state(False)
        live_binning = self._binning_value("cameraLiveResolutionCombo")
        exposure_auto = self.controls["cameraExposureAuto"].isChecked()
        exposure_value = self.controls["cameraExposureSpin"].value()

        def action():
            controller = self._controller()
            devices = controller.list_devices()
            device = controller.open(DEFAULT_CAMERA_SERIAL)
            capabilities = controller.capabilities()
            controller.apply_acquisition_settings(self._settings_for_binning(capabilities, live_binning))
            controller.set_exposure(auto=exposure_auto, value_ms=exposure_value)
            return device, devices, capabilities

        self._task_executor.submit("connect", action, self._on_task_done, self._on_task_error)

    def disconnect_camera(self) -> None:
        self.cutting_reference_revision += 1
        self.stop_preview()
        controller = self.controller
        if controller is None:
            self._on_task_done("disconnect", None)
            return

        def action():
            controller.close()
            return None

        self._task_executor.submit("disconnect", action, self._on_task_done, self._on_task_error)

    def toggle_preview(self) -> None:
        if self._preview_thread is None:
            self.start_preview()
        else:
            self.stop_preview()

    def start_preview(self) -> None:
        if not self._is_connected():
            self._set_status("相机未连接")
            return
        if self._preview_thread is not None:
            return
        thread = CameraPreviewThread(self._controller(), self, context_provider=self._cutting_camera_context)
        thread.frame_ready.connect(self._handle_frame)
        thread.failed.connect(self._handle_preview_error)
        thread.finished.connect(lambda t=thread: self._preview_finished(t))
        self._preview_thread = thread
        self._set_preview_fps(None)
        self.controls["cameraPreview"].setText(self._tr("停止预览"))
        self._set_status("相机预览中")
        thread.start()

    def capture_cutting_reference(self):
        """Worker-safe reference acquisition on the existing camera connection."""
        from .cutting_geometry import CalibrationContext
        from .cutting_session import FrameEnvelope
        controller = self.controller
        if controller is None or controller.current_device is None:
            raise CameraError("Camera is not connected")
        objective = self._current_objective
        revision = self.cutting_reference_revision
        serial = controller.current_device.serial_number
        started = time.monotonic()
        frame = controller.grab_reference()
        if (revision != self.cutting_reference_revision or controller is not self.controller
                or controller.current_device is None
                or controller.current_device.serial_number != serial):
            raise CameraError("Camera or objective changed during capture")
        context = CalibrationContext(serial, objective, frame.width, frame.height, frame.binning)
        return FrameEnvelope(frame, context, started, revision)

    def _invalidate_cutting_reference(self, *_args) -> None:
        self.cutting_reference_revision += 1

    def _cutting_camera_context(self) -> dict:
        device = getattr(self.controller, "current_device", None)
        return {"camera_serial": getattr(device, "serial_number", ""),
                "objective": self._current_objective,
                "source_revision": self.cutting_reference_revision}

    def stop_preview(self, timeout_ms: int = PREVIEW_STOP_TIMEOUT_MS) -> bool:
        thread = self._preview_thread
        if thread is None:
            return True
        thread.request_stop()
        thread.wait(max(0, int(timeout_ms)))
        if thread.isRunning():
            self._detach_preview_thread(thread)
            self._preview_thread = None
            self.controls["cameraPreview"].setText(self._tr("预览"))
            return False
        self._disconnect_preview_thread_signals(thread)
        self._preview_finished(thread)
        return True

    def apply_exposure(self) -> None:
        if not self._is_connected():
            self._set_status("相机未连接")
            return
        auto = self.controls["cameraExposureAuto"].isChecked()
        value = self.controls["cameraExposureSpin"].value()
        controller = self._controller()
        self._task_executor.submit(
            "exposure",
            lambda: controller.set_exposure(auto=auto, value_ms=value),
            self._on_task_done,
            self._on_task_error,
        )

    def apply_gain(self) -> None:
        if not self._is_connected():
            self._set_status("相机未连接")
            return
        auto = self.controls["cameraGainAuto"].isChecked()
        value = self.controls["cameraGainSpin"].value()
        controller = self._controller()
        self._task_executor.submit(
            "gain",
            lambda: controller.set_gain(auto=auto, value=value),
            self._on_task_done,
            self._on_task_error,
        )

    def apply_image_adjustment(self) -> None:
        if not self._is_connected():
            return
        gamma = self.controls["cameraGammaSpin"].value()
        sharpness = self.controls["cameraSharpnessSpin"].value()
        controller = self._controller()
        self._task_executor.submit(
            "image_adjustment",
            lambda: controller.set_image_adjustment(gamma=gamma, sharpness=sharpness),
            self._on_task_done,
            self._on_task_error,
        )

    def _handle_image_click(self, x: float, y: float) -> None:
        if self._calibration_active:
            self.record_calibration_point(x, y)
            return
        if self._measurement_mode is None:
            return
        self._record_measurement_point(x, y)

    def _handle_image_hover(self, x: float, y: float) -> None:
        if self._measurement_mode is None or not self._active_measurement_points:
            return
        self._active_measurement_preview_point = (float(x), float(y))
        self._schedule_overlay_refresh()

    def _set_measurement_mode(self, mode: str | None) -> None:
        if mode not in {None, "line", "polygon"}:
            raise ValueError(f"Unsupported measurement mode: {mode}")
        self._measurement_mode = mode
        self._active_measurement_points = []
        self._active_measurement_preview_point = None
        self._sync_measurement_buttons()
        if mode == "line":
            self._set_measurement_status("测量: 直线距离，点击两个端点")
        elif mode == "polygon":
            self._set_measurement_status("测量: 面积，逐点点击后按完成面积")
        else:
            self._set_measurement_status("测量: 就绪")
        self._refresh_last_frame()

    def _record_measurement_point(self, x: float, y: float) -> None:
        self._active_measurement_points.append((float(x), float(y)))
        self._active_measurement_preview_point = None
        if self._measurement_mode == "line" and len(self._active_measurement_points) >= 2:
            self._create_measurement("line", self._active_measurement_points[:2])
            return
        if self._measurement_mode == "line":
            self._set_measurement_status("测量: 已记录第一个端点")
        elif self._measurement_mode == "polygon":
            self._set_measurement_status(
                "测量: 面积点 {count}，继续点击或按完成面积",
                count=len(self._active_measurement_points),
            )
        self._refresh_last_frame()

    def finish_polygon_measurement(self) -> None:
        if self._measurement_mode != "polygon":
            return
        if len(self._active_measurement_points) < 3:
            self._set_measurement_status("测量: 面积至少需要三个点")
            return
        self._create_measurement("polygon", self._active_measurement_points)

    def undo_measurement(self) -> None:
        if self._active_measurement_points:
            self._active_measurement_points.pop()
            self._set_measurement_status(
                "测量: 当前点数 {count}", count=len(self._active_measurement_points)
            )
        elif self._measurements:
            self._measurements.pop()
            self._set_measurement_status("测量: 已撤销上一条结果")
        self._active_measurement_preview_point = None
        self._update_measurement_table()
        self._refresh_last_frame()

    def clear_measurements(self) -> None:
        self._measurements = []
        self._active_measurement_points = []
        self._active_measurement_preview_point = None
        self._set_measurement_mode(None)
        self._update_measurement_table()
        self._set_measurement_status("测量: 已清空")

    def delete_selected_measurement(self) -> None:
        table = self.controls["cameraMeasurementTable"]
        selected_rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        if not selected_rows:
            return
        for row in selected_rows:
            if 0 <= row < len(self._measurements):
                del self._measurements[row]
        self._update_measurement_table()
        self._set_measurement_status("测量: 已删除选中结果")
        self._refresh_last_frame()

    def _create_measurement(self, kind: str, points: list[tuple[float, float]]) -> None:
        frame_width, frame_height = self._current_measurement_frame_size()
        measurement = MeasurementObject(
            id=self._next_measurement_id,
            kind=kind,
            points=normalized_points(points),
            objective=self._current_objective,
            frame_width=frame_width,
            frame_height=frame_height,
            um_per_pixel=self._current_um_per_pixel(),
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._next_measurement_id += 1
        self._measurements.append(measurement)
        self._active_measurement_points = []
        self._active_measurement_preview_point = None
        self._measurement_mode = None
        self._sync_measurement_buttons()
        self._update_measurement_table()
        result = evaluate_measurement(measurement)
        self._set_measurement_status(
            "测量: M{id} {value}", id=measurement.id, value=result["display"]
        )
        self._refresh_last_frame()

    def _sync_measurement_buttons(self) -> None:
        if "cameraMeasureLine" not in self.controls:
            return
        self.controls["cameraMeasureLine"].setChecked(self._measurement_mode == "line")
        self.controls["cameraMeasurePolygon"].setChecked(self._measurement_mode == "polygon")
        self.controls["cameraFinishPolygon"].setEnabled(self._measurement_mode == "polygon")

    def _update_measurement_table(self) -> None:
        table = self.controls.get("cameraMeasurementTable")
        if table is None:
            return
        table.setRowCount(0)
        for row, measurement in enumerate(self._measurements):
            result = evaluate_measurement(measurement)
            table.insertRow(row)
            values = (
                str(measurement.id),
                str(measurement.kind),
                str(result["display"]),
                self._measurement_note(measurement),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeColumnsToContents()

    def _measurement_note(self, measurement: MeasurementObject) -> str:
        if measurement.um_per_pixel is None or measurement.um_per_pixel <= 0:
            return self._tr("未标定")
        return f"{measurement.objective}, {measurement.um_per_pixel:.4g} um/px"

    def _current_measurement_frame_size(self) -> tuple[int, int]:
        label = self.controls.get("cameraImageLabel")
        if hasattr(label, "source_size"):
            width, height = label.source_size()
            if width > 0 and height > 0:
                return width, height
        if self._last_frame is not None:
            return int(self._last_frame.width), int(self._last_frame.height)
        return self._base_dimensions()

    def _current_um_per_pixel(self, frame: CameraFrame | None = None) -> float | None:
        calibration = self.scale_calibrations.get(self._current_objective)
        frame = frame if frame is not None else self._displayed_frame
        if (calibration is None or not calibration.binning or calibration.binning < 1
                or not math.isfinite(calibration.um_per_pixel) or calibration.um_per_pixel <= 0
                or frame is None or not frame.binning or frame.binning < 1):
            return None
        return calibration.um_per_pixel * frame.binning / calibration.binning

    def _on_crosshair_toggled(self) -> None:
        if not self.controls["cameraCrosshairToggle"].isChecked():
            self._set_measurement_status("测量: 十字线关闭")
        else:
            self._set_measurement_status("{value}", value=self._crosshair_status_text)
        self._refresh_last_frame()

    def _crosshair_status_text(self) -> str:
        x, y = self._center_crosshair_point()
        scale = self._current_um_per_pixel()
        if scale is None:
            return self._tr("中心十字线: X {x:.1f} px, Y {y:.1f} px (未标定)").format(x=x, y=y)
        return self._tr("中心十字线: X {x:.1f} px / {x_um:.1f} um, Y {y:.1f} px / {y_um:.1f} um").format(
            x=x,
            y=y,
            x_um=x * scale,
            y_um=y * scale,
        )

    def _center_crosshair_point(self, width: int | None = None, height: int | None = None) -> tuple[float, float]:
        if width is None or height is None:
            width, height = self._current_measurement_frame_size()
        return float(width) / 2.0, float(height) / 2.0

    def _set_measurement_status(self, source: str, **values) -> None:
        label = self.controls.get("cameraMeasurementStatusLabel")
        if label is not None:
            self._rt.message(label, source, **values)

    def _schedule_overlay_refresh(self) -> None:
        if self._overlay_refresh_scheduled:
            return
        self._overlay_refresh_scheduled = True
        QTimer.singleShot(0, self._refresh_overlay_now)

    def _refresh_overlay_now(self) -> None:
        self._overlay_refresh_scheduled = False
        self._refresh_last_frame()

    def _clear_measurements_for_coordinate_change(self) -> None:
        if (
            not self._measurements
            and not self._active_measurement_points
            and self._active_measurement_preview_point is None
        ):
            return
        self._measurements = []
        self._active_measurement_points = []
        self._active_measurement_preview_point = None
        self._measurement_mode = None
        self._sync_measurement_buttons()
        self._update_measurement_table()
        self._set_measurement_status("测量: 坐标系变化，已清空测量")
        self._refresh_last_frame()

    def _on_display_transform_changed(self) -> None:
        self._calibration_points.clear()
        had_overlay = bool(
            self._measurements
            or self._active_measurement_points
            or self._active_measurement_preview_point is not None
            or self.controls["cameraCrosshairToggle"].isChecked()
        )
        self._clear_measurements_for_coordinate_change()
        if not had_overlay:
            self._refresh_last_frame()

    def _has_measurement_sidecar_content(self) -> bool:
        return bool(
            self._measurements
            or self.controls["cameraCrosshairToggle"].isChecked()
        )

    def _write_measurement_sidecar(self, image_path: Path, frame: CameraFrame) -> Path:
        sidecar = Path(image_path).with_suffix(".measurements.json")
        payload = measurement_sidecar_payload(
            objective=self._current_objective,
            frame_width=int(frame.width),
            frame_height=int(frame.height),
            flip_horizontal=self.controls["cameraFlipHorizontal"].isChecked(),
            flip_vertical=self.controls["cameraFlipVertical"].isChecked(),
            um_per_pixel=self._current_um_per_pixel(frame),
            crosshair=self._center_crosshair_point(frame.width, frame.height)
            if self.controls["cameraCrosshairToggle"].isChecked()
            else None,
            measurements=tuple(self._measurements),
        )
        payload["binning"] = frame.binning
        sidecar.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return sidecar

    def take_snapshot(self) -> None:
        try:
            path = self.save_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._set_status("截图失败: {error}", error=str(exc))
            return
        self._set_status("截图已保存: {name}", name=path.name)

    def save_snapshot(self, *, now=None) -> Path:
        self._save_settings()
        frame = self._last_frame
        was_previewing = self._preview_thread is not None
        if was_previewing and not self.stop_preview(timeout_ms=PREVIEW_STOP_TIMEOUT_MS):
            raise CameraError(self._tr("预览停止超时，已取消拍照以保护相机状态"))
        try:
            if self._is_connected():
                frame = self._controller().grab_one_with_settings(
                    snap_settings=self._snap_settings(),
                    restore_settings=self._live_settings(),
                    timeout_ms=DEFAULT_CAMERA_GRAB_TIMEOUT_MS,
                )
                if self._displayed_frame is None:
                    self._last_frame = frame
            elif frame is None:
                raise CameraError(self._tr("没有可保存的相机图像"))
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = build_snapshot_path(
                directory=self.snapshot_dir,
                template=self.controls["cameraSaveTemplateEdit"].text().strip() or DEFAULT_FILENAME_TEMPLATE,
                objective=self._current_objective,
                image_format=self.controls["cameraFormatCombo"].currentText(),
                now=now,
            )
            include_annotations = self.controls["cameraIncludeAnnotations"].isChecked()
            image = self._processed_image(frame, with_scale=True, with_measurements=include_annotations)
            image_format = self.controls["cameraFormatCombo"].currentText()
            quality = self.controls["cameraJpegQualitySpin"].value() if image_format == "JPG" else -1
            if not image.save(str(path), image_format, quality):
                raise CameraError(self._tr("无法写入截图文件: {path}").format(path=path))
            if include_annotations and self._has_measurement_sidecar_content():
                self._write_measurement_sidecar(path, frame)
            return path
        finally:
            if was_previewing and not self._closing and self._is_connected():
                self.start_preview()

    def prepare_for_close(self, timeout_ms: int = 5000) -> bool:
        deadline = QDeadlineTimer(max(0, int(timeout_ms)))
        if self._close_result is None:
            self.cutting_reference_revision += 1
            self._closing = True
            self.stop_preview(timeout_ms=deadline.remainingTime())
            executor = self._task_executor
            if hasattr(executor, "wait_for_idle"):
                executor.wait_for_idle(deadline.remainingTime())
            if hasattr(executor, "detach_running_threads"):
                executor.detach_running_threads()
            pending = (*self._detached_preview_threads, *getattr(executor, "_detached_threads", ()))
            result = self._close_result = Future()

            def finish_close():
                try:
                    # A pending connect may still be constructing/opening the controller.
                    for worker in pending:
                        worker.wait()
                    if self.controller is not None:
                        self.controller.close()
                        self.controller = None
                except Exception:
                    logging.getLogger("ats.camera").exception("Camera shutdown failed")
                    result.set_result(False)
                else:
                    result.set_result(True)

            # Both grab and close can block inside the SDK. Never hold the GUI
            # thread beyond the deadline, even when close itself is the stuck call.
            threading.Thread(target=finish_close, name="ATSCameraClose", daemon=True).start()
        try:
            return self._close_result.result(timeout=deadline.remainingTime() / 1000)
        except TimeoutError:
            return False

    def _on_task_done(self, kind: str, result) -> None:
        if self._closing:
            return
        if kind == "connect":
            device, _devices, capabilities = result
            self._capabilities = capabilities
            self._apply_capabilities(capabilities)
            self._sync_connection_state(True)
            self._rt.message(
                self.controls["cameraDeviceLabel"], "相机: {model} ({serial})",
                model=device.model_name, serial=device.serial_number,
            )
            self._set_status("Basler 相机已连接")
        elif kind == "disconnect":
            self._sync_connection_state(False)
            self.controller = None
            self._rt.message(self.controls["cameraDeviceLabel"], "相机: 未连接")
            self._last_frame = self._displayed_frame = self._pending_frame = None
            self._update_scale_status()
            self.controls["cameraImageLabel"].setText(self._tr("无图像"))
            self.controls["cameraImageLabel"].setPixmap(QPixmap())
            self.controls["cameraImageLabel"].set_source_size(0, 0)
            self._set_status("Basler 相机已断开")
        elif kind == "exposure":
            self._set_status("曝光设置已发送")
        elif kind == "gain":
            self._set_status("增益设置已发送")
        elif kind == "image_adjustment":
            self._set_status("图像设置已发送")

    def _on_task_error(self, kind: str, message) -> None:
        if self._closing:
            return
        self._sync_connection_state(self._is_connected())
        self._set_status("{message}", message=str(message))

    def _handle_frame(self, frame: CameraFrame, fps: float | None = None) -> None:
        if self._closing:
            return
        self.raw_frame_ready.emit(frame)
        self._last_frame = frame
        self._pending_frame = frame
        self._set_preview_fps(fps)
        if not self._frame_render_scheduled:
            self._schedule_pending_frame_render()

    def _schedule_pending_frame_render(self) -> None:
        delay_ms = 0
        if self._last_render_ms is not None:
            elapsed_ms = int(self._render_clock.elapsed()) - self._last_render_ms
            delay_ms = max(0, int(math.ceil(DEFAULT_CAMERA_RENDER_INTERVAL_MS - elapsed_ms)))
        self._frame_render_scheduled = True
        QTimer.singleShot(delay_ms, self._render_pending_frame)

    def _render_pending_frame(self) -> None:
        self._frame_render_scheduled = False
        if self._closing or self._pending_frame is None:
            return
        frame = self._pending_frame
        self._pending_frame = None
        self._last_render_ms = int(self._render_clock.elapsed())
        self._render_frame(frame)

    def _render_frame(self, frame: CameraFrame) -> None:
        previous = self._displayed_frame
        self._displayed_frame = frame
        if previous is None or (previous.width, previous.height, previous.binning) != (frame.width, frame.height, frame.binning):
            self._calibration_points.clear()
            self._active_measurement_points.clear()
            self._active_measurement_preview_point = None
            self._update_scale_status()
        image = self._processed_image(frame, with_scale=True, with_measurements=True)
        label = self.controls["cameraImageLabel"]
        label.set_source_size(image.width(), image.height())
        label.setPixmap(QPixmap.fromImage(self._scaled_image_for_label(image, label)))

    def _scaled_image_for_label(self, image: QImage, label: QLabel) -> QImage:
        target_size = label.size()
        if target_size.width() > 0 and target_size.height() > 0:
            fit_size = image.size()
            fit_size.scale(target_size, Qt.KeepAspectRatio)
            width = max(1, round(fit_size.width() * self._image_zoom))
            height = max(1, round(fit_size.height() * self._image_zoom))
            return image.scaled(width, height, Qt.KeepAspectRatio, Qt.FastTransformation)
        return image

    def _adjust_image_zoom(self, steps: int) -> None:
        if steps == 0:
            return
        self._set_image_zoom(self._image_zoom * (_IMAGE_ZOOM_STEP ** int(steps)))

    def _set_image_zoom(self, zoom: float) -> None:
        bounded = max(_MIN_IMAGE_ZOOM, min(_MAX_IMAGE_ZOOM, float(zoom)))
        if abs(bounded - self._image_zoom) < 0.001:
            return
        self._image_zoom = bounded
        self._update_image_zoom_label()
        self._refresh_last_frame()

    def _update_image_zoom_label(self) -> None:
        label = self.controls.get("cameraZoomLabel")
        if label is not None:
            label.setText(self._image_zoom_text())

    def _image_zoom_text(self) -> str:
        return self._tr("图像缩放: {percent}%").format(percent=round(self._image_zoom * 100))

    def _set_preview_fps(self, fps: float | None) -> None:
        self._preview_fps = float(fps) if fps and fps > 0 else None
        label = self.controls.get("cameraFpsLabel")
        if label is not None:
            label.setText(self._preview_fps_text())

    def _preview_fps_text(self) -> str:
        if self._preview_fps is None:
            return self._tr("预览帧率: -- FPS")
        return self._tr("预览帧率: {fps:.1f} FPS").format(fps=self._preview_fps)

    def _handle_preview_error(self, message: str) -> None:
        if self._closing:
            return
        self._set_status("预览失败: {error}", error=message)

    def _preview_finished(self, thread: CameraPreviewThread) -> None:
        if self._preview_thread is thread:
            self._preview_thread = None
        if thread in self._detached_preview_threads:
            self._detached_preview_threads.remove(thread)
        if thread in _DETACHED_CAMERA_THREADS:
            _DETACHED_CAMERA_THREADS.remove(thread)
        if not self._closing:
            self.controls["cameraPreview"].setText(self._tr("预览"))
            self._set_preview_fps(None)
        thread.deleteLater()

    def _detach_preview_thread(self, thread: CameraPreviewThread) -> None:
        self._disconnect_preview_thread_signals(thread)
        try:
            thread.setParent(None)
        except Exception:
            pass
        try:
            thread.quit()
        except Exception:
            pass
        self._detached_preview_threads.append(thread)
        _DETACHED_CAMERA_THREADS.append(thread)

    def _disconnect_preview_thread_signals(self, thread: CameraPreviewThread) -> None:
        for signal in (thread.frame_ready, thread.failed, thread.finished):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass

    def _processed_frame(self, frame: CameraFrame) -> CameraFrame:
        return flip_frame(
            frame,
            horizontal=self.controls["cameraFlipHorizontal"].isChecked(),
            vertical=self.controls["cameraFlipVertical"].isChecked(),
        )

    def _processed_image(self, frame: CameraFrame, *, with_scale: bool, with_measurements: bool = True) -> QImage:
        image = qimage_from_frame(frame)
        horizontal = self.controls["cameraFlipHorizontal"].isChecked()
        vertical = self.controls["cameraFlipVertical"].isChecked()
        if horizontal or vertical:
            orientation = Qt.Orientations()
            if horizontal:
                orientation |= Qt.Horizontal
            if vertical:
                orientation |= Qt.Vertical
            image = image.flipped(orientation)
        calibration = self.scale_calibrations.get(self._current_objective)
        scale = self._current_um_per_pixel(frame)
        if with_scale and scale is not None:
            image = draw_scale_bar(image, replace(calibration, um_per_pixel=scale), self._current_objective)
        if with_measurements:
            image = draw_measurement_overlay(
                image,
                measurements=tuple(self._measurements),
                active_points=tuple(self._active_measurement_points),
                active_preview_point=self._active_measurement_preview_point,
                active_kind=self._measurement_mode,
                active_source_size=self._current_measurement_frame_size(),
                center_crosshair=self.controls["cameraCrosshairToggle"].isChecked(),
            )
        return image

    def _refresh_last_frame(self) -> None:
        if self._last_frame is not None:
            self._pending_frame = None
            self._last_render_ms = int(self._render_clock.elapsed())
            self._render_frame(self._last_frame)

    def _base_dimensions(self, capabilities: CameraCapabilities | None = None) -> tuple[int, int]:
        capabilities = capabilities or self._capabilities
        if capabilities and capabilities.sensor_width and capabilities.sensor_height:
            return int(capabilities.sensor_width), int(capabilities.sensor_height)
        width = int(capabilities.width.max) if capabilities and capabilities.width else 2592
        height = int(capabilities.height.max) if capabilities and capabilities.height else 1944
        return width, height

    def _resolution_options(self, capabilities: CameraCapabilities | None = None) -> list[tuple[int, int, int]]:
        width, height = self._base_dimensions(capabilities)
        feature_range = (capabilities or self._capabilities).binning if (capabilities or self._capabilities) else None
        max_binning = int(feature_range.max) if feature_range is not None else 4
        min_binning = int(feature_range.min) if feature_range is not None else 1
        return [
            (binning, max(4, width // binning), max(4, height // binning))
            for binning in range(max(1, min_binning), max(1, max_binning) + 1)
        ]

    def _resolution_label(self, width: int, height: int) -> str:
        return f"{int(width)} x {int(height)} px"

    def _populate_resolution_combos(self, capabilities: CameraCapabilities | None = None) -> None:
        options = self._resolution_options(capabilities)
        selections = {
            "cameraLiveResolutionCombo": self._binning_value("cameraLiveResolutionCombo")
            if "cameraLiveResolutionCombo" in self.controls and self.controls["cameraLiveResolutionCombo"].count()
            else int(self.settings.live_binning),
            "cameraSnapResolutionCombo": self._binning_value("cameraSnapResolutionCombo")
            if "cameraSnapResolutionCombo" in self.controls and self.controls["cameraSnapResolutionCombo"].count()
            else int(self.settings.snap_binning),
        }
        for name, selected_binning in selections.items():
            combo = self.controls[name]
            combo.blockSignals(True)
            combo.clear()
            selected_index = 0
            for index, (binning, width, height) in enumerate(options):
                combo.addItem(self._resolution_label(width, height), binning)
                if int(binning) == int(selected_binning):
                    selected_index = index
            combo.setCurrentIndex(selected_index)
            combo.blockSignals(False)

    def _binning_value(self, name: str) -> int:
        data = self.controls[name].currentData()
        if data is not None:
            try:
                return max(1, int(data))
            except (TypeError, ValueError):
                pass
        text = self.controls[name].currentText().replace("x", "").replace("px", "").strip()
        try:
            return max(1, int(text))
        except ValueError:
            return 1

    def _live_settings(self, capabilities: CameraCapabilities | None = None) -> CameraAcquisitionSettings:
        binning = self._binning_value("cameraLiveResolutionCombo") if "cameraLiveResolutionCombo" in self.controls else int(self.settings.live_binning)
        width, height = self._base_dimensions(capabilities)
        return CameraAcquisitionSettings(width=max(4, width // binning), height=max(4, height // binning), binning=binning)

    def _snap_settings(self) -> CameraAcquisitionSettings:
        binning = self._binning_value("cameraSnapResolutionCombo")
        return self._settings_for_binning(self._capabilities, binning)

    def _settings_for_binning(
        self,
        capabilities: CameraCapabilities | None,
        binning: int,
    ) -> CameraAcquisitionSettings:
        width, height = self._base_dimensions()
        if capabilities is not None:
            width, height = self._base_dimensions(capabilities)
        return CameraAcquisitionSettings(width=max(4, width // binning), height=max(4, height // binning), binning=binning)

    def _apply_capabilities(self, capabilities: CameraCapabilities) -> None:
        self._populate_resolution_combos(capabilities)
        if capabilities.exposure_ms is not None:
            self.controls["cameraExposureSpin"].setRange(capabilities.exposure_ms.min, capabilities.exposure_ms.max)
        if capabilities.gain is not None:
            self.controls["cameraGainSpin"].setRange(capabilities.gain.min, capabilities.gain.max)
        for name, feature_range in (
            ("cameraGammaSpin", capabilities.gamma),
            ("cameraSharpnessSpin", capabilities.sharpness),
        ):
            control = self.controls[name]
            control.setEnabled(feature_range is not None)
            if feature_range is not None:
                control.setRange(feature_range.min, feature_range.max)

    def _save_settings(self) -> None:
        self.snapshot_dir = Path(self.controls["cameraSaveDirEdit"].text().strip() or str(CAMERA_SNAPSHOT_DIR))
        self.settings = CameraSettings(
            save_dir=str(self.snapshot_dir),
            filename_template=self.controls["cameraSaveTemplateEdit"].text().strip() or DEFAULT_FILENAME_TEMPLATE,
            image_format=self.controls["cameraFormatCombo"].currentText(),
            jpeg_quality=self.controls["cameraJpegQualitySpin"].value(),
            live_binning=self._binning_value("cameraLiveResolutionCombo"),
            snap_binning=self._binning_value("cameraSnapResolutionCombo"),
            flip_horizontal=self.controls["cameraFlipHorizontal"].isChecked(),
            flip_vertical=self.controls["cameraFlipVertical"].isChecked(),
            objective=self._current_objective,
            gamma=self.controls["cameraGammaSpin"].value(),
            sharpness=self.controls["cameraSharpnessSpin"].value(),
        )
        self.settings_store.save(self.settings)

    def set_objective(self, label: str, position: int | None = None) -> None:
        if not label:
            return
        previous_objective = self._current_objective
        self._current_objective = str(label)
        if previous_objective != self._current_objective:
            self.cutting_reference_revision += 1
        combo = self.controls.get("cameraObjectiveCombo")
        if isinstance(combo, QComboBox) and combo.currentText() != self._current_objective:
            combo.blockSignals(True)
            combo.setCurrentText(self._current_objective)
            combo.blockSignals(False)
        self._update_scale_status()
        if previous_objective != self._current_objective:
            self._calibration_points.clear()
            self._clear_measurements_for_coordinate_change()
        self._refresh_last_frame()

    def start_scale_calibration(self) -> None:
        length_spin = self.controls.get("cameraCalibrationLengthSpin")
        if isinstance(length_spin, QDoubleSpinBox):
            self._calibration_known_um = float(length_spin.value())
        self._calibration_active = True
        self._calibration_points = []
        self._set_status("请在预览图像上点击标尺两端")

    def record_calibration_point(self, x: float, y: float) -> None:
        if not self._calibration_active:
            return
        frame = self._displayed_frame
        if frame is None or not frame.binning:
            self._set_status("无法确认图像采样倍率，无法标定")
            return
        self._calibration_points.append((float(x), float(y)))
        if len(self._calibration_points) < 2:
            self._set_status("已记录第一个标定点")
            return
        p1, p2 = self._calibration_points[:2]
        calibration = self.scale_store.calibrate_from_points(
            self._current_objective,
            self._calibration_known_um,
            p1,
            p2,
            binning=frame.binning,
        )
        self.scale_calibrations[self._current_objective] = calibration
        self._calibration_active = False
        self._calibration_points = []
        self._update_scale_status()
        self._clear_measurements_for_coordinate_change()
        self._refresh_last_frame()

    def _update_scale_status(self) -> None:
        calibration = self.scale_calibrations.get(self._current_objective)
        scale = self._current_um_per_pixel()
        if calibration is not None and not calibration.binning:
            text = self._tr("{objective}: 旧标定缺少采样倍率，请重新标定").format(objective=self._current_objective)
        elif scale is None:
            text = self._tr("{objective}: 未标定").format(objective=self._current_objective)
        else:
            text = self._tr("{objective}: {value:.4g} um/pixel").format(
                objective=self._current_objective,
                value=scale,
            )
        if "cameraScaleStatusLabel" in self.controls:
            self.controls["cameraScaleStatusLabel"].setText(text)
        if "cameraOverlayStatusLabel" in self.controls:
            self.controls["cameraOverlayStatusLabel"].setText(text)

    def _sync_connection_state(self, connected: bool) -> None:
        self.controls["cameraPreview"].setEnabled(connected)
        self.controls["cameraSnapshot"].setEnabled(connected)

    def _is_connected(self) -> bool:
        return self.controller is not None and bool(getattr(self.controller, "is_open", False))

    def _set_status(self, source: str, **values) -> None:
        self._rt.message(self.controls["cameraStatusLabel"], source, **values)
