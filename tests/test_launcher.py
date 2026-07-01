import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# BP7 — packaging/config consistency tests
# ---------------------------------------------------------------------------

class Bp7SingleSourceBoardIpTests(unittest.TestCase):
    """BP7 Fix 1: board IP must come from a single source (controller_base.BOARD_IP)."""

    def test_launcher_ip_default_equals_controller_base_board_ip(self):
        from automatic_transfer_station.controller_base import BOARD_IP
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args([])
        self.assertEqual(BOARD_IP, args.ip)

    def test_gas_network_probe_default_ip_equals_controller_base_board_ip(self):
        from automatic_transfer_station.controller_base import BOARD_IP
        from automatic_transfer_station.gas_network_probe import DEFAULT_IP_ADDRESS

        self.assertEqual(BOARD_IP, DEFAULT_IP_ADDRESS)

    def test_board_ip_is_only_literal_in_controller_base(self):
        """Smoke: BOARD_IP is a non-empty string."""
        from automatic_transfer_station.controller_base import BOARD_IP

        self.assertIsInstance(BOARD_IP, str)
        self.assertTrue(len(BOARD_IP) > 0)


class Bp7PathsFrozenAwarenessTests(unittest.TestCase):
    """BP7 Fix 2: paths.py frozen-build handling must not alter non-frozen paths."""

    def test_project_root_resolves_correctly_when_not_frozen(self):
        import automatic_transfer_station.paths as paths

        # sys.frozen must not be set in the test environment.
        self.assertFalse(getattr(sys, "frozen", False), "test must not run inside a frozen build")

        # In normal (non-frozen) mode, PROJECT_ROOT must be the project root
        # directory — two levels above the paths.py file.
        expected_root = Path(__file__).resolve().parents[1]
        self.assertEqual(expected_root.resolve(), paths.PROJECT_ROOT.resolve())

    def test_config_dir_resolves_under_project_root(self):
        import automatic_transfer_station.paths as paths

        expected = paths.PROJECT_ROOT / "config"
        self.assertEqual(expected.resolve(), paths.CONFIG_DIR.resolve())

    def test_runtime_dir_resolves_under_project_root(self):
        import automatic_transfer_station.paths as paths

        expected = paths.PROJECT_ROOT / "runtime"
        self.assertEqual(expected.resolve(), paths.RUNTIME_DIR.resolve())

    def test_gas_dll_path_resolves_under_vendor(self):
        import automatic_transfer_station.paths as paths

        expected = paths.PROJECT_ROOT / "vendor" / "motion_card_sdk" / "GAS.dll"
        self.assertEqual(expected.resolve(), paths.GAS_DLL_PATH.resolve())


class Bp7ArgparseDefaultConsistencyTests(unittest.TestCase):
    """BP7 Fix 3: argparse defaults must equal the canonical module constants."""

    def test_nosepiece_baud_default_equals_module_constant(self):
        from automatic_transfer_station.nosepiece_controller import DEFAULT_NOSEPIECE_BAUDRATE
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args([])
        self.assertEqual(DEFAULT_NOSEPIECE_BAUDRATE, args.nosepiece_baud)

    def test_temperature_baud_default_equals_module_constant(self):
        from automatic_transfer_station.rd105_config import RD105_BAUDRATE
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args([])
        self.assertEqual(RD105_BAUDRATE, args.temperature_baud)


