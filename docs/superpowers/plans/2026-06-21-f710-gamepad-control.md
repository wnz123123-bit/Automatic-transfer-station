# F710 Gamepad Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Logitech F710 (XInput) gamepad control of 7 stage axes (X3,Y3,X2,Y2,R,MicroscopeZ,sample-Z) with proportional continuous + single-step jog, reusing the existing motion queue and safety, with focus-aware arming and a queue-bypassing emergency stop.

**Architecture:** Three new modules — a pure ctypes XInput reader (`gamepad_xinput.py`), a pure no-Qt/no-hardware decision layer (`gamepad_controller.py` emitting motion *intents*), and a Qt integration widget (`gamepad_panel.py`) that polls on a QTimer and translates intents to existing controller calls via injected providers. Safety changes land in `axis_calibration.py` (X2/X3 margin + partner look-ahead) and `gas_controller.py` (look-ahead pass-through + a direct stop path). `main_window.py` constructs the panel, exposes `highlight_axis`, disarms on focus loss, and arbitrates GUI-vs-gamepad on the same axis.

**Tech Stack:** Python 3.13, PySide6, ctypes (XInput `xinput1_4.dll`, stdcall), existing `MotionCommandQueue`/`GasMotionController`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-21-f710-gamepad-control-design.md`

**Project note:** This repo is NOT a git repository. Every task ends with a **suite-green checkpoint** (`python -m pytest -q`) instead of a commit. Run all commands from `E:\Automatic transfer station` with `QT_QPA_PLATFORM=offscreen` already set by the test files. Baseline before starting: `184 passed`.

---

## File Structure

**Create:**
- `src/automatic_transfer_station/gamepad_xinput.py` — ctypes XInput wrapper; `GamepadSnapshot` dataclass; `GamepadReader` + `load_default_reader()`; button-bit constants; single-index poll + slow rescan; no deadzone/curve here.
- `src/automatic_transfer_station/gamepad_controller.py` — pure decision logic; `GamepadConfig`, intent classes, `GamepadController.poll(snapshot) -> list[intent]`; deadzone/rescale/curve, tap/hold, per-axis continuous sessions, rate-limit, selector, step snapping, packet-staleness, baseline reset; injected `now()`.
- `src/automatic_transfer_station/gamepad_panel.py` — `GamepadPanel(QGroupBox)`; QTimer tick; translate intents to motion via injected providers; enable button + status HUD; calls `highlight_axis`; disarm hook.
- `tests/test_gamepad_xinput.py`, `tests/test_gamepad_controller.py`, `tests/test_gamepad_panel.py`.

**Modify:**
- `src/automatic_transfer_station/axis_calibration.py` — nonzero `X2_X3_INTERFERENCE_MARGIN_PULSE`; `partner_lookahead_pulse` param on `safe_continuous_target`; per-axis `STEP_SNAP` tables + `snap_step()` helper.
- `src/automatic_transfer_station/gas_controller.py` — `start_jog(..., partner_lookahead_pulse=0)` pass-through to `safe_continuous_target`.
- `src/automatic_transfer_station/controller_base.py` — update `start_jog` Protocol signature to include the optional param.
- `tests/test_main_window.py` — `RecordingController.start_jog` accepts the optional param.
- `src/automatic_transfer_station/main_window.py` — construct `GamepadPanel`; add `highlight_axis()`; disarm on `changeEvent` focus loss; arbitrate GUI jog widgets when armed; pass step provider that uses `snap_step`.
- `src/automatic_transfer_station/run_eth_board_test.py` — `--no-gamepad` flag; wire reader.

---

## Phase A — Safety & calibration foundations

### Task A1: X2/X3 interference margin + partner look-ahead in `safe_continuous_target`

**Files:**
- Modify: `src/automatic_transfer_station/axis_calibration.py`
- Test: `tests/test_axis_calibration.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_axis_calibration.py` inside the existing test class:

```python
    def test_safe_continuous_target_applies_margin_and_partner_lookahead(self):
        from automatic_transfer_station.axis_calibration import (
            safe_continuous_target, X2_X3_INTERFERENCE_MARGIN_PULSE,
        )
        # margin is now a real safety gap, not zero
        self.assertGreater(X2_X3_INTERFERENCE_MARGIN_PULSE, 0)

        # axis5 negative jog with axis7 near the boundary: lookahead must pull the
        # bound further from the boundary (smaller reachable magnitude) than no lookahead.
        positions = {5: 700000.0, 7: -700000.0}
        base = safe_continuous_target(5, -1, positions)
        ahead = safe_continuous_target(5, -1, positions, partner_lookahead_pulse=20000.0)
        self.assertIsNotNone(base)
        self.assertIsNotNone(ahead)
        # lookahead assumes the partner advances toward the boundary, so axis5 must
        # stop sooner -> a larger (less negative direction) clamped target.
        self.assertGreater(ahead, base)

        # lookahead must never reverse direction (still refuses if no room)
        self.assertIsNone(
            safe_continuous_target(5, -1, {5: 100000.0, 7: -1133407.0}, partner_lookahead_pulse=20000.0)
        )
```

- [ ] **Step 2: Run it, verify failure**

Run: `python -m pytest tests/test_axis_calibration.py -k margin_and_partner -v`
Expected: FAIL (`X2_X3_INTERFERENCE_MARGIN_PULSE` is 0.0 / `partner_lookahead_pulse` is an unexpected kwarg).

- [ ] **Step 3: Implement** — in `axis_calibration.py`:

Change the constant:
```python
# Safety gap for the X2/X3 interference boundary. Sized to cover one gamepad
# update period (~100 ms) of mutual approach at a moderate continuous speed so a
# both-sticks-toward-each-other move cannot cross the boundary between re-bounds.
# 1000 um/s * 0.1 s * 50000 pulse/mm-equiv ≈ 5000 pulse per axis; use the pair sum.
X2_X3_INTERFERENCE_MARGIN_PULSE = 10000.0
```

Update `safe_continuous_target` signature and the clamp block:
```python
def safe_continuous_target(
    axis: int,
    direction: int,
    current_positions: dict[int, float],
    axis_calibrations: dict[int, AxisCalibration] | None = None,
    partner_lookahead_pulse: float = 0.0,
) -> int | None:
```
Inside, where `positions` is built and the clamp computed, fold the look-ahead into the partner term (assume the partner has already moved toward the boundary by `partner_lookahead_pulse`):
```python
    lookahead = abs(float(partner_lookahead_pulse))
    if axis == 5 and 7 in positions and target + (positions[7] - lookahead) <= threshold:
        target = _round_half_up(threshold - (positions[7] - lookahead) + 1)
        clamped = True
    elif axis == 7 and 5 in positions and target + (positions[5] - lookahead) <= threshold:
        target = _round_half_up(threshold - (positions[5] - lookahead) + 1)
        clamped = True
