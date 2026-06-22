import copy
import json
from pathlib import Path

from .paths import CONFIG_DIR


CONFIG_PATH = CONFIG_DIR / "rd105_config.json"

MIN_TARGET_TEMPERATURE = -200.0
MAX_TARGET_TEMPERATURE = 205.0
# Over-temperature TRIP margin (°C) above the max settable setpoint. The guard
# trips at MAX_TARGET_TEMPERATURE + OVERTEMP_MARGIN, NOT at MAX_TARGET_TEMPERATURE
# itself, so a legitimate max-temperature target does not trip on normal PID
# overshoot. MAX_TARGET_TEMPERATURE remains the settable ceiling. Kept small
# (a few °C) so a genuine over-temperature near the fragile-sample limit still
# trips promptly and disables the heater.
OVERTEMP_MARGIN = 3.0
DEFAULT_TARGET_TEMPERATURE = 20.0
MAX_HEATING_RATE_C_PER_MIN = 9.9
MAX_HEATING_RATE_C_PER_HOUR = MAX_HEATING_RATE_C_PER_MIN * 60.0
DEFAULT_TEMPERATURE_CONTROLLER_PORT = "COM9"
RD105_BAUDRATE = 38400
RD105_TIMEOUT = 0.3
RD105_CHANNEL = 1

# BP2 independent over-temperature / comm-loss watchdog.
#
# WATCHDOG_STALE_SECONDS: how long the controller may go WITHOUT a successful
# live-data read while heating is active before the watchdog forces the heater
# off. The GUI polls about once per second; this is set to ~8x that cadence so
# normal poll jitter and a few missed/slow polls never trip it, while a genuine
# comm loss or a stopped/frozen GUI poll trips within a bounded few seconds and
# disables the heater. This watchdog is INDEPENDENT of the GUI poll and is the
# fail-safe backstop when the only software over-temp protection (the 1 Hz poll
# guard) stops running.
WATCHDOG_STALE_SECONDS = 8.0

# How often the watchdog thread re-evaluates staleness. One second matches the
# poll cadence: fine-grained enough to act promptly after the stale threshold is
# crossed, coarse enough to add no meaningful serial traffic (and it only ever
# performs IO on an actual trip, never on a healthy tick).
WATCHDOG_CHECK_INTERVAL_SECONDS = 1.0

# Panel-side STUCK-poll timeout. If a single GUI poll (_poll_in_flight) never
# returns within this long, the read is treated as hung (comm failure) and the
# panel surfaces a prominent alarm and clears the in-flight flag so polling can
# recover. Larger than WATCHDOG_STALE_SECONDS so the controller watchdog (which
# actually commands the heater off) is the first line of defense and the panel
# alarm confirms it rather than racing it.
POLL_STUCK_TIMEOUT_SECONDS = 10.0
MIN_MAX_OUTPUT_DUTY_PERCENT = 0
MAX_MAX_OUTPUT_DUTY_PERCENT = 90
DEFAULT_MAX_OUTPUT_DUTY_PERCENT = 40

DEFAULT_CONFIG = {
    # Intentionally different from DEFAULT_TEMPERATURE_CONTROLLER_PORT ("COM9"):
    # "last_port" is the UI-remembered default saved in config and shown in the
    # port combo, while DEFAULT_TEMPERATURE_CONTROLLER_PORT is the hardware
    # fallback used by select_preferred_port() when auto-detecting connected devices.
    "last_port": "COM5",
    "pid_profiles": [
        {
            "name": "pid_30c",
            "min_temp": MIN_TARGET_TEMPERATURE,
            "max_temp": 40.0,
            "kp": 15134,
            "ki": 133,
            "kd": 4109,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_50c",
            "min_temp": 40.0,
            "max_temp": 60.0,
            "kp": 4791,
            "ki": 133,
            "kd": 905,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_70c",
            "min_temp": 60.0,
            "max_temp": 75.0,
            "kp": 3629,
            "ki": 133,
            "kd": 618,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_80c",
            "min_temp": 75.0,
            "max_temp": 85.0,
            "kp": 2674,
            "ki": 133,
            "kd": 402,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_90c",
            "min_temp": 85.0,
            "max_temp": 95.0,
            "kp": 2651,
            "ki": 133,
            "kd": 397,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_100c",
            "min_temp": 95.0,
            "max_temp": 110.0,
            "kp": 2475,
            "ki": 133,
            "kd": 360,
            "max_output_duty": DEFAULT_MAX_OUTPUT_DUTY_PERCENT,
        },
        {
            "name": "pid_120c",
            "min_temp": 110.0,
            "max_temp": 130.0,
            "kp": 2620,
            "ki": 133,
            "kd": 390,
            "max_output_duty": 50,
        },
        {
            "name": "pid_140c",
            "min_temp": 130.0,
            "max_temp": 150.0,
            "kp": 2666,
            "ki": 133,
            "kd": 400,
            "max_output_duty": 50,
        },
        {
            "name": "pid_160c",
            "min_temp": 150.0,
            "max_temp": 170.0,
            "kp": 2942,
            "ki": 133,
            "kd": 460,
            "max_output_duty": 50,
        },
        {
            "name": "pid_180c",
            "min_temp": 170.0,
            "max_temp": 190.0,
            "kp": 3140,
            "ki": 133,
            "kd": 504,
            "max_output_duty": 50,
        },
        {
            "name": "pid_200c",
            "min_temp": 190.0,
            "max_temp": MAX_TARGET_TEMPERATURE,
            "kp": 3935,
            "ki": 133,
            "kd": 691,
            "max_output_duty": 50,
        }
    ],
}


def default_config():
    return copy.deepcopy(DEFAULT_CONFIG)


def _merge_with_defaults(config):
    merged = default_config()
    if isinstance(config, dict):
        if isinstance(config.get("last_port"), str):
            merged["last_port"] = config["last_port"]
        if isinstance(config.get("pid_profiles"), list) and config["pid_profiles"]:
            merged["pid_profiles"] = [_normalize_pid_profile(profile) for profile in config["pid_profiles"]]
    return merged


REQUIRED_PID_PROFILE_KEYS = ("min_temp", "max_temp", "kp", "ki", "kd")


def _normalize_pid_profile(profile):
    normalized = dict(profile)
    # Validate required keys up front so a malformed profile fails fast with a
    # clear, specific error instead of an opaque KeyError later during PID
    # selection or heating (safety-relevant: heating must not start on a bad
    # profile). Only max_output_duty is allowed to be backfilled.
    profile_name = normalized.get("name", "<未命名>")
    for key in REQUIRED_PID_PROFILE_KEYS:
        if key not in normalized:
            raise ValueError(
                f"PID 参数表 '{profile_name}' 缺少必需字段 '{key}'"
            )
    if "max_output_duty" not in normalized:
        normalized["max_output_duty"] = DEFAULT_MAX_OUTPUT_DUTY_PERCENT
    return normalized


def load_config(path=CONFIG_PATH):
    path = Path(path)
    if not path.exists():
        return default_config()
    try:
        return _merge_with_defaults(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return default_config()


def save_config(config, path=CONFIG_PATH):
    # Crash-safe write (temp + fsync + os.replace), matching the position/calibration
    # stores. A torn rd105_config.json would otherwise hold corrupt PID / max-duty
    # caps; atomic replace guarantees readers see either the old or the new file whole.
    from .json_store import atomic_write_json
    atomic_write_json(Path(path), _merge_with_defaults(config))
