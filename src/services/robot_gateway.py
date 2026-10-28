"""机器人适配网关（方案 9.6 节）

边界（依据 9.6 节批注）：
- 动作编排由机器人本体维护，后台服务**不下发动作指令**；
- 后台只把「票务系统状态」转发给机器人，机器人据此自行编排按键与取递动作；
- 后台从机器人侧读取：异常与告警、电量、出票视觉识别结果（/api/print/verify）。
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("host.robot")


class RobotGateway:
    def __init__(self, settings):
        self.settings = settings
        self.base_url = settings.get("robot.base_url", "http://192.168.10.20:8000")
        self.push_enabled = bool(settings.get("robot.push_enabled", True))
        self.forward_events = list(settings.get("robot.forward_events", []) or [])
        self.heartbeat_timeout = int(settings.get("robot.heartbeat_timeout_sec", 15))
        # 联调态：机器人尚未接入心跳通道时不因「从未收到心跳」阻断售票；
        # 一旦收到过心跳，即按超时严格判定（9.8 节会话前快速自检）。
        self.require_heartbeat = bool(settings.get("robot.require_heartbeat", False))
        self._lock = threading.RLock()
        self._last_heartbeat: float = time.time()
        self._heartbeat_received = False
        self._battery: Optional[int] = None
        self._alerts: list[Dict[str, Any]] = []
        self._forward_log: list[Dict[str, Any]] = []

    # ---------- 机器人侧状态 ----------
    def heartbeat(self, payload: Dict[str, Any]):
        with self._lock:
            self._last_heartbeat = time.time()
            self._heartbeat_received = True
            self._battery = payload.get("battery", self._battery)
        logger.debug("机器人心跳 %s", payload)

    @property
    def heartbeat_received(self) -> bool:
        with self._lock:
            return self._heartbeat_received

    @property
    def online(self) -> bool:
        with self._lock:
            if not self._heartbeat_received:
                return not self.require_heartbeat
            return (time.time() - self._last_heartbeat) <= self.heartbeat_timeout

    @property
    def battery(self) -> Optional[int]:
        with self._lock:
            return self._battery

    @property
    def last_seen(self) -> float:
        with self._lock:
            return self._last_heartbeat

    # ---------- 告警 ----------
    def push_alert(self, event: str, level: str, detail: Optional[Dict] = None):
        rec = {"ts": time.time(), "event": event, "level": level, "detail": detail or {}}
        with self._lock:
            self._alerts.append(rec)
            self._alerts = self._alerts[-100:]
        logger.warning("机器人告警 %s[%s] %s", event, level, detail)

    def alerts(self, limit: int = 20):
        with self._lock:
            return list(self._alerts[-limit:])

    def status(self) -> Dict[str, Any]:
        return {
            "online": self.online,
            "heartbeat_received": self.heartbeat_received,
            "require_heartbeat": self.require_heartbeat,
            "battery": self.battery,
            "last_seen": self.last_seen,
            "base_url": self.base_url,
            "push_enabled": self.push_enabled,
        }

    # ---------- 转发（后台 → 机器人） ----------
    def forward(self, kind: str, payload: Dict[str, Any]) -> bool:
        """转发票务状态给机器人。

        失败只记录，不阻断主流程（旁路协同原则）。转发在后台线程执行，
        避免同步网络调用阻塞主事件循环——即便机器人不可达也不影响票务推送时延。
        """
        if not self.push_enabled or kind not in self.forward_events:
            return False
        rec = {"ts": time.time(), "kind": kind, "payload": payload, "ok": False}

        def _do():
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/api/host/forward",
                    data=_json_bytes({"kind": kind, "payload": payload}),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    rec["ok"] = 200 <= resp.status < 300
            except Exception as e:  # noqa: BLE001
                rec["error"] = str(e)
                logger.info("转发机器人失败（旁路不阻断）：%s", e)
            with self._lock:
                self._forward_log.append(rec)
                self._forward_log = self._forward_log[-100:]

        threading.Thread(target=_do, daemon=True).start()
        return True

    def forward_log(self, limit: int = 20):
        with self._lock:
            return list(self._forward_log[-limit:])


def _json_bytes(obj: Any) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
