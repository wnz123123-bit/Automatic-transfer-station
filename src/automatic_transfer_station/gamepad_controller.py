from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .axis_calibration import SELECTOR_GRID_POSITIONS
from .gamepad_xinput import BUTTON, GamepadSnapshot


# ---- intents -------------------------------------------------------------
@dataclass(frozen=True)
class StartContinuous:
    axis: int
    frac: float


@dataclass(frozen=True)
class UpdateContinuous:
    axis: int
    frac: float


@dataclass(frozen=True)
class StopContinuous:
    axis: int


@dataclass(frozen=True)
class Jog:
    axis: int
    direction: int


@dataclass(frozen=True)
class SelectAxis:
    axis: int


@dataclass(frozen=True)
class StepDelta:
    axis: int
    delta: int


@dataclass(frozen=True)
class StopAll:
    pass


@dataclass(frozen=True)
class ToggleEnable:
    enabled: bool


@dataclass
class GamepadConfig:
    deadzone: float = 0.12
    curve_exp: float = 2.0
    hold_delay_s: float = 0.30
    update_period_s: float = 0.10
    # BP4 Fix 3: trigger_threshold set equal to deadzone so the gate and _curve rescale
    # coincide.  Previously 0.15 > 0.12 created a narrow "gated-on but near-zero-frac"
    # sliver; now a trigger that passes the gate immediately rescales from the same
    # deadzone edge as every other axis, so frac starts at a sensible magnitude.
    trigger_threshold: float = 0.12
    inactivity_disarm_s: float = 60.0
    # BP6 Fix 3: explicit trigger deadzone for the proportional curve rescale.
    # Defaults to trigger_threshold so existing behaviour is preserved when only
    # trigger_threshold is customised.  Decouples the trigger curve from the thumb
    # stick deadzone so they can be tuned independently.
    trigger_deadzone: float = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.trigger_deadzone is None:
            self.trigger_deadzone = self.trigger_threshold


# Gamepad-selectable axes (the face-button 2D selector picks among these, and the
# D-pad up/down step-jog drives whichever of these is selected). MicroscopeZ (axis
# 3) is intentionally NOT here: it is driven by the triggers (LT/RT), so it must
# not also be a selectable axis. The 2026-06-22 remap added X1(1)/Y1(2) and moved
# selection from a 1-D B/X cycle to a 2-D directional pick by stage-map geometry;
# the tuple ORDER no longer drives navigation (geometry does), but index 0 is the
# default-selected axis on first arm (X3).
SELECTOR_AXES = (5, 6, 7, 8, 9, 4, 1, 2)  # X3, Y3, X2, Y2, R, Z, X1, Y1

# Every axis the gamepad can drive (sticks 5/6/7/8, triggers 3, bumpers 4, plus the
# selector set 1/2/4/5/6/7/8/9). Used for GUI-jog arbitration (disable while armed)
# and for cancelling pending continuous jogs on an emergency stop — both of which
# must also cover MicroscopeZ (axis 3) even though it isn't selectable.
GAMEPAD_AXES = (5, 6, 7, 8, 9, 3, 4, 1, 2)


def directional_neighbor(current_axis, direction, axes=SELECTOR_AXES,
                         positions=SELECTOR_GRID_POSITIONS):
    """Pick the selectable axis in `direction` ("up"/"down"/"left"/"right") nearest
    to `current_axis` by the GUI card-grid layout (SELECTOR_GRID_POSITIONS), or None
    if there is none on that side.

    Grid convention: +x (col) is right, +y (row) is DOWN — matching how the operator
    reads the on-screen axis-card list (up/down within a column, left/right between
    columns). Among the axes strictly on the pressed side, the winner minimises
    ``primary + 2*ortho`` where `primary` is the distance along the pressed direction
    and `ortho` is the perpendicular offset, so the same-column/row neighbour wins.
    Pure function (positions injectable) for testability; no wrap-around.
    """
    if current_axis not in positions:
        return None
    cx, cy = positions[current_axis]
    best = None
    best_score = None
    for axis in axes:
        if axis == current_axis or axis not in positions:
            continue
        x, y = positions[axis]
        dx, dy = x - cx, y - cy
        if direction == "up":
            if dy >= 0:
                continue
            primary, ortho = -dy, abs(dx)
        elif direction == "down":
            if dy <= 0:
                continue
            primary, ortho = dy, abs(dx)
        elif direction == "left":
            if dx >= 0:
                continue
            primary, ortho = -dx, abs(dy)
        elif direction == "right":
            if dx <= 0:
                continue
            primary, ortho = dx, abs(dy)
        else:
            return None
        score = primary + 2.0 * ortho
        if best_score is None or score < best_score:
            best_score = score
            best = axis
    return best


