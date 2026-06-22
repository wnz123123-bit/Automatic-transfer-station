import os
import sys
import threading
import time
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


def get_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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


class RecordingNosepieceController:
    def __init__(self, port="COM1"):
        self.calls = []
        self.position = 0
        self.is_open = False
        self.port = port

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


class BlockingNosepieceController(RecordingNosepieceController):
    """Mimics a real serial controller whose connect()/read_position() block
    for a long time (the timeout=8.0 freeze described in bug I6)."""

    def __init__(self, port="COM1", delay=0.6):
        super().__init__(port=port)
        self.delay = delay
        self.connect_started = threading.Event()
        self.read_started = threading.Event()
        self.release = threading.Event()

    def connect(self):
        self.connect_started.set()
        self.release.wait(self.delay)
        super().connect()

    def read_position(self):
        self.read_started.set()
        self.release.wait(self.delay)
        super().read_position()
        return self.position


class TrackingOldController(RecordingNosepieceController):
    """An already-connected controller that should be disconnected when the
    panel switches to a freshly-created controller (bug Minor-c)."""

    def __init__(self, port="COM_OLD"):
        super().__init__(port=port)
        self.is_open = True


class ObjectivePanelThreadingTests(unittest.TestCase):
    def setUp(self):
        get_app()

    def _make_panel(self, controller, factory=None, port="COM7", port_lister=None):
        from automatic_transfer_station.objective_panel import ObjectivePanel

        registry = {}
        panel = ObjectivePanel(
            controller,
            register=lambda name, widget: registry.setdefault(name, widget),
            controller_factory=factory,
            port_lister=port_lister or (lambda: []),
            initial_port=port,
        )
        panel.open_com_dialog()
        get_app().processEvents()
        return panel

    # --- Bug I6: connect / read_position run off the GUI thread ---------------

    def test_connect_returns_quickly_with_blocking_controller(self):
        from automatic_transfer_station.objective_panel import ObjectivePanel

        created = []

        def factory(port):
            controller = BlockingNosepieceController(port=port)
            created.append(controller)
            return controller

        panel = self._make_panel(RecordingNosepieceController(), factory=factory)
        panel.controls["objectivePortCombo"].setCurrentText("COM7")

        start = time.monotonic()
        panel.connect_selected_port()
        elapsed = time.monotonic() - start

        # The blocking connect() must NOT run on the GUI thread.
        self.assertLess(elapsed, 0.2)
        self.assertTrue(created[0].connect_started.wait(0.5))

        created[0].release.set()

        self.assertTrue(wait_until(lambda: ("connect",) in created[0].calls))
        self.assertTrue(wait_until(lambda: ("read_position",) in created[0].calls))
        self.assertIs(panel.nosepiece_controller, created[0])
        self.assertTrue(wait_until(lambda: "已连接 COM7" in panel.controls["objectiveComStatusLabel"].text()))

    def test_read_position_button_returns_quickly_with_blocking_controller(self):
        controller = BlockingNosepieceController()
        controller.is_open = True
        controller.position = 3
        panel = self._make_panel(controller)

        start = time.monotonic()
        panel.refresh_position()
        elapsed = time.monotonic() - start

        # The blocking read_position() must NOT run on the GUI thread.
        self.assertLess(elapsed, 0.2)
        self.assertTrue(controller.read_started.wait(0.5))

        controller.release.set()

        self.assertTrue(wait_until(lambda: ("read_position",) in controller.calls))
        self.assertTrue(wait_until(lambda: "20X" in panel.controls["objectiveStatusLabel"].text()))

    def test_wait_for_idle_waits_for_connect_worker(self):
        created = []

        def factory(port):
            controller = BlockingNosepieceController(port=port, delay=2.0)
            created.append(controller)
            return controller

        panel = self._make_panel(RecordingNosepieceController(), factory=factory)
        panel.controls["objectivePortCombo"].setCurrentText("COM7")
        panel.connect_selected_port()
        self.assertTrue(created[0].connect_started.wait(0.5))

        created[0].release.set()
        panel.wait_for_idle()
        get_app().processEvents()

        # After wait_for_idle the worker has finished its blocking call.
        self.assertIn(("connect",), created[0].calls)

    # --- Bug new: serial.SerialException is caught as a connect failure -------

    def test_connect_reports_serial_exception_as_failure(self):
        import serial

        created = []

        def factory(port):
            controller = RecordingNosepieceController(port=port)

            def boom():
                raise serial.SerialException("could not open port COM7")

            controller.connect = boom
            created.append(controller)
            return controller

        original = RecordingNosepieceController()
        panel = self._make_panel(original, factory=factory)
        panel.controls["objectivePortCombo"].setCurrentText("COM7")

        # Must not raise out of the slot.
        panel.connect_selected_port()

        self.assertTrue(
            wait_until(lambda: "连接失败" in panel.controls["objectiveComStatusLabel"].text())
        )
        # On failure the panel keeps the previous controller.
        self.assertIs(panel.nosepiece_controller, original)

    # --- Bug Minor-c: previous open controller is disconnected on replace -----

    def test_connect_disconnects_previous_open_controller(self):
        old = TrackingOldController()
        created = []

        def factory(port):
            controller = RecordingNosepieceController(port=port)
            controller.position = 2
            created.append(controller)
            return controller

        panel = self._make_panel(old, factory=factory)
        panel.controls["objectivePortCombo"].setCurrentText("COM7")

        panel.connect_selected_port()

        self.assertTrue(wait_until(lambda: ("connect",) in created[0].calls))
        self.assertTrue(wait_until(lambda: panel.nosepiece_controller is created[0]))
        self.assertIn(("disconnect",), old.calls)

    # --- Fix 1: worker signals delivered to a torn-down panel are no-ops -------

    def test_completion_slots_noop_when_closing(self):
        """With the panel _closing flag set, every worker-completion slot must
        early-return without writing to (now half-destroyed) widgets."""
        controller = RecordingNosepieceController()
        panel = self._make_panel(controller)
        panel._closing = True

        status_before = panel.controls["objectiveStatusLabel"].text()
        com_before = panel.controls["objectiveComStatusLabel"].text()

        # None of these should raise or mutate the UI while closing.
        panel._complete_connect(controller, 3)
        panel._fail_connect("boom")
        panel._complete_objective_move("20X", 3)
        panel._fail_objective_move("boom")
        panel._apply_position(3)
        panel._fail_read_position("boom")
        panel._clear_connect_thread()
        panel._clear_objective_move()
        panel._clear_read_thread()

        self.assertEqual(status_before, panel.controls["objectiveStatusLabel"].text())
        self.assertEqual(com_before, panel.controls["objectiveComStatusLabel"].text())

    def test_emitted_worker_signal_after_closing_is_noop(self):
        """Emitting a worker signal after _closing is set must not change the UI
        (a late queued delivery into a torn-down panel)."""
        from automatic_transfer_station.objective_panel import ObjectiveMoveThread

        controller = RecordingNosepieceController()
        panel = self._make_panel(controller)
        status_before = panel.controls["objectiveStatusLabel"].text()

        thread = ObjectiveMoveThread(controller, "20X", 3, panel)
        thread.succeeded.connect(panel._complete_objective_move)
        thread.failed.connect(panel._fail_objective_move)
        panel._closing = True
        thread.succeeded.emit("20X", 3)
        thread.failed.emit("boom")
        get_app().processEvents()

        self.assertEqual(status_before, panel.controls["objectiveStatusLabel"].text())

    # --- Fix 2: closing while a worker thread is still running must not destroy
    #     a running QThread (std::terminate) -----------------------------------

    def test_prepare_for_close_detaches_still_running_thread(self):
        """If a move thread is still running after the bounded wait, the close
        path must NOT leave it parented to / owned by the panel (so Qt does not
        destroy a running QThread). It is reparented to None and kept referenced."""
        from PySide6.QtCore import QThread

        class GatedMoveThread(QThread):
            succeeded = __import__("PySide6.QtCore", fromlist=["Signal"]).Signal(str, int)
            failed = __import__("PySide6.QtCore", fromlist=["Signal"]).Signal(str)

            def __init__(self, parent=None):
                super().__init__(parent)
                self._release = threading.Event()
                self.started_running = threading.Event()

            def run(self):
                self.started_running.set()
                self._release.wait(5.0)

        controller = RecordingNosepieceController()
        panel = self._make_panel(controller)

        gated = GatedMoveThread(panel)
        panel._move_thread = gated
        gated.start()
        self.assertTrue(gated.started_running.wait(1.0))

        # Bounded wait expires (thread still running) -> must return False and
        # detach the straggler rather than allowing its destruction.
        idle = panel.prepare_for_close(timeout_ms=50)
        self.assertFalse(idle)
        self.assertTrue(gated.isRunning())
        # Detached from the panel so the panel's destruction won't delete it.
        self.assertIsNone(gated.parent())
        # Still referenced somewhere so Python/Qt won't GC it mid-run.
        self.assertIn(gated, panel._detached_threads)

        # Cleanup: release the gate and let it finish so teardown is clean.
        gated._release.set()
        gated.wait(2000)

    def test_prepare_for_close_returns_true_when_idle(self):
        controller = RecordingNosepieceController()
        panel = self._make_panel(controller)
        self.assertTrue(panel.prepare_for_close(timeout_ms=50))
        self.assertEqual([], list(panel._detached_threads))

    # --- Fix 3: controller swap refused while a move/read thread is running ----

    def test_port_switch_refused_while_move_running(self):
        from PySide6.QtCore import QThread

        class GatedMoveThread(QThread):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._release = threading.Event()
                self.started_running = threading.Event()

            def run(self):
                self.started_running.set()
                self._release.wait(5.0)

        old = TrackingOldController(port="COM_OLD")
        created = []

        def factory(port):
            controller = RecordingNosepieceController(port=port)
            created.append(controller)
            return controller

        panel = self._make_panel(old, factory=factory)
        panel.controls["objectivePortCombo"].setCurrentText("COM7")

        gated = GatedMoveThread(panel)
        panel._move_thread = gated
        gated.start()
        self.assertTrue(gated.started_running.wait(1.0))

        result = panel.connect_selected_port()

        self.assertFalse(result)
        # No new controller created, no swap, old port not closed.
        self.assertEqual([], created)
        self.assertIs(panel.nosepiece_controller, old)
        self.assertNotIn(("disconnect",), old.calls)
        self.assertIn("操作进行中", panel.controls["objectiveComStatusLabel"].text())

        gated._release.set()
        gated.wait(2000)

    def test_refresh_position_refused_while_move_running(self):
        from PySide6.QtCore import QThread

        class GatedMoveThread(QThread):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._release = threading.Event()
                self.started_running = threading.Event()

            def run(self):
                self.started_running.set()
                self._release.wait(5.0)

        controller = RecordingNosepieceController()
        controller.is_open = True
        panel = self._make_panel(controller)

        gated = GatedMoveThread(panel)
        panel._move_thread = gated
        gated.start()
        self.assertTrue(gated.started_running.wait(1.0))

        panel.refresh_position()
        # No read thread started while a move is in flight.
        self.assertIsNone(panel._read_thread)
        self.assertNotIn(("read_position",), controller.calls)

        gated._release.set()
        gated.wait(2000)

    # --- Fix 5: refresh_position must not reopen a deliberately-closed port ----

    def test_refresh_position_noop_when_not_open(self):
        controller = RecordingNosepieceController()
        controller.is_open = False
        panel = self._make_panel(controller)

        panel.refresh_position()
        get_app().processEvents()

        self.assertIsNone(panel._read_thread)
        self.assertNotIn(("read_position",), controller.calls)
        self.assertNotIn(("connect",), controller.calls)


