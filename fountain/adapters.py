"""模拟适配器：返回水位、水质与天气（演示数据）。

真实场景下这里对接 PLC / 仪表 / 气象站；平台所有读数只经此模块获取，
测试与样例命令通过 ``set_scenario`` 注入可复现场景。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Dict, Optional

from . import config

_LOCK = threading.Lock()

# pool_id -> 场景覆盖 {"level":.., "turbidity":.., "chlorine":.., "temp":..,
#                     "clogged": bool, "weather": code, "wind": int}
_SCENARIOS: Dict[int, dict] = {}

# 全局天气场景 {"code": str, "temp_c": float, "wind_level": int}
_WEATHER: Optional[dict] = None


def set_scenario(pool_id: int, **values) -> None:
    with _LOCK:
        _SCENARIOS[pool_id] = dict(_SCENARIOS.get(pool_id, {}), **values)


def clear_scenarios() -> None:
    with _LOCK:
        _SCENARIOS.clear()


def set_weather(code: str = "clear", temp_c: float = 24.0, wind_level: int = 2) -> None:
    global _WEATHER
    with _LOCK:
        _WEATHER = {"code": code, "temp_c": temp_c, "wind_level": wind_level}


def get_weather(now: Optional[datetime] = None) -> dict:
    """返回天气；未注入时按时间生成平稳的演示天气（晴，2 级风）。"""
    with _LOCK:
        if _WEATHER is not None:
            return dict(_WEATHER)
    now = now or datetime.now()
    # 确定性轻微波动，纯装饰
    wave = math.sin(now.hour / 24.0 * 2 * math.pi)
    return {"code": "clear", "temp_c": round(23 + 3 * wave, 1), "wind_level": 2}


def read_telemetry(pool_id: int, design_level: float = 1.20,
                   now: Optional[datetime] = None) -> dict:
    """读取某水池当前水位/水质/泵温/喷头状态。"""
    now = now or datetime.now()
    phase = math.sin(now.timestamp() / 900.0 + pool_id)  # 慢周期波动
    base = {
        "water_level": round(design_level + 0.03 * phase, 3),
        "turbidity": round(2.4 + 0.5 * phase, 2),
        "chlorine": round(0.62 + 0.08 * phase, 2),
        "pump_temp": round(46 + 2 * phase, 1),
        "clogged": False,
    }
    with _LOCK:
        override = dict(_SCENARIOS.get(pool_id, {}))
    base.update({k: v for k, v in override.items()
                 if k in base and v is not None})
    base["weather"] = get_weather(now)
    base["ts"] = now
    return base


def weather_blocks(weather: dict) -> Optional[str]:
    """天气是否要求停运，返回原因文本；否则 None。"""
    if weather["code"] in config.WEATHER_STOP_CODES:
        names = {"storm": "雷暴", "typhoon": "台风", "heavy_rain": "暴雨"}
        return f"天气停运：{names.get(weather['code'], weather['code'])}"
    if weather["wind_level"] >= config.WIND_STOP_LEVEL:
        return f"天气停运：风力 {weather['wind_level']} 级 ≥ {config.WIND_STOP_LEVEL} 级"
    return None
