"""全局配置：阈值、路径与角色。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FOUNTAIN_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "fountain.db"
ATTACH_DIR = DATA_DIR / "attachments"

# ---- 水位阈值（米，相对池底） ----
WATER_LEVEL_MIN = 0.80
WATER_LEVEL_MAX = 1.60

# ---- 水质阈值 ----
TURBIDITY_MAX = 5.0          # NTU
CHLORINE_MIN = 0.3           # mg/L 余氯下限
CHLORINE_MAX = 1.0           # mg/L 余氯上限
CHEMICAL_DOSE_MAX = 12.0     # 单次投加上限（升/当量），超过即“药剂超量”
OBSERVATION_MINUTES = 30     # 投药后观察期（分钟），期内禁止启动

# ---- 设备阈值 ----
PUMP_TEMP_MAX = 75.0         # 泵体温度 ℃，超过判为过热

# ---- 天气 ----
WIND_STOP_LEVEL = 6          # 风力等级 >= 该值停运
WEATHER_STOP_CODES = {"storm", "typhoon", "heavy_rain"}

# ---- 角色 ----
ROLE_KEEPER = "keeper"   # 养护员：处理任务
ROLE_SUPERVISOR = "supervisor"  # 主管：批准越限复开

# ---- 调度 ----
TICK_SECONDS = 10        # 进程内调度 tick 间隔
RETEST_REMIND_MINUTES = 15   # 维修/清池结束后复测提醒间隔


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