class LauncherTests(unittest.TestCase):
    def test_launcher_defaults_to_real_hardware(self):
        from automatic_transfer_station.controller_base import BOARD_IP
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args([])

        self.assertFalse(args.smoke_exit)
        self.assertEqual(BOARD_IP, args.ip)
        self.assertIsNone(args.nosepiece_port)
        self.assertFalse(hasattr(args, "sim"))

    def test_launcher_accepts_nosepiece_serial_options(self):
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args(["--nosepiece-port", "COM7", "--nosepiece-baud", "19200"])

        self.assertEqual("COM7", args.nosepiece_port)
        self.assertEqual(19200, args.nosepiece_baud)

    def test_launcher_accepts_temperature_serial_options(self):
        from automatic_transfer_station.run_eth_board_test import parse_args

        args = parse_args(["--temperature-port", "COM5", "--temperature-baud", "38400"])

        self.assertEqual("COM5", args.temperature_port)
        self.assertEqual(38400, args.temperature_baud)

    def test_launcher_uses_disabled_nosepiece_without_serial_port(self):
        from automatic_transfer_station.nosepiece_controller import DisabledNosepieceController
        from automatic_transfer_station.run_eth_board_test import build_nosepiece_controller, parse_args

        controller = build_nosepiece_controller(parse_args([]))

        self.assertIsInstance(controller, DisabledNosepieceController)

    def test_smoke_exit_launches_without_starting_event_loop(self):
        from automatic_transfer_station.run_eth_board_test import main

        self.assertEqual(0, main(["--smoke-exit"]))

    def test_normal_launch_shows_main_and_camera_windows(self):
        from PySide6.QtWidgets import QApplication
        from automatic_transfer_station import run_eth_board_test

        calls = []

        class FakeWindow:
            def __init__(self, **kwargs):
                calls.append(("construct", kwargs))

            def show(self):
                calls.append(("show_main",))

            def show_camera_window(self):
                calls.append(("show_camera",))

        original_window = run_eth_board_test.MainWindow
        original_build_controller = run_eth_board_test.build_controller
        original_exec = QApplication.exec
        try:
            run_eth_board_test.MainWindow = FakeWindow
            run_eth_board_test.build_controller = lambda args: object()
            QApplication.exec = lambda self: 0

            self.assertEqual(0, run_eth_board_test.main(["--no-gamepad"]))
        finally:
            run_eth_board_test.MainWindow = original_window
            run_eth_board_test.build_controller = original_build_controller
            QApplication.exec = original_exec

        self.assertIn(("show_main",), calls)
        self.assertIn(("show_camera",), calls)

    def test_default_real_smoke_exit_constructs_without_opening_network(self):
        from automatic_transfer_station import run_eth_board_test

        class NoOpenApi:
            def open_network(self, *args, **kwargs):
                raise AssertionError("real smoke exit must not call GA_Open")

        original_loader = run_eth_board_test.load_default_api
        try:
            run_eth_board_test.load_default_api = lambda path: NoOpenApi()
            self.assertEqual(0, run_eth_board_test.main(["--smoke-exit"]))
        finally:
            run_eth_board_test.load_default_api = original_loader

    def test_launcher_gamepad_default_on_and_flag_off(self):
        from automatic_transfer_station.run_eth_board_test import parse_args
        self.assertTrue(parse_args([]).gamepad)
        self.assertFalse(parse_args(["--no-gamepad"]).gamepad)

    def test_main_returns_1_and_warns_when_dll_load_fails(self):
        # The most common deploy failure (missing / wrong-bitness GAS.dll) must fail
        # cleanly: return 1 and show an operator dialog, not crash with a raw traceback.
        from automatic_transfer_station import run_eth_board_test
        from PySide6.QtWidgets import QMessageBox
        critical_calls = []
        original_loader = run_eth_board_test.load_default_api
        original_critical = QMessageBox.critical
        try:
            def bad_dll(path):
                raise OSError("simulated bad-bitness GAS.dll")
            run_eth_board_test.load_default_api = bad_dll
            QMessageBox.critical = lambda *a, **k: critical_calls.append(a)  # type: ignore[assignment]
            self.assertEqual(1, run_eth_board_test.main(["--smoke-exit"]))
            self.assertTrue(critical_calls, "an operator dialog must be attempted on DLL load failure")
        finally:
            run_eth_board_test.load_default_api = original_loader
            QMessageBox.critical = original_critical

    def test_main_launches_with_gamepad_disabled_when_reader_init_fails(self):
        # No F710 plugged in / no XInput must NOT abort launch — the gamepad is
        # disabled and the app still starts.
        from automatic_transfer_station import run_eth_board_test

        class NoOpenApi:
            def open_network(self, *a, **k):
                raise AssertionError("smoke exit must not open the network")

        original_reader = run_eth_board_test.load_default_reader
        original_loader = run_eth_board_test.load_default_api
        try:
            def no_xinput():
                raise RuntimeError("XInput unavailable / no controller")
            run_eth_board_test.load_default_reader = no_xinput
            run_eth_board_test.load_default_api = lambda path: NoOpenApi()
            self.assertEqual(0, run_eth_board_test.main(["--smoke-exit"]))
        finally:
            run_eth_board_test.load_default_reader = original_reader
            run_eth_board_test.load_default_api = original_loader


if __name__ == "__main__":
    unittest.main()
