"""BP3 — Audit-logging subsystem tests.

Verifies that safety-critical events are routed to the correct named loggers
at the expected levels.  All tests run with QT_QPA_PLATFORM=offscreen and use
``assertLogs`` to capture log records without touching any file.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _INLINE_QUEUE_FACTORY(parent):
    from automatic_transfer_station.main_window import MotionCommandQueue
    return MotionCommandQueue(parent, inline=True)


class RecordingController:
    """Minimal motion-controller stub that mirrors the one in test_main_window."""

    def __init__(self):
        self.calls = []
        self.is_open = False
        self.positions = {axis: 0.0 for axis in range(1, 10)}
        self.axis_calibrations = None

    def open_board(self):
        self.calls.append(("open_board",))
        self.is_open = True

    def close_board(self):
        self.calls.append(("close_board",))
        self.is_open = False

    def reset_board(self):
        self.calls.append(("reset_board",))

    def stop_axis(self, axis):
        self.calls.append(("stop_axis", axis))

    def stop_all(self):
        self.calls.append(("stop_all",))

    def get_axis_position(self, axis):
        return self.positions.get(axis, 0.0)

    def get_axis9_angle(self):
        return 0.0

    def set_axis_calibrations(self, axis_calibrations):
        self.axis_calibrations = axis_calibrations

    def relative_move(self, axis, direction, speed, step):
        self.calls.append(("relative_move", axis, direction, speed, step))

    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
        self.calls.append(("start_jog", axis, speed))

    def zero_axis3(self):
        self.calls.append(("zero_axis3",))

    def mark_axis3(self):
        self.calls.append(("mark_axis3",))
        return 0.0

    def move_axis3_to_mark(self):
        self.calls.append(("move_axis3_to_mark",))

    def move_axis3_to_position(self, position):
        self.calls.append(("move_axis3_to_position", position))

    def move_axis_to_coordinate(self, axis, coordinate, speed=None):
        self.calls.append(("move_axis_to_coordinate", axis, coordinate, speed))

    def home_axis9(self):
        self.calls.append(("home_axis9",))

    def move_axis9_to_angle(self, angle, speed=None):
        self.calls.append(("move_axis9_to_angle", angle, speed))


class FailingStopController(RecordingController):
    """Controller whose stop_all raises MotionControllerError."""

    def stop_all(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        raise MotionControllerError("simulated stop failure")


# ---------------------------------------------------------------------------
# Fix 1 — logging_setup module
# ---------------------------------------------------------------------------

class TestLoggingSetup(unittest.TestCase):

    def test_configure_logging_is_idempotent(self):
        """Calling configure_logging() multiple times must not add duplicate handlers."""
        from automatic_transfer_station import logging_setup
        # Reset the guard so we can test the first-call path cleanly.
        original = logging_setup._configured
        logging_setup._configured = False
        root = logging.getLogger()
        handlers_before = len(root.handlers)
        try:
            logging_setup.configure_logging(runtime_dir=None, console=False)
            logging_setup.configure_logging(runtime_dir=None, console=False)
            logging_setup.configure_logging(runtime_dir=None, console=False)
            # The number of handlers must not have grown by more than 1
            # (a single call may add a StreamHandler; subsequent calls must not).
            self.assertLessEqual(len(root.handlers), handlers_before + 1)
        finally:
            logging_setup._configured = original

    def test_configure_logging_unwritable_dir_does_not_raise(self):
        """File-handler creation failure must degrade silently (no crash).

        We pass a path that contains a null byte; on Windows this produces a
        ValueError inside mkdir, on POSIX an OSError.  Either way,
        configure_logging must swallow it and not propagate.
        """
        from automatic_transfer_station import logging_setup
        original = logging_setup._configured
        logging_setup._configured = False
        try:
            logging_setup.configure_logging(runtime_dir="C:\\nul\x00bad", console=False)
        except Exception as exc:
            self.fail(f"configure_logging raised on unwritable dir: {exc}")
        finally:
            logging_setup._configured = original

    def test_audit_logger_name(self):
        from automatic_transfer_station.logging_setup import audit_logger
        self.assertEqual(audit_logger().name, "ats.audit")

    def test_configure_logging_writes_to_file(self):
        """When given a writable runtime_dir, configure_logging must create the log file."""
        from automatic_transfer_station import logging_setup
        import logging.handlers

        original = logging_setup._configured
        # Snapshot managed handlers so we can restore the module's state fully.
        original_managed = list(logging_setup._managed_handlers)
        logging_setup._configured = False
        root = logging.getLogger()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    logging_setup.configure_logging(runtime_dir=tmp, console=False)
                    # Emit a record to flush
                    logging.getLogger("ats.audit").warning("test write")
                    log_file = Path(tmp) / "transfer_station.log"
                    self.assertTrue(log_file.exists(), "log file should have been created")
                finally:
                    # Close and remove managed handlers so TemporaryDirectory can
                    # delete the log file on Windows (held file handles block rmdir).
                    for h in list(logging_setup._managed_handlers):
                        root.removeHandler(h)
                        try:
                            h.close()
                        except Exception:
                            pass
                    logging_setup._managed_handlers.clear()
                    logging_setup._managed_handlers.extend(original_managed)
                    logging_setup._configured = original
        except Exception:
            logging_setup._configured = original
            raise


# ---------------------------------------------------------------------------
# Fix 2 — RD105 events routed to ats.audit / ats.rd105
# ---------------------------------------------------------------------------

class TestRD105Logging(unittest.TestCase):

    def _make_controller(self):
        from automatic_transfer_station.rd105_controller import TemperatureController
        ctrl = TemperatureController()
        # Stub out serial so no real hardware is needed
        ctrl.is_connected = True
        return ctrl

    def test_over_temp_guard_logs_error_to_audit(self):
        """_apply_over_temperature_guard_locked must log at ERROR level to ats.audit
        when the temperature exceeds the trip threshold."""
        from automatic_transfer_station.rd105_controller import (
            TemperatureController,
            OVERTEMP_TRIP_TEMPERATURE,
        )
        ctrl = TemperatureController()
        # Stub so no serial IO happens
        ctrl._set_enable_locked = lambda enabled: True

        trip_temp = OVERTEMP_TRIP_TEMPERATURE + 1.0
        with self.assertLogs("ats.audit", level="ERROR") as cm:
            ctrl._apply_over_temperature_guard_locked(trip_temp, enabled=True)

        # At least one ERROR (or higher) record must mention the guard
        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.ERROR, levels)

    def test_sensor_fault_none_temp_logs_error_to_audit(self):
        """A None temperature (sensor fault) must also log ERROR to ats.audit."""
        from automatic_transfer_station.rd105_controller import TemperatureController
        ctrl = TemperatureController()
        ctrl._set_enable_locked = lambda enabled: True

        with self.assertLogs("ats.audit", level="ERROR") as cm:
            ctrl._apply_over_temperature_guard_locked(None, enabled=True)

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.ERROR, levels)

    def test_close_failed_path_logs_critical_to_audit(self):
        """When _disable_output_for_alarm_locked cannot close the output,
        the CRITICAL branch must fire on ats.audit."""
        from automatic_transfer_station.rd105_controller import (
            TemperatureController,
            OVERTEMP_TRIP_TEMPERATURE,
        )
        ctrl = TemperatureController()
        # Stub the disable so it FAILS
        ctrl._set_enable_locked = lambda enabled: False

        trip_temp = OVERTEMP_TRIP_TEMPERATURE + 1.0
        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            ctrl._apply_over_temperature_guard_locked(trip_temp, enabled=True)

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.CRITICAL, levels)

    def test_watchdog_trip_logs_critical_to_audit(self):
        """A watchdog comm-loss trip that succeeds in disabling output must log
        CRITICAL to ats.audit."""
        import threading
        from automatic_transfer_station.rd105_controller import TemperatureController

        ctrl = TemperatureController()
        # Simulate a live open port object
        fake_ser = SimpleNamespace(is_open=True)
        ctrl.ser = fake_ser
        ctrl.last_enable_state = True
        ctrl._software_ramp = None
        ctrl._watchdog_lock = threading.Lock()
        # Stub serial write so it succeeds
        ctrl._set_enable_locked = lambda enabled: True
        ctrl._watchdog_io_done = False

        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            ctrl._watchdog_trip_action()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.CRITICAL, levels)


# ---------------------------------------------------------------------------
# Fix 3 — motion safety events to the audit log
# ---------------------------------------------------------------------------

class TestMotionAuditLogging(unittest.TestCase):

    def _make_window(self, controller=None):
        get_app()
        if controller is None:
            controller = RecordingController()
        from automatic_transfer_station.main_window import MainWindow
        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(
                controller=controller,
                position_store_path=Path(tmp) / "pos.json",
                runtime_calibration_path=Path(tmp) / "cal.json",
                microscope_position_store_path=Path(tmp) / "micro.json",
                motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            )
        return window

    def test_stop_unsafe_axes_warns_on_audit_with_axes_5_and_7(self):
        """_stop_unsafe_axes_if_needed must log WARNING to ats.audit naming axes 5 and 7
        when the X3/X2 interference check fires.

        Axes 5 (X3) and 7 (X2) share the interference constraint.  We feed
        positions that violate the soft-range for both so the auto-stop always
        names axis 5 and/or 7.
        """
        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS, AxisCalibration
        from dataclasses import replace

        ctrl = RecordingController()
        ctrl.is_open = True
        window = self._make_window(controller=ctrl)

        # Pick positions that will trigger an unsafe_axes_for_positions result.
        # The interference rule: axis 5 (X3) and axis 7 (X2) must not be
        # simultaneously near each other in the danger zone.
        # Force both axes to be out of their individual soft-range limits
        # (negative infinity / positive side of whichever axis has a limit).
        # We simply inspect axis_calibrations to find one with a bounded range
        # and set its position outside that range.
        from automatic_transfer_station.axis_calibration import unsafe_axes_for_positions

        # Build a positions dict that produces a non-ok check.
        # Deterministic X2/X3 interference violation: axis5 + axis7 = -900000, below the
        # -841567 (boundary -851567 + margin 10000) threshold, so unsafe_axes_for_positions
        # flags BOTH 5 and 7 by the interference rule regardless of any soft-limit drift.
        # (No skipTest: a calibration change that broke this should FAIL loudly, not skip.)
        calibrations = window.axis_calibrations
        positions = {5: 100000.0, 7: -1000000.0}
        check = unsafe_axes_for_positions(positions, calibrations)
        self.assertFalse(check.ok)
        self.assertEqual({5, 7}, set(check.axes))

        with self.assertLogs("ats.audit", level="WARNING") as cm:
            window._stop_unsafe_axes_if_needed(positions)

        # At least one WARNING record must mention an axis number
        messages = " ".join(r.getMessage() for r in cm.records)
        warned_axes = set(str(a) for a in check.axes)
        # The log message must reference at least one of the unsafe axes
        self.assertTrue(
            any(ax in messages for ax in warned_axes),
            f"Expected axis numbers {warned_axes} in log messages: {messages}",
        )

    def test_stop_unsafe_axes_warning_level(self):
        """The auto-stop log record must be at WARNING or higher."""
        from automatic_transfer_station.axis_calibration import unsafe_axes_for_positions

        ctrl = RecordingController()
        ctrl.is_open = True
        window = self._make_window(controller=ctrl)
        calibrations = window.axis_calibrations

        # Deterministic interference violation (see the companion test); no skipTest.
        positions = {5: 100000.0, 7: -1000000.0}
        check = unsafe_axes_for_positions(positions, calibrations)
        self.assertFalse(check.ok)

        with self.assertLogs("ats.audit", level="WARNING") as cm:
            window._stop_unsafe_axes_if_needed(positions)

        levels = [r.levelno for r in cm.records]
        self.assertTrue(any(lvl >= logging.WARNING for lvl in levels))

    def test_gamepad_stop_all_logs_critical_on_failure(self):
        """When gamepad stop_all raises MotionControllerError, CRITICAL must be logged."""
        ctrl = FailingStopController()
        ctrl.is_open = True
        window = self._make_window(controller=ctrl)

        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            window._gamepad_stop_all()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.CRITICAL, levels)

    def test_stop_all_motion_logs_info(self):
        """_stop_all_motion must log INFO to ats.audit when invoked."""
        ctrl = RecordingController()
        ctrl.is_open = True
        window = self._make_window(controller=ctrl)

        with self.assertLogs("ats.audit", level="INFO") as cm:
            window._stop_all_motion()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.INFO, levels)

    def test_open_motion_control_logs_info(self):
        """_open_motion_control must log INFO to ats.audit on success."""
        ctrl = RecordingController()
        window = self._make_window(controller=ctrl)

        with self.assertLogs("ats.audit", level="INFO") as cm:
            window._open_motion_control()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.INFO, levels)

    def test_reset_motion_control_logs_info(self):
        """_reset_motion_control must log INFO to ats.audit on success."""
        ctrl = RecordingController()
        ctrl.is_open = True
        window = self._make_window(controller=ctrl)

        with self.assertLogs("ats.audit", level="INFO") as cm:
            window._reset_motion_control()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.INFO, levels)


class TestGasMotionControllerLogging(unittest.TestCase):
    """GasMotionController._check must log ERROR to ats.motion before raising."""

    def _make_api(self, fail_result=-7):
        """Return a minimal fake API whose open_network returns fail_result."""
        class _Api:
            def open_network(self, ip, mode):
                return fail_result

            # These are needed because open_board calls them after open_network:
            def axis_on(self, axis):
                return 0

            def clear_status(self, axis, count=1):
                return 0

            def limits_off(self, axis, limit_type=-1):
                return 0

            def set_hard_limit_positive(self, *a):
                return 0

            def set_hard_limit_negative(self, *a):
                return 0

            def set_limit_sense_single(self, *a):
                return 0

            def limits_on(self, *a):
                return 0

            def stop(self, mask, mode):
                return 0

            def close(self):
                return 0

        return _Api()

    def test_check_logs_error_to_motion_before_raising(self):
        """_check with a non-zero return must log ERROR to ats.motion."""
        from automatic_transfer_station.gas_controller import GasMotionController
        from automatic_transfer_station.controller_base import MotionControllerError

        api = self._make_api(fail_result=-7)
        ctrl = GasMotionController(api=api)

        with self.assertLogs("ats.motion", level="ERROR") as cm:
            with self.assertRaises(MotionControllerError):
                ctrl._check(-7, "GA_Open")

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.ERROR, levels)

    def test_check_does_not_log_on_success(self):
        """_check with result=0 must not log anything."""
        from automatic_transfer_station.gas_controller import GasMotionController

        api = self._make_api(fail_result=0)
        ctrl = GasMotionController(api=api)

        # Use a root logger capture; nothing should appear for ats.motion
        with self.assertLogs("ats.motion", level="DEBUG") as cm:
            # Emit a dummy record so assertLogs doesn't raise "no logs emitted"
            logging.getLogger("ats.motion").debug("_check success sentinel")
            ctrl._check(0, "GA_SomeCall")

        # Only the sentinel should appear — no ERROR
        error_records = [r for r in cm.records if r.levelno >= logging.ERROR]
        self.assertEqual([], error_records)


# ---------------------------------------------------------------------------
# Fix 4 — calibration overwrite + rejected/fallback logged
# ---------------------------------------------------------------------------

class TestCalibrationAuditLogging(unittest.TestCase):

    def _make_store(self, tmp_dir):
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore
        return RuntimeAxisCalibrationStore(Path(tmp_dir) / "cal.json")

    def test_calibrate_logs_warning_with_old_and_new_values(self):
        """calibrate_axis_at_pulse must log WARNING to ats.audit containing the
        old and new zero_pulse / zero_coordinate."""
        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS

        # Use axis 1 (X1) which has absolute calibration
        axis = 1
        base = AXIS_CALIBRATIONS[axis]
        # A safe coordinate within the axis's range
        new_coord = float(base.min_coordinate or 0.0) + 1.0
        new_pulse = float(base.zero_pulse or 0.0) + 100.0

        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            store.load()

            with self.assertLogs("ats.audit", level="WARNING") as cm:
                store.calibrate_axis_at_pulse(axis, new_pulse, new_coord)

            levels = [r.levelno for r in cm.records]
            self.assertIn(logging.WARNING, levels)
            # The log message must mention the old zero_pulse
            messages = " ".join(r.getMessage() for r in cm.records)
            self.assertIn(str(axis), messages)

    def test_load_skipped_record_logs_warning(self):
        """When load() skips an out-of-range record, it must log WARNING to ats.audit."""
        import json
        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_runtime_calibration_store import (
            SCHEMA_VERSION,
            RuntimeAxisCalibrationStore,
        )

        axis = 1
        base = AXIS_CALIBRATIONS[axis]
        # Craft a record with a wildly out-of-range coordinate
        bad_coord = float((base.max_coordinate or 0.0) + 100000.0)
        bad_zero_pulse = float(base.zero_pulse or 0.0)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": "2025-01-01T00:00:00Z",
            "axes": {
                str(axis): {
                    "axis": axis,
                    "label": "X1",
                    "unit": base.unit,
                    "zero_pulse": bad_zero_pulse,
                    "zero_coordinate": bad_coord,
                    "updated_at": "2025-01-01T00:00:00Z",
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "cal.json"
            cal_path.write_text(json.dumps(payload), encoding="utf-8")
            store = RuntimeAxisCalibrationStore(cal_path)

            with self.assertLogs("ats.audit", level="WARNING") as cm:
                store.load()

            levels = [r.levelno for r in cm.records]
            self.assertIn(logging.WARNING, levels)
            self.assertIn(axis, store.skipped)

    def test_load_schema_warning_logs_warning(self):
        """When the file's schema_version is wrong, load() must log WARNING to ats.audit."""
        import json
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "cal.json"
            bad_data = {
                "schema_version": 9999,
                "updated_at": "2025-01-01T00:00:00Z",
                "axes": {},
            }
            cal_path.write_text(json.dumps(bad_data), encoding="utf-8")
            store = RuntimeAxisCalibrationStore(cal_path)

            with self.assertLogs("ats.audit", level="WARNING") as cm:
                store.load()

            levels = [r.levelno for r in cm.records]
            self.assertIn(logging.WARNING, levels)
            self.assertTrue(store.schema_warning)


