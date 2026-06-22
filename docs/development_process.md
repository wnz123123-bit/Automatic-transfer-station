# EthBoardTest Python 开发进程

更新日期：2026-06-22

本文档记录 EthBoardTest Python 版本的开发脉络，便于之后回看为什么采用当前结构、哪些硬件事实已经确认、哪些设计约束需要继续遵守。

原始开发目录：

```text
D:\EthBoardTest_0127
```

当前迁移交付目录：

```text
E:\Automatic transfer station
```

## 1. 初始目标

原程序是 C# WinForms 运动控制程序，使用运动控制卡和厂家 `GAS.dll` 通讯。目标是在不破坏原始 C# 源程序的前提下，单独建立 Python 程序目录，保持原程序 GUI 和运动功能，并逐步加入二维材料转移台的实际轴定义、标定、温控和物镜切换功能。

Python 主程序包当前放在：

```text
E:\Automatic transfer station\src\automatic_transfer_station
```

原始 C# 源文件仍保留在原开发目录根目录，作为协议、控件和历史逻辑参考。迁移交付目录包含 Python 主程序包、测试、文档、运行时状态、配置和厂家运动卡 SDK 参考文件。

## 2. Python 控制层迁移

第一阶段建立了分层控制结构：

- `gas_api.py`：`GAS.dll` 的 ctypes 封装，负责函数签名和错误码返回。
- `gas_controller.py`：真实运动控制卡控制器，封装打开板卡、Trap 运动、Jog/连续运动、停止、回零、位置读取和安全检查。
- `controller_base.py`：运动控制器接口、通用轴号、板卡 IP 等基础定义。
- `run_eth_board_test.py`：程序入口，只有真实硬件模式。

> 注：早期曾有 `sim_controller.py` 模拟控制器和 `--sim` 模拟模式，用于无硬件调试。在线调试完成后已整体移除（见第 11 节），程序现为纯真实硬件。

程序不会在启动时自动打开板卡，必须由用户在界面中点击 `打开运动控制`。这是当前程序的安全默认行为。

## 3. 轴标定和硬件事实确认

随后逐轴完成了二维材料转移台的轴定义和标定，形成了当前的轴命名：

| 轴号 | 名称 | 作用 |
| --- | --- | --- |
| 1 | X1 | 整体左右平移 |
| 2 | Y1 | 整体前后平移 |
| 3 | MicroscopeZ | 显微镜上下位移 |
| 4 | Z | 载玻片升降 |
| 5 | X3 | 吸附台左右移动 |
| 6 | Y3 | 吸附台前后移动 |
| 7 | X2 | 载玻片左右移动 |
| 8 | Y2 | 载玻片前后移动 |
| 9 | R | 样品旋转 |

最终标定参数写入两处：

- 人可读源记录：`docs/axis_calibration_records.md`
- 程序执行常量：`src/automatic_transfer_station/axis_calibration.py`

重要结论：

- X1/Y1 使用 `12500 pulse/mm`。
- 其余平移轴使用 `50000 pulse/mm`。
- R 使用 `0.00024 deg/pulse`，允许范围 `-5..365 deg`。
- X2/X3 有机械干涉关系，程序使用 pulse 空间边界保护。
- Y1 正向/前向限位损坏，程序暂不启用该限位。
- Axis 3 没有绝对坐标和硬限位，作为特殊轴处理。

## 4. GUI 预览和 PySide6 迁移

为了确认界面风格，先建立了本地 HTML 预览：

```text
docs\ui_preview\stage_gui_preview.html
```

确认后的视觉语言包括：

- 左侧深色设备图片区域。
- 图片上使用局部轴标签，不使用大面积箭头覆盖。
- 中间为紧凑轴控制卡片。
- 右侧为温度控制面板。
- 顶部状态栏使用状态灯和 `打开运动控制 / 关闭运动控制 / 复位 / 停止移动` 操作。
- 控制按钮统一为连续负向、点动负向、停止、点动正向、连续正向的五键布局。

该风格已经迁移到 PySide6 主界面 `main_window.py`。后续 GUI 修改应延续这套工业控制风格，不要改成新的通用 dashboard 风格。

## 5. 运行时状态和标定持久化

为了避免每次启动后丢失显示位置，加入了运行时状态文件：

```text
runtime\axis_positions.json
```

该文件只用于显示和状态恢复，不会把保存值写回运动卡。真实硬件打开后，控制卡读回值会覆盖界面显示。

另外加入运行时标定存储：

```text
runtime\axis_runtime_calibration.json
```

通过轴卡片中的 `标定` 按钮，可以把当前 pulse 读数对应到用户输入的实际坐标。标定会同时影响显示坐标和软件安全边界。

当前实现要求标定必须基于真实控制器读回 pulse 或已缓存 pulse，不能仅根据界面显示值反推 pulse。标定值也必须落在该轴的物理范围内。

## 6. 显微镜相关功能

