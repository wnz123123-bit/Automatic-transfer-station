import os, sys
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from automatic_transfer_station.gamepad_xinput import GamepadSnapshot, BUTTON

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


def snap(**kw):
    base = dict(connected=True, packet=1, lx=0.0, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0, buttons=0)
    base.update(kw); return GamepadSnapshot(**base)


class ScriptedReader:
    def __init__(self, frames): self.frames = list(frames); self.last = frames[-1]
    def read_state(self, index=None):
        return self.frames.pop(0) if self.frames else self.last


class Clk:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class RecMotion:
    def __init__(self): self.calls = []; self.is_open = True
    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0): self.calls.append(("start_jog", axis, round(speed, 4)))
    def stop_axis(self, axis): self.calls.append(("stop_axis", axis))
    def stop_all(self): self.calls.append(("stop_all",))
    def relative_move(self, axis, direction, speed, step): self.calls.append(("relative_move", axis, direction, round(speed,4), step))


class InlineQueue:
    def submit(self, action, *, priority=10, kind="", metadata=None):
        action(); return True


class DeferredQueue:
    def __init__(self, accept=True): self.actions = []; self.accept = accept
    def submit(self, action, *, priority=10, kind="", metadata=None):
        if not self.accept:
            return False
        self.actions.append((priority, action)); return True
    def run_all(self):
        for _, a in sorted(self.actions, key=lambda pa: pa[0]):
            a()


