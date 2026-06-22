# F710 手柄控制设计

Date: 2026-06-21（已纳入多代理对抗式审查的修订）

## 1. 目标与背景

为 Automatic Transfer Station 增加 Logitech F710 无线手柄，直接驱动位移台。覆盖 7 个轴，每轴都要"连续 + 点动"：

| 轴号 | 名称 | 说明 |
| --- | --- | --- |
| 5 | X3 | 吸附台左右 |
| 6 | Y3 | 吸附台前后 |
| 7 | X2 | 载玻片左右 |
| 8 | Y2 | 载玻片前后 |
| 9 | R | 样品旋转 |
| 3 | MicroscopeZ | 显微镜上下（无绝对坐标、无硬限位）|
| 4 | Z | 样品台 / 载玻片升降（软限位 0..9.95 mm）|

复用现有运动栈：手柄运动经 `MotionCommandQueue` 下发，继承"板卡未打开不动"、硬限位、X2/X3 干涉等检查；**急停/松手停走绕过队列的直达路径**（见 §6.4/§7）。速度上限与步长复用 GUI 每轴设定。F710 **MODE 拨到 X（XInput）**。

## 2. 范围

**做：** XInput 读取、翻译成运动命令、独立 GamepadPanel 模块、GUI 启用开关 + 状态 HUD、选中轴在左侧设备图上高亮、单元/集成测试、安全加固。

**不做（本期）：** 手柄上调速度档、震动、按键重映射界面、X1/Y1 纳入手柄、跨平台。

**推迟到末期真机验证（不在本期写代码阶段做硬件测试）：** 见 §10。实现时采用不依赖这些未验证行为的保守机制。

## 3. 控制映射

统一机制：**两个摇杆 = 纯比例连续**；前面四个键、D-pad 执行 = **按下即走一步、按住转连续**（见 §4.2）。松手即停。

| 控件 | 轴 / 功能 | 连续 | 点动 |
| --- | --- | --- | --- |
| 左摇杆 X / Y | X3 / Y3（吸附台） | 比例，松手停 | — |
| 右摇杆 X / Y | X2 / Y2（载玻片） | 比例，松手停 | — |
| 下扳机 LT / RT（模拟） | MicroscopeZ(3) 下 / 上 | 按住=比例连续 | 按下=一步 |
| 上肩键 LB / RB（数字） | 样品台 Z(4) 下 / 上 | 按住=定速连续 | 按下=一步 |
| 面键 X · B | 选择器：上一个 / 下一个轴 | — | — |
| 面键 A · Y | 选择器：步长档 **小(A,下) / 大(Y,上)** | — | — |
| 十字键 ← / →（执行） | 当前选中轴 − / + | 按住=定速连续 | 按下=一步 |
| **Back** | **全停（紧急）** | — | — |
| Start | 启用 / 关闭手柄 | — | — |
| 右摇杆按下 R3 | 预留（暂不分配） | — | — |

说明（修订）：
- **急停移到 Back**：R3 与正在推动的右摇杆同指，易误触/误延迟；Back 不参与运动，随手可按。
- **步长极性按空间直觉**：A(下)=更小、Y(上)=更大。
- 选择器循环：X3→Y3→X2→Y2→R→MicroscopeZ→样品台Z（7 轴）；首次武装默认选中 **X3**；切换时**记住上次选择**，并提供"直达 R"（R 是唯一没有专用连续控件的轴）。
- **两种粒度，不冲突**：前键/摇杆 = 粗调连续；选择器 + D-pad = 对任一轴的**精细单步**。两个 Z 既有专用前键（连续 + 粗点动），也可被选择器选中用 D-pad 做精细点动——这是粒度差异，不是重复。文档与 HUD 要让当前粒度可辨。

## 4. 交互行为

### 4.1 比例连续（摇杆 / 扳机）
- 摇杆：先做**死区**（默认 12%，常量可调），再**重缩放**使输出在死区边缘恰为 0、满偏为 1（避免丢掉前 12% 行程），再过**可调指数曲线**（默认平方，常量）。`速度大小 = 曲线(rescale(|偏移|)) × 该轴 GUI 设定速度`。
- 方向：纯逻辑层只输出 `[-1,1]` 的有符号比例；**单位换算与方向取符号在翻译层用现有 `_controller_direction_for_coordinate_direction(axis, sign)` 与 `_speed(axis)` 完成**，确保 X3(pulse_sign=-1)、MicroscopeZ(轴3反向) 等方向与界面按钮一致。XInput 摇杆 +Y=上，归一化时负值除 32768、正值除 32767。
- 扳机（MicroscopeZ）：LT=下、RT=上，各 0..1，仅在判定为 hold 的**连续阶段**按 `速度 = 曲线(push) × GUI 速度`；快速点按则按 §4.2 走一步。
- 回到死区 → 该轴 `Stop`。