显微镜上下轴 `MicroscopeZ` 保留 pulse 坐标，不定义绝对 mm 零点。界面下方提供：

- 显微镜上下控制。
- 点位管理，用于保存多个显微镜高度 pulse 点位。
- 物镜选择预留和控制。

物镜切换参考 LV-NCNT2 手册实现：

- `nosepiece_controller.py`：串口协议控制。
- `objective_panel.py`：GUI 面板和 COM 设置弹窗。

当前物镜映射：

| 放大倍率 | LV-NCNT2 地址 |
| --- | --- |
| 5X | 1 |
| 10X | 2 |
| 20X | 3 |
| 40X | 4 |
| 50X | 5 |

## 7. 温度控制集成

温控 GUI 已集成到主界面最右侧，目标控制器为 RD105：

- `rd105_controller.py`：RD105 串口协议控制。
- `rd105_config.py` / `config\rd105_config.json`：端口、波特率和 PID 配置。
- `temperature_panel.py`：温控 GUI 面板。

当前主界面已经具备连接、状态显示、设温、按速率控温、关闭输出和曲线显示。RD105 的完整控温参数由 PID 和最大输出占空比组成，保存在 `config\rd105_config.json`，程序按目标温度自动选择并写入。

## 8. 安全审查和修复

一次详细代码审查后，已经完成以下安全和稳定性修复：

- `GA_GetSts` 的硬限位判断改用 SDK 真实轴状态位：
  - `AXIS_STATUS_POS_HARD_LIMIT = 0x20`
  - `AXIS_STATUS_NEG_HARD_LIMIT = 0x40`
- 连续运动对有软件行程的轴使用有限 Trap 目标，不再无边界 Jog。
- 模拟控制器也复用同一套连续运动安全边界，避免模拟行为误导测试。
- 红色 `停止移动` 按钮走短路径，发送停止后不立即读取全部轴位置。
- 标定必须使用真实或缓存 pulse，并做范围校验。
- 物镜 COM 弹窗和 MicroscopeZ 点位弹窗同类只保留一个实例，避免旧弹窗读错控件。

相关测试已经扩展到 173 个，覆盖硬限位状态位、运行时标定、连续运动安全边界、GUI 弹窗生命周期和启动 smoke。

## 9. 2026-06-20 温控与运动队列加固

本轮修复聚焦 RD105 温控错误上报和真实模式 GUI 卡顿风险：

- RD105 超温保护现在会区分“已关闭输出”和“关闭输出失败”。如果关闭输出命令失败，GUI 不再误报输出已关闭。
- Python 软件斜坡推进 TG 失败时会取消斜坡，并通过 snapshot 的 `control_error` 传给温控面板显示。
- 温控面板兼容缺少 `software_ramp_rate_per_hour` 的旧 snapshot，不会因为显示速率而崩溃。
- 真实模式下连续移动、短步移动、目标位置移动、单轴停止、全轴停止、R 回零、R twist、MicroscopeZ 点位移动、Z 弹窗运动和位置刷新走 `MotionCommandQueue`，避免控制卡调用阻塞 GUI 线程。
- 位置刷新成功和失败路径都带 generation 标记，关闭或重开运动控制后的旧刷新结果会被丢弃，不能重新创建旧队列、覆盖新状态或清掉新一轮 pending 状态。
- GUI 轴控制继续保留五键左右箭头布局，前后轴和左右轴都使用同一套左右箭头图标。

## 10. 当前清理原则

已清理 Python 缓存、pytest 缓存、Visual Studio `obj/.vs` 生成目录、PySide GUI 预览截图和 preview JSON。

刻意保留：

- `runtime\axis_positions.json`：实际位置显示恢复需要。
- `vendor\motion_card_sdk`：厂家运动卡 SDK，包含 `GAS.dll`、`GAS.lib` 和 `GAS_N.h`。
- 原 C# 源码仍保留在 `D:\EthBoardTest_0127`，后续排查协议和 SDK 行为仍有参考价值。

## 11. 2026-06-20 移除模拟模式与代码审查修复

在线调试完成后，做了一轮"移除模拟模式 + 多代理代码审查 + 修复确认问题"的工作。本阶段完成时测试基线为 `181 passed`（后续手柄功能加入后升至 216，见第 12 节）。

### 移除模拟模式

- 删除 `sim_controller.py`、`SimMotionController`、`SimNosepieceController`，以及 `--sim` / `--real` 命令行参数和 `MainWindow.real_mode` 形参。程序现为纯真实硬件。
- 由此所有运动统一走 `MotionCommandQueue`。`MainWindow` 新增 `motion_command_queue_factory` 注入点，生产默认线程化 worker；`MotionCommandQueue` 新增 `inline=True` 同步模式仅供测试。
- 依赖模拟器的 GUI 测试改用测试内的 `RecordingController` fake。

### 运动卡线程与生命周期修复

