# Automatic Transfer Station 接手文档

更新日期：2026-06-22

本文档面向后续接手 Automatic Transfer Station Python 程序的人，说明如何运行、如何理解项目结构、哪些硬件安全边界必须保留，以及继续开发时应先检查什么。

## 1. 项目位置

当前交付目录：

```text
E:\Automatic transfer station
```

主程序包目录：

```text
E:\Automatic transfer station\src\automatic_transfer_station
```

厂家运动卡 SDK 目录：

```text
E:\Automatic transfer station\vendor\motion_card_sdk
```

原始开发目录仍在：

```text
D:\EthBoardTest_0127
```

原始 C# WinForms 程序保留在原开发目录根目录，包括 `Form1.cs`、`Form1.Designer.cs`、`Program.cs` 和项目文件。不要把它们当作 Python 程序运行入口，但可以作为历史逻辑参考。

## 2. 运行方式

在 PowerShell 中进入项目根目录：

```powershell
cd "E:\Automatic transfer station"
```

真实硬件模式（程序只有真实硬件模式，已移除模拟模式）：

```powershell
python .\run_transfer_station.py
```

指定物镜转盘串口：

```powershell
python .\run_transfer_station.py --nosepiece-port COM7
```

指定温控 RD105 串口：

```powershell
python .\run_transfer_station.py --temperature-port COM5
```

启用手柄控制（默认已启用，F710 背面 MODE 拨到 X）：

```powershell
python .\run_transfer_station.py
```

禁用手柄控制：

```powershell
python .\run_transfer_station.py --no-gamepad
```

XInput DLL 加载失败时不影响程序启动（向 stderr 打印中文提示并自动停用手柄）。

只做 GUI 构造 smoke，不进入事件循环：

```powershell
python .\run_transfer_station.py --smoke-exit
```

程序只有真实硬件模式，启动后不会自动打开运动卡。必须在 GUI 中点击 `打开运动控制` 才会连接并操作运动卡。运动队列只在运动卡打开期间存在；打开前或关闭后下发的运动命令会被忽略，不会重建队列或复活旧队列。

## 3. 常用验证命令

从项目根目录运行：

```powershell
python -m pytest -q
python .\run_transfer_station.py --smoke-exit
```

当前验证基线：

```text
505 passed
```

如果只是验证运动控制核心：

```powershell
python -m pytest tests\test_gas_api.py tests\test_gas_controller.py tests\test_axis_calibration.py tests\test_axis_position_store.py -q
```

## 4. 当前程序结构

| 路径 | 作用 |
| --- | --- |
| `run_transfer_station.py` | 本地启动脚本，自动把 `src` 加入导入路径 |
| `src\automatic_transfer_station` | 主程序包 |
| `src\automatic_transfer_station\run_eth_board_test.py` | 命令行参数和应用入口 |
| `src\automatic_transfer_station\main_window.py` | PySide6 主窗口 |
| `src\automatic_transfer_station\gas_api.py` | `GAS.dll` ctypes 封装 |
| `src\automatic_transfer_station\gas_controller.py` | 真实运动控制卡控制逻辑 |
| `src\automatic_transfer_station\axis_calibration.py` | 轴标定常量、坐标换算、安全边界 |
| `src\automatic_transfer_station\axis_position_store.py` | 运行时轴位置显示记录 |
| `src\automatic_transfer_station\axis_runtime_calibration_store.py` | 用户运行时标定存储 |
| `src\automatic_transfer_station\microscope_position_store.py` | MicroscopeZ 多点位存储 |
| `src\automatic_transfer_station\nosepiece_controller.py` | LV-NCNT2 电动物镜转盘串口控制 |
| `src\automatic_transfer_station\objective_panel.py` | 物镜选择 GUI |
| `src\automatic_transfer_station\rd105_controller.py` | RD105 温控串口控制 |
| `src\automatic_transfer_station\temperature_panel.py` | 温控 GUI |
| `src\automatic_transfer_station\paths.py` | 项目根目录、运行时目录、配置目录和 SDK 目录定义 |
| `src\automatic_transfer_station\gamepad_xinput.py` | XInput（`xinput1_4.dll`，备选 `xinput9_1_0.dll`）ctypes 读取层；`GamepadReader`/`GamepadSnapshot`/`load_default_reader`；连接后单索引缓存轮询，断连时才慢扫 4 个槽位；不含死区/曲线逻辑 |
| `src\automatic_transfer_station\gamepad_controller.py` | 纯决策逻辑（无 Qt / 无硬件 / 无线程，时钟可注入）；把 `GamepadSnapshot` 转成运动意图列表；圆形（径向）死区+重缩放+曲线、tap/hold、轴选择器、断连与长时间失活（中位）自动解除 |
| `src\automatic_transfer_station\gamepad_panel.py` | Qt 面板 `GamepadPanel(QGroupBox)`；用 `QTimer` 轮询 reader，把意图翻译成运动命令（连续/点动经运动队列，急停走直达路径）；启用开关 + 状态 HUD |
| `vendor\motion_card_sdk` | 厂家运动卡 SDK/示例文件 |
| `docs` | 标定记录、开发进程、接手文档和 UI 预览 |
| `runtime` | 程序运行时状态文件 |
| `config` | 本地控制器配置 |
| `tests` | 自动化测试 |

## 5. 运动卡 SDK

`vendor\motion_card_sdk` 是原来名为 `python` 的文件夹，里面是运动卡厂家支持文件，不是我们的主程序。

主要内容：

- `GAS.dll`
- `GAS.lib`
- `bopaipython\GAS_N.h`
- `bopaipython\test*.py`

程序默认从这里加载运动卡 DLL：

```text
vendor\motion_card_sdk\GAS.dll
```

## 6. 硬件和安全边界

### 运动卡

默认运动卡 IP 来自 `controller_base.py`，当前为网络通讯。

只读网络探测：

```powershell
$env:PYTHONPATH = "E:\Automatic transfer station\src"
python -m automatic_transfer_station.gas_network_probe --ip 192.168.0.200 --mode 0
```

该探测不 reset、不 enable、不移动轴。

### 硬限位

`GA_GetSts` 轴状态位中：

```text
POS_HARD_LIMIT = 0x20
NEG_HARD_LIMIT = 0x40
```

不要把 `MC_LIMIT_POSITIVE = 0` 和 `MC_LIMIT_NEGATIVE = 1` 当成 `GA_GetSts` 状态位。它们是输入 IO 类型编号。

### 当前轴参数来源

人可读源记录：

```text
docs\axis_calibration_records.md
```

程序执行常量：

```text
src\automatic_transfer_station\axis_calibration.py
```

后续如果重新标定某个轴，必须同时更新这两个位置。

### 已知硬件注意事项

