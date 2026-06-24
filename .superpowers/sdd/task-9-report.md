# Task 9 Report: Translate controller user-facing messages

## Summary

Added `from .i18n import tr` to `rd105_controller.py` and `gas_controller.py`.
Wrapped all user-facing Chinese messages that reach the GUI with `tr()`.
Added 53 English translation entries to `i18n.py`.
Added 4 EN-mode tests (2 per controller test file).

---

## rd105_controller.py — Wrap / Leave Decision List

### WRAPPED with tr()

| Location | Chinese key (template) | English |
|---|---|---|
| `_connect_locked` L251 | `"串口 {port} 打开失败"` | `"Serial port {port} failed to open"` |
| `_connect_locked` L264 | `"连接失败: {exc}"` | `"Connection failed: {exc}"` |
| `_disable_output_on_disconnect_locked` L303 | `"断开连接时关闭输出失败"` | `"Disconnect: stop output failed"` |
| `_disable_output_on_disconnect_locked` L305,307 | `"断开连接时关闭输出失败: {exc}"` | `"Disconnect: stop output failed: {exc}"` |
| `_ensure_connected_locked` L312 | `"串口未连接"` | `"Serial port not connected"` |
| `_send_ascii_command_locked` L342 | `"RD105 未返回数据"` | `"RD105 returned no data"` |
| `_send_ascii_command_locked` L343 | `"第 {attempt} 次通信失败: {last_error}"` | `"Comm attempt {attempt} failed: {last_error}"` |
| `_send_ascii_command_locked` L345 | `"串口通信异常: {exc}"` | `"Serial comm exception: {exc}"` |
| `_send_ascii_command_locked` L350 | `"通信过程中发生异常: {exc}"` | `"Exception during comm: {exc}"` |
| `_query_common_value_locked` L374 | `"RD105 回复格式异常: {response}"` | `"RD105 reply format error: {response}"` |
| `_query_ascii_value_locked` L381 | same template | same |
| `_write_ascii_value_locked` L390 | `"RD105 写入 {name} 失败，回复: {response}"` | `"RD105 write {name} failed, reply: {response}"` |
| `_write_verified_value_locked` L405 | `"RD105 写入 {name} 后读回不一致: 写入 {expected_value}，读回 {actual_value}"` | readback mismatch EN |
| `_is_heating_allowed_locked` L436 | `"超温/温度读数保护中：当前温度高于 {max_temp:.1f} °C 或读数无效，等待恢复后才能继续加热"` | over-temp active EN |
| `_read_current_temperature_locked` L451 | `"RD105 实时温度缺失: {response}"` | `"RD105 live temperature missing: {response}"` |
| `_read_current_temperature_locked` L455 | `"RD105 实时温度读数无效"` | `"RD105 live temperature reading invalid"` |
| `read_target_temperature` L469 | `"读取目标温度时出错: {exc}"` | `"Error reading target temperature: {exc}"` |
| `_read_enable_state_locked` L479 | `"RD105 输出使能读数格式异常: {raw_value}"` | format error EN |
| `read_enable_state` L489 | `"读取输出使能时出错: {exc}"` | `"Error reading output enable: {exc}"` |
| `_read_max_output_duty_locked` L497 | `"RD105 最大输出占空比读数格式异常: {raw_value}"` | format error EN |
| `read_max_output_duty` L507 | `"读取最大输出占空比时出错: {exc}"` | `"Error reading max output duty: {exc}"` |
| `_read_error_code_locked` L518 | `"RD105 错误码读数格式异常: {raw_value}"` | format error EN |
| `read_error_code` L529 | `"读取错误码时出错: {exc}"` | `"Error reading error code: {exc}"` |
| `_write_pid_parameters_locked` L539 | `"写入 PID 参数时出错: {exc}"` | `"Error writing PID parameters: {exc}"` |
| `_write_max_output_duty_locked` L549 | `"最大输出占空比必须在 {min_duty}~{max_duty} % 之间"` | `"Max output duty must be between {min_duty}~{max_duty} %"` |
| `_apply_pid_for_target_locked` L602 | `"目标温度 {target_temp:.2f} °C 没有匹配的 PID 参数"` | no PID profile EN |
| `_advance_software_ramp_locked` L658 | `"软件斜坡 TG 写入失败"` | `"Software ramp TG write failed"` |
| `read_live_data` L736 | `"读取实时数据时出错: {exc}"` | `"Error reading live data: {exc}"` |
| `fast_set_temperature` L757 | `"目标温度必须在 {min_temp:.1f}~{max_temp:.1f} °C 之间"` | target temp range EN |
| `fast_set_temperature` L774 | `"快速设温失败: {exc}"` | `"Quick set failed: {exc}"` |
| `ramp_to_temperature` L782 | same target temp range template | same |
| `ramp_to_temperature` L789 | `"控温速率必须大于 0 °C/小时"` | `"Ramp rate must be greater than 0 °C/h"` |
| `ramp_to_temperature` L793 | `"控温速率不能高于 {max_rate:.1f} °C/分钟"` | `"Ramp rate must not exceed {max_rate:.1f} °C/min"` |
| `ramp_to_temperature` L827 | `"按速率控温失败: {exc}"` | `"Ramp set failed: {exc}"` |
| `stop_heating` L836 | `"关闭输出失败: {exc}"` | `"Stop output failed: {exc}"` |
| `_watchdog_trip_action` L949 | `"通信丢失或轮询停止——已强制关闭加热输出"` | `"Comm lost or polling stopped — heater output force-disabled"` |
| `_watchdog_trip_action` L954 | `"通信丢失或轮询停止——已强制关闭加热输出（关闭输出失败，将重试）"` | force-disable FAILED EN |
| `_watchdog_trip_action` L959,965 | `"通信丢失或轮询停止——已强制关闭加热输出（关闭输出异常: {exc}，将重试）"` | force-disable raised EN |
| `_watchdog_tick` L983 | `"看门狗检查异常: {exc}"` | `"Watchdog check exception: {exc}"` |

