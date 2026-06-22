# EthBoardTest Python GUI Migration Implementation Plan

> **历史归档（2026-05-28）：** 本文档记录最初的迁移计划，其中的模拟控制器（`sim_controller.py` / `SimMotionController` / `--sim`）和 `real_mode` 形参已在 2026-06-20 整体移除，程序现为纯真实硬件。当前状态以 `docs\development_process.md` 第 11 节和 `docs\handoff.md` 为准；本文件仅作历史参考，不再更新。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python PySide6 replacement for the existing C# WinForms motion-card GUI with equivalent user-facing controls and motion-card behavior.

**Architecture:** The GUI talks to a controller interface. Real hardware access is isolated in a `ctypes` wrapper around `GAS.dll`; simulation uses the same controller interface and is the default startup mode.

**Tech Stack:** Python 3.13, PySide6, `ctypes`, `unittest`, standalone app folder `EthBoardTest_Python/` with `GAS.dll`.

---

## File Structure

- Create `EthBoardTest_Python/gas_api.py`: low-level DLL loader, `ctypes` structures, signatures, and typed wrapper methods.
- Create `EthBoardTest_Python/controller_base.py`: dataclasses, protocol, constants, and shared axis metadata.
- Create `EthBoardTest_Python/gas_controller.py`: real hardware controller preserving current C# axis configuration and motion commands.
- Create `EthBoardTest_Python/sim_controller.py`: no-hardware controller with deterministic simulated positions.
- Create `EthBoardTest_Python/main_window.py`: PySide6 GUI matching the current control groups and workflows.
- Create `EthBoardTest_Python/run_eth_board_test.py`: launcher with default simulation mode and explicit `--real`.
- Modify `tests/test_gas_network_probe.py`: keep existing probe safety coverage.
- Create `tests/test_gas_api.py`: wrapper and signature tests with fake DLL functions.
- Create `tests/test_gas_controller.py`: controller behavior tests with fake API.
- Create `tests/test_sim_controller.py`: simulation behavior tests.
- Create `tests/test_main_window.py`: GUI smoke and dispatch tests in offscreen mode.

No commits are part of this plan. The user instruction requires explicit confirmation before version changes or commits, and this directory is not a Git repository.

## Task 1: GAS API Wrapper

**Files:**
- Create: `EthBoardTest_Python/gas_api.py`
- Test: `tests/test_gas_api.py`

- [ ] **Step 1: Write failing tests**

```python
def test_loads_open_close_signatures_on_supplied_dll():
    dll = FakeDll()
    api = GasApi(dll=dll)
    assert dll.GA_Open.argtypes == [ctypes.c_short, ctypes.c_char_p]
    assert dll.GA_Close.argtypes == []

def test_open_network_encodes_ip_as_ascii():
    dll = FakeDll()
    api = GasApi(dll=dll)
    assert api.open_network("192.168.0.200", mode=0) == 0
    assert dll.GA_Open.calls == [(0, b"192.168.0.200")]

def test_get_prf_pos_passes_axis_count_and_clock_pointer():
    dll = FakeDll()
    dll.GA_GetPrfPos.side_effect = lambda axis, value, count, clock: setattr(value._obj, "value", 12.5) or 0
    api = GasApi(dll=dll)
    assert api.get_prf_pos(3) == 12.5
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_gas_api -v`

Expected: fail with `ModuleNotFoundError` or missing `GasApi`.

- [ ] **Step 3: Implement minimal wrapper**

Implement `GasApi`, `TrapPrm`, `JogPrm`, `load_default_api`, and methods needed by the controller: open/close/reset, axis on/off, clear status, hard limit mapping, limit on/off, trap/jog parameter setters, set/get position and velocity, update, stop, zero, home set/start.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.test_gas_api -v`

Expected: all tests pass.

## Task 2: Real and Simulated Controllers

**Files:**
- Create: `EthBoardTest_Python/controller_base.py`
- Create: `EthBoardTest_Python/gas_controller.py`
- Create: `EthBoardTest_Python/sim_controller.py`
- Test: `tests/test_gas_controller.py`
- Test: `tests/test_sim_controller.py`

- [ ] **Step 1: Write failing tests**

```python
def test_open_board_configures_axis_limit_mapping_and_enables_axes():
    api = FakeApi()
    controller = GasMotionController(api=api)
    controller.open_board()
    assert api.calls[:1] == [("open_network", "192.168.0.200", 0)]
    assert ("set_hard_limit_positive", 6, 1, 0, 14) in api.calls
    assert ("set_hard_limit_negative", 6, 1, 0, 9) in api.calls
    assert ("limits_off", 3, -1) in api.calls
    assert ("limits_off", 9, -1) in api.calls