- **轴3（MicroscopeZ）无任何软件/硬件行程边界**：无标定量程、空 `HardLimitConfig`、开机 `limits_off`；绝对/相对/连续 jog 全路径无真实行程保护，是光学附近最大的无保护运动路径。被污染的显微镜点位文件（`microscope_z_positions.json`）会直接驱动无边界 Z 轴。**当前决定：暂不加真实软件钳位**，待硬件确认是否有行程开关后再接入 `AXIS_HARD_LIMITS[3]` 或保守软钳位。（注：连续 jog 自 2026-06-22 改为梯形行进至 `±MICROSCOPE_Z_MAX_ABS_PULSE` 的 sanity cap——这是"卡按钮"后备封顶，**不是**真实行程限位，操作仍需人工监护。）
- **Y1（轴2）正向超程仅靠软件钳位**：正向硬限位开关已知损坏，正向行程仅由软件 `max_coordinate` 限制，构成单点失效；2026-06-21 修复已收紧连续 jog 降级逻辑，Y1+ 方向不会再降级为无界速度 jog，但硬件单点失效风险依然存在。
- R 轴使用 HOME9 零位，不是普通 IO 限位。
- X2/X3 有机械干涉边界，禁止单独绕过 `axis_calibration.py` 中的安全检查直接下发运动。
- 未确认限位映射时，测试只允许超过标称行程 `2 mm`，然后停止。

### 运动队列

来自 GUI 的连续移动、短步移动、目标位置移动、单轴停止、全轴停止、R 回零、R twist、MicroscopeZ 点位移动和 Z 弹窗运动都通过 `MotionCommandQueue` 下发，避免在 GUI 线程里直接等待控制卡调用。

队列由 `_active_motion_queue()` 统一管理，并以 `controller.is_open` 为门控：运动卡未打开时返回 `None`，运动处理函数直接忽略该命令，**不会**惰性新建队列。因此打开前或关闭后误触运动按钮都不会复活队列或泄漏 worker 线程。队列只在运动卡打开后按需创建，关闭运动控制时关闭并置空。

worker 线程对每个动作捕获所有异常（包括 DLL 层抛出的裸 `RuntimeError`、`ctypes.ArgumentError` 等），通过失败信号上报后继续存活，绝不会静默死亡导致命令堆积无人执行。

关闭运动控制时会递增位置刷新 generation 并关闭队列；如果旧的异步位置刷新在关闭或重开后才返回，主窗口必须丢弃该结果，不允许它重新创建运动队列或覆盖新状态。旧的位置刷新失败信号也必须按 generation 丢弃，不能清掉新一轮 pending 状态或覆盖新状态栏消息。

`MotionCommandQueue` 支持 `inline=True` 同步模式，仅供测试用（同步执行、同步发信号）；生产默认使用线程化 worker。`MainWindow` 通过 `motion_command_queue_factory` 参数注入队列工厂，生产默认线程化。

轴控制卡片继续保持五键左右箭头布局。前后轴和左右轴都使用同一套左右箭头图标，这是当前 GUI 约定。

### 手柄控制 (Logitech F710)

手柄控制功能通过 `gamepad_xinput.py`、`gamepad_controller.py`、`gamepad_panel.py` 三个模块实现，详见规格文档 `docs/superpowers/specs/2026-06-21-f710-gamepad-control-design.md`。

**F710 按键映射（F710 背面 MODE 拨到 X）：**

| 输入 | 功能 |
| --- | --- |
| 左摇杆（轴5/6）| X3/Y3 连续，比例速度 |
| 右摇杆（轴7/8）| X2/Y2 连续，比例速度 |
| LT/RT（轴3）| MicroscopeZ 下/上；轻按单步，长按转连续（比例）。**速度固定 4 mm/s 上限**（`GAMEPAD_MICROSCOPE_Z_SPEED_UM_S`），**与 GUI 显微镜Z 速度档无关**，满扳机=4 mm/s |
| LB/RB（轴4）| 样品台 Z 下/上；轻按单步，长按转连续（定速） |
| 十字键 上/下 | 当前"选中轴"按步长**单步** + / −（**仅单步，不连续、不自动重复**；若该轴正被摇杆连续驱动则抑制这次单步，避免冲突） |
| 十字键 左/右 | 步长档 减小 / 增大（按该轴的 `STEP_SNAP` 吸附档位；与运动无关，连续移动中也可调） |
| 面键 Y/A/X/B | **2D 方向选轴**：Y=上 / A=下 / X=左 / B=右，按 **GUI 卡片网格**（`SELECTOR_GRID_POSITIONS`）选该方向最近的可选轴（`directional_neighbor`，无候选则不动；不环绕）。上/下在同一列内、左/右在两列间，跟界面卡片列表习惯一致 |
| START | 启用 / 停用手柄 |
| BACK | 全部停止（直达，绕过运动队列） |