```
(The existing direction guard and final `is_target_safe` check stay; `is_target_safe` still uses the real positions, so the look-ahead only tightens the clamp, never loosens the final check.)

- [ ] **Step 4: Run it, verify pass**

Run: `python -m pytest tests/test_axis_calibration.py -v`
Expected: PASS (all, including the existing C1/X2 tests — the larger margin must not break `test_safe_continuous_target_uses_soft_range_and_interference_boundary`; if that test asserts the old `-1051566` exactly, update its expectation to the value produced by the new margin and document why).

- [ ] **Step 5: Suite-green checkpoint**

Run: `python -m pytest -q`
Expected: all pass.

---

### Task A2: Per-axis discrete step snap points + `snap_step()`

**Files:**
- Modify: `src/automatic_transfer_station/axis_calibration.py`
- Test: `tests/test_axis_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_step_snap_tables_and_snap_step(self):
        from automatic_transfer_station.axis_calibration import STEP_SNAP, snap_step
        for axis in (5, 6, 7, 8, 9, 3, 4):
            self.assertIn(axis, STEP_SNAP)
            self.assertEqual(sorted(STEP_SNAP[axis]), list(STEP_SNAP[axis]))  # ascending
        # snap to nearest then step by delta, clamped at ends
        snaps = STEP_SNAP[5]
        self.assertEqual(snap_step(5, snaps[0], -1), snaps[0])            # clamp low
        self.assertEqual(snap_step(5, snaps[-1], +1), snaps[-1])          # clamp high
        self.assertEqual(snap_step(5, snaps[0], +1), snaps[1])            # up one notch
        # a free value snaps to nearest before stepping
        mid = (snaps[1] + snaps[2]) / 2.0
        self.assertEqual(snap_step(5, mid + 0.01, +1), snaps[2])
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_axis_calibration.py -k step_snap -v`
Expected: FAIL (no `STEP_SNAP` / `snap_step`).

- [ ] **Step 3: Implement** — add to `axis_calibration.py`:

```python
# Discrete step "notches" the gamepad selector cycles through per axis (GUI step
# spin box stays free-entry; the gamepad only snaps to these). Units match each
# axis's GUI step unit (linear um, Z um, R deg).
STEP_SNAP = {
    5: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    6: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    7: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    8: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    9: (0.05, 0.1, 0.5, 1.0, 5.0, 10.0),
    3: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    4: (0.1, 1.0, 5.0, 10.0, 50.0, 100.0, 500.0),
}


def snap_step(axis: int, current: float, delta: int) -> float:
    snaps = STEP_SNAP[int(axis)]
    nearest = min(range(len(snaps)), key=lambda i: abs(snaps[i] - float(current)))
    idx = max(0, min(len(snaps) - 1, nearest + (1 if delta > 0 else -1 if delta < 0 else 0)))
    return snaps[idx]
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_axis_calibration.py -v`
Expected: PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task A3: Thread `partner_lookahead_pulse` through `start_jog`

**Files:**
- Modify: `src/automatic_transfer_station/gas_controller.py`, `src/automatic_transfer_station/controller_base.py`
- Test: `tests/test_gas_controller.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_gas_controller.py`:

```python
    def test_start_jog_passes_partner_lookahead_to_bound(self):
        from automatic_transfer_station.gas_controller import GasMotionController
        api = FakeApi()
        api.positions[5] = 700000.0
        api.positions[7] = -700000.0
        controller = GasMotionController(api=api)
        controller.is_open = True
        # with a lookahead, axis 5's bounded target must stop sooner than without
        controller.start_jog(5, -50.0)
        no_ahead = next(c for c in reversed(api.calls) if c[0] == "set_position")[2]
        api.calls.clear()
        controller.start_jog(5, -50.0, partner_lookahead_pulse=20000.0)
        with_ahead = next(c for c in reversed(api.calls) if c[0] == "set_position")[2]
        self.assertGreater(with_ahead, no_ahead)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_gas_controller.py -k partner_lookahead -v`
Expected: FAIL (`start_jog` got an unexpected keyword argument).

- [ ] **Step 3: Implement**

In `gas_controller.py` `start_jog`:
```python
    @_api_locked
    def start_jog(self, axis: int, speed: float, partner_lookahead_pulse: float = 0.0) -> None:
        self._require_open()
        direction = 1 if speed > 0 else -1
        if speed != 0:
            self._assert_hard_limit_direction_allowed(axis, direction)
            current_positions = self._current_positions_for_safety(axis)
            bounded_target = safe_continuous_target(
                axis, direction, current_positions, self.axis_calibrations, partner_lookahead_pulse
            )
            ...
```
In `controller_base.py` `MotionController` Protocol:
```python
    def start_jog(self, axis: int, speed: float, partner_lookahead_pulse: float = 0.0) -> None:
        ...
```

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gas_controller.py -v` → PASS.

- [ ] **Step 5: Fix the test double** — in `tests/test_main_window.py` update `RecordingController.start_jog`:
```python
    def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
        self.calls.append(("start_jog", axis, speed))
```
(Keep the recorded tuple shape unchanged so existing assertions still match.)

- [ ] **Step 6: Suite-green checkpoint** — `python -m pytest -q` → all pass (baseline + new).

---

## Phase B — XInput reader

### Task B1: `GamepadSnapshot` + button constants + normalization

**Files:**
- Create: `src/automatic_transfer_station/gamepad_xinput.py`
- Test: `tests/test_gamepad_xinput.py`

- [ ] **Step 1: Write the failing test**

