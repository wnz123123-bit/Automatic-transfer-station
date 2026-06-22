import logging
import threading
import time
from dataclasses import dataclass

import serial

from .rd105_config import (
    DEFAULT_TEMPERATURE_CONTROLLER_PORT,
    MAX_MAX_OUTPUT_DUTY_PERCENT,
    MAX_HEATING_RATE_C_PER_HOUR,
    MAX_HEATING_RATE_C_PER_MIN,
    MAX_TARGET_TEMPERATURE,
    MIN_MAX_OUTPUT_DUTY_PERCENT,
    MIN_TARGET_TEMPERATURE,
    OVERTEMP_MARGIN,
    RD105_BAUDRATE,
    RD105_CHANNEL,
    RD105_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL_SECONDS,
    WATCHDOG_STALE_SECONDS,
    load_config,
)


RD105_TEMPERATURE_SCALE = 100000
RD105_SPEED_SCALE_PER_C_PER_SECOND = 1000
RD105_SENSOR_FAULT_TEMPERATURE = 999999999
ERROR_POLL_INTERVAL_SECONDS = 5.0

_log = logging.getLogger("ats.rd105")
_audit = logging.getLogger("ats.audit")

# Over-temperature guard TRIP threshold. Distinct from MAX_TARGET_TEMPERATURE
# (the settable ceiling): the guard only trips ABOVE this, so a max-temperature
# setpoint does not trip on normal PID overshoot at the ceiling.
OVERTEMP_TRIP_TEMPERATURE = MAX_TARGET_TEMPERATURE + OVERTEMP_MARGIN

# Sanity range for a decoded DATADEMAND temperature. A value far outside the
# operating envelope is treated as a framing/parse error (no valid reading).
# The upper bound uses the trip threshold plus headroom so genuine over-temp
# readings still pass through to the guard (and trip it) rather than being
# silently dropped.
TEMPERATURE_SANE_MIN = MIN_TARGET_TEMPERATURE - 50.0
TEMPERATURE_SANE_MAX = OVERTEMP_TRIP_TEMPERATURE + 100.0

# Software-ramp per-step time cap. The GUI polls (and thus steps the ramp) about
# once per second; this caps the interval used to size a single setpoint step so
# that after a long poll stall (serial hang, UI freeze) the next setpoint cannot
# jump far toward the target in one step. 5 s tolerates normal poll jitter and a
# few missed polls while bounding a post-stall step to rate * 5 s.
RAMP_STEP_MAX_INTERVAL_SECONDS = 5.0


def _safe_int(raw_value):
    """Convert a register payload to int, returning None on a non-numeric value.

    Frame parsing already validated the prefix/terminator; a prefix-matching but
    non-numeric payload (e.g. ``=?`` / ``=ERR``) is a format anomaly that must
    degrade a single field to "unknown" rather than raising through the whole
    read/guard path.
    """
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


@dataclass
class SoftwareRampState:
    start_temperature: float
    final_target: float
    rate_per_hour: float
    start_time: float
    direction: int
    last_setpoint: float
    # Monotonic time at which last_setpoint was last written. BP1 #4 sizes each
    # step from the interval since THIS time (capped), not absolute elapsed time,
    # so a long poll stall cannot produce a large single setpoint jump.
    last_step_time: float


def select_pid_profile(target_temperature, profiles=None):
    profiles = profiles if profiles is not None else load_config()["pid_profiles"]
    target = float(target_temperature)
    ordered_profiles = sorted(profiles, key=lambda item: item["min_temp"])
    for index, profile in enumerate(ordered_profiles):
        lower = float(profile["min_temp"])
        upper = float(profile["max_temp"])
        is_last = index == len(ordered_profiles) - 1
        if lower <= target < upper or (is_last and lower <= target <= upper):
            return profile
    return None


def select_preferred_port(ports, last_port=None, current_port=None):
    devices = [port.device for port in ports]
    if current_port and current_port in devices:
        return current_port

    for port in ports:
        text = f"{port.device} {getattr(port, 'description', '')} {getattr(port, 'hwid', '')}".upper()
        if "1A86:7523" in text or "CH340" in text or "CH341" in text or "USB-SERIAL" in text:
            return port.device

    if last_port and last_port in devices:
        return last_port
    if DEFAULT_TEMPERATURE_CONTROLLER_PORT in devices:
        return DEFAULT_TEMPERATURE_CONTROLLER_PORT
    return devices[0] if devices else last_port or DEFAULT_TEMPERATURE_CONTROLLER_PORT


