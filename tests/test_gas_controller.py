import sys
import threading
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class FakeApi:
    def __init__(self):
        self.calls = []
        self.positions = {}
        self.statuses = {}
        self.ext_di_bits = {}
        self.open_result = 0
        self.fail_on_call = None

    def open_network(self, ip_address, mode=0):
        self.calls.append(("open_network", ip_address, mode))
        return self.open_result

    def set_card_no(self, card_number):
        self.calls.append(("set_card_no", card_number))
        return 0

    def close(self):
        self.calls.append(("close",))
        return 0

    def reset(self):
        self.calls.append(("reset",))
        return 0

    def axis_on(self, axis):
        self.calls.append(("axis_on", axis))
        return 0

    def clear_status(self, axis, count=1):
        self.calls.append(("clear_status", axis, count))
        return 0

    def get_status(self, axis):
        self.calls.append(("get_status", axis))
        return self.statuses.get(axis, 0)

    def set_hard_limit_positive(self, axis, limit_type, card_index, io_index):
        self.calls.append(("set_hard_limit_positive", axis, limit_type, card_index, io_index))
        if self.fail_on_call == ("set_hard_limit_positive", axis):
            return -7
        return 0

    def set_hard_limit_negative(self, axis, limit_type, card_index, io_index):
        self.calls.append(("set_hard_limit_negative", axis, limit_type, card_index, io_index))
        return 0

    def set_limit_sense_single(self, axis, positive_sense, negative_sense):
        self.calls.append(("set_limit_sense_single", axis, positive_sense, negative_sense))
        return 0

    def limits_on(self, axis, limit_type=-1):
        self.calls.append(("limits_on", axis, limit_type))
        return 0

    def limits_off(self, axis, limit_type=-1):
        self.calls.append(("limits_off", axis, limit_type))
        return 0

    def profile_trap(self, axis):
        self.calls.append(("profile_trap", axis))
        return 0

    def set_trap_prm_single(self, axis, acc, dec, vel_start, smooth_time):
        self.calls.append(("set_trap_prm_single", axis, acc, dec, vel_start, smooth_time))
        return 0

    def profile_jog(self, axis):
        self.calls.append(("profile_jog", axis))
        return 0

    def set_jog_prm_single(self, axis, acc, dec, smooth):
        self.calls.append(("set_jog_prm_single", axis, acc, dec, smooth))
        return 0

    def set_velocity(self, axis, velocity):
        self.calls.append(("set_velocity", axis, velocity))
        return 0

    def set_position(self, axis, position):
        self.calls.append(("set_position", axis, position))
        self.positions[axis] = float(position)
        return 0

    def update(self, mask):
        self.calls.append(("update", mask))
        return 0

    def stop(self, mask, option=0):
        self.calls.append(("stop", mask, option))
        return 0

    def get_prf_pos(self, axis):
        self.calls.append(("get_prf_pos", axis))
        return self.positions.get(axis, 0.0)

    def get_ext_di_bit(self, card_index, bit_index):
        self.calls.append(("get_ext_di_bit", card_index, bit_index))
        return self.ext_di_bits.get((card_index, bit_index), 0)

    def zero_position(self, axis, count=1):
        self.calls.append(("zero_position", axis, count))
        self.positions[axis] = 0.0
        return 0

    def home_set_prm(
        self,
        axis,
        home_mode,
        home_direction,
        offset,
        rapid_velocity,
        locate_velocity,
        index_velocity,
        acceleration,
        delay_before_zero=0,
        home_max_distance=0,
    ):
        self.calls.append(
            (
                "home_set_prm",
                axis,
                home_mode,
                home_direction,
                offset,
                rapid_velocity,
                locate_velocity,
                index_velocity,
                acceleration,
                delay_before_zero,
                home_max_distance,
            )
        )
        return 0

    def home_start(self, axis):
        self.calls.append(("home_start", axis))
        return 0


class BlockingPositionFakeApi(FakeApi):
    def __init__(self):
        super().__init__()
        self.entered_set_velocity = threading.Event()
        self.release_set_velocity = threading.Event()
        self.get_position_finished = threading.Event()

    def set_velocity(self, axis, velocity):
        self.calls.append(("set_velocity", axis, velocity))
        if axis == 3:
            self.entered_set_velocity.set()
            self.release_set_velocity.wait(1.0)
        return 0

    def get_prf_pos(self, axis):
        value = super().get_prf_pos(axis)
        self.get_position_finished.set()
        return value