```python
import sys, ctypes
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class FakeXInputDll:
    """Mimics XInputGetState writing into the XINPUT_STATE struct."""
    def __init__(self, *, connected=True, packet=1, lx=0, ly=0, rx=0, ry=0, lt=0, rt=0, buttons=0):
        self.cfg = dict(connected=connected, packet=packet, lx=lx, ly=ly, rx=rx, ry=ry, lt=lt, rt=rt, buttons=buttons)
        self.calls = []

    def XInputGetState(self, index, state_ptr):
        self.calls.append(int(index))
        if not self.cfg["connected"]:
            return 1167  # ERROR_DEVICE_NOT_CONNECTED
        st = state_ptr._obj if hasattr(state_ptr, "_obj") else state_ptr.contents
        st.dwPacketNumber = self.cfg["packet"]
        g = st.Gamepad
        g.wButtons = self.cfg["buttons"]
        g.bLeftTrigger = self.cfg["lt"]
        g.bRightTrigger = self.cfg["rt"]
        g.sThumbLX = self.cfg["lx"]; g.sThumbLY = self.cfg["ly"]
        g.sThumbRX = self.cfg["rx"]; g.sThumbRY = self.cfg["ry"]
        return 0


class GamepadSnapshotTests(unittest.TestCase):
    def test_button_constants_and_pressed(self):
        from automatic_transfer_station.gamepad_xinput import BUTTON, GamepadSnapshot
        self.assertEqual(BUTTON["A"], 0x1000)
        self.assertEqual(BUTTON["START"], 0x0010)
        self.assertEqual(BUTTON["BACK"], 0x0020)
        self.assertEqual(BUTTON["DPAD_LEFT"], 0x0004)
        snap = GamepadSnapshot(connected=True, packet=1, lx=0, ly=0, rx=0, ry=0, lt=0, rt=0,
                               buttons=BUTTON["A"] | BUTTON["START"])
        self.assertTrue(snap.pressed(BUTTON["A"]))
        self.assertTrue(snap.pressed(BUTTON["START"]))
        self.assertFalse(snap.pressed(BUTTON["B"]))

    def test_normalization_signs_and_ranges(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        dll = FakeXInputDll(lx=-32768, ly=32767, rt=255, lt=128)
        reader = GamepadReader(dll)
        snap = reader.read_state()
        self.assertAlmostEqual(snap.lx, -1.0, places=4)   # negative divides by 32768
        self.assertAlmostEqual(snap.ly, 1.0, places=4)    # positive divides by 32767
        self.assertAlmostEqual(snap.rt, 1.0, places=4)
        self.assertAlmostEqual(snap.lt, 128 / 255, places=4)
        self.assertTrue(snap.connected)

    def test_disconnected_returns_not_connected_snapshot(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        reader = GamepadReader(FakeXInputDll(connected=False))
        snap = reader.read_state()
        self.assertFalse(snap.connected)
        self.assertEqual(snap.lx, 0.0)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_gamepad_xinput.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** — `src/automatic_transfer_station/gamepad_xinput.py`:

```python
from __future__ import annotations

import ctypes
from dataclasses import dataclass

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

BUTTON = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LEFT_THUMB": 0x0040, "RIGHT_THUMB": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _XINPUT_GAMEPAD)]


@dataclass(frozen=True)
class GamepadSnapshot:
    connected: bool
    packet: int
    lx: float
    ly: float
    rx: float
    ry: float
    lt: float
    rt: float
    buttons: int

    def pressed(self, mask: int) -> bool:
        return bool(self.buttons & mask)


_DISCONNECTED = GamepadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)


def _norm_thumb(v: int) -> float:
    return max(-1.0, v / 32768.0) if v < 0 else min(1.0, v / 32767.0)


class GamepadReader:
    """Reads one XInput controller. Polls a single cached index once connected;
    only rescans all slots (slowly, while disconnected) to (re)discover."""

    def __init__(self, xinput):
        self._x = xinput
        self._index: int | None = None

    def read_state(self, index: int | None = None) -> GamepadSnapshot:
        st = _XINPUT_STATE()
        candidates = [index] if index is not None else ([self._index] if self._index is not None else [0, 1, 2, 3])
        for idx in candidates:
            if idx is None:
                continue
            rc = int(self._x.XInputGetState(idx, ctypes.byref(st)))
            if rc == ERROR_SUCCESS:
                self._index = idx
                g = st.Gamepad
                return GamepadSnapshot(
                    True, int(st.dwPacketNumber),
                    _norm_thumb(g.sThumbLX), _norm_thumb(g.sThumbLY),
                    _norm_thumb(g.sThumbRX), _norm_thumb(g.sThumbRY),
                    g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0,
                    int(g.wButtons),
                )
        self._index = None
        return _DISCONNECTED


def load_default_reader() -> GamepadReader:
    try:
        dll = ctypes.WinDLL("xinput1_4.dll")
    except OSError:
        dll = ctypes.WinDLL("xinput9_1_0.dll")
    dll.XInputGetState.restype = ctypes.c_uint
    dll.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(_XINPUT_STATE)]
    return GamepadReader(dll)
