from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .nosepiece_controller import (
    DEFAULT_NOSEPIECE_PORT,
    NosepieceController,
    NosepieceError,
    OBJECTIVE_POSITION_BY_LABEL,
)

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - serial is optional for tests and previews
    list_ports = None

try:
    import serial

    _SERIAL_EXCEPTION: type[BaseException] = serial.SerialException
except Exception:  # pragma: no cover - serial is optional for tests and previews
    serial = None

    class _SerialExceptionUnavailable(Exception):
        """Placeholder so ``except _SERIAL_EXCEPTION`` is always valid even
        when pyserial is not installed (test/preview environments)."""

    _SERIAL_EXCEPTION = _SerialExceptionUnavailable


LABEL_BY_POSITION = {position: label for label, position in OBJECTIVE_POSITION_BY_LABEL.items()}


class ObjectiveConnectThread(QThread):
    """Opens the serial connection (and reads the initial position) off the GUI
    thread so a wrong/misconfigured port cannot freeze the UI for up to 8s."""

    succeeded = Signal(object, int)
    failed = Signal(str)

    def __init__(self, controller: NosepieceController, port: str, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.port = port

    def run(self) -> None:
        try:
            self.controller.connect()
        except (NosepieceError, _SERIAL_EXCEPTION) as exc:
            self.failed.emit(str(exc))
            return
        try:
            position = self.controller.read_position()
        except (NosepieceError, _SERIAL_EXCEPTION):
            position = -1
        self.succeeded.emit(self.controller, position)


class ObjectiveReadThread(QThread):
    """Reads the current objective position off the GUI thread."""

    succeeded = Signal(int)
    failed = Signal(str)

    def __init__(self, controller: NosepieceController, parent=None):
        super().__init__(parent)
        self.controller = controller

    def run(self) -> None:
        try:
            position = self.controller.read_position()
        except (NosepieceError, _SERIAL_EXCEPTION) as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(position)


class ObjectiveMoveThread(QThread):
    succeeded = Signal(str, int)
    failed = Signal(str)

    def __init__(self, nosepiece_controller: NosepieceController, label: str, position: int, parent=None):
        super().__init__(parent)
        self.nosepiece_controller = nosepiece_controller
        self.label = label
        self.position = position

    def run(self) -> None:
        try:
            self.nosepiece_controller.move_to_position(self.position)
        except (NosepieceError, ValueError, _SERIAL_EXCEPTION) as exc:
            # Mirror the connect/read threads: also catch serial-layer errors so a
            # port closed out from under an in-flight move (e.g. at app shutdown)
            # cannot let an exception escape the QThread and abort dirtily.
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self.label, self.position)