- `MotionCommandQueue` worker 对每个动作捕获所有异常（含 DLL 层裸 `RuntimeError`、`ctypes.ArgumentError`），上报失败后继续存活，不再静默死亡导致命令堆积。
- `_active_motion_queue()` 以 `controller.is_open` 门控：未打开返回 `None`、运动命令被忽略，关闭后或首次打开前都不会复活队列或泄漏线程。
- `GasMotionController.close_board` 用 try/except 保证 `GA_Stop` 失败也一定执行 `GA_Close`（`GA_Close` 失败则保留 `is_open=True` 以便重试）；`open_board` 增加重复打开保护。
- `relative_move` 目标取整改用 `_round_half_up`；`move_axis_to_coordinate` 不再把显式 `0` 速度当成默认 20。
- 删除已知会让 Python 崩溃的死代码 `home_set_prm_single` / `GA_HomeSetPrmSingle`；`AxisHomePrm` 维持默认对齐并加注说明。

### 标定与安全边界修复

- `safe_continuous_target` 增加方向校验：当 X2/X3 干涉钳制后的目标与请求 jog 方向相反（已处于干涉违规态）时返回 `None` 拒绝，不再反向运动；缺少本轴当前位置且发生钳制时同样拒绝。
- 轴9 `angle_degrees` / `cached_axis9_angle` 改走标定换算，运行时标定后与 `coordinate` 一致。
- X2（轴7）负向软限位改为 `-25.30394 mm`，与文档记录的物理 `NEG_HARD_LIMIT`（`-1133407 pulse`）一致，避免 X2 静止在左限位时被安全扫描误报。

### 温控修复

- `disconnect()` / 程序退出在关串口前先关加热器（ENABLE=0）；关闭失败时不误报已关闭，并在面板上提示。
- 温控串口轮询、设温、控温、连接、关输出移到 worker 线程，避免通信变差时冻结 GUI。
- 超温保护若关输出失败，`run_state` 显式反映"输出未关闭"，不再误读为安全。
- `wait_for_idle` 增加整体超时，避免串口驱动卡死时窗口无法关闭。
- 温控面板只读读数（`QLabel[readout="true"]`，如"控制状态/输出开启"）加 `min-height: 22px`，修复中文字形（纯中文值如"输出开启"）上下被裁切的问题；数字值不受影响是因为数字未用到完整字高。

### 物镜修复

- LV-NCNT2 `connect()` / `read_position()` 移到 worker 线程，端口配错也不再冻结 GUI 最多 8 秒；`connect` 捕获 `serial.SerialException`；替换控制器前先断开旧实例。
- NAK 响应增加命令匹配校验。

### 物镜到位确认（2026-06-20，同批次）

现场现象：发 `cRDC<n>` 切换命令转盘实际转到位，但控制器回 `nRDCd`（控制超时）、读位置 `rRAR` 一直返回 `0`，拿不到到位确认。

加了诊断：`SerialNosepieceController` 新增可选 `trace` 钩子，`run_eth_board_test.py` 新增 `--nosepiece-trace` / `--nosepiece-trace-file`，把所有串口收发帧（含被跳过的 `SAE` 和超时空读）写入 `runtime\nosepiece_trace.log`，纯观测。

随后用户提供 LV-NCNT2 手册并提示"似乎只能朝一个方向旋转"。据此直接驱动硬件实测，定位到根因：直接定位命令 `cRDC<p>` 在本机一律 `nRDCd`（控制超时）+ `RAR=0`，而 `cRDC<当前>` 秒回 `oRDC`。手册命令表显示 `RCW`=正向到下一格、`RCR`/`RCC`=反向到下一格、**反向 1→5 被固件禁止（保护样品）**；实测时序（`1→2` 0.9s、`3→5` 1.3s 失败，未识别态 10s 超时）与"`RDC` 走反向、撞 1→5 禁止堵转超时"完全吻合（堵转疑似还复位过 COM7）。

**修复**：`move_to_position` 改为手册推荐的 **`cRCW` 正向逐格旋转**——`RAR` 读当前位，已在目标即返回；否则每发一次 `cRCW`（`oRCW` 同步确认）后用 `RAR` 严格确认，到目标即停，最多 `FORWARD_STEP_LIMIT`(=8) 步，到不了报错。正向不触发 1→5 禁止，`RAR=0` 也不会误判成功；`RAR=0` 时首个 `cRCW` 会重新落位。移除了不再需要的 `move_verify_timeout/interval` 与 `RDC` 轮询逻辑。已通过单元测试（假串口），**真机最终确认待 COM7 恢复后实跑**（参见 `handoff.md` §6）。诊断日志保留。

## 12. 2026-06-21 F710 手柄控制功能实现

本轮在规格（`docs/superpowers/specs/2026-06-21-f710-gamepad-control-design.md`）和实施计划（`docs/superpowers/plans/2026-06-21-f710-gamepad-control.md`）的指导下，以 TDD 方式分 6 个阶段完成了 Logitech F710 手柄控制功能，并经多代理代码审查确认。本阶段完成时测试基线 **216 passed**（后续审查修复包升至 301，见第 13 节）。

