from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QDeadlineTimer, QObject, Qt, QThread, Signal
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
    QVBoxLayout,
    QWidget,
)

from .camera_controller import (
    CameraAcquisitionSettings,
    CameraCapabilities,
    DEFAULT_CAMERA_SERIAL,
    BaslerCameraController,
    CameraError,
    CameraFrame,
)
from .camera_image import draw_scale_bar, flip_frame, qimage_from_frame
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
    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, controller, parent=None, timeout_ms: int = 1000):
        super().__init__(parent)
        self.controller = controller
        self.timeout_ms = int(timeout_ms)
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            self.controller.start_preview()
            while not self._stop_requested:
                frame = self.controller.grab_one(timeout_ms=self.timeout_ms)
                self.frame_ready.emit(frame)
        except Exception as exc:  # noqa: BLE001
            if not self._stop_requested:
                self.failed.emit(str(exc))
        finally:
            try:
                self.controller.stop_preview()
            except Exception as exc:  # noqa: BLE001
                if not self._stop_requested:
                    self.failed.emit(str(exc))


class CameraImageLabel(QLabel):
    image_clicked = Signal(float, float)

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._source_width = 0
        self._source_height = 0

    def set_source_size(self, width: int, height: int) -> None:
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
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
                self.image_clicked.emit(source_x, source_y)
        super().mousePressEvent(event)