### 4.2 点动 / 连续机制（修订：去掉 tap 延迟）
适用于前面四个键和 D-pad 执行，统一为标准 jog 手感：
- **按下边沿立即走一步**（步长用该轴 GUI 设定）。
- 若**持续按住 ≥ hold-delay**（默认 ~300 ms）→ 进入**连续**（扳机为比例、肩键/D-pad 为定速），并抑制额外的离散步，直到释放。
- 单步零延迟（最高频的精调操作即时响应）；按住转连续时多走的那一步可忽略。

### 4.3 选择器（选轴 + 步长）
- 面键 X/B：在 7 轴环里循环选"当前选中轴"，记忆上次、默认 X3。
- 面键 A/Y：把**选中轴**的步长在该轴的**离散档**里调小/调大。
- **步长档模型**：现有 `txtStep{axis}` 是自由值 spin box（0..5000），没有"档"。为手柄定义每轴一组**规范步长吸附点**（snap 列表，常量表）；A/Y 移到上/下一个吸附点并夹紧，写回该轴 spin box（仍允许手动输入任意值）。这样"档"语义明确、首尾夹紧有定义，且不改动现有控件类型/测试。
- **手柄改步长要可见可恢复**：改动时**闪烁/高亮该轴卡片的步长框**，让操作员看得到值被改了；吸附到规范值便于预测与恢复。

### 4.4 选中反馈（左侧图高亮 + HUD）
- 复用左侧 `StageMapWidget` 既有热点按钮 `axisHotspot{X3,Y3,X2,Y2,R,MicroscopeZ,Z}` 与本仓既有高亮 idiom（设 `selectedAxis` 布尔属性 → unpolish/polish，参照 `objective_panel._mark_selected`）。新增 `MainWindow.highlight_axis(axis|None)`，复用 `AXIS_LABELS` 做轴号→名映射，加一条 `[selectedAxis="true"]` 样式。
- **高显著 HUD**：在现有 `motionStatusText/motionStatusLamp` 旁加一行大号常驻读数：`启用 · 选中:R · 步长:1.0 deg`，让眼在显微镜上也能余光看到（仅靠左侧小标签不够）。

### 4.5 启用 / 安全动作（修订）
- "启用手柄"为关：只读 Start（用于开启）；其余输入一律忽略、绝不产生运动。**开机默认 = 关（永不自动武装）**。
- **失焦自动解除武装**：窗口失焦/最小化（`changeEvent` WindowDeactivate/Minimized 或 `QApplication.focusChanged`/`applicationStateChanged`），或一段时间无越死区输入（inactivity 超时）→ 自动 `disarm + stop_all`，需重新按 Start。
- **Back = 全停**：走 §6.4 的直达停机路径。
- 松手即停（隐式 dead-man）+ 死区为主防线；并在 §7 记录"toggle 武装 + 失焦解除"相对"按住式 dead-man"的残余风险。
- **基线复位**：武装时、断连时、板卡打开时，显式清空纯逻辑层的每轴连续会话/计时器/按钮边沿；若板卡已开，武装时发一次 `stop_all` 确保起点已停。

## 5. 速度与步长来源
速度上限复用 GUI 每轴 `txtSpeed{axis}`（经 `_speed`/`speed_to_pulse_per_ms`，注意轴4/轴9 用不同速度档集，按 `speed_options_for_axis` 取）。步长见 §4.3 的吸附点模型。手柄只决定方向与比例。

## 6. 架构与模块

模块隔离、单一职责、可独立单测；**不把逻辑塞进已 ~1900 行的 main_window**。

