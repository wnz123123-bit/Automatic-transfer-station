# Laser cutting implementation and commissioning

## 使用顺序

右侧分为 **加工 / Cutting** 和 **设置 / Setup**。加工页按拍照、路径、执行排列；
更多图像选项、绘图帮助和区域配方默认收起。单线模式不显示区域扫描参数，
切换连续行扫描或点阵时只显示该模式需要的参数。
标定、回程差补偿、运动与激光连接集中在设置页，硬件诊断可按需展开。
路径预览、执行按钮以及停止按钮固定在滚动区域外；切换页签不会改变运行条件。

左侧图像区分为 **定位与路径 / Reference and path** 和 **实时图像 / Live view**。
拍摄定位照片后自动进入定位页；开始切割或关光空跑时自动进入实时页，复用同一
相机连接及采集线程。运行中可以切回查看定位照片和路径，但不能修改。
实时图像只用于观察，不叠加静态切割路径，也不替换定位照片、ROI 或标定数据。
画面超过 1.5 秒未更新会提示暂停；连接断开后清空旧画面。

1. 从主界面打开 **Laser cutting / 激光切割**，使用现有相机预览。
2. 在激光断电或光路物理遮挡时完成 X3/Y3 插补调试；本次连接确认后拍摄新的定位照片。
3. 在至少三个不共线位置拍照并点击同一固定特征，另行设置激光参考点。标定采样要一致地消除机械回差，建议用独立第四点检查误差。
4. 在定位照片上选择 ROI、绘制切割线，填写两轴实测回差和补偿百分比，生成路径预览。
5. 先执行关光空跑。运行后照片失效，下一次运行须重新拍摄并核对样品位置。
6. 真实出光另需匹配的新固件、独立关光联锁、运动卡触发接线和实测调试记录。当前仅 Arduino Comp 的接线不满足这些条件。

区域加工时，在路径模式中选择连续扫描线或点阵，绘制多边形（双击或 Enter
闭合，Backspace 撤销顶点，Esc 取消），也可直接使用 ROI 边界。设置扫描角度、
行间距；点阵另设点间距、到位等待时间和曝光时间。生成完整路径后先关光空跑。
配方可以保存和载入这些参数；载入后仍须重新拍照对齐并生成路径。

停止按钮始终显示，Space 也可取消。关闭切割窗口或主程序 Exit 会先请求关光、停止并等待后台任务结束，再关闭串口；不能将串口 ACK 当成实测光功率为零。

## Coordinate and optical references

The cutting workspace uses **X3/Y3 (physical axes 5/6, adsorption/sample stage)**.
No other axis is part of a cutting path. The camera is the existing Basler
connection; the workspace displays unflipped, full acquired pixels. ROI selection
does not redefine the image origin. Saved pictures retain both full frame and ROI.

Live monitoring uses an independent read-only canvas and a latest-frame buffer,
rendered at most every 100 ms by the workspace timer. The reference canvas remains
frozen throughout execution. Viewing either tab does not change the accepted
geometry or release motion ownership. Main-window configuration remains locked
while the shared camera acquisition thread continues to deliver frames. Preview
startup does not reconnect the camera or change resolution/exposure settings.
The frame-age warning alone is informational; the existing camera identity,
optics and reference guards still apply to execution.
Malformed preview pixels are rejected before their dimensions can be treated as
optical evidence: the monitor shows an unavailable image without altering the
accepted reference. A valid frame with changed dimensions/binning still triggers
the original optical-context invalidation and cancellation.

A reference capture reserves motion, reads all nine planner positions, flushes
the camera acquisition queue, acquires a fresh frame, and checks the reference
again. Motion/reset/coordinate changes invalidate the capture even after returning
to the original position. Camera connection, objective, dimensions and binning
belong to the reference. **Planner readback is not encoder feedback** and cannot
prove that the sample physically followed the command.

Calibration samples and saved fits also bind the stationary raw positions of
axes 1–4 and 7–9 (within 0.5 planner pulse of the original reference). Moving the
microscope Z or another non-sample axis requires a new calibration. Sample X3/Y3
motion between calibration photos is expected and does not discard the fit.

Track the same stationary sample landmark at at least three noncollinear stage
positions. The fit is `pixel = origin + J * stage_um`. Set the laser intersection
separately in raw pixels; it is never assumed to be the image centre. A selected
pixel in a photo taken at `s0` maps to `s0 + inverse(J) * (laser_pixel - pixel)`.
Residual limits are expressed in physical micrometres. For an exactly determined
three-point fit, an independent fourth point is needed to assess accuracy.