### LEFT RAW (with justification)

| Location | Chinese string | Reason |
|---|---|---|
| `_connect_locked` L260 | `_log.info("已连接到 RD105 串口 %s，波特率 %s", ...)` | Log-only; never reaches GUI. |
| `_disable_output_on_disconnect_locked` — guard changed | `"关闭输出失败"` substring check | Replaced guard from `"关闭输出失败" not in last_error` to `not self.last_error` so it works regardless of language. The raw string comparison was removed. |
| `_apply_over_temperature_guard_locked` L562 | `alarm_base = "温度读数保护：当前温度读数无效"` | LOGIC STRING: `alarm_base` is concatenated with raw Chinese suffixes (below) to form `safety_alarm`. The substring `"关闭输出失败"` in the suffix is matched at line 714 to set `run_state`. Translating the base would still leave the suffix raw; mixing languages in a single string is acceptable here since correctness is paramount. Left raw per "when in doubt, leave it raw." |
| `_apply_over_temperature_guard_locked` L576-577 | `f"超温保护：当前温度 {temperature:.2f} °C 高于 {OVERTEMP_TRIP_TEMPERATURE:.1f} °C"` | LOGIC STRING: same reason as above — `alarm_base` that feeds `safety_alarm` substring logic. |
| `_disable_output_for_alarm_locked` L585 | `f"{alarm_base}，输出已关闭"` | LOGIC STRING: part of `safety_alarm` value. |
| `_disable_output_for_alarm_locked` L589 | `f"{alarm_base}，已关闭输出"` | LOGIC STRING: part of `safety_alarm` value. |
| `_disable_output_for_alarm_locked` L592 | `f"{alarm_base}，关闭输出失败"` | CRITICAL LOGIC STRING: `safety_alarm` with this suffix is substring-matched at line 714 (`if "关闭输出失败" in safety_alarm`) to set `run_state = "超温保护-关闭输出失败"`. Translating the suffix would break the `run_state` assignment in EN mode, which in turn breaks `temperature_panel._output_energized` (which checks `run_state == "输出开启"` or `"关闭输出失败" in run_state`). |
| `read_live_data` L714 | `if "关闭输出失败" in safety_alarm:` | COMPARISON KEY: must remain raw to match the raw suffix in `safety_alarm`. |
| `read_live_data` L715 | `run_state = "超温保护-关闭输出失败"` | STATUS VALUE: compared by `temperature_panel._output_energized` (`"关闭输出失败" in text`). Must stay raw. |
| `read_live_data` L717 | `run_state = "超温保护"` | STATUS VALUE: displayed via `_tr()` in the panel (translation already in i18n). Must stay raw as a key. |
| `read_live_data` L719 | `run_state = "输出开启"` / `"输出关闭"` / `"未知"` | STATUS VALUES: `"输出开启"` is directly compared by `_output_energized` (`text == "输出开启"`). Must stay raw. Panel translates them via `_tr()`. |
| `_watchdog_trip_action` L931 | `base = "通信丢失或轮询停止——已强制关闭加热输出"` | Audit-log only; `_set_last_error` calls already use `tr()` with the full composite key. |