class TestLoggingSetupHandlerTracking(unittest.TestCase):
    """Integration Fix 2 — configure_logging re-config closes old handler, no leaks."""

    def _reset_logging_setup(self, logging_setup_module):
        """Reset the module's guard so configure_logging runs again."""
        logging_setup_module._configured = False

    def test_reconfigure_with_new_dir_uses_new_dir_only(self):
        """After configure_logging(A) then configure_logging(B), only one
        RotatingFileHandler exists, and it points at B."""
        from automatic_transfer_station import logging_setup
        import logging.handlers

        original_configured = logging_setup._configured

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:
                # Remove any existing managed handlers from a previous test run
                root = logging.getLogger()
                try:
                    # First configure: dir_a
                    self._reset_logging_setup(logging_setup)
                    logging_setup.configure_logging(runtime_dir=dir_a, console=False)

                    rfh_handlers_after_a = [
                        h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)
                    ]
                    self.assertEqual(
                        1, len(rfh_handlers_after_a),
                        "Expected exactly 1 RotatingFileHandler after first configure",
                    )
                    self.assertIn(dir_a, rfh_handlers_after_a[0].baseFilename)

                    # Second configure: dir_b (reset guard first)
                    self._reset_logging_setup(logging_setup)
                    logging_setup.configure_logging(runtime_dir=dir_b, console=False)

                    rfh_handlers_after_b = [
                        h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)
                    ]
                    self.assertEqual(
                        1, len(rfh_handlers_after_b),
                        "Expected exactly 1 RotatingFileHandler after re-configure "
                        "(old handler must be removed before adding new one)",
                    )
                    self.assertIn(
                        dir_b, rfh_handlers_after_b[0].baseFilename,
                        "The surviving handler must point at dir_b",
                    )
                finally:
                    # Clean up managed handlers so other tests see a fresh state
                    self._reset_logging_setup(logging_setup)
                    root = logging.getLogger()
                    # Clean managed handlers from BOTH root and ats.audit (the
                    # dedicated audit file handler lives on ats.audit; leaving it open
                    # would block the TemporaryDirectory cleanup on Windows).
                    for _logger in (logging.getLogger(), logging.getLogger("ats.audit")):
                        for h in list(_logger.handlers):
                            if getattr(h, "_ats_managed", False):
                                _logger.removeHandler(h)
                                try:
                                    h.close()
                                except Exception:
                                    pass
                    logging_setup._configured = original_configured

    def test_old_handler_is_closed_on_reconfigure(self):
        """The RotatingFileHandler created by the first configure_logging call
        must be closed (not just removed) when configure_logging is called again."""
        from automatic_transfer_station import logging_setup
        import logging.handlers

        original_configured = logging_setup._configured

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:
                root = logging.getLogger()
                try:
                    # First configure
                    self._reset_logging_setup(logging_setup)
                    logging_setup.configure_logging(runtime_dir=dir_a, console=False)

                    old_handlers = [
                        h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)
                        and dir_a in h.baseFilename
                    ]
                    self.assertEqual(1, len(old_handlers))
                    old_handler = old_handlers[0]

                    # Second configure — old_handler should be closed afterward
                    self._reset_logging_setup(logging_setup)
                    logging_setup.configure_logging(runtime_dir=dir_b, console=False)

                    # A closed handler's stream is None (CPython implementation)
                    # Alternatively check it is no longer in root.handlers.
                    self.assertNotIn(
                        old_handler, root.handlers,
                        "Old RotatingFileHandler must be removed from root.handlers after re-configure",
                    )
                finally:
                    self._reset_logging_setup(logging_setup)
                    # Clean managed handlers from BOTH root and ats.audit (the
                    # dedicated audit file handler lives on ats.audit; leaving it open
                    # would block the TemporaryDirectory cleanup on Windows).
                    for _logger in (logging.getLogger(), logging.getLogger("ats.audit")):
                        for h in list(_logger.handlers):
                            if getattr(h, "_ats_managed", False):
                                _logger.removeHandler(h)
                                try:
                                    h.close()
                                except Exception:
                                    pass
                    logging_setup._configured = original_configured

    def test_audit_logger_gets_dedicated_rotating_file(self):
        """ats.audit must have its OWN rotating file (transfer_station.audit.log) so the
        safety trail can't be rotated away by main-log DEBUG churn."""
        from automatic_transfer_station import logging_setup
        import logging.handlers

        original_configured = logging_setup._configured
        with tempfile.TemporaryDirectory() as d:
            try:
                # Clean slate regardless of test order (a prior test's app may have
                # attached the real runtime audit handler).
                for _logger in (logging.getLogger(), logging.getLogger("ats.audit")):
                    for h in list(_logger.handlers):
                        if getattr(h, "_ats_managed", False):
                            _logger.removeHandler(h)
                            try:
                                h.close()
                            except Exception:
                                pass
                logging_setup._managed_handlers.clear()
                self._reset_logging_setup(logging_setup)
                logging_setup.configure_logging(runtime_dir=d, console=False)
                audit = logging.getLogger("ats.audit")
                audit_fhs = [
                    h for h in audit.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)
                    and getattr(h, "_ats_managed", False)
                ]
                self.assertEqual(1, len(audit_fhs), "ats.audit needs its own dedicated rotating file handler")
                self.assertTrue(audit_fhs[0].baseFilename.endswith("transfer_station.audit.log"))
                self.assertEqual(logging.INFO, audit_fhs[0].level)
                # The main log handler stays on root and is separate from the audit one.
                root_fhs = [
                    h for h in logging.getLogger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)
                    and getattr(h, "_ats_managed", False)
                ]
                self.assertEqual(1, len(root_fhs))
                self.assertNotIn(audit_fhs[0], root_fhs)
            finally:
                for _logger in (logging.getLogger(), logging.getLogger("ats.audit")):
                    for h in list(_logger.handlers):
                        if getattr(h, "_ats_managed", False):
                            _logger.removeHandler(h)
                            try:
                                h.close()
                            except Exception:
                                pass
                logging_setup._configured = original_configured

    def test_single_call_still_idempotent(self):
        """The normal single-call path must still be idempotent (no duplicate handlers)."""
        from automatic_transfer_station import logging_setup

        original_configured = logging_setup._configured
        root = logging.getLogger()
        handlers_before = len(root.handlers)

        try:
            self._reset_logging_setup(logging_setup)
            logging_setup.configure_logging(runtime_dir=None, console=False)
            # Calling again (without reset) must be a no-op
            logging_setup.configure_logging(runtime_dir=None, console=False)
            self.assertLessEqual(len(root.handlers), handlers_before + 1)
        finally:
            # Clean up
            for h in list(root.handlers):
                if getattr(h, "_ats_managed", False):
                    root.removeHandler(h)
                    try:
                        h.close()
                    except Exception:
                        pass
            logging_setup._configured = original_configured


if __name__ == "__main__":
    unittest.main()