## Backlash and dark travel

Each axis has a measured backlash in micrometres and a percentage multiplier.
Effective take-up is measured backlash times percentage / 100. Before a line,
approach its start from the intended cutting direction with the beam inhibited.
Compensation is not accumulated into every target and is not a percentage of
the total cut length. Preload and repositioning moves are visible in the preview
and pass the same travel/collision checks as the cutting segment.

## Motion commissioning

Coordinate interpolation is implemented using the supplied GAS SDK's packed
`TCrdPrm`, `GA_LnXY`, coordinate FIFO and status APIs. Ordinary axis moves are
not used to approximate a simultaneous diagonal cut. The implementation requires
equal pulse/mm scales for X3/Y3. All nine axes must be stationary at acquisition;
manual motion, homing, reset and coordinate changes cannot compete with a job.

Before attesting motion qualification for a connection, verify with the laser
powered off/physically blocked: SDK ABI and absolute origin, axes 5/6 mapping,
direction and physical distance, vector speed/acceleration, target completion,
stop/FIFO clearing, travel limits and X2/X3 interference. Qualification is revoked
on reconnect/reset/calibration changes. Mock tests and DLL exports do not qualify
hardware. A dry-run confirmation means the operator has physically inhibited the
laser; a disconnected Comp cable is not proof of optical darkness.

## Laser hardware boundary

The supplied MLC03A firmware and GUI are reference material and are not overwritten.
The current apparatus has only Arduino Comp control, with no independent shutter.
MLC Comp LOW selects Offset, HIGH selects Nominal; initial startup requires HIGH.
The documented internal pull-up and Arduino reset/power-loss behaviour mean that
software alone cannot guarantee a dark reset/disconnection. Independent fail-dark
inhibit is required before autonomous live execution can be qualified.

Comp gating passes an envelope of a picosecond pulse train. A millisecond exposure
is not a single picosecond pulse. Gate command acknowledgment is not optical power
measurement. USB host timing is not a qualified spatial trigger for continuous
cutting. Real emission remains separately inhibited until gate hardware and timing
are qualified; no mock result automatically enables it.

Reference: Teem Photonics MLC controller manual, 21059624 Rev. 006, Comp input
section; the original controller files supplied by the laboratory.

## Validation record

Implementation is split into three batches, each followed by the full regression
suite and an independent Claude Code review. Software tests use fake devices and
offscreen Qt; no hardware motion or laser emission is performed during development.
Batch results are appended here after the corresponding checks complete.

Batch 1 final regression: 991 tests and 6 subtests passed in all 31 modules.
Native Windows screenshots were inspected in Chinese/English at 1180×780 and
800×560. Intermittent native Qt access violations in single-process legacy GUI
tests were not reproducible as assertion failures. Test cleanup now releases
tracked windows, timers and finished workers; `scripts/run_regression.py` runs
each test module in a new process. Any assertion failure, native crash or timeout
still fails the regression; there are no automatic retries or skipped modules.
CI uses the same runner.

Actual Claude Code (claude-sonnet-4-6) reviewed motion/shutdown and geometry/UI.
An abort/cleanup race was corrected and regression-tested. Camera preview restart
failure retains the known stopped state and reports both acquisition and restart
errors. Four subsequent review claims were challenged with source and independent
numerical evidence; Claude explicitly withdrew all four, with no remaining blocker.

## Qualified live line execution

The new `firmware/MLC03A_TransferStation` sketch and `laser_controller.py` implement
a versioned session/sequence-matched protocol, bounded serial I/O, priority OFF,
latched faults and a 500 ms MCU watchdog in ARMED/FOLLOW/FIRING. STATUS does not
renew emission permission. A job carries a stop generation so a late command from
a cancelled job cannot re-arm. The GUI never supplies background keepalives.
Asynchronous fault reports may carry the last MCU-processed sequence, earlier
than the newest host-sent command; session/boot, monotonic report IDs and processed
sequence bounds still reject replay/future reports. Startup confirmation checks
fresh cached driver state again when its GUI completion is delivered.
Firmware startup keeps Comp HIGH as required by the manufacturer. Connection
does not assert that startup/self-test has completed; an explicit operator action
checks startup, sends OFF then confirms the check. A serial ACK is not an optical
measurement. Port closure may reset the Arduino and must be protected externally.

