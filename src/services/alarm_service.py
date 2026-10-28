"""声光报警驱动（方案 9.7 节 / 9.9 /api/alarm/trigger）

触发场景：打印机不出票、门票纸将用尽、机器人故障、网络中断。
要求：报警可人工取消，取消动作记入日志；同一异常重复触发提高告警等级。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger("host.alarm")


class AlarmService:
    LEVEL_ORDER = {"L3": 0, "L2": 1, "L1": 2}

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._active: Optional[Dict] = None
        self._history: List[Dict] = []
        self._trigger_count: Dict[str, int] = {}
        self._serial = None

    # ---------- 触发 ----------
    def trigger(self, code: str, level: str = "L2", message: str = "",
                session_id: Optional[str] = None) -> Dict:
        with self._lock:
            n = self._trigger_count.get(code, 0) + 1
            self._trigger_count[code] = n
            # 同一异常重复触发 → 提高告警等级（9.7 节）
            if n > 1 and level != "L1":
                level = self._escalate(level)
            rec = {
                "code": code,
                "level": level,
                "message": message,
                "session_id": session_id,
                "ts": time.time(),
                "repeat": n,
                "active": True,
                "cancelled_by": None,
                "cancelled_at": None,
            }
            self._active = rec
            self._history.append(rec)
            self._write_hw(True, level)
            logger.warning("声光报警触发 [%s][%s] %s（第 %d 次）", level, code, message, n)
            return dict(rec)

    def _escalate(self, level: str) -> str:
        cur = self.LEVEL_ORDER.get(level, 1)
        for k, v in self.LEVEL_ORDER.items():
            if v == cur + 1:
                return k
        return "L1"

    # ---------- 人工取消 ----------
    def cancel(self, operator: str = "staff") -> Optional[Dict]:
        with self._lock:
            if not self._active:
                return None
            rec = self._active
            rec["active"] = False
            rec["cancelled_by"] = operator
            rec["cancelled_at"] = time.time()
            self._active = None
            self._write_hw(False)
            logger.warning("声光报警被人工取消：%s by %s", rec["code"], operator)
            return dict(rec)

    # ---------- 查询 ----------
    def active(self) -> Optional[Dict]:
        with self._lock:
            return dict(self._active) if self._active else None

    def history(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return [dict(h) for h in self._history[-limit:]]

    # ---------- 硬件写入 ----------
    def _write_hw(self, on: bool, level: str = "L2"):
        """通过 USB 转串口写入声光报警灯。未接入时仅记录日志，不影响主流程。"""
        if self._serial is None:
            self._open()
        if self._serial is None:
            logger.info("[声光报警灯未接入/模拟] on=%s level=%s", on, level)
            return
        try:
            cmd = f"{'ON' if on else 'OFF'},{level}\r\n".encode("ascii")
            self._serial.write(cmd)
        except Exception as e:  # noqa: BLE001
            logger.warning("声光报警灯写入失败：%s", e)

    def _open(self):
        port = self.settings.get("devices.alarm_light.serial_port")
        if not port or not self.settings.get("devices.alarm_light.enabled", True):
            return
        try:
            import serial  # type: ignore
            self._serial = serial.Serial(
                port,
                int(self.settings.get("devices.alarm_light.baudrate", 9600)),
                timeout=1,
            )
            logger.info("声光报警灯已接入 %s", port)
        except Exception as e:  # noqa: BLE001
            logger.warning("声光报警灯串口打开失败（模拟运行）：%s", e)
            self._serial = None