class GamepadPanelTests(unittest.TestCase):
    def _panel(self, reader, clk, ctrl):
        get_app()
        from automatic_transfer_station.gamepad_panel import GamepadPanel
        controls = {}
        return GamepadPanel(
            register=lambda n, w: controls.setdefault(n, w),
            motion_queue_provider=lambda: InlineQueue(),
            motion_controller=ctrl,
            stop_all=ctrl.stop_all,
            speed_fn=lambda axis: 100.0,
            step_fn=lambda axis: 50,
            set_step_fn=lambda axis, v: None,
            direction_fn=lambda axis, sign: sign,
            highlight_fn=lambda axis: None,
            reader=reader, clock=clk,
        ), controls

    def test_arm_then_stick_starts_jog(self):
        clk = Clk(); ctrl = RecMotion()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0)])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick()
        panel.tick()
        self.assertIn(("start_jog", 5, 100.0), ctrl.calls)

    def test_back_stops_all_via_direct_path_not_queue(self):
        clk = Clk(); ctrl = RecMotion()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(buttons=BUTTON["BACK"])])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick(); panel.tick()
        self.assertIn(("stop_all",), ctrl.calls)

    def test_stop_all_runtime_error_is_logged(self):
        class ExplodingStopController(RecMotion):
            def stop_all(self):
                raise RuntimeError("raw stop failure")

        clk = Clk()
        ctrl = ExplodingStopController()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(buttons=BUTTON["BACK"])])
        panel, _ = self._panel(reader, clk, ctrl)

        panel.tick()
        with self.assertLogs("ats.audit", level="CRITICAL") as cm:
            panel.tick()

        self.assertIn("raw stop failure", "\n".join(cm.output))

    def test_jog_uses_relative_move_with_gui_step(self):
        clk = Clk(); ctrl = RecMotion()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(buttons=BUTTON["RB"])])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick(); panel.tick()
        self.assertIn(("relative_move", 4, 1, 100.0, 50), ctrl.calls)

    def test_x2x3_continuous_passes_partner_lookahead(self):
        clk = Clk()
        class Rec(RecMotion):
            def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
                self.calls.append(("start_jog", axis, round(speed,4), round(partner_lookahead_pulse,1)))
        ctrl = Rec()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0, rx=1.0)])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick(); panel.tick()
        looks = [c for c in ctrl.calls if c[0] == "start_jog" and c[1] in (5, 7)]
        self.assertTrue(looks)
        self.assertTrue(all(c[3] > 0 for c in looks))

    def test_partner_lookahead_is_expressed_in_pulses(self):
        """CP2 Fix 2: partner_lookahead_pulse must be in PULSES.

        It is computed as
            abs(partner_frac) * speed_fn(partner) * (update_period_s * 1000.0)
        where speed_fn (== main_window._speed == speed_to_pulse_per_ms) returns
        controller velocity in pulses/ms. Dimensionally:
            [unitless] * [pulses/ms] * [ms] = pulses.
        This pins the exact numeric value so a future unit regression (e.g.
        someone wiring speed_fn to mm/s) is caught.
        """
        clk = Clk()
        class Rec(RecMotion):
            def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
                self.calls.append(("start_jog", axis, round(speed, 4), round(partner_lookahead_pulse, 6)))
        ctrl = Rec()
        get_app()
        from automatic_transfer_station.gamepad_panel import GamepadPanel
        controls = {}
        # speed_fn returns a known pulses/ms value; both sticks fully deflected so
        # abs(partner_frac) == 1.0 and the lookahead reduces to speed * period_ms.
        SPEED_PULSES_PER_MS = 7.0
        panel = GamepadPanel(
            register=lambda n, w: controls.setdefault(n, w),
            motion_queue_provider=lambda: InlineQueue(),
            motion_controller=ctrl,
            stop_all=ctrl.stop_all,
            speed_fn=lambda axis: SPEED_PULSES_PER_MS,
            step_fn=lambda axis: 50,
            set_step_fn=lambda axis, v: None,
            direction_fn=lambda axis, sign: sign,
            highlight_fn=lambda axis: None,
            reader=ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0, rx=1.0)]),
            clock=clk,
        )
        panel.tick(); panel.tick()
        period_ms = panel._controller.cfg.update_period_s * 1000.0
        expected = round(1.0 * SPEED_PULSES_PER_MS * period_ms, 6)  # pulses
        looks = [c for c in ctrl.calls if c[0] == "start_jog" and c[1] in (5, 7)]
        self.assertTrue(looks)
        for c in looks:
            self.assertAlmostEqual(c[3], expected, places=6)

    def _panel_with_queue(self, reader, clk, ctrl, queue):
        get_app()
        from automatic_transfer_station.gamepad_panel import GamepadPanel
        controls = {}
        return GamepadPanel(
            register=lambda n, w: controls.setdefault(n, w),
            motion_queue_provider=lambda: queue,
            motion_controller=ctrl,
            stop_all=ctrl.stop_all,
            speed_fn=lambda axis: 100.0,
            step_fn=lambda axis: 50,
            set_step_fn=lambda axis, v: None,
            direction_fn=lambda axis, sign: sign,
            highlight_fn=lambda axis: None,
            reader=reader, clock=clk,
        )

    def test_stop_invalidates_pending_continuous(self):
        clk = Clk(); ctrl = RecMotion(); q = DeferredQueue()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0), snap(lx=0.0)])
        panel = self._panel_with_queue(reader, clk, ctrl, q)
        panel.tick()   # arm
        panel.tick()   # lx=1.0 -> StartContinuous(5) queued (deferred)
        panel.tick()   # lx=0.0 -> StopContinuous(5) queued, epoch bumped
        q.run_all()    # priority order: stop(0) then start(5)
        self.assertNotIn(("start_jog", 5, 100.0), ctrl.calls)   # stale continuous skipped
        self.assertIn(("stop_axis", 5), ctrl.calls)

    def test_stop_continuous_direct_fallback_when_queue_rejects(self):
        clk = Clk(); ctrl = RecMotion(); q = DeferredQueue(accept=False)
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0), snap(lx=0.0)])
        panel = self._panel_with_queue(reader, clk, ctrl, q)
        panel.tick(); panel.tick(); panel.tick()
        self.assertIn(("stop_axis", 5), ctrl.calls)   # submit rejected -> direct stop_axis

    def test_stop_all_invalidates_pending_continuous(self):
        clk = Clk(); ctrl = RecMotion(); q = DeferredQueue()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0), snap(buttons=BUTTON["BACK"])])
        panel = self._panel_with_queue(reader, clk, ctrl, q)
        panel.tick(); panel.tick()   # arm, then StartContinuous(5) queued
        panel.tick()                 # BACK -> StopAll (direct) bumps all epochs
        q.run_all()
        self.assertNotIn(("start_jog", 5, 100.0), ctrl.calls)
        self.assertIn(("stop_all",), ctrl.calls)

    # ---- Package 7 Test 2: disarm() body end-to-end ----

    def test_disarm_ends_continuous_jog_and_resets_all_state(self):
        """Calling the real disarm() (not a stub) must:
        1. Set controller.enabled = False
        2. Un-check the enable button
        3. Issue stop_all to the motion controller (via _safe_stop_all)
        4. Invalidate the continuous epoch so a stale lambda is skipped
        5. Fire on_armed_changed(False)
        """
        clk = Clk()
        ctrl = RecMotion()
        # Start with axis 5 in a live continuous jog: use a deferred queue so
        # the stale continuous lambda can be checked after disarm.
        q = DeferredQueue()
        armed_events = []

        get_app()
        from automatic_transfer_station.gamepad_panel import GamepadPanel
        controls = {}
        panel = GamepadPanel(
            register=lambda n, w: controls.setdefault(n, w),
            motion_queue_provider=lambda: q,
            motion_controller=ctrl,
            stop_all=ctrl.stop_all,
            speed_fn=lambda axis: 100.0,
            step_fn=lambda axis: 50,
            set_step_fn=lambda axis, v: None,
            direction_fn=lambda axis, sign: sign,
            highlight_fn=lambda axis: None,
            on_armed_changed=lambda armed: armed_events.append(armed),
            reader=ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0)]),
            clock=clk,
        )

        # Arm the panel and enqueue a continuous jog for axis 5.
        panel.tick()  # START -> arm
        panel.tick()  # lx=1.0 -> StartContinuous(5) queued
        # Capture the epoch before disarm for later comparison.
        epoch_before = panel._cont_epoch.get(5, 0)

        # --- Exercise the real disarm() ---
        panel.disarm()

        # 1. controller.enabled must be False
        self.assertFalse(panel._controller.enabled, "controller.enabled must be False after disarm()")

        # 2. enable button un-checked
        enable_btn = controls.get("btnGamepadEnable")
        self.assertIsNotNone(enable_btn)
        self.assertFalse(enable_btn.isChecked(), "enable button must be unchecked after disarm()")

        # 3. stop_all reached the motion controller
        self.assertIn(("stop_all",), ctrl.calls, "stop_all must be issued to the motion controller")

        # 4. The continuous epoch for axis 5 must have been bumped (stale lambda guard).
        epoch_after = panel._cont_epoch.get(5, 0)
        self.assertGreater(epoch_after, epoch_before,
                           "disarm() must invalidate the continuous epoch so stale lambdas are skipped")
        # Confirm: running the deferred queue now skips the stale jog.
        pre_count = len(ctrl.calls)
        q.run_all()
        jog_calls_after = [c for c in ctrl.calls[pre_count:] if c[0] == "start_jog"]
        self.assertEqual([], jog_calls_after, "stale continuous lambda must be skipped after epoch bump")

        # 5. on_armed_changed(False) was fired
        self.assertIn(False, armed_events, "on_armed_changed(False) must be fired by disarm()")


if __name__ == "__main__":
    unittest.main()
