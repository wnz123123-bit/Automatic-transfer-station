from __future__ import annotations

import time
from threading import Event
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QDeadlineTimer, QObject, QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .rd105_config import (
    DEFAULT_TARGET_TEMPERATURE,
    DEFAULT_TEMPERATURE_CONTROLLER_PORT,
    MAX_HEATING_RATE_C_PER_HOUR,
    MAX_TARGET_TEMPERATURE,
    MIN_TARGET_TEMPERATURE,
    POLL_STUCK_TIMEOUT_SECONDS,
    RD105_BAUDRATE,
    load_config,
    save_config,
)
from .rd105_controller import TemperatureController, select_preferred_port

# Temperature threshold above which a setpoint requires operator confirmation
# before sending to the controller (high-temperature safety gate, Fix 1).
WARN_CONFIRM_TEMPERATURE: float = 150.0

# Keep detached serial QThreads alive even after the owning panel/executor is
# destroyed during application close. Without this module-level reference Python
# can garbage-collect a still-running detached QThread and abort Qt.
_DETACHED_SERIAL_THREADS: list["SerialTaskThread"] = []

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - serial is optional for tests and previews
    list_ports = None


class SerialTaskThread(QThread):
    """Runs one blocking serial operation off the GUI thread (#6/I5).

    Mirrors objective_panel.ObjectiveMoveThread: it executes a callable on a
    worker thread and reports back to the GUI thread via Qt signals. ``kind`` lets
    the panel route results (snapshot / fast-set / rate-set / stop / connect) to
    the right slot. Each task carries its own thread because the controller's
    internal serial_lock already serializes hardware access.
    """

    succeeded = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self, kind: str, action: Callable[[], object], parent=None):
        super().__init__(parent)
        self._kind = kind
        self._action = action

    def run(self) -> None:
        try:
            result = self._action()
        except Exception as exc:  # noqa: BLE001 - keep the worker from dying silently
            self.failed.emit(self._kind, str(exc))
            return
        self.succeeded.emit(self._kind, result)


