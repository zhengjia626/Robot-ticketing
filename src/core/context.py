"""主机后台服务运行时上下文

采用「旁路协同」装配（方案 9.1 节）：后台服务不接管票务业务，
只在票务系统、机器人、显示终端、外设之间建立可控的状态通道。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .config import settings
from .session import EventBus, SessionManager
from ..services.alarm_service import AlarmService
from ..services.device_service import DeviceService
from ..services.display_service import DisplayService
from ..services.robot_gateway import RobotGateway
from ..services.selfcheck import SelfCheck
from ..services.ticketing_bridge import TicketingBridge
from ..services.verification import VerificationService

logger = logging.getLogger("host.ctx")


class DisplayRegistry:
    """显示终端在线状态（9.5 节：每 10s 心跳，连续 3 次丢失判离线）"""

    def __init__(self, interval: int, miss_threshold: int):
        self._lock = threading.Lock()
        self._interval = interval
        self._miss = miss_threshold
        self._last: Dict[str, float] = {}

    def heartbeat(self, terminal_id: str):
        with self._lock:
            self._last[terminal_id] = time.time()

    def online(self, terminal_id: Optional[str] = None) -> bool:
        with self._lock:
            if not self._last:
                return False
            if terminal_id:
                ts = self._last.get(terminal_id)
                return bool(ts and (time.time() - ts) <= self._interval * self._miss)
            newest = max(self._last.values())
            return (time.time() - newest) <= self._interval * self._miss

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            now = time.time()
            return [
                {"terminal_id": k,
                 "last_seen": v,
                 "age_sec": round(now - v, 1),
                 "online": (now - v) <= self._interval * self._miss}
                for k, v in self._last.items()
            ]


class HostContext:
    _instance: Optional["HostContext"] = None

    def __init__(self):
        from .config import rules
        self.settings = settings
        self.rules = rules

        self.devices = DeviceService(settings)
        self.alarm = AlarmService(settings)
        self.robot = RobotGateway(settings)
        self.displays = DisplayRegistry(
            int(settings.get("display.heartbeat_interval_sec", 10)),
            int(settings.get("display.heartbeat_miss_threshold", 3)),
        )
        self.sessions = SessionManager(rules, self.devices.status)
        self.bus = EventBus()
        self.display_svc = DisplayService(rules, settings)
        self.verification = VerificationService(settings)
        self.bridge = TicketingBridge(self.sessions, self.bus, self.display_svc,
                                      self.robot, self.alarm, self.devices,
                                      rules, settings, self.verification)
        self.selfcheck = SelfCheck(settings, self.devices, self.robot,
                                   lambda: self.displays.online())

        self.started_at = time.time()
        self.selfcheck_report: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()

    # ---------- 启动 ----------
    def bootstrap(self):
        self.devices.open_sensor()
        report = self.selfcheck.run(full=True)
        self.selfcheck_report = report
        if not report["sellable"]:
            logger.error("启动自检未通过，拒绝进入售票模式：%s",
                         [i["item"] for i in report["items"] if not i["ok"]])
        restored = self.sessions.restore_unfinished()
        if restored:
            logger.warning("恢复未完成会话：%s", restored)
        # 初次推送待机页
        snap = self.display_svc.build(None)
        self.bus.publish(snap)
        self._start_sweeper()
        return report

    def _start_sweeper(self):
        """超时清扫线程（4.10 节：超时关闭会话并释放库存）"""

        def loop():
            while not self._stop.wait(5):
                try:
                    expired = self.sessions.sweep_timeouts()
                    for e in expired:
                        s = self.sessions.get(e["session_id"])
                        if s:
                            self.bridge._publish(
                                s, {"message": "本次购票已超时取消，欢迎重新开始"})
                            self.sessions.close(s.session_id,
                                                f"超时 {e['elapsed']}s 未推进，自动关闭")
                except Exception as ex:  # noqa: BLE001
                    logger.exception("超时清扫异常：%s", ex)

        t = threading.Thread(target=loop, name="timeout-sweeper", daemon=True)
        t.start()

    def shutdown(self):
        self._stop.set()

    # ---------- 运维指标 ----------
    def metrics(self) -> Dict[str, Any]:
        sessions = self.sessions.all()
        by_state: Dict[str, int] = {}
        for s in sessions:
            by_state[s.state] = by_state.get(s.state, 0) + 1
        durations = []
        for s in sessions:
            if s.closed_at:
                durations.append((s.closed_at - s.created_at).total_seconds())
        done = [s for s in sessions if s.state == "S11"]
        return {
            "uptime_sec": round(time.time() - self.started_at, 1),
            "service": self.settings.get("service.name"),
            "rules_version": self.rules.version,
            "session_total": len(sessions),
            "session_by_state": by_state,
            "session_completed": len(done),
            "avg_session_sec": round(sum(durations) / len(durations), 1) if durations else 0,
            "devices": self.devices.status(),
            "paper_remaining": self.devices.paper_remaining,
            "robot": self.robot.status(),
            "display_online": self.displays.online(),
            "alarm_active": self.alarm.active(),
            "push_received": len(self.bridge.push_log),
            "selfcheck": {
                "sellable": bool(self.selfcheck_report and self.selfcheck_report["sellable"]),
                "conclusion": (self.selfcheck_report or {}).get("conclusion"),
            },
        }


def get_context() -> HostContext:
    if HostContext._instance is None:
        HostContext._instance = HostContext()
    return HostContext._instance