### 6.1 `gamepad_xinput.py`（读取层，纯 ctypes）
- 仿 `gas_api`：`GamepadReader(xinput_dll)` 构造 + `load_default_reader()` 工厂（测试注入假 DLL）。
- DLL：按名加载 `xinput1_4.dll`（`ctypes.WinDLL`，**stdcall**；区别于 `gas_api` 的 CDLL+绝对路径），失败回退 `xinput9_1_0.dll`。`XInputGetState.restype=DWORD`，`argtypes=[DWORD, POINTER(XINPUT_STATE)]`。
- `read_state()` 返回**冻结 dataclass 快照**：摇杆 `-1..1`（负/正分别除 32768/32767，带死区前的原值或原始 short 由控制器层处理曲线）、扳机 `0..1`、各按钮 bool、`connected`、`packet`(dwPacketNumber)。
- **按钮位掩码**在 §6.1 列全：DPAD_LEFT 0x0004 / RIGHT 0x0008 / UP 0x0001 / DOWN 0x0002、START 0x0010、BACK 0x0020、LEFT_THUMB 0x0040 / RIGHT_THUMB 0x0080、LB 0x0100 / RB 0x0200、A 0x1000 / B 0x2000 / X 0x4000 / Y 0x8000；LT/RT 是 `bLeftTrigger`/`bRightTrigger` 字节（X 模式才独立）。
- **断连慢路径**：只轮询**一个已缓存的控制器索引**；仅在断连时以低频(~1-2s)扫描 0..3 槽做（重）发现，用返回码 `ERROR_DEVICE_NOT_CONNECTED(1167)` 跳过空槽；已连接后不再扫描。死区/曲线**不在这里**。

### 6.2 `gamepad_controller.py`（纯逻辑层，无 Qt、无硬件）
- 输入：一个归一化快照 + 注入的 `now()`。
- 状态：`enabled`、`selected_axis`、按钮边沿、tap/hold 计时、每轴连续会话（是否在连续、上次下发速度、~10Hz 限频）、`packet` 陈旧检测。
- 处理：死区+重缩放+曲线、tap/hold 判定（§4.2）、选轴/步长/启用/全停事件、packet 陈旧/断连判定。
- 输出**意图**：`StartContinuous(axis, frac)`、`UpdateContinuous(axis, frac)`（已在内部限频）、`StopContinuous(axis)`、`Jog(axis, dir)`、`SelectAxis(axis)`、`StepDelta(axis, delta)`、`ToggleEnable`、`StopAll`。
- 用合成快照序列 + 假时钟纯单测，无需 Qt/队列。

### 6.3 `gamepad_panel.py`（新 GUI 模块，仿 temperature_panel/objective_panel）
- 构造注入：`register`、`motion_queue_provider`（绑定到 `_active_motion_queue`）、`speed_provider`/`step_provider`（读 GUI 卡片）、`highlight_axis` 回调、`reader`（默认真实）、`clock`（默认 `time.monotonic`）、`stop_provider`（直达停机，见 §6.4）。
- ~30ms `QTimer` → `reader.read_state()` → `gamepad_controller` → 意图 → 翻译层下发：
  - `StartContinuous/UpdateContinuous` → 经队列下发"有界连续移动 + 该速度"；**每轴 latest-only 合并**（仿 `_position_refresh_pending`，新 tick 替换而非追加），独立于 33Hz 读 tick 的 ~10Hz/轴 限频。
  - `Jog` → `relative_move(axis, dir, GUI速度, GUI步长)`。
  - `StopContinuous`/`StopAll`/失焦/断连 → **直达停机**（§6.4），**不受限频影响、submit 返回 False 也兜底**。
  - `SelectAxis` → `highlight_axis` + HUD；`StepDelta` → 改步长 spin（吸附 + 高亮）。
- 失焦/最小化 → 自动 disarm + stop_all。
- 武装时禁用对应轴的 GUI jog 控件（见 §7 仲裁）。

### 6.4 控制器运动机制（本期committed，避开未验证硬件行为）
- **比例连续 = 重发"有界 jog"**：复用 `start_jog` 的有界目标计算，按 ~10Hz/轴重发并更新速度；每次重发都**按对向轴当前位置重新定界**（天然修复 X2/X3 陈旧边界）；latest-only 合并避免积压。MicroscopeZ(3) 走其速度模式 jog 分支（无界），松手可靠 `stop_axis(3)`。
- **直达停机路径**：`StopAll/StopContinuous/急停/断连` 在 **GUI 线程直接调 `controller.stop_all()/stop_axis()`**（`GA_Stop` 单次快调用；经 `_api_lock` 串行化，最坏只等当前一条命令的几次 DLL 调用，而非整条队列积压）。这是对"运动走队列"的**有意安全例外**，与现有 `_reset_motion_control` 直接调用同理，需 `is_open` 守护。
- 队列优先级：连续更新用较低优先级，stop 严格更高；stop 时清掉该轴 pending 连续命令。
- **`set_jog_velocity`（GA_SetVel-only 平滑路径）= 推迟优化**：仅在末期真机验证"`GA_SetVel+GA_Update` 能平滑改 Trap 巡航速度"后才采用；若采用，**加入 `MotionController` Protocol + `RecordingController`/任何桩**。本期不依赖它。

