"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import CleanMode, FanLevel

DOMAIN = "narwal"
DEFAULT_PORT = 9002

MANUFACTURER = "Narwal"
MODEL = "Flow (AX12)"

# Model selector for config flow.
# Keys are user-facing labels; values are product key prefixes.
# "auto" cycles all known keys during discovery (slower, fallback).
NARWAL_MODELS: dict[str, str] = {
    "Narwal Flow": "QoEsI5qYXO",
    "Narwal Flow 2": "QxMSPG6VSO",
    "Narwal Freo Z10 Ultra": "DrzDKQ0MU8",
    "Narwal Freo X10 Pro": "CNbforyZWI",
    "Other / Auto-detect": "auto",
}

CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"

# Options
CONF_MAP_SCALE = "map_scale"
DEFAULT_MAP_SCALE = 4
MIN_MAP_SCALE = 2
MAX_MAP_SCALE = 6

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.CAMERA,
]

FAN_SPEED_MAP: dict[str, FanLevel] = {
    "quiet": FanLevel.QUIET,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.MAX,
}

FAN_SPEED_LIST: list[str] = list(FAN_SPEED_MAP.keys())

CLEAN_MODE_MAP: dict[str, CleanMode] = {
    "sweep": CleanMode.SWEEP,
    "mop": CleanMode.MOP,
    "sweep_mop": CleanMode.SWEEP_MOP,
    "sweep_then_mop": CleanMode.SWEEP_THEN_MOP,
}

CLEAN_MODE_LIST: list[str] = list(CLEAN_MODE_MAP.keys())

DEFAULT_CLEAN_MODE = "sweep_mop"

# Known fault codes reported by the robot, mapped to translation slugs
# (entity.sensor.error.state.<slug> in strings.json / translations/).
# Source: Narwal's official help center (help.narwal.com/helpcenter/vall,
# lang packs key articles by the zero-padded hex of this 32-bit code; the
# 4-digit number in the comment is the code the Narwal app shows).
# Structure: byte1 = actor (0x01 station, 0x02 robot), byte2 = subsystem
# (0x01 water, 0x02 mechanics, 0x03 power, 0x10 nav, 0x11 robot-station,
# 0x13 zones, 0x31 sensors). Unknown codes fall through to the raw number.
ERROR_CODE_SLUGS: dict[int, str] = {
    0x01010013: "base_station_overflow",  # 2003
    0x01010032: "cleaning_tray_not_in_place",  # 2004
    0x01010034: "clean_water_tank_not_in_place",  # 2005
    0x01010035: "clean_water_tank_error",  # 2006 water outlet error
    0x01010036: "dirty_water_tank_full_or_missing",  # 2007
    0x01010051: "water_exchange_clean_tank_error",  # 3000
    0x01010052: "water_exchange_module_error",  # 3001
    0x01010053: "base_station_overflow",  # 3002
    0x01010059: "water_exchange_module_not_installed",  # 3010
    0x01010060: "water_control_module_not_detected",  # 3004
    0x01010061: "water_spray_error",  # 3005
    0x01010063: "dirty_water_pipe_error",  # 3007-3008
    0x01010137: "clean_water_tank_empty",  # live-observed (StratoGh0st99)
    0x02020011: "roller_brush_lifting_error",  # 1007-1008
    0x02020013: "mop_lifting_error",  # 1009-1010
    0x02020015: "mop_motor_overcurrent",  # 1122
    0x02020028: "side_brush_error",  # 1041-1043
    0x02020031: "roller_brush_entangled",  # 1044
    0x02020032: "dust_bin_not_in_place",  # 1045
    0x02020040: "mopping_module_error",  # 1036-1038
    0x02020050: "mopping_module_error",  # 1030-1032
    0x02020060: "mopping_module_error",  # 1033-1035
    0x02030040: "battery_temp_high",  # 1005
    0x02030041: "battery_temp_low",  # 1006
    0x02030050: "charging_pads_error",  # 1020-1021
    0x02030051: "charging_error",  # 1130-1131
    0x02100015: "return_to_base_failed",  # 1127
    0x02100017: "base_station_not_found",  # 1022
    0x02100018: "base_station_not_found",  # 1023
    0x02100019: "repositioning_failed",  # 1011
    0x02100021: "robot_trapped",  # 1012, 1123-1126
    0x02100051: "exit_base_failed",  # 1024-1025
    0x02100070: "exit_base_failed",  # 1128
    0x02110020: "base_connection_failed",  # 1026
    0x02110030: "mop_washing_interrupted",  # 1028
    0x02110040: "mop_drying_interrupted",  # 1039 (live: left dock >5 min)
    0x02130020: "entered_no_go_zone",  # 1013
    0x02310025: "wheel_malfunction",  # 1014-1016
    0x02310031: "robot_lifted",  # 1017 (live: stuck on a doormat)
    0x02310100: "lidar_speed_error",  # 1047
    0x02310101: "lidar_cover_error",  # 1048
    0x02310110: "imu_error",  # 1065-1066
    0x02310118: "front_infrared_error",  # 1073-1080
    0x02310120: "front_ground_sensor_error",  # 1081
    0x02310128: "cliff_sensor_error",  # 1089-1092
    0x02310130: "wall_sensor_error",  # 1097 wall PSD
    0x02310138: "ultrasonic_sensor_error",  # 1105 ground material
    0x02310140: "bumper_error",  # 1113
}

# Deep link to the help-center article for a fault; {code} is the
# zero-padded 8-digit hex of the fault code (articles are keyed by it).
ERROR_HELP_URL_TEMPLATE = (
    "https://help.narwal.com/helpcenter/vall/#/p2/question/all"
    "?eType=1&code={code}&lang=en-US"
)
