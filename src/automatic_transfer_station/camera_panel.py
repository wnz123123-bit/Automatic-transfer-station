from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QDeadlineTimer, QObject, Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .camera_controller import (
    DEFAULT_CAMERA_SERIAL,
    BaslerCameraController,
    CameraError,
    CameraFrame,
    snapshot_path,
)
from .paths import CAMERA_SNAPSHOT_DIR


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


class CameraPanel(QWidget):
    def __init__(
        self,
        register: Callable[[str, object], object] | None = None,
        controller_factory: Callable[[], object] | None = None,
        task_executor: object | None = None,
        snapshot_dir: str | Path = CAMERA_SNAPSHOT_DIR,
    ):
        super().__init__()
        self._external_register = register
        self.controls: dict[str, object] = {}
        self.controller_factory = controller_factory or BaslerCameraController
        self.controller = None
        self.snapshot_dir = Path(snapshot_dir)
        self._task_executor = task_executor if task_executor is not None else ThreadedCameraExecutor(self)
        self._preview_thread: CameraPreviewThread | None = None
        self._detached_preview_threads: list[CameraPreviewThread] = []
        self._last_frame: CameraFrame | None = None
        self._closing = False
        from .i18n import translator, Retranslator, tr
        self._tr = tr
        self._rt = Retranslator(translator)

        self.setObjectName("cameraPanel")
        self._build_ui()
        self._sync_connection_state(False)

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

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = self._register("cameraTitleLabel", QLabel())
        self._rt.text(title, "相机预览")
        title.setStyleSheet("color:#2e3a47;font-size:14px;font-weight:800;")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        device_label = self._register("cameraDeviceLabel", QLabel(self._tr("相机: 未连接")))
        device_label.setProperty("readout", True)
        root.addWidget(device_label)

        image = self._register("cameraImageLabel", QLabel(self._tr("无图像")))
        image.setAlignment(Qt.AlignCenter)
        image.setMinimumHeight(320)
        image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        image.setFrameShape(QFrame.StyledPanel)
        image.setStyleSheet(
            "QLabel#cameraImageLabel{background:#101820;color:#d8e2ee;border:1px solid #2b3948;"
            "border-radius:6px;font-weight:700;}"
        )
        root.addWidget(image)

        button_row = QHBoxLayout()
        connect = self._register("cameraConnect", QPushButton())
        preview = self._register("cameraPreview", QPushButton())
        snapshot = self._register("cameraSnapshot", QPushButton())
        self._rt.text(connect, "连接", context="camera")
        self._rt.text(preview, "开始预览")
        self._rt.text(snapshot, "截图")
        connect.clicked.connect(self.connect_camera)
        preview.clicked.connect(self.toggle_preview)
        snapshot.clicked.connect(self.take_snapshot)
        button_row.addWidget(connect)
        button_row.addWidget(preview)
        button_row.addWidget(snapshot)
        root.addLayout(button_row)

        control_grid = QGridLayout()
        control_grid.setHorizontalSpacing(6)
        control_grid.setVerticalSpacing(5)
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
        exposure_auto.toggled.connect(self._on_exposure_auto_changed)
        exposure_spin.editingFinished.connect(self.apply_exposure)
        gain_auto.toggled.connect(self._on_gain_auto_changed)
        gain_spin.editingFinished.connect(self.apply_gain)
        control_grid.addWidget(exposure_auto, 0, 0)
        control_grid.addWidget(exposure_spin, 0, 1)
        control_grid.addWidget(gain_auto, 1, 0)
        control_grid.addWidget(gain_spin, 1, 1)
        root.addLayout(control_grid)

        status = self._register("cameraStatusLabel", QLabel(self._tr("Basler 相机未连接")))
        status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        status.setWordWrap(True)
        status.setStyleSheet("color:#175cd3;font-weight:700;")
        root.addWidget(status)

        self.setStyleSheet(
            """
            #cameraPanel {
                border: 1px solid #dde5ef;
                border-left: 5px solid #3278b8;
                border-radius: 8px;
                padding: 8px;
                background: #ffffff;
                font-weight: 700;
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
            #cameraPanel QPushButton#cameraConnect,
            #cameraPanel QPushButton#cameraPreview {
                background: #1f6f5b;
                border-color: #1f6f5b;
                color: #ffffff;
            }
            #cameraPanel QLabel[readout="true"] {
                color: #1f3350;
                background: #f1f5f9;
                border-radius: 6px;
                padding: 5px 7px;
                font-weight: 700;
            }
            """
        )

    def _on_exposure_auto_changed(self, checked: bool) -> None:
        self.controls["cameraExposureSpin"].setEnabled(not checked)
        if self._is_connected():
            self.apply_exposure()

    def _on_gain_auto_changed(self, checked: bool) -> None:
        self.controls["cameraGainSpin"].setEnabled(not checked)
        if self._is_connected():
            self.apply_gain()

    def connect_camera(self) -> None:
        if self._is_connected():
            self.disconnect_camera()
            return

        self._set_status(self._tr("正在连接相机..."))
        self.controls["cameraConnect"].setEnabled(False)

        def action():
            controller = self._controller()
            devices = controller.list_devices()
            device = controller.open(DEFAULT_CAMERA_SERIAL)
            return device, devices

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
            self.controls["cameraPreview"].setText(self._tr("开始预览"))
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

    def take_snapshot(self) -> None:
        try:
            path = self.save_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._set_status(self._tr("截图失败: {error}").format(error=exc))
            return
        self._set_status(self._tr("截图已保存: {name}").format(name=path.name))

    def save_snapshot(self) -> Path:
        frame = self._last_frame
        if frame is None:
            if not self._is_connected():
                raise CameraError(self._tr("没有可保存的相机图像"))
            frame = self._controller().grab_one()
            self._handle_frame(frame)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_path(self.snapshot_dir)
        image = self._qimage_from_frame(frame)
        if not image.save(str(path), "PNG"):
            raise CameraError(f"无法写入截图文件: {path}")
        return path

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
            device, _devices = result
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
            self._set_status(self._tr("Basler 相机已断开"))
        elif kind == "exposure":
            self._set_status(self._tr("曝光设置已发送"))
        elif kind == "gain":
            self._set_status(self._tr("增益设置已发送"))

    def _on_task_error(self, kind: str, message) -> None:
        if self._closing:
            return
        self.controls["cameraConnect"].setEnabled(True)
        self._sync_connection_state(self._is_connected())
        self._set_status(str(message))

    def _handle_frame(self, frame: CameraFrame) -> None:
        if self._closing:
            return
        self._last_frame = frame
        pixmap = QPixmap.fromImage(self._qimage_from_frame(frame))
        label = self.controls["cameraImageLabel"]
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
            self.controls["cameraPreview"].setText(self._tr("开始预览"))
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

    def _qimage_from_frame(self, frame: CameraFrame) -> QImage:
        stride = int(frame.width) * 3
        return QImage(frame.rgb_bytes, int(frame.width), int(frame.height), stride, QImage.Format_RGB888).copy()

    def _sync_connection_state(self, connected: bool) -> None:
        self.controls["cameraConnect"].setEnabled(True)
        self.controls["cameraConnect"].setText(
            self._tr("断开", context="camera") if connected else self._tr("连接", context="camera")
        )
        self.controls["cameraPreview"].setEnabled(connected)
        self.controls["cameraSnapshot"].setEnabled(connected)

    def _is_connected(self) -> bool:
        return self.controller is not None and bool(getattr(self.controller, "is_open", False))

    def _set_status(self, message: str) -> None:
        self.controls["cameraStatusLabel"].setText(str(message))