### 6.5 与现有代码复用
`_active_motion_queue`、`_speed`/`_step`、`_controller_direction_for_coordinate_direction`、热点高亮 idiom、`_position_refresh_pending` 合并模式、`speed_options_for_axis`、`AXIS_LABELS`。

## 7. 安全（重写）
- **失焦自动解除武装**（§4.5）——最高优先级安全项：XInput 不管焦点都读得到，手柄被碰会动。
- **急停/松手停绕过队列直达**（§6.4），把停机延迟限到一条命令内；连续合并防积压。
- **X2/X3 相向碰撞窗口**：`X2_X3_INTERFERENCE_MARGIN_PULSE` 设为覆盖"一个更新周期最大行程"的非零余量；定界时把对向轴**前推一个周期**；干涉对(5/7)每次更新**都重新定界**（不走纯改速）；安全停后加**锁存**——须回死区才再响应（防握杆"棘轮"顶进危险区）。
- **断连检测**：`connected=False` **或** `packet` 多帧不变且输入非中立（RF 抖动/缓存旧值）→ 直达 `stop_all` + disarm；不只依赖驱动超时。
- **双控制器仲裁**：手柄武装时禁用对应轴的 GUI jog 控件（或加锁），避免 GUI 按住 + 摇杆同轴并发的优先级/last-writer 混乱；定义手柄死区 Stop 与 GUI 已发移动的相互作用。
- **运行时标定竞态**：`set_axis_calibrations`/`_refresh_axis_calibration_mapping` 在 GUI 线程改 `axis_calibrations` 时，停掉受影响的连续会话并使其缓存失效，避免与 worker 里进行中的定界计算交错。
- **dead-man**：toggle 武装 + 失焦解除 + 松手停为主；记录其相对"按住式 dead-man"的残余风险（武装态持续期间，越死区即动）。
- **基线复位**（§4.5）。`enabled=false` 开机默认。

## 8. 边界情况
- 板卡关闭操作手柄：`_active_motion_queue` 返回 None → 忽略 + 提示，不建队列；Start 仍可切换 enabled，但运动被队列门控挡住。
- 标定在手柄握持中变更 → 停受影响轴（§7）。
- GUI jog 与手柄同轴并发 → 按 §7 仲裁。
- MODE 误拨到 D：LT/RT 独立性失效（合到共享轴），MicroscopeZ 控制退化 → 启动检查 + GUI 提示"需切到 X 模式"。
- 队列正在关闭：`submit` 返回 False 时，stop 必须**兜底直达**，不得静默丢弃。
- `_stop_unsafe_axes_if_needed` 停了握持中的轴后，安全锁存阻止握杆立即重发（§7）。

## 9. 测试策略
- `gamepad_xinput`：假 DLL/假 `XINPUT_STATE` 验证结构解析、归一化（含 ±32768/32767 与 Y 轴符号）、按钮位解码（逐位）、断连返回码、单索引轮询。
- `gamepad_controller`（纯逻辑、重点）：**注入假时钟 + 逐帧脚本快照**，测死区/重缩放/曲线、tap/hold（按下即步、按住转连续）、~10Hz 限频、选轴循环+记忆、步长吸附+首尾夹紧、启用门控、全停、packet 陈旧/断连、基线复位。
- `gamepad_panel` 集成：假 reader + 假时钟 + `RecordingController` + inline 队列，**直接调 panel 的 tick**（不靠真 QTimer），断言下发了正确的连续/jog/stop/直达停机；选中高亮切换；步长改动可见；失焦 → 自动停 + disarm；标定变更停轴；GUI 仲裁禁用。
- 安全用例：未打开不动、松手停、Back 全停走直达、断连停、急停在队列积压下仍及时（直达路径）。

## 10. 推迟到末期真机验证（写完程序后做硬件测试）
1. 比例连续平滑性：`GA_SetVel+GA_Update` 能否平滑改进行中的 **Trap 巡航速度**；若可，再启用 `set_jog_velocity` 平滑路径（否则维持 §6.4 的有界重发）。
2. 备选：四个有界轴是否改用 `GA_PrfJog` 速度模式 + 软件软限位看门狗会更平滑（需评估软限位仅靠软件的安全取舍）。
3. F710 在 X 模式下 LT/RT 独立模拟、各按钮位映射实测确认。
4. 断连/失焦 → 直达 `stop_all` 的端到端延迟实测。
5. 死区/曲线指数/限频/hold-delay 等常量按手感最终调参。

## 11. 不在本期范围
手柄上调速度/步长档 UI、震动、按键重映射界面、X1/Y1 纳入手柄、跨平台、`set_jog_velocity` 平滑路径（待 §10 验证后再定）。
