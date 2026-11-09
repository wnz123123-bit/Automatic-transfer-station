"""Laser standby UI. Only jobs may ARM/fire; this panel never sends keepalive.

Serial ACK/Comp levels describe electronics, never measured optical power.
All I/O and serial enumeration run on daemon workers. A separately retained OFF
worker may preempt ordinary work; the controller owns serial arbitration.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .cutting_widgets import CuttingDisclosure
from .i18n import Retranslator, tr, translator


_ACTIVE_PANELS: set = set()
_UNCONFIRMED_STATES = {"DISCONNECTED", "BOOT_HOLD", "FAULT", "UNKNOWN"}
_STARTUP_CHECK_STATES = {"BOOT_HOLD", "DISARMED"}
_audit = logging.getLogger("ats.audit")


def _default_controller_factory():
    from .laser_controller import LaserController
    return LaserController()


def _default_port_lister():
    from serial.tools import list_ports
    return list(list_ports.comports())


def _state_name(status):
    state = getattr(status, "state", None)
    # Accept Enum or stable protocol identifiers, never translated display text.
    return getattr(state, "name", state) if state is not None else "DISCONNECTED"


class _Signals(QObject):
    done = Signal(str, int, object, object)


class LaserPanel(QWidget):
    """Standalone standby panel with nonblocking shutdown and explicit evidence.

    ``controller`` stays None until an explicitly confirmed connection. Reading
    ``startup_confirmed`` or ``status`` performs no Qt calls or hardware I/O.
    ``busy`` includes operator commands/OFF/shutdown, but not background status
    reads. ``prepare_for_close`` returns False until ALL workers exit, then True
    even if OFF/disconnect failed. Parents must wait for that lifetime barrier
    before deletion and separately report ``shutdown_confirmed`` / ``last_error``.
    Confirmation means protocol cleanup succeeded, never measured optical OFF
    or a known post-disconnect Comp level. Without a controller it remains False.
    A timeout never permits forced destruction or automatic resumption.
    """

    status_changed = Signal(object)
    busy_changed = Signal(bool)
    emergency_requested = Signal()
    shutdown_finished = Signal(bool, object)

    def __init__(self, controller=None, parent=None, *, controller_factory=None, port_lister=None):
        super().__init__(parent)
        self.controller = controller
        self._controller_factory = controller_factory or _default_controller_factory
        self._port_lister = port_lister or _default_port_lister
        self._status = None
        self._connected = False
        self._startup_confirmed = False
        self._job_busy = False
        self._closing = False
        self._close_when_idle = False
        self._close_deadline = None
        self._shutdown_started = False
        self._shutdown_done = False
        self._shutdown_ok = False
        self._last_error = None
        self._epoch = 0
        self._sequence = 0
        self._workers = {}
        self._retained_workers = []
        self._results = {}
        self._last_busy = False
        self._next_monitor = 0.0
        self._confirmation = None
        self._signals = _Signals()  # No QWidget parent: retained by worker closures.
        self._signals.done.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._rt = Retranslator(translator)
        self.setObjectName("laserPanel")
        self._build_ui()
        self._rt.custom(self._render_status)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._message("激光控制未连接；串口只连接 Arduino。")
        self._sync_controls()

    @property
    def busy(self):
        return any(kind != "monitor" for kind in self._workers)

    @property
    def status(self):
        return self._status

    @property
    def shutdown_confirmed(self):
        """Successful OFF/disconnect protocol cleanup, not optical measurement."""
        return (self._shutdown_done and self._shutdown_ok and not self._workers
                and not self._has_serial_resource())

    @property
    def last_error(self):
        return self._last_error

    @property
    def startup_confirmed(self):
        return (self._startup_confirmed and self._connected and not self._closing
                and getattr(self._status, "startup_checked", False) is True
                and _state_name(self._status) not in _UNCONFIRMED_STATES
                and not getattr(self._status, "fault", None))

    def _label(self, text=None):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if text:
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

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # OFF is first and never placed inside a local scroll area or disabled.
        self._button(layout, "off_button", "OFF / 急停", self._user_emergency_off)
        self.off_button.setStyleSheet("QPushButton { background: #a52525; color: white; padding: 8px; font-weight: bold; }")
        ports = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setObjectName("laserPortCombo")
        self.port_combo.setEditable(True)
        self.port_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._rt.placeholder(self.port_combo.lineEdit(), "选择 Arduino 串口")
        ports.addWidget(self.port_combo, 1)
        self._button(ports, "refresh_button", "刷新串口", self.refresh_ports)
        layout.addLayout(ports)
        commands = QHBoxLayout()
        self._button(commands, "connect_button", "连接", self.connect_controller)
        self._button(commands, "disconnect_button", "断开", self.disconnect_controller)
        layout.addLayout(commands)
        self.startup_checkbox = QCheckBox()
        self.startup_checkbox.setObjectName("laserStartupConfirmed")
        self._rt.text(self.startup_checkbox, "启动检查已完成")
        self.startup_checkbox.clicked.connect(self._startup_toggled)
        self.startup_checkbox.toggled.connect(self._startup_selection_changed)
        layout.addWidget(self.startup_checkbox)
        startup_help = "确认激光器自检、温度稳定和 CDRH 启动延时均已完成；复位或故障后须重新确认。"
        self._rt.tooltip(self.startup_checkbox, startup_help)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.diagnostics_section = CuttingDisclosure("硬件诊断", self, expanded=False)
        self.diagnostics_section.setObjectName("laserDiagnostics")
        diagnostics_form = QFormLayout()
        diagnostics_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.status_fields = {}
        for name, title in (
            ("state", "控制器状态"), ("firmware_version", "固件版本"),
            ("startup_checked", "启动检查固件 ACK"),
            ("watchdog_ms", "看门狗窗口 (ms)"), ("interlock_wired", "联锁接线能力"),
            ("trigger_wired", "触发接线能力"), ("interlock_ok", "联锁输入状态"),
            ("trigger_active", "触发输入状态"), ("comp_high", "Comp 电平 ACK"),
            ("last_seen", "最近应答"), ("fault", "故障信息"),
        ):
            value = self._label()
            value.setObjectName("laser_" + name)
            self.status_fields[name] = value
            target = form if name in ("state", "fault") else diagnostics_form
            target.addRow(self._label(title), value)
        layout.addLayout(form)
        self.capability_label = self._label()
        layout.addWidget(self.capability_label)
        self.message_label = self._label()
        layout.addWidget(self.message_label)
        # Presentation only: disclosure changes never connect or attest startup.
        self.diagnostics_section.content_layout.addLayout(diagnostics_form)
        self.diagnostics_section.content_layout.addWidget(self._label(startup_help))
        self.capability_details_label = self._label()
        self.diagnostics_section.content_layout.addWidget(self.capability_details_label)
        self.diagnostics_section.content_layout.addWidget(self._label(
            "Comp HIGH/LOW 和串口 ACK 不是光功率测量。联锁感测不等于独立硬件切断。"
        ))
        layout.addWidget(self.diagnostics_section)

    def _message(self, text, **values):
        self._rt.message(self.message_label, text, **values)

    def _startup_toggled(self, checked):
        self._startup_confirmed = False
        if not checked:
            return
        if (self.busy or self._closing or self._job_busy or not self._connected
                or _state_name(self._status) not in _STARTUP_CHECK_STATES
                or getattr(self._status, "fault", None)):
            self._reset_startup()
            return
        # Human input authorizes this one transaction; programmatic repaint/reset
        # never sends CHECK. Connect deliberately preserves BOOT_HOLD startup HIGH.
        self._reset_startup()
        self._epoch += 1
        epoch, controller = self._epoch, self.controller
        identity = self._startup_identity(self._status)
        self._message("正在发送 OFF 并确认启动检查，请等待固件 ACK。")

        def confirm():
            if self._closing or self._job_busy or epoch != self._epoch:
                raise RuntimeError("Startup confirmation cancelled")
            if self._startup_identity(controller.status) != identity:
                raise RuntimeError("Laser boot/session changed before startup confirmation")
            off = controller.off()
            generation = getattr(off, "stop_generation", None)
            if (self._closing or self._job_busy or epoch != self._epoch
                    or _state_name(off) != "DISARMED" or getattr(off, "fault", None)
                    or self._startup_identity(off) != identity
                    or getattr(controller.status, "stop_generation", None) != generation):
                raise RuntimeError("Startup OFF was cancelled or unconfirmed")
            ack = controller.confirm_startup_check()
            if (self._closing or self._job_busy or epoch != self._epoch
                    or _state_name(ack) != "DISARMED" or getattr(ack, "fault", None)
                    or getattr(ack, "startup_checked", False) is not True
                    or self._startup_identity(ack) != identity
                    or getattr(ack, "stop_generation", None) != generation):
                raise RuntimeError("Startup CHECK acknowledgement was cancelled or unconfirmed")
            return ack

        self._launch("startup", confirm)

    def _startup_selection_changed(self, checked):
        # Programmatic uncheck may revoke consent; programmatic check is never
        # human attestation. Only the clicked handler can transmit OFF/CHECK.
        if not checked:
            self._startup_confirmed = False
            self._epoch += 1

    @staticmethod
    def _startup_identity(status):
        return tuple(getattr(status, name, None) for name in ("boot_nonce", "session_nonce", "firmware_version"))

    def _display_startup_check(self, checked):
        blocked = self.startup_checkbox.blockSignals(True)
        try:
            self.startup_checkbox.setChecked(checked)
        finally:
            self.startup_checkbox.blockSignals(blocked)

    def _reset_startup(self):
        self._startup_confirmed = False
        self._display_startup_check(False)

    def set_job_busy(self, busy):
        self._job_busy = bool(busy)
        if self._job_busy and self._confirmation is not None:
            self._confirmation.reject()
        self._sync_controls()

    def _has_serial_resource(self):
        # This driver property is an unlocked, I/O-free handle-presence snapshot.
        # Do not consult controller.connected/status here: their locks belong on
        # workers, not the GUI rendering path. Legacy injected fakes fall back to
        # the already accepted protocol snapshot.
        controller = self.controller
        return controller is not None and getattr(
            controller, "has_serial_resource", self._connected
        ) is True

    def _sync_controls(self):
        idle = not self.busy and not self._closing and not self._job_busy
        has_resource = self._has_serial_resource()
        can_connect = idle and not self._connected and not has_resource
        self.port_combo.setEnabled(can_connect)
        self.refresh_button.setEnabled(idle)
        self.connect_button.setEnabled(can_connect)
        self.disconnect_button.setEnabled(idle and (self._connected or has_resource))
        self.startup_checkbox.setEnabled(idle and self._connected
                                        and _state_name(self._status) in _STARTUP_CHECK_STATES
                                        and not getattr(self._status, "fault", None))
        self.off_button.setEnabled(True)
        current = self.busy
        if current != self._last_busy:
            self._last_busy = current
            self.busy_changed.emit(current)

    def _render_status(self):
        status = self._status
        unknown = tr("未确认")
        self.status_fields["state"].setText(str(_state_name(status)))
        self.status_fields["firmware_version"].setText(str(getattr(status, "firmware_version", None) or unknown))
        watchdog = getattr(status, "watchdog_ms", None)
        self.status_fields["watchdog_ms"].setText(str(watchdog) if watchdog is not None else unknown)
        for name in ("startup_checked", "interlock_wired", "trigger_wired", "interlock_ok", "trigger_active"):
            value = getattr(status, name, None)
            self.status_fields[name].setText(tr("是") if value is True else tr("否") if value is False else unknown)
        comp = getattr(status, "comp_high", None)
        self.status_fields["comp_high"].setText("HIGH" if comp is True else "LOW" if comp is False else unknown)
        seen = getattr(status, "last_seen_monotonic", None)
        if isinstance(seen, (int, float)) and not isinstance(seen, bool) and math.isfinite(seen) and seen > 0:
            self.status_fields["last_seen"].setText(tr("{seconds:.1f} 秒前").format(seconds=max(0, time.monotonic() - seen)))
        else:
            self.status_fields["last_seen"].setText(unknown)
        self.status_fields["fault"].setText(str(getattr(status, "fault", None) or tr("无已报告故障")))
        capable = (getattr(status, "interlock_wired", None) is True
                   and getattr(status, "trigger_wired", None) is True)
        self.capability_label.setText(tr(
            "出光仍受阻：独立硬件切断与固件能力待作业层确认。" if capable else
            "禁止出光：联锁与触发接线能力未确认。"
        ))
        self.capability_details_label.setText(tr(
            "出光仍受阻：还需作业层确认独立硬件切断及固件能力。" if capable else
            "禁止出光：固件未确认联锁与触发接线能力；当前硬件缺少独立切断。"
        ))

    def _accept_status(self, snapshot):
        if snapshot is None:
            return
        status, connected = snapshot
        previous = self._status
        changed = status != previous or connected != self._connected
        self._status, self._connected = status, connected is True
        if (not self._connected or _state_name(status) in _UNCONFIRMED_STATES
                or getattr(status, "startup_checked", False) is not True
                or getattr(status, "fault", None)
                or (previous is not None and self._startup_identity(previous) != self._startup_identity(status))):
            self._reset_startup()
        self._render_status()
        self._sync_controls()
        if changed:
            self.status_changed.emit(status)

    def _controller_snapshot(self):
        controller = self.controller
        return None if controller is None else (controller.status, controller.connected)

    def _launch(self, kind, action):
        if kind in self._workers:
            return False
        self._sequence += 1
        token, epoch, signals = self._sequence, self._epoch, self._signals

        def run():
            result = error = snapshot = None
            try:
                result = action()
            except Exception as exc:
                error = exc
            try:
                snapshot = self._controller_snapshot()
            except Exception as exc:
                error = error or exc
            signals.done.emit(kind, token, (epoch, result, snapshot), error)

        thread = threading.Thread(target=run, name="ATSLaser-" + kind, daemon=True)
        self._workers[kind] = (token, thread)
        self._retained_workers.append(thread)
        _ACTIVE_PANELS.add(self)
        self._sync_controls()
        thread.start()
        return True

    @Slot(str, int, object, object)
    def _finished(self, kind, token, result, error):
        if kind in self._workers and self._workers[kind][0] == token:
            self._results[kind] = result, error
            self._drain()

    def _drain(self):
        for kind, (token, thread) in list(self._workers.items()):
            if thread.is_alive() or kind not in self._results:
                continue
            (epoch, result, snapshot), error = self._results.pop(kind)
            del self._workers[kind]
            self._retained_workers.remove(thread)
            if epoch == self._epoch or kind in ("emergency", "shutdown"):
                if kind == "startup" and error is None:
                    # The reader can fault/reset after the worker queues done.
                    # Re-read cached attributes only; GUI acceptance never polls
                    # serial I/O or trusts the earlier successful ACK snapshot.
                    try:
                        snapshot = self._controller_snapshot()
                    except Exception as exc:
                        snapshot, error = None, exc
                    current = snapshot[0] if snapshot is not None else None
                    if error is None and (snapshot is None or snapshot[1] is not True or self._closing or self._job_busy
                            or _state_name(current) != "DISARMED" or getattr(current, "fault", None)
                            or getattr(current, "startup_checked", False) is not True
                            or self._startup_identity(current) != self._startup_identity(result)
                            or getattr(current, "stop_generation", None) != getattr(result, "stop_generation", None)):
                        error = RuntimeError("Laser state changed before startup ACK delivery")
                self._accept_status(snapshot)
                if error is not None:
                    self._last_error = str(error)
                    self._reset_startup()
                    self._message("激光控制操作失败：{error}", error=str(error))
                elif kind == "ports":
                    previous = self.port_combo.currentText().strip()
                    self.port_combo.clear()
                    self.port_combo.addItems(result)
                    self.port_combo.setCurrentIndex(-1)
                    if previous:
                        self.port_combo.setEditText(previous)
                elif kind == "connect":
                    self._message("Arduino 已连接；请核对结构化状态并完成启动检查。")
                elif kind == "startup":
                    self._startup_confirmed = True
                    self._display_startup_check(True)
                    self._message("启动检查已获固件确认；保持 OFF，出光仍受硬件与作业检查限制。")
                    self.status_changed.emit(self._status)
                elif kind == "emergency":
                    self._message("OFF 请求已处理；ACK 不代表已测得光功率为零。")
                elif kind == "disconnect":
                    self._message("激光控制已断开。")
            if kind == "shutdown":
                self._shutdown_done = True
                self._shutdown_ok = (error is None and result is True
                                     and snapshot is not None and snapshot[1] is False
                                     and not self._has_serial_resource())
                if self._shutdown_ok:
                    self._last_error = None
                else:
                    self._last_error = str(error) if error is not None else (
                        self._last_error or "OFF/disconnect acknowledgement unconfirmed"
                    )
                    self._message("关闭任务已结束，但 OFF 或串口断开未确认：{error}", error=self._last_error)
                    _audit.critical("Laser shutdown unconfirmed after workers finished: %s", self._last_error)
                self.shutdown_finished.emit(self._shutdown_ok, self._last_error)
        self._sync_controls()
        if not self._workers:
            _ACTIVE_PANELS.discard(self)
            if self._closing and self._close_when_idle:
                self._close_when_idle = False
                self.close()

    def _tick(self):
        self._drain()
        if self._closing:
            if self._close_deadline is not None and time.monotonic() >= self._close_deadline:
                self._close_deadline = None
                if self._workers:
                    self._message("OFF 或断开尚未确认；保留面板并等待安全退出。")
            return
        if time.monotonic() >= self._next_monitor and self.controller is not None and not self._workers:
            self._next_monitor = time.monotonic() + 0.25
            controller = self.controller
            # Jobs own polling/heartbeats. Never contend with their serial I/O.
            def monitor():
                if not self._job_busy and not self._closing and controller.connected:
                    controller.poll()
            self._launch("monitor", monitor)
        self._render_status()

    def refresh_ports(self):
        if self.busy or self._closing or self._job_busy:
            return
        lister = self._port_lister

        def enumerate_ports():
            return sorted({str(getattr(port, "device", port)).strip() for port in lister()
                           if str(getattr(port, "device", port)).strip()})

        self._launch("ports", enumerate_ports)

    def connect_controller(self):
        if (self.busy or self._closing or self._job_busy or self._connected
                or self._has_serial_resource() or self._confirmation is not None):
            return
        port = self.port_combo.currentText().strip()
        if not port:
            self._message("请先选择 Arduino 串口。")
            return
        dialog = QDialog(self)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(380)
        self._rt.title(dialog, "连接 Arduino 前确认")
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._label("仅连接 Arduino 控制串口。激光电源必须关闭或光路已物理遮挡；打开串口可能触发 DTR 复位并将 Comp 置为 HIGH。"))
        check = QCheckBox()
        check.setObjectName("laserConnectConfirmation")
        self._rt.text(check, "已关闭激光电源或物理遮挡光路")
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
            if result == QDialog.DialogCode.Accepted and check.isChecked():
                self._connect(port)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    def _connect(self, port):
        if (self.busy or self._closing or self._job_busy or self._connected
                or self._has_serial_resource()):
            return
        self._epoch += 1
        epoch, factory = self._epoch, self._controller_factory
        self._last_error = None
        self._reset_startup()

        def connect():
            if self.controller is None:
                self.controller = factory()
            controller = self.controller
            if self._closing or epoch != self._epoch:
                return
            controller.connect(port)
            # A late connect completion cannot undo a newer OFF/close request.
            if self._closing or epoch != self._epoch:
                controller.emergency_off()

        self._launch("connect", connect)

    def disconnect_controller(self):
        if self.busy or self._closing or self._job_busy:
            return
        controller = self.controller
        if controller is None:
            return
        self._epoch += 1
        self._reset_startup()

        def disconnect():
            try:
                controller.off()
            finally:
                controller.disconnect()

        self._launch("disconnect", disconnect)

    def _user_emergency_off(self):
        self.emergency_requested.emit()
        self.emergency_off()

    def emergency_off(self):
        """Request high-priority OFF without re-emitting emergency_requested."""
        self._epoch += 1
        self._reset_startup()
        if self._confirmation is not None:
            self._confirmation.reject()
        controller = self.controller
        if controller is None:
            return
        self._launch("emergency", controller.emergency_off)

    def prepare_for_close(self, timeout_ms=0):
        """Initiate cleanup; True means workers exited, not hardware OFF."""
        self._closing = True
        self._reset_startup()
        if self._confirmation is not None:
            self._confirmation.reject()
        self._drain()
        if not self._shutdown_started:
            self._shutdown_started = True
            self._close_deadline = time.monotonic() + max(0, timeout_ms) / 1000
            if self.controller is None and not self._workers:
                self._shutdown_done = True
                # No protocol exchange took place; an idle panel proves no
                # hardware state, although it is immediately safe to destroy.
                self._shutdown_ok = False
                self.shutdown_finished.emit(False, None)
            else:
                self.emergency_off()  # Preempts connect/normal worker before waiting.
                prior = tuple(thread for _, thread in self._workers.values())

                def shutdown():
                    for thread in prior:
                        thread.join()  # Worker-only wait; timeout never deletes it.
                    controller = self.controller
                    if controller is not None:
                        errors = []
                        try:
                            controller.emergency_off()
                        except Exception as exc:
                            errors.append(f"OFF: {exc}")
                        finally:
                            try:
                                controller.disconnect()
                            except Exception as exc:
                                errors.append(f"disconnect: {exc}")
                        if errors:
                            raise RuntimeError("; ".join(errors))
                        return True
                    return False

                self._launch("shutdown", shutdown)
        self._sync_controls()
        return not self._workers

    def reopen(self) -> bool:
        """Reuse a closed panel without opening a port or constructing a driver.

        Pending workers must finish first. If the final cached result still
        reports a connected port, refuse to reset shutdown state rather than
        pretend it is disconnected. A failed link with a retained serial resource
        can reopen for explicit Disconnect cleanup; Connect remains disabled.
        Keep last_error available for auditing; only an explicit new connection
        clears it. This method does not show the widget or restore startup/job
        confirmation.
        """
        self._drain()
        if self._workers:
            return False
        if not self._closing:
            return True
        if self._connected:
            return False
        self._epoch += 1
        self._closing = False
        self._close_when_idle = False
        self._close_deadline = None
        self._shutdown_started = False
        self._shutdown_done = False
        self._shutdown_ok = False
        self._job_busy = False
        self._results.clear()
        self._reset_startup()
        self._next_monitor = time.monotonic() + 0.25
        self._timer.start()
        if self._last_error:
            self._message("激光控制操作失败：{error}", error=self._last_error)
        else:
            self._message("激光控制未连接；串口只连接 Arduino。")
        self._render_status()
        self._sync_controls()
        return True

    def closeEvent(self, event):  # noqa: N802
        if not self.prepare_for_close():
            self._close_when_idle = True
            event.ignore()
            return
        self._timer.stop()
        event.accept()