class ObjectiveLinkBadgeTests(unittest.TestCase):
    def setUp(self):
        get_app()

    def _panel(self, controller):
        from automatic_transfer_station.objective_panel import ObjectivePanel

        registry = {}
        return ObjectivePanel(
            controller,
            register=lambda name, widget: registry.setdefault(name, widget),
            port_lister=lambda: [],
            initial_port="COM7",
        )

    def test_link_badge_shows_disconnected_on_build(self):
        panel = self._panel(RecordingNosepieceController())  # is_open=False
        label = panel.controls["objectiveLinkLabel"]
        self.assertIn("未连接", label.text())
        # Coloured pill styling makes the state unmistakable (not just grey text).
        self.assertIn("border", label.styleSheet())

    def test_link_badge_shows_connected_with_port(self):
        panel = self._panel(RecordingNosepieceController())
        panel._set_link_state(True, "COM7")
        label = panel.controls["objectiveLinkLabel"]
        self.assertIn("已连接", label.text())
        self.assertIn("COM7", label.text())
        self.assertIn("#16a34a", label.styleSheet())  # green = connected

    def test_link_badge_toggles_back_to_disconnected(self):
        panel = self._panel(RecordingNosepieceController())
        panel._set_link_state(True, "COM7")
        panel._set_link_state(False)
        label = panel.controls["objectiveLinkLabel"]
        self.assertIn("未连接", label.text())
        self.assertNotIn("已连接", label.text())


