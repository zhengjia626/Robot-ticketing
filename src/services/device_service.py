"""设备状态采集与监控（方案 9.3 第三种方式 / 9.7 设备日志）

现场设备：门票打印机 ZEBRA ZT211、身份证读卡器、支付二维码扫码器（得力 AA501）、
定制键盘、票据到位传感器（USB 转串口）、声光报警灯（USB 转串口）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("host.device")

# 关键外设：任一不可用则不可创建会话（9.8 节会话前快速自检）
CRITICAL_DEVICES = ["printer", "id_reader", "scanner", "keypad"]


class DeviceService:
    """外设在位与就绪状态。

    现场落地替换点：
    - printer：Windows 打印机状态可用 win32print 轮询，或由票务系统推送；
    - id_reader / scanner：USB HID，通过厂商 SDK 或串口心跳判定；
    - ticket_sensor：USB 转串口干接点信号（pyserial）；
    当前实现以「可注入状态」方式提供，便于在真实驱动接入前完成全链路联调。
    """

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._status: Dict[str, bool] = {
            "printer": True,
            "id_reader": True,
            "scanner": True,
            "keypad": True,
            "ticket_sensor": True,
            "alarm_light": True,
        }
        self._detail: Dict[str, Dict] = {
            k: {"online": v, "last_seen": time.time()} for k, v in self._status.items()
        }
        self.paper_remaining: int = int(settings.get("devices.printer.roll_capacity", 500))
        self._low_threshold = int(settings.get("devices.printer.paper_low_threshold", 5))
        self._sensor_serial = None

    # ---------- 查询 ----------
    def status(self) -> Dict[str, bool]:
        with self._lock:
            return dict(self._status)

    def detail(self) -> Dict[str, Dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._detail.items()}

    def critical_ready(self) -> bool:
        with self._lock:
            return all(self._status.get(d, False) for d in CRITICAL_DEVICES)

    def paper_low(self) -> bool:
        with self._lock:
            return self.paper_remaining <= self._low_threshold

    def consume_paper(self, n: int = 1):
        with self._lock:
            self.paper_remaining = max(0, self.paper_remaining - n)

    def refill_paper(self, capacity: Optional[int] = None):
        with self._lock:
            cap = capacity or int(self.settings.get("devices.printer.roll_capacity", 500))
            self.paper_remaining = cap

    # ---------- 状态注入（联调 / 驱动接入） ----------
    def set_status(self, name: str, online: bool, note: str = ""):
        with self._lock:
            if name not in self._status:
                raise KeyError(f"未知设备 {name}")
            changed = self._status[name] != online
            self._status[name] = online
            self._detail[name] = {"online": online, "last_seen": time.time(), "note": note}
            if changed:
                logger.warning("设备 %s 状态变更 → %s %s", name, online, note)

    def heartbeat(self, name: str):
        with self._lock:
            if name in self._detail:
                self._detail[name]["last_seen"] = time.time()
                self._status[name] = True

    # ---------- 到位传感器（串口） ----------
    def open_sensor(self) -> bool:
        port = self.settings.get("devices.ticket_sensor.serial_port")
        if not port or not self.settings.get("devices.ticket_sensor.enabled", True):
            return False
        try:
            import serial  # type: ignore
            self._sensor_serial = serial.Serial(
                port,
                int(self.settings.get("devices.ticket_sensor.baudrate", 9600)),
                timeout=1,
            )
            logger.info("票据到位传感器已接入 %s", port)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("票据到位传感器串口打开失败（降级为机器人视觉判定）：%s", e)
            self._sensor_serial = None
            return False

    def read_sensor(self) -> Optional[bool]:
        """读取到位信号：True=票据已落托。未接入时返回 None（交由视觉判定）"""
        if not self._sensor_serial:
            return None
        try:
            line = self._sensor_serial.readline().decode("ascii", "ignore").strip()
            if line in ("1", "ON", "ARRIVED"):
                return True
            if line in ("0", "OFF", "EMPTY"):
                return False
        except Exception as e:  # noqa: BLE001
            logger.warning("读取到位传感器失败：%s", e)
        return None