### 6 个实施阶段

1. **安全与标定基础**：确认轴映射、死区、速度比例、干涉边界常量；补齐 `axis_calibration.py` 中 `X2_X3_INTERFERENCE_MARGIN_PULSE` 和相关测试。
2. **XInput 读取层（`gamepad_xinput.py`）**：通过 ctypes 调用 `xinput1_4.dll`（备选 `xinput9_1_0.dll`）；实现 `GamepadReader`、`GamepadSnapshot`、`load_default_reader`；连接后单索引缓存轮询，断连时才慢扫 4 个槽位；不含死区/曲线逻辑，纯读取。
3. **纯决策层（`gamepad_controller.py`）**：无 Qt、无硬件、无线程，时钟可注入；把 `GamepadSnapshot` 转成运动"意图"列表；含死区+重缩放+平方曲线、tap/hold 检测、7 轴轴选择器、按 packet 判定输入卡死、断连与长时间失活自动解除。
4. **Qt 面板（`gamepad_panel.py`）**：`GamepadPanel(QGroupBox)`，用 `QTimer` 轮询 reader，把意图翻译成运动命令（连续/点动经 `MotionCommandQueue`，急停走直达路径）；启用开关 + 状态 HUD；`closeEvent` 中先停 timer 再关队列。
5. **主窗口集成**：将 `GamepadPanel` 嵌入 `MainWindow`；手柄启用时对应轴 GUI 按钮禁用；`changeEvent` 失焦自动 disarm；全局 `btnStopAll` 始终可用；`X2/X3` 对向前推（partner look-ahead）干涉保护。
6. **启动器**：`run_transfer_station.py` / `run_eth_board_test.py` 新增 `--no-gamepad` 参数；默认启用手柄；XInput DLL 加载失败时非致命（打印中文提示并停用，程序照常运行）。

### 安全要点

- 手柄启用是显式开关；窗口失焦/最小化自动 disarm 并全停。
- 断连、输入卡死（packet number 不变且非中位）、长时间无操作均触发自动 disarm + 全停。
- 全停走直达（绕过队列），停止命令最高优先级 + epoch 机制作废队列中该轴尚待执行的连续命令。
- X2/X3 连续移动带 partner look-ahead，只收紧干涉边界，绝不放松最终 `is_target_safe` 检查。

### 硬件实测推迟

按用户要求，所有 F710 手柄相关硬件测试（扳机独立性确认、断连延迟测量、速度模式评估、参数调优等）均推迟到程序完全写完后再做。待办清单见 `handoff.md` §10。

## 13. 2026-06-21 多代理 12 维度代码+GUI 审查与修复

本轮对完整代码库和 GUI 进行 12 维度多代理审查，产生 49 条经过验证的发现，分 P1–P7 共 7 个修复包以 TDD 方式完成。当前测试基线 **301 passed**（原手柄实现后基线 216）。

### 审查与修复范围

- **P1 持久化加固**：运行时标定/位置/显微镜点位三个存储文件加入 `schema_version` 校验；标定加载增加坐标范围和 `zero_pulse` 合理性校验；越界/损坏自动回退内置标定并告警；原子写升级为 write→flush→`os.fsync`→`os.replace`；加载时清理残留 `.tmp`；抽出共享 `json_store.py`。
- **P2 运动队列与急停**：连续 jog 按轴合并（latest-wins），杜绝板卡卡顿恢复后回放陈旧命令；急停路径（"停止移动"按钮/手柄 BACK/松手）先 `cancel_pending()` 再下发停止；位置刷新 `QTimer` 关窗时先停并加守卫。
- **P3 连续 jog 边界收紧**：有量程轴 `safe_continuous_target` 返回 `None` 时拒绝并抛 `MotionControllerError`，不再降级为无界速度 jog；无量程轴（轴3）当时保留速度 jog（**已于 2026-06-22 改为梯形行进至 sanity cap，详见 §16 第 9 项**）；坐标移动对无绝对标定轴抛 `MotionControllerError` 替换裸 `ValueError`。
- **P4 手柄逻辑修复**：摇杆与方向键同轴不再抖动；活动连续 jog 期间抑制同轴点按；扳机死区对齐；同 tick 换轴+改步长作用到新轴。
- **P5 死代码清理**：移除孤立常量、未使用导入和过时注释。
- **P6 GUI 安全可视化**："停止移动"按钮放大并绑定全局 Space 急停快捷键；危险操作（标定覆盖/设温 ≥150 °C/R 回零）加确认对话框；干涉/限位自动停轴时轴卡红边+顶部指示灯持久 alarm 态；禁用按钮明显置灰；手柄状态在状态栏提示；选中轴高亮含非颜色线索；GAS.dll 加载失败给出清晰中文提示。
- **P7 补测试**：针对上述修复补充单元测试和 GUI smoke，将基线从 216 升至 301。

