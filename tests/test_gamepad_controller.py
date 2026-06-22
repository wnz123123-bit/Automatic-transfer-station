import sys
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from automatic_transfer_station.gamepad_xinput import GamepadSnapshot, BUTTON


def snap(**kw):
    base = dict(connected=True, packet=1, lx=0.0, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0, buttons=0)
    base.update(kw)
    return GamepadSnapshot(**base)


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class GamepadControllerTests(unittest.TestCase):
    def test_disabled_ignores_everything_but_start(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, ToggleEnable
        c = GamepadController(now=Clock())
        self.assertEqual(c.poll(snap(lx=1.0)), [])
        out = c.poll(snap(buttons=BUTTON["START"]))
        self.assertTrue(any(isinstance(i, ToggleEnable) and i.enabled for i in out))
        self.assertTrue(c.enabled)

    def test_stick_proportional_start_update_stop(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, StartContinuous, UpdateContinuous, StopContinuous,
        )
        clk = Clock()
        c = GamepadController(now=clk)
        c.enabled = True
        out = c.poll(snap(lx=1.0))
        starts = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        self.assertEqual(len(starts), 1)
        self.assertGreater(starts[0].frac, 0)
        clk.advance(0.02)
        self.assertFalse([i for i in c.poll(snap(lx=1.0)) if isinstance(i, UpdateContinuous)])
        clk.advance(0.10)
        self.assertTrue([i for i in c.poll(snap(lx=0.5)) if isinstance(i, UpdateContinuous) and i.axis == 5])
        self.assertTrue([i for i in c.poll(snap(lx=0.0)) if isinstance(i, StopContinuous) and i.axis == 5])

    def test_deadzone_and_curve(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StartContinuous
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertEqual([i for i in c.poll(snap(lx=0.10)) if isinstance(i, StartContinuous)], [])
        out = c.poll(snap(lx=0.20))
        s = [i for i in out if isinstance(i, StartContinuous)][0]
        self.assertGreater(s.frac, 0.0)
        self.assertLess(s.frac, 0.20)

    def test_trigger_tap_steps_immediately_hold_goes_continuous(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, Jog, StartContinuous, StopContinuous, GamepadConfig,
        )
        clk = Clock()
        c = GamepadController(GamepadConfig(), now=clk); c.enabled = True
        out = c.poll(snap(rt=1.0))
        self.assertIn(Jog(3, +1), out)
        clk.advance(0.1); self.assertEqual(c.poll(snap(rt=1.0)), [])
        clk.advance(0.25)
        out = c.poll(snap(rt=1.0))
        self.assertTrue([i for i in out if isinstance(i, StartContinuous) and i.axis == 3 and i.frac > 0])
        self.assertIn(StopContinuous(3), c.poll(snap(rt=0.0)))

    def test_bumper_tap_steps_and_hold_continuous_axis4(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog, StartContinuous
        from automatic_transfer_station.gamepad_xinput import BUTTON
        clk = Clock(); c = GamepadController(now=clk); c.enabled = True
        out = c.poll(snap(buttons=BUTTON["RB"]))
        self.assertIn(Jog(4, +1), out)
        clk.advance(0.35)
        self.assertTrue([i for i in c.poll(snap(buttons=BUTTON["RB"])) if isinstance(i, StartContinuous) and i.axis == 4])

    # ---- 2026-06-22 remap: D-pad up/down = step-jog, D-pad left/right = step size,
    #      face buttons Y/A/X/B = 2-D directional axis selection ----

    def test_dpad_up_down_single_step_jogs_selected_axis(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog, SELECTOR_AXES
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertIn(Jog(SELECTOR_AXES[0], +1), c.poll(snap(buttons=BUTTON["DPAD_UP"])))
        self.assertIn(Jog(SELECTOR_AXES[0], -1), c.poll(snap(buttons=BUTTON["DPAD_DOWN"])))

    def test_dpad_up_down_never_goes_continuous(self):
        # "仅步长，不连续": holding D-pad up past the hold delay emits no continuous intent.
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, StartContinuous, UpdateContinuous, Jog)
        clk = Clock(); c = GamepadController(now=clk); c.enabled = True
        first = c.poll(snap(buttons=BUTTON["DPAD_UP"]))
        self.assertEqual(1, len([i for i in first if isinstance(i, Jog)]))
        clk.advance(0.5)
        out = c.poll(snap(buttons=BUTTON["DPAD_UP"]))   # still held, well past hold_delay
        self.assertFalse([i for i in out if isinstance(i, (StartContinuous, UpdateContinuous))])
        self.assertFalse([i for i in out if isinstance(i, Jog)])  # no auto-repeat, edge-only

    def test_dpad_left_right_changes_step_size_on_selected_axis(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StepDelta, SELECTOR_AXES
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertIn(StepDelta(SELECTOR_AXES[0], +1), c.poll(snap(buttons=BUTTON["DPAD_RIGHT"])))
        self.assertIn(StepDelta(SELECTOR_AXES[0], -1), c.poll(snap(buttons=BUTTON["DPAD_LEFT"])))

    def test_dpad_up_jog_suppressed_when_selected_axis_continuous(self):
        # A one-shot D-pad step must not fight a continuous owner (e.g. the stick).
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog, StartContinuous
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(lx=1.0))  # left stick drives axis 5 continuous
        self.assertTrue([i for i in out if isinstance(i, StartContinuous) and i.axis == 5])
        out2 = c.poll(snap(lx=1.0, buttons=BUTTON["DPAD_UP"]))
        self.assertFalse([i for i in out2 if isinstance(i, Jog) and i.axis == 5])

    def test_directional_neighbor_geometry(self):
        # Navigation follows the GUI card grid (SELECTOR_GRID_POSITIONS):
        #   col0 吸附台: X3(5) Y3(6) R(9) X1(1)   col1 载玻片侧: X2(7) Y2(8) Z(4) Y1(2)
        from automatic_transfer_station.gamepad_controller import directional_neighbor
        # X3(5) is top-left: up/left -> nothing; down -> Y3(6); right -> X2(7).
        self.assertIsNone(directional_neighbor(5, "up"))
        self.assertIsNone(directional_neighbor(5, "left"))
        self.assertEqual(6, directional_neighbor(5, "down"))
        self.assertEqual(7, directional_neighbor(5, "right"))
        # Z(4) is the right column, row 2: up -> Y2(8); down -> Y1(2); left -> R(9); right -> None.
        self.assertEqual(8, directional_neighbor(4, "up"))
        self.assertEqual(2, directional_neighbor(4, "down"))
        self.assertEqual(9, directional_neighbor(4, "left"))
        self.assertIsNone(directional_neighbor(4, "right"))
        # R(9) left column row 2: up -> Y3(6); down -> X1(1); right -> Z(4).
        self.assertEqual(6, directional_neighbor(9, "up"))
        self.assertEqual(1, directional_neighbor(9, "down"))
        self.assertEqual(4, directional_neighbor(9, "right"))
        # X1(1) bottom-left (整体位移): down/left -> None; up -> R(9); right -> Y1(2).
        self.assertIsNone(directional_neighbor(1, "down"))
        self.assertEqual(9, directional_neighbor(1, "up"))
        self.assertEqual(2, directional_neighbor(1, "right"))

    def test_directional_neighbor_excludes_microscope_z(self):
        # MicroscopeZ (axis 3) is never selectable, even though it sits highest on the map.
        from automatic_transfer_station.gamepad_controller import directional_neighbor
        for direction in ("up", "down", "left", "right"):
            for axis in (5, 6, 7, 8, 9, 4, 1, 2):
                self.assertNotEqual(3, directional_neighbor(axis, direction))

    def test_face_buttons_2d_select_and_update_index(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, SelectAxis, SELECTOR_AXES
        c = GamepadController(now=Clock()); c.enabled = True
        # default selected = X3 (5), top-left. B = right -> X2 (7).
        out = c.poll(snap(buttons=BUTTON["B"]))
        self.assertIn(SelectAxis(7), out)
        self.assertEqual(7, SELECTOR_AXES[c.selected_idx])
        c.poll(snap(buttons=0))                      # release so the next edge registers
        out2 = c.poll(snap(buttons=BUTTON["A"]))     # A = down from X2 (7) -> Y2 (8)
        self.assertIn(SelectAxis(8), out2)
        self.assertEqual(8, SELECTOR_AXES[c.selected_idx])

    def test_face_button_noop_when_no_neighbor(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, SelectAxis, SELECTOR_AXES
        c = GamepadController(now=Clock()); c.enabled = True
        # X3 (5) is top-left, so UP (and LEFT) have no neighbour -> no-op.
        self.assertEqual(5, SELECTOR_AXES[c.selected_idx])
        out = c.poll(snap(buttons=BUTTON["Y"]))                      # X3 up -> nothing
        self.assertFalse([i for i in out if isinstance(i, SelectAxis)])
        self.assertEqual(5, SELECTOR_AXES[c.selected_idx])
        c.poll(snap(buttons=0))
        out2 = c.poll(snap(buttons=BUTTON["X"]))                     # X3 left -> nothing
        self.assertFalse([i for i in out2 if isinstance(i, SelectAxis)])
        self.assertEqual(5, SELECTOR_AXES[c.selected_idx])

    # ---- remap review follow-ups: invariants the deleted tests used to guard ----

    def test_same_tick_stick_continuous_suppresses_dpad_up_jog(self):
        # Single poll where the stick deflection AND the D-pad press arrive together:
        # _handle_sticks runs before _handle_dpad, so axis 5 is already continuous when
        # the D-pad UP is processed -> no conflicting one-shot Jog. Pins the handler order.
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, Jog, StartContinuous)
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(lx=1.0, buttons=BUTTON["DPAD_UP"]))
        self.assertEqual(1, len([i for i in out if isinstance(i, StartContinuous) and i.axis == 5]))
        self.assertFalse([i for i in out if isinstance(i, Jog) and i.axis == 5])

    def test_dpad_jog_and_step_follow_the_selected_axis(self):
        # The load-bearing intent: D-pad up/down jogs and left/right steps WHICHEVER axis
        # is currently selected — not a hardcoded default. Select X2(7) via a face button.
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, Jog, StepDelta, SelectAxis)
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertIn(SelectAxis(7), c.poll(snap(buttons=BUTTON["B"])))  # X3 -> right -> X2(7)
        c.poll(snap(buttons=0))                                          # release
        self.assertIn(Jog(7, +1), c.poll(snap(buttons=BUTTON["DPAD_UP"])))
        c.poll(snap(buttons=0))
        self.assertIn(Jog(7, -1), c.poll(snap(buttons=BUTTON["DPAD_DOWN"])))
        c.poll(snap(buttons=0))
        self.assertIn(StepDelta(7, +1), c.poll(snap(buttons=BUTTON["DPAD_RIGHT"])))

    def test_dpad_step_size_not_suppressed_during_continuous_and_edge_only(self):
        # Step-size (D-pad left/right) is independent of motion: it must still fire while
        # the selected axis is continuous, and it must be edge-only (no auto-repeat).
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, StepDelta, StartContinuous)
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(lx=1.0))  # axis 5 continuous via stick
        self.assertTrue([i for i in out if isinstance(i, StartContinuous) and i.axis == 5])
        out2 = c.poll(snap(lx=1.0, buttons=BUTTON["DPAD_RIGHT"]))
        self.assertIn(StepDelta(5, +1), out2)                # NOT suppressed during continuous
        out3 = c.poll(snap(lx=1.0, buttons=BUTTON["DPAD_RIGHT"]))  # still held
        self.assertFalse([i for i in out3 if isinstance(i, StepDelta)])  # edge-only

    def test_face_button_selects_on_same_tick_as_arming_and_persists_across_rearm(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, SelectAxis, ToggleEnable, SELECTOR_AXES)
        c = GamepadController(now=Clock())  # starts disabled
        # START + B in one packet: arm AND select-right in the same tick (X3 -> X2/7).
        out = c.poll(snap(buttons=BUTTON["START"] | BUTTON["B"]))
        self.assertTrue(any(isinstance(i, ToggleEnable) and i.enabled for i in out))
        self.assertIn(SelectAxis(7), out)
        self.assertEqual(7, SELECTOR_AXES[c.selected_idx])
        # Disarm then re-arm: selection persists (reset() does not clear selected_idx).
        c.poll(snap(buttons=0))
        c.poll(snap(buttons=BUTTON["START"]))                 # disarm
        c.poll(snap(buttons=0))
        c.poll(snap(buttons=BUTTON["START"]))                 # re-arm
        self.assertEqual(7, SELECTOR_AXES[c.selected_idx])

    def test_simultaneous_dpad_and_face_button_combinations(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, Jog, StepDelta, SelectAxis)
        # UP+DOWN in one packet -> only the UP jog (if/elif), never both.
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(buttons=BUTTON["DPAD_UP"] | BUTTON["DPAD_DOWN"]))
        self.assertIn(Jog(5, +1), out)
        self.assertNotIn(Jog(5, -1), out)
        # UP+RIGHT -> both a jog and a step-size change (separate if-blocks).
        c2 = GamepadController(now=Clock()); c2.enabled = True
        out2 = c2.poll(snap(buttons=BUTTON["DPAD_UP"] | BUTTON["DPAD_RIGHT"]))
        self.assertIn(Jog(5, +1), out2)
        self.assertIn(StepDelta(5, +1), out2)
        # A+B (down + right, both valid from top-left X3) -> only ONE SelectAxis: the
        # 'down' branch wins via if/elif order (Y, A, X, B).
        c3 = GamepadController(now=Clock()); c3.enabled = True
        out3 = c3.poll(snap(buttons=BUTTON["A"] | BUTTON["B"]))
        selects = [i for i in out3 if isinstance(i, SelectAxis)]
        self.assertEqual(1, len(selects))
        self.assertEqual(6, selects[0].axis)   # down from X3(5) -> Y3(6), NOT right->X2(7)
        # DPAD_DOWN + B same tick: D-pad jogs the OLD selected axis (5); selection then
        # moves right to X2(7). Handler order (_handle_dpad before _handle_selector) is the contract.
        c4 = GamepadController(now=Clock()); c4.enabled = True
        out4 = c4.poll(snap(buttons=BUTTON["DPAD_DOWN"] | BUTTON["B"]))
        self.assertIn(Jog(5, -1), out4)        # jog on the pre-change axis
        self.assertIn(SelectAxis(7), out4)     # selection advances same tick (X3 -> X2)

    def test_back_stops_all(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StopAll
        from automatic_transfer_station.gamepad_xinput import BUTTON
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertIn(StopAll(), c.poll(snap(buttons=BUTTON["BACK"])))

    def test_disconnect_stops_and_disarms(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StopAll, ToggleEnable
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(connected=False))
        self.assertIn(StopAll(), out)
        self.assertTrue(any(isinstance(i, ToggleEnable) and not i.enabled for i in out))
        self.assertFalse(c.enabled)

    def test_inactivity_auto_disarms(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, ToggleEnable, StopAll,
        )
        clk = Clock()
        c = GamepadController(GamepadConfig(inactivity_disarm_s=5.0), now=clk)
        c.enabled = True
        c.poll(snap(lx=1.0))
        clk.advance(3.0); c.poll(snap())
        self.assertTrue(c.enabled)
        clk.advance(3.0)
        out = c.poll(snap())
        self.assertFalse(c.enabled)
        self.assertIn(StopAll(), out)
        self.assertTrue(any(isinstance(i, ToggleEnable) and not i.enabled for i in out))


    def test_held_button_across_rearm_does_not_phantom_jog(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog, ToggleEnable
        c = GamepadController(now=Clock()); c.enabled = True
        c.poll(snap(buttons=BUTTON["RB"]))                              # genuine press -> Jog(4,+1)
        c.poll(snap(buttons=BUTTON["RB"] | BUTTON["START"]))           # START edge -> disable
        c.poll(snap(buttons=BUTTON["RB"]))                             # disabled; RB still held
        out = c.poll(snap(buttons=BUTTON["RB"] | BUTTON["START"]))    # re-arm via START, RB still held
        self.assertTrue(any(isinstance(i, ToggleEnable) and i.enabled for i in out))
        self.assertFalse([i for i in out if isinstance(i, Jog)])       # no phantom step

    # ---- Fix 3: trigger gate vs deadzone (near-zero crawl) ----

    def test_fix3_trigger_below_gate_gives_zero_frac(self):
        """Fix 3: A trigger value below the gate (trigger_threshold) produces no continuous."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        # Below trigger_threshold (which should now equal deadzone): no continuous
        below = cfg.trigger_threshold - 0.01
        out = c.poll(snap(rt=below))
        starts = [i for i in out if isinstance(i, StartContinuous) and i.axis == 3]
        self.assertFalse(starts)

    def test_fix3_trigger_at_gate_produces_sensible_frac(self):
        """Fix 3: A trigger at moderate deflection (0.5) produces a non-tiny frac, not near-zero."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous, Jog)
        clk = Clock()
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=clk)
        c.enabled = True
        # First tick: tap (Jog) at moderate value
        out = c.poll(snap(rt=0.5))
        self.assertIn(Jog(3, +1), out)
        # Hold past delay; check that continuous frac is sensible (> 0.01) and < 1.0
        clk.advance(0.35)
        out2 = c.poll(snap(rt=0.5))
        starts = [i for i in out2 if isinstance(i, StartContinuous) and i.axis == 3]
        if starts:
            frac = starts[0].frac
            self.assertGreater(frac, 0.01, f"frac too small (near-zero crawl): {frac}")
            self.assertLess(frac, 1.0)

    def test_fix3_trigger_gate_equals_deadzone(self):
        """Fix 3: trigger_threshold default must equal deadzone default (gate/rescale aligned)."""
        from automatic_transfer_station.gamepad_controller import GamepadConfig
        cfg = GamepadConfig()
        self.assertEqual(cfg.trigger_threshold, cfg.deadzone,
                         "trigger_threshold should equal deadzone so gate and rescale coincide")

    # ---- Fix 5: remove dead `or -0.0` branch ----

    def test_fix5_lt_negative_frac_is_not_positive_zero(self):
        """Fix 5: LT held returns a negative frac (not -0.0 masking as 0.0 if _curve returned 0)."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous, Jog)
        clk = Clock()
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=clk)
        c.enabled = True
        lt_val = 0.5  # well past deadzone
        out = c.poll(snap(lt=lt_val))
        self.assertIn(Jog(3, -1), out)
        clk.advance(0.35)
        out2 = c.poll(snap(lt=lt_val))
        starts = [i for i in out2 if isinstance(i, StartContinuous) and i.axis == 3]
        if starts:
            self.assertLess(starts[0].frac, 0, "LT continuous frac should be negative")


    # ---- BP6 Fix 1+2: radial (circular) deadzone for stick pairs ----

    def test_bp6_radial_deadzone_inside_kills_both_axes(self):
        """BP6 Fix 1: lx=0.08, ly=0.08 → m≈0.113 ≤ deadzone(0.12) → both axes 5 and 6 are zero."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        cfg = GamepadConfig(deadzone=0.12)
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        import math
        lx, ly = 0.08, 0.08
        # sanity: magnitude is inside the radial deadzone
        self.assertLessEqual(math.hypot(lx, ly), cfg.deadzone)
        out = c.poll(snap(lx=lx, ly=ly))
        starts5 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        starts6 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 6]
        self.assertFalse(starts5, f"axis 5 started inside radial deadzone: {out}")
        self.assertFalse(starts6, f"axis 6 started inside radial deadzone: {out}")

    def test_bp6_cardinal_push_lx_matches_single_axis_curve_and_ly_zero(self):
        """BP6 Fix 1: Pure cardinal lx=1.0, ly=0.0 → axis 5 frac identical to old per-axis
        result; axis 6 frac = 0. Radial reduces to per-axis for cardinal input."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous, _curve)
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        out = c.poll(snap(lx=1.0, ly=0.0))
        starts5 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        starts6 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 6]
        self.assertTrue(starts5, "axis 5 should start for lx=1.0")
        expected_frac = _curve(1.0, cfg)
        self.assertAlmostEqual(starts5[0].frac, expected_frac, places=10,
                               msg="radial cardinal should match per-axis curve exactly")
        self.assertFalse(starts6, "axis 6 should be zero for purely cardinal lx push")

    def test_bp6_diagonal_full_deflection_magnitude_bounded(self):
        """BP6 Fix 2: lx=1.0, ly=1.0 → hypot(frac5, frac6) ≤ 1.0 + epsilon (NOT ~1.41)."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        import math
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        out = c.poll(snap(lx=1.0, ly=1.0))
        starts5 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        starts6 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 6]
        self.assertTrue(starts5, "axis 5 should start for lx=1.0")
        self.assertTrue(starts6, "axis 6 should start for ly=1.0")
        combined_m = math.hypot(starts5[0].frac, starts6[0].frac)
        self.assertLessEqual(combined_m, 1.0 + 1e-9,
                             f"diagonal magnitude {combined_m:.4f} exceeds 1.0 (Fix 2 violated)")

    def test_bp6_diagonal_outside_deadzone_nonzero_both(self):
        """BP6 Fix 1: A diagonal push outside the radial deadzone yields nonzero on both axes."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        import math
        cfg = GamepadConfig(deadzone=0.12)
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        # lx=ly=0.15 → m≈0.212 > 0.12: outside radial deadzone
        lx = ly = 0.15
        self.assertGreater(math.hypot(lx, ly), cfg.deadzone)
        out = c.poll(snap(lx=lx, ly=ly))
        starts5 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        starts6 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 6]
        self.assertTrue(starts5, "axis 5 should start outside radial deadzone")
        self.assertTrue(starts6, "axis 6 should start outside radial deadzone")

    def test_bp6_right_stick_radial_deadzone_inside_kills_both_axes(self):
        """BP6 Fix 1: Right stick rx=0.07, ry=0.07 inside radial deadzone → axes 7 and 8 zero."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        import math
        cfg = GamepadConfig(deadzone=0.12)
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        rx, ry = 0.07, 0.07
        self.assertLessEqual(math.hypot(rx, ry), cfg.deadzone)
        out = c.poll(snap(rx=rx, ry=ry))
        starts7 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 7]
        starts8 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 8]
        self.assertFalse(starts7, f"axis 7 started inside radial deadzone: {out}")
        self.assertFalse(starts8, f"axis 8 started inside radial deadzone: {out}")

    def test_bp6_right_stick_cardinal_matches_curve(self):
        """BP6 Fix 1: Pure cardinal rx=1.0, ry=0.0 → axis 7 frac matches _curve(1.0); axis 8 zero."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous, _curve)
        cfg = GamepadConfig()
        c = GamepadController(cfg, now=Clock())
        c.enabled = True
        out = c.poll(snap(rx=1.0, ry=0.0))
        starts7 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 7]
        starts8 = [i for i in out if isinstance(i, StartContinuous) and i.axis == 8]
        self.assertTrue(starts7)
        self.assertAlmostEqual(starts7[0].frac, _curve(1.0, cfg), places=10)
        self.assertFalse(starts8)

    # ---- BP6 Fix 3: explicit trigger_deadzone field ----

    def test_bp6_fix3_trigger_deadzone_field_exists_and_defaults_to_trigger_threshold(self):
        """BP6 Fix 3: GamepadConfig has trigger_deadzone defaulting to trigger_threshold."""
        from automatic_transfer_station.gamepad_controller import GamepadConfig
        cfg = GamepadConfig()
        self.assertTrue(hasattr(cfg, "trigger_deadzone"),
                        "GamepadConfig must have trigger_deadzone field")
        self.assertEqual(cfg.trigger_deadzone, cfg.trigger_threshold,
                         "trigger_deadzone default must equal trigger_threshold")

    def test_bp6_fix3_trigger_deadzone_decoupled_from_thumb_deadzone(self):
        """BP6 Fix 3: Changing thumb deadzone alone must NOT shift trigger response frac."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous, Jog)
        import math
        clk1, clk2 = Clock(), Clock()
        # Config A: default deadzone + default trigger_deadzone
        cfg_a = GamepadConfig(deadzone=0.12, trigger_deadzone=0.12)
        # Config B: larger thumb deadzone but same trigger_deadzone
        cfg_b = GamepadConfig(deadzone=0.30, trigger_deadzone=0.12)
        c1 = GamepadController(cfg_a, now=clk1); c1.enabled = True
        c2 = GamepadController(cfg_b, now=clk2); c2.enabled = True

        rt_val = 0.5
        # advance past hold delay for both
        c1.poll(snap(rt=rt_val)); clk1.advance(0.35)
        c2.poll(snap(rt=rt_val)); clk2.advance(0.35)
        out1 = c1.poll(snap(rt=rt_val))
        out2 = c2.poll(snap(rt=rt_val))

        frac1 = next((i.frac for i in out1 if isinstance(i, StartContinuous) and i.axis == 3), None)
        frac2 = next((i.frac for i in out2 if isinstance(i, StartContinuous) and i.axis == 3), None)

        # Both must have produced a continuous (both configs have same trigger_deadzone)
        self.assertIsNotNone(frac1, f"cfg_a produced no continuous: {out1}")
        self.assertIsNotNone(frac2, f"cfg_b produced no continuous: {out2}")
        # Fracs must be equal (trigger_deadzone is the same, only thumb deadzone differs)
        self.assertAlmostEqual(frac1, frac2, places=10,
                               msg="trigger frac must not change when only thumb deadzone changes")

    def test_bp6_fix3_trigger_uses_trigger_deadzone_not_thumb(self):
        """BP6 Fix 3: trigger rescale uses trigger_deadzone; setting a wider trigger_deadzone
        vs a narrower one shifts the frac (trigger_deadzone is truly in play)."""
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, StartContinuous)
        clk_narrow, clk_wide = Clock(), Clock()
        cfg_narrow = GamepadConfig(deadzone=0.12, trigger_threshold=0.12, trigger_deadzone=0.12)
        cfg_wide   = GamepadConfig(deadzone=0.12, trigger_threshold=0.12, trigger_deadzone=0.25)
        c_n = GamepadController(cfg_narrow, now=clk_narrow); c_n.enabled = True
        c_w = GamepadController(cfg_wide,   now=clk_wide);   c_w.enabled = True

        rt_val = 0.6
        c_n.poll(snap(rt=rt_val)); clk_narrow.advance(0.35)
        c_w.poll(snap(rt=rt_val)); clk_wide.advance(0.35)
        out_n = c_n.poll(snap(rt=rt_val))
        out_w = c_w.poll(snap(rt=rt_val))
        frac_n = next((i.frac for i in out_n if isinstance(i, StartContinuous) and i.axis == 3), None)
        frac_w = next((i.frac for i in out_w if isinstance(i, StartContinuous) and i.axis == 3), None)
        self.assertIsNotNone(frac_n)
        self.assertIsNotNone(frac_w)
        # wider trigger_deadzone → smaller scaled → smaller frac (for same input)
        self.assertGreater(frac_n, frac_w,
                           f"narrow trigger_deadzone should give larger frac: {frac_n} vs {frac_w}")

    def test_selector_order_excludes_microscope_z(self):
        from automatic_transfer_station.gamepad_controller import SELECTOR_AXES, GAMEPAD_AXES

        # Selectable axes (face-button 2D selector): X3,Y3,X2,Y2,R,Z,X1,Y1 — the
        # 2026-06-22 remap added X1(1)/Y1(2). MicroscopeZ (3) is NOT selectable.
        self.assertEqual((5, 6, 7, 8, 9, 4, 1, 2), SELECTOR_AXES)
        self.assertNotIn(3, SELECTOR_AXES)
        self.assertIn(1, SELECTOR_AXES)
        self.assertIn(2, SELECTOR_AXES)
        # MicroscopeZ (3) is still a gamepad-driven axis (triggers) for arbitration/stop;
        # X1(1)/Y1(2) are now driven too, so they must be in GAMEPAD_AXES as well.
        self.assertIn(3, GAMEPAD_AXES)
        self.assertIn(4, GAMEPAD_AXES)
        self.assertIn(1, GAMEPAD_AXES)
        self.assertIn(2, GAMEPAD_AXES)

    # ---- real-hardware: held control with a constant packet must NOT disarm ----

    def test_held_stick_with_constant_packet_does_not_disarm(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, ToggleEnable

        c = GamepadController(now=Clock())
        c.enabled = True
        # Hold the left stick fully deflected with an UNCHANGING packet number for
        # many ticks (a steady real-hardware hold — XInput keeps dwPacketNumber
        # constant when the value doesn't change). It must stay armed.
        for _ in range(20):
            out = c.poll(snap(packet=1, lx=1.0))
            self.assertFalse([i for i in out if isinstance(i, ToggleEnable) and not i.enabled])
        self.assertTrue(c.enabled)

    def test_held_trigger_with_constant_packet_does_not_disarm(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, ToggleEnable

        c = GamepadController(now=Clock())
        c.enabled = True
        for _ in range(20):
            out = c.poll(snap(packet=1, rt=1.0))
            self.assertFalse([i for i in out if isinstance(i, ToggleEnable) and not i.enabled])
        self.assertTrue(c.enabled)


if __name__ == "__main__":
    unittest.main()