class ThreadedSerialExecutor(QObject):
    """Default executor: dispatches each blocking serial call to a QThread.

    A clicked 连接/设温/控温 returns immediately; the slow serial transaction runs
    on a worker thread and the result is delivered back via on_done/on_error,
    which the panel connects to GUI-thread slots.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._threads: set[SerialTaskThread] = set()
        # Holds SerialTaskThreads that were still running when the panel closed.
        # Reparented to None so the panel's destruction cannot delete a running
        # QThread (→ std::terminate), yet referenced here so Python/Qt do not
        # GC them mid-run (mirrors objective_panel._detached_threads).
        self._detached_threads: list[SerialTaskThread] = []

    def submit(self, kind, action, on_done, on_error) -> None:
        thread = SerialTaskThread(kind, action, self)
        self._threads.add(thread)

        def _handle_done(task_kind, result):
            on_done(task_kind, result)

        def _handle_failed(task_kind, message):
            on_error(task_kind, message)

        thread.succeeded.connect(_handle_done)
        thread.failed.connect(_handle_failed)
        thread.finished.connect(lambda t=thread: self._cleanup(t))
        thread.start()

    def _cleanup(self, thread: SerialTaskThread) -> None:
        self._threads.discard(thread)
        if thread in self._detached_threads:
            self._detached_threads.remove(thread)
        if thread in _DETACHED_SERIAL_THREADS:
            _DETACHED_SERIAL_THREADS.remove(thread)
        thread.deleteLater()

    def wait_for_idle(self, timeout_ms: int = 5000) -> None:
        # Block until every worker thread has finished. We wait on each thread and
        # also wait on any threads spawned by a finishing thread's GUI-side slot
        # (e.g. a completed command triggers a follow-up poll), so no QThread is
        # left running when the panel/executor is torn down (which would abort Qt).
        # An overall deadline guarantees a hung serial driver cannot freeze window
        # close forever; on expiry we return and let the caller proceed.
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
        """Detach any still-running threads after a bounded wait.

        For each thread still running: reparent to None (so the panel's
        destruction does not delete a running QThread → std::terminate),
        keep a reference in _detached_threads (so Python/Qt do not GC it
        mid-run), and call quit() (never terminate()) to ask the thread to
        stop. Mirrors objective_panel._detach_running_thread exactly.
        """
        for thread in list(self._threads):
            if not thread.isRunning():
                continue
            if thread in self._detached_threads:
                continue
            for signal in (thread.succeeded, thread.failed, thread.finished):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            try:
                thread.setParent(None)
            except Exception:  # pragma: no cover - defensive; reparent must not crash
                pass
            try:
                thread.quit()
            except Exception:  # pragma: no cover - quit on a run()-only thread is a no-op
                pass
            self._detached_threads.append(thread)
            _DETACHED_SERIAL_THREADS.append(thread)

    def reap_detached_threads(self) -> None:
        """Release detached threads that have since finished.

        Normal application close exits the process shortly after detaching, but
        the test suite keeps the same QApplication alive and continues pumping Qt
        events. Reaping finished detached threads avoids retaining half-torn-down
        QThread wrappers across unrelated tests.
        """
        for thread in list(self._detached_threads):
            if thread.isRunning():
                continue
            self._detached_threads.remove(thread)
            if thread in _DETACHED_SERIAL_THREADS:
                _DETACHED_SERIAL_THREADS.remove(thread)
            try:
                from shiboken6 import delete

                delete(thread)
            except Exception:  # pragma: no cover - fallback if shiboken is unavailable
                thread.deleteLater()


class InlineSerialExecutor:
    """Synchronous executor for deterministic tests.

    Runs the action immediately on the calling thread and invokes the callbacks
    before submit() returns, so a button click dispatches and completes its action
    without a running event loop.
    """

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


class TemperaturePlotWidget(QFrame):
    def __init__(self, max_samples: int = 600):
        super().__init__()
        self.max_samples = int(max_samples)
        self._samples = deque(maxlen=self.max_samples)
        self.setMinimumHeight(125)
        self.setStyleSheet("background: #f8fbff; border: 1px solid #cbd6e2; border-radius: 7px;")

    def add_snapshot(self, snapshot: dict[str, object]) -> None:
        temperature = self._float_or_none(snapshot.get("temperature"))
        setpoint = self._float_or_none(snapshot.get("effective_setpoint"))
        if temperature is None and setpoint is None:
            return
        self._samples.append((temperature, setpoint))
        self.update()

    def sample_count(self) -> int:
        return len(self._samples)

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(12, 10, -10, -12)
        if rect.width() <= 4 or rect.height() <= 4:
            return

        painter.setPen(QPen(QColor("#d7e1ec"), 1))
        for fraction in (0.25, 0.5, 0.75):
            y = rect.top() + rect.height() * fraction
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        values = [value for sample in self._samples for value in sample if value is not None]
        if not values:
            from .i18n import tr as _tr
            painter.setPen(QColor("#64748b"))
            painter.drawText(rect, Qt.AlignCenter, _tr("等待实时读数"))
            return

        low = min(values)
        high = max(values)
        if abs(high - low) < 1.0:
            middle = (high + low) / 2.0
            low = middle - 0.5
            high = middle + 0.5
        else:
            margin = (high - low) * 0.12
            low -= margin
            high += margin

        samples = list(self._samples)
        self._draw_series(painter, rect, samples, index=1, low=low, high=high, color="#7c8798")
        self._draw_series(painter, rect, samples, index=0, low=low, high=high, color="#1f6f5b")

        painter.setPen(QColor("#334154"))
        painter.drawText(rect.left(), rect.top() + 12, f"{high:.1f} °C")
        painter.drawText(rect.left(), rect.bottom(), f"{low:.1f} °C")

    def _draw_series(
        self,
        painter: QPainter,
        rect,
        samples: list[tuple[float | None, float | None]],
        index: int,
        low: float,
        high: float,
        color: str,
    ) -> None:
        points = []
        count = max(1, len(samples) - 1)
        for sample_index, sample in enumerate(samples):
            value = sample[index]
            if value is None:
                continue
            x = rect.left() + rect.width() * sample_index / count
            y = rect.bottom() - rect.height() * (value - low) / (high - low)
            points.append(QPointF(x, y))
        if len(points) < 2:
            return
        painter.setPen(QPen(QColor(color), 2))
        painter.drawPolyline(QPolygonF(points))


class TemperaturePanel(QWidget):
    def __init__(
        self,
        register: Callable[[str, object], object] | None = None,
        controller_factory: Callable[[str], object] | None = None,
        port_lister: Callable[[], list[object]] | None = None,
        initial_port: str | None = None,
        baudrate: int = RD105_BAUDRATE,
        auto_poll: bool = True,
        serial_executor: object | None = None,
    ):
        super().__init__()
        self._external_register = register
        self.controls: dict[str, object] = {}
        self.config = load_config()
        self.initial_port = initial_port
        self.baudrate = baudrate
        self.port_lister = port_lister or self._default_port_lister
        self.controller_factory = controller_factory or self._make_controller
        self.controller = None
        self.auto_poll = auto_poll
        # Blocking serial I/O must not run on the GUI thread (#6/I5). By default we
        # use a threaded executor so a clicked 连接/设温/控温 returns immediately;
        # tests may inject an InlineSerialExecutor for deterministic behavior.
        self._serial_executor = serial_executor if serial_executor is not None else ThreadedSerialExecutor(self)
        # Set once teardown begins (prepare_for_close). Every worker-completion
        # slot early-returns when this is set, so a late cross-thread queued
        # delivery re-dispatched by processEvents during teardown cannot write
        # into a half-destroyed widget tree (mirrors objective_panel pattern).
        self._closing = False
        self._poll_in_flight = False
        self._connect_done: Event | None = None
        self._disconnect_in_flight = False
        # BP2 panel side: timestamp when _poll_in_flight was set, so a later timer
        # tick can detect a STUCK poll (a read that never returned) and treat it as
        # a comm-failure safety event. Injectable clock keeps tests deterministic.
        self._poll_in_flight_since: float | None = None
        self._clock = time.monotonic

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(1000)
        self.monitor_timer.timeout.connect(self.poll_temperature_once)

        self.setObjectName("temperaturePanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        from .i18n import translator, Retranslator, tr
        self._tr = tr
        self._rt = Retranslator(translator)
        self._build_ui()
        self.refresh_ports()

    def _register(self, name: str, widget):
        widget.setObjectName(name)
        self.controls[name] = widget
        if self._external_register is not None:
            self._external_register(name, widget)
        return widget

    def _make_controller(self, port: str):
        return TemperatureController(
            port=port,
            baudrate=self.baudrate,
            pid_profiles=self.config["pid_profiles"],
        )

    @staticmethod
    def _default_port_lister() -> list[object]:
        if list_ports is None:
            return []
        return list(list_ports.comports())

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = self._register("tempPanelTitle", QLabel())
        self._rt.text(title, "温度控制")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #142033;")
        subtitle = QLabel("RD105")
        subtitle.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        subtitle.setStyleSheet("color: #64748b; font-weight: 700;")
        header.addWidget(title)
        header.addWidget(subtitle, 1)
        root.addLayout(header)

        # Prominent heating-hazard banner, pinned at the top of the panel so it is
        # impossible to miss before touching any RD105 control. High-contrast red so
        # it reads as a hazard, not a normal status line.
        warning = self._register(
            "tempHeatWarning",
            QLabel(),
        )
        self._rt.text(warning, "⚠ 高温警告：加热时请勿触碰加热台，谨防烫伤；离开或退出前务必关闭输出（ENABLE=0）")
        warning.setObjectName("tempHeatWarning")
        warning.setWordWrap(True)
        warning.setAlignment(Qt.AlignCenter)
        warning.setStyleSheet(
            "QLabel#tempHeatWarning{background:#b42318;color:#ffffff;font-weight:800;"
            "border:1px solid #7a1711;border-radius:6px;padding:6px 10px;}"
        )
        # Only shown while the heater output is actually energized (control status
        # 输出开启, or 超温保护-关闭输出失败 where the disable failed and it is still on).
        # Hidden at rest so the banner means "live hazard right now", not boilerplate.
        warning.hide()
        root.addWidget(warning)

        root.addWidget(self._build_connection_group())
        root.addWidget(self._build_status_group())
        root.addWidget(self._build_control_group())
        root.addWidget(self._build_plot_group(), 1)

        status = self._register("tempStatusLabel", QLabel())
        self._rt.text(status, "RD105 未连接")
        status.setAlignment(Qt.AlignCenter)
        status.setStyleSheet("color: #175cd3; font-weight: 700;")
        root.addWidget(status)

        self.setStyleSheet(
            """
            #temperaturePanel {
                background: #f8fafc;
                border: 1px solid #dfe6ee;
                border-radius: 8px;
            }
            QGroupBox {
                font-weight: 700;
                border: 1px solid #dde5ef;
                border-left: 5px solid #b54708;
                border-radius: 8px;
                margin-top: 10px;
                padding: 8px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #334154;
            }
            QPushButton {
                min-height: 28px;
                border: 1px solid #cbd6e2;
                border-radius: 6px;
                background: #edf2f7;
                color: #223044;
                font-weight: 700;
                padding: 3px 8px;
            }
            QPushButton:pressed {
                background: #d3deea;
                border-color: #90a4ba;
                padding-top: 4px;
                padding-bottom: 2px;
            }
            QPushButton#tempConnect,
            QPushButton#tempFastSet {
                background: #1f6f5b;
                border-color: #1f6f5b;
                color: #ffffff;
            }
            QPushButton#tempConnect:pressed,
            QPushButton#tempFastSet:pressed {
                background: #155d4c;
                border-color: #155d4c;
            }
            QPushButton#tempStopHeating {
                background: #fff1f0;
                border-color: #efb4af;
                color: #9f2a22;
            }
            QPushButton#tempStopHeating:pressed {
                background: #ffd8d5;
                border-color: #dc817b;
                color: #7f1d1d;
            }
            QLabel[readout="true"] {
                color: #1f3350;
                background: #f1f5f9;
                border-radius: 6px;
                padding: 5px 7px;
                font-weight: 700;
                min-height: 22px;
            }
            """
        )

    def _build_connection_group(self) -> QGroupBox:
        group = self._register("tempConnectionGroup", QGroupBox())
        self._rt.title(group, "连接")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        port_combo = self._register("tempPortCombo", QComboBox())
        refresh = self._register("tempRefreshPorts", QPushButton())
        self._rt.text(refresh, "刷新端口")
        connect = self._register("tempConnect", QPushButton())
        self._rt.text(connect, "开始连接")
        connection = self._make_readout("tempConnectionLabel", "")
        self._rt.text(connection, "等待接入")

        refresh.clicked.connect(self.refresh_ports)
        connect.clicked.connect(self.toggle_connection)

        com_label = QLabel()
        self._rt.text(com_label, "COM 端口")
        conn_label = QLabel()
        self._rt.text(conn_label, "连接状态")
        layout.addWidget(com_label, 0, 0)
        layout.addWidget(port_combo, 0, 1)
        layout.addWidget(refresh, 1, 0)
        layout.addWidget(connect, 1, 1)
        layout.addWidget(conn_label, 2, 0)
        layout.addWidget(connection, 2, 1)
        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox()
        self._rt.title(group, "当前状态")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        self._add_translated_readout_row(layout, 0, "当前温度", "tempCurrentTempLabel", "--.-- °C")
        self._add_translated_readout_row(layout, 1, "当前给定", "tempLiveSetpointLabel", "--.-- °C")
        self._add_translated_readout_row(layout, 2, "目标温度", "tempTargetTempLabel", "--.-- °C")
        self._add_translated_readout_row(layout, 3, "输出 (PWM)", "tempOutputLabel", "-- PWM")
        self._add_translated_readout_row(layout, 4, "最大输出占空比", "tempMaxOutputDutyLabel", "-- %")
        self._add_translated_readout_row(layout, 5, "控制状态", "tempRunStateLabel", "等待接入", translate_value=True)
        self._add_translated_readout_row(layout, 6, "错误码", "tempErrorCodeLabel", "--")
        return group

    def _build_control_group(self) -> QGroupBox:
        group = QGroupBox()
        self._rt.title(group, "控制")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        target = self._register("tempTargetSpin", QDoubleSpinBox())
        target.setRange(MIN_TARGET_TEMPERATURE, MAX_TARGET_TEMPERATURE)
        target.setDecimals(2)
        target.setSingleStep(1.0)
        target.setValue(DEFAULT_TARGET_TEMPERATURE)
        target.setSuffix(" °C")
        target.setButtonSymbols(QAbstractSpinBox.UpDownArrows)

        rate = self._register("tempRateSpin", QDoubleSpinBox())
        rate.setRange(0.0, MAX_HEATING_RATE_C_PER_HOUR)
        rate.setDecimals(1)
        rate.setSingleStep(1.0)
        rate.setValue(10.0)
        rate.setSuffix(" °C/h")
        rate.setButtonSymbols(QAbstractSpinBox.UpDownArrows)

        fast = self._register("tempFastSet", QPushButton())
        self._rt.text(fast, "快速设温")
        ramp = self._register("tempRamp", QPushButton())
        self._rt.text(ramp, "按速率控温")
        stop = self._register("tempStopHeating", QPushButton())
        self._rt.text(stop, "关闭输出")

        fast.clicked.connect(self.apply_fast_set)
        ramp.clicked.connect(self.apply_rate_set)
        stop.clicked.connect(self.stop_heating)

        target_label = QLabel()
        self._rt.text(target_label, "目标温度")
        rate_label = QLabel()
        self._rt.text(rate_label, "控温速率")
        layout.addWidget(target_label, 0, 0)
        layout.addWidget(target, 0, 1)
        layout.addWidget(fast, 1, 0)
        layout.addWidget(ramp, 1, 1)
        layout.addWidget(rate_label, 2, 0)
        layout.addWidget(rate, 2, 1)
        layout.addWidget(stop, 3, 0, 1, 2)
        return group

    def _build_plot_group(self) -> QGroupBox:
        group = QGroupBox()
        self._rt.title(group, "温度曲线")
        layout = QVBoxLayout(group)
        plot = self._register("tempPlot", TemperaturePlotWidget())
        label = self._register("tempPlotStatusLabel", QLabel())
        self._rt.text(label, "等待实时读数")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #64748b; font-weight: 700;")
        layout.addWidget(plot)
        layout.addWidget(label)
        return group

    def _add_readout_row(self, layout: QGridLayout, row: int, caption: str, name: str, text: str) -> None:
        layout.addWidget(QLabel(caption), row, 0)
        layout.addWidget(self._make_readout(name, text), row, 1)

    def _add_translated_readout_row(
        self, layout: QGridLayout, row: int, caption_zh: str, name: str, text: str, translate_value: bool = False
    ) -> None:
        cap = QLabel()
        self._rt.text(cap, caption_zh)
        layout.addWidget(cap, row, 0)
        readout = self._make_readout(name, text)
        if translate_value:
            self._rt.text(readout, text)
        layout.addWidget(readout, row, 1)

    def _make_readout(self, name: str, text: str) -> QLabel:
        label = self._register(name, QLabel(text))
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setProperty("readout", True)
        return label

    def refresh_ports(self) -> None:
        ports = self.port_lister()
        combo = self.controls["tempPortCombo"]
        current = combo.currentText() or self.initial_port
        preferred = select_preferred_port(
            ports,
            last_port=self.initial_port or self.config.get("last_port"),
            current_port=current,
        )
        devices = [port.device for port in ports]
        if preferred and preferred not in devices:
            devices.insert(0, preferred)

        combo.blockSignals(True)
        combo.clear()
        combo.addItems(devices or [DEFAULT_TEMPERATURE_CONTROLLER_PORT])
        if preferred:
            index = combo.findText(preferred)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._set_status(self._tr("RD105 端口已刷新"))

    def toggle_connection(self) -> None:
        if self._disconnect_in_flight:
            return
        if self.controller is not None and getattr(self.controller, "is_connected", False):
            self.disconnect_from_device()
        else:
            self.connect_to_device()

    def connect_to_device(self) -> bool:
        if self._closing or self._connect_done is not None:
            return False
        if self._disconnect_in_flight:
            self._set_status(self._tr("RD105 正在断开连接，请稍候"))
            return False
        port = self.controls["tempPortCombo"].currentText().strip() or DEFAULT_TEMPERATURE_CONTROLLER_PORT
        controller = self.controller_factory(port)
        self.controller = controller
        connected = self._connect_done = Event()
        self._set_status(self._tr("RD105 正在连接：{port}").format(port=port))

        def connect():
            try:
                return controller.connect()
            finally:
                connected.set()

        def finish(success):
            if self._connect_done is connected:
                self._connect_done = None
                if not self._slots_disabled():
                    self._handle_connect_result(port, success)

        # Blocking serial connect runs off the GUI thread (#6/I5); the click
        # returns immediately and the result lands in the slots below.
        self._connect_result = False
        self._serial_executor.submit(
            "connect",
            connect,
            lambda kind, result: finish(result),
            lambda kind, message: finish(False),
        )
        return self._connect_result

    def _handle_connect_result(self, port: str, success: bool) -> None:
        controller = self.controller
        if not success:
            error = getattr(controller, "last_error", "") or self._tr("连接失败")
            self.controls["tempConnectionLabel"].setText(self._tr("连接失败"))
            self._set_status(f"RD105 {error}")
            self._connect_result = False
            return

        self.config["last_port"] = port
        save_config(self.config)
        self.controls["tempConnectionLabel"].setText(self._tr("已连接 {port}").format(port=port))
        self.controls["tempConnect"].setText(self._tr("断开连接"))
        self._set_status(self._tr("RD105 已连接：{port}").format(port=port))
        self._connect_result = True
        if self.auto_poll:
            self.monitor_timer.start()
        self.poll_temperature_once()

    def prepare_for_close(self, timeout_ms: int = 5000) -> bool:
        """Prepare the panel for window close (mirrors objective_panel pattern).

        1. Sets ``_closing`` so any late queued worker-completion signal is a
           no-op rather than writing into a partially-destroyed widget tree.
        2. Bounded-waits the executor for in-flight threads to finish.
        3. For any thread still running after the wait: detaches it (reparent
           to None, keep reference, quit) so the panel's destruction cannot
           delete a running QThread → std::terminate.

        Returns True if every worker reached idle within the timeout.
        The caller (closeEvent) still runs disconnect_from_device() afterward to
        turn the heater off; that path is unchanged.
        """
        # Set the closing flag FIRST so any queued completion signal that fires
        # during processEvents inside the wait is already a no-op.
        self._closing = True
        executor = self._serial_executor
        all_idle = True
        if hasattr(executor, "wait_for_idle"):
            executor.wait_for_idle(timeout_ms)
            if hasattr(executor, "is_busy") and executor.is_busy():
                all_idle = False
        # Detach threads still running after the wait so the panel's destruction
        # cannot delete a live QThread (std::terminate safety net).
        if hasattr(executor, "detach_running_threads"):
            executor.detach_running_threads()
            if any(thread.isRunning() for thread in getattr(executor, "_detached_threads", ())):
                all_idle = False
        return all_idle

    def disconnect_from_device(self) -> None:
        self.monitor_timer.stop()
        self._poll_in_flight = False
        self._poll_in_flight_since = None
        controller = self.controller
        if controller is None:
            if not self._slots_disabled():
                self.controls["tempConnectionLabel"].setText(self._tr("已断开"))
                self.controls["tempConnect"].setText(self._tr("开始连接"))
                self.controls["tempConnect"].setEnabled(True)
                self._set_status(self._tr("RD105 已断开"))
            return
        if self._disconnect_in_flight:
            return
        self._disconnect_in_flight = True
        connecting, self._connect_done = self._connect_done, None

        def disconnect():
            # An already submitted open must not resurrect the port after exit.
            if connecting is not None:
                connecting.wait()
            controller.disconnect()

        if not self._slots_disabled():
            self.controls["tempConnect"].setEnabled(False)
            self.controls["tempConnectionLabel"].setText(self._tr("正在断开"))
            self._set_status(self._tr("RD105 正在断开连接（先关闭输出）"))
        self._serial_executor.submit(
            "disconnect",
            disconnect,
            lambda kind, _result: self._handle_disconnect_result(controller),
            lambda kind, message: self._handle_disconnect_failure(controller, message),
        )

    def _handle_disconnect_result(self, controller) -> None:
        self._disconnect_in_flight = False
        self._poll_in_flight = False
        self._poll_in_flight_since = None
        if self.controller is controller:
            self.controller = controller
        if self._slots_disabled():
            return
        last_error = getattr(controller, "last_error", "") or ""
        disable_failed = bool(getattr(controller, "disconnect_disable_failed", False))
        # Keep the hazard banner up if disconnect could NOT turn the output off
        # (still energized); otherwise the heater is off, so hide it. This MUST use a
        # language-independent flag, not a substring of the (translated) last_error.
        self._apply_heat_warning("关闭输出失败" if disable_failed else "输出关闭")
        self.controls["tempConnectionLabel"].setText(self._tr("已断开（关闭输出失败）") if disable_failed else self._tr("已断开"))
        self.controls["tempConnect"].setText(self._tr("开始连接"))
        self.controls["tempConnect"].setEnabled(True)
        self._set_status(f"RD105 {last_error}" if disable_failed else self._tr("RD105 已断开"))

    def _handle_disconnect_failure(self, controller, message: str) -> None:
        self._disconnect_in_flight = False
        self._poll_in_flight = False
        self._poll_in_flight_since = None
        if self._slots_disabled():
            return
        self.controls["tempConnectionLabel"].setText(self._tr("断开失败"))
        self.controls["tempConnect"].setText(self._tr("断开连接"))
        self.controls["tempConnect"].setEnabled(True)
        self._set_status(self._tr("RD105 断开失败：{message}").format(message=message), sticky=4)

    def poll_temperature_once(self) -> None:
        if self.controller is None or not getattr(self.controller, "is_connected", False):
            return
        if self._poll_in_flight:
            # BP2: a poll is still outstanding. Normally we drop the overlapping
            # poll so the 1s timer never stacks blocking reads. But if THIS read has
            # been outstanding far too long it is a STUCK poll (a read that never
            # returned) — a comm-failure safety event. The controller watchdog is
            # what actually forces the heater off; here we surface a prominent alarm
            # and clear the flag so polling can recover (no permanent deadlock).
            since = self._poll_in_flight_since
            if since is not None and (self._clock() - since) > POLL_STUCK_TIMEOUT_SECONDS:
                self._set_status(
                    self._tr("RD105 通信丢失或读取卡死——温度轮询无响应，请检查连接（加热输出由看门狗保护）"),
                    sticky=4,
                )
                # Recover: clear the in-flight latch so the NEXT timer tick can
                # dispatch a fresh read. We do NOT dispatch here — a still-hung
                # worker may yet call back, and stacking a second read on top would
                # defeat the overlap guard. The 1s timer retries promptly.
                self._poll_in_flight = False
                self._poll_in_flight_since = None
            return
        controller = self.controller
        self._poll_in_flight = True
        self._poll_in_flight_since = self._clock()
        self._serial_executor.submit(
            "poll",
            controller.read_live_data,
            self._handle_poll_result,
            self._handle_poll_failure,
        )

    def _slots_disabled(self) -> bool:
        """True when the panel is tearing down; late worker signals must no-op."""
        return self._closing or not self.controls

    def _handle_poll_result(self, kind: str, snapshot: object) -> None:
        self._poll_in_flight = False
        self._poll_in_flight_since = None
        if self._slots_disabled():
            return
        if snapshot is None:
            error = getattr(self.controller, "last_error", "") or self._tr("读取失败")
            self._set_status(f"RD105 {error}")
            return
        self.on_temperature_snapshot(snapshot)

    def _handle_poll_failure(self, kind: str, message: str) -> None:
        self._poll_in_flight = False
        self._poll_in_flight_since = None
        if self._slots_disabled():
            return
        self._set_status(f"RD105 {message}")

    @staticmethod
    def _output_energized(run_state) -> bool:
        """True when the RD105 heater output is currently ON (a live burn hazard).

        "输出开启" is the normal output-on state; "超温保护-关闭输出失败" means the
        over-temperature guard tried to cut output but the disable command failed, so
        it is still energized — also a hazard. "输出关闭"/"超温保护"/未知 are off."""
        text = str(run_state or "")
        return text == "输出开启" or "关闭输出失败" in text

    def _apply_heat_warning(self, run_state) -> None:
        warning = self.controls.get("tempHeatWarning")
        if warning is not None:
            warning.setVisible(self._output_energized(run_state))

    def on_temperature_snapshot(self, snapshot: dict[str, object]) -> None:
        if self._slots_disabled():
            return
        self.controls["tempCurrentTempLabel"].setText(self._format_temperature(snapshot.get("temperature")))
        self.controls["tempLiveSetpointLabel"].setText(self._format_temperature(snapshot.get("effective_setpoint")))
        self.controls["tempOutputLabel"].setText(self._format_output_pwm(snapshot.get("output_percent")))
        max_output_duty = snapshot.get("max_output_duty_percent")
        self.controls["tempMaxOutputDutyLabel"].setText(self._format_percent(max_output_duty))
        run_state = snapshot.get("run_state")
        self.controls["tempRunStateLabel"].setText(self._tr(str(run_state or "未知")))
        self._apply_heat_warning(run_state)
        error_code = snapshot.get("error_code")
        self.controls["tempErrorCodeLabel"].setText("--" if error_code is None else str(error_code))
        self._update_temperature_plot(snapshot)
        safety_alarm = snapshot.get("safety_alarm")
        control_error = snapshot.get("control_error")
        if safety_alarm:
            self._set_status(f"RD105 {safety_alarm}")
        elif control_error:
            self._set_status(self._tr("RD105 控温错误：{control_error}").format(control_error=control_error))
        elif snapshot.get("software_ramp_active"):
            final_target = self._format_temperature(snapshot.get("software_ramp_final_target"))
            rate = self._format_rate(snapshot.get("software_ramp_rate_per_hour"))
            self._set_status(self._tr("RD105 软件斜坡控温中：目标 {final_target}，速率 {rate} °C/h").format(final_target=final_target, rate=rate))
        else:
            # Don't let the 1 Hz steady-reading message overwrite a recent command
            # failure that was marked sticky; alarm/control-error/ramp branches above
            # always win because they re-assert real state every poll.
            remaining = getattr(self, "_status_sticky_polls", 0)
            if remaining > 0:
                self._status_sticky_polls = remaining - 1
            else:
                self._set_status(self._tr("RD105 实时读数已更新"))

    def _update_temperature_plot(self, snapshot: dict[str, object]) -> None:
        plot = self.controls.get("tempPlot")
        if isinstance(plot, TemperaturePlotWidget):
            plot.add_snapshot(snapshot)
        temperature = snapshot.get("temperature")
        setpoint = snapshot.get("effective_setpoint")
        self.controls["tempPlotStatusLabel"].setText(
            self._tr("当前 {cur} / 给定 {sp}").format(cur=self._format_temperature(temperature), sp=self._format_temperature(setpoint))
        )

    def _confirm(self, title: str, text: str) -> bool:
        """Show a Yes/No confirmation dialog. Monkeypatchable for tests."""
        from PySide6.QtWidgets import QMessageBox
        result = QMessageBox.question(self, title, text)
        return result == QMessageBox.Yes

    def apply_fast_set(self) -> bool:
        controller = self._require_controller()
        if controller is None:
            return False
        target = float(self.controls["tempTargetSpin"].value())
        if target >= WARN_CONFIRM_TEMPERATURE:
            if not self._confirm(
                self._tr("确认高温设定"),
                self._tr("目标温度 {target:.2f} °C 超过 {warn:g} °C，请确认操作安全后再发送。确定设温？").format(target=target, warn=WARN_CONFIRM_TEMPERATURE),
            ):
                return False
        self._set_status(self._tr("RD105 正在发送快速设温"), flush=True)
        # Blocking set runs off the GUI thread (#6/I5).
        self._command_result = False
        self._serial_executor.submit(
            "fast_set",
            lambda: controller.fast_set_temperature(target),
            lambda kind, ok: self._handle_command_result("fast_set", controller, target, ok),
            lambda kind, message: self._handle_command_failure("fast_set", controller, message),
        )
        return self._command_result

    def apply_rate_set(self) -> bool:
        controller = self._require_controller()
        if controller is None:
            return False
        target = float(self.controls["tempTargetSpin"].value())
        rate = float(self.controls["tempRateSpin"].value())
        if target >= WARN_CONFIRM_TEMPERATURE:
            if not self._confirm(
                self._tr("确认高温设定"),
                self._tr("目标温度 {target:.2f} °C 超过 {warn:g} °C，请确认操作安全后再发送。确定设温？").format(target=target, warn=WARN_CONFIRM_TEMPERATURE),
            ):
                return False
        self._set_status(self._tr("RD105 正在发送按速率控温"), flush=True)
        self._command_result = False
        self._serial_executor.submit(
            "rate_set",
            lambda: controller.ramp_to_temperature(target, rate),
            lambda kind, ok: self._handle_command_result("rate_set", controller, target, ok),
            lambda kind, message: self._handle_command_failure("rate_set", controller, message),
        )
        return self._command_result

    def stop_heating(self) -> bool:
        controller = self._require_controller()
        if controller is None:
            return False
        self._set_status(self._tr("RD105 正在关闭输出"), flush=True)
        self._command_result = False
        self._serial_executor.submit(
            "stop",
            controller.stop_heating,
            lambda kind, ok: self._handle_command_result("stop", controller, None, ok),
            lambda kind, message: self._handle_command_failure("stop", controller, message),
        )
        return self._command_result

    _COMMAND_LABELS = {
        "fast_set": ("快速设温", "快速设温已发送"),
        "rate_set": ("按速率控温", "按速率控温已发送"),
        "stop": ("关闭输出", "输出已关闭"),
    }

    def _handle_command_result(self, kind: str, controller, target, ok: bool) -> None:
        if self._slots_disabled():
            return
        action_name, success_message = self._COMMAND_LABELS[kind]
        if not ok:
            err = getattr(controller, 'last_error', '')
            self._set_status(self._tr("RD105 {action}失败：{err}").format(action=self._tr(action_name), err=err), sticky=4)
            self._command_result = False
            return
        self._command_result = True
        if kind == "stop":
            self.controls["tempRunStateLabel"].setText(self._tr("输出关闭"))
            self._apply_heat_warning("输出关闭")
            self._set_status(self._tr("RD105 输出已关闭"))
            self.poll_temperature_once()
            return
        self._after_temperature_command(target, success_message)

    def _handle_command_failure(self, kind: str, controller, message: str) -> None:
        if self._slots_disabled():
            return
        action_name, _ = self._COMMAND_LABELS[kind]
        self._set_status(self._tr("RD105 {action}失败：{message}").format(action=self._tr(action_name), message=message), sticky=4)
        self._command_result = False

    def _require_controller(self):
        if self.controller is None or not getattr(self.controller, "is_connected", False):
            self._set_status(self._tr("RD105 未连接，不能发送温控命令"))
            return None
        return self.controller

    def _after_temperature_command(self, target: float, message: str) -> None:
        self.controls["tempTargetTempLabel"].setText(self._format_temperature(target))
        self._set_status(self._tr(message))
        self.poll_temperature_once()

    def _set_status(self, text: str, flush: bool = False, sticky: int = 0) -> None:
        self.controls["tempStatusLabel"].setText(text)
        if sticky:
            # Keep this message for the next `sticky` steady-reading polls so a
            # one-shot command failure is not wiped within ~1s by "实时读数已更新".
            self._status_sticky_polls = sticky
        # ``flush`` is kept for call-site readability: the label is updated
        # synchronously by setText(), and serial work now runs on workers. Do not
        # force QApplication.processEvents() here; re-entering the event loop from
        # a command button can dispatch unrelated late worker callbacks.

    @staticmethod
    def _format_temperature(value: object) -> str:
        if value is None:
            return "--.-- °C"
        return f"{float(value):.2f} °C"

    @staticmethod
    def _format_percent(value: object) -> str:
        if value is None:
            return "-- %"
        return f"{float(value):.1f} %"

    @staticmethod
    def _format_output_pwm(value: object) -> str:
        # Bug #10: the RD105 PWM full-scale is UNDOCUMENTED in this repo, so the
        # underlying value (PWM_raw / 20000.0) is NOT a confirmed percentage. Render
        # it with a distinct "PWM" marker so it cannot be visually confused with the
        # adjacent max-output-duty percent. The numeric value itself is unchanged.
        if value is None:
            return "-- PWM"
        return f"{float(value):.1f} PWM"

    @staticmethod
    def _format_rate(value: object) -> str:
        if value is None:
            return "--"
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return "--"