### 补审盲区（下一轮目标）

本轮未覆盖的风险区域留待下一轮审查：串口/网络 IO 健壮性（`rd105_controller`/`nosepiece_controller` 协议解析、超时、部分帧；`gas_network_probe`）、`gamepad_xinput.py` 摇杆死区归一化源头（扁平死区非圆形）、`objective_panel.py` 三个 `QThread` 并发/生命周期、RD105 超温安全逻辑、安全事件持久化审计日志。轴3 软钳位和 Space 快捷键现场确认亦挂起（见 `handoff.md` §10）。

## 14. 2026-06-22 7 维度盲点审查与 BP1–BP7 修复

本轮对第 13 节修复包之后的剩余盲区进行 7 维度（网络 IO、串口 IO、RD105 热安全、手柄输入源、物镜并发/生命周期、可观测性/审计、打包配置一致性）多代理盲点审查，确认 **34 条经过验证的发现**，以 TDD 方式分 BP1–BP7 修复包完成。测试基线由 **301 → 411 passed**（本节新增 9 条 BP7 测试后实测 420）。

### 审查与修复范围

- **BP1 RD105 热安全**：畸形/截断帧三重防御（`int()` 守卫 + `@` 终止符 + 数值合理性）；软件斜坡 dt 上限钳制防大跳；过温跳闸阈值与最大设定点分离（`OVERTEMP_MARGIN = +3 °C`）；删除可绕过上限的 `write_verified_value`。
- **BP2 RD105 独立过温看门狗**：独立后台守护线程，基于最后成功读取陈旧度强制 `ENABLE=0`；失败持续重试；面板侧 in-flight 卡死超时 `POLL_STUCK_TIMEOUT_SECONDS` 按通信失败处理。
- **BP3 审计日志**：`RotatingFileHandler` 写 `RUNTIME_DIR/transfer_station.log`，`ats.audit` 命名 logger；运动自动停止/限位/急停/板操作/加热器/过温/看门狗/校准覆写落盘；RD105 由 `print()` 改为日志。
- **BP4 网络/串口健壮性**：`get_prf_pos`/`get_status` 失败统一翻译为 `MotionControllerError`；`GA_Open` 前 IP 三重校验；probe 复用 `gas_api` 签名；转盘 `read_until(size=64)` + 单读超时 + 墙钟截止；串口异常自愈。（注：BP4 曾在 open 时加显式 `GA_SetCardNo(0)`，真机返回错误 7 阻断打开，后已移除。）
- **BP5 板卡 open/close/reset 线程化**：可注入执行器在工作线程执行 `GA_Open`/`GA_Close`/`GA_Reset`（生产线程化，测试内联），不可达时不再冻结 GUI ~21 s。
- **BP6 物镜并发/生命周期**：`_closing` 守卫 worker 回调；关窗 QThread reparent+保留引用+quit 替换 terminate；操作中拒绝切换控制器；IO 加锁；`refresh_position` is-open 前置检查。手柄圆形（径向）死区替换方形死区；`trigger_deadzone` 独立。存储拒绝 NaN/inf 脉冲。
- **BP7 打包/配置一致性**（本节）：`gas_network_probe.DEFAULT_IP_ADDRESS` 引用 `controller_base.BOARD_IP`（单一来源）；`paths.py` 新增 `sys.frozen` 分支（非冻结路径字节等价）；argparse baud 默认值通过测试与模块常量对齐。

### 完整性批评遗留缺口（下一轮优先）

- 运动安全几何/算术核心：`safe_continuous_target` 钳制反解边界、`_round_half_up` 边界无单元测试。
- 反应式安全扫描单点失效/时序：运动侧唯一靠 GUI poll，无独立后台守卫（同 RD105 看门狗修复前类比）。
- 干涉判定缺轴位置时静默跳过（应明确拒绝）。
- `reset_board` 在报警态下清状态语义未验证。
- 失焦解除时已发出连续 jog 的 epoch 机制覆盖范围需确认。

详见 `handoff.md` §10。

## 15. 2026-06-22 运动安全核心第三轮审查（CP1–CP5）

本轮对运动安全核心进行 **6 维度**（运动安全算术、反应式扫描时序、gas_controller 路径、急停/解除、持久化值路径、测试覆盖）专项审查，确认 **11 条经过验证的发现**（其中 2 条 Critical），分 CP1–CP5 修复包以 TDD 方式完成。测试基线由 **411 → 467 passed**（新增 B1/B2/B3 安全路径测试 + A1/A3 合规测试）。

### 修复范围