```

Note: the FakeXInputDll in the test writes via `state_ptr.contents` (ctypes.byref gives a pointer whose `.contents` is the struct); adjust the fake's `_obj`/`contents` access to match `ctypes.byref`. (`byref` objects expose `._obj`.)

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_xinput.py -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task B2: Single-index poll + slow rescan behavior

**Files:** Modify: `tests/test_gamepad_xinput.py` (add tests; impl already supports it)

- [ ] **Step 1: Write the test**

```python
    def test_polls_single_cached_index_after_connect(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        dll = FakeXInputDll(connected=True, lx=0)
        # make only index 2 connected
        orig = dll.XInputGetState
        def only2(index, ptr):
            if index != 2:
                dll.calls.append(int(index)); return 1167
            return orig(index, ptr)
        dll.XInputGetState = only2
        reader = GamepadReader(dll)
        reader.read_state()                 # discovers index 2 (scans 0,1,2)
        dll.calls.clear()
        reader.read_state()                 # should poll ONLY index 2 now
        self.assertEqual(dll.calls, [2])
```

- [ ] **Step 2: Run, verify pass** (impl already caches `_index`):

Run: `python -m pytest tests/test_gamepad_xinput.py -k single_cached -v`
Expected: PASS. If it fails, fix `read_state` caching per Task B1 Step 3.

- [ ] **Step 3: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

## Phase C — Pure decision layer (the core)

### Task C1: Intents, config, deadzone/rescale/curve, and enable gating

**Files:**
- Create: `src/automatic_transfer_station/gamepad_controller.py`
- Test: `tests/test_gamepad_controller.py`

- [ ] **Step 1: Write the failing test**

```python
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
        # not enabled: pushing a stick does nothing
        self.assertEqual(c.poll(snap(lx=1.0)), [])
        # pressing Start enables (edge)
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
        # cross deadzone on left X -> StartContinuous for axis 5 (X3) with sign
        out = c.poll(snap(lx=1.0))
        starts = [i for i in out if isinstance(i, StartContinuous) and i.axis == 5]
        self.assertEqual(len(starts), 1)
        self.assertGreater(starts[0].frac, 0)          # full deflection -> ~1.0 after curve
        # within rate-limit period, no duplicate update
        clk.advance(0.02)
        self.assertFalse([i for i in c.poll(snap(lx=1.0)) if isinstance(i, UpdateContinuous)])
        # after the rate-limit period, an UpdateContinuous is emitted
        clk.advance(0.10)
        self.assertTrue([i for i in c.poll(snap(lx=0.5)) if isinstance(i, UpdateContinuous) and i.axis == 5])
        # return to deadzone -> StopContinuous
        self.assertTrue([i for i in c.poll(snap(lx=0.0)) if isinstance(i, StopContinuous) and i.axis == 5])

    def test_deadzone_and_curve(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StartContinuous
        c = GamepadController(now=Clock()); c.enabled = True
        # inside deadzone -> no motion
        self.assertEqual([i for i in c.poll(snap(lx=0.10)) if isinstance(i, StartContinuous)], [])
        # just past deadzone -> small but nonzero, and < linear (squared curve)
        out = c.poll(snap(lx=0.20))
        s = [i for i in out if isinstance(i, StartContinuous)][0]
        self.assertGreater(s.frac, 0.0)
        self.assertLess(s.frac, 0.20)
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_gamepad_controller.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — `src/automatic_transfer_station/gamepad_controller.py` (intents + config + sticks; tap/hold and selector added in later tasks but include their handlers now so the file is whole):

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .gamepad_xinput import BUTTON, GamepadSnapshot


# ---- intents -------------------------------------------------------------
@dataclass(frozen=True)
class StartContinuous: axis: int; frac: float
@dataclass(frozen=True)
class UpdateContinuous: axis: int; frac: float
@dataclass(frozen=True)
class StopContinuous: axis: int
@dataclass(frozen=True)
class Jog: axis: int; direction: int
@dataclass(frozen=True)
class SelectAxis: axis: int
@dataclass(frozen=True)
class StepDelta: axis: int; delta: int
@dataclass(frozen=True)
class StopAll: pass
@dataclass(frozen=True)
class ToggleEnable: enabled: bool


@dataclass
class GamepadConfig:
    deadzone: float = 0.12
    curve_exp: float = 2.0
    hold_delay_s: float = 0.30
    update_period_s: float = 0.10
    trigger_threshold: float = 0.15
    stale_ticks: int = 4


SELECTOR_AXES = (5, 6, 7, 8, 9, 3, 4)  # X3,Y3,X2,Y2,R,MicroscopeZ,Z


def _curve(value: float, cfg: GamepadConfig) -> float:
    """Deadzone + rescale to [0,1] at the deadzone edge + exponent curve. Signed."""
    a = abs(value)
    if a <= cfg.deadzone:
        return 0.0
    scaled = (a - cfg.deadzone) / (1.0 - cfg.deadzone)
    out = scaled ** cfg.curve_exp
    return out if value >= 0 else -out


class GamepadController:
    def __init__(self, config: GamepadConfig | None = None, now=time.monotonic):
        self.cfg = config or GamepadConfig()
        self._now = now
        self.enabled = False
        self.selected_idx = 0
        self._prev_buttons = 0
        self._cont = {}          # axis -> last emitted frac (None means not active)
        self._cont_t = {}        # axis -> last update time
        self._hold = {}          # control-key -> press time (None if up)
        self._hold_cont = {}     # control-key -> bool in-continuous
        self._prev_packet = None
        self._stale = 0

    # -- helpers ----------------------------------------------------------
    def reset(self):
        self._cont.clear(); self._cont_t.clear(); self._hold.clear(); self._hold_cont.clear()

    def _edge(self, snap, mask) -> bool:
        return bool(snap.buttons & mask) and not (self._prev_buttons & mask)

    def _emit_continuous(self, out, axis, frac):
        t = self._now()
        if self._cont.get(axis) is None:
            self._cont[axis] = frac; self._cont_t[axis] = t
            out.append(StartContinuous(axis, frac))
        elif (t - self._cont_t.get(axis, 0.0)) >= self.cfg.update_period_s:
            self._cont[axis] = frac; self._cont_t[axis] = t
            out.append(UpdateContinuous(axis, frac))

    def _stop_continuous(self, out, axis):
        if self._cont.get(axis) is not None:
            self._cont[axis] = None
            out.append(StopContinuous(axis))

    # -- main -------------------------------------------------------------
    def poll(self, snap: GamepadSnapshot) -> list:
        out: list = []
        # disconnect / stale-input safety
        if not snap.connected or self._is_stale(snap):
            if self.enabled:
                self.enabled = False
                self.reset()
                out.append(StopAll())
                out.append(ToggleEnable(False))
            self._prev_buttons = snap.buttons
            self._prev_packet = snap.packet
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
            self._handle_dpad_executor(snap, out)
            self._handle_selector(snap, out)
            if self._edge(snap, BUTTON["BACK"]):
                out.append(StopAll())

        self._prev_buttons = snap.buttons
        self._prev_packet = snap.packet
        return out

    def _is_stale(self, snap) -> bool:
        nonneutral = (abs(snap.lx) > self.cfg.deadzone or abs(snap.ly) > self.cfg.deadzone
                      or abs(snap.rx) > self.cfg.deadzone or abs(snap.ry) > self.cfg.deadzone
                      or snap.lt > self.cfg.trigger_threshold or snap.rt > self.cfg.trigger_threshold)
        if self._prev_packet is not None and snap.packet == self._prev_packet and nonneutral:
            self._stale += 1
        else:
            self._stale = 0
        return self._stale >= self.cfg.stale_ticks

    def _handle_sticks(self, snap, out):
        for axis, value in ((5, snap.lx), (6, snap.ly), (7, snap.rx), (8, snap.ry)):
            frac = _curve(value, self.cfg)
            if frac == 0.0:
                self._stop_continuous(out, axis)
            else:
                self._emit_continuous(out, axis, frac)

    # _handle_triggers / _handle_bumpers / _handle_dpad_executor / _handle_selector
    # are implemented in Tasks C2 and C3 below; define no-op stubs here so the
    # module imports, then fill them in those tasks (do NOT ship the stubs).
    def _handle_triggers(self, snap, out): ...
    def _handle_bumpers(self, snap, out): ...
    def _handle_dpad_executor(self, snap, out): ...
    def _handle_selector(self, snap, out): ...
```

(Note: the `...` method bodies are placeholders ONLY for ordering; Tasks C2/C3 replace each with the real body and its own failing test first. Do not leave any `...` in the shipped file after C3.)

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_controller.py -v` → PASS (the C1 tests; stick + enable paths).

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task C2: Tap/hold for triggers (MicroscopeZ), bumpers (Z), D-pad (selected axis)

**Files:**
- Modify: `src/automatic_transfer_station/gamepad_controller.py`
- Test: `tests/test_gamepad_controller.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_trigger_tap_steps_immediately_hold_goes_continuous(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, Jog, StartContinuous, StopContinuous, GamepadConfig,
        )
        clk = Clock()
        c = GamepadController(GamepadConfig(), now=clk); c.enabled = True
        # press RT (Z up) -> immediate one step on axis 3 (+)
        out = c.poll(snap(rt=1.0))
        self.assertIn(Jog(3, +1), out)
        # still held but < hold_delay -> nothing new
        clk.advance(0.1); self.assertEqual(c.poll(snap(rt=1.0)), [])
        # held past hold_delay -> StartContinuous (proportional frac from push)
        clk.advance(0.25)
        out = c.poll(snap(rt=1.0))
        self.assertTrue([i for i in out if isinstance(i, StartContinuous) and i.axis == 3 and i.frac > 0])
        # release -> StopContinuous
        self.assertIn(StopContinuous(3), c.poll(snap(rt=0.0)))

    def test_bumper_tap_steps_and_hold_continuous_axis4(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog, StartContinuous
        from automatic_transfer_station.gamepad_xinput import BUTTON
        clk = Clock(); c = GamepadController(now=clk); c.enabled = True
        out = c.poll(snap(buttons=BUTTON["RB"]))        # Z up
        self.assertIn(Jog(4, +1), out)
        clk.advance(0.35)
        self.assertTrue([i for i in c.poll(snap(buttons=BUTTON["RB"])) if isinstance(i, StartContinuous) and i.axis == 4])

    def test_dpad_executor_acts_on_selected_axis(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, Jog
        from automatic_transfer_station.gamepad_xinput import BUTTON
        c = GamepadController(now=Clock()); c.enabled = True   # selected = X3 (axis 5)
        out = c.poll(snap(buttons=BUTTON["DPAD_RIGHT"]))
        self.assertIn(Jog(5, +1), out)
        out = c.poll(snap(buttons=BUTTON["DPAD_LEFT"]))
        self.assertIn(Jog(5, -1), out)
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_gamepad_controller.py -k "trigger_tap or bumper_tap or dpad_executor" -v` → FAIL (stub no-ops).

- [ ] **Step 3: Implement** — replace the three stub methods with a shared tap/hold helper:

```python
    def _taphold(self, out, key, active, axis, direction, *, frac_when_held):
        """Generic press=one-step, hold>=delay=continuous, release=stop."""
        pressed_at = self._hold.get(key)
        if active and pressed_at is None:
            self._hold[key] = self._now()
            self._hold_cont[key] = False
            out.append(Jog(axis, direction))                 # immediate step on press
        elif active and pressed_at is not None:
            if not self._hold_cont.get(key) and (self._now() - pressed_at) >= self.cfg.hold_delay_s:
                self._hold_cont[key] = True
                self._emit_continuous(out, axis, frac_when_held())
            elif self._hold_cont.get(key):
                self._emit_continuous(out, axis, frac_when_held())
        elif not active and pressed_at is not None:
            self._hold[key] = None
            if self._hold_cont.get(key):
                self._hold_cont[key] = False
                self._stop_continuous(out, axis)

    def _handle_triggers(self, snap, out):
        # MicroscopeZ (axis 3): LT=down(-), RT=up(+); proportional on hold.
        lt = snap.lt > self.cfg.trigger_threshold
        rt = snap.rt > self.cfg.trigger_threshold
        self._taphold(out, "LT", lt, 3, -1, frac_when_held=lambda: -_curve(snap.lt, self.cfg) or -0.0)
        self._taphold(out, "RT", rt, 3, +1, frac_when_held=lambda: _curve(snap.rt, self.cfg))

    def _handle_bumpers(self, snap, out):
        # sample Z (axis 4): LB=down(-), RB=up(+); fixed-speed on hold.
        self._taphold(out, "LB", snap.pressed(BUTTON["LB"]), 4, -1, frac_when_held=lambda: -1.0)
        self._taphold(out, "RB", snap.pressed(BUTTON["RB"]), 4, +1, frac_when_held=lambda: 1.0)

    def _handle_dpad_executor(self, snap, out):
        axis = SELECTOR_AXES[self.selected_idx]
        self._taphold(out, "DPADL", snap.pressed(BUTTON["DPAD_LEFT"]), axis, -1, frac_when_held=lambda: -1.0)
        self._taphold(out, "DPADR", snap.pressed(BUTTON["DPAD_RIGHT"]), axis, +1, frac_when_held=lambda: 1.0)
```

Note: for the triggers, `frac_when_held` for LT returns a negative magnitude (down). For axis 3 the frac sign encodes direction; the panel multiplies by the controller direction helper anyway, so keep StartContinuous/UpdateContinuous frac signed.

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_controller.py -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task C3: Selector (axis cycle + step delta), Back stop-all, baseline reset

**Files:**
- Modify: `src/automatic_transfer_station/gamepad_controller.py`
- Test: `tests/test_gamepad_controller.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_selector_cycles_axes_and_emits_select(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, SelectAxis, SELECTOR_AXES
        from automatic_transfer_station.gamepad_xinput import BUTTON
        c = GamepadController(now=Clock()); c.enabled = True
        out = c.poll(snap(buttons=BUTTON["B"]))      # next axis
        self.assertIn(SelectAxis(SELECTOR_AXES[1]), out)
        out = c.poll(snap(buttons=BUTTON["X"]))      # prev axis -> back to first
        self.assertIn(SelectAxis(SELECTOR_AXES[0]), out)

    def test_step_delta_polarity(self):
        from automatic_transfer_station.gamepad_controller import GamepadController, StepDelta, SELECTOR_AXES
        from automatic_transfer_station.gamepad_xinput import BUTTON
        c = GamepadController(now=Clock()); c.enabled = True
        self.assertIn(StepDelta(SELECTOR_AXES[0], +1), c.poll(snap(buttons=BUTTON["Y"])))  # Y(top)=larger
        self.assertIn(StepDelta(SELECTOR_AXES[0], -1), c.poll(snap(buttons=BUTTON["A"])))  # A(bottom)=smaller

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
```

- [ ] **Step 2: Run, verify failure** — relevant `-k` → FAIL.

- [ ] **Step 3: Implement** — fill `_handle_selector`:

```python
    def _handle_selector(self, snap, out):
        axis = SELECTOR_AXES[self.selected_idx]
        if self._edge(snap, BUTTON["B"]):
            self.selected_idx = (self.selected_idx + 1) % len(SELECTOR_AXES)
            out.append(SelectAxis(SELECTOR_AXES[self.selected_idx]))
        elif self._edge(snap, BUTTON["X"]):
            self.selected_idx = (self.selected_idx - 1) % len(SELECTOR_AXES)
            out.append(SelectAxis(SELECTOR_AXES[self.selected_idx]))
        if self._edge(snap, BUTTON["Y"]):
            out.append(StepDelta(axis, +1))
        elif self._edge(snap, BUTTON["A"]):
            out.append(StepDelta(axis, -1))
```

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_controller.py -v` → PASS. Confirm no `...` stub bodies remain in the module.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task C4: Inactivity auto-disarm (spec §4.5)

**Files:**
- Modify: `src/automatic_transfer_station/gamepad_controller.py`
- Test: `tests/test_gamepad_controller.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_inactivity_auto_disarms(self):
        from automatic_transfer_station.gamepad_controller import (
            GamepadController, GamepadConfig, ToggleEnable, StopAll,
        )
        clk = Clock()
        c = GamepadController(GamepadConfig(inactivity_disarm_s=5.0), now=clk)
        c.enabled = True
        c.poll(snap(lx=1.0))            # activity resets the idle timer
        clk.advance(3.0); c.poll(snap())   # neutral, still within window
        self.assertTrue(c.enabled)
        clk.advance(3.0)                # now > 5s since last non-neutral input
        out = c.poll(snap())
        self.assertFalse(c.enabled)
        self.assertIn(StopAll(), out)
        self.assertTrue(any(isinstance(i, ToggleEnable) and not i.enabled for i in out))
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_gamepad_controller.py -k inactivity -v` → FAIL (`inactivity_disarm_s` unknown / no disarm).

- [ ] **Step 3: Implement**

Add to `GamepadConfig`: `inactivity_disarm_s: float = 60.0`.
In `GamepadController.__init__`: `self._last_activity = None`.
At the START of `poll` (after the disconnect/stale block, before the Start edge), when enabled, check idle:
```python
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
                self._prev_buttons = snap.buttons
                self._prev_packet = snap.packet
                return out
```
Reset `self._last_activity = None` inside `reset()` and on enable toggle.

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_controller.py -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

## Phase D — Qt integration widget

### Task D1: `GamepadPanel` translation of intents to motion (no real timer/hardware)

**Files:**
- Create: `src/automatic_transfer_station/gamepad_panel.py`
- Test: `tests/test_gamepad_panel.py`

- [ ] **Step 1: Write the failing test** — drive `tick()` directly with a fake reader/clock + `RecordingController` + inline queue:

```python
import os, sys
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from automatic_transfer_station.gamepad_xinput import GamepadSnapshot, BUTTON


def get_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


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


class GamepadPanelTests(unittest.TestCase):
    def _panel(self, reader, clk, ctrl):
        from automatic_transfer_station.gamepad_panel import GamepadPanel
        controls = {}
        return GamepadPanel(
            register=lambda n, w: controls.setdefault(n, w),
            motion_queue_provider=lambda: InlineQueue(),
            motion_controller=ctrl,
            stop_all=ctrl.stop_all,
            speed_fn=lambda axis: 100.0,        # pulse/ms-equiv stand-in
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
        panel.tick()                            # Start -> arm
        panel.tick()                            # left stick -> start_jog axis 5
        self.assertIn(("start_jog", 5, 100.0), ctrl.calls)

    def test_back_stops_all_via_direct_path_not_queue(self):
        clk = Clk(); ctrl = RecMotion()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(buttons=BUTTON["BACK"])])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick(); panel.tick()
        self.assertIn(("stop_all",), ctrl.calls)

    def test_jog_uses_relative_move_with_gui_step(self):
        clk = Clk(); ctrl = RecMotion()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(buttons=BUTTON["RB"])])
        panel, _ = self._panel(reader, clk, ctrl)
        panel.tick(); panel.tick()
        self.assertIn(("relative_move", 4, 1, 100.0, 50), ctrl.calls)
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_gamepad_panel.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — `src/automatic_transfer_station/gamepad_panel.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton

from .gamepad_controller import (
    GamepadController, StartContinuous, UpdateContinuous, StopContinuous,
    Jog, SelectAxis, StepDelta, StopAll, ToggleEnable,
)
from .gamepad_xinput import load_default_reader


class GamepadPanel(QGroupBox):
    def __init__(self, register, motion_queue_provider, motion_controller, stop_all, speed_fn,
                 step_fn, set_step_fn, direction_fn, highlight_fn, on_armed_changed=None,
                 reader=None, clock=time.monotonic, controller=None, poll_ms: int = 30, parent=None):
        super().__init__("手柄控制", parent)
        self._register = register
        self._queue_provider = motion_queue_provider
        self._motion_controller = motion_controller
        self._on_armed_changed = on_armed_changed or (lambda armed: None)
        self._stop_all = stop_all
        self._speed_fn = speed_fn
        self._step_fn = step_fn
        self._set_step_fn = set_step_fn
        self._direction_fn = direction_fn
        self._highlight_fn = highlight_fn
        self._reader = reader or load_default_reader()
        self._controller = controller or GamepadController(now=clock)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    def _build_ui(self):
        row = QHBoxLayout(self)
        self._enable_btn = self._reg("btnGamepadEnable", QPushButton("启用手柄"))
        self._enable_btn.setCheckable(True)
        self._enable_btn.clicked.connect(self._toggle_from_ui)
        self._hud = self._reg("gamepadHud", QLabel("手柄未启用"))
        row.addWidget(self._enable_btn)
        row.addWidget(self._hud, 1)

    def _reg(self, name, w):
        w.setObjectName(name)
        if self._register:
            self._register(name, w)
        return w

    def _toggle_from_ui(self):
        # mirror UI button into controller enable state
        self._controller.enabled = self._enable_btn.isChecked()
        self._controller.reset()
        if not self._controller.enabled:
            self._safe_stop_all()
        self._refresh_hud()

    def disarm(self):
        if self._controller.enabled:
            self._controller.enabled = False
            self._controller.reset()
            self._enable_btn.setChecked(False)
            self._safe_stop_all()
            self._refresh_hud()

    def _safe_stop_all(self):
        try:
            self._stop_all()
        except Exception:
            pass

    def tick(self):
        snap = self._reader.read_state()
        for intent in self._controller.poll(snap):
            self._apply(intent)
        # keep UI button in sync (e.g., auto-disarm on disconnect)
        if self._enable_btn.isChecked() != self._controller.enabled:
            self._enable_btn.setChecked(self._controller.enabled)
            self._refresh_hud()

    def _apply(self, intent):
        if isinstance(intent, StopAll):
            self._safe_stop_all(); return
        if isinstance(intent, ToggleEnable):
            self._refresh_hud(); return
        if isinstance(intent, SelectAxis):
            self._highlight_fn(intent.axis); self._refresh_hud(); return
        if isinstance(intent, StepDelta):
            self._set_step_fn(intent.axis, intent.delta); self._refresh_hud(); return

        queue = self._queue_provider()
        if queue is None:
            return
        if isinstance(intent, (StartContinuous, UpdateContinuous)):
            axis, frac = intent.axis, intent.frac
            sign = 1 if frac >= 0 else -1
            speed = self._direction_fn(axis, sign) * abs(frac) * self._speed_fn(axis)
            queue.submit(lambda a=axis, s=speed: self._jog(a, s), priority=5)
        elif isinstance(intent, StopContinuous):
            queue.submit(lambda a=intent.axis: self._stop_axis_via_queue(a), priority=0)
        elif isinstance(intent, Jog):
            axis, d = intent.axis, intent.direction
            cdir = self._direction_fn(axis, d)
            speed = self._speed_fn(axis); step = self._step_fn(axis)
            queue.submit(lambda a=axis, c=cdir, sp=speed, st=step: self._relative(a, c, sp, st), priority=10)

    # bound through the queue worker; resolve the live controller lazily
    def _jog(self, axis, speed): self._motion().start_jog(axis, speed)
    def _stop_axis_via_queue(self, axis): self._motion().stop_axis(axis)
    def _relative(self, axis, d, sp, st): self._motion().relative_move(axis, d, sp, st)

    def _motion(self):
        return self._motion_controller

    def _refresh_hud(self):
        from .gamepad_controller import SELECTOR_AXES
        from .axis_calibration import AXIS_LABELS
        axis = SELECTOR_AXES[self._controller.selected_idx]
        state = "启用" if self._controller.enabled else "未启用"
        self._hud.setText(f"{state} · 选中:{AXIS_LABELS.get(axis, axis)} · 步长:{self._step_fn(axis)}")
```