Both firmware wiring capabilities ship false. Live execution requires an explicit
commissioning record matching the axis configuration, independent inhibit tests,
board-output polarity/bit mapping and measured trigger latency. Templates contain
no guessed output mapping or timing values and do not enable emission. Motion and
gate qualification are revoked by controller reset/reconnection/configuration.

A continuous line uses dark preload, dark acceleration lead-in, a constant-speed
cutting segment, and dark deceleration lead-out. The motion-card FIFO queues gate
changes at cut boundaries; Arduino FOLLOW gates this signal only while its bounded
lease, interlock and window remain valid. Lead distance and speed must satisfy
acceleration, pulse-quantization margin and measured spatial-error bounds. The
trigger must be inactive before FOLLOW; both host and firmware reject an already
active input rather than opening at a stationary lead-in point. All endpoints, including dark
travel, pass machine bounds/collision checks before any movement. When a live
route is previewed, dark rehearsal traverses those same points (stopping between
points; this is not a qualification of continuous-motion trigger timing).

Batch 2 regression: **1251 tests + 6 subtests, all 35 modules passed**. The batch-2
firmware (protocol 2.0) compiled for Arduino Uno/AVR core 1.8.8: 7,170 bytes flash
and 1,222 bytes static RAM. Batch 3 replaces it with protocol 2.1 below.
Compilation and a Python protocol peer do not execute the real sketch or prove
electrical/optical timing. No ports were opened or firmware uploaded.

Independent review corrected inactive-trigger enforcement, processed EVENT
sequence handling, queued startup-result freshness, pulse rounding margins,
preview/rehearsal consistency and idempotent cancellation. Unexpected MCU window
expiry remains a failure; endpoint arrival alone cannot prove complete exposure.
The legacy temperature test crash was reproduced with retained standalone panels
and active timers; test-only cleanup now retains owners through worker completion,
then deletes them. It passed repeated isolated runs and the full regression.

## Area ablation and recipes

The area tool accepts a closed simple polygon (including concave outlines), or
uses the selected ROI as its outline. Scan geometry is computed in physical
X3/Y3 micrometres through the full fitted transform. Row pitch is perpendicular
to the scan direction; point pitch is along it. Scan angle is relative to +X3.
The pattern is clipped to the ROI. Separate spans of a concave row remain
separate exposures; gaps and returns are dark. Rows run in one direction with
explicit dark backlash take-up instead of alternating the cutting direction.

- **Continuous rows** use the qualified board-triggered line execution. Point
  pitch and dwell parameters do not subdivide a continuous exposure.
- **Dots** travel dark, settle at each target, emit a bounded millisecond FIRE
  envelope, verify OFF, then travel to the next point. Point pitch and row pitch
  are independent. Dwell controls an envelope of the ps pulse train, not PRF or
  single-pulse selection.

All routes are built before the first move, including lead-in/out, returns and
preloads. Exposure counts, action counts and nominal duration are bounded; a job
also has a monotonic runtime deadline. Geometry includes beam **centres** only,
not a measured spot footprint or material-dependent heat-affected zone. Account
for spot size when choosing the polygon/ROI and spacing.

Recipes save camera context, ROI, polygon, raster settings and backlash settings.
They do not save active motion, laser permission, hardware qualification or an
executable job. Load onto a fresh matching reference photo, realign the pattern
to the sample, and generate a new preview. Matching camera/objective names alone
do not establish alignment with the same material feature.

## Batch 3 validation

Final regression: **1499 tests + 6 subtests, all 39 modules passed**. An independent
seeded geometry check covered 80 rotated, reflected and skewed scan/dot cases,
checking 22,730 centre samples against both the ROI and a concave outline.
Native Windows line/dot views were checked in Chinese and English at 800×560
and 1180×780; controls remained readable and STOP stayed visible.

Protocol 2.1 attributes each FIRE to its actual command sequence and original stop
generation. Only an explicit natural `FIRE_DONE` receipt permits advancement to
the next dot; OFF, faults and window expiry cannot substitute for that receipt.
Firmware transmits bounded complete frames without interleaving event/ACK bytes.
If an already-transmitted FIRE_DONE event arrives while OFF/ESTOP awaits its ACK,
the driver drains that known old event without restoring completion authority or
cancelling the stop command.

Both line and area runners close their abort entry atomically before releasing
motion ownership. A completed runner cannot send OFF into a later job. Failed
cleanup waits for independent abort workers and retains the UI/recovery interlock
until OFF and stop are confirmed; retrying cleanup never resumes cutting.