- **CP1 反应式安全扫描后备**：`_request_async_position_refresh` 加有界单次看门狗定时器（`POSITION_REFRESH_WATCHDOG_MS`）；超时 → 响亮持久 CRITICAL 审计 + bump generation（陈旧读结果被丢弃）+ 清 pending 闩锁（扫描可恢复）+ 尽力保护性停（`_attempt_protective_stop_all`，priority 0）；连续失败达 `CONSECUTIVE_READ_FAILURE_LIMIT` → 保护性停止 + CRITICAL 审计 + 持久告警灯。已知架构残留：单 worker + `_api_lock`，wedged DLL 读无法被软件抢占，看门狗提供检测/告警/恢复，最终后备为物理急停/硬限位/预防性 jog 钳位。
- **CP2 软限位端点取整**：`safe_continuous_target` 用 `soft_limit_endpoint_pulse`（定向取整）替代 `pulse_from_coordinate`（半入取整），保证端点脉冲严格落在软范围内；分数 `zero_coordinate` 重归零后不再误拒 jog 方向；核实手柄 `partner_lookahead_pulse` 量纲为脉冲，正确。
- **CP3 持久化 MicroscopeZ 脉冲**：`move_axis3_to_position` 前加 int32 范围守卫（`-2^31 ≤ pulse ≤ 2^31-1`，防 `c_long` 静默回绕导致错向/负位移动）+ 保守物理 sanity cap（`MICROSCOPE_Z_MAX_ABS_PULSE`）；`microscope_position_store` 加载时跳过/保存时拒绝超 cap 脉冲。
- **CP4 reset_board**：`GA_Reset` 后幂等重应用硬限位配置（`_apply_hard_limit_config`），与 `open_board` 行为一致，保证复位后限位仍激活。
- **CP5 干涉检查伙伴缺失 + 急停对称性**：`is_target_safe` / `unsafe_axes_for_positions` 改为 fail-safe——缺伙伴位置时判不安全并返回 `"干涉伙伴位置不可用 (axes (5, 7))"` 而非静默跳过；`_on_motion_queue_pending_cancelled` 新增 `_disarm_position_refresh_watchdog()` 使"pending 清除 ⇒ 看门狗解除"成为显式不变式；`_stop_all_motion` 加明确注释解释 GUI 队列路径 vs 手柄直达路径的刻意不对称（避免未来维护者"统一"导致回归）。

### 新增测试

- **B1**: `start_jog` 在 partner 轴 `get_prf_pos` 抛 `RuntimeError` 时必须抛 `MotionControllerError` 且不下发任何运动（轴5/轴7 各一个用例，`tests/test_gas_controller.py`）。
- **B2**: `safe_continuous_target` 在 own 轴位置缺失且钳位触发时必须返回 `None`（直接单元测试，`tests/test_axis_calibration.py`）。
- **B3**: 位置快照缺一个干涉轴时 `_stop_unsafe_axes_if_needed` 必须触发停止——不静默视为安全（`tests/test_main_window.py`）。
- **A1/A3 合规测试**: `is_target_safe(7, target, {7: ...})` → not ok；`_on_motion_queue_pending_cancelled` → 看门狗不激活（`tests/test_axis_calibration.py` 和 `tests/test_main_window.py`）。

详见 `handoff.md` §6 "运动安全核心审查后增强（CP1–CP5）" 和 §10 真机待确认项。

## 16. 2026-06-22 真机调试修正

本轮为 CP1–CP5 完成后在真实硬件上首次完整调试 + v0.0.1 发布前审查中处理的 14 项事项（含手柄重映射、显微镜Z 手柄速度解耦、Y3/Y2 方向反转、版本号 + 发布前全量审查等）。自动化测试使用假 DLL（始终返回 0），无法复现多数真机行为。测试基线由 **467 → 494 passed**。

1. **运动卡打开回归修复**：BP4 曾在 `open_board` 加显式 `GA_SetCardNo(0)`，真机返回错误 7，被 `_check` 当致命错误阻断打开。已移除该调用——单板网络下 DLL 默认即 card 0，无需调用。

2. **物镜默认 COM 口 = COM7**：新增 `DEFAULT_NOSEPIECE_PORT="COM7"`，COM 对话框下拉默认预选 COM7；`--nosepiece-port` 仍可覆盖。

3. **物镜 cRCW 控制超时容错**：LV-NCNT2 常在 `cRCW` 命令上回 Control Timeout（`'d'`），即使转盘已物理转到位。现改为：收到 `'d'` 不直接失败，而是用 RAR 读实际地址确认——到目标即判成功，否则再走一格（最多 8 步），8 步仍不到才报错；RAR 读到 `'d'` 也按"暂不可识别"重试。数据/命令/硬件/禁止方向错误仍如实报错；显式"读取位置"按钮保持严格。

4. **物镜连接状态徽标**：常驻连接指示从灰色小字升级为带彩色圆点的胶囊徽标——绿色●=已连接 COMx，红色●=未连接。