class CameraPanel(QWidget):
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
        self._last_frame: CameraFrame | None = None
        self._capabilities: CameraCapabilities | None = None
        self._current_objective = self.settings.objective or DEFAULT_OBJECTIVE
        self._calibration_active = False
        self._calibration_known_um = 100.0
        self._calibration_points: list[tuple[float, float]] = []
        self._scale_dialog: QDialog | None = None
        self._closing = False
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

        header = QHBoxLayout()
        title = self._register("cameraTitleLabel", QLabel())
        self._rt.text(title, "相机预览")
        title.setStyleSheet("color:#d8e2ee;font-size:16px;font-weight:800;")
        header.addWidget(title)
        header.addStretch(1)
        device_label = self._register("cameraDeviceLabel", QLabel(self._tr("相机: 未连接")))
        device_label.setProperty("readout", True)
        header.addWidget(device_label)
        preview_layout.addLayout(header)

        image = self._register("cameraImageLabel", CameraImageLabel(self._tr("无图像")))
        image.setAlignment(Qt.AlignCenter)
        image.setMinimumSize(520, 420)
        image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        image.setFrameShape(QFrame.NoFrame)
        image.setStyleSheet(
            "QLabel#cameraImageLabel{background:#071018;color:#d8e2ee;border:1px solid #253546;"
            "border-radius:6px;font-weight:700;}"
        )
        image.image_clicked.connect(self.record_calibration_point)
        preview_layout.addWidget(image, 1)

        overlay = self._register("cameraOverlayStatusLabel", QLabel(self._tr("未标定")))
        overlay.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
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
        control_scroll.setMinimumWidth(330)
        control_scroll.setMaximumWidth(420)
        control_pane = self._register("cameraControlPane", QWidget())
        control_layout = QVBoxLayout(control_pane)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(8)
        control_scroll.setWidget(control_pane)

        acquire_group = self._register("cameraAcquireGroup", QGroupBox(self._tr("采集")))
        acquire_layout = QGridLayout(acquire_group)
        preview = self._register("cameraPreview", QPushButton())
        snapshot = self._register("cameraSnapshot", QPushButton())
        self._rt.text(preview, "预览")
        self._rt.text(snapshot, "拍照")
        preview.clicked.connect(self.toggle_preview)
        snapshot.clicked.connect(self.take_snapshot)
        acquire_layout.addWidget(preview, 0, 0)
        acquire_layout.addWidget(snapshot, 0, 1)
        control_layout.addWidget(acquire_group)

        save_group = self._register("cameraSaveGroup", QGroupBox(self._tr("保存")))
        save_layout = QGridLayout(save_group)
        save_dir = self._register("cameraSaveDirEdit", QLineEdit(str(self.snapshot_dir)))
        browse = self._register("cameraBrowseSaveDir", QPushButton(self._tr("浏览")))
        template = self._register("cameraSaveTemplateEdit", QLineEdit(self.settings.filename_template or DEFAULT_FILENAME_TEMPLATE))
        fmt = self._register("cameraFormatCombo", QComboBox())
        fmt.addItems(["TIFF", "PNG", "JPG"])
        fmt.setCurrentText(self.settings.image_format if self.settings.image_format in {"TIFF", "PNG", "JPG"} else "TIFF")
        quality = self._register("cameraJpegQualitySpin", QSpinBox())
        quality.setRange(1, 100)
        quality.setValue(int(self.settings.jpeg_quality))
        browse.clicked.connect(self.browse_save_dir)
        save_layout.addWidget(QLabel(self._tr("目录")), 0, 0)
        save_layout.addWidget(save_dir, 0, 1)
        save_layout.addWidget(browse, 0, 2)
        save_layout.addWidget(QLabel(self._tr("文件名")), 1, 0)
        save_layout.addWidget(template, 1, 1, 1, 2)
        save_layout.addWidget(QLabel(self._tr("格式")), 2, 0)
        save_layout.addWidget(fmt, 2, 1)
        save_layout.addWidget(quality, 2, 2)
        control_layout.addWidget(save_group)

        quality_group = self._register("cameraQualityGroup", QGroupBox(self._tr("清晰度")))
        quality_layout = QFormLayout(quality_group)
        live_resolution = self._register("cameraLiveResolutionCombo", QComboBox())
        snap_resolution = self._register("cameraSnapResolutionCombo", QComboBox())
        self._populate_resolution_combos()
        quality_layout.addRow(self._tr("Live 像素"), live_resolution)
        quality_layout.addRow(self._tr("Snap 像素"), snap_resolution)
        control_layout.addWidget(quality_group)

        exposure_group = self._register("cameraExposureGroup", QGroupBox(self._tr("曝光/增益")))
        exposure_layout = QGridLayout(exposure_group)
        exposure_auto = self._register("cameraExposureAuto", QCheckBox())
        self._rt.text(exposure_auto, "自动曝光")
        exposure_auto.setChecked(True)
        exposure_spin = self._register("cameraExposureSpin", QDoubleSpinBox())
        exposure_spin.setRange(0.01, 100000.0)
        exposure_spin.setDecimals(2)
        exposure_spin.setValue(10.0)
        exposure_spin.setSuffix(" ms")
        exposure_spin.setEnabled(False)
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
        exposure_layout.addWidget(QLabel(self._tr("锐化")), 3, 0)
        exposure_layout.addWidget(sharpness_spin, 3, 1)
        control_layout.addWidget(exposure_group)

        scale_group = self._register("cameraScaleGroup", QGroupBox(self._tr("比例尺")))
        scale_layout = QGridLayout(scale_group)
        objective_combo = self._register("cameraObjectiveCombo", QComboBox())
        objective_combo.addItems(list(OBJECTIVE_LABELS))
        objective_combo.setCurrentText(self._current_objective)
        calibrate = self._register("cameraOpenScaleCalibration", QPushButton(self._tr("标定...")))
        scale_status = self._register("cameraScaleStatusLabel", QLabel(""))
        scale_status.setWordWrap(True)
        objective_combo.currentTextChanged.connect(self.set_objective)
        calibrate.clicked.connect(self.open_scale_calibration_dialog)
        scale_layout.addWidget(QLabel(self._tr("倍率")), 0, 0)
        scale_layout.addWidget(objective_combo, 0, 1)
        scale_layout.addWidget(calibrate, 1, 0, 1, 2)
        scale_layout.addWidget(scale_status, 2, 0, 1, 2)
        control_layout.addWidget(scale_group)

        display_group = self._register("cameraDisplayGroup", QGroupBox(self._tr("显示")))
        display_layout = QVBoxLayout(display_group)
        flip_x = self._register("cameraFlipHorizontal", QCheckBox(self._tr("水平翻转")))
        flip_y = self._register("cameraFlipVertical", QCheckBox(self._tr("垂直翻转")))
        flip_x.setChecked(bool(self.settings.flip_horizontal))
        flip_y.setChecked(bool(self.settings.flip_vertical))
        flip_x.toggled.connect(lambda _checked: self._refresh_last_frame())
        flip_y.toggled.connect(lambda _checked: self._refresh_last_frame())
        display_layout.addWidget(flip_x)
        display_layout.addWidget(flip_y)
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
        self._update_scale_status()

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
        dialog.setWindowTitle(self._tr("比例尺标定"))
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
        form.addRow(self._tr("当前倍率"), objective)
        form.addRow(self._tr("已知长度"), length_spin)
        layout.addLayout(form)

        start = self._register("cameraStartScaleCalibration", QPushButton(self._tr("开始两点标定")))
        close = QPushButton(self._tr("关闭"))
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

        self._set_status(self._tr("正在连接相机..."))
        self._sync_connection_state(False)
        live_binning = self._binning_value("cameraLiveResolutionCombo")

        def action():
            controller = self._controller()
            devices = controller.list_devices()
            device = controller.open(DEFAULT_CAMERA_SERIAL)
            capabilities = controller.capabilities()
            controller.apply_acquisition_settings(self._settings_for_binning(capabilities, live_binning))
            return device, devices, capabilities

        self._task_executor.submit("connect", action, self._on_task_done, self._on_task_error)

    def disconnect_camera(self) -> None:
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
            self._set_status(self._tr("相机未连接"))
            return
        if self._preview_thread is not None:
            return
        thread = CameraPreviewThread(self._controller(), self)
        thread.frame_ready.connect(self._handle_frame)
        thread.failed.connect(self._handle_preview_error)
        thread.finished.connect(lambda t=thread: self._preview_finished(t))
        self._preview_thread = thread
        self.controls["cameraPreview"].setText(self._tr("停止预览"))
        self._set_status(self._tr("相机预览中"))
        thread.start()

    def stop_preview(self, timeout_ms: int = 2000) -> bool:
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
            self._set_status(self._tr("相机未连接"))
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
            self._set_status(self._tr("相机未连接"))
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

    def take_snapshot(self) -> None:
        try:
            path = self.save_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._set_status(self._tr("截图失败: {error}").format(error=exc))
            return
        self._set_status(self._tr("截图已保存: {name}").format(name=path.name))

    def save_snapshot(self, *, now=None) -> Path:
        self._save_settings()
        frame = self._last_frame
        was_previewing = self._preview_thread is not None
        if was_previewing and not self.stop_preview(timeout_ms=2000):
            raise CameraError(self._tr("预览停止超时，已取消拍照以保护相机状态"))
        try:
            if self._is_connected():
                frame = self._controller().grab_one_with_settings(
                    snap_settings=self._snap_settings(),
                    restore_settings=self._live_settings(),
                    timeout_ms=1000,
                )
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
            image = self._processed_image(frame, with_scale=True)
            image_format = self.controls["cameraFormatCombo"].currentText()
            quality = self.controls["cameraJpegQualitySpin"].value() if image_format == "JPG" else -1
            if not image.save(str(path), image_format, quality):
                raise CameraError(self._tr("无法写入截图文件: {path}").format(path=path))
            return path
        finally:
            if was_previewing and not self._closing and self._is_connected():
                self.start_preview()

    def prepare_for_close(self, timeout_ms: int = 5000) -> bool:
        self._closing = True
        all_idle = self.stop_preview(timeout_ms=timeout_ms)
        executor = self._task_executor
        if hasattr(executor, "wait_for_idle"):
            executor.wait_for_idle(timeout_ms)
            if hasattr(executor, "is_busy") and executor.is_busy():
                all_idle = False
        if hasattr(executor, "detach_running_threads"):
            executor.detach_running_threads()
            if getattr(executor, "_detached_threads", None):
                all_idle = False
        if self.controller is not None:
            try:
                self.controller.close()
            except Exception as exc:  # noqa: BLE001
                self._set_status(str(exc))
            finally:
                self.controller = None
        return all_idle

    def _on_task_done(self, kind: str, result) -> None:
        if self._closing:
            return
        if kind == "connect":
            device, _devices, capabilities = result
            self._capabilities = capabilities
            self._apply_capabilities(capabilities)
            self._sync_connection_state(True)
            self.controls["cameraDeviceLabel"].setText(
                self._tr("相机: {model} ({serial})").format(
                    model=device.model_name,
                    serial=device.serial_number,
                )
            )
            self._set_status(self._tr("Basler 相机已连接"))
        elif kind == "disconnect":
            self._sync_connection_state(False)
            self.controller = None
            self.controls["cameraDeviceLabel"].setText(self._tr("相机: 未连接"))
            self._last_frame = None
            self.controls["cameraImageLabel"].setText(self._tr("无图像"))
            self.controls["cameraImageLabel"].setPixmap(QPixmap())
            self.controls["cameraImageLabel"].set_source_size(0, 0)
            self._set_status(self._tr("Basler 相机已断开"))
        elif kind == "exposure":
            self._set_status(self._tr("曝光设置已发送"))
        elif kind == "gain":
            self._set_status(self._tr("增益设置已发送"))
        elif kind == "image_adjustment":
            self._set_status(self._tr("图像设置已发送"))

    def _on_task_error(self, kind: str, message) -> None:
        if self._closing:
            return
        self._sync_connection_state(self._is_connected())
        self._set_status(str(message))

    def _handle_frame(self, frame: CameraFrame) -> None:
        if self._closing:
            return
        self._last_frame = frame
        image = self._processed_image(frame, with_scale=True)
        pixmap = QPixmap.fromImage(image)
        label = self.controls["cameraImageLabel"]
        label.set_source_size(image.width(), image.height())
        target_size = label.size()
        if target_size.width() > 0 and target_size.height() > 0:
            pixmap = pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pixmap)

    def _handle_preview_error(self, message: str) -> None:
        if self._closing:
            return
        self._set_status(self._tr("预览失败: {error}").format(error=message))

    def _preview_finished(self, thread: CameraPreviewThread) -> None:
        if self._preview_thread is thread:
            self._preview_thread = None
        if thread in self._detached_preview_threads:
            self._detached_preview_threads.remove(thread)
        if thread in _DETACHED_CAMERA_THREADS:
            _DETACHED_CAMERA_THREADS.remove(thread)
        if not self._closing:
            self.controls["cameraPreview"].setText(self._tr("预览"))
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

    def _processed_image(self, frame: CameraFrame, *, with_scale: bool) -> QImage:
        image = qimage_from_frame(self._processed_frame(frame))
        calibration = self.scale_calibrations.get(self._current_objective)
        if with_scale and calibration is not None:
            image = draw_scale_bar(image, calibration, self._current_objective)
        return image

    def _refresh_last_frame(self) -> None:
        if self._last_frame is not None:
            self._handle_frame(self._last_frame)

    def _base_dimensions(self, capabilities: CameraCapabilities | None = None) -> tuple[int, int]:
        capabilities = capabilities or self._capabilities
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
        self._current_objective = str(label)
        combo = self.controls.get("cameraObjectiveCombo")
        if isinstance(combo, QComboBox) and combo.currentText() != self._current_objective:
            combo.blockSignals(True)
            combo.setCurrentText(self._current_objective)
            combo.blockSignals(False)
        self._update_scale_status()
        self._refresh_last_frame()

    def start_scale_calibration(self) -> None:
        length_spin = self.controls.get("cameraCalibrationLengthSpin")
        if isinstance(length_spin, QDoubleSpinBox):
            self._calibration_known_um = float(length_spin.value())
        self._calibration_active = True
        self._calibration_points = []
        self._set_status(self._tr("请在预览图像上点击标尺两端"))

    def record_calibration_point(self, x: float, y: float) -> None:
        if not self._calibration_active:
            return
        self._calibration_points.append((float(x), float(y)))
        if len(self._calibration_points) < 2:
            self._set_status(self._tr("已记录第一个标定点"))
            return
        p1, p2 = self._calibration_points[:2]
        calibration = self.scale_store.calibrate_from_points(
            self._current_objective,
            self._calibration_known_um,
            p1,
            p2,
        )
        self.scale_calibrations[self._current_objective] = calibration
        self._calibration_active = False
        self._calibration_points = []
        self._update_scale_status()
        self._refresh_last_frame()

    def _update_scale_status(self) -> None:
        calibration = self.scale_calibrations.get(self._current_objective)
        if calibration is None:
            text = self._tr("{objective}: 未标定").format(objective=self._current_objective)
        else:
            text = self._tr("{objective}: {value:.4g} um/pixel").format(
                objective=self._current_objective,
                value=calibration.um_per_pixel,
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

    def _set_status(self, message: str) -> None:
        self.controls["cameraStatusLabel"].setText(str(message))