class GasMotionControllerTests(unittest.TestCase):
    def test_open_board_configures_axis_limit_mapping_and_enables_axes(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)

        controller.open_board()

        self.assertEqual(("open_network", "192.168.0.200", 0), api.calls[0])
        # GA_SetCardNo must NOT be called: the real DLL returns error 7 for it on this
        # single-board network setup (card 0 is already the default) and would abort open.
        self.assertNotIn(("set_card_no", 0), api.calls)
        self.assertIn(("axis_on", 9), api.calls)
        self.assertIn(("set_hard_limit_positive", 2, 1, 0, 3), api.calls)
        self.assertIn(("set_hard_limit_negative", 2, 1, 0, 2), api.calls)
        self.assertIn(("limits_on", 2, 0), api.calls)
        self.assertIn(("limits_on", 2, 1), api.calls)
        self.assertIn(("set_hard_limit_positive", 6, 1, 0, 14), api.calls)
        self.assertIn(("set_hard_limit_negative", 6, 1, 0, 9), api.calls)
        self.assertIn(("set_limit_sense_single", 8, 0, 0), api.calls)
        self.assertIn(("limits_off", 3, -1), api.calls)
        self.assertIn(("limits_off", 9, -1), api.calls)
        self.assertTrue(controller.is_open)

    def test_open_board_failure_after_network_open_stops_closes_and_stays_closed(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.fail_on_call = ("set_hard_limit_positive", 6)
        controller = GasMotionController(api=api)

        with self.assertRaises(MotionControllerError):
            controller.open_board()

        self.assertFalse(controller.is_open)
        self.assertIn(("stop", 0xFFFF, 0), api.calls)
        self.assertIn(("close",), api.calls)

    def test_stop_all_stops_every_axis_without_closing_board(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.stop_all()

        self.assertIn(("stop", 0xFFFF, 0), api.calls)
        self.assertNotIn(("close",), api.calls)
        self.assertTrue(controller.is_open)

    def test_close_board_still_closes_when_stop_fails(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()

        def failing_stop(mask, option=0):
            api.calls.append(("stop", mask, option))
            return -3  # a failing all-axis stop must not abort the close

        api.stop = failing_stop
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.close_board()

        # GA_Close ran and the board is marked closed despite the stop failure.
        self.assertIn(("close",), api.calls)
        self.assertFalse(controller.is_open)

    def test_close_board_keeps_open_when_close_fails(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.close = lambda: -9  # GA_Close failure must leave is_open True for retry
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.close_board()

        self.assertTrue(controller.is_open)

    def test_open_board_is_idempotent_when_already_open(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.open_board()
        api.calls.clear()

        controller.open_board()  # second call must not re-open or re-enable axes

        self.assertEqual([], api.calls)
        self.assertTrue(controller.is_open)

    def test_relative_move_uses_current_position_and_axis_mask(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[2] = 100.0
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.relative_move(axis=2, direction=-1, speed=20.0, step=5.0)

        self.assertIn(("profile_trap", 2), api.calls)
        self.assertIn(("set_trap_prm_single", 2, 0.1, 0.1, 0.0, 0), api.calls)
        self.assertIn(("set_velocity", 2, 20.0), api.calls)
        self.assertIn(("set_position", 2, 95), api.calls)
        self.assertIn(("update", 0b10), api.calls)

    def _set_positions_for_axis(self, api, axis):
        return [c[2] for c in api.calls if c[0] == "set_position" and c[1] == axis]

    # NOTE: Y1 (axis 2) has zero_coordinate=25, so coordinate = 25 + pulse/12500;
    # the in-range [0,50] mm window is pulses [-312500, 312500] (pulse 0 = 25 mm).
    def test_relative_step_accumulates_rapid_same_direction_presses(self):
        # Pressing again mid-move adds a FULL step (extends the move) instead of
        # re-targeting a closer point from the moving position.
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi(); api.positions[2] = 0.0
        controller = GasMotionController(api=api); controller.is_open = True
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # -> 1000
        api.positions[2] = 300.0                                               # move in progress
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # accumulate -> 2000
        self.assertEqual([1000, 2000], self._set_positions_for_axis(api, 2))

    def test_relative_step_reverse_direction_restarts_from_current(self):
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi(); api.positions[2] = 0.0
        controller = GasMotionController(api=api); controller.is_open = True
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # -> 1000
        api.positions[2] = 300.0                                               # in progress
        controller.relative_move(axis=2, direction=-1, speed=20.0, step=1000)  # reverse: from current -> -700
        self.assertEqual([1000, -700], self._set_positions_for_axis(api, 2))

    def test_relative_step_baseline_cleared_by_stop(self):
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi(); api.positions[2] = 0.0
        controller = GasMotionController(api=api); controller.is_open = True
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # -> 1000
        controller.stop_axis(2)                                                # ends accumulation
        api.positions[2] = 300.0
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # fresh from current -> 1300
        self.assertEqual([1000, 1300], self._set_positions_for_axis(api, 2))

    def test_relative_step_refused_out_of_range_does_not_advance_baseline(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi(); api.positions[2] = 300000.0   # 49 mm, near Y1 +max (50 mm = 312500 pulse)
        controller = GasMotionController(api=api); controller.is_open = True
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # -> 301000 (in range)
        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=2, direction=1, speed=20.0, step=200000)  # 501000 -> out of range
        api.positions[2] = 300500.0
        controller.relative_move(axis=2, direction=1, speed=20.0, step=1000)   # accumulate from 301000 -> 302000
        # The refused step never reached set_position and never advanced the baseline.
        self.assertEqual([301000, 302000], self._set_positions_for_axis(api, 2))

    def test_relative_move_rejects_targets_outside_calibrated_soft_range(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[2] = 0.0
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=2, direction=1, speed=20.0, step=400000.0)

        self.assertNotIn(("set_position", 2, 400000), api.calls)

    def test_relative_move_rejects_x2_x3_interference_boundary(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[5] = 200000.0
        api.positions[7] = -900000.0
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=7, direction=-1, speed=20.0, step=200000.0)

        self.assertNotIn(("set_position", 7, -1100000), api.calls)

    def test_absolute_coordinate_move_converts_coordinate_to_pulse_and_uses_axis_mask(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.move_axis_to_coordinate(axis=5, coordinate=4.25, speed=50.0)

        self.assertIn(("profile_trap", 5), api.calls)
        self.assertIn(("set_trap_prm_single", 5, 0.1, 0.1, 0.0, 0), api.calls)
        self.assertIn(("set_velocity", 5, 50.0), api.calls)
        self.assertIn(("set_position", 5, 475398), api.calls)
        self.assertIn(("update", 0b10000), api.calls)

    def test_absolute_coordinate_move_rejects_outside_calibrated_soft_range(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.move_axis_to_coordinate(axis=5, coordinate=50.0, speed=50.0)

        self.assertNotIn(("set_position", 5, -1812102), api.calls)

    def test_motion_before_open_raises_without_hardware_calls(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=1, direction=1, speed=20.0, step=5.0)

        self.assertEqual([], api.calls)

    def test_jog_start_and_stop_use_signed_velocity_and_axis_mask(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.start_jog(axis=3, speed=-12.0)
        controller.stop_axis(3)

        # Axis 3 (MicroscopeZ) drives a profiled GA_PrfTrap move toward the
        # MicroscopeZ sanity cap in the jog direction (negative here), NOT a
        # GA_PrfJog velocity jog. Velocity is abs(speed); stop uses the axis mask.
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        self.assertIn(("profile_trap", 3), api.calls)
        self.assertNotIn(("profile_jog", 3), api.calls)
        self.assertIn(("set_velocity", 3, 12.0), api.calls)
        self.assertIn(("set_position", 3, -int(MICROSCOPE_Z_MAX_ABS_PULSE)), api.calls)
        self.assertIn(("update", 0b100), api.calls)
        self.assertIn(("stop", 0b100, 0), api.calls)

    def test_real_controller_serializes_jog_start_and_position_refresh(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = BlockingPositionFakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        jog_thread = threading.Thread(target=lambda: controller.start_jog(axis=3, speed=-12.0))
        jog_thread.start()
        self.assertTrue(api.entered_set_velocity.wait(0.5))
        api.get_position_finished.clear()

        read_thread = threading.Thread(target=lambda: controller.get_axis_position(3))
        read_thread.start()
        time.sleep(0.05)

        self.assertFalse(api.get_position_finished.is_set())

        api.release_set_velocity.set()
        jog_thread.join(1.0)
        read_thread.join(1.0)

        self.assertTrue(api.get_position_finished.is_set())

    def test_limited_axis_continuous_motion_uses_safe_trap_target_instead_of_unbounded_jog(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[1] = 0.0
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.start_jog(axis=1, speed=12.5)
        controller.stop_axis(1)

        self.assertIn(("profile_trap", 1), api.calls)
        self.assertNotIn(("profile_jog", 1), api.calls)
        self.assertIn(("set_velocity", 1, 12.5), api.calls)
        self.assertIn(("set_position", 1, 625000), api.calls)
        self.assertIn(("update", 0b1), api.calls)

    def test_continuous_motion_clips_x2_x3_target_at_interference_boundary(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[5] = 200000.0
        api.positions[7] = -900000.0
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.start_jog(axis=7, speed=-20.0)

        self.assertIn(("profile_trap", 7), api.calls)
        # Value updated from -1051566 to -1041566 when X2_X3_INTERFERENCE_MARGIN_PULSE
        # was raised from 0 to 10000 (10000 pulse safety gap tightens the clamped target).
        self.assertIn(("set_position", 7, -1041566), api.calls)

    def test_motion_rejects_same_direction_when_hard_limit_status_is_active(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_api import AXIS_STATUS_POS_HARD_LIMIT
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.statuses[1] = AXIS_STATUS_POS_HARD_LIMIT
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=1, direction=1, speed=20.0, step=5.0)

        self.assertIn(("get_status", 1), api.calls)
        self.assertNotIn(("set_position", 1, 5), api.calls)

    def test_motion_allows_reverse_move_out_of_active_hard_limit(self):
        from automatic_transfer_station.gas_api import AXIS_STATUS_POS_HARD_LIMIT
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.statuses[1] = AXIS_STATUS_POS_HARD_LIMIT
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.relative_move(axis=1, direction=-1, speed=20.0, step=5.0)

        self.assertIn(("get_status", 1), api.calls)
        self.assertIn(("set_position", 1, -5), api.calls)

    def test_motion_rejects_negative_direction_when_negative_hard_limit_status_is_active(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_api import AXIS_STATUS_NEG_HARD_LIMIT
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.statuses[2] = AXIS_STATUS_NEG_HARD_LIMIT
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=2, direction=-1, speed=20.0, step=5.0)

        self.assertIn(("get_status", 2), api.calls)
        self.assertNotIn(("set_position", 2, -5), api.calls)

    def test_axis3_mark_and_move_uses_stored_position(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[3] = 123.4
        controller = GasMotionController(api=api)
        controller.is_open = True

        marked = controller.mark_axis3()
        api.positions[3] = 10.0
        controller.move_axis3_to_mark()

        self.assertEqual(123.4, marked)
        self.assertIn(("set_velocity", 3, 20.0), api.calls)
        self.assertIn(("set_position", 3, 123), api.calls)
        self.assertIn(("update", 0b100), api.calls)

    def test_axis3_moves_to_explicit_saved_point_position(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.move_axis3_to_position(456.7)

        self.assertIn(("set_velocity", 3, 20.0), api.calls)
        self.assertIn(("set_position", 3, 456), api.calls)
        self.assertIn(("update", 0b100), api.calls)

    def test_axis9_home_uses_card0_io8_zero_switch_when_already_active(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.ext_di_bits[(0, 8)] = 1
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.home_axis9()

        self.assertIn(("get_ext_di_bit", 0, 8), api.calls)
        self.assertNotIn(("home_start", 9), api.calls)
        self.assertFalse(any(call[0] == "home_set_prm" for call in api.calls))
        self.assertNotIn(("set_position", 9, -41666), api.calls)
        self.assertNotIn(("set_position", 9, 41667), api.calls)

    def test_axis9_home_refuses_to_start_from_outside_global_soft_range(self):
        from automatic_transfer_station.controller_base import AXIS9_DEGREES_PER_PULSE, MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.ext_di_bits[(0, 8)] = 1
        api.positions[9] = round(366.0 / AXIS9_DEGREES_PER_PULSE)
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError) as ctx:
            controller.home_axis9()

        self.assertIn("-10", str(ctx.exception))
        self.assertIn("365", str(ctx.exception))
        self.assertNotIn(("set_position", 9, -41666), api.calls)
        self.assertNotIn(("set_position", 9, 41667), api.calls)
        self.assertFalse(any(call[0] == "home_set_prm" for call in api.calls))
        self.assertNotIn(("home_start", 9), api.calls)

    def test_axis9_home_can_start_outside_io8_search_window(self):
        from automatic_transfer_station.controller_base import AXIS9_DEGREES_PER_PULSE
        from automatic_transfer_station.gas_controller import GasMotionController

        class Io8ActiveAfterPositiveScanFakeApi(FakeApi):
            def get_ext_di_bit(self, card_index, bit_index):
                self.calls.append(("get_ext_di_bit", card_index, bit_index))
                saw_search_start = ("set_position", 9, -41666) in self.calls
                return 1 if saw_search_start and self.positions.get(9, 0.0) > 0 else 0

        for start_angle in (20.0, 350.0):
            with self.subTest(start_angle=start_angle):
                api = Io8ActiveAfterPositiveScanFakeApi()
                api.statuses[9] = 0x800
                api.positions[9] = round(start_angle / AXIS9_DEGREES_PER_PULSE)
                controller = GasMotionController(api=api)
                controller.is_open = True

                controller.home_axis9()

                self.assertIn(("get_ext_di_bit", 0, 8), api.calls)
                self.assertIn(("set_position", 9, -41666), api.calls)
                self.assertIn(("set_position", 9, 41667), api.calls)
                self.assertNotIn(("home_start", 9), api.calls)

    def test_axis9_home_accepts_displayed_ten_degree_boundary(self):
        from automatic_transfer_station.controller_base import AXIS9_DEGREES_PER_PULSE
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[9] = round(10.0 / AXIS9_DEGREES_PER_PULSE)
        api.ext_di_bits[(0, 8)] = 1
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.home_axis9()

        self.assertIn(("get_ext_di_bit", 0, 8), api.calls)
        self.assertNotIn(("set_position", 9, -41666), api.calls)

    def test_axis9_home_scans_negative_then_positive_until_card0_io8_active(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        class Io8ActiveAfterPositiveScanFakeApi(FakeApi):
            def get_ext_di_bit(self, card_index, bit_index):
                self.calls.append(("get_ext_di_bit", card_index, bit_index))
                saw_search_start = ("set_position", 9, -41666) in self.calls
                return 1 if saw_search_start and self.positions.get(9, 0.0) > 0 else 0

        api = Io8ActiveAfterPositiveScanFakeApi()
        api.statuses[9] = 0x800
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.home_axis9()

        self.assertIn(("set_position", 9, -41666), api.calls)
        self.assertIn(("set_position", 9, 41667), api.calls)
        self.assertLess(
            api.calls.index(("set_position", 9, -41666)),
            api.calls.index(("set_position", 9, 41667)),
        )
        self.assertIn(("stop", 0b100000000, 0), api.calls)
        self.assertNotIn(("home_start", 9), api.calls)

    def test_axis9_home_reports_no_zero_when_card0_io8_missing_in_search_window(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.statuses[9] = 0x800
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError) as ctx:
            controller.home_axis9()

        self.assertIn("card0 IO8", str(ctx.exception))
        self.assertIn("-10..10", str(ctx.exception))
        self.assertIn(("set_position", 9, -41666), api.calls)
        self.assertIn(("set_position", 9, 41667), api.calls)
        self.assertNotIn(("home_start", 9), api.calls)

    def test_axis9_io8_home_and_angle_move_match_current_behavior(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.ext_di_bits[(0, 8)] = 1
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.home_axis9()
        controller.move_axis9_to_angle(45.0)

        self.assertIn(("set_velocity", 9, 20.0), api.calls)
        self.assertIn(("set_position", 9, 187500), api.calls)
        self.assertIn(("update", 0b100000000), api.calls)

    def test_axis9_angle_range_allows_home_search_overtravel(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.move_axis9_to_angle(-10.0)
        controller.move_axis9_to_angle(365.0)

        self.assertIn(("set_position", 9, -41666), api.calls)
        self.assertIn(("set_position", 9, 1520833), api.calls)

        with self.assertRaises(MotionControllerError):
            controller.move_axis9_to_angle(-10.1)
        with self.assertRaises(MotionControllerError):
            controller.move_axis9_to_angle(365.1)

    def test_start_jog_on_bounded_axis_refuses_when_no_safe_target(self):
        # Safety: a bounded axis (X3 = axis 5) whose requested direction has no
        # safe continuous target must REFUSE, not silently fall back to an
        # unbounded velocity jog (which would throw away the soft/interference
        # software protection, leaving only the reactive position watchdog).
        #
        # Scenario chosen so the OLD silent fallback was a genuine hole: with the
        # partner lookahead folded in, safe_continuous_target(5, -1, ...) is None
        # (the clamp would reverse the requested direction), yet the 1-pulse
        # safety probe at current-1 IS safe -> the old code reached the unbounded
        # velocity-jog branch. The fix must refuse instead.
        from automatic_transfer_station.axis_calibration import safe_continuous_target, is_target_safe
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        positions = {5: 100000.0, 7: -940000.0}
        lookahead = 20000.0
        # Pin down the scenario invariants the test relies on.
        self.assertIsNone(safe_continuous_target(5, -1, positions, partner_lookahead_pulse=lookahead))
        self.assertTrue(is_target_safe(5, positions[5] - 1, positions).ok)

        api = FakeApi()
        api.positions[5] = positions[5]
        api.positions[7] = positions[7]
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.start_jog(axis=5, speed=-50.0, partner_lookahead_pulse=lookahead)

        # No jog and no motion at all may be issued for the refused direction.
        self.assertNotIn(("profile_jog", 5), api.calls)
        self.assertNotIn(("profile_trap", 5), api.calls)
        self.assertFalse(any(c[0] == "set_velocity" for c in api.calls))
        self.assertFalse(any(c[0] == "set_position" for c in api.calls))
        self.assertFalse(any(c[0] == "update" for c in api.calls))

    def test_start_jog_axis3_uses_bounded_trap_move_toward_sanity_cap(self):
        # Axis 3 (MicroscopeZ) has no soft range. It used to keep a GA_PrfJog
        # velocity jog, but that mode did not produce motion-while-held on the real
        # card, so axis 3 now uses the same GA_PrfTrap profiled move as every other
        # axis — toward a far target at the MicroscopeZ sanity cap in the jog
        # direction. The held button moves the axis; release (stop_axis) halts it
        # long before the cap. The cap is a backstop against a stuck button.
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.start_jog(axis=3, speed=-12.0)

        self.assertIn(("profile_trap", 3), api.calls)
        self.assertNotIn(("profile_jog", 3), api.calls)
        self.assertIn(("set_velocity", 3, 12.0), api.calls)
        self.assertIn(("set_position", 3, -int(MICROSCOPE_Z_MAX_ABS_PULSE)), api.calls)
        self.assertIn(("update", 0b100), api.calls)

    def test_start_jog_axis3_positive_targets_positive_sanity_cap(self):
        # The far trap target follows the jog direction: a positive jog targets
        # +cap so the proportional gamepad trigger can drive MicroscopeZ up.
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        controller.start_jog(axis=3, speed=250.0)  # 5 mm/s in pulse/ms

        self.assertIn(("profile_trap", 3), api.calls)
        self.assertIn(("set_velocity", 3, 250.0), api.calls)
        self.assertIn(("set_position", 3, int(MICROSCOPE_Z_MAX_ABS_PULSE)), api.calls)

    def test_move_axis_to_coordinate_on_uncalibrated_axis_raises_motion_error(self):
        # Fix 2: axis 3 has no absolute coordinate calibration; a coordinate move
        # must raise MotionControllerError (not a bare ValueError) and issue no
        # motion.
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.move_axis_to_coordinate(axis=3, coordinate=1.0, speed=20.0)

        self.assertFalse(any(c[0] == "set_position" for c in api.calls))

    def test_start_jog_passes_partner_lookahead_to_bound(self):
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi()
        api.positions[5] = 700000.0
        api.positions[7] = -890000.0
        controller = GasMotionController(api=api)
        controller.is_open = True
        controller.start_jog(5, -50.0)                       # no lookahead
        no_ahead = next(c for c in reversed(api.calls) if c[0] == "set_position")[2]
        api.positions[5] = 700000.0                          # reset moved axis
        api.calls.clear()
        controller.start_jog(5, -50.0, partner_lookahead_pulse=20000.0)
        with_ahead = next(c for c in reversed(api.calls) if c[0] == "set_position")[2]
        self.assertGreater(with_ahead, no_ahead)

    # ---- Package 7 Test 3 (controller part): get_status raising RuntimeError
    #      must be translated to MotionControllerError and block motion. ----

    def test_get_status_raising_halts_relative_move_with_motion_controller_error(self):
        """If get_status raises RuntimeError (DLL nonzero), relative_move must
        raise MotionControllerError and issue NO motion commands to the API."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[1] = 0.0
        # Override get_status to simulate a DLL failure (what GasApi.get_status
        # does when GA_GetSts returns nonzero).
        def failing_get_status(axis):
            api.calls.append(("get_status", axis))
            raise RuntimeError(f"GA_GetSts({axis}) failed with -1")
        api.get_status = failing_get_status

        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=1, direction=1, speed=20.0, step=5.0)

        # No motion must have been issued to the hardware.
        self.assertFalse(any(c[0] == "set_position" for c in api.calls),
                         "set_position must not be issued when get_status raises")
        self.assertFalse(any(c[0] == "update" for c in api.calls),
                         "update must not be issued when get_status raises")

    def test_get_status_raising_halts_start_jog_with_motion_controller_error(self):
        """start_jog on an axis with a hard-limit config reads get_status first;
        if that raises, start_jog must raise MotionControllerError and issue no
        motion to the API."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[1] = 0.0
        def failing_get_status(axis):
            api.calls.append(("get_status", axis))
            raise RuntimeError(f"GA_GetSts({axis}) failed with -1")
        api.get_status = failing_get_status

        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.start_jog(axis=1, speed=20.0)

        self.assertFalse(any(c[0] in ("set_velocity", "set_position", "update") for c in api.calls),
                         "no motion must be issued when get_status raises")

    # ---- Fix 2: invalid IP → MotionControllerError before any DLL call ----

    def test_open_board_with_empty_ip_raises_motion_controller_error(self):
        """open_board with an empty board_ip must raise MotionControllerError,
        not a bare ValueError or UnicodeEncodeError, and must issue no DLL calls."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api, board_ip="")

        with self.assertRaises(MotionControllerError):
            controller.open_board()

        self.assertNotIn(("open_network",), [c[:1] for c in api.calls])

    def test_open_board_with_nul_ip_raises_motion_controller_error(self):
        """NUL in IP must raise MotionControllerError before any DLL call."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api, board_ip="192.168.0\x00.200")

        with self.assertRaises(MotionControllerError):
            controller.open_board()

    def test_open_board_with_non_ascii_ip_raises_motion_controller_error(self):
        """Non-ASCII IP must raise MotionControllerError before any DLL call."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api, board_ip="192.168.0.2éé")

        with self.assertRaises(MotionControllerError):
            controller.open_board()

    # ---- Fix 1: get_prf_pos RuntimeError in position-read path → MotionControllerError ----

    def test_get_prf_pos_raising_in_get_axis_position_raises_motion_controller_error(self):
        """get_axis_position must translate RuntimeError from get_prf_pos into
        MotionControllerError so it is handled by the GUI's error handler."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        def failing_get_prf_pos(axis):
            api.calls.append(("get_prf_pos", axis))
            raise RuntimeError(f"GA_GetPrfPos({axis}) failed with -2")
        api.get_prf_pos = failing_get_prf_pos

        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.get_axis_position(3)

    def test_get_prf_pos_raising_in_current_positions_for_safety_raises_motion_controller_error(self):
        """_current_positions_for_safety (called by start_jog) must translate
        RuntimeError from get_prf_pos into MotionControllerError."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[3] = 0.0
        def failing_get_prf_pos(axis):
            api.calls.append(("get_prf_pos", axis))
            raise RuntimeError(f"GA_GetPrfPos({axis}) failed with -2")
        api.get_prf_pos = failing_get_prf_pos

        controller = GasMotionController(api=api)
        controller.is_open = True

        # Axis 3 has no finite soft range, so start_jog calls _current_positions_for_safety
        with self.assertRaises(MotionControllerError):
            controller.start_jog(axis=3, speed=10.0)

    def test_get_prf_pos_raising_in_relative_move_raises_motion_controller_error(self):
        """relative_move reads current position via get_prf_pos; a RuntimeError
        there must become MotionControllerError, not escape as bare RuntimeError."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        def failing_get_prf_pos(axis):
            api.calls.append(("get_prf_pos", axis))
            raise RuntimeError(f"GA_GetPrfPos({axis}) failed with -2")
        api.get_prf_pos = failing_get_prf_pos

        controller = GasMotionController(api=api)
        controller.is_open = True

        # Axis 3 has no hard-limit config, so we skip get_status and go straight to get_prf_pos
        with self.assertRaises(MotionControllerError):
            controller.relative_move(axis=3, direction=1, speed=20.0, step=5.0)

    # ---- CP3: MicroscopeZ target guards (int32 wraparound + sanity cap) ----

    def test_cp3_move_axis3_rejects_int32_overflow_target(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        for bad in (3e9, 1e10, -5e9):
            api = FakeApi()
            controller = GasMotionController(api=api)
            controller.is_open = True
            with self.assertRaises(MotionControllerError):
                controller.move_axis3_to_position(bad)
            # No set_position must reach the DLL (would silently wrap mod 2^32).
            self.assertEqual([], [c for c in api.calls if c[0] == "set_position"])

    def test_cp3_move_axis3_rejects_over_sanity_cap_target(self):
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        # Over the sanity cap but still inside int32 — must still be refused.
        over_cap = float(int(MICROSCOPE_Z_MAX_ABS_PULSE) * 2)
        self.assertLess(over_cap, 2 ** 31)
        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True
        with self.assertRaises(MotionControllerError):
            controller.move_axis3_to_position(over_cap)
        self.assertEqual([], [c for c in api.calls if c[0] == "set_position"])

    def test_cp3_move_axis3_allows_normal_in_range_target(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True
        controller.move_axis3_to_position(81506.0)  # realistic marked reading
        self.assertIn(("set_position", 3, 81506), api.calls)

    # ---- CP4: reset_board re-applies the hard-limit configuration ----

    def test_cp4_reset_board_reapplies_hard_limit_configuration(self):
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        controller = GasMotionController(api=api)
        controller.is_open = True
        api.calls.clear()

        controller.reset_board()

        # GA_Reset runs, and the same hard-limit setup open_board installs is re-applied
        # (so an axis driving into a physical limit still auto-stops after a reset).
        self.assertIn(("reset",), api.calls)
        limit_calls = [c for c in api.calls if c[0] in ("set_hard_limit_positive", "set_hard_limit_negative")]
        self.assertTrue(limit_calls, "reset_board must re-apply the hard-limit configuration")
        self.assertTrue([c for c in api.calls if c[0] == "limits_on"],
                        "reset_board must re-enable GA_LmtsOn for limited axes")


    # ---- B1 (CP5): partner-read-fails during interference validation → refuse move ----

    def test_b1_partner_axis_read_fails_during_start_jog_axis5_raises_motion_controller_error(self):
        """Safety pin: if get_prf_pos SUCCEEDS for the moving axis (5) but RAISES
        RuntimeError for the PARTNER axis (7), start_jog must raise
        MotionControllerError and issue NO motion commands.

        This exercises the _current_positions_for_safety path for axis 5: it reads
        both axis 5 (own) and axis 7 (partner) before any move is issued.  A DLL
        failure on the partner read must be surfaced as MotionControllerError (via
        _get_prf_pos) so the move is refused rather than proceeding with an
        incomplete interference check.
        """
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[5] = 687898.0  # X3 at 0 mm — valid starting position
        api.positions[7] = -841567.0  # near boundary — partner value never actually used

        original_get_prf_pos = api.get_prf_pos

        def selective_failing_get_prf_pos(axis):
            if axis == 7:
                api.calls.append(("get_prf_pos", axis))
                raise RuntimeError(f"GA_GetPrfPos({axis}) failed with -2")
            return original_get_prf_pos(axis)

        api.get_prf_pos = selective_failing_get_prf_pos

        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.start_jog(axis=5, speed=50.0)

        # No motion must have been issued to the hardware.
        self.assertFalse(any(c[0] == "set_velocity" for c in api.calls),
                         "set_velocity must not be issued when partner read raises")
        self.assertFalse(any(c[0] == "set_position" for c in api.calls),
                         "set_position must not be issued when partner read raises")
        self.assertFalse(any(c[0] == "update" for c in api.calls),
                         "update must not be issued when partner read raises")

    def test_b1_partner_axis_read_fails_during_start_jog_axis7_raises_motion_controller_error(self):
        """Same as B1 above but for the axis7 (X2) moving, axis5 (X3) as partner."""
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController

        api = FakeApi()
        api.positions[7] = -841567.0  # X2 near boundary
        api.positions[5] = 687898.0   # partner — read will fail

        original_get_prf_pos = api.get_prf_pos

        def selective_failing_get_prf_pos(axis):
            if axis == 5:
                api.calls.append(("get_prf_pos", axis))
                raise RuntimeError(f"GA_GetPrfPos({axis}) failed with -2")
            return original_get_prf_pos(axis)

        api.get_prf_pos = selective_failing_get_prf_pos

        controller = GasMotionController(api=api)
        controller.is_open = True

        with self.assertRaises(MotionControllerError):
            controller.start_jog(axis=7, speed=-50.0)

        self.assertFalse(any(c[0] == "set_velocity" for c in api.calls))
        self.assertFalse(any(c[0] == "set_position" for c in api.calls))
        self.assertFalse(any(c[0] == "update" for c in api.calls))


class GasMotionControllerI18nTests(unittest.TestCase):
    """Task-9: controller messages appear in English when language is set to EN."""

    def test_gas_microscope_z_sanity_cap_error_appears_in_english_when_en_mode(self):
        from automatic_transfer_station.controller_base import MotionControllerError
        from automatic_transfer_station.gas_controller import GasMotionController
        from automatic_transfer_station.i18n import translator

        translator.set_language("en")
        try:
            api = FakeApi()
            controller = GasMotionController(api=api)
            controller.is_open = True
            # An int32-overflow target triggers the first Chinese error message
            with self.assertRaises(MotionControllerError) as ctx:
                controller.move_axis3_to_position(3e9)
            message = str(ctx.exception)
            self.assertNotIn("脉冲", message, "Chinese must not appear in EN mode")
            self.assertIn("int32", message, "English expected in EN mode")
        finally:
            translator.initialize("zh")


if __name__ == "__main__":
    unittest.main()