def _curve(value: float, cfg: GamepadConfig) -> float:
    """Deadzone + rescale to [0,1] at the deadzone edge + exponent curve. Signed.

    Uses cfg.deadzone for the rescale.  Triggers use _curve_trigger which
    substitutes cfg.trigger_deadzone so the two can be tuned independently
    (BP6 Fix 3).
    """
    a = abs(value)
    if a <= cfg.deadzone:
        return 0.0
    scaled = (a - cfg.deadzone) / (1.0 - cfg.deadzone)
    out = scaled ** cfg.curve_exp
    return out if value >= 0 else -out


def _curve_trigger(value: float, cfg: GamepadConfig) -> float:
    """Like _curve but uses cfg.trigger_deadzone for the rescale (BP6 Fix 3)."""
    a = abs(value)
    if a <= cfg.trigger_deadzone:
        return 0.0
    scaled = (a - cfg.trigger_deadzone) / (1.0 - cfg.trigger_deadzone)
    out = scaled ** cfg.curve_exp
    return out if value >= 0 else -out


def _radial_fracs(x: float, y: float, cfg: GamepadConfig) -> tuple[float, float]:
    """Radial (circular) deadzone + magnitude curve for a stick pair (BP6 Fix 1+2).

    1. Compute magnitude m = hypot(x, y).
    2. If m <= deadzone: both outputs are 0.0 (circular dead zone).
    3. Clamp m to 1.0 (Fix 2: full diagonal cannot exceed full cardinal magnitude).
    4. Rescale m past the deadzone and apply the exponent curve to the magnitude.
    5. Split the curved magnitude back proportionally by direction.

    For a pure cardinal push (e.g. x=v, y=0): m = |v|, frac_x = curved_m * sign(v),
    frac_y = 0.  The curved_m is identical to _curve(v, cfg) because the math is
    the same (same deadzone rescale, same curve_exp).  So existing cardinal tests
    are unaffected.
    """
    m = math.hypot(x, y)
    if m <= cfg.deadzone:
        return 0.0, 0.0
    m_clamped = min(m, 1.0)
    scaled = (m_clamped - cfg.deadzone) / (1.0 - cfg.deadzone)
    curved_m = scaled ** cfg.curve_exp
    # direction unit vector (m > 0 guaranteed here)
    ux, uy = x / m, y / m
    return curved_m * ux, curved_m * uy