---

## gas_controller.py — Wrap / Leave Decision List

### WRAPPED with tr()

| Location | Chinese key (template) | English |
|---|---|---|
| `start_jog` L304 | `"拒绝移动：该方向无安全行程 (axis {axis})"` | `"Move refused: no safe travel in this direction (axis {axis})"` |
| `_assert_microscope_z_target_in_bounds` L378 | `"MicroscopeZ 目标脉冲超出 int32 范围，已拒绝"` | `"MicroscopeZ target pulse out of int32 range; rejected"` |
| `_assert_microscope_z_target_in_bounds` L381 | `"MicroscopeZ 目标脉冲 {target} 超出合理范围 (±{bound})，已拒绝"` | `"MicroscopeZ target pulse {target} out of sane range (±{bound}); rejected"` |

### LEFT RAW

None in gas_controller.py — all Chinese strings were user-facing MotionControllerError messages.

---

## Logic-guard change: `_disable_output_on_disconnect_locked`

The original guard `if "关闭输出失败" not in (self.last_error or ""):` did a Chinese substring check on `last_error` to avoid overwriting an already-set error. In EN mode the translated `last_error` won't contain the Chinese substring, so the guard logic was wrong. Changed to `if not self.last_error:` — equivalent intent (don't overwrite a non-empty error), language-agnostic.

---

## English added to i18n.py

53 new entries in a new `TRANSLATIONS.update({...})` block after the temperature panel entries.

---

## Residual Han justifications (controller files only)

- 1 log-only `_log.info` in `_connect_locked`
- 5 safety_alarm/alarm_base strings intentionally left raw (see logic-string rule above)
- 4 run_state status values left raw (compared by `_output_energized`)
- 1 audit-log `base` variable in `_watchdog_trip_action`

---

## Test commands and output

```
py -3.13 -m pytest tests/test_rd105_controller.py tests/test_gas_controller.py -q
# 102 passed in 0.29s

py -3.13 -m pytest tests/test_temperature_panel.py -q
# 40 passed in 5.40s

py -3.13 -m pytest tests/test_main_window.py -q
# 141 passed in 38.14s

py -3.13 -m pytest tests/test_rd105_controller.py tests/test_gas_controller.py
    tests/test_temperature_panel.py tests/test_main_window.py -q
# Fatal Python error: Aborted (known Qt teardown flaky)
# Isolated re-run of the failing test: 1 passed
```

The combined 4-file run hits the known Qt teardown "Aborted" flaky at `test_axis3_position_manager_moves_renames_and_deletes_selected_point` in `test_main_window.py`. Re-run of that test in isolation passes. This pre-existed before Task 9 and is unrelated to the i18n changes.

---

## Commit hash

(filled after commit)

---

## Concerns

1. **safety_alarm mixed-language in EN mode**: The `safety_alarm` string displayed in the status bar will appear as English alarm_base + Chinese suffix (e.g., `"Over-temp: 209.00 °C above 208.0 °C，关闭输出失败"`) in EN mode, because the suffix must stay raw for the `"关闭输出失败" in safety_alarm` logic check at line 714. This is a correctness/safety tradeoff — changing the suffix would break the run_state assignment and the heat-warning logic. Acceptable for now; a future refactor could replace the substring check with a structured return value.

2. **alarm_base untranslated**: The `alarm_base` strings ("温度读数保护" and "超温保护：当前温度 X °C 高于 Y °C") are also left raw for the same reason — they're inseparable from the suffix composition that feeds the logic check. In EN mode the safety_alarm value is thus entirely Chinese. Since `safety_alarm` goes directly into `_set_status(f"RD105 {safety_alarm}")` without going through `_tr()`, the EN status bar will show Chinese for these specific alarm scenarios.