Note for the executor: `_jog`/`_stop_axis_via_queue`/`_relative` call `self._motion()` which must resolve to the real controller. Add a `motion_controller` provider OR have `motion_queue_provider` actions call the controller directly. Simplest: pass the controller in and store it; the queue actions call it on the worker thread. Update the constructor to accept `motion_controller` and set `self._motion_controller`. In the test, `RecMotion` is both the controller and exposes start_jog/stop_axis/relative_move; pass it as `motion_controller`. Adjust the test `_panel` to also pass `motion_controller=ctrl`. (Update the constructor signature + test accordingly.)

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_panel.py -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task D2: Continuous look-ahead for the X2/X3 pair + stop-on-deadzone bypasses rate-limit

**Files:**
- Modify: `src/automatic_transfer_station/gamepad_panel.py`
- Test: `tests/test_gamepad_panel.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_x2x3_continuous_passes_partner_lookahead(self):
        # When both X2(7) and X3(5) are driven, start_jog must receive a nonzero
        # partner_lookahead_pulse so the bound accounts for the partner moving.
        clk = Clk()
        class Rec(RecMotion):
            def start_jog(self, axis, speed, partner_lookahead_pulse=0.0):
                self.calls.append(("start_jog", axis, round(speed,4), round(partner_lookahead_pulse,1)))
        ctrl = Rec()
        reader = ScriptedReader([snap(buttons=BUTTON["START"]), snap(lx=1.0, rx=1.0)])
        panel, _ = self._panel(reader, clk, ctrl)
        # _panel must pass motion_controller=ctrl
        panel.tick(); panel.tick()
        looks = [c for c in ctrl.calls if c[0] == "start_jog" and c[1] in (5, 7)]
        self.assertTrue(all(c[3] > 0 for c in looks))
```