5. **手柄：移除 packet"卡死"误判自动停用**：原 `_is_stale` 在 `dwPacketNumber` 连续 4 帧不变且非中位时自动停用。真机上按住摇杆/扳机数值不变 → packet 不变 → 被误判为冻结，导致"一动就退出"。无法区分"按住"与"真冻结"，已移除该检测。保留的安全网：断连自停、中位失活自停、软限位/干涉钳住、失焦自停、BACK/Space 急停。

6. **手柄：选中轴横幅**：设备图顶部叠加琥珀色横幅"手柄选中: X3 · 吸附台左右"，启用手柄即显示当前选中轴，切轴实时更新，停用隐藏；同时保留轴卡高亮 + 热点 ▶ 前缀。

7. **手柄：方向键选轴顺序调整**：`SELECTOR_AXES` 由 `(5,6,7,8,9,3,4)` 改为 `(5,6,7,8,9,4)`——显微镜Z（轴3）移出方向键选轴（仍由扳机 LT/RT 控制）。新增 `GAMEPAD_AXES=(5,6,7,8,9,3,4)` 用于"启用时禁用 GUI 按钮的仲裁"和"急停作废待执行连续命令"，使显微镜Z 两项保护仍被覆盖。

8. **设备图热点自动去重叠**：`StageMapWidget` 加 declutter 算法，重叠的轴标签自动沿最小穿透方向推开，任意窗口尺寸/缩放下不再堆叠。

9. **显微镜Z（轴3）连续移动改用梯形行进**：轴3 原是唯一走 `GA_PrfJog` 速度爬行模式的连续移动轴；真机上该模式按住不出力，表现为"按住不动、松手才动一下"。改为和其它轴一致的 `GA_PrfTrap` 梯形行进——朝 jog 方向的远端 sanity cap 目标（`±MICROSCOPE_Z_MAX_ABS_PULSE`）行进、速度取 `abs(speed)`、松手 `stop_axis(3)` 即停。GUI 连续按钮与手柄 LT/RT 扳机共用 `start_jog(3,…)`，一并修好；扳机/摇杆均为比例。连续 jog 全轴不再用 `GA_PrfJog`（仅 `speed==0` 防御性 no-op 保留）。改写轴3 jog 两条断言 + 增正向目标断言，基线 481 → **482 passed**。

10. **（续）显微镜Z 速度封顶 4 mm/s——步进失步**：梯形行进修好后，真机实测 1000–4000 um/s 都平滑，但 **5000 um/s（= 250 脉冲/ms = 250 kHz 步率 @ 50000 脉冲/mm）步进电机失步**：电机原地丢步不走，而界面坐标读的是**指令位置**（`GA_GetPrfPos`，非真实编码器）所以数字照常递增、看不出失步；松手减速段经过可跟速度才"动一下"。GUI 按钮（单指令）与手柄都复现 → 确认是硬件速度上限，非软件。修法：`MICROSCOPE_Z_SPEED_OPTIONS_UM_S` 去掉 5000、封顶 4000（补 2000/3000 中间档）。移除上一轮临时诊断日志。基线维持 **482 passed**。（注：手柄 LT/RT 的显微镜Z 速度随后于本轮第 12 项与 GUI 速度档解耦为固定 4 mm/s。）其它 50000 脉冲/mm 轴（4/5/6/7/8）在 5000 同为 250 kHz，理论上也可能失步，待现场反馈再决定是否同样封顶。

11. **手柄重映射（十字键 + 面键 + 实时横幅）**：按用户新指示重排手柄。**十字键 上/下** = 选中轴按步长**单步** ±（仅单步、不连续、不自动重复，且若该轴正被摇杆连续驱动则抑制这次单步）；**十字键 左/右** = 步长档减/增。**四面键 Y/A/X/B** = **2D 方向选轴**（上/下/左/右），用新纯函数 `directional_neighbor()` 按设备图几何选该方向最近的可选轴。`SELECTOR_AXES` 扩为 8 轴 `(5,6,7,8,9,4,1,2)`（新增 **X1/Y1**，仍排除显微镜Z），`GAMEPAD_AXES=(5,6,7,8,9,3,4,1,2)`；为 X1/Y1 补 `STEP_SNAP[1]/[2]`（否则 `_gamepad_set_step` KeyError）。新增 `STAGE_MAP_AXIS_POSITIONS` 作为"设备图热点位置 + 选轴几何"的单一来源（`_build_stage_map` 改从它取坐标）。设备图横幅升级为"手柄选中: 轴 · 角色 · 步长 X µm · 坐标 Y mm"，并挂到 100ms 位置刷新**实时更新坐标**（R 轴用 °）。清理：移除重映射后失效的 `_active_stick_axes` 及 `_taphold` 的 `active_stick_axes` 死参数/死分支。改写/新增手柄测试（删 12 旧、增 13 新，含 2D 几何、同 tick 仲裁、选轴跟随、对角组合、横幅单位）。经 4 维对抗式审查（22 项发现→10 确认，全为测试空白/死码/文档，无行为缺陷）后补齐测试。基线 482 → **485 passed**。

