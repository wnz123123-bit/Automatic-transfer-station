import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class RD105ControllerTests(unittest.TestCase):
    def test_pid_profile_boundaries_match_confirmed_rd105_config(self):
        from automatic_transfer_station.rd105_config import load_config
        from automatic_transfer_station.rd105_controller import select_pid_profile

        profiles = load_config()["pid_profiles"]

        self.assertEqual("pid_30c", select_pid_profile(39.9, profiles)["name"])
        self.assertEqual("pid_50c", select_pid_profile(40.0, profiles)["name"])
        self.assertEqual("pid_50c", select_pid_profile(59.9, profiles)["name"])
        self.assertEqual("pid_70c", select_pid_profile(60.0, profiles)["name"])
        self.assertEqual("pid_70c", select_pid_profile(74.9, profiles)["name"])
        self.assertEqual("pid_80c", select_pid_profile(75.0, profiles)["name"])
        self.assertEqual("pid_80c", select_pid_profile(84.9, profiles)["name"])
        self.assertEqual("pid_90c", select_pid_profile(85.0, profiles)["name"])
        self.assertEqual("pid_90c", select_pid_profile(94.9, profiles)["name"])
        self.assertEqual("pid_100c", select_pid_profile(95.0, profiles)["name"])
        self.assertEqual("pid_100c", select_pid_profile(109.9, profiles)["name"])
        self.assertEqual("pid_120c", select_pid_profile(110.0, profiles)["name"])
        self.assertEqual("pid_120c", select_pid_profile(129.9, profiles)["name"])
        self.assertEqual("pid_140c", select_pid_profile(130.0, profiles)["name"])
        self.assertEqual("pid_140c", select_pid_profile(149.9, profiles)["name"])
        self.assertEqual("pid_160c", select_pid_profile(150.0, profiles)["name"])
        self.assertEqual("pid_160c", select_pid_profile(169.9, profiles)["name"])
        self.assertEqual("pid_180c", select_pid_profile(170.0, profiles)["name"])
        self.assertEqual("pid_180c", select_pid_profile(189.9, profiles)["name"])
        self.assertEqual("pid_200c", select_pid_profile(190.0, profiles)["name"])
        self.assertEqual("pid_200c", select_pid_profile(205.0, profiles)["name"])
        self.assertIsNone(select_pid_profile(205.1, profiles))

        expected_output_duty = {
            "pid_30c": 40,
            "pid_50c": 40,
            "pid_70c": 40,
            "pid_80c": 40,
            "pid_90c": 40,
            "pid_100c": 40,
            "pid_120c": 50,
            "pid_140c": 50,
            "pid_160c": 50,
            "pid_180c": 50,
            "pid_200c": 50,
        }
        self.assertEqual(expected_output_duty, {profile["name"]: profile["max_output_duty"] for profile in profiles})

    def test_rd105_rejects_target_temperature_above_confirmed_safety_limit(self):
        from automatic_transfer_station.rd105_config import MAX_TARGET_TEMPERATURE
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()

        self.assertEqual(205.0, MAX_TARGET_TEMPERATURE)
        self.assertFalse(controller.fast_set_temperature(205.1))
        self.assertIn("205.0", controller.last_error)
        self.assertFalse(controller.ramp_to_temperature(205.1, 18.0))
        self.assertIn("205.0", controller.last_error)

    def test_rd105_temperature_and_speed_value_conversions(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        self.assertEqual(4500000, TemperatureController._encode_temperature_value(45.0))
        self.assertEqual(45.0, TemperatureController._decode_temperature_value(4500000))
        self.assertEqual(5, TemperatureController._speed_from_c_per_hour(18.0))
        self.assertEqual(1, TemperatureController._speed_from_c_per_hour(0.1))

    def test_rd105_rate_mode_uses_python_software_setpoint_steps(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        now = [100.0]
        calls = []
        latest_target = {"value": None}

        controller._clock = lambda: now[0]
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: calls.append(("PID", target)) or True
        controller._set_speed_locked = lambda value: calls.append(("SPEED", value)) or True
        controller._set_enable_locked = lambda enabled: calls.append(("ENABLE", enabled)) or True

        def set_target(value):
            rounded = round(float(value), 4)
            latest_target["value"] = rounded
            calls.append(("TG", rounded))
            return True

        controller._set_target_temperature_locked = set_target
        controller._read_target_temperature_locked = lambda: latest_target["value"]
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0
        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0@"

        self.assertTrue(controller.ramp_to_temperature(41.0, 36.0))
        self.assertEqual(
            [("PID", 41.0), ("SPEED", 0), ("TG", 40.0), ("ENABLE", True)],
            calls,
        )

        # Advance by exactly the per-step time cap (BP1 #4). At 36.0 C/h that is
        # 36.0 * 5 / 3600 = 0.05 C from the previous setpoint. (A larger gap is
        # capped to this same step; see the dedicated long-gap tests below.)
        now[0] += 5.0
        snapshot = controller.read_live_data()

        self.assertIn(("TG", 40.05), calls)
        self.assertEqual(40.05, snapshot["effective_setpoint"])
        self.assertTrue(snapshot["software_ramp_active"])
        self.assertEqual(41.0, snapshot["software_ramp_final_target"])

    def test_rd105_rate_mode_sensor_fault_cancels_ramp_and_disables_output(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        now = [100.0]
        calls = []
        latest_target = {"value": None}

        controller._clock = lambda: now[0]
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: True
        controller._set_speed_locked = lambda value: True
        controller._set_enable_locked = lambda enabled: True
        controller._set_target_temperature_locked = lambda value: latest_target.update(value=float(value)) or True
        controller._read_target_temperature_locked = lambda: latest_target["value"]
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0

        self.assertTrue(controller.ramp_to_temperature(41.0, 36.0))

        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=999999999@TC1:PWM=0@"
        controller._set_enable_locked = lambda enabled: calls.append(("ENABLE", enabled)) or True

        snapshot = controller.read_live_data()

        self.assertEqual([("ENABLE", False)], calls)
        self.assertFalse(snapshot["software_ramp_active"])
        self.assertIn("温度读数", snapshot["safety_alarm"])
        self.assertIn("温度读数", controller.last_error)

    def test_rd105_rate_mode_rejects_start_when_current_temperature_exceeds_limit(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        calls = []
        # 209.0 C is above the over-temp TRIP threshold (205.0 + 3.0 margin).
        controller._read_current_temperature_locked = lambda: 209.0
        controller._read_enable_state_locked = lambda: True
        controller._apply_pid_for_target_locked = lambda target: calls.append(("PID", target)) or True
        controller._set_speed_locked = lambda value: calls.append(("SPEED", value)) or True
        controller._set_target_temperature_locked = lambda value: calls.append(("TG", value)) or True
        controller._set_enable_locked = lambda enabled: calls.append(("ENABLE", enabled)) or True

        self.assertFalse(controller.ramp_to_temperature(180.0, 36.0))

        self.assertEqual([("ENABLE", False)], calls)
        self.assertTrue(controller.over_temperature_alarm)
        self.assertIn("超温保护", controller.last_error)

    def test_rd105_failed_new_rate_command_cancels_existing_software_ramp(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: True
        controller._set_speed_locked = lambda value: True
        controller._set_target_temperature_locked = lambda value: True
        controller._set_enable_locked = lambda enabled: True

        self.assertTrue(controller.ramp_to_temperature(41.0, 36.0))
        self.assertIsNotNone(controller._software_ramp)

        controller._read_current_temperature_locked = lambda: None

        self.assertFalse(controller.ramp_to_temperature(42.0, 36.0))
        self.assertIsNone(controller._software_ramp)

    def test_rd105_ramp_advance_write_failure_cancels_software_ramp(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        now = [100.0]
        latest_target = {"value": None}
        writes_enabled = {"value": True}

        def set_target(value):
            if not writes_enabled["value"]:
                controller._set_last_error("TG 写入失败")
                return False
            latest_target["value"] = float(value)
            return True

        controller._clock = lambda: now[0]
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: True
        controller._set_speed_locked = lambda value: True
        controller._set_enable_locked = lambda enabled: True
        controller._set_target_temperature_locked = set_target
        controller._read_target_temperature_locked = lambda: latest_target["value"]
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0
        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0@"

        self.assertTrue(controller.ramp_to_temperature(41.0, 36.0))
        writes_enabled["value"] = False
        now[0] += 10.0

        snapshot = controller.read_live_data()

        self.assertFalse(snapshot["software_ramp_active"])
        self.assertEqual("TG 写入失败", snapshot["control_error"])
        self.assertIn("TG 写入失败", controller.last_error)

    def test_rd105_max_output_duty_internal_write_uses_limited_with_readback_verification(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        calls = []
        controller._write_verified_value_locked = lambda name, value: calls.append((name, value)) or True

        self.assertFalse(hasattr(controller, "set_max_output_duty"))
        self.assertTrue(controller._write_max_output_duty_locked(40))
        self.assertEqual([("LIMITED", 40)], calls)

    def test_rd105_rejects_internal_max_output_duty_outside_manual_range(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()

        self.assertFalse(controller._write_max_output_duty_locked(91))
        self.assertIn("0~90", controller.last_error)

    def test_rd105_over_temperature_reading_disables_output_and_locks_heating(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        disable_calls = []
        # 209.0 C is above the over-temp TRIP threshold (max 205.0 + margin 3.0 =
        # 208.0), so the guard must trip.
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=20900000@TC1:PWM=120000@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True

        snapshot = controller.read_live_data()

        self.assertEqual([False], disable_calls)
        self.assertTrue(controller.over_temperature_alarm)
        self.assertIn("208.0", snapshot["safety_alarm"])
        self.assertEqual("超温保护", snapshot["run_state"])

        self.assertFalse(controller.fast_set_temperature(180.0))
        self.assertIn("超温", controller.last_error)

    def test_rd105_over_temperature_reports_when_output_disable_fails(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=20900000@TC1:PWM=120000@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: False

        snapshot = controller.read_live_data()

        self.assertIn("关闭输出失败", snapshot["safety_alarm"])
        self.assertIn("关闭输出失败", controller.last_error)
        self.assertNotIn("已关闭输出", snapshot["safety_alarm"])

    def test_rd105_over_temperature_disable_failure_run_state_reports_not_closed(self):
        # Bug #9: when the over-temp disable command FAILS, the snapshot must not
        # read as "system is now safe". run_state must convey the output is NOT
        # closed, and `enabled` must not be falsely flipped to False.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=20900000@TC1:PWM=120000@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: False  # disable always fails

        snapshot = controller.read_live_data()

        self.assertIn("关闭输出失败", snapshot["run_state"])
        self.assertNotEqual("超温保护", snapshot["run_state"])
        self.assertIn("关闭输出失败", snapshot["safety_alarm"])
        self.assertIn("关闭输出失败", controller.last_error)
        # The output is still on; last_enable_state must not be falsely False.
        self.assertNotEqual(False, controller.last_enable_state)

    def test_rd105_over_temperature_disable_success_run_state_is_plain_alarm(self):
        # Counterpart: when the disable succeeds the original plain run_state stays.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=20900000@TC1:PWM=120000@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: True  # disable succeeds

        snapshot = controller.read_live_data()

        self.assertEqual("超温保护", snapshot["run_state"])

    def test_disconnect_disables_output_before_closing_when_enabled(self):
        # Bug #3: disconnect() must turn the heater off (ENABLE=0) before closing
        # the serial port, so exiting the app does not leave the heater running.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        calls = []

        class FakeSerial:
            def __init__(self):
                self.is_open = True

            def close(self):
                calls.append(("close",))
                self.is_open = False

        controller.ser = FakeSerial()
        controller.is_connected = True
        controller.last_enable_state = True
        controller._set_enable_locked = lambda enabled: calls.append(("ENABLE", enabled)) or True

        controller.disconnect()

        # The disable command must be sent, and it must happen before the close.
        self.assertEqual(("ENABLE", False), calls[0])
        self.assertIn(("close",), calls)
        self.assertTrue(calls.index(("ENABLE", False)) < calls.index(("close",)))
        self.assertFalse(controller.is_connected)

    def test_disconnect_surfaces_disable_failure_and_still_closes_port(self):
        # Bug #3: a disable FAILURE during disconnect must be surfaced (recorded in
        # last_error), must not crash, and must not falsely claim the output closed.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        calls = []

        class FakeSerial:
            def __init__(self):
                self.is_open = True

            def close(self):
                calls.append(("close",))
                self.is_open = False

        controller.ser = FakeSerial()
        controller.is_connected = True
        controller.last_enable_state = True
        controller._set_enable_locked = lambda enabled: calls.append(("ENABLE", enabled)) or False

        controller.disconnect()

        self.assertIn(("ENABLE", False), calls)
        self.assertIn(("close",), calls)  # port still closed despite failure
        self.assertFalse(controller.is_connected)
        self.assertIn("关闭输出失败", controller.last_error)

    def test_disconnect_is_idempotent_and_safe_when_not_connected(self):
        # Bug #3: disconnect() must stay safe/idempotent when never connected.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        calls = []
        controller._set_enable_locked = lambda enabled: calls.append(enabled) or True

        controller.disconnect()  # no serial, no connection
        controller.disconnect()  # again

        self.assertEqual([], calls)  # never tried to disable when not connected
        self.assertFalse(controller.is_connected)

    def test_normalize_pid_profile_raises_clear_error_for_missing_required_key(self):
        # PID validation (#): a profile missing kp/ki/kd/min_temp/max_temp must
        # fail fast at load time with a clear error naming the key and profile,
        # not an opaque KeyError deep in select_pid_profile / heating.
        from automatic_transfer_station.rd105_config import _normalize_pid_profile

        malformed = {
            "name": "pid_broken",
            "min_temp": 40.0,
            "max_temp": 60.0,
            "kp": 4791,
            "ki": 133,
            # kd missing
            "max_output_duty": 40,
        }
        with self.assertRaises(ValueError) as ctx:
            _normalize_pid_profile(malformed)
        message = str(ctx.exception)
        self.assertIn("kd", message)
        self.assertIn("pid_broken", message)

    def test_load_config_rejects_pid_profile_missing_required_key(self):
        import json
        import tempfile
        from pathlib import Path

        from automatic_transfer_station.rd105_config import load_config

        bad_config = {
            "last_port": "COM9",
            "pid_profiles": [
                {
                    "name": "pid_no_min_temp",
                    "max_temp": 60.0,
                    "kp": 4791,
                    "ki": 133,
                    "kd": 905,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rd105_config.json"
            path.write_text(json.dumps(bad_config), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
        message = str(ctx.exception)
        self.assertIn("min_temp", message)
        self.assertIn("pid_no_min_temp", message)

    def test_normalize_pid_profile_backfills_max_output_duty_when_valid(self):
        from automatic_transfer_station.rd105_config import (
            DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
            _normalize_pid_profile,
        )

        profile = {
            "name": "pid_ok",
            "min_temp": 40.0,
            "max_temp": 60.0,
            "kp": 4791,
            "ki": 133,
            "kd": 905,
        }
        normalized = _normalize_pid_profile(profile)
        self.assertEqual(DEFAULT_MAX_OUTPUT_DUTY_PERCENT, normalized["max_output_duty"])

    def test_rd105_applies_profile_max_output_duty_before_pid(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController(
            pid_profiles=[
                {
                    "name": "pid_80c",
                    "min_temp": 75.0,
                    "max_temp": 85.0,
                    "kp": 2674,
                    "ki": 133,
                    "kd": 402,
                    "max_output_duty": 40,
                }
            ]
        )
        calls = []
        controller._write_max_output_duty_locked = lambda value: calls.append(("LIMITED", value)) or True
        controller._write_pid_parameters_locked = lambda kp, ki, kd: calls.append(("PID", kp, ki, kd)) or True

        self.assertTrue(controller._apply_pid_for_target_locked(80.0))
        self.assertEqual([("LIMITED", 40), ("PID", 2674, 133, 402)], calls)

    # --- BP1 Fix 1: malformed temperature frame must not skip the over-temp guard ---
    def test_decode_temperature_value_returns_none_for_non_integer_payload(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        self.assertIsNone(TemperatureController._decode_temperature_value("garbage"))
        self.assertIsNone(TemperatureController._decode_temperature_value("4500000.5"))
        self.assertIsNone(TemperatureController._decode_temperature_value("=?"))
        # Genuine integer payloads still decode normally.
        self.assertEqual(45.0, TemperatureController._decode_temperature_value("4500000"))

    def test_rd105_malformed_temperature_payload_triggers_safe_disable_path(self):
        # Bug (BP1 #1): a corrupt temperature payload must NOT raise past the guard
        # leaving the heater enabled. It must decode to None and fall into the
        # "temperature is None -> disable/safe" branch.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        disable_calls = []
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=NOTANUMBER@TC1:PWM=0@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True

        snapshot = controller.read_live_data()

        self.assertIsNotNone(snapshot)  # not an exception/None whole-read failure
        self.assertIsNone(snapshot["temperature"])
        self.assertEqual([False], disable_calls)  # output disabled
        self.assertTrue(controller.over_temperature_alarm)
        self.assertIn("温度读数", snapshot["safety_alarm"])

    # --- BP1 Fix 2: DATADEMAND frame must be terminated before parsing ---
    def test_rd105_truncated_datademand_frame_does_not_clear_guard_with_low_temp(self):
        # Bug (BP1 #2): a truncated DATADEMAND frame (no trailing '@') must not
        # decode to a too-low temperature that clears a real over-temp alarm. It
        # must be treated as a framing error -> no valid reading (None) -> safe.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        disable_calls = []
        # Truncated: PWM value is missing its trailing '@' terminator.
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True

        snapshot = controller.read_live_data()

        self.assertIsNotNone(snapshot)
        # The truncated frame must NOT yield a (low) temperature reading.
        self.assertIsNone(snapshot["temperature"])
        self.assertEqual([False], disable_calls)
        self.assertTrue(controller.over_temperature_alarm)

    def test_parse_data_demand_requires_trailing_terminator(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        # Properly terminated frame parses.
        data = controller._parse_data_demand("TC1:TCADJTEMP=4000000@TC1:PWM=0@")
        self.assertEqual("4000000", data["TC1:TCADJTEMP"])
        # Non-terminated frame is a framing error -> no data.
        self.assertEqual({}, controller._parse_data_demand("TC1:TCADJTEMP=4000000@TC1:PWM=0"))

    # --- BP1 Fix 3: integer field parsing returns None on non-numeric ---
    def test_rd105_non_numeric_enable_field_degrades_to_none_without_crashing(self):
        # Bug (BP1 #3): a prefix-valid but non-numeric field must yield None for
        # that field rather than raising through the whole read.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()

        def fake_send(command):
            if command.startswith("DATADEMAND"):
                return "TC1:TCADJTEMP=4000000@TC1:PWM=0@"
            if "ENABLE" in command:
                return "OKTC1:ENABLE=?@"  # prefix-valid, non-numeric payload
            if "TG" in command:
                return "OKTC1:TG=4000000@"
            if "LIMITED" in command:
                return "OKTC1:LIMITED=40@"
            if "ERRORCODE" in command:
                return "OKERRORCODE=0@"
            return None

        controller._send_ascii_command_locked = fake_send

        snapshot = controller.read_live_data()

        self.assertIsNotNone(snapshot)  # one bad field did not crash the snapshot
        self.assertEqual(40.0, snapshot["temperature"])

    def test_read_enable_state_returns_none_on_non_numeric_payload(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller._send_ascii_command_locked = lambda command: "OKTC1:ENABLE=ERR@"

        self.assertIsNone(controller._read_enable_state_locked())

    # --- BP1 Fix 4: software ramp limits per-step jump after a long gap ---
    def test_rd105_ramp_step_caps_jump_after_long_poll_gap(self):
        # Bug (BP1 #4): after a long gap between ramp steps the next setpoint must
        # advance by at most rate * capped_dt from the PREVIOUS setpoint, not jump
        # a large absolute amount toward the target.
        from automatic_transfer_station.rd105_controller import (
            RAMP_STEP_MAX_INTERVAL_SECONDS,
            TemperatureController,
        )

        controller = TemperatureController()
        now = [100.0]
        latest_target = {"value": None}

        controller._clock = lambda: now[0]
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: True
        controller._set_speed_locked = lambda value: True
        controller._set_enable_locked = lambda enabled: True
        controller._set_target_temperature_locked = (
            lambda value: latest_target.update(value=round(float(value), 6)) or True
        )
        controller._read_target_temperature_locked = lambda: latest_target["value"]
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0
        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0@"

        # Target far away so the ramp cannot reach it in one step.
        rate = 360.0  # 0.1 C/s
        self.assertTrue(controller.ramp_to_temperature(80.0, rate))
        self.assertEqual(40.0, latest_target["value"])

        # Simulate a very long gap between ramp steps.
        long_gap = 600.0  # 10 minutes
        now[0] += long_gap
        snapshot = controller.read_live_data()

        # Without the cap the step would be rate*600/3600 = 60 C (jump to 80).
        # With the cap the step is at most rate*RAMP_STEP_MAX_INTERVAL/3600.
        max_step = rate * RAMP_STEP_MAX_INTERVAL_SECONDS / 3600.0
        advanced = snapshot["effective_setpoint"] - 40.0
        self.assertLessEqual(advanced, max_step + 1e-6)
        self.assertGreater(advanced, 0.0)
        self.assertTrue(snapshot["software_ramp_active"])

    def test_rd105_ramp_consecutive_steps_each_capped_relative_to_last_setpoint(self):
        from automatic_transfer_station.rd105_controller import (
            RAMP_STEP_MAX_INTERVAL_SECONDS,
            TemperatureController,
        )

        controller = TemperatureController()
        now = [0.0]
        latest_target = {"value": None}

        controller._clock = lambda: now[0]
        controller._read_current_temperature_locked = lambda: 40.0
        controller._apply_pid_for_target_locked = lambda target: True
        controller._set_speed_locked = lambda value: True
        controller._set_enable_locked = lambda enabled: True
        controller._set_target_temperature_locked = (
            lambda value: latest_target.update(value=round(float(value), 6)) or True
        )
        controller._read_target_temperature_locked = lambda: latest_target["value"]
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0
        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0@"

        rate = 360.0  # 0.1 C/s
        self.assertTrue(controller.ramp_to_temperature(80.0, rate))

        previous = 40.0
        max_step = rate * RAMP_STEP_MAX_INTERVAL_SECONDS / 3600.0
        for _ in range(3):
            now[0] += 1000.0  # long gap every time
            snapshot = controller.read_live_data()
            advanced = snapshot["effective_setpoint"] - previous
            self.assertLessEqual(advanced, max_step + 1e-6)
            self.assertGreater(advanced, 0.0)
            previous = snapshot["effective_setpoint"]

    # --- BP1 Fix 5: over-temp trip threshold separate from max settable setpoint ---
    def test_rd105_setpoint_at_max_does_not_trip_guard_at_reading_equal_to_max(self):
        # Bug (BP1 #5): a legitimate max-temperature target must not trip the guard
        # on a reading equal to the max ceiling (normal PID overshoot tolerance).
        from automatic_transfer_station.rd105_config import (
            MAX_TARGET_TEMPERATURE,
            OVERTEMP_MARGIN,
        )
        from automatic_transfer_station.rd105_controller import TemperatureController

        self.assertGreater(OVERTEMP_MARGIN, 0.0)

        controller = TemperatureController()
        disable_calls = []
        # Reading exactly at the max ceiling (205.0 C -> 20500000).
        controller._send_ascii_command_locked = (
            lambda command: "TC1:TCADJTEMP=20500000@TC1:PWM=0@"
        )
        controller._read_target_temperature_locked = lambda: MAX_TARGET_TEMPERATURE
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True

        snapshot = controller.read_live_data()

        self.assertFalse(controller.over_temperature_alarm)
        self.assertEqual([], disable_calls)  # guard did NOT trip
        self.assertIsNone(snapshot["safety_alarm"])

    def test_rd105_guard_still_trips_above_max_plus_margin(self):
        from automatic_transfer_station.rd105_config import (
            MAX_TARGET_TEMPERATURE,
            OVERTEMP_MARGIN,
        )
        from automatic_transfer_station.rd105_controller import (
            RD105_TEMPERATURE_SCALE,
            TemperatureController,
        )

        controller = TemperatureController()
        disable_calls = []
        # Reading above max+margin must still trip.
        over = MAX_TARGET_TEMPERATURE + OVERTEMP_MARGIN + 0.5
        raw = int(round(over * RD105_TEMPERATURE_SCALE))
        controller._send_ascii_command_locked = (
            lambda command: f"TC1:TCADJTEMP={raw}@TC1:PWM=120000@"
        )
        controller._read_target_temperature_locked = lambda: 200.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 50
        controller._read_error_code_locked = lambda force=False: 0
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True

        snapshot = controller.read_live_data()

        self.assertEqual([False], disable_calls)
        self.assertTrue(controller.over_temperature_alarm)
        self.assertEqual("超温保护", snapshot["run_state"])

    # --- BP1 Fix 6: dead write_verified_value raw bypass removed ---
    def test_rd105_write_verified_value_public_bypass_removed(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        self.assertFalse(hasattr(controller, "write_verified_value"))


class RD105WatchdogTests(unittest.TestCase):
    """BP2: controller-owned over-temp / comm-loss watchdog, independent of GUI."""

    def _heating_controller(self, now_value=100.0):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        self._now = [now_value]
        controller._clock = lambda: self._now[0]
        # Simulate "heating active, last read just succeeded".
        controller.last_enable_state = True
        controller._last_read_ok_time = self._now[0]
        return controller

    def test_watchdog_stale_seconds_config_is_documented_multiple_of_poll(self):
        from automatic_transfer_station.rd105_config import (
            WATCHDOG_CHECK_INTERVAL_SECONDS,
            WATCHDOG_STALE_SECONDS,
        )

        # A few-times-the-1s-poll value: tolerant of jitter, still protective.
        self.assertGreaterEqual(WATCHDOG_STALE_SECONDS, 5.0)
        self.assertLessEqual(WATCHDOG_STALE_SECONDS, 10.0)
        self.assertGreater(WATCHDOG_CHECK_INTERVAL_SECONDS, 0.0)

    def test_read_live_data_records_last_successful_read_time(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        now = [500.0]
        controller._clock = lambda: now[0]
        controller._last_read_ok_time = None
        controller._send_ascii_command_locked = lambda command: "TC1:TCADJTEMP=4000000@TC1:PWM=0@"
        controller._read_target_temperature_locked = lambda: 40.0
        controller._read_enable_state_locked = lambda: True
        controller._read_max_output_duty_locked = lambda: 40
        controller._read_error_code_locked = lambda force=False: 0

        snapshot = controller.read_live_data()

        self.assertIsNotNone(snapshot)
        self.assertEqual(500.0, controller._last_read_ok_time)

    def test_read_live_data_does_not_update_read_time_on_failed_read(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        now = [500.0]
        controller._clock = lambda: now[0]
        controller._last_read_ok_time = 100.0
        # A failed DATADEMAND (None) must NOT refresh the freshness timestamp.
        controller._send_ascii_command_locked = lambda command: None

        snapshot = controller.read_live_data()

        self.assertIsNone(snapshot)
        self.assertEqual(100.0, controller._last_read_ok_time)

    def test_watchdog_evaluate_no_trip_when_read_is_fresh(self):
        controller = self._heating_controller()
        self._now[0] += 1.0  # only 1s since last read

        self.assertFalse(controller._watchdog_evaluate(self._now[0]))
        self.assertFalse(controller._watchdog_tripped)

    def test_watchdog_evaluate_trips_when_heating_active_and_read_stale(self):
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        self.assertTrue(controller._watchdog_evaluate(self._now[0]))

    def test_watchdog_evaluate_no_trip_when_not_heating_even_if_stale(self):
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        controller.last_enable_state = False
        controller._software_ramp = None
        self._now[0] += WATCHDOG_STALE_SECONDS + 100.0

        self.assertFalse(controller._watchdog_evaluate(self._now[0]))

    def test_watchdog_evaluate_trips_when_ramp_active_and_stale(self):
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS
        from automatic_transfer_station.rd105_controller import SoftwareRampState

        controller = self._heating_controller()
        # Output not (yet) known on, but a software ramp is driving heating.
        controller.last_enable_state = None
        controller._software_ramp = SoftwareRampState(
            start_temperature=40.0,
            final_target=60.0,
            rate_per_hour=36.0,
            start_time=0.0,
            direction=1,
            last_setpoint=40.0,
            last_step_time=0.0,
        )
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        self.assertTrue(controller._watchdog_evaluate(self._now[0]))

    def test_watchdog_evaluate_retries_while_io_not_done_stops_after_success(self):
        # Fix 1: _watchdog_evaluate must keep returning True on subsequent stale
        # ticks while the ENABLE=0 write has NOT been confirmed (_watchdog_io_done
        # is False), so the tick loop retries the write until it succeeds.
        # Once the write IS confirmed (_watchdog_io_done=True) it must stop.
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        # First evaluation trips.
        self.assertTrue(controller._watchdog_evaluate(self._now[0]))
        self.assertTrue(controller._watchdog_tripped)
        # _watchdog_io_done is still False (no successful write yet).
        self.assertFalse(controller._watchdog_io_done)

        # Subsequent stale ticks MUST still return True (the write must be retried).
        self._now[0] += 5.0
        self.assertTrue(controller._watchdog_evaluate(self._now[0]))
        self._now[0] += 5.0
        self.assertTrue(controller._watchdog_evaluate(self._now[0]))

        # Simulate a successful disable write: mark io_done=True.
        controller._watchdog_io_done = True

        # Now subsequent stale ticks must return False (no redundant write).
        self._now[0] += 5.0
        self.assertFalse(controller._watchdog_evaluate(self._now[0]))
        self._now[0] += 5.0
        self.assertFalse(controller._watchdog_evaluate(self._now[0]))

    def test_watchdog_latch_resets_when_comm_restored(self):
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0
        self.assertTrue(controller._watchdog_evaluate(self._now[0]))

        # A successful read refreshes freshness; latch must clear so a NEW stall
        # can trip again.
        self._now[0] += 1.0
        controller._last_read_ok_time = self._now[0]
        self.assertFalse(controller._watchdog_evaluate(self._now[0]))
        self.assertFalse(controller._watchdog_tripped)

        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0
        self.assertTrue(controller._watchdog_evaluate(self._now[0]))

    def test_watchdog_trip_action_disables_output_cancels_ramp_sets_alarm(self):
        from automatic_transfer_station.rd105_controller import SoftwareRampState

        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller._software_ramp = SoftwareRampState(
            start_temperature=40.0,
            final_target=60.0,
            rate_per_hour=36.0,
            start_time=0.0,
            direction=1,
            last_setpoint=40.0,
            last_step_time=0.0,
        )
        # Pretend the port is open so the trip's IO guard passes.
        controller.ser = SimpleNamespace(is_open=True)

        controller._watchdog_trip_action()

        self.assertEqual([False], disable_calls)
        self.assertIsNone(controller._software_ramp)
        self.assertTrue(controller.over_temperature_alarm)
        self.assertIn("通信丢失或轮询停止", controller.last_error)

    def test_watchdog_trip_action_is_idempotent_no_repeat_writes(self):
        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller.ser = SimpleNamespace(is_open=True)

        controller._watchdog_trip_action()
        controller._watchdog_trip_action()  # second call while still tripped
        controller._watchdog_trip_action()

        # Only ONE disable write despite repeated trip actions (latched).
        self.assertEqual([False], disable_calls)

    def test_watchdog_trip_action_does_not_write_to_closed_port(self):
        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller.ser = None  # not connected

        controller._watchdog_trip_action()

        self.assertEqual([], disable_calls)

    def test_watchdog_tick_trips_via_evaluate_then_action(self):
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller.ser = SimpleNamespace(is_open=True)
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        controller._watchdog_tick()

        self.assertEqual([False], disable_calls)
        self.assertTrue(controller.over_temperature_alarm)

    def test_watchdog_tick_is_noop_when_fresh(self):
        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller.ser = SimpleNamespace(is_open=True)
        self._now[0] += 1.0  # fresh

        controller._watchdog_tick()

        self.assertEqual([], disable_calls)

    def test_watchdog_thread_starts_and_stops_promptly(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        controller.start_watchdog()
        self.assertTrue(controller._watchdog_thread is not None)
        self.assertTrue(controller._watchdog_thread.is_alive())
        self.assertTrue(controller._watchdog_thread.daemon)

        controller.stop_watchdog()
        # Stop must return promptly and the thread must be gone/joined.
        thread = getattr(controller, "_watchdog_thread", None)
        if thread is not None:
            self.assertFalse(thread.is_alive())

    def test_watchdog_thread_does_no_io_after_stop(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        # Heating-active, stale -> would trip if a tick ran.
        controller.last_enable_state = True
        controller._last_read_ok_time = 0.0
        controller.ser = SimpleNamespace(is_open=True)
        controller._clock = lambda: 10_000.0  # very stale

        controller.start_watchdog()
        controller.stop_watchdog()
        disable_calls.clear()

        # After stop, an explicit tick is harmless on its own, but the thread must
        # not be producing IO anymore. Give a brief window for any rogue tick.
        time.sleep(0.05)
        self.assertEqual([], disable_calls)

    # --- Fix 1: retry ENABLE=0 until it succeeds ---

    def test_watchdog_retries_disable_on_each_stale_tick_until_success(self):
        # Fix 1 (CRITICAL): if the first ENABLE=0 write fails (e.g. comm loss),
        # the watchdog MUST retry on subsequent stale ticks — it must NOT latch
        # _watchdog_io_done=True before the write succeeds.
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        disable_calls = []
        # First two write attempts fail; third succeeds.
        write_results = [False, False, True]

        def fake_set_enable(enabled):
            result = write_results.pop(0) if write_results else True
            disable_calls.append(("ENABLE", enabled, result))
            return result

        controller._set_enable_locked = fake_set_enable
        controller.ser = SimpleNamespace(is_open=True)
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        # Tick 1: evaluates stale, trips, tries write — fails, io_done stays False.
        controller._watchdog_tick()
        self.assertEqual(1, len(disable_calls))
        self.assertFalse(disable_calls[0][2], "first attempt should have failed")
        self.assertFalse(controller._watchdog_io_done, "io_done must stay False on write failure")
        self.assertIn("将重试", controller.last_error)

        # Tick 2: still stale, io_done=False → re-evaluates True → retries write,
        # still fails.
        self._now[0] += 1.0
        controller._watchdog_tick()
        self.assertEqual(2, len(disable_calls))
        self.assertFalse(disable_calls[1][2], "second attempt should have failed")
        self.assertFalse(controller._watchdog_io_done)

        # Tick 3: write finally succeeds → io_done latches True.
        self._now[0] += 1.0
        controller._watchdog_tick()
        self.assertEqual(3, len(disable_calls))
        self.assertTrue(disable_calls[2][2], "third attempt should have succeeded")
        self.assertTrue(controller._watchdog_io_done, "io_done must be True after success")

        # Tick 4: io_done=True → evaluate returns False → no more writes.
        self._now[0] += 1.0
        controller._watchdog_tick()
        self.assertEqual(3, len(disable_calls), "no more writes after success")

    def test_watchdog_first_trip_side_effects_not_repeated_on_retry(self):
        # Fix 1: ramp cancel and alarm are first-trip side effects; they must
        # happen exactly once even when the write is retried multiple times.
        from automatic_transfer_station.rd105_controller import SoftwareRampState
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        ramp_cancel_count = [0]
        original_cancel = controller._cancel_software_ramp_locked

        def counting_cancel():
            ramp_cancel_count[0] += 1
            original_cancel()

        controller._cancel_software_ramp_locked = counting_cancel
        controller._software_ramp = SoftwareRampState(
            start_temperature=40.0,
            final_target=60.0,
            rate_per_hour=36.0,
            start_time=0.0,
            direction=1,
            last_setpoint=40.0,
            last_step_time=0.0,
        )
        # Write always fails so we get multiple ticks.
        controller._set_enable_locked = lambda enabled: False
        controller.ser = SimpleNamespace(is_open=True)
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0

        # Three ticks, all writes fail.
        controller._watchdog_tick()
        controller._watchdog_tick()
        controller._watchdog_tick()

        # Ramp must be cancelled exactly once.
        self.assertEqual(1, ramp_cancel_count[0])
        # Alarm must be set.
        self.assertTrue(controller.over_temperature_alarm)

    # --- Fix 2: io_done reset on start_watchdog ---

    def test_start_watchdog_resets_io_done_latch(self):
        # Fix 2: start_watchdog must reset _watchdog_io_done=False so a stale
        # io_done=True from a prior session cannot suppress a real disable.
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        # Simulate leftover state from a prior tripped session.
        controller._watchdog_io_done = True
        controller._watchdog_tripped = True

        controller.start_watchdog()
        controller.stop_watchdog()

        self.assertFalse(controller._watchdog_io_done)
        self.assertFalse(controller._watchdog_tripped)

    def test_watchdog_disable_issued_in_new_session_after_prior_trip(self):
        # Fix 2: after stop_watchdog()+start_watchdog() on the same controller,
        # the watchdog MUST issue ENABLE=0 when reads go stale in the new session,
        # even if _watchdog_io_done was True from the previous session.
        from automatic_transfer_station.rd105_config import WATCHDOG_STALE_SECONDS

        controller = self._heating_controller()
        disable_calls = []
        controller._set_enable_locked = lambda enabled: disable_calls.append(enabled) or True
        controller.ser = SimpleNamespace(is_open=True)

        # --- Session 1: trip and succeed ---
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0
        controller._watchdog_tick()
        self.assertTrue(controller._watchdog_io_done)
        self.assertEqual([False], disable_calls)

        # --- Simulate stop + restart (reuse same instance) ---
        disable_calls.clear()
        controller.stop_watchdog()
        # Reconnect: reset freshness, re-arm the watchdog.
        controller._last_read_ok_time = self._now[0]  # fresh read at reconnect time
        controller.last_enable_state = True
        controller.start_watchdog()
        # Confirm io_done was cleared by start_watchdog.
        self.assertFalse(controller._watchdog_io_done)
        controller.stop_watchdog()  # stop immediately; we'll drive ticks manually

        # --- Session 2: go stale before any successful read ---
        self._now[0] += WATCHDOG_STALE_SECONDS + 1.0
        controller._watchdog_tick()

        # The watchdog MUST have issued ENABLE=0 in this new session.
        self.assertEqual([False], disable_calls)
        self.assertTrue(controller._watchdog_io_done)

    def test_connect_starts_watchdog_and_disconnect_stops_it(self):
        from automatic_transfer_station.rd105_controller import TemperatureController

        controller = TemperatureController()
        started = []
        stopped = []
        controller.start_watchdog = lambda: started.append(True)
        controller.stop_watchdog = lambda: stopped.append(True)
        # Make connect succeed without real serial.
        controller._connect_locked = lambda: True

        controller.connect()
        self.assertEqual([True], started)

        controller.disconnect()
        self.assertEqual([True], stopped)


if __name__ == "__main__":
    unittest.main()