- [ ] **Step 2: Run, verify failure** — FAIL (lookahead passed as 0).

- [ ] **Step 3: Implement** — in `_apply` for Start/UpdateContinuous, when `axis in (5, 7)` compute a look-ahead from the partner's current commanded speed-fraction and pass it:

```python
        if isinstance(intent, (StartContinuous, UpdateContinuous)):
            axis, frac = intent.axis, intent.frac
            sign = 1 if frac >= 0 else -1
            speed = self._direction_fn(axis, sign) * abs(frac) * self._speed_fn(axis)
            look = 0.0
            partner = {5: 7, 7: 5}.get(axis)
            if partner is not None:
                pf = self._controller._cont.get(partner)  # last partner frac, or None
                if pf:
                    # one update period of partner travel, in pulse: speed(pulse/ms)*period(ms)
                    look = abs(pf) * self._speed_fn(partner) * (self._controller.cfg.update_period_s * 1000.0)
            queue.submit(lambda a=axis, s=speed, lk=look: self._jog(a, s, lk), priority=5)
```
Add `partner_lookahead_pulse` to `_jog`:
```python
    def _jog(self, axis, speed, lookahead=0.0):
        self._motion().start_jog(axis, speed, partner_lookahead_pulse=lookahead)
```
(Reaching into `self._controller._cont` is acceptable since the panel owns the controller; alternatively expose a `last_frac(axis)` accessor on the controller — preferred. Add `def last_frac(self, axis): return self._cont.get(axis)` to `GamepadController` and use it.)