Cross-review reproduced and corrected near-axis numerical over-insetting,
pre-stop FIRE completion telemetry, and late-abort handoff races. The lifecycle
tests reproduced six failures before the fix; all 16 lifecycle cases passed five
additional isolated-process runs afterward. Geometry, UI, protocol and both
execution paths were reviewed with actual Claude Code (claude-sonnet-4-6).

The final 2.1 sketch compiled for Uno/AVR core 1.8.8 in both configurations:

| Compile-only configuration | Flash | Static RAM | Remaining RAM |
| --- | ---: | ---: | ---: |
| Repository defaults, both wiring capabilities false | 7,662 B | 1,469 B | 579 B |
| Temporary copy with both capabilities true | 8,412 B | 1,503 B | 545 B |

The temporary build checks branches omitted by constant-folding the default
configuration. It does not qualify wiring, stack headroom under real operation,
firmware execution timing or optical behaviour. Repository defaults remain false.

## Compact sidebar validation

The Cutting/Setup split and collapsed diagnostics were checked with native Qt
renders in Chinese and English at 1180×780 and 800×560. Single-line controls fit
without scrolling; the small dot-pattern view uses vertical scrolling while
preview, run and STOP remain fixed. No clipped buttons or horizontal overflow
were observed in those configurations. These previews used simulated images and
devices; no hardware was connected.

Regression: **1508 tests + 6 subtests, all 39 modules passed**. Navigation and
language changes preserve the plan and cannot attest hardware or send commands.
Returning from Setup clears an active calibration drawing tool. Cross-review
caught a lock-taking controller property in the new status renderer; the read
was removed and covered by a regression test, retaining worker-side qualification
checks. Actual Claude Code reviewed the revised presentation source and did not
confirm any new blocking finding.

## Live-monitor validation

Regression after integration and malformed-frame isolation: **1566 tests + 6
subtests, all 40 modules passed**. Tests exercise the actual camera preview QThread
with a fake driver while MainWindow disables camera configuration, successive
frames during a dark-motion worker, automatic monitoring for execution job types,
reference preservation, STOP, close/reconnect, stale/invalid frames and bilingual
status updates. The new monitoring widget has no device-control dependency.

Native Qt views were checked in Chinese and English at 1180×780 and 800×560,
including the live, paused and frozen-reference pages. STOP remained visible and
no clipped buttons or horizontal overflow were observed. Independent cross-review
identified the malformed-preview/context ordering issue, and verified its fix;
actual Claude Code reviewed both integration and the final fix without confirming
a remaining blocker. Verification used simulated devices only; physical camera
throughput and image quality during laser exposure still require bench testing.

## Pre-merge review

Independent review reproduced and corrected five additional boundary issues:

- Failed serial close retains its handle and reader until cleanup succeeds.
  Output becomes unknown, and reconnect remains blocked while resources are held.
  A stopped reader cannot reuse a new session's stop event or consume a late frame.
- A protocol fault leaves Disconnect available for explicit cleanup, with Connect
  disabled until the resource is released. Neither rendering nor reconnect resets
  the human startup confirmation into an accepted state.
- The final single-line OFF verifies the expected stop generation, so an external
  OFF or ESTOP at motion completion cannot be reported as successful cutting.
- Integer pulse coordinates are projected back into the reference image before
  execution. Exposure points and complete centerlines must remain inside the ROI
  and polygon, including concave boundaries. Narrow or boundary-touching paths may
  therefore be rejected instead of silently expanding the selected region.
- Dark and live single-line paths share the same backlash direction calculation;
  calibration roundoff on a stationary axis cannot add a full preload movement.

Preview quantization also accepts the immutable calibration-value proxies used
by the UI. These checks concern commanded beam centers/centerlines, not the full
optical spot, actual mechanical positioning error or measured laser timing.

The corrective patches were reviewed with actual Claude Code, including a final
follow-up on preview compatibility; no concrete remaining blocker was confirmed.
All review reproductions and tests used simulated devices. No serial hardware was
connected and no firmware was uploaded.

Final regression after all review fixes: **1596 tests + 6 subtests, all 40 modules
passed**. Both Uno firmware configurations were also recompiled successfully with
the same flash/RAM sizes recorded above. The repository still ships both wiring
capabilities disabled; compilation and software tests do not qualify hardware.
