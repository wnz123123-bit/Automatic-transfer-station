"""Fake-only serial panel regressions: no port opens and no hardware imports."""
import os
import builtins
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QThread, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox, QLabel, QPushButton

from automatic_transfer_station.i18n import TRANSLATIONS, translator
from automatic_transfer_station.laser_panel import LaserPanel, _ACTIVE_PANELS


class State(Enum):
    DISCONNECTED = "DISCONNECTED"
    BOOT_HOLD = "BOOT_HOLD"
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    FOLLOW = "FOLLOW"
    FIRING = "FIRING"
    FAULT = "FAULT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Status:
    state: State = State.DISCONNECTED
    firmware_version: str | None = None
    interlock_wired: bool = False
    trigger_wired: bool = False
    interlock_ok: bool | None = None
    trigger_active: bool | None = None
    comp_high: bool | None = None
    last_seen_monotonic: float = 0
    fault: str | None = None
    watchdog_ms: int | None = None
    startup_checked: bool = False
    boot_nonce: str = "test-boot"
    session_nonce: str = "test-session"
    stop_generation: int = 0


class FakeLaser:
    def __init__(self):
        self.connected = False
        self._serial_resource = False
        self.status = Status()
        self.calls = []
        self.connect_entered = threading.Event()
        self.emergency_entered = threading.Event()
        self.disconnect_entered = threading.Event()
        self.connect_gate = None
        self.disconnect_gate = None
        self.emergency_gate = None
        self.check_gate = None
        self.check_entered = threading.Event()
        self.check_error = None
        self.disconnect_error = None

    @property
    def has_serial_resource(self):
        return self._serial_resource

    def record(self, command):
        self.calls.append((command, threading.get_ident()))

    def connect(self, port):
        self.record(("connect", port))
        self.connect_entered.set()
        if self.connect_gate:
            assert self.connect_gate.wait(3)
        self._serial_resource = True
        self.connected = True
        self.status = Status(State.BOOT_HOLD, "test-fw", comp_high=True,
                             last_seen_monotonic=time.monotonic(), watchdog_ms=250)

    def off(self):
        self.record("off")
        self.status = replace(self.status, state=State.DISARMED, comp_high=False, fault=None,
                              stop_generation=self.status.stop_generation + 1)
        return self.status

    def confirm_startup_check(self):
        assert self.status.state is State.DISARMED, "OFF must precede CHECK"
        self.record("check")
        self.check_entered.set()
        if self.check_gate:
            assert self.check_gate.wait(3)
        if self.check_error:
            raise self.check_error
        self.status = replace(self.status, startup_checked=True)
        return self.status

    def emergency_off(self):
        self.record("emergency_off")
        self.emergency_entered.set()
        if self.emergency_gate:
            assert self.emergency_gate.wait(3)
        self.status = replace(self.status, state=State.FAULT if self.connected else State.DISCONNECTED,
                              comp_high=False, startup_checked=False, fault="ESTOP",
                              stop_generation=self.status.stop_generation + 1)
        return self.status

    def disconnect(self):
        self.record("disconnect")
        self.disconnect_entered.set()
        if self.disconnect_gate:
            assert self.disconnect_gate.wait(3)
        self.connected = False
        if self.disconnect_error is not None:
            self.status = replace(self.status, state=State.UNKNOWN, comp_high=None,
                                  startup_checked=False, fault="CLOSE_UNCONFIRMED")
            raise self.disconnect_error
        self._serial_resource = False
        self.status = replace(self.status, state=State.DISCONNECTED, startup_checked=False)

    def poll(self):
        self.record("poll")
        self.status = replace(self.status, last_seen_monotonic=time.monotonic())
        return self.status

    def keepalive(self):
        pytest.fail("Panel must never send keepalive")

    def arm(self):
        pytest.fail("Panel must never ARM")

    def fire(self, duration_ms):
        pytest.fail("Panel must never FIRE")


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert predicate(), "Qt condition did not complete"


@pytest.fixture
def rig(app):
    controller = FakeLaser()
    panel = LaserPanel(controller_factory=lambda: controller,
                       port_lister=lambda: [SimpleNamespace(device="COM17"), "COM3", "COM17"])
    panel.resize(360, 900)
    panel.show()
    app.processEvents()
    yield panel, controller
    for gate in (controller.connect_gate, controller.disconnect_gate, controller.emergency_gate, controller.check_gate):
        if gate:
            gate.set()
    wait_for(lambda: panel.prepare_for_close(50))
    panel.close()
    panel.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def confirm_connect(panel):
    panel.port_combo.setEditText("COM17")
    panel.connect_controller()
    dialog = panel._confirmation
    assert dialog is not None
    checkbox = dialog.findChild(QCheckBox, "laserConnectConfirmation")
    ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert not checkbox.isChecked() and not ok.isEnabled()
    checkbox.setChecked(True)
    QTest.mouseClick(ok, Qt.MouseButton.LeftButton)