For stop-on-deadzone bypass: ensure `StopContinuous`/`StopAll` are submitted at priority 0 and `StopAll` always uses the direct `_safe_stop_all()` path (already done in D1). No rate-limit applies to stops (the controller only rate-limits `UpdateContinuous`, never `StopContinuous`).

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_gamepad_panel.py -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

## Phase E — main_window integration

### Task E1: `highlight_axis()` on the stage map

**Files:**
- Modify: `src/automatic_transfer_station/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run, verify failure** — FAIL (`highlight_axis` missing).

- [ ] **Step 3: Implement** — add to `MainWindow`:

```python
    def highlight_axis(self, axis: int | None) -> None:
        from .axis_calibration import AXIS_LABELS
        for ax, label in AXIS_LABELS.items():
            name = f"axisHotspot{label}"
            btn = self.controls.get(name)
            if btn is None:
                continue
            selected = (axis is not None and int(ax) == int(axis))
            if bool(btn.property("selectedAxis")) != selected:
                btn.setProperty("selectedAxis", selected)
                btn.style().unpolish(btn); btn.style().polish(btn)
```
Add a `[selectedAxis="true"]` rule to the hotspot stylesheet in `_add_stage_hotspot` (bright outline, e.g. `border: 3px solid #f59e0b;`).

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_main_window.py -k highlight_axis -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task E2: Construct `GamepadPanel`, wire providers, focus-loss disarm