class ObjectivePanel(QGroupBox):
    def __init__(
        self,
        nosepiece_controller: NosepieceController,
        register: Callable[[str, object], object] | None = None,
        status_callback: Callable[[str], None] | None = None,
        controller_factory: Callable[[str], NosepieceController] | None = None,
        port_lister: Callable[[], list[object]] | None = None,
        initial_port: str | None = None,
    ):
        super().__init__("")
        self.nosepiece_controller = nosepiece_controller
        self._external_register = register
        self._status_callback = status_callback
        self.controller_factory = controller_factory
        self.port_lister = port_lister or self._default_port_lister
        # Pre-select COM7 by default so the operator doesn't have to type it; an
        # explicit --nosepiece-port still overrides via the passed initial_port.
        self.initial_port = initial_port or DEFAULT_NOSEPIECE_PORT
        self.controls: dict[str, object] = {}
        self.objective_buttons: dict[int, QPushButton] = {}
        self._move_thread: ObjectiveMoveThread | None = None
        self._connect_thread: ObjectiveConnectThread | None = None
        self._read_thread: ObjectiveReadThread | None = None
        self._previous_controller: object | None = None
        # Set once teardown/disconnect begins. Every worker-completion slot
        # early-returns when this is set, so a late cross-thread queued delivery
        # re-dispatched by processEvents during teardown cannot write into a
        # half-destroyed widget tree (Fix 1).
        self._closing = False
        # Holds QThreads that were still running when the window closed. They are
        # reparented to None (so the panel's destruction will not delete a running
        # QThread → std::terminate) yet kept referenced here so Python/Qt do not
        # GC them mid-run (Fix 2).
        self._detached_threads: list[QThread] = []
        self.setObjectName("objectivePanel")
        from .i18n import translator, Retranslator, tr
        self._tr = tr
        self._rt = Retranslator(translator)
        self._build_ui()

    @staticmethod
    def _default_port_lister() -> list[object]:
        if list_ports is None:
            return []
        return list(list_ports.comports())

    def _register(self, name: str, widget):
        widget.setObjectName(name)
        self.controls[name] = widget
        if self._external_register is not None:
            self._external_register(name, widget)
        return widget

    def _build_ui(self) -> None:
        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(6)

        title = self._register("objectiveTitleLabel", QLabel(self._tr("物镜选择")))
        title.setMinimumHeight(18)
        title.setStyleSheet("color: #2e3a47; font-size: 14px; font-weight: 800;")
        self._rt.text(title, "物镜选择")
        layout.addWidget(title, 0, 0, 1, 4)
        com_button = self._register("btnObjectiveCom", QPushButton("COM"))
        com_button.clicked.connect(self.open_com_dialog)
        layout.addWidget(com_button, 0, 4)

        link = self._register("objectiveLinkLabel", QLabel(""))
        link.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(link, 1, 0, 1, 5)

        status = self._register("objectiveStatusLabel", QLabel(self._tr("当前物镜: 未读取")))
        status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(status, 2, 0, 1, 5)

        for col, (label, position) in enumerate(OBJECTIVE_POSITION_BY_LABEL.items()):
            button = self._register(f"btnObjective{label}", QPushButton(label))
            button.setProperty("selectedObjective", False)
            button.clicked.connect(lambda checked=False, label=label, position=position: self.select_objective(label, position))
            self.objective_buttons[position] = button
            layout.addWidget(button, 3, col)

        self._set_link_state(
            getattr(self.nosepiece_controller, "is_open", False),
            getattr(self.nosepiece_controller, "port", "") or "",
        )

        self.setStyleSheet(
            """
            #objectivePanel {
                border: 1px solid #dde5ef;
                border-left: 5px solid #9a3d7a;
                border-radius: 8px;
                padding: 8px;
                background: #ffffff;
                font-weight: 700;
            }
            #objectivePanel QPushButton {
                min-height: 30px;
                border: 1px solid #cbd6e2;
                border-radius: 6px;
                background: #edf2f7;
                color: #223044;
                font-weight: 800;
            }
            #objectivePanel QPushButton:pressed {
                background: #d3deea;
                border-color: #90a4ba;
                padding-top: 4px;
                padding-bottom: 2px;
            }
            #objectivePanel QPushButton#btnObjectiveCom {
                min-height: 22px;
                padding: 2px 8px;
                background: #ffffff;
                color: #243140;
            }
            #objectivePanel QPushButton[selectedObjective="true"] {
                background: #9a3d7a;
                border-color: #9a3d7a;
                color: #ffffff;
            }
            #objectivePanel QPushButton[selectedObjective="true"]:pressed {
                background: #7a2d60;
                border-color: #7a2d60;
            }
            #objectivePanel QPushButton:disabled {
                background: #e8ecf0;
                color: #9aa8b6;
                border-color: #cdd5dd;
            }
            #objectiveStatusLabel {
                color: #52606e;
                font-weight: 700;
            }
            """
        )

    def open_com_dialog(self) -> None:
        existing = self.controls.get("objectiveComDialog")
        if isinstance(existing, QDialog):
            existing.close()
            existing.deleteLater()

        dialog = self._register("objectiveComDialog", QDialog(self))
        dialog.setWindowTitle(self._tr("物镜 COM 设置"))
        dialog.setModal(False)
        dialog.setMinimumWidth(320)
        dialog.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #334154;
                font-weight: 700;
            }
            QComboBox {
                background: #ffffff;
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                padding: 5px 7px;
                color: #12345a;
            }
            QPushButton {
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                background: #eef4fa;
                padding: 6px 10px;
                font-weight: 700;
                color: #142033;
            }
            QPushButton#objectiveConnect {
                background: #0b8067;
                color: #ffffff;
                border-color: #0b8067;
            }
            QLabel#objectiveComStatusLabel {
                color: #526174;
                background: #f1f5f9;
                border-radius: 6px;
                padding: 5px 7px;
            }
            """
        )

        root = QVBoxLayout(dialog)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        port_row = QGridLayout()
        combo = self._register("objectivePortCombo", QComboBox())
        refresh = self._register("objectiveRefreshPorts", QPushButton(self._tr("刷新端口")))
        self._rt.text(refresh, "刷新端口")
        connect = self._register("objectiveConnect", QPushButton(self._tr("连接", context="objective")))
        self._rt.text(connect, "连接", context="objective")
        read = self._register("objectiveReadPosition", QPushButton(self._tr("读取位置")))
        self._rt.text(read, "读取位置")
        status = self._register("objectiveComStatusLabel", QLabel(self._tr("物镜串口未连接")))
        port_row.addWidget(QLabel(self._tr("COM 端口")), 0, 0)
        port_row.addWidget(combo, 0, 1, 1, 2)
        port_row.addWidget(refresh, 1, 0)
        port_row.addWidget(connect, 1, 1)
        port_row.addWidget(read, 1, 2)
        root.addLayout(port_row)
        root.addWidget(status)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = self._register("objectiveComClose", QPushButton(self._tr("关闭")))
        self._rt.text(close, "关闭")
        close.clicked.connect(dialog.close)
        close_row.addWidget(close)
        root.addLayout(close_row)

        refresh.clicked.connect(self.refresh_com_ports)
        connect.clicked.connect(self.toggle_com_connection)
        read.clicked.connect(self.refresh_position)

        self.refresh_com_ports()
        dialog.open()

    def refresh_com_ports(self) -> None:
        if "objectivePortCombo" not in self.controls:
            return
        combo = self.controls["objectivePortCombo"]
        current = combo.currentText() or self.initial_port or getattr(self.nosepiece_controller, "port", None)
        ports = self.port_lister()
        devices = [port.device for port in ports]
        if current and current not in devices:
            devices.insert(0, current)
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(devices or ([current] if current else [DEFAULT_NOSEPIECE_PORT]))
        if current:
            index = combo.findText(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._set_com_status(self._tr("端口已刷新"))

    def toggle_com_connection(self) -> None:
        if getattr(self.nosepiece_controller, "is_open", False):
            self.nosepiece_controller.disconnect()
            self._set_com_status(self._tr("物镜串口已断开"))
            self._set_link_state(False)
            if "objectiveConnect" in self.controls:
                self.controls["objectiveConnect"].setText(self._tr("连接", context="objective"))
            return
        self.connect_selected_port()

    def connect_selected_port(self) -> bool:
        if self.controller_factory is None:
            self._set_com_status(self._tr("未配置物镜串口工厂"))
            return False
        if self._connect_thread is not None and self._connect_thread.isRunning():
            self._set_com_status(self._tr("正在连接，请稍候"))
            return False
        # Refuse to swap controllers while a move/read worker is in flight. The
        # worker holds (and may be writing to) the current controller's port; a
        # swap here would close that port out from under the worker and the new
        # controller could silently reopen the old port via _request (Fix 3).
        if self._move_thread is not None and self._move_thread.isRunning():
            self._set_com_status(self._tr("操作进行中，请稍候"))
            return False
        if self._read_thread is not None and self._read_thread.isRunning():
            self._set_com_status(self._tr("操作进行中，请稍候"))
            return False
        port = self.controls["objectivePortCombo"].currentText().strip()
        controller = self.controller_factory(port)

        # Swap to the new controller up-front so the panel (and the main window's
        # close handler, which reads ``nosepiece_controller`` after wait_for_idle)
        # observes it without waiting for the worker's queued signal. The blocking
        # connect()/read_position() still happen off the GUI thread below.
        self._previous_controller = self.nosepiece_controller
        self._disconnect_previous_controller(self._previous_controller, controller)
        self.nosepiece_controller = controller

        self._set_com_status(self._tr("正在连接 {port} ...").format(port=port))
        self._set_com_controls_enabled(False)

        thread = ObjectiveConnectThread(controller, port, self)
        self._connect_thread = thread
        thread.succeeded.connect(self._complete_connect)
        thread.failed.connect(self._fail_connect)
        thread.finished.connect(self._clear_connect_thread)
        thread.start()
        return True

    def _slots_disabled(self) -> bool:
        # True when the panel is tearing down (or its widgets are gone), so a late
        # worker-completion slot must be a no-op rather than touch dead widgets.
        return self._closing or not self.controls

    def _complete_connect(self, controller: object, position: int) -> None:
        if self._slots_disabled():
            return
        thread = self.sender()
        port = getattr(thread, "port", getattr(controller, "port", ""))
        if "objectiveConnect" in self.controls:
            self.controls["objectiveConnect"].setText(self._tr("断开"))
        self._set_com_status(self._tr("已连接 {port}").format(port=port))
        self._set_link_state(True, str(port))
        if position >= 0:
            self._apply_position(position)

    def _fail_connect(self, error: str) -> None:
        if self._slots_disabled():
            return
        # Roll back to the controller we held before this connect attempt so a
        # failed open does not orphan the panel on a never-connected controller.
        previous = getattr(self, "_previous_controller", None)
        if previous is not None:
            self.nosepiece_controller = previous
        self._set_com_status(self._tr("连接失败: {error}").format(error=error))
        self._set_link_state(
            getattr(self.nosepiece_controller, "is_open", False),
            getattr(self.nosepiece_controller, "port", "") or "",
        )

    def _clear_connect_thread(self) -> None:
        thread = self.sender()
        if self._connect_thread is thread or thread is None:
            self._connect_thread = None
        if self._slots_disabled():
            # Tearing down: do not re-enable / repaint widgets that may be going
            # away. The thread bookkeeping above is still cleared so the panel's
            # references stay consistent.
            return
        self._set_com_controls_enabled(True)
        if isinstance(thread, QThread):
            thread.deleteLater()

    @staticmethod
    def _disconnect_previous_controller(previous: object, controller: object) -> None:
        if previous is not controller and getattr(previous, "is_open", False):
            try:
                previous.disconnect()
            except Exception:  # pragma: no cover - best effort cleanup of orphaned handle
                pass

    def _set_com_status(self, message: str) -> None:
        if "objectiveComStatusLabel" in self.controls:
            self.controls["objectiveComStatusLabel"].setText(message)
        self._emit_status(message)

    def _set_link_state(self, connected: bool, port: str = "") -> None:
        # Persistent connection badge on the always-visible panel so the operator
        # can tell the nosepiece serial link state at a glance (a coloured dot +
        # pill, not just grey text) without opening the COM dialog.
        label = self.controls.get("objectiveLinkLabel")
        if label is None:
            return
        label.setTextFormat(Qt.RichText)
        if connected:
            port_txt = f" {port}".rstrip()
            connected_txt = self._tr("物镜串口: 已连接")
            label.setText(f'<span style="color:#16a34a;">●</span> {connected_txt}{port_txt}')
            label.setStyleSheet(
                "QLabel{color:#0b8067; font-weight:700; background:#e6f7ef;"
                " border:1px solid #16a34a; border-radius:6px; padding:3px 8px;}"
            )
        else:
            disconnected_txt = self._tr("物镜串口: 未连接")
            label.setText(f'<span style="color:#c0392b;">●</span> {disconnected_txt}')
            label.setStyleSheet(
                "QLabel{color:#6b7785; font-weight:700; background:#f5f0f0;"
                " border:1px solid #e0c4c0; border-radius:6px; padding:3px 8px;}"
            )

    def select_objective(self, label: str, position: int) -> None:
        if self._move_thread is not None and self._move_thread.isRunning():
            message = self._tr("物镜切换中，请等待")
            self.controls["objectiveStatusLabel"].setText(message)
            self._emit_status(message)
            return

        message = self._tr("物镜切换中: {label} (孔位 {position})").format(label=label, position=position)
        self.controls["objectiveStatusLabel"].setText(message)
        self._emit_status(message)
        self._set_move_controls_enabled(False)

        thread = ObjectiveMoveThread(self.nosepiece_controller, label, position, self)
        self._move_thread = thread
        thread.succeeded.connect(self._complete_objective_move)
        thread.failed.connect(self._fail_objective_move)
        thread.finished.connect(self._clear_objective_move)
        thread.start()

    def _complete_objective_move(self, label: str, position: int) -> None:
        if self._slots_disabled():
            return
        self._mark_selected(position)
        message = self._tr("当前物镜: {label} (孔位 {position})").format(label=label, position=position)
        self.controls["objectiveStatusLabel"].setText(message)
        self._emit_status(message)

    def _fail_objective_move(self, error: str) -> None:
        if self._slots_disabled():
            return
        message = self._tr("物镜切换失败: {error}").format(error=error)
        self.controls["objectiveStatusLabel"].setText(message)
        self._emit_status(message)

    def _clear_objective_move(self) -> None:
        thread = self.sender()
        if self._move_thread is thread or thread is None:
            self._move_thread = None
        if self._slots_disabled():
            return
        self._set_move_controls_enabled(True)
        if isinstance(thread, QThread):
            thread.deleteLater()

    def refresh_position(self) -> None:
        if self._closing:
            return
        if self._read_thread is not None and self._read_thread.isRunning():
            return
        # Do not read while a move is in flight: the move thread already owns the
        # serial handle, and a concurrent read would interleave on it (Fix 3).
        if self._move_thread is not None and self._move_thread.isRunning():
            message = self._tr("操作进行中，请稍候")
            if "objectiveStatusLabel" in self.controls:
                self.controls["objectiveStatusLabel"].setText(message)
            self._emit_status(message)
            return
        # Do not auto-reopen a deliberately-disconnected port. _request no longer
        # silently reconnects, so without this guard the read would simply fail;
        # surface the disconnected state instead of issuing a doomed read (Fix 5).
        if not getattr(self.nosepiece_controller, "is_open", False):
            message = self._tr("物镜串口未连接")
            self._set_com_status(message)
            return
        controller = self.nosepiece_controller
        thread = ObjectiveReadThread(controller, self)
        self._read_thread = thread
        thread.succeeded.connect(self._apply_position)
        thread.failed.connect(self._fail_read_position)
        thread.finished.connect(self._clear_read_thread)
        thread.start()

    def _apply_position(self, position: int) -> None:
        if self._slots_disabled():
            return
        self._mark_selected(position)
        label = LABEL_BY_POSITION.get(position, self._tr("未知"))
        self.controls["objectiveStatusLabel"].setText(
            self._tr("当前物镜: {label} (孔位 {position})").format(label=label, position=position)
        )

    def _fail_read_position(self, error: str) -> None:
        if self._slots_disabled():
            return
        message = self._tr("物镜位置读取失败: {error}").format(error=error)
        self.controls["objectiveStatusLabel"].setText(message)
        self._emit_status(message)

    def _clear_read_thread(self) -> None:
        thread = self.sender()
        if self._read_thread is thread or thread is None:
            self._read_thread = None
        if self._slots_disabled():
            return
        if isinstance(thread, QThread):
            thread.deleteLater()

    def _set_com_controls_enabled(self, enabled: bool) -> None:
        for name in ("objectiveRefreshPorts", "objectiveConnect", "objectiveReadPosition"):
            control = self.controls.get(name)
            if isinstance(control, QPushButton):
                control.setEnabled(enabled)

    def _set_move_controls_enabled(self, enabled: bool) -> None:
        for button in self.objective_buttons.values():
            button.setEnabled(enabled)
        for name in ("btnObjectiveCom", "objectiveRefreshPorts", "objectiveConnect", "objectiveReadPosition"):
            control = self.controls.get(name)
            if isinstance(control, QPushButton):
                control.setEnabled(enabled)

    def wait_for_idle(self, timeout_ms: int = 20000) -> bool:
        # Returns True only if every worker thread reached idle within the timeout.
        # The caller (window close) uses this to avoid closing the serial port out
        # from under a worker that is still mid-transfer after the timeout.
        all_idle = True
        for thread in (self._move_thread, self._connect_thread, self._read_thread):
            if thread is not None and thread.isRunning():
                if not thread.wait(timeout_ms):
                    all_idle = False
        return all_idle

    def prepare_for_close(self, timeout_ms: int = 20000) -> bool:
        """Prepare the panel for window close.

        Marks the panel as closing (so any late worker-completion signal becomes
        a no-op — Fix 1), bounded-waits for every worker thread to finish, and for
        any thread still running after the wait, DETACHES it: reparent to None so
        the panel's destruction does not delete a still-running QThread (which
        aborts the process via std::terminate), and keep a reference so Python/Qt
        do not GC it mid-run (Fix 2).

        Returns True only if every worker reached idle within the timeout. A False
        return tells the caller (window close) not to close the serial port out
        from under a worker that is still mid-transfer.
        """
        # Set the closing flag FIRST so a queued completion slot re-dispatched by
        # processEvents during teardown is a no-op even if a thread finishes during
        # the wait below.
        self._closing = True
        all_idle = True
        for thread in (self._move_thread, self._connect_thread, self._read_thread):
            if thread is None:
                continue
            if thread.isRunning():
                if not thread.wait(timeout_ms):
                    all_idle = False
            if thread.isRunning():
                # Still running after the bounded wait: detach it so it outlives
                # the panel without being destroyed mid-run. Reparenting to None
                # removes it from the panel's child list (Qt won't delete it when
                # the panel is destroyed); the reference in _detached_threads keeps
                # it alive. quit() asks its event loop (if any) to stop; we never
                # terminate() a running worker.
                self._detach_running_thread(thread)
        return all_idle

    def _detach_running_thread(self, thread: QThread) -> None:
        if thread in self._detached_threads:
            return
        try:
            thread.setParent(None)
        except Exception:  # pragma: no cover - defensive: reparenting must not crash
            pass
        try:
            thread.quit()
        except Exception:  # pragma: no cover - quit on a run()-only thread is a no-op
            pass
        self._detached_threads.append(thread)

    def _mark_selected(self, selected_position: int) -> None:
        for position, button in self.objective_buttons.items():
            button.setProperty("selectedObjective", position == selected_position)
            button.style().unpolish(button)
            button.style().polish(button)

    def _emit_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)
