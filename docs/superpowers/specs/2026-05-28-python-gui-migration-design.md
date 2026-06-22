# EthBoardTest Python GUI Migration Design

Date: 2026-05-28

> **历史归档：** 本设计文档中的模拟控制器（`sim_controller.py` / `SimMotionController`）和 `real_mode` 已在 2026-06-20 移除，程序现为纯真实硬件。当前状态以 `docs\development_process.md` 第 11 节和 `docs\handoff.md` 为准；本文件仅作历史参考，不再更新。

## Goal

Rewrite the existing C# WinForms motion-card GUI as a Python application while keeping the visible GUI workflow and motion functions equivalent to the current program.

The Python application must use the official `GAS.dll` network API through `ctypes`. It must preserve the current hardware behavior unless a change is explicitly requested.

## Current Baseline

- Existing entrypoint: `Program.cs`, which launches `Form1`.
- Existing GUI and logic: `Form1.cs` plus `Form1.Designer.cs`.
- Current C# hardware backend: `MultiCardCS` / `MultiCardCLR`.
- Python SDK files are available under `python/`; the standalone migrated app lives under `EthBoardTest_Python/`.
- `EthBoardTest_Python/GAS.dll` loads successfully from 64-bit Python.
- A network open/close probe already succeeded with `GA_Open(0, "192.168.0.200") -> 0` and `GA_Close() -> 0`.

## Architecture

Use a layered Python structure:

- `EthBoardTest_Python/gas_api.py`
  - Loads `GAS.dll`.
  - Defines `ctypes` function signatures and small structures.
  - Exposes only low-level GAS calls.

- `EthBoardTest_Python/controller_base.py`
  - Defines the controller interface used by the GUI.
  - Keeps GUI code independent from real hardware implementation details.

- `EthBoardTest_Python/gas_controller.py`
  - Implements the real controller using `gas_api.py`.
  - Owns board connection state, axis enabling, limit configuration, movement, stopping, position reads, and homing.

- `EthBoardTest_Python/sim_controller.py`
  - Implements the same controller interface without hardware.
  - Allows the GUI to launch and be exercised safely on any PC.

- `EthBoardTest_Python/main_window.py`
  - Recreates the current WinForms GUI in PySide6.
  - Preserves current button labels, default speed/step values, axis grouping, and workflows as closely as practical.

- `EthBoardTest_Python/run_eth_board_test.py`
  - Starts the application.
  - Defaults to simulation mode unless `--real` is passed.

## Hardware Behavior

The first Python version must preserve the current C# logic:

- Open board with network mode `GA_Open(0, "192.168.0.200")`.
- On user clicking "open board" in real mode:
  - Call `GA_Open`.
  - Configure axes 1 through 9.
  - Enable axes and clear statuses like the current C# code.
  - Apply the current hard-limit mapping:
    - axis 1: positive HOME2/index 1, negative HOME1/index 0
    - axis 2: positive HOME3/index 2, negative HOME4/index 3
    - axis 3: limits off
    - axis 4: positive HOME6/index 5, negative HOME5/index 4
    - axis 5: positive HOME7/index 6, negative HOME8/index 7
    - axis 6: positive HOME15/index 14, negative HOME10/index 9
    - axis 7: positive HOME12/index 11, negative HOME11/index 10
    - axis 8: positive HOME14/index 13, negative HOME13/index 12
    - axis 9: limits off
- Relative step movement:
  - Use trap mode.
  - Read current planned position.
  - Move to current position plus signed step.
- Jog movement:
  - Use jog mode.
  - Start on mouse press.
  - Stop the corresponding axis on mouse release.
- Axis 3:
  - Display planned position.
  - Zero current position.
  - Mark current position.
  - Move back to marked position.
- Axis 9:
  - Display angle from planned position.
  - Home via HOME mode.
  - Move to entered angle by converting angle to pulses.

## Safety Rules

- The application must not connect to real hardware at startup.
- Default startup mode is simulation.
- Real hardware mode must be explicit through `--real`.
- No reset, axis enable, or motion command may run before the user clicks "open board" in real mode.
- Tests must cover that probe/open-only paths do not call reset, axis enable, update, or stop.
- Movement tests use fake DLL/controller objects, not real hardware.
- Real motion validation is not automatic. Each real motion test must be separately confirmed by the user.

## GUI Requirements

The GUI should match the existing program's functional layout:

- Top-level board buttons:
  - open board
  - close board
  - reset board
- Axis groups:
  - axis 1 through 4 controls
  - paired axes 5/6 and 7/8 controls
  - axis 3 mark/zero/move controls
  - axis 9 rotation controls
- Numeric speed and step controls keep existing defaults and ranges where visible in the designer.
- Labels should continue to show live axis 3 position and axis 9 angle.
- A timer should refresh displayed positions, but only after the board is open or when using simulation.

## Testing Plan

Use Python unit tests before implementation of production behavior:

- DLL/probe safety tests:
  - open/close calls only.
  - failed open does not call close.

- GAS wrapper tests:
  - correct argument encoding for `GA_Open`.
  - correct masks for axis update and stop.
  - correct `ctypes` structures and function signatures for trap, jog, home, and position read calls.

- Controller tests:
  - opening the board configures axis limits exactly as specified.
  - relative movement computes target from current planned position.
  - jog starts with signed velocity and stops the same axis.
  - axis 3 mark/move uses stored planned position.
  - axis 9 angle conversion uses `axis9_degrees_per_pulse`.

- GUI smoke tests:
  - app launches in simulation mode.
  - controls exist and dispatch to controller methods.
  - timer updates displayed position labels in simulation.

## Non-Goals

- Do not redesign the experiment workflow.
- Do not change version numbers.
- Do not create commits.
- Do not package or release the application in this phase.
- Do not automatically run real motion commands during validation.