class GamepadController:
    def __init__(self, config: GamepadConfig | None = None, now=time.monotonic):
        self.cfg = config or GamepadConfig()
        self._now = now
        self.enabled = False
        self.selected_idx = 0
        self._prev_buttons = 0
        self._cont = {}
        self._cont_t = {}
        self._hold = {}
        self._hold_cont = {}
        self._hold_axis = {}          # NEW: locks each tap/hold key to its press-time axis
        self._prev_lt = False         # previous trigger-active (for press edges)
        self._prev_rt = False
        self._last_activity = None

    # -- helpers ----------------------------------------------------------
    def reset(self):
        self._cont.clear()
        self._cont_t.clear()
        self._hold.clear()
        self._hold_cont.clear()
        self._hold_axis.clear()
        self._last_activity = None

    def last_frac(self, axis):
        return self._cont.get(axis)

    def _remember(self, snap):
        self._prev_buttons = snap.buttons
        self._prev_lt = snap.lt > self.cfg.trigger_threshold
        self._prev_rt = snap.rt > self.cfg.trigger_threshold

    def _edge(self, snap, mask) -> bool:
        return bool(snap.buttons & mask) and not (self._prev_buttons & mask)

    def _emit_continuous(self, out, axis, frac):
        t = self._now()
        if self._cont.get(axis) is None:
            self._cont[axis] = frac
            self._cont_t[axis] = t
            out.append(StartContinuous(axis, frac))
        elif (t - self._cont_t.get(axis, 0.0)) >= self.cfg.update_period_s:
            self._cont[axis] = frac
            self._cont_t[axis] = t
            out.append(UpdateContinuous(axis, frac))

    def _stop_continuous(self, out, axis):
        if self._cont.get(axis) is not None:
            self._cont[axis] = None
            out.append(StopContinuous(axis))

    # -- main -------------------------------------------------------------
    def poll(self, snap: GamepadSnapshot) -> list:
        out: list = []
        # Disconnect safety. NOTE: there is intentionally NO packet-"stale" auto-disarm
        # here. XInput's dwPacketNumber stays CONSTANT while a stick/trigger is held at
        # a steady deflection (no jitter on real hardware), so a packet-frozen check
        # cannot distinguish a legitimately-held control from a frozen controller and
        # would falsely disarm during normal use (observed on real hardware: moving a
        # stick or holding a trigger kicked the gamepad out). A genuinely frozen-but-
        # connected controller is covered by: the bounded continuous targets (a stuck
        # jog cannot cross the soft/interference limits), inactivity-on-neutral disarm,
        # focus-loss disarm, and the BACK / Space emergency stop.
        if not snap.connected:
            if self.enabled:
                self.enabled = False
                self.reset()
                out.append(StopAll())
                out.append(ToggleEnable(False))
            self._remember(snap)
            return out

        # inactivity auto-disarm
        if self.enabled:
            neutral = not (abs(snap.lx) > self.cfg.deadzone or abs(snap.ly) > self.cfg.deadzone
                           or abs(snap.rx) > self.cfg.deadzone or abs(snap.ry) > self.cfg.deadzone
                           or snap.lt > self.cfg.trigger_threshold or snap.rt > self.cfg.trigger_threshold
                           or (snap.buttons & ~BUTTON["START"]))
            t = self._now()
            if not neutral or self._last_activity is None:
                self._last_activity = t
            elif (t - self._last_activity) >= self.cfg.inactivity_disarm_s:
                self.enabled = False
                self.reset()
                out.append(StopAll())
                out.append(ToggleEnable(False))
                self._remember(snap)
                return out

        if self._edge(snap, BUTTON["START"]):
            self.enabled = not self.enabled
            self.reset()
            out.append(ToggleEnable(self.enabled))
            if not self.enabled:
                out.append(StopAll())

        if self.enabled:
            self._handle_sticks(snap, out)
            self._handle_triggers(snap, out)
            self._handle_bumpers(snap, out)
            self._handle_dpad(snap, out)
            self._handle_selector(snap, out)
            if self._edge(snap, BUTTON["BACK"]):
                out.append(StopAll())

        self._remember(snap)
        return out

    def _handle_sticks(self, snap, out):
        """Emit/stop continuous for both stick pairs using radial (circular) deadzone.

        BP6 Fix 1+2: _radial_fracs computes the pair magnitude, applies the circular
        deadzone, clamps to 1.0, curves the magnitude, then splits back by direction.
        For pure cardinal input this is numerically identical to the old per-axis
        _curve result (see _radial_fracs docstring), so existing cardinal tests pass.
        """
        owned = self._taphold_continuous_axes()
        lx_frac, ly_frac = _radial_fracs(snap.lx, snap.ly, self.cfg)
        rx_frac, ry_frac = _radial_fracs(snap.rx, snap.ry, self.cfg)
        for axis, frac in ((5, lx_frac), (6, ly_frac), (7, rx_frac), (8, ry_frac)):
            if frac == 0.0:
                if axis not in owned:
                    self._stop_continuous(out, axis)
            else:
                self._emit_continuous(out, axis, frac)

    def _taphold(self, out, key, active, edge, axis, direction, *, frac_when_held):
        """press-edge=one immediate step, hold>=delay=continuous, release=stop.
        The driven axis is LOCKED at press time so changing the selector mid-hold
        cannot start one axis and stop another.  A control already held across an
        (re)arm has no fresh edge, so it is ignored until released and re-pressed.

        Only the triggers (axis 3) and bumpers (axis 4) use _taphold now — and
        neither shares an axis with the sticks (5/6/7/8) or the D-pad. So there is
        no stick/tap-hold overlap to arbitrate on release (the old `active_stick_axes`
        Fix-1 path was removed with _active_stick_axes in the 2026-06-22 remap), and
        release always emits StopContinuous. Fix 2 (suppress the press-Jog when the
        axis already has a continuous owner) stays, keyed off self._cont.
        """
        pressed_at = self._hold.get(key)
        if active and pressed_at is None:
            if not edge:
                return  # held across (re)arm without a fresh press: ignore
            self._hold[key] = self._now()
            self._hold_cont[key] = False
            self._hold_axis[key] = axis
            # Fix 2: suppress Jog if the axis is already under an active continuous session.
            if self._cont.get(axis) is None:
                out.append(Jog(axis, direction))          # immediate step on press
            # else: axis has an active continuous owner — emitting Jog would conflict;
            # the hold timer is still started so a subsequent hold-to-continuous works.
        elif active and pressed_at is not None:
            held_axis = self._hold_axis.get(key, axis)
            if not self._hold_cont.get(key) and (self._now() - pressed_at) >= self.cfg.hold_delay_s:
                self._hold_cont[key] = True
                self._emit_continuous(out, held_axis, frac_when_held())
            elif self._hold_cont.get(key):
                self._emit_continuous(out, held_axis, frac_when_held())
        elif not active and pressed_at is not None:
            held_axis = self._hold_axis.get(key, axis)
            self._hold[key] = None
            if self._hold_cont.get(key):
                self._hold_cont[key] = False
                self._stop_continuous(out, held_axis)

    def _taphold_continuous_axes(self):
        """Axes currently driven by an active tap/hold continuous session."""
        return {self._hold_axis[k] for k, on in self._hold_cont.items()
                if on and k in self._hold_axis}

    def _handle_triggers(self, snap, out):
        # MicroscopeZ (axis 3): LT=down(-), RT=up(+); proportional on hold.
        lt = snap.lt > self.cfg.trigger_threshold
        rt = snap.rt > self.cfg.trigger_threshold
        lt_edge = lt and not self._prev_lt
        rt_edge = rt and not self._prev_rt
        # BP6 Fix 3: use _curve_trigger (rescales against cfg.trigger_deadzone) so
        # the trigger proportional curve is independent of the thumb stick deadzone.
        # For the default config (trigger_deadzone == trigger_threshold == deadzone)
        # _curve_trigger is numerically identical to _curve, so existing tests pass.
        self._taphold(out, "LT", lt, lt_edge, 3, -1, frac_when_held=lambda: -_curve_trigger(snap.lt, self.cfg))
        self._taphold(out, "RT", rt, rt_edge, 3, +1, frac_when_held=lambda: _curve_trigger(snap.rt, self.cfg))

    def _handle_bumpers(self, snap, out):
        # sample Z (axis 4): LB=down(-), RB=up(+); fixed-speed on hold.
        self._taphold(out, "LB", snap.pressed(BUTTON["LB"]), self._edge(snap, BUTTON["LB"]),
                      4, -1, frac_when_held=lambda: -1.0)
        self._taphold(out, "RB", snap.pressed(BUTTON["RB"]), self._edge(snap, BUTTON["RB"]),
                      4, +1, frac_when_held=lambda: 1.0)

    def _handle_dpad(self, snap, out):
        """D-pad on the SELECTED axis (2026-06-22 remap):
          UP / DOWN  -> one single-step jog (+ / -). STEP ONLY, never continuous.
          RIGHT / LEFT -> step-size notch up / down (StepDelta).
        All four are pure press edges (no hold-to-continuous). UP/DOWN suppress the
        Jog when the selected axis already has an active continuous session (e.g. a
        stick is driving it) so a one-shot step never fights a continuous owner.
        """
        axis = SELECTOR_AXES[self.selected_idx]
        if self._edge(snap, BUTTON["DPAD_UP"]):
            if self._cont.get(axis) is None:
                out.append(Jog(axis, +1))
        elif self._edge(snap, BUTTON["DPAD_DOWN"]):
            if self._cont.get(axis) is None:
                out.append(Jog(axis, -1))
        if self._edge(snap, BUTTON["DPAD_RIGHT"]):
            out.append(StepDelta(axis, +1))
        elif self._edge(snap, BUTTON["DPAD_LEFT"]):
            out.append(StepDelta(axis, -1))

    def _handle_selector(self, snap, out):
        """Face buttons = 2-D directional axis selection by stage-map geometry
        (2026-06-22 remap): Y=up, A=down, X=left, B=right. The picked neighbour is
        the nearest selectable axis on that side (directional_neighbor); if there is
        none, the press is a no-op. Replaces the old B/X linear cycle + Y/A step."""
        current = SELECTOR_AXES[self.selected_idx]
        direction = None
        if self._edge(snap, BUTTON["Y"]):
            direction = "up"
        elif self._edge(snap, BUTTON["A"]):
            direction = "down"
        elif self._edge(snap, BUTTON["X"]):
            direction = "left"
        elif self._edge(snap, BUTTON["B"]):
            direction = "right"
        if direction is None:
            return
        neighbor = directional_neighbor(current, direction)
        if neighbor is not None and neighbor != current:
            self.selected_idx = SELECTOR_AXES.index(neighbor)
            out.append(SelectAxis(neighbor))