def connect(rig):
    panel, controller = rig
    confirm_connect(panel)
    wait_for(lambda: not panel.busy and panel.status is not None and panel.status.state is State.BOOT_HOLD)
    assert controller.connected


def click_startup(panel):
    # QCheckBox's clickable region is its indicator/text, not unused row width.
    QTest.mouseClick(panel.startup_checkbox, Qt.MouseButton.LeftButton,
                     pos=QPoint(8, panel.startup_checkbox.height() // 2))


def confirm_startup(panel):
    click_startup(panel)
    assert not panel.startup_confirmed  # GUI input alone is never the firmware ACK.
    wait_for(lambda: not panel.busy)
    assert panel.startup_confirmed, panel.message_label.text()


def update_status(panel, controller, **fields):
    controller.status = replace(controller.status, **fields)
    panel._next_monitor = 0
    panel._tick()
    wait_for(lambda: panel.status == controller.status)


def test_construction_and_unconnected_close_do_not_create_controller_or_enumerate(app):
    calls = []
    panel = LaserPanel(controller_factory=lambda: calls.append("factory"), port_lister=lambda: calls.append("ports"))
    QTest.qWait(40)
    assert calls == []
    assert panel.controller is None and not panel.busy and not panel.startup_confirmed
    assert panel.status_fields["state"].text() == "DISCONNECTED"
    assert panel.off_button.isEnabled()
    epoch = panel._epoch
    for expanded in (True, False, True):
        panel.diagnostics_section.set_expanded(expanded)
        QTest.qWait(40)
    panel.diagnostics_section.toggle_button.click()
    assert not panel.diagnostics_section.is_expanded
    assert panel.controller is None and panel._confirmation is None
    assert not panel.busy and not panel.startup_confirmed
    assert not panel.startup_checkbox.isChecked()
    assert panel._epoch == epoch and panel._sequence == 0
    assert calls == []
    panel.emergency_off()
    assert panel.prepare_for_close(0)
    assert not panel.shutdown_confirmed
    assert panel.last_error is None
    assert calls == []
    panel.close()
    panel.deleteLater()


def test_collapsed_diagnostics_keep_controls_fault_and_blocking_reason_visible(rig):
    panel, controller = rig
    diagnostics = panel.diagnostics_section
    diagnostic_fields = {
        "firmware_version", "startup_checked", "watchdog_ms", "interlock_wired",
        "trigger_wired", "interlock_ok", "trigger_active", "comp_high", "last_seen",
    }
    fields = dict(panel.status_fields)
    assert set(fields) == diagnostic_fields | {"state", "fault"}
    assert not diagnostics.is_expanded and not diagnostics.body.isVisible()
    assert panel.layout().itemAt(0).widget() is panel.off_button
    for name in diagnostic_fields:
        assert diagnostics.body.isAncestorOf(fields[name])
        assert not fields[name].isVisible()
        assert fields[name].objectName() == "laser_" + name
    visible_controls = (
        panel.off_button, panel.port_combo, panel.refresh_button,
        panel.connect_button, panel.disconnect_button, panel.startup_checkbox,
        fields["state"], fields["fault"], panel.capability_label, panel.message_label,
    )
    assert all(control.isVisible() for control in visible_controls)
    assert not any(diagnostics.body.isAncestorOf(control) for control in visible_controls)
    # Full explanations stay reachable in diagnostics and the startup tooltip.
    help_labels = diagnostics.body.findChildren(QLabel)
    assert any(label.text() == panel.startup_checkbox.toolTip() for label in help_labels)
    assert any("不是光功率测量" in label.text() for label in help_labels)

    connect(rig)
    update_status(panel, controller, state=State.FAULT, fault="INTERLOCK_OPEN")
    assert fields["state"].text() == "FAULT"
    assert fields["fault"].text() == "INTERLOCK_OPEN"
    assert "禁止出光" in panel.capability_label.text()
    assert fields["firmware_version"].text() == "test-fw"
    collapsed_height = panel.sizeHint().height()
    QTest.mouseClick(diagnostics.toggle_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert diagnostics.is_expanded and diagnostics.body.isVisible()
    assert panel.sizeHint().height() > collapsed_height
    assert all(fields[name].isVisible() for name in diagnostic_fields)
    assert all(control.isVisible() for control in visible_controls)
    assert panel.status_fields == fields  # Existing references survive disclosure changes.
    QTest.mouseClick(diagnostics.toggle_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert not diagnostics.is_expanded
    assert all(control.isVisible() for control in visible_controls)
    assert fields["fault"].text() == "INTERLOCK_OPEN"
    assert panel.off_button.isEnabled()


@pytest.mark.parametrize("confirmed", [False, True])
def test_diagnostics_toggle_preserves_attestation_without_hardware_commands(rig, confirmed):
    panel, controller = rig
    connect(rig)
    if confirmed:
        confirm_startup(panel)
    # Exclude normal scheduled polls while checking disclosure side effects.
    panel._next_monitor = float("inf")
    wait_for(lambda: not panel._workers)
    calls = list(controller.calls)
    epoch, sequence, status = panel._epoch, panel._sequence, panel.status
    startup_changes = QSignalSpy(panel.startup_checkbox.toggled)
    status_changes = QSignalSpy(panel.status_changed)
    for expanded in (True, False, True):
        panel.diagnostics_section.set_expanded(expanded)
        QTest.qWait(40)
        assert panel.startup_confirmed is confirmed
        assert panel.startup_checkbox.isChecked() is confirmed
    QTest.mouseClick(panel.diagnostics_section.toggle_button, Qt.MouseButton.LeftButton)
    assert not panel.diagnostics_section.is_expanded
    assert panel.startup_confirmed is confirmed
    assert panel.startup_checkbox.isChecked() is confirmed
    assert panel._epoch == epoch and panel._sequence == sequence
    assert panel.status is status and controller.calls == calls
    assert panel._confirmation is None and not panel._workers
    assert startup_changes.count() == status_changes.count() == 0


def test_port_refresh_is_explicit_and_connection_confirmation_can_be_cancelled(rig):
    panel, controller = rig
    assert panel.port_combo.count() == 0
    panel.refresh_ports()
    wait_for(lambda: not panel.busy)
    assert [panel.port_combo.itemText(i) for i in range(panel.port_combo.count())] == ["COM17", "COM3"]
    assert panel.port_combo.currentIndex() == -1
    panel.port_combo.setEditText("COM17")
    panel.connect_controller()
    assert panel.controller is None
    panel._confirmation.reject()
    assert panel.controller is None and controller.calls == []


def test_confirmed_connect_runs_off_gui_and_returns_queued_status(rig):
    panel, controller = rig
    threads = []
    panel.status_changed.connect(lambda _: threads.append(QThread.currentThread()))
    connect(rig)
    assert panel.controller is controller
    assert all(thread == QApplication.instance().thread() for thread in threads)
    assert all(thread != threading.get_ident() for _, thread in controller.calls)
    assert panel.status_fields["comp_high"].text() == "HIGH"
    assert panel.status_fields["watchdog_ms"].text() == "250"
    assert not panel.startup_confirmed
    confirm_startup(panel)
    panel.disconnect_controller()
    wait_for(lambda: not panel.busy)
    assert not panel.startup_confirmed and not controller.connected


def test_link_fault_keeps_disconnect_available_until_resource_is_released(rig, monkeypatch):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    controller.connected = False  # A failed protocol session still owns its port.
    update_status(panel, controller, state=State.UNKNOWN, comp_high=None,
                  startup_checked=False, fault="READER_FAULT")
    assert controller.has_serial_resource
    assert panel.disconnect_button.isEnabled()
    assert not panel.connect_button.isEnabled() and not panel.port_combo.isEnabled()
    assert not panel.startup_confirmed
    calls = list(controller.calls)
    panel.connect_controller()
    assert panel._confirmation is None
    panel._connect("COM_OTHER")  # Programmatic calls cannot bypass the resource gate.
    assert controller.calls == calls

    original_off = controller.off

    def failed_off():
        controller.record("off")
        raise OSError("OFF acknowledgement unavailable after link loss")

    monkeypatch.setattr(controller, "off", failed_off)
    panel.disconnect_button.click()
    wait_for(lambda: not panel.busy)
    assert controller.disconnect_entered.is_set()  # Cleanup runs even when OFF fails.
    assert not controller.has_serial_resource
    assert panel.connect_button.isEnabled() and panel.port_combo.isEnabled()
    assert not panel.disconnect_button.isEnabled()
    assert "OFF acknowledgement unavailable" in panel.last_error
    assert all(thread != threading.get_ident() for _, thread in controller.calls)

    monkeypatch.setattr(controller, "off", original_off)
    connect(rig)
    assert controller.has_serial_resource and panel.last_error is None
    assert not panel.startup_confirmed  # Reconnection never restores operator consent.


@pytest.mark.parametrize("close_window", [False, True])
def test_close_failure_keeps_cleanup_available_and_blocks_reconnect(rig, close_window):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    controller.disconnect_error = OSError("injected close failure with retained handle")
    if close_window:
        panel.close()
        wait_for(lambda: not panel.isVisible())
        assert not panel.shutdown_confirmed
        assert panel.reopen()  # Reopen for cleanup, not permission to open another port.
        panel.show()
    else:
        panel.disconnect_button.click()
        wait_for(lambda: not panel.busy)
    assert not controller.connected and controller.has_serial_resource
    assert panel.disconnect_button.isEnabled()
    assert not panel.connect_button.isEnabled() and not panel.port_combo.isEnabled()
    assert not panel.startup_confirmed and not panel.startup_checkbox.isEnabled()
    assert "injected close failure" in panel.last_error
    calls = list(controller.calls)
    panel.connect_controller()
    assert panel._confirmation is None
    panel._connect("COM_OTHER")
    QTest.qWait(300)
    assert controller.calls == calls  # No automatic retry, polling, or reconnection.

    controller.disconnect_error = None
    panel.disconnect_button.click()
    wait_for(lambda: not panel.busy)
    assert not controller.has_serial_resource
    assert panel.connect_button.isEnabled() and panel.port_combo.isEnabled()
    assert not panel.disconnect_button.isEnabled()
    connect(rig)
    assert controller.has_serial_resource and panel.last_error is None
    assert not panel.startup_confirmed
    assert sum(isinstance(command, tuple) and command[0] == "connect"
               for command, _ in controller.calls) == 2


@pytest.mark.parametrize("has_resource", [False, True])
def test_rendering_and_connection_gate_use_only_nonblocking_resource_snapshot(rig, has_resource):
    panel, original = rig

    class SnapshotOnlyController:
        has_serial_resource = has_resource

        @property
        def connected(self):
            pytest.fail("GUI rendering must not acquire the driver's connected lock")

        @property
        def status(self):
            pytest.fail("GUI rendering must not acquire the driver's status lock")

        def __getattr__(self, name):
            pytest.fail(f"GUI rendering must not call controller I/O: {name}")

    panel.controller = SnapshotOnlyController()
    try:
        panel._render_status()
        panel._sync_controls()
        assert panel.disconnect_button.isEnabled() is has_resource
        assert panel.connect_button.isEnabled() is (not has_resource)
        if has_resource:
            panel.connect_controller()
            panel._connect("COM_OTHER")
            assert panel._confirmation is None and not panel.busy
    finally:
        panel.controller = None
    assert original.calls == []


@pytest.mark.parametrize("state", [State.BOOT_HOLD, State.FAULT, State.UNKNOWN, State.DISCONNECTED])
def test_reset_fault_and_unknown_states_clear_startup_confirmation(rig, state):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    update_status(panel, controller, state=state, startup_checked=False)
    assert not panel.startup_confirmed
    assert not panel.startup_checkbox.isChecked()
    assert panel.startup_checkbox.isEnabled() is (state is State.BOOT_HOLD)
    assert panel.off_button.isEnabled()


def test_source_flags_and_comp_ack_never_claim_independent_optical_safety(rig):
    panel, controller = rig
    connect(rig)
    update_status(panel, controller, interlock_ok=True, interlock_wired=False, trigger_wired=False, comp_high=True)
    assert "禁止出光" in panel.capability_label.text()
    assert panel.status_fields["interlock_ok"].text() == "是"
    assert panel.status_fields["comp_high"].text() == "HIGH"
    update_status(panel, controller, interlock_wired=True, trigger_wired=True)
    assert "出光仍受阻" in panel.capability_label.text()
    assert "独立硬件切断" in panel.capability_label.text()


def test_job_busy_locks_inputs_and_observes_cached_status_without_poll_or_keepalive(rig):
    panel, controller = rig
    connect(rig)
    panel.set_job_busy(True)
    previous_calls = list(controller.calls)
    assert not panel.connect_button.isEnabled()
    assert not panel.disconnect_button.isEnabled()
    assert not panel.startup_checkbox.isEnabled()
    assert panel.off_button.isEnabled()
    update_status(panel, controller, state=State.FOLLOW, comp_high=False)
    QTest.qWait(300)
    assert controller.calls == previous_calls
    assert panel.status_fields["state"].text() == "FOLLOW"
    assert panel.status_fields["comp_high"].text() == "LOW"
    assert not any("ARM" in button.text() or "FIRE" in button.text() for button in panel.findChildren(QPushButton))


def test_user_off_signal_does_not_loop_when_abort_calls_emergency_off(rig):
    panel, controller = rig
    connect(rig)
    spy = QSignalSpy(panel.emergency_requested)
    panel.emergency_requested.connect(panel.emergency_off)  # Simulate window.abort.
    QTest.mouseClick(panel.off_button, Qt.MouseButton.LeftButton)
    wait_for(lambda: not panel.busy)
    assert spy.count() == 1
    assert any(command == "emergency_off" for command, _ in controller.calls)
    panel.emergency_off()
    wait_for(lambda: not panel.busy)
    assert spy.count() == 1


def test_emergency_worker_preempts_blocked_connect_and_late_connect_reasserts_off(rig):
    panel, controller = rig
    controller.connect_gate = threading.Event()
    confirm_connect(panel)
    wait_for(controller.connect_entered.is_set)
    assert panel.busy
    panel.emergency_off()
    wait_for(controller.emergency_entered.is_set)
    assert not controller.connect_gate.is_set() and panel.busy
    controller.connect_gate.set()
    wait_for(lambda: not panel.busy)
    assert sum(command == "emergency_off" for command, _ in controller.calls) >= 2
    assert controller.status.comp_high is False
    assert not panel.startup_confirmed


def test_close_is_nonblocking_preempts_connect_and_waits_for_disconnect(rig):
    panel, controller = rig
    controller.connect_gate = threading.Event()
    controller.disconnect_gate = threading.Event()
    confirm_connect(panel)
    wait_for(controller.connect_entered.is_set)
    started = time.monotonic()
    assert panel.prepare_for_close(0) is False
    assert time.monotonic() - started < 0.1
    wait_for(controller.emergency_entered.is_set)
    panel.close()
    assert panel.isVisible()
    controller.connect_gate.set()
    wait_for(controller.disconnect_entered.is_set)
    assert not panel.prepare_for_close(0)
    assert not panel.shutdown_confirmed
    assert panel.off_button.isEnabled()
    controller.disconnect_gate.set()
    wait_for(lambda: not panel.isVisible())
    assert panel.prepare_for_close(0)
    assert panel.shutdown_confirmed
    assert panel.last_error is None
    assert not controller.connected
    assert all(not worker.is_alive() for worker in panel._retained_workers)


@pytest.mark.parametrize("disconnect_fails", [False, True])
def test_dead_port_shutdown_finishes_workers_preserves_failure_and_attempts_disconnect(
        rig, monkeypatch, caplog, disconnect_fails):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    panel.set_job_busy(True)
    original_disconnect = controller.disconnect

    def fail_off():
        controller.record("emergency_off")
        controller.status = replace(controller.status, state=State.UNKNOWN, comp_high=None, fault="port lost")
        raise OSError("serial port lost before OFF acknowledgement")

    def disconnect():
        original_disconnect()
        if disconnect_fails:
            raise OSError("disconnect acknowledgement unavailable")

    monkeypatch.setattr(controller, "emergency_off", fail_off)
    monkeypatch.setattr(controller, "disconnect", disconnect)
    finished = QSignalSpy(panel.shutdown_finished)
    with caplog.at_level("CRITICAL", logger="ats.audit"):
        panel.close()
        wait_for(lambda: not panel.isVisible())
    assert panel.prepare_for_close(0) is True
    assert not panel.shutdown_confirmed
    assert "serial port lost" in panel.last_error
    if disconnect_fails:
        assert "disconnect acknowledgement unavailable" in panel.last_error
    assert controller.disconnect_entered.is_set()
    assert not controller.connected and not panel.busy
    assert not panel.startup_confirmed and not panel.startup_checkbox.isChecked()
    assert not panel.connect_button.isEnabled()
    assert not panel._retained_workers and panel not in _ACTIVE_PANELS
    assert finished.count() == 1
    assert finished.at(0) == [False, panel.last_error]
    assert "Laser shutdown unconfirmed" in caplog.text
    assert "OFF" in panel.message_label.text() and panel.last_error in panel.message_label.text()
    calls = list(controller.calls)
    assert panel.prepare_for_close(0)
    QTest.qWait(40)
    assert controller.calls == calls  # No automatic retry/reconnect/resumption.


def test_language_switch_preserves_protocol_state_port_and_startup_check(rig, monkeypatch):
    panel, controller = rig
    monkeypatch.setitem(TRANSLATIONS, "启动检查已完成", "Startup checks complete")
    translations = {
        "硬件诊断": "Hardware diagnostics",
        "出光仍受阻：独立硬件切断与固件能力待作业层确认。":
            "Emission remains blocked: the job must qualify independent inhibit and firmware capabilities.",
        "禁止出光：联锁与触发接线能力未确认。":
            "Emission blocked: interlock/trigger wiring capabilities are unconfirmed.",
    }
    for key, english in translations.items():
        monkeypatch.setitem(TRANSLATIONS, key, english)
    connect(rig)
    confirm_startup(panel)
    for language in ("en", "zh"):
        translator.set_language(language)
        assert panel.startup_confirmed
        assert panel.status_fields["state"].text() == "DISARMED"
        assert panel.status_fields["comp_high"].text() == "LOW"
        assert panel.port_combo.currentText() == "COM17"
        assert panel.startup_checkbox.text() == ("Startup checks complete" if language == "en" else "启动检查已完成")
        title = "硬件诊断"
        warning = "禁止出光：联锁与触发接线能力未确认。"
        assert panel.diagnostics_section.toggle_button.text() == (translations[title] if language == "en" else title)
        assert panel.capability_label.text() == (translations[warning] if language == "en" else warning)
        assert not panel.diagnostics_section.is_expanded
        assert not panel.status_fields["startup_checked"].isVisible()
    update_status(panel, controller, interlock_wired=True, trigger_wired=True)
    panel.diagnostics_section.set_expanded(True)
    for language in ("en", "zh"):
        translator.set_language(language)
        warning = "出光仍受阻：独立硬件切断与固件能力待作业层确认。"
        assert panel.capability_label.text() == (translations[warning] if language == "en" else warning)
        assert panel.diagnostics_section.is_expanded
        assert panel.status_fields["startup_checked"].isVisible()
        assert panel.startup_confirmed and panel.startup_checkbox.isChecked()


def test_default_panel_close_reopen_does_not_import_or_construct_controller(app, monkeypatch):
    original_import = builtins.__import__

    def no_laser_driver(name, *args, **kwargs):
        if name in ("laser_controller", "automatic_transfer_station.laser_controller"):
            pytest.fail("Default panel must not import the laser driver until explicit Connect")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_laser_driver)
    panel = LaserPanel()
    assert panel.prepare_for_close()
    panel.close()
    assert not panel._timer.isActive()
    assert panel.reopen()
    assert panel._timer.isActive()
    QTest.qWait(300)
    assert panel.controller is None
    assert panel.connect_button.isEnabled()
    assert not panel.startup_confirmed and not panel.shutdown_confirmed
    assert panel.prepare_for_close()
    panel.close()
    panel.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_closed_panel_reopens_disconnected_and_requires_explicit_reconnect(rig):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    panel.set_job_busy(True)
    wait_for(lambda: panel.prepare_for_close())
    assert panel.shutdown_confirmed
    panel.close()
    calls = list(controller.calls)
    assert panel.reopen()
    panel.show()
    assert panel._timer.isActive()
    assert not panel._closing and not panel._shutdown_started
    assert not panel._close_when_idle and not panel._job_busy
    assert not panel.startup_confirmed and not panel.shutdown_confirmed
    assert panel.connect_button.isEnabled()
    assert not panel.disconnect_button.isEnabled()
    QTest.qWait(300)
    assert controller.calls == calls and not controller.connected
    connect(rig)
    assert sum(isinstance(command, tuple) and command[0] == "connect"
               for command, _ in controller.calls) == 2
    assert not panel.startup_confirmed


def test_reopen_refuses_pending_shutdown_without_clearing_close_flags(rig):
    panel, controller = rig
    connect(rig)
    controller.disconnect_gate = threading.Event()
    assert not panel.prepare_for_close(0)
    wait_for(controller.disconnect_entered.is_set)
    assert not panel.reopen()
    assert panel._closing and panel._shutdown_started
    assert not panel.connect_button.isEnabled()
    controller.disconnect_gate.set()
    wait_for(lambda: panel.prepare_for_close(0))
    assert panel.reopen()
    assert not controller.connected


def test_reopen_preserves_unconfirmed_shutdown_error_without_reconnecting(rig, monkeypatch):
    panel, controller = rig
    connect(rig)

    def fail_off():
        controller.record("emergency_off")
        raise OSError("no OFF acknowledgement")

    monkeypatch.setattr(controller, "emergency_off", fail_off)
    wait_for(lambda: panel.prepare_for_close())
    assert not panel.shutdown_confirmed
    error = panel.last_error
    assert error and "no OFF acknowledgement" in error
    calls = list(controller.calls)
    assert panel.reopen()
    assert panel.last_error == error and not panel.shutdown_confirmed
    QTest.qWait(300)
    assert controller.calls == calls
    assert not panel.startup_confirmed and not controller.connected


def test_boot_hold_human_click_orders_off_then_check_and_waits_for_ack(rig):
    panel, controller = rig
    connect(rig)
    assert panel.status.state is State.BOOT_HOLD and panel.status.comp_high is True
    assert panel.startup_checkbox.isEnabled()
    assert not any(command in ("off", "check") for command, _ in controller.calls)
    controller.check_gate = threading.Event()
    click_startup(panel)
    wait_for(controller.check_entered.is_set)
    assert panel.busy and not panel.startup_confirmed
    assert not panel.startup_checkbox.isChecked()
    commands = [command for command, _ in controller.calls if command in ("off", "check")]
    assert commands == ["off", "check"]
    assert all(thread != threading.get_ident() for command, thread in controller.calls if command in ("off", "check"))
    epoch, sequence, calls = panel._epoch, panel._sequence, list(controller.calls)
    for expanded in (True, False):
        panel.diagnostics_section.set_expanded(expanded)
        QTest.qWait(20)
        assert panel.busy and not panel.startup_confirmed
        assert not panel.startup_checkbox.isChecked()
        assert panel.off_button.isVisible() and panel.off_button.isEnabled()
    assert panel._epoch == epoch and panel._sequence == sequence
    assert controller.calls == calls
    controller.check_gate.set()
    wait_for(lambda: not panel.busy)
    assert panel.startup_confirmed and panel.startup_checkbox.isChecked()
    assert panel.status.startup_checked is True
    assert panel.status_fields["startup_checked"].text() == "是"
    assert panel.status_fields["comp_high"].text() == "LOW"


@pytest.mark.parametrize("failure", ["exception", "missing_ack"])
def test_startup_check_failure_never_promotes_human_checkbox_to_confirmation(rig, monkeypatch, failure):
    panel, controller = rig
    connect(rig)
    if failure == "exception":
        controller.check_error = RuntimeError("independent interlock is not qualified")
    else:
        def missing_ack():
            controller.record("check")
            return controller.status  # No firmware startup_checked confirmation.
        monkeypatch.setattr(controller, "confirm_startup_check", missing_ack)
    click_startup(panel)
    wait_for(lambda: not panel.busy)
    assert not panel.startup_confirmed and not panel.startup_checkbox.isChecked()
    assert panel.status.startup_checked is False
    assert panel.last_error
    assert panel.off_button.isEnabled()


def test_late_startup_ack_after_emergency_cannot_restore_confirmation(rig, monkeypatch):
    panel, controller = rig
    connect(rig)
    controller.check_gate = threading.Event()

    def late_ack():
        stale_ack = replace(controller.status, startup_checked=True)
        controller.record("check")
        controller.check_entered.set()
        assert controller.check_gate.wait(3)
        # Simulate a misbehaving/late transport delivery that restores old state.
        controller.status = stale_ack
        return stale_ack

    monkeypatch.setattr(controller, "confirm_startup_check", late_ack)
    click_startup(panel)
    wait_for(controller.check_entered.is_set)
    panel.emergency_off()
    wait_for(controller.emergency_entered.is_set)
    controller.check_gate.set()
    wait_for(lambda: not panel.busy)
    QTest.qWait(300)
    assert not panel.startup_confirmed and not panel.startup_checkbox.isChecked()


@pytest.mark.parametrize("change", ["fault", "disconnect", "new_boot", "new_session",
                                  "stop_generation", "unchecked", "armed"])
def test_queued_startup_done_cannot_restore_confirmation_after_driver_change(rig, change):
    panel, controller = rig
    connect(rig)
    panel._next_monitor = float("inf")
    wait_for(lambda: not panel._workers)
    panel._timer.stop()
    controller.check_gate = threading.Event()
    confirmations = []
    panel.status_changed.connect(lambda _: confirmations.append(panel.startup_confirmed))
    click_startup(panel)
    worker = panel._workers["startup"][1]
    assert controller.check_entered.wait(1)
    controller.check_gate.set()
    # Wait without dispatching Qt events: done and its successful worker snapshot
    # are queued, while the GUI has not accepted the startup acknowledgement.
    worker.join(1)
    assert not worker.is_alive() and "startup" not in panel._results
    assert controller.status.startup_checked and not panel.startup_confirmed
    epoch = panel._epoch
    if change == "disconnect":
        controller.connected = False
    else:
        fields = {
            "fault": dict(state=State.FAULT, fault="INTERLOCK_OPEN", startup_checked=False),
            "new_boot": dict(boot_nonce="new-boot"),
            "new_session": dict(session_nonce="new-session"),
            "stop_generation": dict(stop_generation=controller.status.stop_generation + 1),
            "unchecked": dict(startup_checked=False),
            "armed": dict(state=State.ARMED),
        }[change]
        controller.status = replace(controller.status, **fields)
    commands = list(controller.calls)
    wait_for(lambda: not panel.busy)
    assert panel._epoch == epoch  # No panel stop/new operation cancelled the ACK.
    assert not panel.startup_confirmed and not panel.startup_checkbox.isChecked()
    assert not any(confirmations)  # No transient success signal reaches the job UI.
    assert panel.status == controller.status
    assert panel._connected == controller.connected
    assert panel.last_error and "startup ACK delivery" in panel.last_error
    assert controller.calls == commands  # GUI drain only reads cached attributes.


def test_normal_off_preserves_confirmed_startup_but_new_boot_clears_it(rig):
    panel, controller = rig
    connect(rig)
    confirm_startup(panel)
    panel._launch("ordinary-off", controller.off)
    wait_for(lambda: not panel.busy)
    assert panel.startup_confirmed and panel.status.startup_checked
    assert sum(command == "check" for command, _ in controller.calls) == 1
    # A matching-looking ACK from a new boot cannot inherit the previous human check.
    update_status(panel, controller, boot_nonce="new-boot", startup_checked=True)
    assert not panel.startup_confirmed and not panel.startup_checkbox.isChecked()


def test_programmatic_check_and_unsolicited_firmware_ack_do_not_attest_startup(rig):
    panel, controller = rig
    connect(rig)
    panel.startup_checkbox.setChecked(True)
    QTest.qWait(40)
    assert not panel.startup_confirmed
    assert not any(command in ("off", "check") for command, _ in controller.calls)
    update_status(panel, controller, state=State.DISARMED, startup_checked=True)
    assert not panel.startup_confirmed


@pytest.mark.parametrize("interlock", [True, False])
def test_startup_sequence_matches_real_driver_with_fake_serial_peer(rig, interlock):
    from automatic_transfer_station.laser_controller import LaserController, LaserState
    from test_laser_controller import FakeSerial

    panel, _ = rig
    peers = []

    def serial_factory(**options):
        peer = FakeSerial(interlock=interlock, **options)
        peers.append(peer)
        return peer

    panel._controller_factory = lambda: LaserController(serial_factory=serial_factory)
    confirm_connect(panel)
    wait_for(lambda: not panel.busy)
    assert panel.status.state is LaserState.BOOT_HOLD, panel.message_label.text()
    assert panel.status.comp_high and panel.startup_checkbox.isEnabled()
    assert "OFF" not in peers[0].commands and "CHECK" not in peers[0].commands
    click_startup(panel)
    wait_for(lambda: not panel.busy)
    assert panel.status.state is LaserState.DISARMED
    assert panel.status.comp_high is False
    if interlock:
        assert panel.startup_confirmed and panel.status.startup_checked
        assert peers[0].commands.index("OFF") < peers[0].commands.index("CHECK")
    else:
        assert not panel.startup_confirmed and not panel.status.startup_checked
        assert "CHECK" not in peers[0].commands
        assert panel.last_error
    assert not any(command in ("ARM", "FIRE", "FOLLOW", "KEEP") for command in peers[0].commands)


@pytest.mark.parametrize("failure", ["read", "close"])
def test_real_driver_fault_resource_cleanup_restores_explicit_reconnect(rig, monkeypatch, failure):
    from automatic_transfer_station.laser_controller import LaserController, LaserState
    from test_laser_controller import FakeSerial

    panel, _ = rig
    peers = []

    def serial_factory(**options):
        peer = FakeSerial(**options)
        peers.append(peer)
        return peer

    driver = LaserController(serial_factory=serial_factory)
    driver.CONNECT_TIMEOUT_S = 0.02
    driver.ACK_TIMEOUT_S = 0.08
    driver.READ_TIMEOUT_S = 0.01
    driver.WRITE_TIMEOUT_S = 0.03
    panel._controller_factory = lambda: driver
    confirm_connect(panel)
    wait_for(lambda: not panel.busy and panel.status is not None
             and panel.status.state is LaserState.BOOT_HOLD)
    peer = peers[-1]
    original_close = peer.close
    if failure == "read":
        peer.disappeared = True
        wait_for(lambda: panel.status.state is LaserState.UNKNOWN and not driver.connected)
    else:
        def failed_close():
            raise OSError("injected close failure before resource release")

        monkeypatch.setattr(peer, "close", failed_close)
        panel.disconnect_button.click()
        wait_for(lambda: not panel.busy)
        assert "injected close failure" in panel.last_error
        monkeypatch.setattr(peer, "close", original_close)
    assert not driver.connected and driver.has_serial_resource
    assert panel.disconnect_button.isEnabled() and not panel.connect_button.isEnabled()
    assert not panel.startup_confirmed and panel.status.comp_high is None

    # OFF may be unconfirmed on the failed link; an explicit retry must still
    # release the serial resource. Only then may the operator connect again.
    panel.disconnect_button.click()
    wait_for(lambda: not panel.busy and not driver.has_serial_resource)
    assert peer.closed and driver._reader is None
    assert panel.connect_button.isEnabled() and not panel.disconnect_button.isEnabled()
    confirm_connect(panel)
    wait_for(lambda: not panel.busy and panel.status.state is LaserState.BOOT_HOLD)
    assert len(peers) == 2 and driver.connected and driver.has_serial_resource
    assert panel.last_error is None and not panel.startup_confirmed
    assert not any(command in ("ARM", "FIRE", "FOLLOW", "KEEP")
                   for serial_peer in peers for command in serial_peer.commands)