**Files:**
- Modify: `src/automatic_transfer_station/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_gamepad_panel_constructed_and_disarms_on_window_deactivate(self):
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
        window.changeEvent(QEvent(QEvent.ActivationChange))
        # when not active, disarm should be called
        if not window.isActiveWindow():
            self.assertGreaterEqual(disarmed["n"], 1)
```

- [ ] **Step 2: Run, verify failure** — FAIL (`gamepad_reader` kwarg / `gamepad_panel` missing).

- [ ] **Step 3: Implement** — in `MainWindow.__init__` add `gamepad_reader=None` param; construct the panel after the axis panel exists, injecting providers:

```python
        self.gamepad_panel = GamepadPanel(
            register=self._register,
            motion_queue_provider=self._active_motion_queue,
            motion_controller=self.controller,
            stop_all=self._gamepad_stop_all,
            speed_fn=self._speed,
            step_fn=lambda axis: float(self.controls[self._step_control_name(axis)].value()),
            set_step_fn=self._gamepad_set_step,
            direction_fn=self._controller_direction_for_coordinate_direction,
            highlight_fn=self.highlight_axis,
            reader=gamepad_reader,
        )
```
Add helpers:
```python
    def _gamepad_stop_all(self):
        if self.controller.is_open:
            self.controller.stop_all()   # direct path; bypasses the queue for emergency/release stop

    def _step_control_name(self, axis):
        name = f"txtStep{axis}"
        return name if name in self.controls else f"txtStep{speed_axis_for(axis)}"

    def _gamepad_set_step(self, axis, delta):
        from .axis_calibration import snap_step
        ctrl = self.controls[self._step_control_name(axis)]
        ctrl.setValue(snap_step(axis, float(ctrl.value()), delta))
```
Place the panel in the layout (e.g., add to the axis control panel’s top row or below the status bar). Override `changeEvent`:
```python
    def changeEvent(self, event):
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.ActivationChange, QEvent.WindowStateChange):
            panel = getattr(self, "gamepad_panel", None)
            if panel is not None and not self.isActiveWindow():
                panel.disarm()
        super().changeEvent(event)
```
In `closeEvent`, stop the panel timer: `self.gamepad_panel._timer.stop()`.

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_main_window.py -k gamepad_panel_constructed -v` → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

### Task E3: GUI-vs-gamepad arbitration (disable GUI jog widgets while armed)

**Files:**
- Modify: `src/automatic_transfer_station/main_window.py`, `src/automatic_transfer_station/gamepad_panel.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_gui_jog_widgets_disabled_while_gamepad_armed(self):
        from automatic_transfer_station.main_window import MainWindow
        get_app()
        controller = RecordingController(); controller.is_open = True
        window = MainWindow(controller=controller, motion_command_queue_factory=_INLINE_QUEUE_FACTORY,
                            position_store_path=self.position_store_path)
        window.set_gamepad_armed(True)
        self.assertFalse(window.controls["buttonfwd5"].isEnabled())   # continuous jog pad
        self.assertFalse(window.controls["btnFwd5"].isEnabled())      # step button
        window.set_gamepad_armed(False)
        self.assertTrue(window.controls["buttonfwd5"].isEnabled())
```

- [ ] **Step 2: Run, verify failure** — FAIL (`set_gamepad_armed` missing).

- [ ] **Step 3: Implement** — add `MainWindow.set_gamepad_armed(armed)` that enables/disables the per-axis motion-pad + arrow buttons for the gamepad-driven axes (5,6,7,8,9,3,4):

```python
    def set_gamepad_armed(self, armed: bool) -> None:
        names = []
        for axis in (5, 6, 7, 8, 9, 3, 4):
            for prefix in ("buttonfwd", "buttonback", "btnFwd", "btnBack", "btnStop"):
                names.append(f"{prefix}{axis}")
        for name in names:
            w = self.controls.get(name)
            if w is not None:
                w.setEnabled(not armed)
```
Have `GamepadPanel` call an injected `on_armed_changed(bool)` whenever enable flips (in `_toggle_from_ui`, `disarm`, and when `tick()` syncs an auto-disarm). Wire `on_armed_changed=self.set_gamepad_armed` in `MainWindow`.

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_main_window.py -k arbitration -v` (or the test name) → PASS.

- [ ] **Step 5: Suite-green checkpoint** — `python -m pytest -q` → all pass.

---

## Phase F — Launcher wiring & final verification

### Task F1: `--no-gamepad` flag and reader wiring

**Files:**
- Modify: `src/automatic_transfer_station/run_eth_board_test.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_launcher_gamepad_default_on_and_flag_off(self):
        from automatic_transfer_station.run_eth_board_test import parse_args
        self.assertTrue(parse_args([]).gamepad)
        self.assertFalse(parse_args(["--no-gamepad"]).gamepad)
```

- [ ] **Step 2: Run, verify failure** — FAIL.

- [ ] **Step 3: Implement** — add the arg + wiring:
```python
    parser.add_argument("--no-gamepad", dest="gamepad", action="store_false", help="Disable F710 gamepad control.")
    parser.set_defaults(gamepad=True)
```
In `main()`, pass `gamepad_reader=load_default_reader() if args.gamepad else None` to `MainWindow` (import `load_default_reader` from `.gamepad_xinput`). `MainWindow` must skip constructing/timer-starting the panel when `gamepad_reader is None` (guard the construction).

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_launcher.py -v` → PASS.

- [ ] **Step 5: Final verification**

Run: `python -m pytest -q`  →  Expected: all pass (baseline 184 + all new gamepad tests).
Run: `python run_transfer_station.py --smoke-exit`  →  Expected: exit 0 (GUI builds with the gamepad panel; with no controller connected, the reader returns disconnected and nothing moves).
Run: `python run_transfer_station.py --no-gamepad --smoke-exit`  →  Expected: exit 0.

---

## Deferred (post-implementation, on real hardware — see spec §10)
Not part of this plan's tasks; do after the program is written:
1. Verify `GA_SetVel+GA_Update` smoothly retargets a TRAP cruise velocity; if so, add a `set_jog_velocity` fast path (and add it to the `MotionController` Protocol + `RecordingController`).
2. Evaluate `GA_PrfJog` velocity-mode-for-stick-axes alternative.
3. Confirm F710 X-mode independent triggers + button bit mapping on the bench.
4. Measure disconnect/focus-loss → stop latency end to end.
5. Tune deadzone / curve exponent / hold-delay / update rate / interference margin for feel.