class TemperatureController:
    MAX_RETRIES = 3
    RETRY_DELAY = 0.08

    def __init__(
        self,
        port=DEFAULT_TEMPERATURE_CONTROLLER_PORT,
        baudrate=RD105_BAUDRATE,
        timeout=RD105_TIMEOUT,
        pid_profiles=None,
    ):
        self.serial_lock = threading.Lock()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.pid_profiles = pid_profiles if pid_profiles is not None else load_config()["pid_profiles"]

        self.ser = None
        self.is_connected = False
        self.last_error = ""
        self.last_pid_profile = None
        self.last_enable_state = None
        self.last_error_code = None
        self.last_max_output_duty = None
        self.over_temperature_alarm = False
        self._last_error_poll_time = 0.0
        self._software_ramp: SoftwareRampState | None = None
        self._clock = time.monotonic

        # BP2 independent over-temp / comm-loss watchdog. The watchdog is the
        # fail-safe backstop that protects the heater even when the GUI's 1 Hz
        # poll (the only other software over-temp guard) stops running or comm is
        # lost mid-heating. read_live_data records _last_read_ok_time on every
        # successful read; the watchdog thread checks staleness and forces the
        # heater off if heating is active but reads have gone stale.
        self._last_read_ok_time: float | None = None
        # _watchdog_tripped latches the "currently in a tripped stall" decision so
        # _watchdog_evaluate reports a trip only on the transition into staleness.
        # _watchdog_io_done latches the fail-safe disable WRITE so the trip action
        # is idempotent (no repeated ENABLE=0 spam per tick). Both clear when a
        # successful read restores comm freshness.
        self._watchdog_tripped = False
        self._watchdog_io_done = False
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_lock = threading.Lock()
        self._watchdog_interval = WATCHDOG_CHECK_INTERVAL_SECONDS

    @staticmethod
    def _encode_temperature_value(value):
        return int(round(float(value) * RD105_TEMPERATURE_SCALE))

    @staticmethod
    def _decode_temperature_value(raw_value):
        # Safety (BP1 #1): a corrupted/non-integer payload must NOT raise. If it
        # did, the broad except in read_live_data would return None and the
        # over-temp guard would never run while the heater stays enabled. Return
        # None instead so a malformed reading falls into the existing
        # "temperature is None -> disable/safe" branch.
        value = _safe_int(raw_value)
        if value is None:
            return None
        if value == RD105_SENSOR_FAULT_TEMPERATURE:
            return None
        temperature = value / RD105_TEMPERATURE_SCALE
        # Safety (BP1 #2): reject decoded outliers far outside the operating
        # envelope as framing/parse errors so a corrupt-but-numeric frame cannot
        # report a bogus (possibly low) temperature that clears a real alarm.
        if temperature < TEMPERATURE_SANE_MIN or temperature > TEMPERATURE_SANE_MAX:
            return None
        return temperature

    @staticmethod
    def _speed_from_c_per_hour(rate_per_hour):
        speed = int(round(float(rate_per_hour) / 3600.0 * RD105_SPEED_SCALE_PER_C_PER_SECOND))
        return max(1, speed)

    @staticmethod
    def _output_percent_from_raw(raw_value):
        # TODO(#10): The RD105 PWM register full-scale is NOT documented anywhere
        # in this repo. The divisor 20000.0 is therefore UNCONFIRMED — if the PWM
        # full-scale were actually 20000 = 100%, a full-on output (raw 20000) would
        # read as "1.0" here, which is almost certainly wrong. Do NOT change this
        # divisor until a repo/datasheet source confirms the real full-scale.
        # Because the scale is unconfirmed, the GUI deliberately labels this value
        # as a raw-PWM-derived figure (with a "PWM" marker) rather than a calibrated
        # percentage, so it cannot be confused with the LIMITED max-output-duty
        # percent shown right beside it (see temperature_panel._format_output_pwm).
        return int(raw_value) / 20000.0

    def _set_last_error(self, message):
        self.last_error = message
        _log.debug("rd105 error: %s", message)

    def _close_serial_locked(self):
        if self.ser:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.is_connected = False

    def _reset_buffers_locked(self):
        if not self.ser or not self.ser.is_open:
            return
        try:
            self.ser.reset_input_buffer()
        except Exception:
            try:
                if getattr(self.ser, "in_waiting", 0):
                    self.ser.read_all()
            except Exception:
                pass
        try:
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def _connect_locked(self):
        try:
            self._close_serial_locked()
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
                inter_byte_timeout=min(0.05, self.timeout),
            )
            self.is_connected = bool(self.ser and self.ser.is_open)
            if not self.is_connected:
                self._set_last_error(f"串口 {self.port} 打开失败")
                return False

            self._reset_buffers_locked()
            if self._send_ascii_command_locked("DATADEMAND=1@") is None:
                self._close_serial_locked()
                return False

            self.last_error = ""
            _log.info("已连接到 RD105 串口 %s，波特率 %s", self.port, self.baudrate)
            return True
        except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
            self._close_serial_locked()
            self._set_last_error(f"连接失败: {exc}")
            return False

    def connect(self):
        with self.serial_lock:
            ok = self._connect_locked()
        # BP2: start the independent watchdog AFTER releasing serial_lock (it
        # acquires the same lock for any IO; starting it outside avoids any
        # lock-ordering surprises). Only arm it on a successful connect so an idle
        # controller never spawns a thread or produces serial traffic.
        if ok:
            self.start_watchdog()
        return ok

    def disconnect(self):
        # BP2: stop the watchdog FIRST, BEFORE taking serial_lock. The watchdog
        # thread acquires serial_lock for its (brief) IO; joining it while we hold
        # the lock could deadlock. stop_watchdog() signals the stop Event and joins
        # the thread, after which no watchdog IO can race this disconnect.
        self.stop_watchdog()
        with self.serial_lock:
            self._cancel_software_ramp_locked()
            # Safety (#3): never leave the heater running after disconnect/app exit.
            # Attempt ENABLE=0 while the port is still open. Stay idempotent and
            # safe when not connected, and surface a disable failure instead of
            # closing the port while falsely claiming the output is off.
            self._disable_output_on_disconnect_locked()
            self._close_serial_locked()

    def _disable_output_on_disconnect_locked(self):
        if not (self.ser and self.ser.is_open):
            return
        if self.last_enable_state is False:
            # Output already known to be off; nothing to send.
            return
        try:
            if not self._set_enable_locked(False):
                # Do not raise/crash, but record that the output was NOT closed.
                if "关闭输出失败" not in (self.last_error or ""):
                    self._set_last_error("断开连接时关闭输出失败")
        except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
            self._set_last_error(f"断开连接时关闭输出失败: {exc}")
        except Exception as exc:
            self._set_last_error(f"断开连接时关闭输出失败: {exc}")

    def _ensure_connected_locked(self):
        if self.ser and self.ser.is_open:
            return True
        self._set_last_error("串口未连接")
        self.is_connected = False
        return False

    def _send_ascii_command_locked(self, command):
        if not self._ensure_connected_locked():
            return None

        if not command.endswith("@"):
            command += "@"
        payload = (command + "\n").encode("ascii")
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                self._reset_buffers_locked()
                self.ser.write(payload)
                self.ser.flush()
                response = self.ser.read_until(b"\n", size=1024)
                if not response:
                    time.sleep(0.02)
                    response = self.ser.read(1024)

                text = response.decode("ascii", errors="replace").replace("\x00", "").strip()
                if text:
                    self.is_connected = True
                    self.last_error = ""
                    time.sleep(0.005)
                    return text

                last_error = "RD105 未返回数据"
                self._set_last_error(f"第 {attempt} 次通信失败: {last_error}")
            except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
                last_error = f"串口通信异常: {exc}"
                self._set_last_error(last_error)
                self._close_serial_locked()
                break
            except Exception as exc:
                last_error = f"通信过程中发生异常: {exc}"
                self._set_last_error(last_error)

            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY)

        self.is_connected = False
        if last_error and last_error != self.last_error:
            self._set_last_error(last_error)
        return None

    @staticmethod
    def _extract_response_value(response, prefixes):
        if response is None:
            return None
        for prefix in prefixes:
            if response.startswith(prefix) and response.endswith("@"):
                return response[len(prefix):-1]
        return None

    def _query_common_value_locked(self, name):
        response = self._send_ascii_command_locked(f"{name}=?@")
        value = self._extract_response_value(response, (f"OK{name}=", f"OK:{name}="))
        if value is None:
            self._set_last_error(f"RD105 回复格式异常: {response}")
        return value

    def _query_ascii_value_locked(self, name):
        response = self._send_ascii_command_locked(f"TC{RD105_CHANNEL}:{name}=?@")
        value = self._extract_response_value(response, (f"OKTC{RD105_CHANNEL}:{name}=",))
        if value is None:
            self._set_last_error(f"RD105 回复格式异常: {response}")
        return value

    def _write_ascii_value_locked(self, name, value):
        command = f"TC{RD105_CHANNEL}:{name}={int(value)}@"
        response = self._send_ascii_command_locked(command)
        expected = f"OK{command}"
        if response == expected:
            return True
        self._set_last_error(f"RD105 写入 {name} 失败，回复: {response or '无'}")
        return False

    def _write_verified_value_locked(self, name, value):
        expected_value = int(value)
        if not self._write_ascii_value_locked(name, expected_value):
            return False
        actual_value = self._query_ascii_value_locked(name)
        if actual_value is None:
            return False
        # BP1 #3: a non-numeric read-back is a format anomaly, not a verified
        # match. Treat it as a verification failure (genuine read failure stays
        # surfaced) rather than raising through the write path.
        actual_int = _safe_int(actual_value)
        if actual_int is None or actual_int != expected_value:
            self._set_last_error(f"RD105 写入 {name} 后读回不一致: 写入 {expected_value}，读回 {actual_value}")
            return False
        return True

    def _parse_data_demand(self, response):
        data = {}
        if not response:
            return data
        # Safety (BP1 #2): the frame MUST be terminated with '@'. A truncated
        # frame (e.g. "...PWM=0" instead of "...PWM=0@") is a framing error;
        # parsing it would decode the last field from a partial value and could
        # clear a real over-temp alarm. Mirror the endswith("@") check that
        # _extract_response_value already uses and reject non-terminated frames.
        if not response.endswith("@"):
            return data
        for part in response.split("@"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            data[key.strip()] = value.strip()
        return data

    def _is_target_temperature_valid(self, target_temperature):
        return MIN_TARGET_TEMPERATURE <= float(target_temperature) <= MAX_TARGET_TEMPERATURE

    def _is_max_output_duty_valid(self, duty_percent):
        return MIN_MAX_OUTPUT_DUTY_PERCENT <= float(duty_percent) <= MAX_MAX_OUTPUT_DUTY_PERCENT

    def _is_heating_allowed_locked(self):
        if self.over_temperature_alarm:
            self._set_last_error(
                f"超温/温度读数保护中：当前温度高于 {MAX_TARGET_TEMPERATURE:.1f} °C 或读数无效，等待恢复后才能继续加热"
            )
            return False
        return True

    def _cancel_software_ramp_locked(self):
        self._software_ramp = None

    def _read_current_temperature_locked(self):
        response = self._send_ascii_command_locked("DATADEMAND=1@")
        if response is None:
            return None
        data = self._parse_data_demand(response)
        temp_raw = data.get(f"TC{RD105_CHANNEL}:TCADJTEMP")
        if temp_raw is None:
            self._set_last_error(f"RD105 实时温度缺失: {response}")
            return None
        temperature = self._decode_temperature_value(temp_raw)
        if temperature is None:
            self._set_last_error("RD105 实时温度读数无效")
        return temperature

    def _read_target_temperature_locked(self):
        raw_value = self._query_ascii_value_locked("TG")
        if raw_value is None:
            return None
        return self._decode_temperature_value(raw_value)

    def read_target_temperature(self):
        with self.serial_lock:
            try:
                return self._read_target_temperature_locked()
            except Exception as exc:
                self._set_last_error(f"读取目标温度时出错: {exc}")
                return None

    def _read_enable_state_locked(self):
        raw_value = self._query_ascii_value_locked("ENABLE")
        # BP1 #3: a prefix-valid but non-numeric ENABLE payload degrades to
        # "unknown" (None) instead of raising through read_live_data/the guard.
        value = _safe_int(raw_value)
        if value is None:
            if raw_value is not None:
                self._set_last_error(f"RD105 输出使能读数格式异常: {raw_value}")
            return None
        self.last_enable_state = bool(value)
        return self.last_enable_state

    def read_enable_state(self):
        with self.serial_lock:
            try:
                return self._read_enable_state_locked()
            except Exception as exc:
                self._set_last_error(f"读取输出使能时出错: {exc}")
                return None

    def _read_max_output_duty_locked(self):
        raw_value = self._query_ascii_value_locked("LIMITED")
        value = _safe_int(raw_value)
        if value is None:
            if raw_value is not None:
                self._set_last_error(f"RD105 最大输出占空比读数格式异常: {raw_value}")
            return None
        self.last_max_output_duty = value
        return self.last_max_output_duty

    def read_max_output_duty(self):
        with self.serial_lock:
            try:
                return self._read_max_output_duty_locked()
            except Exception as exc:
                self._set_last_error(f"读取最大输出占空比时出错: {exc}")
                return None

    def _read_error_code_locked(self, force=False):
        now = time.monotonic()
        if not force and self.last_error_code is not None and now - self._last_error_poll_time < ERROR_POLL_INTERVAL_SECONDS:
            return self.last_error_code
        raw_value = self._query_common_value_locked("ERRORCODE")
        value = _safe_int(raw_value)
        if value is None:
            if raw_value is not None:
                self._set_last_error(f"RD105 错误码读数格式异常: {raw_value}")
            return self.last_error_code
        self.last_error_code = value
        self._last_error_poll_time = now
        return self.last_error_code

    def read_error_code(self, force=True):
        with self.serial_lock:
            try:
                return self._read_error_code_locked(force=force)
            except Exception as exc:
                self._set_last_error(f"读取错误码时出错: {exc}")
                return self.last_error_code

    def _write_pid_parameters_locked(self, kp, ki, kd):
        try:
            success_p = self._write_verified_value_locked("KP", int(round(float(kp))))
            success_i = self._write_verified_value_locked("KI", int(round(float(ki))))
            success_d = self._write_verified_value_locked("KD", int(round(float(kd))))
            return success_p and success_i and success_d
        except Exception as exc:
            self._set_last_error(f"写入 PID 参数时出错: {exc}")
            return False

    def write_pid_parameters(self, kp, ki, kd):
        with self.serial_lock:
            return self._write_pid_parameters_locked(kp, ki, kd)

    def _write_max_output_duty_locked(self, duty_percent):
        if not self._is_max_output_duty_valid(duty_percent):
            self._set_last_error(
                f"最大输出占空比必须在 {MIN_MAX_OUTPUT_DUTY_PERCENT}~{MAX_MAX_OUTPUT_DUTY_PERCENT} % 之间"
            )
            return False
        duty_value = int(round(float(duty_percent)))
        if self._write_verified_value_locked("LIMITED", duty_value):
            self.last_max_output_duty = duty_value
            return True
        return False

    def _apply_over_temperature_guard_locked(self, temperature, enabled):
        if temperature is None:
            self.over_temperature_alarm = True
            self._cancel_software_ramp_locked()
            alarm_base = "温度读数保护：当前温度读数无效"
            enabled, alarm = self._disable_output_for_alarm_locked(enabled, alarm_base)
            self._set_last_error(alarm)
            return enabled, alarm
        # BP1 #5: trip at the over-temp TRIP threshold (max settable + margin),
        # NOT at the settable ceiling itself, so a legitimate max-temperature
        # target does not trip on normal PID overshoot at the ceiling.
        if float(temperature) <= OVERTEMP_TRIP_TEMPERATURE:
            self.over_temperature_alarm = False
            return enabled, None

        self.over_temperature_alarm = True
        self._cancel_software_ramp_locked()
        alarm_base = (
            f"超温保护：当前温度 {float(temperature):.2f} °C 高于 "
            f"{OVERTEMP_TRIP_TEMPERATURE:.1f} °C"
        )
        enabled, alarm = self._disable_output_for_alarm_locked(enabled, alarm_base)
        self._set_last_error(alarm)
        return enabled, alarm

    def _disable_output_for_alarm_locked(self, enabled, alarm_base):
        if enabled is False:
            alarm = f"{alarm_base}，输出已关闭"
            _audit.error("RD105 over-temp/sensor-fault guard tripped (output already off): %s", alarm_base)
        elif self._set_enable_locked(False):
            enabled = False
            alarm = f"{alarm_base}，已关闭输出"
            _audit.error("RD105 over-temp/sensor-fault guard tripped, output disabled: %s", alarm_base)
        else:
            alarm = f"{alarm_base}，关闭输出失败"
            _audit.critical(
                "RD105 CRITICAL: over-temp/sensor-fault guard tripped but FAILED to disable output: %s",
                alarm_base,
            )
        return enabled, alarm

    def _apply_pid_for_target_locked(self, target_temperature):
        profile = select_pid_profile(target_temperature, self.pid_profiles)
        if profile is None:
            self._set_last_error(f"目标温度 {target_temperature:.2f} °C 没有匹配的 PID 参数")
            return False
        max_output_duty = profile.get("max_output_duty")
        if max_output_duty is not None and not self._write_max_output_duty_locked(max_output_duty):
            return False
        if not self._write_pid_parameters_locked(profile["kp"], profile["ki"], profile["kd"]):
            return False
        self.last_pid_profile = profile
        return True

    def _guard_current_temperature_before_heating_locked(self):
        current_temperature = self._read_current_temperature_locked()
        if current_temperature is None:
            enabled = self._read_enable_state_locked()
            self._apply_over_temperature_guard_locked(None, enabled)
            return None, False
        # BP1 #5: use the same TRIP threshold the live guard uses so the
        # pre-heating check and the live over-temp guard agree on what is "too hot".
        if float(current_temperature) > OVERTEMP_TRIP_TEMPERATURE:
            enabled = self._read_enable_state_locked()
            _, safety_alarm = self._apply_over_temperature_guard_locked(current_temperature, enabled)
            return current_temperature, safety_alarm is None
        self.over_temperature_alarm = False
        return current_temperature, True

    def _software_ramp_next_setpoint_locked(self):
        ramp = self._software_ramp
        if ramp is None:
            return None

        # BP1 #4: size the step from the interval since the LAST written setpoint
        # (capped), stepping relative to last_setpoint — NOT from absolute elapsed
        # time since start. With SPEED=0 the hardware rate limit is off, so after a
        # long poll stall an absolute-time computation would jump the setpoint a
        # large amount toward the target in one step, defeating slow heating of
        # fragile samples. Capping dt bounds a single post-stall step to
        # rate * RAMP_STEP_MAX_INTERVAL_SECONDS.
        dt = max(0.0, self._clock() - ramp.last_step_time)
        dt = min(dt, RAMP_STEP_MAX_INTERVAL_SECONDS)
        travel = ramp.rate_per_hour * dt / 3600.0
        candidate = ramp.last_setpoint + ramp.direction * travel
        if ramp.direction > 0:
            return min(candidate, ramp.final_target)
        return max(candidate, ramp.final_target)

    def _advance_software_ramp_locked(self):
        ramp = self._software_ramp
        if ramp is None:
            return None, None

        next_setpoint = self._software_ramp_next_setpoint_locked()
        if next_setpoint is None:
            return None, None

        if abs(next_setpoint - ramp.last_setpoint) >= 0.00001:
            if not self._set_target_temperature_locked(next_setpoint):
                control_error = self.last_error or "软件斜坡 TG 写入失败"
                self._cancel_software_ramp_locked()
                return None, control_error
            ramp.last_setpoint = float(next_setpoint)
            # Consume the elapsed interval only once a setpoint is actually
            # written. If the step was below the write threshold, leave
            # last_step_time unchanged so the small fraction keeps accumulating.
            ramp.last_step_time = self._clock()

        reached = (
            ramp.direction > 0 and ramp.last_setpoint >= ramp.final_target
        ) or (
            ramp.direction < 0 and ramp.last_setpoint <= ramp.final_target
        )
        current_setpoint = ramp.last_setpoint
        if reached:
            self._software_ramp = None
        return current_setpoint, None

    def read_live_data(self):
        with self.serial_lock:
            try:
                response = self._send_ascii_command_locked("DATADEMAND=1@")
                if response is None:
                    return None

                # BP2: a successful live-data read refreshes the watchdog freshness
                # timestamp and clears any latched watchdog trip so a NEW stall can
                # trip again. Done under serial_lock (we hold it here) so the
                # watchdog thread sees a consistent timestamp.
                self._last_read_ok_time = self._clock()
                self._watchdog_tripped = False
                self._watchdog_io_done = False

                data = self._parse_data_demand(response)
                temp_raw = data.get(f"TC{RD105_CHANNEL}:TCADJTEMP")
                output_raw = data.get(f"TC{RD105_CHANNEL}:PWM")

                temperature = None if temp_raw is None else self._decode_temperature_value(temp_raw)
                output_percent = None if output_raw is None else self._output_percent_from_raw(output_raw)
                target_temperature = self._read_target_temperature_locked()
                enabled = self._read_enable_state_locked()
                max_output_duty = self._read_max_output_duty_locked()
                enabled, safety_alarm = self._apply_over_temperature_guard_locked(temperature, enabled)
                control_error = None
                if safety_alarm:
                    ramp_setpoint = None
                else:
                    ramp_setpoint, control_error = self._advance_software_ramp_locked()
                if ramp_setpoint is not None:
                    target_temperature = ramp_setpoint
                error_code = self._read_error_code_locked(force=False)
                if safety_alarm:
                    # Safety (#9): if the guard fired but the disable command
                    # FAILED, the output is still on — the control-state field must
                    # not read as "system is now safe". Surface the not-closed case.
                    if "关闭输出失败" in safety_alarm:
                        run_state = "超温保护-关闭输出失败"
                    else:
                        run_state = "超温保护"
                else:
                    run_state = "输出开启" if enabled else "输出关闭" if enabled is False else "未知"
                ramp = self._software_ramp

                return {
                    "temperature": temperature,
                    "effective_setpoint": target_temperature,
                    "output_percent": output_percent,
                    "max_output_duty_percent": max_output_duty,
                    "run_state": run_state,
                    "error_code": error_code,
                    "safety_alarm": safety_alarm,
                    "control_error": control_error,
                    "software_ramp_active": ramp is not None,
                    "software_ramp_final_target": None if ramp is None else ramp.final_target,
                    "software_ramp_rate_per_hour": None if ramp is None else ramp.rate_per_hour,
                }
            except Exception as exc:
                self._set_last_error(f"读取实时数据时出错: {exc}")
                return None

    def _set_target_temperature_locked(self, target_temperature):
        return self._write_verified_value_locked("TG", self._encode_temperature_value(target_temperature))

    def _set_speed_locked(self, speed_value):
        return self._write_verified_value_locked("SPEED", speed_value)

    def _set_enable_locked(self, enabled):
        if self._write_verified_value_locked("ENABLE", 1 if enabled else 0):
            self.last_enable_state = bool(enabled)
            _log.info("RD105 ENABLE → %s", "ON" if enabled else "OFF")
            return True
        return False

    def fast_set_temperature(self, target_temperature):
        with self.serial_lock:
            try:
                if not self._is_target_temperature_valid(target_temperature):
                    self._set_last_error(
                        f"目标温度必须在 {MIN_TARGET_TEMPERATURE:.1f}~{MAX_TARGET_TEMPERATURE:.1f} °C 之间"
                    )
                    return False
                if not self._is_heating_allowed_locked():
                    return False
                self._cancel_software_ramp_locked()
                _, heating_safe = self._guard_current_temperature_before_heating_locked()
                if not heating_safe:
                    return False
                if not self._apply_pid_for_target_locked(target_temperature):
                    return False
                if not self._set_speed_locked(0):
                    return False
                if not self._set_target_temperature_locked(target_temperature):
                    return False
                return self._set_enable_locked(True)
            except Exception as exc:
                self._set_last_error(f"快速设温失败: {exc}")
                return False

    def ramp_to_temperature(self, target_temperature, rate_per_hour):
        with self.serial_lock:
            try:
                if not self._is_target_temperature_valid(target_temperature):
                    self._set_last_error(
                        f"目标温度必须在 {MIN_TARGET_TEMPERATURE:.1f}~{MAX_TARGET_TEMPERATURE:.1f} °C 之间"
                    )
                    return False
                if not self._is_heating_allowed_locked():
                    return False
                rate_per_hour = float(rate_per_hour)
                if rate_per_hour <= 0:
                    self._set_last_error("控温速率必须大于 0 °C/小时")
                    return False
                if rate_per_hour > MAX_HEATING_RATE_C_PER_HOUR:
                    self._set_last_error(
                        f"控温速率不能高于 {MAX_HEATING_RATE_C_PER_MIN:.1f} °C/分钟"
                    )
                    return False
                self._cancel_software_ramp_locked()
                current_temperature, heating_safe = self._guard_current_temperature_before_heating_locked()
                if not heating_safe:
                    return False
                if not self._apply_pid_for_target_locked(target_temperature):
                    return False
                if not self._set_speed_locked(0):
                    return False
                direction = 1 if float(target_temperature) >= float(current_temperature) else -1
                first_setpoint = float(current_temperature)
                if abs(float(target_temperature) - first_setpoint) < 0.00001:
                    first_setpoint = float(target_temperature)
                if not self._set_target_temperature_locked(first_setpoint):
                    return False
                if not self._set_enable_locked(True):
                    return False
                if abs(float(target_temperature) - first_setpoint) < 0.00001:
                    self._cancel_software_ramp_locked()
                else:
                    ramp_now = self._clock()
                    self._software_ramp = SoftwareRampState(
                        start_temperature=first_setpoint,
                        final_target=float(target_temperature),
                        rate_per_hour=rate_per_hour,
                        start_time=ramp_now,
                        direction=direction,
                        last_setpoint=first_setpoint,
                        last_step_time=ramp_now,
                    )
                return True
            except Exception as exc:
                self._set_last_error(f"按速率控温失败: {exc}")
                return False

    def stop_heating(self):
        with self.serial_lock:
            try:
                self._cancel_software_ramp_locked()
                return self._set_enable_locked(False)
            except Exception as exc:
                self._set_last_error(f"关闭输出失败: {exc}")
                return False

    # ------------------------------------------------------------------ BP2
    # Independent over-temperature / comm-loss watchdog.
    #
    # This is a fail-safe backstop that is INDEPENDENT of the GUI poll. The GUI's
    # 1 Hz poll is the only other software over-temp protection; if it stops
    # (timer stopped, panel destroyed, GUI frozen, _poll_in_flight stuck,
    # auto_poll off) or comm is lost mid-heating, the RD105 PID would keep driving
    # the heater with NO software protection. The watchdog closes that gap.

    def _watchdog_heating_active(self):
        """True when the controller BELIEVES heating is active.

        Uses only controller-owned state (no IO): the last commanded output-enable
        state and whether a software ramp is driving setpoints. When neither is
        true the watchdog is a no-op, so an idle controller never trips and never
        emits serial traffic.
        """
        if self._software_ramp is not None:
            return True
        return self.last_enable_state is True

    def _watchdog_evaluate(self, now):
        """Pure decision: should the watchdog trip NOW? No IO, no side effects
        except latching.

        Trips when heating is active AND the time since the last SUCCESSFUL read
        exceeds WATCHDOG_STALE_SECONDS.

        Latch behaviour (Fix 1):
        - Once tripped, returns True on subsequent stale ticks UNTIL the fail-safe
          ENABLE=0 write has been CONFIRMED (``_watchdog_io_done=True``). This
          lets the tick loop re-enter ``_watchdog_trip_action`` on every tick until
          the write actually succeeds — critical when the very comm-loss that tripped
          the watchdog causes the first ENABLE=0 attempt to fail.
        - Once the write has succeeded (``_watchdog_io_done=True``) subsequent stale
          ticks return False so the loop stops issuing redundant writes.
        - Both latches clear when a successful read restores comm freshness (see
          ``read_live_data`` and the ``not stale`` branch below).
        """
        if not self._watchdog_heating_active():
            return False
        last_ok = self._last_read_ok_time
        if last_ok is None:
            # Never had a successful read while heating-active is asserted: treat
            # as stale (cannot prove comm is healthy).
            stale = True
        else:
            stale = (now - last_ok) > WATCHDOG_STALE_SECONDS
        if not stale:
            # Comm is fresh again: clear the latches so a NEW stall can trip and
            # re-issue the fail-safe disable. (read_live_data also clears these on
            # a successful read; this covers freshness observed by the watchdog.)
            self._watchdog_tripped = False
            self._watchdog_io_done = False
            return False
        if self._watchdog_tripped and self._watchdog_io_done:
            # Already tripped AND the disable write has been confirmed. No need to
            # re-issue; stop calling trip_action until comm is restored.
            return False
        # Either this is the first trip on this stall, or it is a retry because a
        # previous disable attempt failed (tripped=True, io_done=False).
        self._watchdog_tripped = True
        return True

    def _watchdog_trip_action(self):
        """Best-effort fail-safe IO on a watchdog trip.

        Writes ENABLE=0 under serial_lock (reusing the verified disable path),
        cancels the software ramp, latches a persistent safety alarm so the next
        snapshot/UI surfaces it. Never writes to a closed/None port. Routes any
        failure through the existing error channel (CRITICAL-worthy once BP3
        logging lands).

        Retry semantics (Fix 1): ``_watchdog_io_done`` is set True ONLY when the
        ENABLE=0 write SUCCEEDS. If it fails (e.g. the comm-loss that tripped the
        watchdog also breaks the write), ``_watchdog_io_done`` is left False so the
        next watchdog tick re-enters this method and retries the write. The alarm
        and ramp-cancel happen on the FIRST call (guarded by ``_watchdog_tripped``)
        and are not repeated; only the write itself is retried.

        Idempotency once the write succeeds: ``_watchdog_io_done=True`` causes
        ``_watchdog_evaluate`` to return False, so the tick loop no longer calls
        this method — no redundant ENABLE=0 spam.
        """
        with self._watchdog_lock:
            # Fast-exit if the disable write has already been confirmed this stall.
            if self._watchdog_io_done:
                return
            with self.serial_lock:
                # Guard: never write to a closed/None port.
                if not (self.ser and getattr(self.ser, "is_open", False)):
                    return
                base = "通信丢失或轮询停止——已强制关闭加热输出"
                # First-trip side-effects: latch the alarm and cancel the ramp.
                # These happen exactly once per stall (guarded by _watchdog_tripped
                # which was set by _watchdog_evaluate on the first trip). They do
                # NOT depend on the write outcome and must not be repeated.
                if not self._watchdog_tripped:
                    # Defensive: evaluate always sets this before calling us, but
                    # guard it anyway.
                    self._watchdog_tripped = True
                if not self.over_temperature_alarm:
                    self.over_temperature_alarm = True
                    self._cancel_software_ramp_locked()
                # Attempt the fail-safe ENABLE=0. Only latch _watchdog_io_done
                # when the write is CONFIRMED so a failed write is retried on the
                # next watchdog tick.
                try:
                    if self._set_enable_locked(False):
                        self._watchdog_io_done = True
                        self._set_last_error(base)
                        _audit.critical("RD105 watchdog: comm-loss/stale — forced ENABLE=0 succeeded. %s", base)
                    else:
                        # Write failed; leave _watchdog_io_done=False so the next
                        # tick retries. Record the failure so the UI sees it.
                        self._set_last_error(f"{base}（关闭输出失败，将重试）")
                        _audit.critical(
                            "RD105 watchdog CRITICAL: comm-loss/stale — ENABLE=0 FAILED, will retry. %s", base
                        )
                except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
                    self._set_last_error(f"{base}（关闭输出异常: {exc}，将重试）")
                    _audit.critical(
                        "RD105 watchdog CRITICAL: comm-loss/stale — ENABLE=0 raised %s, will retry. %s",
                        exc, base,
                    )
                except Exception as exc:
                    self._set_last_error(f"{base}（关闭输出异常: {exc}，将重试）")
                    _audit.critical(
                        "RD105 watchdog CRITICAL: comm-loss/stale — ENABLE=0 raised %s, will retry. %s",
                        exc, base,
                    )

    def _watchdog_tick(self):
        """One watchdog iteration: evaluate (no IO) then act (IO) only on a trip.

        Acquires NO lock for the evaluate step and only the serial_lock briefly
        inside _watchdog_trip_action when it actually needs to write. Swallows and
        routes its own exceptions so the loop can never die silently.
        """
        try:
            if self._watchdog_evaluate(self._clock()):
                self._watchdog_trip_action()
        except Exception as exc:  # never let the watchdog thread die silently
            try:
                self._set_last_error(f"看门狗检查异常: {exc}")
            except Exception:
                pass

    def _watchdog_loop(self):
        # Loop until stop is signaled. Critically, we WAIT on the stop Event
        # instead of sleeping so disconnect()/shutdown returns promptly, and we
        # NEVER hold serial_lock across this wait — the lock is only taken briefly
        # inside _watchdog_trip_action.
        while not self._watchdog_stop.is_set():
            self._watchdog_tick()
            # Event.wait returns True as soon as stop is set -> prompt exit.
            self._watchdog_stop.wait(self._watchdog_interval)

    def start_watchdog(self):
        """Start the daemon watchdog thread if not already running.

        Daemon so it can never block process exit; also stopped explicitly on
        disconnect(). Idempotent: a second call while running is a no-op.

        Fix 2: resets BOTH watchdog latches (_watchdog_tripped AND
        _watchdog_io_done) so that a reused controller instance (after a
        disconnect→reconnect cycle) cannot inherit a stale _watchdog_io_done=True
        from a prior session that would suppress a real disable in the new session.
        """
        with self._watchdog_lock:
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop.clear()
            self._watchdog_tripped = False
            self._watchdog_io_done = False  # Fix 2: clear stale latch from prior session
            thread = threading.Thread(
                target=self._watchdog_loop,
                name="rd105-watchdog",
                daemon=True,
            )
            self._watchdog_thread = thread
            thread.start()

    def stop_watchdog(self):
        """Signal the watchdog to stop and join it promptly.

        Must NOT be called while holding serial_lock (the thread may be acquiring
        serial_lock for IO; joining under the lock could deadlock). Safe/idempotent
        when no thread is running.

        Fix 3: the join timeout now covers the worst-case in-flight verified
        ENABLE=0: a _write_verified_value_locked call issues a write command then
        a read-back query, each of which retries up to MAX_RETRIES times with
        waits of RD105_TIMEOUT + RETRY_DELAY between attempts.  The worst-case
        latency for one of those passes is:
            MAX_RETRIES × (RD105_TIMEOUT + RETRY_DELAY) ≈ 3 × 0.38 s ≈ 1.14 s
        Two passes (write + readback) gives ≈ 2.3 s; double that plus the
        watchdog interval for a comfortable margin → 6.0 s.
        """
        # Worst-case time for one verified write (write + readback), computed from
        # the class-level constants so the timeout stays correct if they change.
        _worst_case_verified_write = (
            2 * self.MAX_RETRIES * (self.timeout + self.RETRY_DELAY)
        )
        _join_timeout = max(6.0, self._watchdog_interval + _worst_case_verified_write + 1.0)
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            # The interval wait wakes immediately on the stop Event; allow the
            # computed margin for any in-flight tick's IO to finish before we
            # drop the thread reference.
            thread.join(timeout=_join_timeout)
        self._watchdog_thread = None