> **2026-06-22 手柄重映射**：方向键选轴的 1-D 循环（旧 B/X）已改为面键 2-D 方向选择；十字键由"选轴轻按/长按连续"改为"上下单步点动 + 左右改步长"。可选轴 `SELECTOR_AXES` 由 6 个扩为 **8 个**：`(5,6,7,8,9,4,1,2)` = X3/Y3/X2/Y2/R/载玻片Z/**X1/Y1**（新增 X1/Y1）；**显微镜Z(3) 仍不可选**，由扳机 LT/RT 控制。`GAMEPAD_AXES=(5,6,7,8,9,3,4,1,2)`。**选轴几何**用 `SELECTOR_GRID_POSITIONS`（GUI 卡片网格：吸附台列 X3/Y3/R/X1、载玻片列 X2/Y2/Z/Y1），**与设备图热点的照片定位 `STAGE_MAP_AXIS_POSITIONS` 解耦**（后者只管照片上画热点；选轴跟界面列表走）。

当前选中轴在左侧设备图**顶部叠加横幅**显示，格式"手柄选中: X3 · 吸附台左右 · 步长 100 µm · 坐标 1.250 mm"（**含该轴实时步长与当前坐标，随 100ms 位置刷新实时更新**；R 轴用 °，启用手柄即显示、切轴更新、停用隐藏），同时设备图热点描边高亮（琥珀色 + ▶ 前缀）、对应轴卡高亮。横幅 `setWordWrap(True)` 且宽度钳到图像宽-8，长横幅**自动换行不溢出边框**。

> **2026-06-22 Y3/Y2 方向反转（真机方向纠正）**：轴6(Y3)、轴8(Y2) 电机实际方向与软件 +pulse 假设相反，已把 `AXIS_CALIBRATIONS` 里它们的 `pulse_sign` 翻为 **-1**。`_controller_direction_for_coordinate_direction` 对 `pulse_sign<0` 返回 `-dir`，**GUI 按钮与手柄（摇杆+方向键）方向一起纠正**、坐标读数随之翻号一致。Y2 非对称软限位做了 swap+negate（min/max −25.099/25.280 → **−25.280/25.099**），**物理脉冲行程包络 `[-1339950, 1179000]` 完全不变**（往返断言已钉死，见测试）。⚠️ **物理方向必须真机小步长复验**（尤其 Y2 靠近载玻片）——见 §11 D12。

**安全语义（必须保留）：**

- 手柄启用是显式开关。窗口失焦或最小化时自动停用并停止（`changeEvent` → `disarm`）。
- 手柄断连、长时间无操作（**中位**输入）都会自动停用并发出全停命令。（注：原先的"packet 卡死自动停用"已于 2026-06-22 移除——真机按住摇杆/扳机时 `dwPacketNumber` 不变会被误判，详见下方真机调试小节。）
- 手柄启用时，**全部手柄可驱动轴** `GAMEPAD_AXES`（5、6、7、8、9、3、4、1、2——含显微镜Z 与新增的 X1/Y1）的 GUI 连续/点动/单轴停止按钮会被禁用，防止两路输入抢同一轴；全局"停止移动"（`btnStopAll`）始终可用。注意此处用 `GAMEPAD_AXES`（仲裁全集，9 轴全含）而非 `SELECTOR_AXES`（面键可选的 8 轴，不含显微镜Z）。
- 全停走直达 `controller.stop_all()`（绕过队列）；停止命令在队列里用最高优先级且不限频；停止会作废队列中尚未执行的该轴连续命令（epoch 机制），避免急停或松手后被排队命令重新启动。
- X2/X3 连续移动带"对向前推"（partner look-ahead）：把伙伴轴一个更新周期的位移折进干涉边界 clamp，只收紧、绝不放松最终的 `is_target_safe` 检查（用真实位置）。`X2_X3_INTERFERENCE_MARGIN_PULSE` 已从 0 提升到 10000。
- 运动仍全部经现有 `MotionCommandQueue`，受 `controller.is_open` 门控；面板 `QTimer` 在 `closeEvent` 里先停，再关队列。

### 2026-06-22 盲点审查后安全/健壮性增强（BP1–BP7）

本轮 7 维度（网络 IO、串口 IO、RD105 热安全、手柄输入源、物镜并发、可观测性、打包配置）多代理盲点审查确认 **34 条经过验证的发现**，分 BP1–BP7 修复包完成，测试基线由 301 升至 **411 passed**。以下变更涉及安全边界或可观测行为，后续维护必须保留。

**RD105 热安全**

- 畸形/截断温度帧不再绕过过温守卫：`int()` 守卫 + DATADEMAND `@` 终止符校验 + 数值合理性边界，三重防御。
- 软件斜坡按 `last_setpoint + rate*dt`（dt 上限钳制）限步，长停滞后不再单步大跳温。
- 过温跳闸阈值与最大设定点分离（`MAX_TARGET_TEMPERATURE + OVERTEMP_MARGIN`，默认 +3 °C），合法 205 °C 目标不再因正常 PID 过冲跳闸。
- 删除可绕过上限的 `write_verified_value`。

**RD105 独立过温看门狗**

- 控制器自有、独立于 GUI 轮询的后台守护线程，通信中断/轮询停止/读失败时（基于最后成功读取的陈旧度 `WATCHDOG_STALE_SECONDS` 判断）强制 `ENABLE=0` 并取消斜坡、置告警。
- 失败时持续重试直到确认关闭；面板侧检测卡死的 in-flight 轮询超时 `POLL_STUCK_TIMEOUT_SECONDS` 按通信失败处理。

**审计日志**

- 新增 `logging` 配置（`RotatingFileHandler`，`RUNTIME_DIR/transfer_station.log`，ISO 时间戳）+ `ats.audit` 等命名 logger。
- 以下事件落盘：运动自动停止/限位/急停（含失败 CRITICAL）/板开关复位/加热器使能/过温/看门狗/校准覆写。
- RD105 由 `print()` 改为结构化日志记录。

**网络/串口健壮性**

- `get_prf_pos`/`get_status` 失败在 `gas_controller` 边界统一翻译为 `MotionControllerError`。
- `GA_Open` 前校验 IP（空/NUL/非 ASCII）；probe 复用 `gas_api` 签名防漂移。（注：曾尝试 open 时显式 `GA_SetCardNo(0)`，但真机返回错误 7 导致打开失败，已移除——单板网络下 DLL 默认即 card 0。）
- 转盘 `read_until(size=64)` + 单读超时 + 整体墙钟截止；串口异常自愈清句柄。
- **板卡 open/close/reset 改为经可注入执行器在工作线程执行（生产线程化，测试内联），不可达时不再冻结 GUI ~21 s。**

**物镜并发/生命周期**

- 关闭/拆除时 `_closing` 守卫所有 worker 回调；关窗时仍在运行的 `QThread` 改为 reparent+保留引用+quit（不再 terminate 崩溃）。
- 操作中拒绝切换控制器；串口 IO 加锁串行化；`refresh_position` 增 is-open 前置检查不再静默重连。

**手柄输入源**

- 摇杆改为每对圆形（径向）死区 + 幅值钳到 1.0（消除方形死区与对角超速）。
- 新增 `trigger_deadzone` 与拇指死区解耦。

**存储**

- 位置/显微镜点位存储拒绝 NaN/inf 脉冲（写入抛错、加载跳过并告警）。

**打包/配置一致性（BP7）**

- `gas_network_probe.DEFAULT_IP_ADDRESS` 改为引用 `controller_base.BOARD_IP`（单一来源）。
- `paths.py` 新增 frozen-build 分支（`sys.frozen`）；非冻结路径字节级等价于旧版本。
- `run_eth_board_test.py` argparse 默认值已通过测试确认与 `DEFAULT_NOSEPIECE_BAUDRATE`/`RD105_BAUDRATE` 一致。

### 2026-06-22 运动安全核心审查后增强（CP1–CP5）

本轮 6 维度（运动安全算术、反应式扫描时序、gas_controller 路径、急停/解除、持久化值路径、测试覆盖）审查确认 **11 条经过验证的发现**（其中 2 条 Critical），分 CP1–CP5 修复包以 TDD 方式完成，测试基线由 411 升至 **467 passed**（含 B1/B2/B3 安全路径补测试）。

**反应式安全扫描后备（CP1）**

- 位置刷新加有界看门狗：卡死读取 → 响亮持久告警 + bump generation + 复位 pending 闩锁（扫描可恢复）+ 尽力保护性停 + 审计 CRITICAL；连续读失败达阈值 → 保护性停止 + 告警。
- **残留（已知架构限制）**：单运动队列 worker + 共享 `_api_lock`，卡死的阻塞 DLL 读取无法被软件抢占——看门狗负责检测 + 告警 + 恢复，最终后备是物理急停 / 硬限位（有限位的轴）/ jog 发起时的预防性钳位（独立于扫描）。

**软限位端点取整（CP2）**

- 端点脉冲定向取整保证落在软范围内；分数坐标重归零后不再误拒某 jog 方向；经核实手柄 partner_lookahead 量纲为脉冲、正确。

**持久化 MicroscopeZ 脉冲（CP3）**

- `move_axis3_to_position` 加 int32 范围守卫（防 `c_long` 静默回绕 → 错向/负位）+ 保守物理 sanity cap；存储 load 跳过 / save 拒绝超 cap 脉冲。

**reset_board（CP4）**

- 复位后重新应用硬限位配置（与 open 一致）。

**干涉检查伙伴缺失（CP5）**

- `is_target_safe` / `unsafe_axes_for_positions` 改为 fail-safe：缺伙伴位置判不安全（`"干涉伙伴位置不可用 (axes (5, 7))"`）而非静默跳过；`_on_motion_queue_pending_cancelled` 对称解除 position-refresh 看门狗（"pending 清除 ⇒ 看门狗解除"不变式）。

### 2026-06-22 真机调试后修正

本节记录 CP1–CP5 轮次完成后、在真实硬件上首次完整调试时发现并修复的 9 项问题。自动化测试使用假 DLL（始终返回 0），无法覆盖以下真机行为。修复后测试基线 **482 passed**。

1. **运动卡打开回归修复**：BP4 曾在 `open_board` 加显式 `GA_SetCardNo(0)`，真机返回错误 7，被 `_check` 当致命错误阻断打开（GUI 报"GA_SetCardNo(0) failed with 7"）。已移除该调用——单板网络下 DLL 默认即 card 0，无需调用。

2. **物镜默认 COM 口 = COM7**：新增 `DEFAULT_NOSEPIECE_PORT="COM7"`，COM 对话框下拉默认预选 COM7；`--nosepiece-port` 仍可覆盖。

3. **物镜 cRCW 控制超时容错**：LV-NCNT2 常在 `cRCW`（正向逐格）命令上回 Control Timeout（`'d'`），即使转盘已物理转到位。原代码把它当硬失败，出现"转到位了却报 RCW failed: Control Timeout Error"。现改为：收到 `'d'` 超时不直接失败，而是用 RAR 读实际地址确认——到目标即判成功，否则再走一格（最多 8 步），8 步仍不到才报错；move 流程中 RAR 读到 `'d'` 超时也按"暂不可识别"重试。只放过 `'d'`，数据/命令/硬件/禁止方向错误仍如实报错；显式"读取位置"按钮保持严格。

4. **物镜连接状态徽标**：常驻连接指示从灰色小字升级为带彩色圆点的胶囊徽标——绿色●=已连接 COMx，红色●=未连接。

5. **手柄：移除 packet"卡死"误判自动停用**：原 `_is_stale` 在 XInput `dwPacketNumber` 连续 4 帧不变且非中位时判定冻结并自动停用。真机上按住摇杆/扳机数值不变 → packet 不变 → 被误判，导致"一动就退出"。无法区分"按住"与"真冻结"，已移除该检测。保留的安全网：断连自停、中位长时间失活自停、连续 jog 被软限位/干涉钳住、失焦自停、BACK/Space 急停。

6. **手柄：选中轴横幅**：设备图顶部叠加醒目琥珀色横幅"手柄选中: X3 · 吸附台左右"，启用手柄即显示当前选中轴（不再只有按 B/X 才出现），切轴实时更新，停用隐藏；同时保留轴卡高亮 + 热点 ▶ 前缀。

7. **手柄：方向键选轴顺序调整**：`SELECTOR_AXES` 由 `(5,6,7,8,9,3,4)` 改为 `(5,6,7,8,9,4)` = X3 / Y3 / X2 / Y2 / R / 载玻片Z——**显微镜Z（轴3）移出方向键选轴**（它仍由扳机 LT/RT 控制）。新增 `GAMEPAD_AXES=(5,6,7,8,9,3,4)`（手柄能驱动的全部轴），用于"启用时禁用 GUI 按钮的仲裁"和"急停作废待执行连续命令"——使显微镜Z 这两项保护仍被覆盖。**（已被同日"手柄重映射"超越：选轴改为面键 2D 方向、`SELECTOR_AXES` 扩为 8 轴含 X1/Y1、十字键改为单步+步长——见 §6 顶部「2026-06-22 手柄重映射」与 §11 D7/D8/D11。）**

8. **设备图热点自动去重叠**：`StageMapWidget` 加 declutter 算法，重叠的轴标签自动沿最小穿透方向推开（保持贴近各自部件），任意窗口尺寸/缩放下不再堆叠。

9. **显微镜Z（轴3）连续移动改用梯形行进**：轴3 原是**唯一**走 `GA_PrfJog` 速度爬行模式的轴；真机上该模式按住不出力，表现为"按住不动、松手才动一下"。改为和其它正常轴一致的 `GA_PrfTrap` 梯形行进——朝 jog 方向的一个远端目标（`±MICROSCOPE_Z_MAX_ABS_PULSE`，即 ±1000mm 等效 sanity cap）行进，速度取 `abs(speed)`，按住即走、松手 `stop_axis(3)` 即停。GUI 连续按钮与手柄 LT/RT 扳机都走同一条 `start_jog(3, speed)`，一并修好。
   - **手柄速度 + 比例**：手柄速度 = `扳机平方曲线比例 × 该轴速度档`。扳机/摇杆均为**比例**控制（扳机平方曲线：轻按慢、利于对焦微调，满按到顶速）。梯形行进的 `abs(speed)` 直接承接比例值，无需单独的手柄速度上限。
   - **2026-06-22 续：显微镜Z 速度上限 = 4 mm/s（步进失步）**：真机实测 1000–4000 um/s 都平滑，**5000 um/s（= 250 脉冲/ms = 250 kHz 步率 @ 50000 脉冲/mm）步进电机失步**——电机原地丢步、不走,而界面坐标读的是**指令位置**(`GA_GetPrfPos`,非真实编码器)所以数字照常递增、看不出失步;松手时减速段经过可跟速度,才"动一下"。因此 `MICROSCOPE_Z_SPEED_OPTIONS_UM_S` 去掉 5000，封顶 4000(并补 2000/3000 中间档)。**手柄 LT/RT 后续(2026-06-22)进一步与 GUI 速度档解耦**：改用固定常量 `GAMEPAD_MICROSCOPE_Z_SPEED_UM_S=4000`(=4 mm/s),由 `MainWindow._gamepad_speed(3)` 返回,不再读 `txtSpeed3`;满扳机恒为 4 mm/s、比例向下,GUI 速度档只影响 GUI 按钮。其它 50000 脉冲/mm 的轴(4/5/6/7/8)在 5000 下同为 250 kHz,**理论上也可能失步**,但用户未报告,暂不动,待反馈。
   - sanity cap 是"卡按钮"后备，不是真实行程限位；轴3 真实量程实测后按 §11 A5 收紧 `MICROSCOPE_Z_MAX_ABS_PULSE`。
   - 测试基线 481 → **482 passed**（改写轴3 jog 两条断言 + 新增正向远端目标断言；显微镜Z 速度档断言改为封顶 4000）。

### 2026-06-21 安全与行为变更（多代理代码+GUI 审查修复包）

本轮 12 维度多代理审查确认 49 条发现，分 P1–P7 包修复，测试基线升至 **301 passed**。以下变更涉及安全边界或可观测行为，后续维护必须保留。

**持久化加固**

- 运行时标定文件加载时校验坐标范围 + `zero_pulse` 合理性带（base ± 满量程，含浮点余量），越界或损坏时回退内置标定并在状态栏告警。
- 三个存储文件（`axis_positions.json`、`axis_runtime_calibration.json`、`microscope_z_positions.json`）读取时校验 `schema_version`；缺失或版本不匹配时回退默认值并告警。
- 原子写改为 write → flush → `os.fsync` → `os.replace` 四步；加载时清理残留 `.tmp` 文件。
- 抽出共享 `json_store.py` 统一存储逻辑。

**运动队列与急停**

- 连续 jog 命令按轴合并（latest-wins），板卡卡顿恢复后不再回放陈旧连续命令。
- 急停路径（"停止移动"按钮 / 手柄 BACK / 松手）先调 `cancel_pending()` 清空队列中待执行运动命令，再下发停止指令。
- 位置刷新 `QTimer` 在关窗时先停并加关闭守卫，防止刷新回调触达已销毁对象。

**连续 jog 边界收紧**

- 有标定量程的轴在 `safe_continuous_target` 返回 `None`（无安全行程）时**拒绝移动并抛 `MotionControllerError`**，不再降级为无界速度 jog。
- ~~无界速度 jog 仅保留给无量程轴（轴3 MicroscopeZ）。~~ **已于 2026-06-22 改：** 轴3 连续移动也改用 `GA_PrfTrap` 梯形行进至 sanity cap（速度爬行模式真机不出力，详见上方真机调试小节第 9 项）；连续 jog 路径全轴不再使用 `GA_PrfJog` 速度模式（`speed==0` 的防御性 no-op 分支除外）。
- 对无绝对标定轴做坐标移动时抛 `MotionControllerError` 而非裸 `ValueError`。

**GUI 安全可视化**

- "停止移动"按钮放大并绑定全局 **Space** 键作为急停快捷键（注意：应用级快捷键，聚焦文本控件时文本控件优先消费，见 §10 待确认项）。
- 危险操作需二次确认：标定覆盖、设温 ≥ `WARN_CONFIRM_TEMPERATURE`（150 °C）、轴9（R）回零。
- 干涉检测或软限位自动停轴时，对应轴卡显示红色边框，顶部指示灯进入持久 alarm 态；轴回到安全范围后才清除。
- 禁用按钮样式改为明显置灰（`:disabled` 样式 + 运动键禁用淡化），不再与普通按钮视觉相同。
- 手柄启用/停用在状态栏提示；当前选中轴在轴卡高亮（含非颜色线索）。

**手柄逻辑**

- 摇杆与方向键在同一轴同时输入时不再产生"停-再起"抖动。
- 活动连续 jog 期间同轴点按命令被抑制，不注入冲突点动。
- 扳机阈值与死区对齐，消除近零爬行。
- 同一 tick 换轴并改步长时，步长作用到新选中轴而非旧轴。

**启动健壮性**

- GAS.dll 加载失败现在给出包含路径和位数的清晰提示（中文），而非裸 traceback 崩溃退出。

### RD105 温控安全

RD105 控温参数由 PID 和最大输出占空比共同组成，完整表保存在 `config\rd105_config.json` 的 `pid_profiles` 中。程序在设温或按速率控温前会按目标温度自动选择对应参数，并写入 `TC1:LIMITED` 最大输出占空比。

当前软件允许的最高目标温度为 `205.0 °C`。目标温度高于该值会被拒绝；实时读数高于该值或读数无效时，会取消 Python 软件斜坡并尝试关闭输出。若关闭输出失败，GUI 状态必须显示“关闭输出失败”，不能误报为已经关闭。

按速率控温由 Python 逻辑实现：先以当前温度作为起点写入 TG，然后按目标温度和速率逐步推进小目标。推进过程中如果 TG 写入失败，`rd105_controller.py` 会取消软件斜坡，并通过 snapshot 的 `control_error` 字段让 `temperature_panel.py` 显示控温错误。

### 物镜切换到位判定与串口诊断（LV-NCNT2）

LV-NCNT2 协议代码全在 `nosepiece_controller.py`（按手册新写，原 C# 没有物镜代码，仓库内也没有手册）。已知现场现象：发 `cRDC<n>` 切换命令，转盘会实际转到位，但控制器回 `nRDCd`（控制超时），随后读位置 `rRAR` 一直返回 `0`，拿不到到位确认。

**根因（手册 + 实测确认）**：直接定位命令 `cRDC<p>` 在本机驱动转盘时一律 `nRDCd`（控制超时）+ `RAR=0`（未落位），但移到当前位 `cRDC<当前>` 秒回 `oRDC`。结合手册命令表（`RCW`=正向到下一格；`RCR`/`RCC`=反向到下一格，且**反向 1→5 被固件禁止以保护样品**）和实测时序（`1→2` 0.9s、`3→5` 1.3s 即失败，未识别态 10s 超时），判断 `RDC` 是按**反向**寻址、撞上"1→5 反向禁止"而堵转超时（堵转电流疑似还把 FTDI/COM7 复位过一次）。

**当前到位判定（严格，正向逐格）**：`move_to_position` 不再用 `RDC`，改为手册推荐的 **`cRCW` 正向逐格旋转**：先 `RAR` 读当前位，已在目标则直接返回；否则每发一次 `cRCW`（正向一格，`oRCW` 同步确认落位）后用 `RAR` 严格确认，到目标即停，最多 `FORWARD_STEP_LIMIT`(=8) 步，到不了则报错。正向永不触发"1→5 反向禁止"，且 `RAR` 恒读 0 也不会被当成功。`RAR=0`（地址未识别）时首个 `cRCW` 会把转盘重新转进定位槽。任何一格 `RCW` 返回 NAK 立即报错。

⚠️ 该修复已通过单元测试（假串口验证逻辑），但**尚未在真实硬件上最终确认**——测试当晚 COM7 掉线。COM7 恢复后需实跑一次：连接物镜 → 切换几个位置，确认每次都能 `oRCW`+`RAR` 读到目标。若届时正向逐格仍落不进槽，则属机械/电源问题（参见 Troubleshooting：信号线/AC 适配器接触、手动用面板正/反转开关转到定位）。

诊断日志（纯观测）仍保留，必要时抓真实协议：

```powershell
python .\run_transfer_station.py --nosepiece-port COM7 --nosepiece-trace
```

然后在界面里做一次"切换物镜 + 读取位置"，日志写到 `runtime\nosepiece_trace.log`，每行形如：

```text
12:00:00.123 TX  'cRDC1\r' hex=[63 52 44 43 31 0d]
12:00:00.456 RX  'nRDCd\r' hex=[6e 52 44 43 64 0d]
12:00:00.500 TX  'rRAR\r' hex=[72 52 41 52 0d]
12:00:00.750 RX  'aRAR0\r' hex=[61 52 41 52 30 0d]
```

日志会捕获所有读到的帧（含被跳过的 `SAE` 状态事件和超时空读），据此即可做协议级精确修复。日志文件路径可用 `--nosepiece-trace-file` 自定义。

## 7. 运行时数据文件

运行时文件目录：

```text
runtime
```

重要文件：

| 文件 | 是否应保留 | 说明 |
| --- | --- | --- |
| `axis_positions.json` | 是 | 上次位置显示恢复，不写回运动卡 |
| `axis_runtime_calibration.json` | 是，如果存在 | 用户运行时坐标标定 |
| `microscope_z_positions.json` | 是，如果存在 | MicroscopeZ 点位管理 |
| `nosepiece_trace.log` | 否 | `--nosepiece-trace` 物镜串口诊断日志，排查完可删 |
| `pyside_*.png` | 否 | GUI 调试截图，可删 |
| `preview_*.json` | 否 | 预览用临时数据，可删 |

## 8. GUI 设计约束

当前 GUI 风格已经由 HTML 预览确认并迁移到 PySide6。后续修改应保持：

- 左侧深色设备图区域。
- 图片上使用局部轴标签。
- 中间紧凑轴控制卡片。
- 右侧温控面板。
- 顶部状态灯和运动控制按钮。
- 五键运动布局：连续负向、点动负向、停止、点动正向、连续正向。

不要把界面改成新的通用 dashboard 风格；如果要重排，先在现有风格里局部调整。

## 9. 接手后的推荐检查顺序

1. 先做 GUI 构造 smoke（不进入事件循环，也不打开运动卡）：

```powershell
python .\run_transfer_station.py --smoke-exit
```

2. 运行全量测试：

```powershell
python -m pytest -q
```

3. 做真实运动卡只读探测：

```powershell
$env:PYTHONPATH = "E:\Automatic transfer station\src"
python -m automatic_transfer_station.gas_network_probe --ip 192.168.0.200 --mode 0
```

4. 打开真实 GUI 后，先只点击 `打开运动控制`，观察状态，不立即移动。

5. 若要移动，先用小步长，并观察 GUI 坐标、实际显微镜视野和限位状态是否一致。

6. 涉及限位或干涉边界时，不要跳过软件安全检查直接调用 DLL。

## 10. 当前未完全完成或需后续实测的事项

> 本节按主题罗列遗留项的背景与详情；**硬件就绪后请直接按 §12「真机验收清单」逐条勾选执行**——那是把本节所有真机/SDK 待确认项汇总后的可操作单。

- RD105 温控 GUI 已集成，但实时曲线和 PID 自动档位仍可继续完善。
- LV-NCNT2 物镜切换协议已实现，需要在真实串口上最终确认端口、波特率和物镜孔位。
- 真实硬件上，安全修复后的 `GA_GetSts` 硬限位拦截建议再做一次小步长验证。

### F710 手柄控制——真实硬件待办（按用户要求，所有手柄硬件实测均推迟到程序完全写完后再做）

1. 验证 `GA_SetVel`+`GA_Update` 能否平滑重定向 TRAP 巡航速度；若可行再加 `set_jog_velocity` 快路径。
2. 评估 `GA_PrfJog` 速度模式作为摇杆轴的替代方案。
3. 台架确认 F710 在 X 模式下两个扳机（LT/RT）独立读取 + 全部按钮位映射。
4. 端到端测量断连 / 失焦 → 停止的实际延迟。
5. 调参：死区阈值 / 曲线指数 / 长按延迟 / 更新率 / X2-X3 干涉余量手感。

### 2026-06-21 多代理审查后新增待办

- **轴3 软件钳位**：待硬件确认行程开关后接入 `AXIS_HARD_LIMITS[3]` 或设置保守软钳位；未完成前 MicroscopeZ 方向操作保持人工监护。
- **Space 急停快捷键现场确认**：Space 为应用级全局快捷键，聚焦的文本控件（如坐标输入框）会优先消费按键；需在真实操作场景中确认急停不被输入控件或对话框拦截。

### 2026-06-22 BP1–BP7 盲点审查后的完整性批评遗留缺口（供下一轮修复）

以下缺口由完整性批评师在 BP1–BP7 之后确认仍存在，优先级从高到低：

- **运动安全几何/算术核心**（B1/B2/B3 已补测试，CP5 已修复干涉缺轴问题）：`axis_calibration.safe_continuous_target` 钳制反解的 `_round_half_up` 极端边界情形已有单元测试（B2）。
- **反应式安全扫描单点失效/时序**（CP1 已加有界看门狗）：看门狗检测 + 告警 + 恢复已实现；更深层的可抢占 wedged DLL 调用架构课题待未来 DLL 级超时支持时再推进。
- **干涉判定缺轴位置静默跳过**（CP5 已修复为 fail-safe）：已关闭。
- **`reset_board` 在报警态下清状态语义**（CP4 已幂等重应用限位配置）：`GA_Reset` 是否真正清除轴报警并使 `GA_GetSts` 重置仍需 SDK/真机确认——代码侧已幂等重应用，真机验证待办。
- **失焦解除时已发出连续 jog 是否停止**：手柄失焦 disarm 调用 `stop_all()`，但队列中已排队的连续 jog 命令是否都被 `cancel_pending()` 清除？需确认 epoch 机制覆盖了手柄 disarm 路径，不仅覆盖急停按钮路径。

### 需真实硬件/SDK 确认的遗留项（无法仅凭代码审查关闭）

- **`GA_Reset` 是否真清限位使能**：CP4 已幂等重应用硬限位配置，但 `GA_Reset` 是否在 SDK 内部清除 `GA_LmtsOn` 状态尚未实测确认——真机 reset 后用 `GA_GetSts` 验证限位仍激活。
- **Space 全局急停是否被聚焦输入控件/对话框吞键**：Space 为应用级 `Qt::ApplicationShortcut`，聚焦文本输入框或对话框时 Qt 事件路由可能优先分发给控件——需在真实操作场景中点击坐标输入框后按 Space 确认急停生效。
- **轴3（MicroscopeZ）物理行程实测后细化 sanity cap**：CP3 的保守 sanity cap 基于估算；待真实硬件确认行程极限后按实测值收紧，并更新 `axis_calibration.py` 中的 `MICROSCOPE_Z_MAX_ABS_PULSE`。
- **运动侧可抢占 wedged DLL 调用的停止机制**：单 worker + `_api_lock` 架构下，卡死的阻塞 DLL 读取无法被软件抢占——CP1 看门狗已提供检测/告警/恢复；进一步的可抢占支持是更深层架构课题，需 SDK 提供真正的非阻塞或可取消 API。

## 11. 不要随意删除的内容

- `docs\axis_calibration_records.md`
- `src\automatic_transfer_station\axis_calibration.py`
- `runtime\axis_positions.json`
- `vendor\motion_card_sdk\GAS.dll`
- `vendor\motion_card_sdk\bopaipython\GAS_N.h`
- 原始 C# 源文件所在的 `D:\EthBoardTest_0127`

这些文件分别对应标定事实、程序安全边界、运行时位置恢复、真实硬件 DLL、SDK 头文件和原程序参考。

## 12. 真机验收清单（硬件就绪后逐条执行）

本清单把三轮代码+GUI 审查（主审 / 盲点 / 运动核心）后**所有只能在真实硬件/真实操作场景中确认**的遗留项，汇总成一份可逐条勾选的验收单。代码侧能修的都已修复并通过 505 项自动化测试；下面这些**必须上真机验证**，因为它们依赖 GAS.dll/串口/手柄的真实行为或 SDK 未文档化的语义。

执行原则：先空载/小步长，再正常使用；每条记录"实测结果"与"是否需改代码"。涉及运动时身边常备物理急停。`§` 指向本文档相关详述章节。

### A. 运动卡与硬限位
- [ ] **A1 `GA_Reset` 限位语义**（CP4，§10）：点"复位"后用 `GA_GetSts` 读各轴状态位，确认硬限位仍激活、报警位已清。代码已在 reset 后**幂等重应用**限位配置；若实测 `GA_Reset` 本就不清限位使能，则该重应用是冗余但无害（在 `gas_controller._configure_axes_and_limits` 注释标注即可）。
- [ ] **A2 硬限位拦截小步长验证**（§6 安全修复后）：对每个有限位的轴（1,2,4,5,6,7,8）朝限位方向小步长点动，确认在限位处**自动停**，且越限方向被 `_assert_hard_limit_direction_allowed` 提前拒绝。
- [ ] **A3 Y1（轴2）正向硬限位开关**：现状为损坏、正向行程仅靠软件 `max_coordinate` 钳位（单点失效，§6）。确认/维修该开关；未修复前正向操作人工监护。
- [ ] **A4 轴3（MicroscopeZ）是否有行程开关**：确认。若有 → 接入 `AXIS_HARD_LIMITS[3]` 或保守软钳位；若确无 → 保持现状（无真实软钳位，连续 jog 用梯形行进至 sanity cap 作"卡按钮"后备，人工监护，按你的决定）。
- [ ] **A5 轴3 物理行程实测 → 细化 sanity cap**（CP3）：测出真实 Z 行程后，按实测值收紧 `axis_calibration.MICROSCOPE_Z_MAX_ABS_PULSE`（当前为保守的 1000mm 等效上限）。
- [ ] **A6 板卡不可达不冻结 GUI**（BP4b）：拔网线 / 用错 IP 启动并点"打开运动控制"，确认窗口**不冻结 ~21s**、显示"连接中…"且超时后弹清晰告警（而非裸 traceback）。

### B. 运动安全扫描（反应式）
- [ ] **B1 X2/X3 双轴同时接近干涉边界**：两轴同时朝彼此移动，确认反应式扫描在越界前**同时停轴5和7**；记录从越界到停止的延迟。
- [ ] **B2 卡死读取 → 看门狗**（CP1）：若能模拟一次位置读取挂起（不返回不抛错），确认 ~2s 后看门狗弹**持久告警**+保护性停+扫描随后**恢复**（审计日志出现 CRITICAL）。
- [ ] **B3 位置读失败 fail-safe**（CP1）：制造连续读取失败，确认达阈值后**保护性停止**+告警。

### C. 急停与延迟
- [ ] **C1 Space 全局急停吞键**（CP5/§10）：先点击坐标输入框使其聚焦，再按 Space，确认急停**仍生效**。若被输入控件/对话框吞键 → 改用专用功能键或加 `eventFilter`。
- [ ] **C2 各路急停延迟 + 不回放**（P4）：运动中分别触发"停止移动"按钮 / 手柄 BACK / 松开摇杆，测端到端停止延迟；确认队列中**待执行运动命令被清空、恢复后不回放**陈旧移动。
- [ ] **C3 失焦解除**（BP5/§10）：运动中 alt-tab 切走窗口，确认手柄自动停用且**在飞的连续 jog 被停止**（cancel_pending + stop_all 覆盖 disarm 路径）。

### D. 手柄 F710（手柄规格 §10）
- [ ] **D1 MODE=X 按键映射**：台架确认 X 模式下 LT/RT 两扳机**独立读取** + 全部按钮位映射与 `gamepad_xinput.BUTTON` 一致。
- [ ] **D2 `GA_SetVel`+`GA_Update` 平滑重定向 TRAP 巡航速度**：若可行 → 增加 `set_jog_velocity` 快路径（并加入 `MotionController` Protocol + `RecordingController`）。
- [ ] **D3 评估 `GA_PrfJog` 速度模式**作为摇杆轴的替代方案。
- [ ] **D4 手感调参**：死区阈值 / 曲线指数 / 长按延迟 / 更新率 / X2-X3 干涉余量。
- [ ] **D5 圆形（径向）死区手感**（BP6）：确认对角推杆死区一致、无意外轴串扰。
- [ ] **D6 按住摇杆/扳机不应被误停用**（真机调试修正）：按住摇杆或扳机保持输入不变时，手柄**不得**自动停用；确认已移除 `_is_stale` packet 卡死误判检测，保留断连和中位失活自停即可。
- [ ] **D7 面键 2D 方向选轴（跟 GUI 卡片网格）**（2026-06-22 重映射）：用 Y/A/X/B 上/下/左/右选轴，确认可达全部 8 个可选轴 **X3/Y3/X2/Y2/R/载玻片Z/X1/Y1**、显微镜Z(3) 选不到；方向跟界面卡片列表一致——上/下在同一列（吸附台列 X3→Y3→R→X1；载玻片列 X2→Y2→Z→Y1）、左/右在两列间切换。
- [ ] **D8 设备图顶部横幅含步长+坐标、不溢出**（重映射）：手柄启用时设备图顶部琥珀色横幅显示如"手柄选中: X3 · 吸附台左右 · 步长 100 µm · 坐标 1.250 mm"，切轴更新、**坐标随移动实时刷新**、停用隐藏；R 轴单位为 °；**长横幅自动换行、不超出图片边框**。
- [ ] **D9 显微镜Z 连续移动按住即走、松手即停**（真机调试修正，关键）：GUI 显微镜Z 连续按钮 + 手柄 LT/RT 扳机，**按住时立即连续移动**（不再"只在松手时动一下"）；松手立即停。速度档 **1000–4000 已实测平滑；5000 已移除**（步进失步——见 §6 真机调试第 9 项）。**手柄 LT/RT 现固定 4 mm/s 上限（与 GUI 速度档无关）**：直接满扳机确认顶速 4 mm/s、半按比例变慢，无需先设 GUI 速度档；GUI 显微镜Z 按钮仍按速度档（1000–4000）走。注意：sanity cap 是卡按钮后备，**轴3 仍无真实行程限位**，靠近光学/样品时务必人工监护。
- [ ] **D10 其它细分轴在 5000 是否也失步**（待反馈）：轴 4/5/6/7/8 同为 50000 脉冲/mm，5000 um/s = 250 kHz 步率与显微镜Z 相同，理论上也可能失步。若现场发现 X2/X3/Y2/Y3/载玻片Z 在 5000 档"按住不走/失步"，按显微镜Z 同法封顶（去掉对应速度档的 5000）。
- [ ] **D11 十字键单步点动 + 左右改步长**（2026-06-22 重映射）：选中任一轴后，**十字键 上/下**确认按当前步长单步移动（+/−），**按住不连续、不自动重复**；**十字键 左/右**确认只改步长档（连续移动中也能改）。重点验证 **X1/Y1 现在可被手柄选中并单步点动**（它们的 GUI 点动按钮在手柄启用时应同时被禁用）。注意 Y1(轴2) 正向硬限位开关已知损坏，正向单步只靠软件软限位钳位——靠近正向极限时人工监护。
- [x] **D12 Y3/Y2 方向反转复验**（2026-06-22 方向纠正，**关键·安全**）：`pulse_sign` 已翻 -1。**✅ 真机已确认（2026-06-22）：Y3、Y2 方向正确、坐标读数一致，GUI 与手柄均无误。**

### E. RD105 温控
- [ ] **E1 独立过温看门狗**（BP2）：加热中**拔掉串口**（制造通信中断），确认看门狗**强制 `ENABLE=0`** + 告警 + 失败时持续重试直到确认关闭。
- [ ] **E2 过温跳闸阈值 208°C**（BP1，= MAX 205 + 余量 3°C）：确认设到 205°C 目标时正常 PID 过冲**不误跳**、真实过温**及时跳**；确认 3°C 过冲余量对脆弱样品可接受（否则调 `OVERTEMP_MARGIN`）。
- [ ] **E3 畸形/截断温度帧**（BP1）：真机确认坏帧不会被解析成偏低温度而清除真实过温告警。
- [ ] **E4 卡死轮询检测**（BP2）：确认面板 in-flight 轮询超时按通信失败处理（不死锁）。

### F. 物镜 LV-NCNT2
- [ ] **F1 光电隔离 RS232 适配器**：装上后确认拔插 / 手动控制不再因地环路复位 COM 口（此前诊断的故障，§6）。
- [ ] **F2 cRCW 控制超时容错 + 默认 COM7**（真机调试修正）：连接物镜（默认 COM7，可 `--nosepiece-port` 覆盖）→ 切换几个孔位，确认收到 Control Timeout（`'d'`）后代码自动用 RAR 读实际地址确认到位，而非直接报错；若 RAR 确认到目标则视为成功；若落不进定位槽 → 机械/电源排查。
- [ ] **F3 端口/波特率/孔位最终确认**（§6）。
- [ ] **F4 关窗/切端口线程安全**（BP5）：切换中关窗或切端口，确认在飞的 move/read 线程不崩（reparent+引用+quit，不 terminate）。
- [ ] **F5 确认 GA_SetCardNo 移除后开板正常**（真机调试修正）：点"打开运动控制"，确认 GUI 不再报"GA_SetCardNo(0) failed with 7"，运动卡正常打开；单板网络下 DLL 默认即 card 0，无需该调用。

### G. 可观测性
- [ ] **G1 审计日志落盘**（BP3）：真机运行确认 `runtime\transfer_station.log` 生成，且安全事件（自动停止/限位/急停含失败/板开关复位/加热器使能/过温/看门狗/校准覆写）都有时间戳记录。
- [ ] **G2 独立审计文件**（v0.0.1 审查）：确认 `runtime\transfer_station.audit.log` 也生成，且只含安全事件（INFO+），与主日志独立轮转，不会被 DEBUG 刷掉。

### H. v0.0.1 发布前审查的暂缓项（已记录，非 live bug，择机处理）
- [x] **H1 Space 急停（聚焦输入框时是否被吞键）**（重要·安全相关）：Space 全局急停用 `Qt.ApplicationShortcut`。**✅ 真机已确认（2026-06-22）：焦点在输入框/spinbox 时按 Space 仍能急停，不被吞键，无需加应用级事件过滤。** 红色"停止移动"按钮同样始终可用。
- [ ] **H2 手柄连续 jog 队列合并**：手柄连续 jog 未带 `kind/metadata`，仅靠面板 `_cont_epoch` 守卫防"停后被陈旧命令重启"。CPython GIL 下原子且有序，当前安全；若改 free-threaded 或想与 GUI jog 路径一致，给它加 `kind=CONTINUOUS_JOG_KIND, metadata=axis` 走队列级 latest-wins 合并。
- [ ] **H3 板动作回调关窗守卫**：`_run_board_action` 的 on_done/on_error 缺 `if self._closing: return`，关窗后排队信号可能触达正在销毁的 UI（今天因 is_open=False 短路，未崩）；加守卫更稳。
- [ ] **H4 安全扫描异常别降级**：`_handle_async_position_refresh` 把 `_stop_unsafe_axes_if_needed` 与标签更新放同一 try，安全扫描若抛异常会被降级成"位置记录失败"而非报警；拆开、扫描异常单独升级为 alarm + audit CRITICAL。
- [ ] **H5 杂项 nit**：`directional_neighbor` 显式平手规则（当前网格无平手）；`GamepadPanel.prepare_for_close()`（关窗时停 timer + 解除，替代直戳 `_timer`）；`update_axes` 重复 `float()`；真机 DLL smoke 测试加 `skipUnless`（仅 Windows+有 DLL）；RD105 PWM 满量程 20000 待数据手册确认（仅显示、不上安全路径）。

> 备注：A1、C1、A4 是"读码无法闭环、必须真机确认"的核心三项；**D12（Y3/Y2 方向）与 H1（Space 急停）已于 2026-06-22 真机确认通过**；D2/D3 决定是否值得加摇杆速度模式快路径；其余多为确认既有修复在真实硬件上按预期工作。