def test_relative_move_uses_current_position_and_axis_mask():
    api = FakeApi()
    api.positions[2] = 100.0
    controller = GasMotionController(api=api)
    controller.is_open = True
    controller.relative_move(axis=2, direction=-1, speed=20, step=5)
    assert ("set_position", 2, 95) in api.calls
    assert ("update", 0b10) in api.calls

def test_sim_controller_updates_positions_without_hardware():
    controller = SimMotionController()
    controller.open_board()
    controller.relative_move(axis=3, direction=1, speed=20, step=10)
    assert controller.get_axis_position(3) == 10.0
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_gas_controller tests.test_sim_controller -v`

Expected: fail with missing modules/classes.

- [ ] **Step 3: Implement controllers**

Implement shared constants for the current C# behavior:

```python
BOARD_IP = "192.168.0.200"
OPEN_MODE = 0
AXIS9_DEGREES_PER_PULSE = 1.0
AXIS_LIMITS = {
    1: (1, 0),
    2: (2, 3),
    4: (5, 4),
    5: (6, 7),
    6: (14, 9),
    7: (11, 10),
    8: (13, 12),
}
```

Real controller methods must raise a controller error if motion is requested before the board is open. Simulation controller must mirror the public methods without touching `GAS.dll`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.test_gas_controller tests.test_sim_controller -v`

Expected: all tests pass.

## Task 3: PySide6 GUI

**Files:**
- Create: `EthBoardTest_Python/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing tests**

```python
def test_main_window_launches_with_sim_controller():
    app = get_app()
    controller = SimMotionController()
    window = MainWindow(controller=controller, real_mode=False)
    assert window.windowTitle() == "EthBoardTest Python"
    assert "btnOpen" in window.controls
    assert "btnAxis9MoveToAngle" in window.controls

def test_axis_button_dispatches_relative_move():
    app = get_app()
    controller = RecordingController()
    window = MainWindow(controller=controller, real_mode=False)
    window.controls["btnFwd3"].click()
    assert ("relative_move", 3, 1, 20.0, 20.0) in controller.calls
```

- [ ] **Step 2: Run tests to verify RED**

Run: `set QT_QPA_PLATFORM=offscreen && python -m unittest tests.test_main_window -v`

Expected: fail with missing `main_window`.

- [ ] **Step 3: Implement GUI**

Build a `QMainWindow` with controls equivalent to the current WinForms GUI:

- board controls: `btnOpen`, `btnClose`, reset button.
- axis groups for axes 1-4, coupled axes 5/6, coupled axes 7/8, and axis 9.
- speed and step spin boxes named like the C# controls.
- buttons named like the C# controls where practical.
- mouse press/release jog handling.
- 100 ms timer that updates axis 3 position and axis 9 angle labels.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `set QT_QPA_PLATFORM=offscreen && python -m unittest tests.test_main_window -v`

Expected: all tests pass.

## Task 4: Launcher and Integration Verification

**Files:**
- Create: `EthBoardTest_Python/run_eth_board_test.py`
- Modify: `tests/test_gas_network_probe.py` only if import paths need adjustment.

- [ ] **Step 1: Write failing test for launcher parser**

```python
def test_launcher_defaults_to_simulation_mode():
    args = parse_args([])
    assert args.real is False
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest discover -s tests -v`

Expected: fail with missing launcher parser or missing GUI imports.

- [ ] **Step 3: Implement launcher**

Implement `parse_args(argv=None)`, `build_controller(args)`, and `main(argv=None)`. Default to simulation mode. Use real `GasMotionController(load_default_api())` only when `--real` is passed.

- [ ] **Step 4: Run complete verification**

Run:

```powershell
python -m unittest discover -s tests -v
$env:QT_QPA_PLATFORM='offscreen'; python -c "import sys; sys.path.insert(0, r'D:\EthBoardTest_0127\EthBoardTest_Python'); from run_eth_board_test import main; raise SystemExit(main(['--smoke-exit']))"
python 'EthBoardTest_Python\gas_network_probe.py' --dll 'EthBoardTest_Python\GAS.dll' --ip '192.168.0.200' --mode 0
```

Expected:

- all unit tests pass.
- GUI smoke exits with code 0 in simulation mode.
- real network probe still reports `GA_Open -> 0` and `GA_Close -> 0`.

## Self-Review

- Spec coverage: tasks cover DLL wrapper, real controller, simulation controller, GUI, launcher, tests, and real open/close probe.
- Placeholder scan: no `TBD`, `TODO`, or unspecified implementation steps remain.
- Type consistency: controller, API, GUI, and launcher names are fixed and used consistently across tasks.
