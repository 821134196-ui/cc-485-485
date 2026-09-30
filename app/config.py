"""全局路径与业务常量。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "fountain.db"
ATTACH_DIR = DATA_DIR / "attachments"
STATIC_DIR = BASE_DIR / "app" / "static"

# ---- 启动前置条件阈值 ----
WATER_LEVEL_MIN = 0.60          # 水位（占池深比例）下限
WATER_LEVEL_MAX = 0.95
TURBIDITY_MAX = 5.0             # NTU
CHLORINE_MIN = 0.3              # mg/L 余氯
CHLORINE_MAX = 1.0
PUMP_TEMP_MAX = 75.0            # ℃ 泵体温度
NOZZLE_PRESSURE_MIN = 0.18      # MPa 喷头组压力，低于判堵塞

# ---- 业务时序 ----
DOSING_OBSERVE_MINUTES = 30     # 投药后观察期
REOPEN_RETEST_HOURS = 24        # 复开水质复测有效时限

# ---- 角色 ----
ROLE_KEEPER = "keeper"          # 养护员：处理任务
ROLE_SUPERVISOR = "supervisor"  # 主管：批准越限复开

# ---- 颜色（水泥白 / 喷泉蓝 / 植被绿）----
COLORS = {
    "concrete": "#F2F1ED",
    "concrete_deep": "#E2E0D8",
    "fountain_blue": "#2E86C1",
    "fountain_blue_deep": "#1B5E8C",
    "vegetation_green": "#4C9A57",
    "alarm_red": "#C0392B",
    "amber": "#D98E04",
    "ink": "#33383D",
}
