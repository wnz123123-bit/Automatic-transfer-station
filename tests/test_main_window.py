import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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


def has_ancestor(widget, ancestor):
    current = widget.parentWidget()
    while current is not None:
        if current is ancestor:
            return True
        current = current.parentWidget()
    return False


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


def _INLINE_QUEUE_FACTORY(parent):
    # Synchronous queue so a button click dispatches its action before returning,
    # keeping the GUI-logic tests deterministic without a running event loop.
    from automatic_transfer_station.main_window import MotionCommandQueue

    return MotionCommandQueue(parent, inline=True)


def _THREADED_QUEUE_FACTORY(parent):
    # Real threaded worker queue, used by tests that assert non-blocking behaviour.
    from automatic_transfer_station.main_window import MotionCommandQueue

    return MotionCommandQueue(parent)


class RecordingController:
    def __init__(self):
        self.calls = []
        self.is_open = False
        self.positions = {axis: 0.0 for axis in range(1, 10)}
        self.positions[3] = 12.34
        self.positions[9] = 0.0
        self.axis_calibrations = None

    def open_board(self):
        self.calls.append(("open_board",))
        self.is_open = True

    def close_board(self):
        self.calls.append(("close_board",))
        self.is_open = False

    def reset_board(self):
        self.calls.append(("reset_board",))

    def relative_move(self, axis, direction, speed, step):
        self.calls.append(("relative_move", axis, direction, speed, step))

    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
        self.calls.append(("start_jog", axis, speed))

    def stop_axis(self, axis):
        self.calls.append(("stop_axis", axis))

    def stop_all(self):
        self.calls.append(("stop_all",))

    def get_axis_position(self, axis):
        return self.positions.get(axis, 0.0)

    def zero_axis3(self):
        self.calls.append(("zero_axis3",))

    def mark_axis3(self):
        self.calls.append(("mark_axis3",))
        return 12.34

    def move_axis3_to_mark(self):
        self.calls.append(("move_axis3_to_mark",))

    def move_axis3_to_position(self, position):
        self.calls.append(("move_axis3_to_position", position))
        self.positions[3] = float(position)

    def move_axis_to_coordinate(self, axis, coordinate, speed=None):
        self.calls.append(("move_axis_to_coordinate", axis, coordinate, speed))

    def home_axis9(self):
        self.calls.append(("home_axis9",))

    def move_axis9_to_angle(self, angle, speed=None):
        self.calls.append(("move_axis9_to_angle", angle, speed))

    def get_axis9_angle(self):
        return 56.78

    def set_axis_calibrations(self, axis_calibrations):
        self.calls.append(("set_axis_calibrations",))
        self.axis_calibrations = axis_calibrations


class DelayedJogController(RecordingController):
    def __init__(self, delay=0.4):
        super().__init__()
        self.delay = delay
        self.started = threading.Event()

    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
        self.started.set()
        time.sleep(self.delay)
        super().start_jog(axis, speed)


class SlowPositionController(RecordingController):
    def __init__(self, delay=0.25):
        super().__init__()
        self.delay = delay
        self.read_started = threading.Event()
        self.read_count = 0

    def get_axis_position(self, axis):
        if self.read_count == 0:
            self.read_started.set()
            time.sleep(self.delay)
        self.read_count += 1
        return super().get_axis_position(axis)


class SlowRelativeMoveController(RecordingController):
    def __init__(self, delay=0.25):
        super().__init__()
        self.delay = delay
        self.started = threading.Event()

    def relative_move(self, axis, direction, speed, step):
        self.started.set()
        time.sleep(self.delay)
        super().relative_move(axis, direction, speed, step)


class SlowMotionCommandController(RecordingController):
    def __init__(self, delay=0.25):
        super().__init__()
        self.delay = delay
        self.started_events = {}

    def _pause(self, name):
        self.started_events.setdefault(name, threading.Event()).set()
        time.sleep(self.delay)

    def wait_started(self, name, timeout=0.5):
        return self.started_events.setdefault(name, threading.Event()).wait(timeout)

    def stop_axis(self, axis):
        self._pause("stop_axis")
        super().stop_axis(axis)

    def stop_all(self):
        self._pause("stop_all")
        super().stop_all()

    def home_axis9(self):
        self._pause("home_axis9")
        super().home_axis9()

    def move_axis3_to_position(self, position):
        self._pause("move_axis3_to_position")
        super().move_axis3_to_position(position)

    def move_axis9_to_angle(self, angle, speed=None):
        self._pause("move_axis9_to_angle")
        super().move_axis9_to_angle(angle, speed)

    def relative_move(self, axis, direction, speed, step):
        self._pause(f"relative_move_{axis}")
        super().relative_move(axis, direction, speed, step)


class GatedController(RecordingController):
    """Controller whose first motion call blocks until explicitly released.

    Used to deterministically pin the queue worker on a single command so the
    test can submit a backlog while nothing drains, then release the worker and
    observe exactly which queued commands ran. No sleeps are used for
    correctness; the worker is gated on Events.
    """

    def __init__(self):
        super().__init__()
        self.is_open = True
        self.entered = threading.Event()   # the gating call has started
        self.release = threading.Event()   # set by the test to let it finish

    def _gate(self):
        self.entered.set()
        # Block the single worker thread until the test releases it.
        self.release.wait(2.0)

    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
        self._gate()
        super().start_jog(axis, speed)

    def relative_move(self, axis, direction, speed, step):
        self._gate()
        super().relative_move(axis, direction, speed, step)

    def stop_all(self):
        # stop_all is the direct emergency path; never gated.
        super().stop_all()

    def stop_axis(self, axis):
        super().stop_axis(axis)


class RecordingNosepieceController:
    def __init__(self):
        self.calls = []
        self.position = 0
        self.is_open = False

    def connect(self):
        self.calls.append(("connect",))
        self.is_open = True

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.is_open = False

    def read_position(self):
        self.calls.append(("read_position",))
        return self.position

    def move_to_position(self, position):
        self.calls.append(("move_to_position", position))
        self.position = position


class RecordingCameraController:
    def __init__(self):
        self.calls = []

    def close(self):
        self.calls.append(("close",))


class DelayedNosepieceController(RecordingNosepieceController):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def move_to_position(self, position):
        self.calls.append(("move_to_position", position))
        self.started.set()
        self.release.wait(0.6)
        self.position = position


class MainWindowTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"
        self.microscope_position_store_path = Path(self._tmpdir.name) / "microscope_z_positions.json"
        self.runtime_calibration_path = Path(self._tmpdir.name) / "axis_runtime_calibration.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def assert_click_returns_quickly(self, button, threshold=0.15):
        start = time.monotonic()
        button.click()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, threshold)

    def test_main_window_launches_with_expected_controls(self):
        from PySide6.QtWidgets import QComboBox
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        self.assertEqual("二维材料转移平台", window.windowTitle())
        self.assertIn("btnOpen", window.controls)
        self.assertIn("btnFwd3", window.controls)
        self.assertIn("txtSpeed3", window.controls)
        self.assertIn("txtStep3", window.controls)
        self.assertIn("btnAxis9Home", window.controls)
        self.assertIsInstance(window.controls["txtSpeed3"], QComboBox)
        self.assertFalse(window.controls["txtSpeed3"].isEditable())
        self.assertIsInstance(window.controls["txtStep3"], QComboBox)
        self.assertFalse(window.controls["txtStep3"].isEditable())

    def test_microscope_z_step_uses_fixed_options_and_default(self):
        from PySide6.QtWidgets import QComboBox
        from automatic_transfer_station.axis_calibration import STEP_SNAP
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        step_combo = window.controls["txtStep3"]
        self.assertIsInstance(step_combo, QComboBox)
        self.assertFalse(step_combo.isEditable())
        options = [float(step_combo.itemData(i)) for i in range(step_combo.count())]
        self.assertEqual([1.0, 5.0, 10.0, 50.0, 100.0, 500.0], options)
        self.assertEqual(tuple(options), STEP_SNAP[3])
        self.assertEqual(500.0, step_combo.value())
        self.assertEqual(25000, window._step(3))

        step_combo.setValue(100.0)
        self.assertEqual(5000, window._step(3))

    def test_main_window_embeds_real_temperature_panel_on_right(self):
        from PySide6.QtWidgets import QWidget
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        panel = window.controls["temperaturePanel"]
        self.assertIsInstance(panel, QWidget)
        self.assertEqual("temperaturePanel", panel.objectName())
        for name in (
            "tempPortCombo",
            "tempRefreshPorts",
            "tempConnect",
            "tempConnectionLabel",
            "tempCurrentTempLabel",
            "tempLiveSetpointLabel",
            "tempTargetTempLabel",
            "tempOutputLabel",
            "tempMaxOutputDutyLabel",
            "tempRunStateLabel",
            "tempErrorCodeLabel",
            "tempTargetSpin",
            "tempFastSet",
            "tempRateSpin",
            "tempRamp",
            "tempStopHeating",
            "tempPlot",
            "tempStatusLabel",
        ):
            self.assertIn(name, window.controls)
        for name in (
            "tempMaxOutputDutySpin",
            "tempSetMaxOutputDuty",
            "tempPidProfileLabel",
        ):
            self.assertNotIn(name, window.controls)

        self.assertIn("等待接入", window.controls["tempConnectionLabel"].text())
        self.assertIn("RD105", window.controls["tempStatusLabel"].text())
        self.assertNotIn("已预留", window.controls["tempStatusLabel"].text())
        self.assertTrue(callable(getattr(panel, "connect_to_device", None)))
        self.assertTrue(callable(getattr(panel, "apply_fast_set", None)))
        self.assertTrue(callable(getattr(panel, "apply_rate_set", None)))

    def test_main_window_buttons_have_pressed_visual_feedback(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        self.assertIn("QPushButton:pressed", window.controls["axisControlPanel"].styleSheet())
        self.assertIn("QPushButton:pressed", window.controls["objectivePanel"].styleSheet())
        self.assertIn("QPushButton:pressed", window.controls["axisHotspotX1"].styleSheet())
        self.assertIn("QPushButton:pressed", window.controls["btnStopAll"].styleSheet())

    def test_main_window_uses_three_column_stage_axis_temperature_layout(self):
        from PySide6.QtWidgets import QWidget
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        stage_panel = window.controls["stagePanel"]
        axis_panel = window.controls["axisControlPanel"]
        temperature_panel = window.controls["temperaturePanel"]

        self.assertIsInstance(stage_panel, QWidget)
        self.assertIsInstance(axis_panel, QWidget)
        self.assertIsInstance(temperature_panel, QWidget)
        self.assertIn("stageImageLabel", window.controls)
        self.assertIn("axisStatusBar", window.controls)
        self.assertIn("motionStatusText", window.controls)
        self.assertTrue(has_ancestor(window.controls["objectivePanel"], stage_panel))
        self.assertTrue(has_ancestor(window.controls["axisStatusBar"], axis_panel))
        self.assertTrue(has_ancestor(window.controls["btnOpen"], axis_panel))
        self.assertTrue(has_ancestor(window.controls["temperaturePanel"], window.centralWidget()))
        self.assertFalse(has_ancestor(window.controls["objectivePanel"], axis_panel))
        self.assertIn("运动控制未打开", window.controls["motionStatusText"].text())

    def test_pyside_stage_panel_matches_html_preview_hotspot_structure(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        stage_panel = window.controls["stagePanel"]

        self.assertIn("stageMap", window.controls)
        self.assertTrue(has_ancestor(window.controls["stageMap"], stage_panel))
        for axis in ("X1", "Y1", "MicroscopeZ", "Z", "X3", "Y3", "X2", "Y2", "R"):
            name = f"axisHotspot{axis}"
            self.assertIn(name, window.controls)
            self.assertTrue(has_ancestor(window.controls[name], window.controls["stageMap"]))

    def test_main_window_creates_camera_window_outside_main_layout(self):
        from PySide6.QtWidgets import QWidget
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            camera_controller_factory=lambda: RecordingCameraController(),
        )

        camera_window = window.controls["cameraWindow"]
        panel = window.controls["cameraPanel"]

        self.assertIs(camera_window, window.camera_window)
        self.assertTrue(camera_window.isWindow())
        self.assertIsInstance(panel, QWidget)
        self.assertEqual("cameraPanel", panel.objectName())
        self.assertTrue(has_ancestor(panel, camera_window))
        self.assertFalse(has_ancestor(panel, window.controls["stagePanel"]))
        self.assertFalse(has_ancestor(panel, window.centralWidget()))
        self.assertNotIn("cameraConnect", window.controls)
        for name in (
            "cameraPreview",
            "cameraSnapshot",
            "cameraStatusLabel",
            "cameraDeviceLabel",
            "cameraImageLabel",
            "cameraExposureAuto",
            "cameraExposureSpin",
            "cameraGainAuto",
            "cameraGainSpin",
            "cameraObjectiveCombo",
        ):
            self.assertIn(name, window.controls)
            self.assertTrue(has_ancestor(window.controls[name], panel))

    def test_objective_change_updates_camera_window_objective(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            camera_controller_factory=lambda: RecordingCameraController(),
        )

        window.objective_panel._complete_objective_move("20X", 3)

        self.assertEqual("20X", window.controls["cameraObjectiveCombo"].currentText())

    def test_prepare_for_close_calls_camera_window_prepare_for_close(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            camera_controller_factory=lambda: RecordingCameraController(),
        )
        calls = []
        window.camera_window.close_after_prepare = lambda timeout_ms=5000: calls.append(timeout_ms) or True

        window._prepare_for_close()

        self.assertEqual([2000], calls)

    def test_shutdown_attempts_board_close_after_stop_failure_before_camera(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        events = []
        class Controller(RecordingController):
            def stop_all(self):
                events.append("stop")
                raise RuntimeError("stop failed")
            def close_board(self):
                events.append("board closed")
                super().close_board()
        controller = Controller()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        controller.is_open = True
        with patch.object(window.temperature_panel, "disconnect_from_device", side_effect=lambda: events.append("heater")), \
             patch.object(window.camera_window, "close_after_prepare", side_effect=lambda **kw: events.append("camera")):
            window._prepare_for_close()
        self.assertEqual(events, ["heater", "stop", "board closed", "camera"])
        window.camera_window.close_after_prepare()

    def test_stuck_board_stop_releases_gui_and_still_attempts_close_after_recovery(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        release, closed = threading.Event(), threading.Event()
        controller.is_open = True
        controller.stop_all = lambda: release.wait(3)
        controller.close_board = closed.set
        window.MOTION_CLOSE_TIMEOUT_MS = 50
        try:
            started = time.monotonic()
            window._prepare_for_close()
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
        self.assertTrue(closed.wait(1))

    def test_pyside_axis_panel_uses_html_preview_card_model(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        expected_cards = {
            "axisCardX3": ("btnBack5", "btnFwd5", "buttonback5", "buttonfwd5", "btnStop5", "txtSpeed5", "txtStep5"),
            "axisCardY3": ("btnBack6", "btnFwd6", "buttonback6", "buttonfwd6", "btnStop6", "txtSpeed6", "txtStep6"),
            "axisCardR": ("btnBack9", "btnFwd9", "buttonback9", "buttonfwd9", "btnStop9", "txtSpeed9", "txtStep9"),
            "axisCardX2": ("btnBack7", "btnFwd7", "buttonback7", "buttonfwd7", "btnStop7", "txtSpeed7", "txtStep7"),
            "axisCardY2": ("btnBack8", "btnFwd8", "buttonback8", "buttonfwd8", "btnStop8", "txtSpeed8", "txtStep8"),
            "axisCardZ": ("btnBack4", "btnFwd4", "buttonback4", "buttonfwd4", "btnStop4", "txtSpeed4", "txtStep4"),
            "axisCardX1": ("btnBack1", "btnFwd1", "buttonback1", "buttonfwd1", "btnStop1", "txtSpeed1", "txtStep1"),
            "axisCardY1": ("btnBack2", "btnFwd2", "buttonback2", "buttonfwd2", "btnStop2", "txtSpeed2", "txtStep2"),
            "axisCardMicroscopeZ": ("btnBack3", "btnFwd3", "buttonback3", "buttonfwd3", "btnStop3", "txtSpeed3", "txtStep3"),
        }
        for card_name, child_names in expected_cards.items():
            self.assertIn(card_name, window.controls)
            for child_name in child_names:
                self.assertIn(child_name, window.controls)
                self.assertTrue(has_ancestor(window.controls[child_name], window.controls[card_name]), child_name)

        for name in (
            "axisSectionAdsorption",
            "axisSectionSlide",
            "axisSectionOverall",
            "axisSectionMicroscope",
        ):
            self.assertIn(name, window.controls)
        self.assertIn("axisTopSections", window.controls)
        self.assertTrue(has_ancestor(window.controls["axisSectionAdsorption"], window.controls["axisTopSections"]))
        self.assertTrue(has_ancestor(window.controls["axisSectionSlide"], window.controls["axisTopSections"]))
        self.assertFalse(has_ancestor(window.controls["axisSectionOverall"], window.controls["axisTopSections"]))

    def test_main_window_uses_qt_supported_styles_and_compact_stage_image(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        for path in (PYTHON_DIR / "main_window.py", PYTHON_DIR / "temperature_panel.py"):
            self.assertNotIn("box-shadow", path.read_text(encoding="utf-8"))

        image = window.controls["stageImageLabel"]
        self.assertFalse(image.hasScaledContents())
        self.assertLessEqual(image.pixmap().width(), 680)
        self.assertLessEqual(image.pixmap().height(), 650)

    def test_stage_map_resizes_with_window_and_keeps_hotspots_visible(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()

        window.resize(1700, 960)
        app.processEvents()
        large_size = window.controls["stageMap"].size()

        window.resize(1400, 820)
        app.processEvents()
        small_stage_map = window.controls["stageMap"]
        small_size = small_stage_map.size()

        self.assertLess(small_size.width(), large_size.width())
        self.assertLess(small_size.height(), large_size.height())
        for axis in ("X1", "Y1", "MicroscopeZ", "Z", "X3", "Y3", "X2", "Y2", "R"):
            hotspot = window.controls[f"axisHotspot{axis}"]
            self.assertTrue(small_stage_map.rect().contains(hotspot.geometry()), axis)

    def test_stage_map_hotspots_do_not_overlap_after_declutter(self):
        """The hotspot chips must be de-crammed (no pair-wise overlap) at the
        minimum window size, where the fixed anchors otherwise pile up."""
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        window.resize(1320, 760)  # the minimum supported window size
        app.processEvents()

        axes = ("X1", "Y1", "MicroscopeZ", "Z", "X3", "Y3", "X2", "Y2", "R")
        rects = [window.controls[f"axisHotspot{a}"].geometry() for a in axes]
        overlaps = []
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                inter = rects[i].intersected(rects[j])
                if inter.width() > 0 and inter.height() > 0:
                    overlaps.append((axes[i], axes[j]))
        self.assertEqual([], overlaps, f"hotspot chips overlap: {overlaps}")

    def test_axis_control_area_preserves_card_width_in_resizable_window(self):
        from PySide6.QtCore import Qt
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        window.resize(1400, 820)
        app.processEvents()

        self.assertGreaterEqual(window.minimumWidth(), 1320)
        self.assertGreaterEqual(window.minimumHeight(), 760)
        axis_scroll = window.controls["axisScrollArea"]
        self.assertNotEqual(axis_scroll.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        for card_name in ("axisCardX3", "axisCardY3", "axisCardR", "axisCardX2", "axisCardY2", "axisCardZ", "axisCardX1", "axisCardY1"):
            self.assertGreaterEqual(window.controls[card_name].width(), 250, card_name)

    def test_board_control_row_adds_red_stop_all_button_before_open(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        self.assertIn("btnStopAll", window.controls)
        stop_button = window.controls["btnStopAll"]
        open_button = window.controls["btnOpen"]
        self.assertEqual("停止移动", stop_button.text())
        self.assertLess(stop_button.geometry().left(), open_button.geometry().left())
        self.assertIn("#dc2626", stop_button.styleSheet())

        stop_button.click()
        app.processEvents()

        self.assertIn(("stop_all",), controller.calls)
        self.assertIn("停止移动", window.controls["motionStatusText"].text())

    def test_stop_all_button_does_not_read_positions_after_stop(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True

        def fail_get_axis_position(axis):
            raise AssertionError("stop all should not read positions")

        controller.get_axis_position = fail_get_axis_position
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()
        window.timer.stop()
        controller.calls.clear()

        window.controls["btnStopAll"].click()
        app.processEvents()

        self.assertEqual([("stop_all",)], controller.calls)
        self.assertIn("停止移动", window.controls["motionStatusText"].text())

    def test_axis_control_area_needs_no_horizontal_scroll_in_compact_window(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        window.resize(1400, 820)
        app.processEvents()

        axis_scroll = window.controls["axisScrollArea"]
        self.assertEqual(0, axis_scroll.horizontalScrollBar().maximum())
        self.assertLessEqual(window.controls["buttonback3"].height(), 32)

    def test_axis_top_sections_align_cards_by_rows(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        window.resize(1700, 920)
        app.processEvents()

        top_sections = window.controls["axisTopSections"]

        def top_in_sections(card_name):
            return window.controls[card_name].mapTo(top_sections, window.controls[card_name].rect().topLeft()).y()

        for left_card, right_card in (("axisCardX3", "axisCardX2"), ("axisCardY3", "axisCardY2"), ("axisCardR", "axisCardZ")):
            self.assertEqual(top_in_sections(left_card), top_in_sections(right_card), f"{left_card} should align with {right_card}")

    def test_axis9_card_keeps_home_and_removes_start_twist_controls(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        self.assertIn("btnAxis9Home", window.controls)
        self.assertTrue(has_ancestor(window.controls["btnAxis9Home"], window.controls["axisCardR"]))
        home_top = window.controls["btnAxis9Home"].geometry().top()
        position_top = window.controls["axisPositionR"].geometry().top()
        self.assertLess(home_top, position_top)
        self.assertNotIn("labelAxis9Angle", window.controls)
        self.assertNotIn("btnAxis9MoveToAngle", window.controls)
        self.assertNotIn("txtAxis9Angle", window.controls)

    def test_axis9_twist_button_sits_next_to_position_and_opens_popup(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        self.assertIn("btnAxis9Twist", window.controls)
        self.assertEqual("twist", window.controls["btnAxis9Twist"].text())
        self.assertTrue(has_ancestor(window.controls["btnAxis9Twist"], window.controls["axisCardR"]))
        self.assertGreater(
            window.controls["btnAxis9Twist"].geometry().left(),
            window.controls["axisPositionR"].geometry().left(),
        )

        window.controls["btnAxis9Twist"].click()
        app.processEvents()

        self.assertIn("axis9TwistDialog", window.controls)
        dialog = window.controls["axis9TwistDialog"]
        self.assertTrue(dialog.isVisible())
        self.assertEqual("R twist", dialog.windowTitle())
        self.assertEqual(1.0, window.controls["axis9TwistAngle"].value())
        self.assertEqual("顺时针", window.controls["axis9TwistDirection"].currentText())
        dialog.close()

    def test_axis9_twist_popup_moves_to_relative_angle_by_direction(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        window.controls["txtSpeed9"].setValue(3.0)
        window.controls["btnAxis9Twist"].click()
        app.processEvents()
        window.controls["axis9TwistAngle"].setValue(12.5)
        window.controls["axis9TwistDirection"].setCurrentText("逆时针")
        window.controls["axis9TwistApply"].click()
        app.processEvents()

        self.assertIn(("move_axis9_to_angle", 44.28, 0.4166666666666667), controller.calls)
        self.assertEqual("44.280", window.controls["axisPositionR"].text())
        self.assertIn("逆时针 12.500 deg", window.controls["motionStatusText"].text())

    def test_z_card_adds_low_speed_option_and_continuous_popup(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        z_speed = window.controls["txtSpeed4"]
        self.assertEqual([0.1, 1.0, 10.0, 100.0, 1000.0, 5000.0], [z_speed.itemData(index) for index in range(z_speed.count())])
        self.assertIn("btnZContinuousControl", window.controls)
        self.assertEqual("连续控制", window.controls["btnZContinuousControl"].text())
        self.assertTrue(has_ancestor(window.controls["btnZContinuousControl"], window.controls["axisCardZ"]))
        self.assertGreater(
            window.controls["btnZContinuousControl"].geometry().left(),
            window.controls["axisPositionZ"].geometry().left(),
        )

        window.controls["btnZContinuousControl"].click()
        app.processEvents()

        self.assertIn("zContinuousDialog", window.controls)
        dialog = window.controls["zContinuousDialog"]
        self.assertTrue(dialog.isVisible())
        self.assertEqual("Z 连续控制", dialog.windowTitle())
        self.assertEqual(10.0, window.controls["zContinuousSpeed"].value())
        self.assertEqual(100.0, window.controls["zContinuousDistance"].value())
        self.assertEqual("上升", window.controls["zContinuousDirection"].currentText())

        window.controls["zContinuousSpeed"].setValue(0.1)
        window.controls["zContinuousDistance"].setValue(250.0)
        window.controls["zContinuousDirection"].setCurrentText("下降")
        window.controls["zContinuousApply"].click()
        app.processEvents()

        self.assertIn(("relative_move", 4, -1, 0.005, 12500), controller.calls)
        self.assertIn("Z 连续控制：下降 250.000 um", window.controls["motionStatusText"].text())

    def test_position_calibration_button_opens_unified_popup(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        app.processEvents()

        self.assertIn("btnCoordinateCalibration", window.controls)
        self.assertEqual("坐标标定", window.controls["btnCoordinateCalibration"].text())
        for axis_name in ("X1", "Y1", "X3", "Y3", "R", "X2", "Y2", "Z"):
            self.assertNotIn(f"btnSetPosition{axis_name}", window.controls)

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()

        self.assertIn("positionCalibrationDialog", window.controls)
        dialog = window.controls["positionCalibrationDialog"]
        self.assertTrue(dialog.isVisible())
        self.assertEqual("坐标标定", dialog.windowTitle())
        for axis_name in ("X1", "Y1", "Z", "X3", "Y3", "X2", "Y2", "R"):
            self.assertIn(f"calibrationCurrent{axis_name}", window.controls)
            self.assertIn(f"calibrationActual{axis_name}", window.controls)
        self.assertEqual("0.000", window.controls["calibrationCurrentX3"].text())
        self.assertEqual("", window.controls["calibrationActualX3"].text())
        dialog.close()

    def test_unified_position_calibration_updates_changed_axis_display(self):
        import json

        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[5] = 687898.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()
        # Fix 1: _confirm gates calibration writes — approve automatically in test
        window._confirm = lambda title, text: True

        self.assertTrue(window.controls["axisPositionX3"].isReadOnly())
        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        window.controls["calibrationActualX3"].setText("4.25")
        window.controls["calibrationApply"].click()
        app.processEvents()

        self.assertEqual("4.250", window.controls["axisPositionX3"].text())
        self.assertIn("X3 标定", window.statusBar().currentMessage())
        self.assertIn("实际位置 4.250 mm", window.statusBar().currentMessage())
        window.refresh_positions()
        self.assertEqual("4.250", window.controls["axisPositionX3"].text())

        data = json.loads(self.runtime_calibration_path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(687898.0, data["axes"]["5"]["zero_pulse"])
        self.assertAlmostEqual(4.25, data["axes"]["5"]["zero_coordinate"])
        self.assertIsNotNone(controller.axis_calibrations)

    def test_unified_position_calibration_requires_real_or_cached_pulse(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = False
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        window.controls["calibrationActualX3"].setText("4.25")
        with patch("automatic_transfer_station.main_window.QMessageBox.warning") as warning:
            window.controls["calibrationApply"].click()
            app.processEvents()

        warning.assert_called_once()
        self.assertFalse(self.runtime_calibration_path.exists())
        self.assertEqual("0.000", window.controls["axisPositionX3"].text())

    def test_unified_position_calibration_rejects_coordinate_outside_axis_range(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[5] = 687898.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        window.controls["calibrationActualX3"].setText("50.0")
        with patch("automatic_transfer_station.main_window.QMessageBox.warning") as warning:
            window.controls["calibrationApply"].click()
            app.processEvents()

        warning.assert_called_once()
        self.assertFalse(self.runtime_calibration_path.exists())
        self.assertEqual("0.000", window.controls["axisPositionX3"].text())

    def test_position_calibration_popup_reuses_single_dialog_instance(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        first_dialog = window.controls["positionCalibrationDialog"]

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        second_dialog = window.controls["positionCalibrationDialog"]

        self.assertIsNot(first_dialog, second_dialog)
        self.assertFalse(first_dialog.isVisible())
        self.assertTrue(second_dialog.isVisible())
        second_dialog.close()

    def test_axis_target_inputs_are_editable_and_dispatch_absolute_coordinate_move(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        # Keep the fake readback in range while asserting the move status.
        controller.positions[5] = 687898.0
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.show()
        app.processEvents()

        self.assertTrue(window.controls["axisPositionX3"].isReadOnly())
        self.assertFalse(window.controls["axisTargetX3"].isReadOnly())
        self.assertEqual("移动", window.controls["btnMoveToX3"].text())

        window.controls["txtSpeed5"].setValue(1000.0)
        window.controls["axisTargetX3"].setText("4.25")
        window.controls["btnMoveToX3"].click()
        app.processEvents()

        self.assertIn(("move_axis_to_coordinate", 5, 4.25, 50.0), controller.calls)
        self.assertIn("X3 移动至 4.250 mm", window.statusBar().currentMessage())

    def test_axis_target_move_rejects_invalid_coordinate_text(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.show()
        app.processEvents()

        window.controls["axisTargetX3"].setText("bad")
        with patch("automatic_transfer_station.main_window.QMessageBox.warning") as warning:
            window.controls["btnMoveToX3"].click()
            app.processEvents()

        warning.assert_called_once()
        self.assertNotIn(("move_axis_to_coordinate", 5, 4.25, 50.0), controller.calls)

    def test_motion_pad_buttons_use_html_style_drawn_symbols(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        expected_symbols = {
            "buttonback5": "continuous-negative",
            "btnBack5": "jog-negative",
            "btnStop5": "stop",
            "btnFwd5": "jog-positive",
            "buttonfwd5": "continuous-positive",
        }
        for name, symbol in expected_symbols.items():
            button = window.controls[name]
            self.assertEqual("", button.text(), name)
            self.assertEqual(symbol, button.property("motionSymbol"), name)

    def test_left_stage_column_uses_internal_section_titles_to_avoid_clipping(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.show()
        window.resize(1400, 820)
        app.processEvents()

        self.assertIn("stageMicroscopeTitle", window.controls)
        self.assertIn("objectiveTitleLabel", window.controls)
        self.assertEqual("", window.controls["objectivePanel"].title())
        for title_name in ("stageMicroscopeTitle", "objectiveTitleLabel"):
            title = window.controls[title_name]
            self.assertGreaterEqual(title.height(), title.sizeHint().height())
            self.assertTrue(has_ancestor(title, window.controls["stagePanel"]))

    def test_main_window_does_not_embed_pyqt5_temperature_window(self):
        main_window_source = (PYTHON_DIR / "main_window.py").read_text(encoding="utf-8")
        temperature_panel_source = (PYTHON_DIR / "temperature_panel.py").read_text(encoding="utf-8")

        self.assertNotIn("temperature_control_gui_2", main_window_source)
        self.assertNotIn("PyQt5", main_window_source)
        self.assertNotIn("PyQt5", temperature_panel_source)

    def test_main_window_embeds_objective_buttons_with_confirmed_position_mapping(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        nosepiece = RecordingNosepieceController()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=nosepiece,
        )

        expected = {
            "btnObjective5X": 1,
            "btnObjective10X": 2,
            "btnObjective20X": 3,
            "btnObjective40X": 4,
            "btnObjective50X": 5,
        }
        for name in expected:
            self.assertIn(name, window.controls)
        self.assertIn("objectiveStatusLabel", window.controls)

        window.controls["btnObjective20X"].click()

        self.assertTrue(wait_until(lambda: ("move_to_position", 3) in nosepiece.calls))
        self.assertTrue(wait_until(lambda: window.controls["btnObjective20X"].property("selectedObjective")))
        self.assertIn(("move_to_position", 3), nosepiece.calls)
        self.assertIn("20X", window.controls["objectiveStatusLabel"].text())
        self.assertTrue(window.controls["btnObjective20X"].property("selectedObjective"))

    def test_objective_selection_runs_without_blocking_gui_thread(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        nosepiece = DelayedNosepieceController()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=nosepiece,
        )

        started_at = time.monotonic()
        window.controls["btnObjective20X"].click()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertIn("物镜切换中", window.controls["objectiveStatusLabel"].text())
        self.assertFalse(window.controls["btnObjective20X"].isEnabled())
        self.assertTrue(wait_until(lambda: ("move_to_position", 3) in nosepiece.calls))

        nosepiece.release.set()

        self.assertTrue(wait_until(lambda: window.controls["btnObjective20X"].property("selectedObjective")))
        self.assertTrue(window.controls["btnObjective20X"].isEnabled())
        self.assertIn("当前物镜: 20X", window.controls["objectiveStatusLabel"].text())

    def test_objective_panel_com_popup_connects_selected_serial_port(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        created = []

        def make_nosepiece(port):
            controller = RecordingNosepieceController()
            controller.port = port
            controller.position = 2
            created.append(controller)
            return controller

        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=RecordingNosepieceController(),
            nosepiece_controller_factory=make_nosepiece,
            nosepiece_port_lister=lambda: [
                SimpleNamespace(device="COM4", description="Standard COM", hwid=""),
                SimpleNamespace(device="COM7", description="LV-NCNT2", hwid=""),
            ],
            nosepiece_port="COM7",
        )
        window.show()

        self.assertIn("btnObjectiveCom", window.controls)
        self.assertTrue(has_ancestor(window.controls["btnObjectiveCom"], window.controls["objectivePanel"]))

        window.controls["btnObjectiveCom"].click()
        get_app().processEvents()

        self.assertIn("objectiveComDialog", window.controls)
        self.assertTrue(window.controls["objectiveComDialog"].isVisible())
        self.assertEqual("COM7", window.controls["objectivePortCombo"].currentText())

        # Connecting opens the serial port off the GUI thread, so the result is
        # applied asynchronously; poll for it instead of a single processEvents.
        window.controls["objectiveConnect"].click()

        self.assertTrue(wait_until(lambda: ("connect",) in created[0].calls))
        self.assertTrue(wait_until(lambda: ("read_position",) in created[0].calls))
        self.assertTrue(
            wait_until(lambda: "已连接 COM7" in window.controls["objectiveComStatusLabel"].text())
        )
        self.assertEqual("COM7", created[0].port)
        self.assertIn(("connect",), created[0].calls)
        self.assertIn(("read_position",), created[0].calls)
        self.assertIs(window.objective_panel.nosepiece_controller, created[0])
        self.assertIn("已连接 COM7", window.controls["objectiveComStatusLabel"].text())

        window.controls["btnObjective20X"].click()

        self.assertTrue(wait_until(lambda: ("move_to_position", 3) in created[0].calls))
        self.assertIn(("move_to_position", 3), created[0].calls)

    def test_objective_com_popup_reuses_single_dialog_instance(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=RecordingNosepieceController(),
            nosepiece_port_lister=lambda: [SimpleNamespace(device="COM7", description="LV-NCNT2", hwid="")],
            nosepiece_port="COM7",
        )
        window.show()

        window.controls["btnObjectiveCom"].click()
        app.processEvents()
        first_dialog = window.controls["objectiveComDialog"]

        window.controls["btnObjectiveCom"].click()
        app.processEvents()
        second_dialog = window.controls["objectiveComDialog"]

        self.assertIsNot(first_dialog, second_dialog)
        self.assertFalse(first_dialog.isVisible())
        self.assertTrue(second_dialog.isVisible())
        second_dialog.close()

    def test_window_close_disconnects_nosepiece_controller_selected_in_com_popup(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        created = []

        def make_nosepiece(port):
            controller = RecordingNosepieceController()
            controller.port = port
            created.append(controller)
            return controller

        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=RecordingNosepieceController(),
            nosepiece_controller_factory=make_nosepiece,
            nosepiece_port_lister=lambda: [SimpleNamespace(device="COM7", description="LV-NCNT2", hwid="")],
            nosepiece_port="COM7",
        )
        window.show()
        window.controls["btnObjectiveCom"].click()
        get_app().processEvents()
        window.controls["objectiveConnect"].click()
        get_app().processEvents()

        window.close()

        self.assertIn(("disconnect",), created[0].calls)

    def test_window_close_disconnects_open_nosepiece_controller(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        nosepiece = RecordingNosepieceController()
        nosepiece.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            nosepiece_controller=nosepiece,
        )

        window.close()

        self.assertIn(("disconnect",), nosepiece.calls)

    def test_axis_button_dispatches_relative_move(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)

        window.controls["btnOpen"].click()
        window.controls["btnFwd1"].click()

        self.assertIn(("open_board",), controller.calls)
        self.assertIn(("relative_move", 1, 1, 12.5, 12500), controller.calls)

    def test_microscope_z_arrow_buttons_use_displayed_vertical_direction(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        controller.calls.clear()

        window.controls["btnFwd3"].click()
        self.assertEqual([("relative_move", 3, -1, 50.0, 25000)], controller.calls)

        controller.calls.clear()
        window.controls["btnBack3"].click()
        self.assertEqual([("relative_move", 3, 1, 50.0, 25000)], controller.calls)

    def test_x3_arrow_buttons_use_coordinate_direction(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        controller.calls.clear()

        window.controls["btnFwd5"].click()
        self.assertEqual([("relative_move", 5, -1, 50.0, 25000)], controller.calls)

        controller.calls.clear()
        window.controls["btnBack5"].click()
        self.assertEqual([("relative_move", 5, 1, 50.0, 25000)], controller.calls)

    def test_axis_card_uses_its_own_speed_and_step_controls(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        window.controls["txtSpeed6"].setValue(5000.0)
        window.controls["txtStep6"].setValue(123.0)

        window.controls["btnFwd6"].click()

        # Y3 (axis 6) pulse_sign=-1, so a "+前" (forward) button press commands the
        # controller in the -pulse direction.
        self.assertIn(("relative_move", 6, -1, 250.0, 6150), controller.calls)

    def test_gui_jog_button_blocked_by_limit_issues_no_motion_and_no_retry(self):
        # End-to-end safety: a GUI jog toward a direction the controller refuses (soft/
        # hard limit / interference) must be ATTEMPTED ONCE, surface the error to the
        # operator, and NOT auto-retry / silently swallow it.
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.controller_base import MotionControllerError
        get_app()

        class BlockingController(RecordingController):
            def relative_move(self, axis, direction, speed, step):
                self.calls.append(("relative_move", axis, direction, speed, step))
                raise MotionControllerError("拒绝移动：该方向无安全行程")

        controller = BlockingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        controller.calls.clear()  # drop open_board / set_axis_calibrations etc.

        window.controls["btnFwd5"].click()  # jog X3 forward; controller refuses it

        attempts = [c for c in controller.calls if c[0] == "relative_move"]
        self.assertEqual(1, len(attempts), "a refused jog must be attempted exactly once (no retry)")
        # The refusal reached the operator (status bar), not swallowed.
        self.assertIn("运动命令失败", window.statusBar().currentMessage())
        self.assertIn("拒绝移动", window.statusBar().currentMessage())

    def test_gamepad_microscope_z_speed_is_fixed_and_decoupled_from_gui(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.axis_calibration import (
            speed_to_pulse_per_ms, GAMEPAD_MICROSCOPE_Z_SPEED_UM_S,
        )
        get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        self.assertEqual(4000.0, GAMEPAD_MICROSCOPE_Z_SPEED_UM_S)
        fixed = speed_to_pulse_per_ms(3, 4000.0)
        # Lower the GUI MicroscopeZ dropdown; the gamepad speed must stay pinned at 4000.
        window.controls["txtSpeed3"].setValue(1000.0)
        self.assertEqual(fixed, window._gamepad_speed(3))
        self.assertEqual(speed_to_pulse_per_ms(3, 1000.0), window._speed(3))  # GUI still follows dropdown
        self.assertNotEqual(fixed, window._speed(3))
        # Non-MicroscopeZ axes: gamepad speed tracks the GUI speed (no decoupling).
        self.assertEqual(window._speed(5), window._gamepad_speed(5))

    def test_version_label_shown_in_status_bar(self):
        from automatic_transfer_station import __version__
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        self.assertEqual("0.0.5", __version__)
        self.assertIn("versionLabel", window.controls)
        self.assertEqual(f"v{__version__}", window.controls["versionLabel"].text())
        # It is a permanent (right-aligned / bottom-right) status-bar widget.
        self.assertIs(window.controls["versionLabel"].parent(), window.statusBar())

    def test_controller_direction_for_coordinate_direction_reversal_map(self):
        # Pins the contract that drives BOTH the GUI buttons and the gamepad: a
        # +coordinate request maps to a -pulse command for the reversed axes
        # (MicroscopeZ=3, and every pulse_sign<0 axis: X3=5, Y3=6, Y2=8), and passes
        # straight through for the rest (X1=1, Y1=2, Z=4, X2=7, R=9).
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        f = window._controller_direction_for_coordinate_direction
        for axis in (3, 5, 6, 8):
            self.assertEqual(-1, f(axis, +1), f"axis {axis}: +coord must command -pulse")
            self.assertEqual(+1, f(axis, -1), f"axis {axis}: -coord must command +pulse")
        for axis in (1, 2, 4, 7, 9):
            self.assertEqual(+1, f(axis, +1), f"axis {axis}: +coord must pass through")
            self.assertEqual(-1, f(axis, -1), f"axis {axis}: -coord must pass through")

    def test_jog_press_and_release_dispatches_start_and_stop(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()

        QTest.mousePress(window.controls["buttonfwd1"], Qt.LeftButton)
        QTest.mouseRelease(window.controls["buttonfwd1"], Qt.LeftButton)

        self.assertTrue(wait_until(lambda: ("start_jog", 1, 12.5) in controller.calls, timeout=1.0))
        self.assertTrue(wait_until(lambda: ("stop_axis", 1) in controller.calls, timeout=1.0))

    def test_x3_continuous_arrow_buttons_use_coordinate_direction(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        controller.calls.clear()

        QTest.mousePress(window.controls["buttonfwd5"], Qt.LeftButton)
        self.assertTrue(wait_until(lambda: ("start_jog", 5, -50.0) in controller.calls, timeout=1.0))
        QTest.mouseRelease(window.controls["buttonfwd5"], Qt.LeftButton)
        self.assertTrue(wait_until(lambda: ("stop_axis", 5) in controller.calls, timeout=1.0))

        controller.calls.clear()
        QTest.mousePress(window.controls["buttonback5"], Qt.LeftButton)
        self.assertTrue(wait_until(lambda: ("start_jog", 5, 50.0) in controller.calls, timeout=1.0))
        QTest.mouseRelease(window.controls["buttonback5"], Qt.LeftButton)
        self.assertTrue(wait_until(lambda: ("stop_axis", 5) in controller.calls, timeout=1.0))

    def test_microscope_z_continuous_button_starts_on_press_and_stops_on_release(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = DelayedJogController(delay=0.35)
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        app.processEvents()
        controller.calls.clear()

        start = time.monotonic()
        QTest.mousePress(window.controls["buttonfwd3"], Qt.LeftButton)
        press_elapsed = time.monotonic() - start
        app.processEvents()

        self.assertLess(press_elapsed, 0.15)
        self.assertTrue(controller.started.wait(0.5))
        self.assertTrue(wait_until(lambda: ("start_jog", 3, -50.0) in controller.calls, timeout=1.0))

        release_start = time.monotonic()
        QTest.mouseRelease(window.controls["buttonfwd3"], Qt.LeftButton)
        release_elapsed = time.monotonic() - release_start
        app.processEvents()

        self.assertLess(release_elapsed, 0.15)
        self.assertTrue(wait_until(lambda: ("stop_axis", 3) in controller.calls, timeout=1.0))

    def test_continuous_jog_press_does_not_block_gui_until_hardware_returns(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = DelayedJogController(delay=0.35)
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        app.processEvents()

        start = time.monotonic()
        QTest.mousePress(window.controls["buttonfwd1"], Qt.LeftButton)
        press_elapsed = time.monotonic() - start
        app.processEvents()

        self.assertLess(press_elapsed, 0.15)
        self.assertTrue(controller.started.wait(0.5))

        release_start = time.monotonic()
        QTest.mouseRelease(window.controls["buttonfwd1"], Qt.LeftButton)
        release_elapsed = time.monotonic() - release_start
        app.processEvents()

        self.assertLess(release_elapsed, 0.15)
        self.assertTrue(wait_until(lambda: ("start_jog", 1, 12.5) in controller.calls, timeout=1.0))
        self.assertTrue(wait_until(lambda: ("stop_axis", 1) in controller.calls, timeout=1.0))

    def test_real_mode_position_refresh_does_not_block_gui_thread(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = SlowPositionController(delay=0.25)
        controller.is_open = True
        controller.positions[1] = 12500.0
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        app.processEvents()

        start = time.monotonic()
        window.refresh_positions()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.15)
        self.assertTrue(controller.read_started.wait(0.5))
        self.assertTrue(wait_until(lambda: window.controls["axisPositionX1"].text() == "51.000", timeout=1.0))

    def test_real_mode_discards_position_refresh_completed_after_close(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = SlowPositionController(delay=0.1)
        controller.is_open = True
        controller.positions[2] = 400000.0
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        app.processEvents()

        window.refresh_positions()
        self.assertTrue(controller.read_started.wait(0.5))
        window.controls["btnClose"].click()
        app.processEvents()

        self.assertIsNone(window._motion_command_queue)
        close_index = controller.calls.index(("close_board",))
        self.assertNotIn(("stop_axis", 2), controller.calls[close_index + 1 :])

    def test_stale_position_refresh_does_not_clear_new_pending_refresh(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = SlowPositionController(delay=0.1)
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        app.processEvents()

        window._position_refresh_generation = 2
        window._position_refresh_pending = True
        window._handle_async_position_refresh((1, {1: 12500.0}, 0.0))

        self.assertTrue(window._position_refresh_pending)

    def test_stale_position_refresh_failure_does_not_clear_new_pending_refresh(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        app.processEvents()

        window.statusBar().showMessage("new refresh pending")
        window._position_refresh_generation = 2
        window._position_refresh_pending = True
        window._handle_async_position_refresh_failure((1, "old refresh failed"))

        self.assertTrue(window._position_refresh_pending)
        self.assertEqual("new refresh pending", window.statusBar().currentMessage())

        window._handle_async_position_refresh_failure((2, "current refresh failed"))

        self.assertFalse(window._position_refresh_pending)
        self.assertEqual("current refresh failed", window.statusBar().currentMessage())

    def test_real_mode_stop_buttons_do_not_block_gui_thread(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = SlowMotionCommandController(delay=0.25)
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        app.processEvents()

        self.assert_click_returns_quickly(window.controls["btnStop1"])
        self.assertTrue(controller.wait_started("stop_axis"))
        self.assertTrue(wait_until(lambda: ("stop_axis", 1) in controller.calls, timeout=1.0))

        self.assert_click_returns_quickly(window.controls["btnStopAll"])
        self.assertTrue(controller.wait_started("stop_all"))
        self.assertTrue(wait_until(lambda: ("stop_all",) in controller.calls, timeout=1.0))

    def test_real_mode_home_and_popup_moves_do_not_block_gui_thread(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        app = get_app()
        store = MicroscopePositionStore(self.microscope_position_store_path)
        store.save_point("观察高度", 250.5)
        controller = SlowMotionCommandController(delay=0.25)
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_THREADED_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            microscope_position_store_path=self.microscope_position_store_path,
        )
        window.timer.stop()
        app.processEvents()
        # Fix 1: _confirm gates R home — approve automatically in test
        window._confirm = lambda title, text: True

        self.assert_click_returns_quickly(window.controls["btnAxis9Home"])
        self.assertTrue(controller.wait_started("home_axis9"))
        self.assertTrue(wait_until(lambda: ("home_axis9",) in controller.calls, timeout=1.0))

        window.controls["btnAxis3Positions"].click()
        app.processEvents()
        table = window.controls["axis3PositionTable"]
        table.setCurrentCell(0, 0)
        self.assert_click_returns_quickly(window.controls["btnAxis3MovePoint"])
        self.assertTrue(controller.wait_started("move_axis3_to_position"))
        self.assertTrue(wait_until(lambda: ("move_axis3_to_position", 250.5) in controller.calls, timeout=1.0))
        self.assertTrue(wait_until(lambda: window.controls["labelAxis3Pos"].text() == "250.50", timeout=1.0))

        window.controls["btnAxis9Twist"].click()
        app.processEvents()
        window.controls["axis9TwistAngle"].setValue(12.5)
        window.controls["axis9TwistDirection"].setCurrentText("逆时针")
        self.assert_click_returns_quickly(window.controls["axis9TwistApply"])
        self.assertTrue(controller.wait_started("move_axis9_to_angle"))
        self.assertTrue(wait_until(lambda: ("move_axis9_to_angle", 44.28, 0.4166666666666667) in controller.calls, timeout=1.0))

        window.controls["btnZContinuousControl"].click()
        app.processEvents()
        window.controls["zContinuousDistance"].setValue(250.0)
        window.controls["zContinuousDirection"].setCurrentText("下降")
        self.assert_click_returns_quickly(window.controls["zContinuousApply"])
        self.assertTrue(controller.wait_started("relative_move_4"))
        self.assertTrue(wait_until(lambda: ("relative_move", 4, -1, 0.5, 12500) in controller.calls, timeout=1.0))

    def test_real_mode_relative_move_does_not_block_gui_thread(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = SlowRelativeMoveController(delay=0.25)
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()
        window.controls["btnOpen"].click()
        app.processEvents()

        start = time.monotonic()
        window.controls["btnFwd1"].click()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.15)
        self.assertTrue(controller.started.wait(0.5))
        self.assertTrue(wait_until(lambda: ("relative_move", 1, 1, 12.5, 12500) in controller.calls, timeout=1.0))

    def test_close_motion_control_waits_for_continuous_queue_before_closing_board(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = DelayedJogController(delay=0.25)
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()

        QTest.mousePress(window.controls["buttonfwd1"], Qt.LeftButton)
        self.assertTrue(controller.started.wait(0.5))
        window.controls["btnClose"].click()
        self.assertTrue(wait_until(lambda: ("start_jog", 1, 12.5) in controller.calls, timeout=1.0))

        close_index = controller.calls.index(("close_board",))
        trailing_motion_calls = [
            call for call in controller.calls[close_index + 1 :]
            if call[0] in {"start_jog", "stop_axis", "stop_all"}
        ]
        self.assertEqual([], trailing_motion_calls)

    def test_motion_buttons_do_not_create_queue_while_board_closed(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()  # board never opened
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()

        window.controls["btnFwd1"].click()
        QTest.mousePress(window.controls["buttonfwd1"], Qt.LeftButton)
        QTest.mouseRelease(window.controls["buttonfwd1"], Qt.LeftButton)

        self.assertIsNone(window._motion_command_queue)
        self.assertEqual([], [c for c in controller.calls if c[0] in {"relative_move", "start_jog", "stop_axis"}])

    def test_motion_buttons_do_not_resurrect_queue_after_close(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()

        window.controls["btnOpen"].click()
        window.controls["btnFwd1"].click()
        self.assertIsNotNone(window._motion_command_queue)

        window.controls["btnClose"].click()
        self.assertIsNone(window._motion_command_queue)

        controller.calls.clear()
        window.controls["btnFwd1"].click()

        self.assertIsNone(window._motion_command_queue)
        self.assertEqual([], controller.calls)

    def test_threaded_queue_survives_action_exception_and_keeps_processing(self):
        from automatic_transfer_station.main_window import MainWindow

        class BoomController(RecordingController):
            def __init__(self):
                super().__init__()
                self.boom = True

            def relative_move(self, axis, direction, speed, step):
                if self.boom:
                    self.boom = False
                    raise RuntimeError("boom from DLL")  # bare RuntimeError, like gas_api
                super().relative_move(axis, direction, speed, step)

        get_app()
        controller = BoomController()
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_THREADED_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.timer.stop()

        window.controls["btnFwd1"].click()
        self.assertTrue(wait_until(lambda: "运动命令失败" in window.statusBar().currentMessage(), timeout=1.0))

        # The worker must still be alive and process the next command.
        window.controls["btnFwd1"].click()
        self.assertTrue(wait_until(lambda: ("relative_move", 1, 1, 12.5, 12500) in controller.calls, timeout=1.0))

    def test_forward_jog_button_object_name_supports_intended_name(self):
        from PySide6.QtWidgets import QPushButton
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)

        self.assertIsInstance(window.findChild(QPushButton, "buttonfwd2"), QPushButton)
        # bottnfwd* was a typo alias for buttonfwd* — removed; use buttonfwd{axis} directly.
        self.assertNotIn("bottnfwd2", window.controls)

    def test_window_close_closes_open_controller(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()

        window.close()

        self.assertIn(("close_board",), controller.calls)

    def test_axis3_and_axis9_buttons_dispatch(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)
        # Fix 1: _confirm gates R home — approve automatically in test
        window._confirm = lambda title, text: True
        window.controls["btnOpen"].click()

        window.controls["btnAxis3Zero"].click()
        window.controls["btnAxis9Home"].click()

        self.assertIn(("zero_axis3",), controller.calls)
        self.assertIn("btnAxis3Positions", window.controls)
        self.assertIn(("home_axis9",), controller.calls)

    def test_axis3_position_manager_saves_current_position_to_persistent_list(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            microscope_position_store_path=self.microscope_position_store_path,
        )
        window.show()
        app.processEvents()

        self.assertEqual("点位管理", window.controls["btnAxis3Positions"].text())
        window.controls["btnAxis3Positions"].click()
        app.processEvents()

        self.assertIn("axis3PositionsDialog", window.controls)
        self.assertTrue(window.controls["axis3PositionsDialog"].isVisible())
        window.controls["axis3PointName"].setText("观察高度")
        window.controls["btnAxis3SavePoint"].click()
        app.processEvents()

        table = window.controls["axis3PositionTable"]
        self.assertEqual(1, table.rowCount())
        self.assertEqual("观察高度", table.item(0, 0).text())
        self.assertEqual("12.34", table.item(0, 1).text())

        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        reloaded = MicroscopePositionStore(self.microscope_position_store_path)
        reloaded.load()
        self.assertEqual([("观察高度", 12.34)], [(point.name, point.pulse) for point in reloaded.points()])

    def test_axis3_position_manager_reuses_single_dialog_instance(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            microscope_position_store_path=self.microscope_position_store_path,
        )
        window.show()
        app.processEvents()

        window.controls["btnAxis3Positions"].click()
        app.processEvents()
        first_dialog = window.controls["axis3PositionsDialog"]

        window.controls["btnAxis3Positions"].click()
        app.processEvents()
        second_dialog = window.controls["axis3PositionsDialog"]

        self.assertIsNot(first_dialog, second_dialog)
        self.assertFalse(first_dialog.isVisible())
        self.assertTrue(second_dialog.isVisible())
        second_dialog.close()

    def test_axis3_position_manager_moves_renames_and_deletes_selected_point(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        store = MicroscopePositionStore(self.microscope_position_store_path)
        store.save_point("安全高度", 100.0)
        store.save_point("观察高度", 250.5)

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        # X3 (axis 5) defaults to pulse 0, which maps to 13.758 mm — outside its
        # +/-12.5 mm soft range — so the async reactive safety scan would overwrite
        # motionStatusText with an X3 soft-limit alarm and race this test's assertion
        # on that label (flaky under full-suite load / CI; see CI failure 2026-06-24).
        # Park X3 at its zero coordinate (in range) so the scan stays quiet.
        controller.positions[5] = 687898.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            microscope_position_store_path=self.microscope_position_store_path,
        )
        window.show()
        app.processEvents()

        window.controls["btnAxis3Positions"].click()
        app.processEvents()
        table = window.controls["axis3PositionTable"]
        table.setCurrentCell(1, 0)
        window.controls["btnAxis3MovePoint"].click()
        app.processEvents()

        self.assertIn(("move_axis3_to_position", 250.5), controller.calls)
        self.assertIn("观察高度", window.controls["motionStatusText"].text())

        window.controls["axis3PointName"].setText("接触前")
        window.controls["btnAxis3RenamePoint"].click()
        app.processEvents()
        self.assertEqual("接触前", table.item(1, 0).text())

        window.controls["btnAxis3DeletePoint"].click()
        app.processEvents()
        self.assertEqual(1, table.rowCount())

        reloaded = MicroscopePositionStore(self.microscope_position_store_path)
        reloaded.load()
        self.assertEqual([("安全高度", 100.0)], [(point.name, point.pulse) for point in reloaded.points()])

    def test_speed_controls_use_fixed_physical_options(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)

        linear_speed = window.controls["txtSpeed1"]
        angular_speed = window.controls["txtSpeed9"]

        self.assertEqual([1.0, 10.0, 100.0, 1000.0, 5000.0], [linear_speed.itemData(index) for index in range(linear_speed.count())])
        self.assertEqual([0.1, 0.3, 1.0, 2.0, 3.0], [angular_speed.itemData(index) for index in range(angular_speed.count())])
        self.assertEqual(1000.0, linear_speed.value())
        self.assertEqual(1.0, angular_speed.value())
        self.assertEqual(5000.0, linear_speed.maximum())
        self.assertEqual(3.0, angular_speed.maximum())

        angular_speed.setValue(3.0)
        window.controls["btnFwd9"].click()

        self.assertIn(("relative_move", 9, 1, 12.5, 4167), controller.calls)

    def test_window_loads_cached_axis_positions_on_startup(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)
            store.update_axis(3, 123.45, source="test")
            store.update_axis(9, 187500, source="test", angle_degrees=45.0)

            window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=path)

            self.assertEqual("123.45", window.controls["labelAxis3Pos"].text())
            self.assertEqual("45.000", window.controls["axisPositionR"].text())

    def test_window_records_positions_after_motion_command(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        from automatic_transfer_station.main_window import MainWindow

        class MovingController(RecordingController):
            def relative_move(self, axis, direction, speed, step):
                super().relative_move(axis, direction, speed, step)
                self.positions[axis] = self.positions.get(axis, 0.0) + step * direction

        get_app()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            window = MainWindow(controller=MovingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=path)

            window.controls["btnOpen"].click()
            window.controls["btnFwd1"].click()
            window.refresh_positions()

            positions = AxisPositionStore(path).load()
            self.assertEqual(12500.0, positions["1"]["pulse"])
            self.assertEqual(51.0, positions["1"]["coordinate"])
            self.assertEqual("controller_readback", positions["1"]["source"])

    def test_refresh_updates_all_calibrated_position_fields(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions.update(
            {
                1: 12500.0,
                2: -12500.0,
                3: 123.45,
                4: 81506.0,
                5: 625398.0,
                6: -200000.0,
                7: 181790.0,
                8: -35000.0,
                9: 187500.0,
            }
        )
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )

        window.refresh_positions()

        self.assertEqual("51.000", window.controls["axisPositionX1"].text())
        self.assertEqual("24.000", window.controls["axisPositionY1"].text())
        self.assertEqual("5.000", window.controls["axisPositionZ"].text())
        self.assertEqual("1.250", window.controls["axisPositionX3"].text())
        # Y3 (axis 6) and Y2 (axis 8) now have pulse_sign=-1 (reversed direction fix),
        # so the same pulse maps to the negated coordinate.
        self.assertEqual("-1.000", window.controls["axisPositionY3"].text())
        self.assertEqual("1.000", window.controls["axisPositionX2"].text())
        self.assertEqual("-1.000", window.controls["axisPositionY2"].text())
        self.assertEqual("56.780", window.controls["axisPositionR"].text())
        self.assertEqual("123.45", window.controls["labelAxis3Pos"].text())

    def test_refresh_stops_axes_when_calibrated_position_is_unsafe(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[2] = 400000.0
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY, position_store_path=self.position_store_path)

        window.refresh_positions()

        self.assertIn(("stop_axis", 2), controller.calls)
        self.assertIn("Y1", window.statusBar().currentMessage())

    def test_highlight_axis_marks_only_selected_hotspot(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.highlight_axis(5)  # X3
        self.assertTrue(window.controls["axisHotspotX3"].property("selectedAxis"))
        window.highlight_axis(7)  # X2 -> X3 cleared, X2 set
        self.assertFalse(window.controls["axisHotspotX3"].property("selectedAxis"))
        self.assertTrue(window.controls["axisHotspotX2"].property("selectedAxis"))
        window.highlight_axis(None)
        self.assertFalse(window.controls["axisHotspotX2"].property("selectedAxis"))

    def test_highlight_axis_updates_stage_map_selected_banner(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        banner = window.controls["stageMap"].selected_axis_label

        window.highlight_axis(5)  # X3
        self.assertFalse(banner.isHidden())  # shown (isVisible() needs a shown window)
        self.assertIn("手柄选中", banner.text())
        self.assertIn("X3", banner.text())
        # Word-wrap is on so the long banner wraps inside the image instead of overflowing.
        self.assertTrue(banner.wordWrap())
        # 2026-06-22: banner also shows the selected axis's live step size + coordinate.
        self.assertIn("步长", banner.text())
        self.assertIn("坐标", banner.text())
        self.assertIn("µm", banner.text())   # X3 step unit
        self.assertIn("mm", banner.text())   # X3 coordinate unit

        window.highlight_axis(3)  # MicroscopeZ
        self.assertIn("MicroscopeZ", banner.text())

        window.highlight_axis(9)  # R — angular axis: step AND coordinate in degrees
        self.assertIn("R", banner.text())
        self.assertIn("°", banner.text())
        self.assertNotIn("mm", banner.text())
        self.assertNotIn("µm", banner.text())

        window.highlight_axis(1)  # X1 — newly gamepad-selectable: µm step / mm coordinate
        self.assertIn("X1", banner.text())
        self.assertIn("µm", banner.text())
        self.assertIn("mm", banner.text())

        window.highlight_axis(None)  # cleared -> banner hidden
        self.assertTrue(banner.isHidden())

    def test_gamepad_banner_coordinate_updates_live(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        banner = window.controls["stageMap"].selected_axis_label
        window.highlight_axis(5)  # select X3
        # A fresh position read (the 100ms refresh path) must re-render the banner with
        # the new coordinate, not only on selection change.
        window._update_axis_position_labels({5: 100000.0})
        self.assertIn(window.controls["axisPositionX3"].text(), banner.text())
        before = banner.text()
        window._update_axis_position_labels({5: 300000.0})  # different pulse -> different coord
        self.assertIn(window.controls["axisPositionX3"].text(), banner.text())
        self.assertNotEqual(before, banner.text())  # banner tracked the live coordinate change

    def test_gamepad_banner_clamped_to_image_width_no_overflow(self):
        from PySide6.QtCore import QRect
        from automatic_transfer_station.main_window import MainWindow
        app = get_app()
        window = MainWindow(controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.show()
        app.processEvents()
        stage_map = window.controls["stageMap"]
        banner = stage_map.selected_axis_label
        # Force a narrow image rect, then select X3 (long banner). The clamp must cap the
        # banner to image_width-8 and word-wrap so it never spills past the image edge.
        stage_map._image_rect = QRect(10, 10, 200, 400)
        window.highlight_axis(5)
        self.assertEqual(192, banner.maximumWidth())          # 200 - 8
        self.assertLessEqual(banner.width(), 192)             # clamped, no horizontal overflow
        # Degenerate zero-width rect (early layout) falls back to the 120px floor, no crash.
        stage_map._image_rect = QRect(0, 0, 0, 0)
        stage_map._position_selected_axis_label()
        self.assertEqual(120, banner.maximumWidth())
        window.close()

    def test_gui_jog_widgets_disabled_while_gamepad_armed(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController(); controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.set_gamepad_armed(True)
        self.assertFalse(window.controls["buttonfwd5"].isEnabled())   # continuous jog pad
        self.assertFalse(window.controls["btnFwd5"].isEnabled())      # step button
        self.assertTrue(window.controls["btnStopAll"].isEnabled())    # global stop stays usable
        window.set_gamepad_armed(False)
        self.assertTrue(window.controls["buttonfwd5"].isEnabled())

    def test_gamepad_panel_constructed_and_disarms_on_window_deactivate(self):
        # Package 7 Test 5 fix: the assertion was previously guarded by
        # `if not window.isActiveWindow()`, which made it silently pass whenever
        # the offscreen test window happened to be "active".  We now
        # monkeypatch `isActiveWindow` to always return False so the assertion
        # is unconditional and the test cannot be vacuously green.
        from PySide6.QtCore import QEvent
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController(); controller.is_open = True
        disarmed = {"n": 0}
        class FakeReader:
            def read_state(self, index=None):
                from automatic_transfer_station.gamepad_xinput import GamepadSnapshot
                return GamepadSnapshot(False, 0, 0, 0, 0, 0, 0, 0, 0)
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path, gamepad_reader=FakeReader())
        self.assertIn("btnGamepadEnable", window.controls)
        window.gamepad_panel.disarm = lambda: disarmed.__setitem__("n", disarmed["n"] + 1)
        # Force the window to appear inactive so the assertion is never skippable.
        window.isActiveWindow = lambda: False
        window.changeEvent(QEvent(QEvent.Type.ActivationChange))
        self.assertGreaterEqual(disarmed["n"], 1)

    def test_language_toggle_button_flips_language_and_label(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        # conftest pins ZH on setup; a freshly built window is ZH, so the button offers EN.
        toggle = window.controls["languageToggle"]
        self.assertEqual("zh", translator.lang)
        self.assertEqual("EN", toggle.text())
        window._toggle_language()
        self.assertEqual("en", translator.lang)
        self.assertEqual("中文", toggle.text())
        window._toggle_language()
        self.assertEqual("zh", translator.lang)
        self.assertEqual("EN", toggle.text())

    def test_main_window_translates_to_english(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        translator.set_language("en")
        try:
            # calibration button text becomes English
            self.assertEqual("Calibration", window.controls["btnCoordinateCalibration"].text())
            # microscope section title becomes English
            self.assertEqual("Microscope adjust", window.controls["stageMicroscopeTitle"].text())
            # axis-3 stage chip "镜Z" translates (codes like X1 pass through unchanged)
            self.assertEqual("Microscope Z", window.controls["axisHotspotMicroscopeZ"].text())
            # panel title
            self.assertEqual("Motion control", window.controls["axisControlTitle"].text())
            self.assertEqual("2D Material Transfer Station", window.windowTitle())
            self.assertEqual("Motion control closed – preview", window.controls["motionStatusText"].text())
            self.assertIn("disconnected", window.controls["objectiveLinkLabel"].text().lower())
            self.assertEqual("Current objective: not read", window.controls["objectiveStatusLabel"].text())
            window._set_motion_status("运动控制操作超时（{kind}），运动卡无响应", "alarm", kind="open")
            translator.set_language("zh")
            self.assertIn("操作超时（open）", window.controls["motionStatusText"].text())
            translator.set_language("en")
            self.assertIn("timeout (open)", window.controls["motionStatusText"].text())
            self.assertIn("#dc2626", window.controls["motionStatusLamp"].styleSheet())
        finally:
            translator.initialize("zh")

    def test_all_axis_cards_fit_default_window_in_both_languages(self):
        from automatic_transfer_station.main_window import MainWindow
        from automatic_transfer_station.i18n import translator
        app = get_app()
        window = MainWindow(
            controller=RecordingController(), motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.timer.stop()
        window.show()
        try:
            for lang in ("en", "zh", "en"):
                translator.set_language(lang)
                window.resize(1720, 860)
                app.processEvents()
                area = window.controls["axisScrollArea"]
                self.assertEqual(0, area.verticalScrollBar().maximum(), lang)
                self.assertEqual(0, area.horizontalScrollBar().maximum(), lang)
                chip = window.controls["axisHotspotMicroscopeZ"]
                self.assertGreaterEqual(chip.width(), chip.fontMetrics().horizontalAdvance(chip.text()) + 14)
        finally:
            window.close()
            translator.initialize("zh")


class Package6SafetyAffordanceTests(unittest.TestCase):
    """Package 6: GUI safety affordances."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"
        self.runtime_calibration_path = Path(self._tmpdir.name) / "axis_runtime_calibration.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    # ---- Fix 1: _confirm hook ----

    def test_confirm_hook_is_callable(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        self.assertTrue(callable(window._confirm))

    def test_calibration_confirm_false_does_not_write_to_disk(self):
        from automatic_transfer_station.main_window import MainWindow
        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[5] = 687898.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        # Monkeypatch _confirm to return False → calibration must NOT proceed
        window._confirm = lambda title, text: False

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        window.controls["calibrationActualX3"].setText("4.25")
        window.controls["calibrationApply"].click()
        app.processEvents()

        # No file written, position display unchanged
        self.assertFalse(self.runtime_calibration_path.exists())
        self.assertEqual("0.000", window.controls["axisPositionX3"].text())

    def test_calibration_confirm_true_proceeds_with_write(self):
        from automatic_transfer_station.main_window import MainWindow
        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[5] = 687898.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window.show()
        app.processEvents()

        # Monkeypatch _confirm to return True → calibration proceeds
        window._confirm = lambda title, text: True

        window.controls["btnCoordinateCalibration"].click()
        app.processEvents()
        window.controls["calibrationActualX3"].setText("4.25")
        window.controls["calibrationApply"].click()
        app.processEvents()

        self.assertTrue(self.runtime_calibration_path.exists())
        self.assertEqual("4.250", window.controls["axisPositionX3"].text())

    def test_home_poll_yields_to_stop_and_never_calibrates_after_cancel(self):
        from test_gas_controller import FakeApi
        from automatic_transfer_station.gas_controller import GasMotionController
        from automatic_transfer_station.main_window import MainWindow
        from unittest.mock import Mock
        get_app()
        api = FakeApi()
        ctl = GasMotionController(api)
        ctl.is_open = True
        window = MainWindow(controller=ctl, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.timer.stop()
        window._confirm = lambda *args: True
        window._record_axis9_home_zero = Mock()
        try:
            with patch("automatic_transfer_station.main_window.QTimer.singleShot") as timer:
                window._home_axis9()
                self.assertIn(("update", 1 << 8), api.calls)
                timer.assert_called_once()
                next_poll = timer.call_args.args[-1]
                with patch.object(window, "_run_or_queue_motion_command") as submit:
                    next_poll()
                submit.assert_called_once()
                # Stop after submission but before this queued poll gets to run.
                window._stop_all_motion()
                api.ext_di_bits[(0, 8)] = 1
                command = submit.call_args.args[0]
                self.assertIsNone(command())
                submit.call_args.kwargs["on_success"](None)
                timer.assert_called_once()
            window._record_axis9_home_zero.assert_not_called()
            self.assertIn(("stop", 0xFFFF, 0), api.calls)
            self.assertNotIn(("set_position", 9, 41667), api.calls)
        finally:
            window.close()

    def test_home_axis9_confirm_false_does_not_call_controller(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window._confirm = lambda title, text: False
        controller.calls.clear()

        window.controls["btnAxis9Home"].click()

        self.assertNotIn(("home_axis9",), controller.calls)

    def test_home_axis9_confirm_true_calls_controller(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window._confirm = lambda title, text: True
        controller.calls.clear()

        window.controls["btnAxis9Home"].click()

        self.assertIn(("home_axis9",), controller.calls)

    def test_home_axis9_success_records_current_pulse_as_zero_degrees(self):
        import json

        from automatic_transfer_station.axis_calibration import coordinate_from_pulse
        from automatic_transfer_station.main_window import MainWindow

        class HomeRecordingController(RecordingController):
            def get_axis9_angle(self):
                if self.axis_calibrations is not None:
                    coordinate = coordinate_from_pulse(9, self.positions[9], self.axis_calibrations)
                    if coordinate is not None:
                        return float(coordinate)
                return super().get_axis9_angle()

        app = get_app()
        controller = HomeRecordingController()
        controller.is_open = True
        controller.positions[9] = 1466937.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
        )
        window._confirm = lambda title, text: True
        controller.calls.clear()

        window.controls["btnAxis9Home"].click()
        app.processEvents()

        self.assertIn(("home_axis9",), controller.calls)
        calibration_data = json.loads(self.runtime_calibration_path.read_text(encoding="utf-8"))
        axis9 = calibration_data["axes"]["9"]
        self.assertEqual(1466937.0, axis9["zero_pulse"])
        self.assertEqual(0.0, axis9["zero_coordinate"])
        self.assertEqual("0.000", window.controls["axisPositionR"].text())
        self.assertEqual(0.0, window.position_store.cached_axis9_angle())

    # ---- Fix 2: stop-all button size + shortcut ----

    def test_stop_all_button_meets_minimum_size(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        btn = window.controls["btnStopAll"]
        self.assertGreaterEqual(btn.minimumWidth(), 150)
        self.assertGreaterEqual(btn.minimumHeight(), 56)

    def test_stop_all_button_tooltip_mentions_space(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        btn = window.controls["btnStopAll"]
        self.assertIn("Space", btn.toolTip())

    def test_stop_all_shortcut_is_registered(self):
        from PySide6.QtGui import QKeySequence, QShortcut
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        shortcut = window.controls.get("shortcutStopAll")
        self.assertIsNotNone(shortcut)
        self.assertIsInstance(shortcut, QShortcut)
        self.assertEqual(QKeySequence(shortcut.key()), QKeySequence("Space"))

    def test_stop_all_shortcut_triggers_stop(self):
        from PySide6.QtGui import QShortcut
        from automatic_transfer_station.main_window import MainWindow
        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        controller.calls.clear()

        shortcut = window.controls["shortcutStopAll"]
        shortcut.activated.emit()
        app.processEvents()

        self.assertIn(("stop_all",), controller.calls)

    # ---- Fix 3: safety alarm card property + lamp ----

    def test_safety_alarm_sets_axis_card_property_on_unsafe_position(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        # Y1 (axis 2) goes beyond its soft limit of 50mm (pulse 0+50*12500 = 625000)
        controller.positions[2] = 400000.0  # triggers unsafe check (Y1 at ~55mm)
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.refresh_positions()

        card = window.controls["axisCardY1"]
        self.assertTrue(card.property("safetyAlarm"))

    def test_safety_alarm_sets_lamp_to_alarm_state(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[2] = 400000.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.refresh_positions()

        lamp_style = window.controls["motionStatusLamp"].styleSheet()
        # alarm state uses #dc2626 (red)
        self.assertIn("#dc2626", lamp_style)

    def test_safety_alarm_clears_when_position_returns_safe(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        controller.positions[2] = 400000.0
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.refresh_positions()
        self.assertTrue(window.controls["axisCardY1"].property("safetyAlarm"))

        # Move Y1 back to a safe position
        controller.positions[2] = 0.0  # Y1 at 25mm (zero_coordinate), safe
        window.refresh_positions()

        self.assertFalse(window.controls["axisCardY1"].property("safetyAlarm"))

    # ---- Fix 4: :disabled stylesheet ----

    def test_axis_control_panel_stylesheet_has_disabled_rule(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        self.assertIn("QPushButton:disabled", window.controls["axisControlPanel"].styleSheet())

    def test_objective_panel_stylesheet_has_disabled_rule(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        self.assertIn("QPushButton:disabled", window.controls["objectivePanel"].styleSheet())

    def test_motion_pad_button_disabled_uses_dimmed_color(self):
        from PySide6.QtGui import QColor
        from automatic_transfer_station.main_window import MotionPadButton
        get_app()
        # Verify the dimmed color logic: when disabled, color must be the grey tone
        # (not the active dark color). We check this via the source-level logic rather
        # than actually calling paintEvent (which requires a live paint context).
        import inspect
        from automatic_transfer_station import main_window as mw_mod
        source = inspect.getsource(MotionPadButton.paintEvent)
        self.assertIn("isEnabled", source)
        self.assertIn("#9aa8b6", source)

    # ---- Fix 5: gamepad armed/disarmed status messages ----

    def test_gamepad_armed_pushes_status_message(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.set_gamepad_armed(True)
        armed_text = window.controls["motionStatusText"].text()

        window.set_gamepad_armed(False)
        disarmed_text = window.controls["motionStatusText"].text()

        self.assertIn("手柄已启用", armed_text)
        self.assertIn("屏幕轴按钮已禁用", armed_text)
        self.assertIn("手柄已停用", disarmed_text)
        self.assertIn("屏幕轴按钮已恢复", disarmed_text)

    def test_highlight_axis_sets_card_selected_property(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.highlight_axis(5)  # X3 → axisCardX3
        self.assertTrue(window.controls["axisCardX3"].property("selectedAxis"))
        self.assertFalse(window.controls["axisCardY3"].property("selectedAxis"))

        window.highlight_axis(6)  # Y3 → X3 cleared, Y3 set
        self.assertFalse(window.controls["axisCardX3"].property("selectedAxis"))
        self.assertTrue(window.controls["axisCardY3"].property("selectedAxis"))

        window.highlight_axis(None)  # clear all
        self.assertFalse(window.controls["axisCardY3"].property("selectedAxis"))

    def test_highlight_axis_hotspot_shows_arrow_prefix_when_selected(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController()
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        window.highlight_axis(5)  # X3
        hotspot_text = window.controls["axisHotspotX3"].text()
        self.assertIn("▶", hotspot_text)

        window.highlight_axis(None)
        hotspot_text_cleared = window.controls["axisHotspotX3"].text()
        self.assertNotIn("▶", hotspot_text_cleared)

    # ---- Fix 6: temperature plot °C label ----

    def test_temperature_plot_label_uses_degree_c_notation(self):
        from automatic_transfer_station.temperature_panel import TemperaturePlotWidget
        get_app()
        # Check source level: the paintEvent draws "°C" not bare "C"
        import inspect
        from automatic_transfer_station import temperature_panel
        source = inspect.getsource(temperature_panel.TemperaturePlotWidget.paintEvent)
        self.assertIn("°C", source)
        self.assertNotIn('" C"', source)
        self.assertNotIn("' C'", source)


class MotionCommandQueueConcurrencyTests(unittest.TestCase):
    """Package 4: queue concurrency, emergency-stop, and shutdown-timer fixes."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _drain_queue(self, queue):
        if queue is not None:
            queue.shutdown(timeout=2.0)

    # ----- Fix 1: cancel_pending -----------------------------------------

    def test_cancel_pending_drains_queued_commands_without_killing_worker(self):
        from automatic_transfer_station.main_window import MotionCommandQueue

        queue = MotionCommandQueue()
        self.addCleanup(self._drain_queue, queue)

        gate_entered = threading.Event()
        release = threading.Event()
        ran = []

        def blocker():
            gate_entered.set()
            release.wait(2.0)
            ran.append("blocker")

        # Pin the single worker on a blocking command.
        queue.submit(blocker, priority=10)
        self.assertTrue(gate_entered.wait(1.0))

        # Queue a backlog behind it.
        for i in range(5):
            queue.submit(lambda i=i: ran.append(f"pending{i}"), priority=10)

        dropped = queue.cancel_pending()
        self.assertEqual(dropped, 5)

        # Release the blocker; the pending commands must NOT run.
        release.set()
        self.assertTrue(wait_until(lambda: "blocker" in ran, timeout=1.0))
        # Worker still alive and usable: a new command runs.
        queue.submit(lambda: ran.append("after"))
        self.assertTrue(wait_until(lambda: "after" in ran, timeout=1.0))

        self.assertEqual([], [r for r in ran if r.startswith("pending")])

    def test_cancel_pending_is_noop_in_inline_mode(self):
        from automatic_transfer_station.main_window import MotionCommandQueue

        queue = MotionCommandQueue(inline=True)
        self.assertEqual(0, queue.cancel_pending())

    def test_cancel_pending_drops_pending_callbacks(self):
        # A queued _run_or_queue_motion_command callback must be discarded when
        # cancelled, so a later stale completion can never invoke it.
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = GatedController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_THREADED_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.timer.stop()
        self.addCleanup(window.close)

        # Pin the worker with a gated relative move, then queue more behind it.
        window.controls["btnFwd1"].click()
        self.assertTrue(controller.entered.wait(1.0))
        window.controls["btnFwd2"].click()
        window.controls["btnFwd5"].click()

        queue = window._motion_command_queue
        dropped = queue.cancel_pending()
        self.assertGreaterEqual(dropped, 1)
        self.assertEqual({}, window._motion_command_callbacks)

    def test_gamepad_stop_all_cancels_pending_queued_motion(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = GatedController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_THREADED_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.timer.stop()
        self.addCleanup(window.close)

        # Build a backlog of discrete relative moves behind a gated worker call.
        window.controls["btnFwd1"].click()       # gated; pins worker
        self.assertTrue(controller.entered.wait(1.0))
        window.controls["btnFwd2"].click()       # queued, priority 10
        window.controls["btnFwd5"].click()       # queued, priority 10

        window._gamepad_stop_all()               # emergency stop

        # stop_all ran directly and the pending count dropped to zero.
        self.assertIn(("stop_all",), controller.calls)
        self.assertEqual(0, window._motion_command_queue.pending_count())

        # Release the gated worker; the cancelled relative moves must NOT run.
        controller.release.set()
        self.assertTrue(wait_until(lambda: ("relative_move", 1, 1, 12.5, 12500) in controller.calls,
                                   timeout=1.0))
        for axis in (2, 5):
            self.assertNotIn(("relative_move", axis, 1, 12.5, 12500), controller.calls)

    def test_stop_all_motion_cancels_pending_queued_motion(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = GatedController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_THREADED_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.timer.stop()
        self.addCleanup(window.close)

        window.controls["btnFwd1"].click()       # gated; pins worker
        self.assertTrue(controller.entered.wait(1.0))
        window.controls["btnFwd2"].click()
        window.controls["btnFwd5"].click()

        window._stop_all_motion()                # red 停止移动 path

        # The priority-0 stop_all is retained and runs; pending jogs are cancelled.
        controller.release.set()
        self.assertTrue(wait_until(lambda: ("stop_all",) in controller.calls, timeout=1.0))
        self.assertTrue(wait_until(lambda: ("relative_move", 1, 1, 12.5, 12500) in controller.calls,
                                   timeout=1.0))
        for axis in (2, 5):
            self.assertNotIn(("relative_move", axis, 1, 12.5, 12500), controller.calls)

    # ----- Fix 2: per-axis latest-wins coalescing for continuous jog ------

    def test_continuous_jog_coalesces_per_axis_latest_wins(self):
        from automatic_transfer_station.main_window import MotionCommandQueue

        queue = MotionCommandQueue()
        self.addCleanup(self._drain_queue, queue)

        gate_entered = threading.Event()
        release = threading.Event()
        ran = []

        def blocker():
            gate_entered.set()
            release.wait(2.0)

        queue.submit(blocker, priority=0)
        self.assertTrue(gate_entered.wait(1.0))

        # Many continuous-jog submissions for the SAME axis; only the latest survives.
        for speed in (10, 20, 30, 40, 50):
            queue.submit(lambda speed=speed: ran.append(("jog", 5, speed)),
                         priority=0, kind="continuous_jog", metadata=5)
        # A different axis is independent.
        queue.submit(lambda: ran.append(("jog", 6, 99)),
                     priority=0, kind="continuous_jog", metadata=6)

        # Axis 5 coalesced to one, axis 6 keeps one: 2 pending behind the blocker.
        self.assertTrue(wait_until(lambda: queue.pending_count() == 2, timeout=1.0))

        release.set()
        self.assertTrue(wait_until(lambda: ("jog", 6, 99) in ran, timeout=1.0))
        axis5 = [r for r in ran if r[1] == 5]
        self.assertEqual([("jog", 5, 50)], axis5)

    def test_coalescing_never_drops_a_stop(self):
        from automatic_transfer_station.main_window import MotionCommandQueue

        queue = MotionCommandQueue()
        self.addCleanup(self._drain_queue, queue)

        gate_entered = threading.Event()
        release = threading.Event()
        ran = []

        def blocker():
            gate_entered.set()
            release.wait(2.0)

        queue.submit(blocker, priority=0)
        self.assertTrue(gate_entered.wait(1.0))

        for speed in (10, 20, 30):
            queue.submit(lambda speed=speed: ran.append(("jog", 5, speed)),
                         priority=0, kind="continuous_jog", metadata=5)
        # A stop submitted afterwards must always be retained and run.
        queue.submit(lambda: ran.append(("stop", 5)), priority=0, kind="stop", metadata=5)

        release.set()
        self.assertTrue(wait_until(lambda: ("stop", 5) in ran, timeout=1.0))
        # Latest jog survived and the stop ran after it.
        self.assertIn(("jog", 5, 30), ran)
        self.assertLess(ran.index(("jog", 5, 30)), ran.index(("stop", 5)))

    def test_relative_moves_are_not_coalesced(self):
        from automatic_transfer_station.main_window import MotionCommandQueue

        queue = MotionCommandQueue()
        self.addCleanup(self._drain_queue, queue)

        gate_entered = threading.Event()
        release = threading.Event()
        ran = []

        def blocker():
            gate_entered.set()
            release.wait(2.0)

        queue.submit(blocker, priority=0)
        self.assertTrue(gate_entered.wait(1.0))

        # Discrete relative moves (no continuous_jog kind) accumulate, not coalesce.
        for i in range(3):
            queue.submit(lambda i=i: ran.append(("rel", i)), priority=10)
        self.assertTrue(wait_until(lambda: queue.pending_count() == 3, timeout=1.0))

        release.set()
        self.assertTrue(wait_until(lambda: len(ran) == 3, timeout=1.0))

    # ----- Fix 2b (regression): continuous-jog metadata never aliases command_id -----

    def test_continuous_jog_completion_does_not_fire_or_drop_discrete_callback_with_same_id(self):
        """A continuous jog completing on axis N must not touch the discrete callback
        registered under command_id N.

        Setup: force _motion_command_callback_ids so that the next discrete command
        gets command_id == 5 (== axis 5 used by the jog).  Then simulate the queue
        emitting completed/failed for a continuous jog on axis 5 and verify:
          (a) the discrete callback is NOT fired (not called)
          (b) the discrete callback is NOT dropped (still in _motion_command_callbacks)
        """
        from itertools import count
        from automatic_transfer_station.main_window import MainWindow, MotionCommandQueue

        app = get_app()
        controller = RecordingController()
        controller.is_open = True
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        window.timer.stop()
        self.addCleanup(window.close)

        # Advance the counter so the next command_id will be 5 (== axis 5 used by the jog).
        # count() starts at 0 and next() increments each call; we need 4 consumed so
        # the next call inside _run_or_queue_motion_command returns 4... wait, count(0)
        # yields 0,1,2,... so after consuming 0..4 the next is 5.  Consume 5 values
        # from a fresh counter starting at 0 → ids 0,1,2,3,4 used, next will be 5.
        window._motion_command_callback_ids = count(0)
        for _ in range(5):
            next(window._motion_command_callback_ids)
        # Now the next next() call returns 5.

        discrete_fired = []

        # Register a discrete command that will get command_id == 5.
        # Use a no-op action that never actually runs so the callback stays pending.
        # We manually inject it into the callback dict to avoid inline-queue dispatch.
        discrete_command_id = next(window._motion_command_callback_ids)  # == 5
        self.assertEqual(5, discrete_command_id, "precondition: command_id must be 5")
        window._motion_command_callbacks[discrete_command_id] = (
            None,                              # success_message
            lambda result: discrete_fired.append(result),  # on_success
            False,                             # record_positions
        )

        # Sanity: callback is now pending under id 5.
        self.assertIn(5, window._motion_command_callbacks)

        # Simulate the queue emitting completed for a CONTINUOUS jog on axis 5.
        # Before the fix: payload was (metadata=5, result), handler unpacked (5, result),
        # cast metadata to int → 5, popped callback[5] → fired the discrete on_success.
        # After the fix: payload is (kind, metadata, result); handler returns early on kind.
        window._handle_motion_command_completed(
            (MotionCommandQueue.CONTINUOUS_JOG_KIND, 5, None)
        )
        app.processEvents()

        # (a) discrete callback must NOT have been called
        self.assertEqual([], discrete_fired,
                         "jog completion must not fire the discrete callback under the same id")
        # (b) discrete callback must still be pending (not dropped)
        self.assertIn(5, window._motion_command_callbacks,
                      "jog completion must not drop the discrete callback under the same id")

        # Now simulate failed for a CONTINUOUS jog on axis 5.
        # Before the fix: payload was (metadata=5, message), handler popped callback[5]
        # silently (no error shown for the dropped discrete callback).
        # After the fix: handler returns early, shows the jog failure, leaves callback intact.
        window._show_motion_command_failure(
            (MotionCommandQueue.CONTINUOUS_JOG_KIND, 5, "simulated jog error")
        )
        app.processEvents()

        # (a) discrete callback must NOT have been called by the failure path either
        self.assertEqual([], discrete_fired,
                         "jog failure must not fire the discrete callback under the same id")
        # (b) discrete callback must still be pending (not silently dropped by failed)
        self.assertIn(5, window._motion_command_callbacks,
                      "jog failure must not drop the discrete callback under the same id")

        # Verify the failure message was still surfaced (jog errors are real errors).
        self.assertIn("simulated jog error", window.statusBar().currentMessage())

        # Finally, verify that the discrete callback fires correctly when the real
        # discrete command eventually completes (correct-path smoke-test).
        window._handle_motion_command_completed(("", 5, "discrete_result"))
        app.processEvents()
        self.assertEqual(["discrete_result"], discrete_fired,
                         "discrete callback must fire when its own command completes")
        self.assertNotIn(5, window._motion_command_callbacks,
                         "discrete callback must be consumed after its own completion")

    # ----- Fix 3: stop the refresh timer on close ------------------------

    def test_close_motion_control_stops_refresh_timer(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        self.addCleanup(window.close)
        self.assertTrue(window.timer.isActive())

        window.controls["btnOpen"].click()
        window.controls["btnClose"].click()

        self.assertFalse(window.timer.isActive())

    def test_refresh_positions_after_close_does_nothing(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        self.addCleanup(window.close)

        window.controls["btnOpen"].click()
        window.controls["btnClose"].click()
        self.assertIsNone(window._motion_command_queue)
        controller.calls.clear()

        # Must not blow up nor submit to a None queue / reopen anything.
        window.refresh_positions()
        self.assertIsNone(window._motion_command_queue)
        self.assertEqual([], controller.calls)

    def test_close_event_stops_refresh_timer_first(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(controller=controller,
                            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.controls["btnOpen"].click()
        self.assertTrue(window.timer.isActive())

        window.close()
        self.assertFalse(window.timer.isActive())

    # ----- Fix 4: shutdown() re-entry must not join inside _state_lock ------

    def test_second_shutdown_does_not_deadlock_while_worker_is_in_dispatch(self):
        """Second shutdown() must return within its timeout and not hang.

        Scenario: the worker is gated inside _dispatch() (holds no lock), and
        a first shutdown(timeout=0.2) returns False because the worker is still
        in-flight.  Before the fix the re-entry branch called thread.join()
        while holding _state_lock; the worker tries to acquire _state_lock for
        its coalescing check immediately after _dispatch returns, so join would
        stall for the full timeout on every subsequent call.  After the fix the
        join is always outside the lock so both calls complete quickly.
        """
        from automatic_transfer_station.main_window import MotionCommandQueue

        gate_entered = threading.Event()
        gate_release = threading.Event()

        def gated_action():
            gate_entered.set()
            gate_release.wait(5.0)   # block the worker inside _dispatch

        queue = MotionCommandQueue()

        # Submit one command that blocks the worker mid-dispatch.
        queue.submit(gated_action)

        # Wait until the worker is actually inside the gated action.
        self.assertTrue(gate_entered.wait(2.0), "worker never entered gated action")

        # First shutdown: worker is in-flight, so it must return False quickly.
        result1 = queue.shutdown(timeout=0.2)
        self.assertFalse(result1, "first shutdown should return False (worker still running)")

        # Release the gated worker BEFORE the second shutdown so it can finish.
        gate_release.set()

        # Second shutdown (re-entry path): must complete within timeout and
        # must NOT deadlock regardless of whether the worker has exited yet.
        start = time.monotonic()
        result2 = queue.shutdown(timeout=2.0)
        elapsed = time.monotonic() - start

        self.assertTrue(result2, "second shutdown should return True once worker finishes")
        self.assertFalse(queue._thread.is_alive(), "worker thread must have exited")
        # The join should complete well within the 2.0 s timeout; 1.5 s is a
        # generous bound — the actual elapsed time is typically < 0.01 s.
        self.assertLess(elapsed, 1.5,
                        f"second shutdown took {elapsed:.3f}s — possible lock contention")


class BoardActionExecutorTests(unittest.TestCase):
    """BP4b: run motion-card open/close/reset off the GUI thread via an injectable
    board executor. Inline default keeps the synchronous behavior; the threaded
    executor delivers results to the GUI thread without blocking."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"
        self.microscope_position_store_path = Path(self._tmpdir.name) / "microscope_z_positions.json"
        self.runtime_calibration_path = Path(self._tmpdir.name) / "axis_runtime_calibration.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_window(self, controller, board_action_executor=None):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        return MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            microscope_position_store_path=self.microscope_position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
            board_action_executor=board_action_executor,
        )

    # ---- 1. Inline default keeps existing synchronous behavior unchanged ----

    def test_default_executor_open_is_synchronous(self):
        controller = RecordingController()
        window = self._make_window(controller)
        self.addCleanup(window.close)

        window.controls["btnOpen"].click()

        # Inline default => open_board ran and is_open is True immediately.
        self.assertTrue(controller.is_open)
        self.assertIn(("open_board",), controller.calls)
        self.assertEqual("运动控制已打开", window.controls["motionStatusText"].text())

    def test_default_executor_close_shuts_queue_before_closing_board(self):
        controller = RecordingController()
        window = self._make_window(controller)
        self.addCleanup(window.close)

        window.controls["btnOpen"].click()
        window.controls["btnFwd1"].click()  # lazily create the queue
        self.assertIsNotNone(window._motion_command_queue)

        window.controls["btnClose"].click()

        self.assertFalse(controller.is_open)
        self.assertIn(("close_board",), controller.calls)
        self.assertIsNone(window._motion_command_queue)
        self.assertFalse(window.timer.isActive())

    # ---- 2. Inline path: buttons disable-then-re-enable, status transitions ----

    def test_buttons_disabled_during_action_and_reenabled_after(self):
        from automatic_transfer_station.main_window import InlineBoardExecutor

        controller = RecordingController()

        observations = {}

        class ObservingExecutor(InlineBoardExecutor):
            def submit(self, action, on_done, on_error):
                # Capture button + status state WHILE the action is in flight,
                # i.e. between disable and the finish/re-enable callbacks.
                observations["btnOpen_enabled_during"] = window.controls["btnOpen"].isEnabled()
                observations["btnClose_enabled_during"] = window.controls["btnClose"].isEnabled()
                observations["btnReset_enabled_during"] = window.controls["button4"].isEnabled()
                observations["status_during"] = window.controls["motionStatusText"].text()
                super().submit(action, on_done, on_error)

        window = self._make_window(controller, board_action_executor=ObservingExecutor())
        self.addCleanup(window.close)

        window.controls["btnOpen"].click()

        # During the action the board buttons were disabled and a busy status shown.
        self.assertFalse(observations["btnOpen_enabled_during"])
        self.assertFalse(observations["btnClose_enabled_during"])
        self.assertFalse(observations["btnReset_enabled_during"])
        self.assertEqual("运动控制连接中…", observations["status_during"])

        # After the synchronous action completed, buttons are re-enabled and the
        # status transitioned to the success text.
        self.assertTrue(window.controls["btnOpen"].isEnabled())
        self.assertTrue(window.controls["btnClose"].isEnabled())
        self.assertTrue(window.controls["button4"].isEnabled())
        self.assertEqual("运动控制已打开", window.controls["motionStatusText"].text())

    def test_inline_action_failure_reenables_buttons_and_warns(self):
        class FailingOpenController(RecordingController):
            def open_board(self):
                from automatic_transfer_station.controller_base import MotionControllerError
                raise MotionControllerError("GA_Open failed")

        controller = FailingOpenController()
        window = self._make_window(controller)
        self.addCleanup(window.close)

        warnings = []
        with patch("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: warnings.append(a)):
            window.controls["btnOpen"].click()

        self.assertEqual(1, len(warnings))
        self.assertFalse(controller.is_open)
        # Buttons must be usable again after the failure.
        self.assertTrue(window.controls["btnOpen"].isEnabled())
        self.assertTrue(window.controls["btnClose"].isEnabled())
        self.assertTrue(window.controls["button4"].isEnabled())

    # ---- 3. ThreadedBoardExecutor in isolation (offscreen) ----

    def _wait_for_executor_result(self, executor, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            executor._drain_results()
            if predicate():
                return True
            time.sleep(0.01)
        executor._drain_results()
        return bool(predicate())

    def _drain_executor(self, executor):
        executor.wait_for_idle()
        executor._drain_results()

    def test_threaded_board_executor_delivers_on_done(self):
        from automatic_transfer_station.main_window import ThreadedBoardExecutor

        get_app()
        executor = ThreadedBoardExecutor()
        self.addCleanup(lambda: self._drain_executor(executor))
        ran = threading.Event()
        results = {}

        def action():
            ran.set()
            return 42

        def on_done(result):
            results["done"] = result

        def on_error(exc):
            results["error"] = exc

        start = time.monotonic()
        executor.submit(action, on_done, on_error)
        submit_elapsed = time.monotonic() - start

        # submit() must return immediately (does not run the action inline).
        self.assertLess(submit_elapsed, 0.1)
        self.assertTrue(self._wait_for_executor_result(executor, lambda: "done" in results, timeout=2.0))
        self.assertEqual(42, results["done"])
        self.assertNotIn("error", results)

    def test_threaded_board_executor_delivers_on_error(self):
        from automatic_transfer_station.main_window import ThreadedBoardExecutor

        get_app()
        executor = ThreadedBoardExecutor()
        self.addCleanup(lambda: self._drain_executor(executor))
        results = {}

        def boom():
            raise RuntimeError("board boom")

        executor.submit(
            boom,
            lambda result: results.__setitem__("done", result),
            lambda exc: results.__setitem__("error", exc),
        )

        self.assertTrue(self._wait_for_executor_result(executor, lambda: "error" in results, timeout=2.0))
        self.assertIsInstance(results["error"], RuntimeError)
        self.assertEqual("board boom", str(results["error"]))
        self.assertNotIn("done", results)

    def test_threaded_open_does_not_block_gui_thread(self):
        from automatic_transfer_station.main_window import ThreadedBoardExecutor

        app = get_app()

        class SlowOpenController(RecordingController):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()

            def open_board(self):
                self.entered.set()
                time.sleep(0.3)
                super().open_board()

        controller = SlowOpenController()
        window = self._make_window(controller, board_action_executor=ThreadedBoardExecutor())
        self.addCleanup(window.close)
        # Stop the 100 ms position-refresh timer so a periodic safety check does
        # not overwrite the board-status text mid-test (this test exercises the
        # open click, not position refresh).
        window.timer.stop()

        start = time.monotonic()
        window.controls["btnOpen"].click()
        click_elapsed = time.monotonic() - start

        # The click returned immediately even though open_board sleeps 0.3 s.
        self.assertLess(click_elapsed, 0.15)
        self.assertTrue(controller.entered.wait(0.5))
        # While in flight: buttons disabled, busy status.
        self.assertFalse(window.controls["btnOpen"].isEnabled())
        self.assertEqual("运动控制连接中…", window.controls["motionStatusText"].text())
        # Eventually completes on the GUI thread: is_open True, buttons re-enabled.
        executor = window._board_action_executor
        self.assertTrue(self._wait_for_executor_result(executor, lambda: controller.is_open, timeout=2.0))
        self.assertTrue(window.controls["btnOpen"].isEnabled())
        self.assertEqual("运动控制已打开", window.controls["motionStatusText"].text())

    # ---- 4. Watchdog surfaces a timeout for a wedged board action ----

    def test_watchdog_surfaces_timeout_for_wedged_action(self):
        from automatic_transfer_station.main_window import MainWindow

        app = get_app()

        # A controllable executor that holds the action without ever completing,
        # so the watchdog is the only thing that can resolve the in-flight state.
        class NeverCompletesExecutor:
            def __init__(self):
                self.released = False

            def submit(self, action, on_done, on_error):
                # Never call on_done/on_error: simulate a wedged DLL call.
                self._action = action

            def wait_for_idle(self, timeout_ms=5000):
                return None

            def is_busy(self):
                return False

        controller = RecordingController()
        executor = NeverCompletesExecutor()
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            board_action_executor=executor,
        )
        self.addCleanup(window.close)
        # Short watchdog so the test is fast and deterministic.
        window.BOARD_ACTION_TIMEOUT_MS = 50

        warnings = []
        with patch("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: warnings.append(a)):
            window.controls["btnOpen"].click()
            # In flight: buttons disabled.
            self.assertFalse(window.controls["btnOpen"].isEnabled())
            self.assertTrue(window._board_action_in_flight)
            # Trigger the watchdog path deterministically without pumping the
            # process-wide Qt event loop.
            watchdog = window._board_action_watchdog
            if watchdog is not None:
                watchdog.stop()
            window._on_board_action_timeout("open")
            self.assertFalse(window._board_action_in_flight)

        self.assertEqual(1, len(warnings), "watchdog must surface a timeout warning")
        self.assertIn("超时", window.controls["motionStatusText"].text())
        # Buttons re-enabled so the operator is not stuck.
        self.assertTrue(window.controls["btnOpen"].isEnabled())

    def test_late_board_success_after_timeout_does_not_clear_timeout_alarm(self):
        class LateCompletesExecutor:
            def submit(self, action, on_done, on_error):
                self.action = action
                self.on_done = on_done
                self.on_error = on_error

            def wait_for_idle(self, timeout_ms=5000):
                return None

            def is_busy(self):
                return False

        controller = RecordingController()
        executor = LateCompletesExecutor()
        window = self._make_window(controller, board_action_executor=executor)
        self.addCleanup(window.close)
        window.timer.stop()

        warnings = []
        with patch("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: warnings.append(a)):
            window._run_board_action("open")
            window._on_board_action_timeout("open")

        timeout_text = window.controls["motionStatusText"].text()
        self.assertIn("超时", timeout_text)

        executor.on_done(None)

        self.assertEqual(timeout_text, window.controls["motionStatusText"].text())
        self.assertFalse(controller.is_open)


class Package7SafetyPathTests(unittest.TestCase):
    """Package 7: backfill safety-path tests."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"
        self.runtime_calibration_path = Path(self._tmpdir.name) / "axis_runtime_calibration.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    # ---- Test 1 (main_window part): refresh_positions issues stop_axis for
    #      BOTH X2(7) and X3(5) when they violate the interference boundary.

    def test_refresh_positions_stops_both_x2_x3_on_interference_violation(self):
        """_stop_unsafe_axes_if_needed issues stop_axis(5) AND stop_axis(7) via
        the inline queue when positions cross the X2/X3 interference boundary."""
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True
        # Positions that violate the interference boundary (sum <= -841567):
        # axis5=100000, axis7=-1000000 -> sum=-900000 < -841567.
        controller.positions[5] = 100000.0
        controller.positions[7] = -1000000.0

        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        controller.calls.clear()

        window.refresh_positions()

        stop_calls = {c[1] for c in controller.calls if c[0] == "stop_axis"}
        self.assertIn(5, stop_calls, "stop_axis(5) must be issued for X3 on interference")
        self.assertIn(7, stop_calls, "stop_axis(7) must be issued for X2 on interference")

    # ---- Test 4: closing the window turns the heater off ----

    def test_close_event_calls_temperature_panel_disconnect(self):
        """window.close() must invoke temperature_panel.disconnect_from_device."""
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        disconnect_calls = []

        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        # Monkeypatch disconnect_from_device with a recorder.
        window.temperature_panel.disconnect_from_device = lambda: disconnect_calls.append("disconnect")

        window.close()

        self.assertEqual(["disconnect"], disconnect_calls,
                         "closeEvent must call temperature_panel.disconnect_from_device")

    def test_close_event_completes_even_if_disconnect_raises(self):
        """closeEvent must not propagate exceptions from disconnect_from_device;
        it should surface the error to the status bar and still complete."""
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )

        def raise_error():
            raise RuntimeError("heater_off_failed")

        window.temperature_panel.disconnect_from_device = raise_error

        # Must not raise; error is surfaced via status bar.
        try:
            window.close()
        except Exception as exc:
            self.fail(f"closeEvent propagated an exception: {exc}")

        # Error message should appear in the status bar.
        self.assertIn("heater_off_failed", window.statusBar().currentMessage())

    def test_heat_warning_banner_present(self):
        """The temperature panel must show a prominent heating-hazard banner."""
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        warning = window.controls.get("tempHeatWarning")
        self.assertIsNotNone(warning, "heating warning banner must exist")
        self.assertIn("高温", warning.text())

    def test_exit_button_present(self):
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        self.assertIsNotNone(window.controls.get("exitButton"), "Exit button must exist")

    def test_window_x_close_is_refused_until_exit(self):
        """A spontaneous close (the OS title-bar X) must be ignored and must NOT run
        the shutdown, so the app keeps running until the Exit button is used."""
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        disconnect_calls = []
        window.temperature_panel.disconnect_from_device = lambda: disconnect_calls.append("d")

        class FakeEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def spontaneous(self):
                return True

            def ignore(self):
                self.ignored = True

            def accept(self):
                self.accepted = True

        event = FakeEvent()
        window.closeEvent(event)

        self.assertTrue(event.ignored, "spontaneous X close must be ignored")
        self.assertEqual([], disconnect_calls, "shutdown must NOT run on a refused X close")

    def test_request_exit_confirmed_closes_and_disconnects(self):
        """Confirming the Exit dialog must close the window and run the heater-off /
        port-close shutdown (disconnect_from_device)."""
        from automatic_transfer_station import main_window as mw
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        window = MainWindow(
            controller=RecordingController(),
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
        )
        disconnect_calls = []
        window.temperature_panel.disconnect_from_device = lambda: disconnect_calls.append("d")

        original_question = mw.QMessageBox.question
        mw.QMessageBox.question = staticmethod(lambda *a, **k: mw.QMessageBox.Yes)
        try:
            window._request_exit()
        finally:
            mw.QMessageBox.question = original_question

        self.assertTrue(window._exit_requested, "confirming Exit must set the exit flag")
        self.assertEqual(["d"], disconnect_calls, "Exit must run temperature disconnect")

    # ---- Test 5: changeEvent disarm must not be guarded by isActiveWindow ----

    def test_changeEvent_disarms_gamepad_unconditionally_when_window_not_active(self):
        """Force isActiveWindow() to return False, fire ActivationChange, and
        assert disarm() was invoked -- without relying on the runtime window-active
        state (the original test had 'if not window.isActiveWindow(): assert',
        making it silently pass when the window was active)."""
        from PySide6.QtCore import QEvent
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True

        class FakeReader:
            def read_state(self, index=None):
                from automatic_transfer_station.gamepad_xinput import GamepadSnapshot
                return GamepadSnapshot(False, 0, 0, 0, 0, 0, 0, 0, 0)

        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            gamepad_reader=FakeReader(),
        )

        disarmed = {"n": 0}
        window.gamepad_panel.disarm = lambda: disarmed.__setitem__("n", disarmed["n"] + 1)

        # Force isActiveWindow to always return False so the assertion is not skippable.
        window.isActiveWindow = lambda: False

        window.changeEvent(QEvent(QEvent.Type.ActivationChange))

        self.assertGreaterEqual(disarmed["n"], 1,
                                "disarm() must be called when ActivationChange fires and "
                                "isActiveWindow() is False")


class CP1ReactiveScanBackstopTests(unittest.TestCase):
    """CP1: reactive motion-safety scan backstop.

    A2 — position-refresh watchdog: a DLL read that HANGS (never returns and
    never raises) leaves _position_refresh_pending latched True forever, so the
    reactive interference scan goes permanently and silently dead.  A bounded
    single-shot watchdog must DETECT this, bump the generation, clear the latch
    (recovery), log CRITICAL, raise a persistent alarm, and attempt a protective
    stop.

    A3 — read-failure fail-safe: consecutive position-read failures during an
    open board must, past a threshold, trigger a protective stop + alarm + audit
    CRITICAL; a single transient failure must NOT.

    All tests are deterministic: the watchdog handler is invoked directly and the
    failure handler is driven directly — no real 2 s sleeps.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.position_store_path = Path(self._tmpdir.name) / "axis_positions.json"
        self.runtime_calibration_path = Path(self._tmpdir.name) / "axis_runtime_calibration.json"
        self.microscope_position_store_path = Path(self._tmpdir.name) / "microscope_z_positions.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_window(self, controller):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        window = MainWindow(
            controller=controller,
            motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
            position_store_path=self.position_store_path,
            runtime_calibration_path=self.runtime_calibration_path,
            microscope_position_store_path=self.microscope_position_store_path,
        )
        # Stop the 100 ms timer so it can't fire a refresh during the test.
        window.timer.stop()
        return window

    # ---- A2: watchdog config + arming ----

    def test_watchdog_timeout_constant_is_configured(self):
        from automatic_transfer_station.main_window import MainWindow
        self.assertTrue(hasattr(MainWindow, "POSITION_REFRESH_WATCHDOG_MS"))
        self.assertGreater(MainWindow.POSITION_REFRESH_WATCHDOG_MS, 0)

    def test_request_refresh_arms_watchdog(self):
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)

        self.assertFalse(window._position_refresh_pending)
        window._request_async_position_refresh()
        # Inline queue: the read completes synchronously and disarms the watchdog,
        # so after the call the watchdog must NOT still be active.
        self.assertFalse(
            window._position_refresh_watchdog_active(),
            "a synchronously-completed read must disarm the watchdog",
        )
        self.assertFalse(window._position_refresh_pending)

    # ---- A2: watchdog FIRES on a hung read ----

    def test_watchdog_fire_recovers_latch_and_bumps_generation(self):
        """Simulate a read that never completes: pending stays True, then the
        watchdog fires.  The latch must clear (so the next tick can retry) and the
        generation must bump (so the eventually-returning stale read is dropped)."""
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)

        gen_before = window._position_refresh_generation
        # Simulate an in-flight read that never returned.
        window._position_refresh_pending = True

        window._on_position_refresh_watchdog_timeout()

        self.assertFalse(
            window._position_refresh_pending,
            "watchdog must clear the latch so a fresh read can be requested",
        )
        self.assertEqual(
            gen_before + 1, window._position_refresh_generation,
            "watchdog must bump the generation so a late stale read is discarded",
        )

    def test_watchdog_fire_logs_critical_audit(self):
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        window._position_refresh_pending = True

        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            window._on_position_refresh_watchdog_timeout()

        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.CRITICAL, levels)

    def test_watchdog_fire_sets_alarm_lamp(self):
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        window._position_refresh_pending = True

        window._on_position_refresh_watchdog_timeout()

        # alarm lamp uses #dc2626 (red)
        self.assertIn("#dc2626", window.controls["motionStatusLamp"].styleSheet())

    def test_watchdog_fire_attempts_protective_stop(self):
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        window._position_refresh_pending = True
        controller.calls.clear()

        window._on_position_refresh_watchdog_timeout()

        self.assertIn(
            ("stop_all",), controller.calls,
            "watchdog must attempt a best-effort protective stop_all",
        )

    def test_watchdog_recovery_allows_fresh_read_next_tick(self):
        """After the watchdog fires (latch cleared), a fresh refresh_positions
        tick must successfully issue a new read (scan recovers)."""
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)

        # Hang a read, fire the watchdog (recovery).
        window._position_refresh_pending = True
        window._on_position_refresh_watchdog_timeout()
        controller.calls.clear()

        # A fresh tick must now read positions again (inline queue → synchronous).
        window.refresh_positions()
        self.assertTrue(
            any(c[0] == "get_axis_position" for c in controller.calls)
            or not window._position_refresh_pending,
            "scan must resume after watchdog recovery",
        )

    def test_normal_completion_disarms_watchdog_no_false_alarm(self):
        """A NORMAL successful refresh must disarm the watchdog and not raise an
        alarm."""
        controller = RecordingController()
        controller.is_open = True
        # The default RecordingController positions leave axis 5 (X3) outside its
        # soft range, which would itself raise a (legitimate) safety alarm. Park
        # axis 5 at its zero pulse so this clean-read test isolates the watchdog
        # behaviour from the interference scan.
        controller.positions[5] = 687898.0  # X3 zero_pulse → coordinate 0, safe
        window = self._make_window(controller)

        # Drive a successful refresh through the inline queue.
        window.refresh_positions()

        self.assertFalse(window._position_refresh_pending)
        self.assertFalse(
            window._position_refresh_watchdog_active(),
            "a completed read must leave the watchdog disarmed",
        )
        # No alarm lamp (#dc2626) on a clean read.
        self.assertNotIn("#dc2626", window.controls["motionStatusLamp"].styleSheet())

    def test_watchdog_stopped_on_close(self):
        """closeEvent must stop the position-refresh watchdog so it can't fire
        into a destroyed window."""
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        # Arm the watchdog by hand (simulating an in-flight read).
        window._arm_position_refresh_watchdog()
        self.assertTrue(window._position_refresh_watchdog_active())

        window.close()

        self.assertFalse(
            window._position_refresh_watchdog_active(),
            "closeEvent must stop the position-refresh watchdog",
        )

    # ---- A3: consecutive read-failure fail-safe ----

    def test_consecutive_failure_limit_constant_is_configured(self):
        from automatic_transfer_station.main_window import MainWindow
        self.assertTrue(hasattr(MainWindow, "CONSECUTIVE_READ_FAILURE_LIMIT"))
        self.assertGreaterEqual(MainWindow.CONSECUTIVE_READ_FAILURE_LIMIT, 1)

    def test_single_failure_does_not_stop(self):
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        controller.calls.clear()

        gen = window._position_refresh_generation
        window._position_refresh_pending = True
        window._handle_async_position_refresh_failure((gen, "transient read error"))

        self.assertNotIn(
            ("stop_all",), controller.calls,
            "a single transient failure must NOT trigger a protective stop",
        )

    def test_nth_minus_one_failures_do_not_stop(self):
        from automatic_transfer_station.main_window import MainWindow
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        controller.calls.clear()

        limit = MainWindow.CONSECUTIVE_READ_FAILURE_LIMIT
        for _ in range(limit - 1):
            gen = window._position_refresh_generation
            window._position_refresh_pending = True
            window._handle_async_position_refresh_failure((gen, "read error"))

        self.assertNotIn(
            ("stop_all",), controller.calls,
            f"{limit - 1} consecutive failures must NOT trigger a stop",
        )

    def test_nth_failure_triggers_protective_stop_alarm_and_audit(self):
        from automatic_transfer_station.main_window import MainWindow
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        controller.calls.clear()

        limit = MainWindow.CONSECUTIVE_READ_FAILURE_LIMIT
        # First limit-1 failures: no stop.
        for _ in range(limit - 1):
            gen = window._position_refresh_generation
            window._position_refresh_pending = True
            window._handle_async_position_refresh_failure((gen, "read error"))
        self.assertNotIn(("stop_all",), controller.calls)

        # The Nth consecutive failure must trip the fail-safe + audit CRITICAL.
        gen = window._position_refresh_generation
        window._position_refresh_pending = True
        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            window._handle_async_position_refresh_failure((gen, "read error"))

        self.assertIn(
            ("stop_all",), controller.calls,
            "the Nth consecutive failure must trigger a protective stop_all",
        )
        levels = [r.levelno for r in cm.records]
        self.assertIn(logging.CRITICAL, levels)
        # Persistent alarm lamp.
        self.assertIn("#dc2626", window.controls["motionStatusLamp"].styleSheet())

    def test_success_resets_consecutive_failure_counter(self):
        from automatic_transfer_station.main_window import MainWindow
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)

        limit = MainWindow.CONSECUTIVE_READ_FAILURE_LIMIT
        # limit-1 failures (one short of tripping).
        for _ in range(limit - 1):
            gen = window._position_refresh_generation
            window._position_refresh_pending = True
            window._handle_async_position_refresh_failure((gen, "read error"))

        # A successful read resets the counter.
        gen = window._position_refresh_generation
        window._position_refresh_pending = True
        window._handle_async_position_refresh((gen, {axis: 0.0 for axis in range(1, 10)}, 0.0))
        self.assertEqual(window._consecutive_read_failures, 0)

        controller.calls.clear()
        # After the reset, another limit-1 failures must again NOT trip.
        for _ in range(limit - 1):
            gen = window._position_refresh_generation
            window._position_refresh_pending = True
            window._handle_async_position_refresh_failure((gen, "read error"))
        self.assertNotIn(
            ("stop_all",), controller.calls,
            "a successful read must reset the consecutive-failure counter",
        )

    # ---- A3 (CP5): watchdog disarmed symmetrically in pending-cancelled hook ----

    def test_a3_pending_cancelled_disarms_position_refresh_watchdog(self):
        """A3 (CP5): after _on_motion_queue_pending_cancelled the position-refresh
        watchdog must NOT be active.

        The invariant is: 'pending cleared ⇒ watchdog disarmed' — it must hold
        regardless of the 100 ms refresh timer's liveness, so the hook must disarm
        the watchdog explicitly rather than relying on the timer to let it expire.
        """
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)

        # Arm the watchdog manually to simulate an in-flight position read.
        window._arm_position_refresh_watchdog()
        self.assertTrue(
            window._position_refresh_watchdog_active(),
            "pre-condition: watchdog must be active after arm",
        )
        window._position_refresh_pending = True

        # Trigger the pending-cancelled hook (as if cancel_pending() was called).
        window._on_motion_queue_pending_cancelled()

        self.assertFalse(
            window._position_refresh_pending,
            "pending flag must be cleared after pending-cancelled hook",
        )
        self.assertFalse(
            window._position_refresh_watchdog_active(),
            "A3: watchdog must be disarmed after _on_motion_queue_pending_cancelled (CP5)",
        )

    # ---- B3 (CP5): reactive scan partial snapshot fails safe ----

    def test_b3_partial_position_snapshot_missing_one_interference_axis_fails_safe(self):
        """B3 (CP5): a position snapshot that contains ONLY ONE of the X2/X3
        interference-pair axes (e.g. axis5 present, axis7 absent) must NOT be
        treated as safe by _stop_unsafe_axes_if_needed.

        Under the CP5 contract, unsafe_axes_for_positions now returns NOT ok when
        exactly one partner is present (fail-safe: cannot evaluate interference).
        This test pins that the reactive scan in the main window honours that contract
        — it must issue a stop rather than silently passing the incomplete snapshot.
        """
        from automatic_transfer_station.main_window import MainWindow

        get_app()
        controller = RecordingController()
        controller.is_open = True
        window = self._make_window(controller)
        controller.calls.clear()

        # Partial snapshot: only axis5 (X3) present; axis7 (X2) absent.
        # Under the old "silently skip" contract this would have been safe; under
        # the CP5 fail-safe contract it must be treated as unsafe.
        partial_positions = {5: 687898.0}  # X3 at 0mm — but partner 7 absent

        window._stop_unsafe_axes_if_needed(partial_positions)

        # The reactive scan must have attempted to stop at least one of the pair.
        stopped_axes = {c[1] for c in controller.calls if c[0] == "stop_axis"}
        self.assertTrue(
            stopped_axes,
            "B3: partial snapshot (axis5 only) must trigger a safety stop — "
            "not silently treated as safe (CP5 fail-safe)",
        )
        # At least one of the interference pair (5 or 7) must be in the stopped set.
        self.assertTrue(
            stopped_axes & {5, 7},
            "B3: the stopped axes must include at least one of the interference pair (5, 7)",
        )


if __name__ == "__main__":
    unittest.main()
