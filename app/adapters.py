"""模拟适配器：返回水位、水质、泵体/喷头遥测与天气。

所有数据均为演示数据。可通过 set_scenario 注入异常工况，
供调度器与演示命令使用；调用 pop_events 取出边沿事件。
"""
import math
import threading
from datetime import datetime
from typing import Dict, List, Optional

from . import config

# 可注入的异常场景
SCENARIOS = [
    "normal", "level_low", "quality_over", "nozzle_block",
    "overheat", "weather", "overdose", "intrusion",
]

WEATHER_OK = {"code": "clear", "text": "晴到多云", "wind_ms": 2.1, "rain_mm": 0.0, "allow_run": True}
WEATHER_BAD = {"code": "storm", "text": "雷暴大风预警", "wind_ms": 12.4, "rain_mm": 18.6, "allow_run": False}

_CHEMICAL_CN = {"chlorine": "氯片", "flocculant": "絮凝剂", "algaecide": "除藻剂"}


class SimulatedAdapter:
    """进程内单例模拟源，按水池保存场景与最近读数。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._scenario: Dict[int, str] = {}
        self._weather_override: Optional[dict] = None
        self._intrusion_zones = set()
        # 泵体温度（℃）、喷头压力（MPa）
        self._pump_temp: Dict[int, float] = {}
        self._nozzle_pressure: Dict[int, float] = {}
        self._tick = 0

    # ---------- 场景注入 ----------
    def set_scenario(self, pool_id: int, scenario: str):
        with self._lock:
            assert scenario in SCENARIOS
            self._scenario[pool_id] = scenario

    def set_weather(self, bad: bool):
        with self._lock:
            self._weather_override = WEATHER_BAD if bad else WEATHER_OK

    def set_intrusion(self, zone_id: int, inside: bool):
        with self._lock:
            if inside:
                self._intrusion_zones.add(zone_id)
            else:
                self._intrusion_zones.discard(zone_id)

    # ---------- 遥测读取 ----------
    def read_water(self, pool_id: int) -> dict:
        """返回当前模拟水位与水质。normal 时在阈值内小幅波动。"""
        with self._lock:
            sc = self._scenario.get(pool_id, "normal")
            self._tick += 1
            wave = math.sin(self._tick / 4.0 + pool_id) * 0.01
            if sc == "level_low":
                level, turb, chl = config.WATER_LEVEL_MIN - 0.12, 3.2, 0.55
            elif sc == "quality_over":
                # 浊度与余氯同时越上限
                level, turb, chl = 0.78, config.TURBIDITY_MAX + 3.4, config.CHLORINE_MAX + 0.35
            else:
                level = round(0.74 + wave, 3)
                turb = round(2.6 + abs(wave) * 8, 2)
                chl = round(0.55 + wave, 3)
            return {"level": level, "turbidity": turb, "chlorine": chl}

    def read_pump(self, pump_id: int, running: bool) -> dict:
        with self._lock:
            base = self._pump_temp.get(pump_id, 46.0)
            # 场景按泵所属水池判断（由调用方保证 overheat 注入）
            if self._pump_temp.get("__force_overheat__") == pump_id:
                temp = config.PUMP_TEMP_MAX + 6.0
            elif running:
                temp = min(base + 0.4, 62.0)
            else:
                temp = max(base - 0.3, 42.0)
            self._pump_temp[pump_id] = temp
            return {"temp_c": round(temp, 1)}

    def force_pump_overheat(self, pump_id: int):
        with self._lock:
            self._pump_temp["__force_overheat__"] = pump_id
            self._pump_temp[pump_id] = config.PUMP_TEMP_MAX + 6.0

    def clear_faults(self, pump_id: int):
        with self._lock:
            self._pump_temp[pump_id] = 46.0
            if self._pump_temp.get("__force_overheat__") == pump_id:
                self._pump_temp.pop("__force_overheat__", None)

    def read_nozzle_group(self, group_id: int) -> dict:
        with self._lock:
            blocked = self._nozzle_pressure.get(("__blocked__", group_id), False)
            if blocked:
                pressure = config.NOZZLE_PRESSURE_MIN - 0.07
            else:
                pressure = 0.26 + math.sin(self._tick / 3.0 + group_id) * 0.01
            return {"pressure_mpa": round(pressure, 3), "blocked": bool(blocked)}

    def force_nozzle_block(self, group_id: int, blocked: bool = True):
        with self._lock:
            self._nozzle_pressure[("__blocked__", group_id)] = blocked

    def weather(self) -> dict:
        with self._lock:
            if self._weather_override is not None:
                return dict(self._weather_override)
            return dict(WEATHER_OK)

    def intrusion(self, zone_id: int) -> bool:
        with self._lock:
            return zone_id in self._intrusion_zones

    def scenario(self, pool_id: int) -> str:
        with self._lock:
            return self._scenario.get(pool_id, "normal")

    def reset(self):
        """恢复全部模拟源到正常态（测试/演示用）。"""
        with self._lock:
            self._scenario.clear()
            self._weather_override = None
            self._intrusion_zones.clear()
            self._pump_temp.clear()
            self._nozzle_pressure.clear()
            self._tick = 0


def chemical_cn(code: str) -> str:
    return _CHEMICAL_CN.get(code, code)


# 进程内单例
adapter = SimulatedAdapter()
