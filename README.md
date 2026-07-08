# Automatic Transfer Station

[![CI](https://github.com/wnz123123-bit/Automatic-transfer-station/actions/workflows/ci.yml/badge.svg)](https://github.com/wnz123123-bit/Automatic-transfer-station/actions/workflows/ci.yml)

Python GUI controller for the two-dimensional-material transfer station.

## Project Layout

```text
E:\Automatic transfer station
├── config\                     # Local controller configuration
├── docs\                       # Calibration, development, and handoff documents
├── runtime\                    # Runtime state loaded by the GUI
├── src\automatic_transfer_station\
│   ├── run_eth_board_test.py    # Application entrypoint
│   ├── main_window.py           # PySide6 GUI
│   ├── gas_api.py               # GAS.dll ctypes wrapper
│   ├── gas_controller.py        # Real motion-card controller
│   ├── temperature_panel.py     # RD105 GUI panel
│   ├── objective_panel.py       # LV-NCNT2 objective panel
│   ├── camera_controller.py     # Basler pypylon camera controller
│   ├── camera_measurement.py    # Camera measurement geometry and JSON sidecars
│   ├── camera_panel.py          # Basler camera preview panel
│   ├── camera_window.py         # Top-level Basler camera window
│   ├── gamepad_xinput.py        # XInput ctypes read layer (GamepadReader/GamepadSnapshot)
│   ├── gamepad_controller.py    # Pure decision logic: snapshot → motion intents
│   └── gamepad_panel.py         # Qt panel (QGroupBox) with QTimer polling loop
├── tests\                      # Unit and GUI smoke tests
├── vendor\motion_card_sdk\     # Vendor motion-card SDK files
├── pyproject.toml              # Test configuration
└── run_transfer_station.py     # Simple local launcher
```

`vendor\motion_card_sdk` is the renamed vendor support folder. It contains the motion-card SDK files such as `GAS.dll`, `GAS.lib`, `bopaipython\GAS_N.h`, and vendor examples. It is not the application source code.

## Run

From the project root:

```powershell
cd "E:\Automatic transfer station"
```

The program is real-hardware only; there is no simulation mode. It does not
connect at startup.

```powershell
python .\run_transfer_station.py
```

With LV-NCNT2 motorized nosepiece control:

```powershell
python .\run_transfer_station.py --nosepiece-port COM7
```

With RD105 temperature controller:

```powershell
python .\run_transfer_station.py --temperature-port COM5
```

### Basler camera workstation

The microscope camera opens as a second top-level window when launching the app
normally. It targets the Basler `puA2500-14ucMIC` USB camera with serial
`25141247`. Install Basler pylon Suite first, then install the Python binding in
the same Python environment used to launch this app:

```powershell
python -m pip install pypylon
```

Quick smoke check:

```powershell
python -c "from pypylon import pylon; print([(d.GetModelName(), d.GetSerialNumber()) for d in pylon.TlFactory.GetInstance().EnumerateDevices()])"
```

Close pylon Viewer before using the camera window; the viewer can hold the camera
open and prevent this application from grabbing frames.

The camera window uses a microscope-workbench layout: large live preview on the
left and grouped camera controls on the right. It auto-connects to the default
Basler camera when the window opens, so the acquisition group only exposes
Preview and Capture actions. It also supports save directory and filename
templates, TIFF/PNG/JPG output, independent Live/Snap pixel-resolution choices,
preview zoom readout with mouse-wheel zoom, exposure/gain, Gamma, sharpness,
horizontal+vertical software flip, and two-point scale calibration per objective
through a popup dialog. Camera preview annotations are rendered in red. The
measurement group provides a button-controlled center crosshair, two-point line
distance with a live first-point-to-cursor preview line, closed-polygon
area/perimeter with cursor preview while adding points, undo, clear,
delete-selected, and a measurement result table. Uncalibrated objectives can
still be measured in px / px^2; calibrated objectives use the current
`um_per_pixel` scale and display um/mm or um^2/mm^2 as appropriate. Live preview
shows the current FPS, caps camera grabs at 60 FPS, limits GUI frame
delivery/rendering to 30 FPS, coalesces queued frames so only the latest frame is
rendered, and uses Qt image transforms for preview flips to keep the
motion-control UI responsive while the camera is running. Live exposure, gain,
Gamma, and sharpness writes are applied without restarting preview;
acquisition-size changes still briefly pause preview while the camera is
reconfigured. Exposure defaults to manual 10 ms so a dark field cannot make
Basler auto exposure stretch preview grabs into second-long frames. The default
snapshot format is TIFF; the default filename template is `{date}_{time}_{objective}_{index}`.
Saved images include the red scale bar when the selected objective has been
calibrated, and the default annotated snapshot path also burns in measurement
objects/center crosshair plus writes a same-stem `.measurements.json` sidecar.

Runtime camera files:

```text
runtime\camera_snapshots\
runtime\camera_settings.json
runtime\camera_scale_calibration.json
runtime\camera_snapshots\*.measurements.json
```

### Gamepad control (Logitech F710)

Gamepad support is enabled by default (requires F710 set to **X mode** via the back-panel MODE switch). To disable it:

```powershell
python .\run_transfer_station.py --no-gamepad
```

If the XInput DLL fails to load the program still starts normally (a message is printed to stderr and gamepad support is silently disabled).

Quick button mapping (2026-07-01 remap): left stick = X3/Y3 continuous; right stick = X2/Y2 continuous; LT/RT = MicroscopeZ (step/hold, fixed 4 mm/s top speed); LB/RB = MicroscopeZ single-step down/up using the GUI MicroscopeZ step notch; D-pad up/down = single-step jog of the selected axis (step only, no continuous); D-pad left/right = step-size down/up; face buttons Y/A/X/B = 2D directional axis selection (up/down/left/right by the GUI axis-card grid layout) among X3/Y3/X2/Y2/R/Z/X1/Y1 (MicroscopeZ is not selectable); START = enable/disable gamepad; BACK = stop all (bypasses queue). The selected axis shows on a stage-map banner (word-wrapped, clamped to the image) with its live step size and coordinate. Press ? in the gamepad panel to view a labelled Logitech F710 diagram of the full button mapping; the global Space key is an emergency stop shortcut.

The default real mode does not connect at startup. The program connects to the motion card only after clicking `打开运动控制`.

## Test

```powershell
python -m pytest -q
python .\run_transfer_station.py --smoke-exit
```

Current baseline:

```text
625 passed, 6 warnings, 2 subtests passed
```

## Runtime State

The GUI keeps controller readback and user calibration state in:

```text
runtime\axis_positions.json
runtime\axis_runtime_calibration.json
runtime\microscope_z_positions.json
runtime\camera_snapshots\
runtime\camera_settings.json
runtime\camera_scale_calibration.json
runtime\camera_snapshots\*.measurements.json
```

These files are display/state recovery files. They do not write saved values back into the motion card.

## Safety Notes

The program does not connect at startup. All GUI motion commands, stop commands, R home/twist, MicroscopeZ point moves, Z popup moves, and position refreshes run through the worker command queue so hardware calls do not block the UI thread. The queue only exists while the board is open: motion commands issued before the board is opened, or after it is closed, are ignored and never recreate the worker (they cannot resurrect a stale queue). Dangerous operations (calibration overwrite, setting temperature at or above 150 °C, R axis homing) require explicit confirmation before executing. When an axis is stopped automatically by interference detection or a soft-limit, the corresponding axis card is highlighted with a red border and the top status indicator enters a persistent alarm state; it clears only when the axis returns to a safe range.

When gamepad control is enabled the GUI disables the corresponding axis buttons (continuous, jog, and single-axis stop for axes controlled by the gamepad) to prevent conflicting inputs. The global Stop button is always active. Gamepad control is automatically disarmed — and all motion stopped — on window minimize/unfocus, controller disconnect, or prolonged inactivity. The false-disarm that triggered on a held stick or trigger (packet number unchanged while physically pressed) has been removed; the remaining safety nets are disconnect auto-stop, neutral-input inactivity auto-stop, soft-limit/interference clamping on continuous jog, focus-loss disarm, and BACK/Space emergency stop.

Board open/close/reset (GA_Open/GA_Close/GA_Reset) run off the GUI thread so an unreachable board no longer freezes the window for the ~21 s OS TCP timeout. The RD105 heater has an independent over-temperature watchdog thread that forces the heater off when the GUI poll stops or communication is lost, and writes all safety-relevant events to a dedicated audit log (`runtime/transfer_station.audit.log`), separate from the main `runtime/transfer_station.log`.

The motion-safety core has received a dedicated 6-dimension review (CP1–CP5): the reactive-scan position refresh now has a bounded watchdog (hung read → loud alarm + generation bump + recoverable pending latch + best-effort stop); soft-limit endpoints use directional rounding so fractional re-zero values cannot silently block a jog direction; MicroscopeZ persisted pulses are guarded against int32 wraparound and a conservative physical sanity cap; board reset re-applies the hard-limit configuration; and the X2/X3 interference partner check is fail-safe (missing partner position → refuse move rather than silently skip).

Real-hardware debugging (post-CP5) produced the following additional fixes. `GA_SetCardNo(0)` has been removed from `open_board`; it was unnecessary for single-board Ethernet (the DLL defaults to card 0) and returned error 7 on real hardware, blocking the open. The LV-NCNT2 objective nosepiece defaults to COM7 (`DEFAULT_NOSEPIECE_PORT`); pass `--nosepiece-port` to override. The `cRCW` (forward-step) command on LV-NCNT2 frequently returns a Control Timeout reply even when the turret has physically moved; the controller now reads the actual position via RAR to confirm arrival instead of treating the timeout as a hard failure, retrying up to 8 steps. The stage map device diagram now overlays a selected-axis banner and auto-declutters overlapping axis labels at any window size. MicroscopeZ (axis 3) continuous motion was the only axis using a `GA_PrfJog` velocity jog, which did not produce motion while held on the real card (the axis appeared to move only on release); it now uses the same `GA_PrfTrap` profiled move as every other axis — toward a far target at the MicroscopeZ sanity cap in the jog direction, stopping on release — so both the GUI continuous buttons and the gamepad LT/RT triggers move it while held, proportionally up to the axis speed cap at full deflection. That sanity cap is a stuck-button backstop, not a real travel limit; axis 3 still has no travel switch and needs manual supervision near the optics. The MicroscopeZ speed is capped at 4 mm/s (4000 um/s): the focus stepper stalls at 5000 um/s (= 250 kHz step rate at 50000 pulse/mm) — it loses steps while the commanded-position readout keeps advancing — so 5000 is not offered. The gamepad LT/RT triggers drive MicroscopeZ at a FIXED 4 mm/s top speed (`GAMEPAD_MICROSCOPE_Z_SPEED_UM_S`), decoupled from the GUI MicroscopeZ speed dropdown — the trigger scales 0..4 mm/s proportionally regardless of the on-screen setting, which only affects the GUI MicroscopeZ buttons. Finally, Y3 (axis 6) and Y2 (axis 8) had their `pulse_sign` flipped to -1 because the real motors run opposite to the software's assumed +pulse direction; this reverses both the jog direction and the coordinate readout (GUI buttons and gamepad alike). Y2's asymmetric soft limits were swapped+negated to keep the exact same physical pulse envelope. This is a real-hardware direction correction — verify the physical direction of Y3/Y2 with small steps (especially Y2, near the slide) before trusting it.

RD105 temperature control uses PID plus maximum output duty from `config\rd105_config.json`. The software target limit is `205.0 °C`; readings above that limit or invalid readings cancel the software ramp and attempt to disable output. If output disable fails, the GUI reports that failure instead of claiming the output is closed. All RD105 serial I/O (polling, set/ramp, connect, stop, disconnect) and the LV-NCNT2 connect/read run on worker threads, so the disconnect button never blocks the GUI. On app exit the heater-disable/disconnect also runs on a worker, but close waits a bounded time (`HEATER_OFF_CLOSE_TIMEOUT_MS`, 3 s) for ENABLE=0 to reach the standalone RD105 before the process exits — a wedged serial driver is capped at that timeout and detached so close still cannot hang.

## Motion-Card Safety Probe

Open and close the network motion-card connection without reset, axis enable, or motion:

```powershell
python -m automatic_transfer_station.gas_network_probe --ip 192.168.0.200 --mode 0
```

If running this module directly from the source tree without installing, keep `src` on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "E:\Automatic transfer station\src"
python -m automatic_transfer_station.gas_network_probe --ip 192.168.0.200 --mode 0
```

## Key Documents

- `docs\axis_calibration_records.md`: canonical axis calibration and limit-switch record.
- `docs\development_process.md`: development history.
- `docs\handoff.md`: operational handoff notes.