12. **手柄显微镜Z 速度与 GUI 解耦为固定 4 mm/s**：按用户要求，手柄 LT/RT 控制显微镜Z 的速度**固定为 4000 um/s（4 mm/s）**，不再读 GUI `txtSpeed3` 速度档（即"不需和界面一一对应"）。新增常量 `GAMEPAD_MICROSCOPE_Z_SPEED_UM_S=4000`；`MainWindow._gamepad_speed(axis)` 包装：轴3 返回固定值，其它轴仍走 `_speed`（跟 GUI）；`GamepadPanel(speed_fn=self._gamepad_speed)`。比例扳机仍 0..4000 缩放，满扳机=4 mm/s。GUI 显微镜Z 按钮仍按速度档。基线 485 → **486 passed**。

13. **三项收尾：横幅不溢出 + 2D 选轴跟 GUI 列表 + Y3/Y2 方向反转**：
    - **横幅溢出**：`StageMapWidget` 选中轴横幅 `setWordWrap(True)` + `_position_selected_axis_label` 把宽度上限钳到图像宽-8，长横幅自动换行不再越界。
    - **2D 选轴几何改用 GUI 卡片网格**：新增 `SELECTOR_GRID_POSITIONS`（(col,row)，吸附台列 X3/Y3/R/X1、载玻片列 X2/Y2/Z/Y1），`directional_neighbor` 默认改用它（不再用设备照片几何 `STAGE_MAP_AXIS_POSITIONS`，后者只供设备图热点）。上下在列内、左右在列间，跟界面卡片列表直觉一致。
    - **Y3(6)/Y2(8) 方向反转**（真机方向纠正）：电机实际方向与软件 +pulse 假设相反，`pulse_sign` 翻为 -1，GUI 按钮与手柄（摇杆+方向键）方向**一起**纠正、坐标读数随之一致。Y2 非对称软限位 swap+negate（min/max −25.099/25.280 → −25.280/25.099），**物理脉冲行程包络 [-1339950,1179000] 完全不变**（仅坐标标签翻号）。`_controller_direction_for_coordinate_direction` 对 pulse_sign<0 返回 -dir，GUI/手柄共用。
    - 经聚焦对抗式审查（11 项→4 确认，全为测试加强/过时注释，**代码确认正确**）后补：精确边界+往返断言钉死 Y2 包络、`_controller_direction_for_coordinate_direction` 全 9 轴直测、横幅钳宽测试、修正两处过时注释。基线 486 → **489 passed**。**Y3/Y2 物理方向必须真机小步长复验。**

14. **v0.0.1 版本号 + 发布前全量代码审查**：
    - **版本号**：单一来源 `automatic_transfer_station.__version__ = "0.0.1"`；主窗口状态栏**右下角**常驻 `v0.0.1`（状态栏永久部件，不被临时消息挤掉）。
    - **5 子系统对抗式审查**（运动安全核心 / 手柄 / GUI 生命周期·并发 / 外设·持久化·日志 / 测试·入口）：**无 critical/important 安全缺陷**，5 个子系统均评为 strong shape；20 项发现全是健壮性/取证/测试覆盖改进，逐条自核实。
    - **已修（源码）**：① `ats.audit` 安全审计独立到 `transfer_station.audit.log`（专用 RotatingFileHandler，INFO+，独立轮转——不再被主日志 DEBUG churn 轮转掉）；② `rd105_config.save_config` 改原子写（temp+fsync+replace），与其它存储一致；③ MicroscopeZ `relative_move` 补 int32+sanity cap（抽出 `_assert_microscope_z_target_in_bounds` 共享，三条轴3 目标路径不再漂移）；④ 手柄失焦改用 `applicationStateChanged`——整个**应用**失去前台即解除手柄（不只本窗口失活）；⑤ `now_utc()`/日志格式改毫秒（安全事件可排序）；⑥ 修正 `_gamepad_stop_all` 注释（直达仅绕队列、仍受 `_api_lock`，不能抢占卡死的 worker）。
    - **已补（测试，+5）**：DLL 加载失败→返回 1+弹窗、手柄初始化失败→仍启动（禁用手柄）、`ats.audit` 专用文件、GUI 点动被拒→只试一次不重试且报错入状态栏、两条安全审计测试改确定性快照（去掉自禁用 `skipTest`）。基线 489 → **494 passed**。
    - **暂缓（已记录，非 live bug）**：手柄连续 jog 加 `kind/metadata` 队列合并（CPython GIL 下 epoch 守卫已安全）；`directional_neighbor` 显式平手规则；Space 急停可能被聚焦输入框吞键（**红色"停止移动"按钮始终可用**，待真机确认是否需应用级事件过滤）；板动作 on_done/on_error 加 `_closing` 守卫；安全扫描异常不应被降级为"位置记录失败"；`GamepadPanel.prepare_for_close`；其余 nit。详见 §11 验收清单 H 组。
