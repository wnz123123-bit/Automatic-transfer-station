import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))

_QT_APP = None


def get_app():
    global _QT_APP
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _QT_APP = QApplication([])
        app = _QT_APP
    elif _QT_APP is None:
        _QT_APP = app
    app.setQuitOnLastWindowClosed(False)
    return app


def inline_executor():
    # Synchronous serial executor so a button click dispatches and completes its
    # action before returning, keeping these GUI-logic tests deterministic.
    from automatic_transfer_station.temperature_panel import InlineSerialExecutor

    return InlineSerialExecutor()


def wait_until(predicate, timeout=2.0):
    app = get_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def wait_plain(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeTemperatureController:
    def __init__(self):
        self.calls = []
        self.is_connected = False
        self.last_error = ""
        self.last_pid_profile = None

    def connect(self):
        self.calls.append(("connect",))
        self.is_connected = True
        return True

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.is_connected = False

    def read_live_data(self):
        self.calls.append(("read_live_data",))
        return {
            "temperature": 32.45,
            "effective_setpoint": 45.0,
            "output_percent": 12.5,
            "max_output_duty_percent": 40,
            "run_state": "输出开启",
            "error_code": 0,
        }

    def fast_set_temperature(self, target):
        self.calls.append(("fast_set_temperature", target))
        self.last_pid_profile = {"name": "pid_50c"}
        return True

    def ramp_to_temperature(self, target, rate):
        self.calls.append(("ramp_to_temperature", target, rate))
        self.last_pid_profile = {"name": "pid_50c"}
        return True

    def stop_heating(self):
        self.calls.append(("stop_heating",))
        return True


class TemperaturePanelTests(unittest.TestCase):
    def test_panel_connects_to_selected_rd105_port_and_updates_live_readouts(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        fake = FakeTemperatureController()
        created_ports = []
        controls = {}

        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: created_ports.append(port) or fake,
            serial_executor=inline_executor(),
            port_lister=lambda: [
                SimpleNamespace(device="COM1", description="Standard port", hwid=""),
                SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="USB VID:PID=1A86:7523"),
            ],
        )

        self.assertEqual("COM5", controls["tempPortCombo"].currentText())

        panel.connect_to_device()

        self.assertEqual(["COM5"], created_ports)
        self.assertIn(("connect",), fake.calls)
        self.assertIn(("read_live_data",), fake.calls)
        self.assertIn("COM5", controls["tempConnectionLabel"].text())
        self.assertIn("32.45", controls["tempCurrentTempLabel"].text())
        self.assertIn("45.00", controls["tempLiveSetpointLabel"].text())
        self.assertIn("12.5", controls["tempOutputLabel"].text())
        self.assertIn("40.0", controls["tempMaxOutputDutyLabel"].text())
        self.assertNotIn("tempMaxOutputDutySpin", controls)
        self.assertNotIn("tempSetMaxOutputDuty", controls)
        self.assertNotIn("tempPidProfileLabel", controls)
        self.assertIn("输出开启", controls["tempRunStateLabel"].text())
        self.assertEqual("0", controls["tempErrorCodeLabel"].text())
        self.assertEqual(1, controls["tempPlot"].sample_count())
        self.assertIn("32.45", controls["tempPlotStatusLabel"].text())

    def test_panel_buttons_call_rd105_write_methods(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        fake = FakeTemperatureController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=inline_executor(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )
        panel.connect_to_device()

        controls["tempTargetSpin"].setValue(45.0)
        controls["tempRateSpin"].setValue(18.0)
        controls["tempFastSet"].click()
        controls["tempRamp"].click()
        controls["tempStopHeating"].click()

        self.assertIn(("fast_set_temperature", 45.0), fake.calls)
        self.assertIn(("ramp_to_temperature", 45.0, 18.0), fake.calls)
        self.assertIn(("stop_heating",), fake.calls)
        self.assertFalse(any(call[0] == "set_max_output_duty" for call in fake.calls))

    def test_temperature_buttons_have_pressed_visual_feedback(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        panel = TemperaturePanel(
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        style = panel.styleSheet()

        self.assertIn("QPushButton:pressed", style)
        self.assertIn("QPushButton#tempConnect:pressed", style)
        self.assertIn("QPushButton#tempFastSet:pressed", style)
        self.assertIn("QPushButton#tempStopHeating:pressed", style)

    def test_temperature_command_buttons_update_status_before_controller_call(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        fake = FakeTemperatureController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=inline_executor(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )
        panel.connect_to_device()
        status_at_call = {}

        def fast_set_temperature(target):
            status_at_call["fast"] = controls["tempStatusLabel"].text()
            fake.calls.append(("fast_set_temperature", target))
            return True

        def ramp_to_temperature(target, rate):
            status_at_call["ramp"] = controls["tempStatusLabel"].text()
            fake.calls.append(("ramp_to_temperature", target, rate))
            return True

        def stop_heating():
            status_at_call["stop"] = controls["tempStatusLabel"].text()
            fake.calls.append(("stop_heating",))
            return True

        fake.fast_set_temperature = fast_set_temperature
        fake.ramp_to_temperature = ramp_to_temperature
        fake.stop_heating = stop_heating

        controls["tempFastSet"].click()
        controls["tempRamp"].click()
        controls["tempStopHeating"].click()

        self.assertIn("正在发送快速设温", status_at_call["fast"])
        self.assertIn("正在发送按速率控温", status_at_call["ramp"])
        self.assertIn("正在关闭输出", status_at_call["stop"])

    def test_panel_shows_over_temperature_alarm_from_snapshot(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        panel.on_temperature_snapshot(
            {
                "temperature": 206.0,
                "effective_setpoint": 200.0,
                "output_percent": 0.0,
                "max_output_duty_percent": 50,
                "run_state": "超温保护",
                "error_code": 0,
                "safety_alarm": "超温保护：当前温度 206.00 °C 高于 205.0 °C，已关闭输出",
            }
        )

        self.assertIn("超温保护", controls["tempRunStateLabel"].text())
        self.assertIn("超温保护", controls["tempStatusLabel"].text())

    def test_heat_warning_only_visible_when_output_energized(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="", hwid="")],
        )

        warning = controls["tempHeatWarning"]
        # isHidden() reflects the explicit show/hide state without needing the panel
        # to be shown (isVisible() would require all ancestors visible).
        self.assertTrue(warning.isHidden(), "warning hidden at rest")

        def snap(run_state):
            return {
                "temperature": 80.0,
                "effective_setpoint": 80.0,
                "output_percent": 10.0,
                "max_output_duty_percent": 50,
                "run_state": run_state,
                "error_code": 0,
            }

        panel.on_temperature_snapshot(snap("输出开启"))
        self.assertFalse(warning.isHidden(), "warning must show while output is on")

        panel.on_temperature_snapshot(snap("输出关闭"))
        self.assertTrue(warning.isHidden(), "warning must hide when output is off")

        # Over-temp guard that failed to disable: output still on -> still a hazard.
        panel.on_temperature_snapshot(snap("超温保护-关闭输出失败"))
        self.assertFalse(warning.isHidden(), "warning must show if disable failed")

        # Over-temp guard that DID disable output: off -> hidden.
        panel.on_temperature_snapshot(snap("超温保护"))
        self.assertTrue(warning.isHidden(), "warning must hide when output cut off")

    def test_panel_shows_software_ramp_status_from_snapshot(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        panel.on_temperature_snapshot(
            {
                "temperature": 40.0,
                "effective_setpoint": 40.1,
                "output_percent": 10.0,
                "max_output_duty_percent": 40,
                "run_state": "输出开启",
                "error_code": 0,
                "software_ramp_active": True,
                "software_ramp_final_target": 41.0,
                "software_ramp_rate_per_hour": 36.0,
            }
        )

        self.assertIn("软件斜坡控温中", controls["tempStatusLabel"].text())
        self.assertIn("41.00", controls["tempStatusLabel"].text())
        self.assertIn("36.0", controls["tempStatusLabel"].text())

    def test_panel_shows_control_error_from_snapshot(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        panel.on_temperature_snapshot(
            {
                "temperature": 40.0,
                "effective_setpoint": 40.0,
                "output_percent": 10.0,
                "max_output_duty_percent": 40,
                "run_state": "输出开启",
                "error_code": 0,
                "control_error": "TG 写入失败",
            }
        )

        self.assertIn("TG 写入失败", controls["tempStatusLabel"].text())

    def test_panel_handles_ramp_snapshot_without_rate(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        panel.on_temperature_snapshot(
            {
                "temperature": 40.0,
                "effective_setpoint": 40.1,
                "output_percent": 10.0,
                "max_output_duty_percent": 40,
                "run_state": "输出开启",
                "error_code": 0,
                "software_ramp_active": True,
                "software_ramp_final_target": 41.0,
            }
        )

        self.assertIn("软件斜坡控温中", controls["tempStatusLabel"].text())
        self.assertIn("速率 --", controls["tempStatusLabel"].text())

    def test_temperature_plot_replaces_placeholder_and_records_snapshots(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        self.assertIn("tempPlot", controls)
        self.assertNotIn("tempPlotPlaceholder", controls)
        self.assertEqual(0, controls["tempPlot"].sample_count())

        panel.on_temperature_snapshot({"temperature": 30.0, "effective_setpoint": 40.0})
        panel.on_temperature_snapshot({"temperature": 31.5, "effective_setpoint": 41.0})

        self.assertEqual(2, controls["tempPlot"].sample_count())
        self.assertIn("31.50", controls["tempPlotStatusLabel"].text())

    def test_output_and_max_output_duty_readouts_are_clearly_distinguishable(self):
        # Bug #10 (display only, conservative): the raw-PWM-derived output value and
        # the max_output_duty percent sit next to each other. The PWM full-scale is
        # NOT documented anywhere in this repo, so we do NOT change the divisor
        # (output_percent = PWM_raw / 20000.0 is preserved upstream). This test
        # pins the CURRENT numeric behavior and asserts the two readouts are
        # visually distinguishable so they cannot be confused.
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        panel.on_temperature_snapshot(
            {
                "temperature": 30.0,
                "effective_setpoint": 40.0,
                "output_percent": 12.5,
                "max_output_duty_percent": 40,
                "run_state": "输出开启",
                "error_code": 0,
            }
        )

        output_text = controls["tempOutputLabel"].text()
        max_duty_text = controls["tempMaxOutputDutyLabel"].text()

        # Pin current numeric behavior.
        self.assertIn("12.5", output_text)
        self.assertIn("40.0", max_duty_text)
        # The two readouts must not render identically-formatted percentages that
        # could be confused; the max-output-duty stays a plain "%" reading while
        # the PWM-derived output carries a distinguishing marker.
        self.assertNotEqual(output_text, max_duty_text)
        self.assertIn("%", max_duty_text)
        self.assertIn("PWM", output_text)

    def test_slow_serial_connect_and_commands_do_not_block_gui_thread(self):
        # Bug #6/I5: blocking serial I/O must run OFF the GUI thread so the
        # operator can always reach 关闭输出. With the default (threaded) executor
        # and a deliberately slow fake controller, a clicked 连接/设温/控温/关闭输出
        # must return in well under the hardware delay.
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()

        class SlowController:
            DELAY = 0.4

            def __init__(self):
                self.is_connected = False
                self.last_error = ""
                self.last_pid_profile = None
                self.calls = []
                self.read_started = threading.Event()

            def connect(self):
                time.sleep(self.DELAY)
                self.is_connected = True
                self.calls.append(("connect",))
                return True

            def disconnect(self):
                self.is_connected = False
                self.calls.append(("disconnect",))

            def read_live_data(self):
                self.read_started.set()
                time.sleep(self.DELAY)
                self.calls.append(("read_live_data",))
                return {
                    "temperature": 33.0,
                    "effective_setpoint": 45.0,
                    "output_percent": 5.0,
                    "max_output_duty_percent": 40,
                    "run_state": "输出开启",
                    "error_code": 0,
                }

            def fast_set_temperature(self, target):
                time.sleep(self.DELAY)
                self.calls.append(("fast_set_temperature", target))
                return True

            def ramp_to_temperature(self, target, rate):
                time.sleep(self.DELAY)
                self.calls.append(("ramp_to_temperature", target, rate))
                return True

            def stop_heating(self):
                time.sleep(self.DELAY)
                self.calls.append(("stop_heating",))
                return True

        slow = SlowController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: slow,
            auto_poll=False,  # avoid the 1s timer racing the assertions
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )

        start = time.monotonic()
        controls["tempConnect"].click()  # triggers toggle_connection -> connect_to_device
        connect_elapsed = time.monotonic() - start
        self.assertLess(connect_elapsed, 0.2)  # click returns before the slow connect

        # The connect completes on the worker thread; wait for it without blocking.
        self.assertTrue(wait_until(lambda: slow.is_connected, timeout=2.0))
        self.assertTrue(wait_until(lambda: "已连接" in controls["tempConnectionLabel"].text(), timeout=2.0))

        # The connect handler kicks off an initial poll; let it fully settle so the
        # in-flight guard is clear before we exercise a fresh, isolated read.
        self.assertTrue(wait_until(lambda: "33.00" in controls["tempCurrentTempLabel"].text(), timeout=2.0))
        self.assertTrue(wait_until(lambda: not panel._poll_in_flight, timeout=2.0))

        # A slow live read must not block the GUI thread.
        slow.read_started.clear()
        start = time.monotonic()
        panel.poll_temperature_once()
        poll_elapsed = time.monotonic() - start
        self.assertLess(poll_elapsed, 0.2)  # returned before the 0.4s read finished
        self.assertTrue(slow.read_started.wait(2.0))  # but the read DID run, off-thread

        # Set-temperature must also return quickly and run off-thread.
        controls["tempTargetSpin"].setValue(50.0)
        start = time.monotonic()
        controls["tempFastSet"].click()
        fast_elapsed = time.monotonic() - start
        self.assertLess(fast_elapsed, 0.2)
        self.assertTrue(wait_until(lambda: ("fast_set_temperature", 50.0) in slow.calls, timeout=2.0))

        # 关闭输出 must return quickly even while serial comms are slow - this is the
        # safety-critical path the operator needs during comms trouble.
        start = time.monotonic()
        controls["tempStopHeating"].click()
        stop_elapsed = time.monotonic() - start
        self.assertLess(stop_elapsed, 0.2)
        self.assertTrue(wait_until(lambda: ("stop_heating",) in slow.calls, timeout=2.0))

        panel.disconnect_from_device()
        self.assertTrue(wait_until(lambda: ("disconnect",) in slow.calls, timeout=2.0))
        panel.prepare_for_close(timeout_ms=2000)
        get_app().processEvents()  # flush any pending deleteLater from finished threads

    def test_disconnect_button_does_not_block_gui_thread(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()

        class SlowDisconnectController(FakeTemperatureController):
            def __init__(self):
                super().__init__()
                self.is_connected = True
                self.disconnect_started = threading.Event()

            def disconnect(self):
                self.disconnect_started.set()
                time.sleep(0.4)
                super().disconnect()

        slow = SlowDisconnectController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: slow,
            auto_poll=False,
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )
        panel.controller = slow
        controls["tempConnect"].setText("断开连接")
        controls["tempConnectionLabel"].setText("已连接 COM5")

        start = time.monotonic()
        controls["tempConnect"].click()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.2)
        self.assertTrue(slow.disconnect_started.wait(2.0))
        self.assertTrue(wait_until(lambda: ("disconnect",) in slow.calls, timeout=2.0))
        self.assertTrue(wait_until(lambda: "已断开" in controls["tempConnectionLabel"].text(), timeout=2.0))
        self.assertIn("已断开", controls["tempConnectionLabel"].text())
        panel.prepare_for_close(timeout_ms=2000)
        get_app().processEvents()

    def test_disconnect_surfaces_heater_disable_failure_to_operator(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        class DisableFailController(FakeTemperatureController):
            def disconnect(self):
                super().disconnect()
                self.last_error = "断开连接时关闭输出失败"

        get_app()
        fake = DisableFailController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=inline_executor(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="RD105", hwid="")],
        )
        panel.connect_to_device()

        panel.disconnect_from_device()

        # A failed heater-disable during disconnect must NOT read as a clean shutdown.
        self.assertIn("关闭输出失败", controls["tempConnectionLabel"].text())
        self.assertIn("关闭输出失败", controls["tempStatusLabel"].text())


class TemperaturePanelStuckPollTests(unittest.TestCase):
    """BP2 panel side: a STUCK poll (read that never returns) is a comm failure."""

    def _make_panel(self, fake):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=inline_executor(),
            auto_poll=False,
            port_lister=lambda: [SimpleNamespace(device="COM5", description="RD105", hwid="")],
        )
        return panel, controls

    def test_poll_records_in_flight_timestamp(self):
        # When a poll is dispatched the panel must stamp when _poll_in_flight was
        # set so a later tick can measure how long the read has been outstanding.
        fake = FakeTemperatureController()
        fake.is_connected = True
        panel, controls = self._make_panel(fake)
        now = [1000.0]
        panel._clock = lambda: now[0]

        # A read that never completes: keep _poll_in_flight latched.
        class NeverReturns:
            def submit(self_inner, kind, action, on_done, on_error):
                pass  # simulate the worker never calling back

        panel._serial_executor = NeverReturns()
        panel.controller = fake

        panel.poll_temperature_once()

        self.assertTrue(panel._poll_in_flight)
        self.assertEqual(1000.0, panel._poll_in_flight_since)

    def test_stuck_poll_past_timeout_surfaces_alarm_and_recovers(self):
        from automatic_transfer_station.rd105_config import POLL_STUCK_TIMEOUT_SECONDS

        fake = FakeTemperatureController()
        fake.is_connected = True
        panel, controls = self._make_panel(fake)
        now = [1000.0]
        panel._clock = lambda: now[0]

        # Stuck read: the worker never calls back, so _poll_in_flight stays set.
        dispatched = []

        class NeverReturns:
            def submit(self_inner, kind, action, on_done, on_error):
                dispatched.append(kind)

        panel._serial_executor = NeverReturns()
        panel.controller = fake

        # First tick dispatches a poll that never returns.
        panel.poll_temperature_once()
        self.assertTrue(panel._poll_in_flight)
        self.assertEqual(["poll"], dispatched)

        # A later tick BEFORE the timeout: still in-flight, no alarm, no recovery.
        now[0] += POLL_STUCK_TIMEOUT_SECONDS - 1.0
        panel.poll_temperature_once()
        self.assertTrue(panel._poll_in_flight)
        self.assertEqual(["poll"], dispatched)  # dropped overlapping poll

        # A tick PAST the timeout: treat as comm failure, surface alarm, recover.
        now[0] += 2.0  # now beyond the stuck timeout
        panel.poll_temperature_once()

        self.assertIn("通信", controls["tempStatusLabel"].text())
        # The flag was reset so polling can recover (no permanent deadlock).
        self.assertFalse(panel._poll_in_flight)

    def test_completed_poll_clears_in_flight_so_no_false_stuck_alarm(self):
        fake = FakeTemperatureController()
        fake.is_connected = True
        panel, controls = self._make_panel(fake)
        now = [1000.0]
        panel._clock = lambda: now[0]
        panel.controller = fake

        # Inline executor completes immediately, clearing the in-flight flag.
        panel.poll_temperature_once()
        self.assertFalse(panel._poll_in_flight)

        # Even far in the future, a fresh tick must NOT raise a stuck-poll alarm.
        now[0] += 10_000.0
        panel.poll_temperature_once()
        self.assertNotIn("通信丢失", controls["tempStatusLabel"].text())


class Package6TemperatureSafetyTests(unittest.TestCase):
    """Package 6 Fix 1: high-temperature setpoint confirmation gate."""

    def _make_panel(self, fake=None):
        from automatic_transfer_station.temperature_panel import TemperaturePanel
        get_app()
        if fake is None:
            fake = FakeTemperatureController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=inline_executor(),
            port_lister=lambda: [SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")],
        )
        panel.connect_to_device()
        return panel, controls, fake

    def test_confirm_hook_is_callable(self):
        from automatic_transfer_station.temperature_panel import TemperaturePanel
        get_app()
        panel = TemperaturePanel(
            controller_factory=lambda port: FakeTemperatureController(),
            port_lister=lambda: [],
        )
        self.assertTrue(callable(panel._confirm))

    def test_warn_confirm_temperature_constant_is_150(self):
        from automatic_transfer_station.temperature_panel import WARN_CONFIRM_TEMPERATURE
        self.assertEqual(150.0, WARN_CONFIRM_TEMPERATURE)

    def test_fast_set_below_threshold_does_not_prompt(self):
        panel, controls, fake = self._make_panel()
        confirmed = []
        panel._confirm = lambda title, text: confirmed.append((title, text)) or False

        controls["tempTargetSpin"].setValue(50.0)  # below 150
        panel.apply_fast_set()

        # No confirmation dialog shown; command still dispatched
        self.assertEqual([], confirmed)
        self.assertIn(("fast_set_temperature", 50.0), fake.calls)

    def test_fast_set_at_threshold_prompts_and_confirm_false_blocks_command(self):
        panel, controls, fake = self._make_panel()
        panel._confirm = lambda title, text: False

        # Clear calls from connect
        fast_calls_before = [c for c in fake.calls if c[0] == "fast_set_temperature"]
        controls["tempTargetSpin"].setValue(150.0)  # at threshold
        result = panel.apply_fast_set()

        fast_calls_after = [c for c in fake.calls if c[0] == "fast_set_temperature"]
        self.assertFalse(result)
        self.assertEqual(fast_calls_before, fast_calls_after)

    def test_fast_set_above_threshold_prompts_and_confirm_true_proceeds(self):
        panel, controls, fake = self._make_panel()
        panel._confirm = lambda title, text: True

        controls["tempTargetSpin"].setValue(200.0)  # above threshold
        result = panel.apply_fast_set()

        self.assertIn(("fast_set_temperature", 200.0), fake.calls)

    def test_rate_set_below_threshold_does_not_prompt(self):
        panel, controls, fake = self._make_panel()
        confirmed = []
        panel._confirm = lambda title, text: confirmed.append((title, text)) or False

        controls["tempTargetSpin"].setValue(100.0)  # below 150
        controls["tempRateSpin"].setValue(10.0)
        panel.apply_rate_set()

        self.assertEqual([], confirmed)
        self.assertIn(("ramp_to_temperature", 100.0, 10.0), fake.calls)

    def test_rate_set_above_threshold_confirm_false_blocks_command(self):
        panel, controls, fake = self._make_panel()
        panel._confirm = lambda title, text: False

        rate_calls_before = [c for c in fake.calls if c[0] == "ramp_to_temperature"]
        controls["tempTargetSpin"].setValue(160.0)
        controls["tempRateSpin"].setValue(5.0)
        result = panel.apply_rate_set()

        rate_calls_after = [c for c in fake.calls if c[0] == "ramp_to_temperature"]
        self.assertFalse(result)
        self.assertEqual(rate_calls_before, rate_calls_after)

    def test_rate_set_above_threshold_confirm_true_proceeds(self):
        panel, controls, fake = self._make_panel()
        panel._confirm = lambda title, text: True

        controls["tempTargetSpin"].setValue(160.0)
        controls["tempRateSpin"].setValue(5.0)
        panel.apply_rate_set()

        self.assertIn(("ramp_to_temperature", 160.0, 5.0), fake.calls)


class TemperaturePanelCloseTests(unittest.TestCase):
    """Integration Fix 1 — prepare_for_close / _closing / detach pattern."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_panel(self, fake=None, serial_executor=None):
        from automatic_transfer_station.temperature_panel import TemperaturePanel

        get_app()
        if fake is None:
            fake = FakeTemperatureController()
        controls = {}
        panel = TemperaturePanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda port: fake,
            serial_executor=serial_executor if serial_executor is not None else inline_executor(),
            auto_poll=False,
            port_lister=lambda: [
                SimpleNamespace(device="COM5", description="USB-SERIAL CH340", hwid="")
            ],
        )
        return panel, controls, fake

    # ------------------------------------------------------------------
    # Test: _closing flag starts False; prepare_for_close sets it
    # ------------------------------------------------------------------

    def test_closing_flag_starts_false(self):
        panel, controls, fake = self._make_panel()
        self.assertFalse(panel._closing)

    def test_prepare_for_close_sets_closing_flag(self):
        panel, controls, fake = self._make_panel()
        panel.prepare_for_close()
        self.assertTrue(panel._closing)

    # ------------------------------------------------------------------
    # Test: worker signals are no-ops after _closing is set
    # ------------------------------------------------------------------

    def test_poll_result_slot_noop_when_closing(self):
        """_handle_poll_result after _closing must not write any widget."""
        panel, controls, fake = self._make_panel()
        panel.connect_to_device()
        # Record the status text before closing
        before = controls["tempStatusLabel"].text()
        panel._closing = True
        # Deliver a poll result — should be silently ignored
        panel._handle_poll_result("poll", {
            "temperature": 99.0,
            "effective_setpoint": 100.0,
            "output_percent": 5.0,
            "max_output_duty_percent": 50,
            "run_state": "输出开启",
            "error_code": 0,
        })
        # The temperature label should NOT have been updated to 99.00
        self.assertNotIn("99.00", controls["tempCurrentTempLabel"].text())

    def test_poll_failure_slot_noop_when_closing(self):
        """_handle_poll_failure after _closing must not write status."""
        panel, controls, fake = self._make_panel()
        panel.connect_to_device()
        panel._closing = True
        before = controls["tempStatusLabel"].text()
        panel._handle_poll_failure("poll", "some serial error")
        # Status must not have been overwritten with the error message
        self.assertNotIn("some serial error", controls["tempStatusLabel"].text())

    def test_command_result_slot_noop_when_closing(self):
        """_handle_command_result after _closing must not write any widget."""
        panel, controls, fake = self._make_panel()
        panel.connect_to_device()
        panel._closing = True
        before = controls["tempRunStateLabel"].text()
        panel._handle_command_result("stop", fake, None, True)
        # stop branch would normally write "输出关闭"; must not happen after _closing
        self.assertNotEqual("输出关闭", controls["tempRunStateLabel"].text())

    def test_command_failure_slot_noop_when_closing(self):
        """_handle_command_failure after _closing must not write status."""
        panel, controls, fake = self._make_panel()
        panel.connect_to_device()
        panel._closing = True
        panel._handle_command_failure("fast_set", fake, "forced error")
        self.assertNotIn("forced error", controls["tempStatusLabel"].text())

    def test_on_temperature_snapshot_noop_when_closing(self):
        """on_temperature_snapshot after _closing must not touch widgets."""
        panel, controls, fake = self._make_panel()
        initial_temp = controls["tempCurrentTempLabel"].text()
        panel._closing = True
        panel.on_temperature_snapshot({
            "temperature": 77.0,
            "effective_setpoint": 80.0,
            "output_percent": 1.0,
            "max_output_duty_percent": 40,
            "run_state": "输出开启",
            "error_code": 0,
        })
        self.assertEqual(initial_temp, controls["tempCurrentTempLabel"].text())

    # ------------------------------------------------------------------
    # Test: normal operation still works (closing flag not set)
    # ------------------------------------------------------------------

    def test_poll_result_slot_works_normally_when_not_closing(self):
        """_handle_poll_result must still update widgets when not closing."""
        panel, controls, fake = self._make_panel()
        panel.connect_to_device()
        self.assertFalse(panel._closing)
        panel._poll_in_flight = True
        panel._handle_poll_result("poll", {
            "temperature": 55.5,
            "effective_setpoint": 60.0,
            "output_percent": 3.0,
            "max_output_duty_percent": 40,
            "run_state": "输出开启",
            "error_code": 0,
        })
        self.assertIn("55.50", controls["tempCurrentTempLabel"].text())

    # ------------------------------------------------------------------
    # Test: still-running thread at prepare_for_close is detached
    # ------------------------------------------------------------------

    def test_running_thread_at_prepare_for_close_is_detached(self):
        """A SerialTaskThread still running when prepare_for_close is called
        must be reparented to None and kept in _detached_threads."""
        import threading
        from automatic_transfer_station.temperature_panel import ThreadedSerialExecutor

        get_app()
        ready = threading.Event()
        hold = threading.Event()

        def slow_action():
            ready.set()
            hold.wait(timeout=5.0)
            return {"temperature": 25.0}

        executor = ThreadedSerialExecutor()
        panel, controls, fake = self._make_panel(serial_executor=executor)
        fake.is_connected = True
        panel.controller = fake

        # Submit a task that holds until we release it
        executor.submit("poll", slow_action, lambda k, r: None, lambda k, m: None)
        self.assertTrue(ready.wait(timeout=2.0), "slow_action should have started")

        # Now prepare_for_close with a very short timeout so the wait expires
        panel.prepare_for_close(timeout_ms=10)

        # The executor must have a _detached_threads list with at least one entry
        self.assertTrue(panel._closing)
        detached = getattr(executor, "_detached_threads", None)
        self.assertIsNotNone(detached, "_detached_threads should exist on executor")
        self.assertGreater(len(detached), 0, "at least one thread should be detached")

        # The detached thread's parent must be None (so the panel doesn't own it)
        for t in detached:
            self.assertIsNone(t.parent(), "detached thread must have no parent")

        # Release the slow action so the thread can exit cleanly
        hold.set()
        # Give it a moment to finish, then reap the detached QThread wrapper so
        # later tests can safely pump the shared QApplication event loop.
        for t in detached:
            t.wait(3000)
        executor.reap_detached_threads()

    def test_prepare_for_close_is_idempotent(self):
        """Calling prepare_for_close twice must not raise."""
        panel, controls, fake = self._make_panel()
        panel.prepare_for_close()
        panel.prepare_for_close()  # second call — must not raise
        self.assertTrue(panel._closing)


class TemperaturePanelCloseWithMainWindowTests(unittest.TestCase):
    """Verify closeEvent calls prepare_for_close on temperature_panel."""

    def _dispose_window(self, window):
        # The test calls close() only to exercise closeEvent. Remove any posted
        # events for this test window tree so later tests sharing the same
        # QApplication do not process teardown callbacks from this closed window.
        from PySide6.QtCore import QCoreApplication, QObject

        try:
            objects = [window, *window.findChildren(QObject)]
            for obj in objects:
                QCoreApplication.removePostedEvents(obj)
        except RuntimeError:
            pass

    def _make_window(self):
        get_app()
        import tempfile
        from automatic_transfer_station.main_window import MainWindow, MotionCommandQueue

        class MinController:
            calls = []
            is_open = False
            positions = {axis: 0.0 for axis in range(1, 10)}
            axis_calibrations = None

            def open_board(self): pass
            def close_board(self): pass
            def reset_board(self): pass
            def stop_axis(self, axis): pass
            def stop_all(self): pass
            def get_axis_position(self, axis): return 0.0
            def get_axis9_angle(self): return 0.0
            def set_axis_calibrations(self, c): self.axis_calibrations = c
            def relative_move(self, *a): pass
            def start_jog(self, *a): pass
            def zero_axis3(self): pass
            def mark_axis3(self): return 0.0
            def move_axis3_to_mark(self): pass
            def move_axis3_to_position(self, p): pass
            def move_axis_to_coordinate(self, *a): pass
            def home_axis9(self): pass
            def move_axis9_to_angle(self, *a): pass

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            window = MainWindow(
                controller=MinController(),
                position_store_path=Path(tmp) / "pos.json",
                runtime_calibration_path=Path(tmp) / "cal.json",
                microscope_position_store_path=Path(tmp) / "micro.json",
                motion_command_queue_factory=lambda parent: MotionCommandQueue(parent, inline=True),
            )
        return window

    def test_close_event_calls_prepare_for_close_on_temperature_panel(self):
        """closeEvent must call prepare_for_close() on the temperature panel."""
        window = self._make_window()
        called = []
        orig = getattr(window.temperature_panel, "prepare_for_close", None)
        window.temperature_panel.prepare_for_close = lambda *a, **kw: called.append(True) or (orig(*a, **kw) if orig else True)

        window._prepare_for_close()

        self.assertTrue(called, "prepare_for_close must be called from closeEvent")
        self.assertTrue(window.temperature_panel._closing,
                        "_closing must be set on the temperature panel after close")
        self._dispose_window(window)

    def test_disconnect_still_called_after_prepare_for_close(self):
        """disconnect_from_device must still be called even when prepare_for_close runs."""
        window = self._make_window()
        fake = FakeTemperatureController()
        fake.is_connected = True
        window.temperature_panel.controller = fake

        window._prepare_for_close()

        # disconnect() is submitted asynchronously on close, so wait for the
        # worker action without re-entering Qt while the window is tearing down.
        self.assertTrue(wait_plain(lambda: ("disconnect",) in fake.calls, timeout=2.0))
        window.temperature_panel._serial_executor.reap_detached_threads()
        self._dispose_window(window)

    def test_close_event_completes_heater_off_when_disconnect_responsive(self):
        # Safety: the RD105 keeps heating if the PC exits without ENABLE=0, so on
        # close the heater-off (disconnect) must actually COMPLETE before close
        # returns when the serial port is responsive — not race the process exit.
        window = self._make_window()

        class SlowDisconnectController(FakeTemperatureController):
            def __init__(self):
                super().__init__()
                self.is_connected = True
                self.disconnect_finished = threading.Event()

            def disconnect(self):
                time.sleep(0.3)
                super().disconnect()
                self.disconnect_finished.set()

        fake = SlowDisconnectController()
        window.temperature_panel.controller = fake

        start = time.monotonic()
        window._prepare_for_close()
        elapsed = time.monotonic() - start

        # Close waited for the heater-off to finish (>= the 0.3 s disconnect)...
        self.assertTrue(fake.disconnect_finished.is_set(),
                        "heater-off (disconnect) must complete before close returns")
        self.assertIn(("disconnect",), fake.calls)
        self.assertGreaterEqual(elapsed, 0.25)
        # ...but stayed well within the bounded close timeout (3 s).
        self.assertLess(elapsed, 2.0)
        window.temperature_panel._serial_executor.reap_detached_threads()
        self._dispose_window(window)

    def test_close_event_is_bounded_when_disconnect_wedged(self):
        # A wedged serial driver must not hang window close: the heater-off wait is
        # capped at HEATER_OFF_CLOSE_TIMEOUT_MS and the worker is then detached.
        window = self._make_window()
        window.HEATER_OFF_CLOSE_TIMEOUT_MS = 200

        release = threading.Event()

        class WedgedDisconnectController(FakeTemperatureController):
            def __init__(self):
                super().__init__()
                self.is_connected = True

            def disconnect(self):
                release.wait(5.0)  # simulate a wedged serial driver
                super().disconnect()

        fake = WedgedDisconnectController()
        window.temperature_panel.controller = fake

        start = time.monotonic()
        window._prepare_for_close()
        elapsed = time.monotonic() - start

        # Close returned promptly (bounded by the 200 ms cap), did not wait 5 s.
        self.assertLess(elapsed, 1.5)
        # Release the wedged worker and let it finish before reaping.
        release.set()
        self.assertTrue(wait_plain(lambda: ("disconnect",) in fake.calls, timeout=2.0))
        window.temperature_panel._serial_executor.reap_detached_threads()
        self._dispose_window(window)


if __name__ == "__main__":
    unittest.main()