class ObjectiveDefaultPortTests(unittest.TestCase):
    def setUp(self):
        get_app()

    def test_default_com_port_is_com7_in_dialog(self):
        from automatic_transfer_station.nosepiece_controller import DEFAULT_NOSEPIECE_PORT
        from automatic_transfer_station.objective_panel import ObjectivePanel

        self.assertEqual("COM7", DEFAULT_NOSEPIECE_PORT)
        registry = {}
        panel = ObjectivePanel(
            RecordingNosepieceController(),
            register=lambda name, widget: registry.setdefault(name, widget),
            port_lister=lambda: [],
            initial_port=None,  # no explicit --nosepiece-port → default to COM7
        )
        self.assertEqual("COM7", panel.initial_port)
        panel.open_com_dialog()
        get_app().processEvents()
        self.assertEqual("COM7", panel.controls["objectivePortCombo"].currentText())

    def test_explicit_initial_port_overrides_default(self):
        from automatic_transfer_station.objective_panel import ObjectivePanel

        registry = {}
        panel = ObjectivePanel(
            RecordingNosepieceController(),
            register=lambda name, widget: registry.setdefault(name, widget),
            port_lister=lambda: [],
            initial_port="COM5",
        )
        self.assertEqual("COM5", panel.initial_port)


if __name__ == "__main__":
    unittest.main()
