"""会话管理（方案 9.4 节 状态联动引擎的宿主）

会话是后台服务唯一的业务状态持有者（4.8 节第一条：机器人只执行动作并上报结果）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import BASE_DIR
from .models import SessionState, StateSnapshot
from .state_machine import StateMachine

logger = logging.getLogger("host.session")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _now() -> datetime:
    return datetime.now()


def mask_id(card_no: str) -> str:
    """身份证脱敏：仅保留前 6 位与后 2 位（9.7 节合规要求）"""
    if not card_no:
        return ""
    if len(card_no) <= 8:
        return card_no[:2] + "*" * max(len(card_no) - 2, 0)
    return card_no[:6] + "*" * (len(card_no) - 8) + card_no[-2:]


class Session:
    """一笔售票会话的上下文（同时作为门禁判定的输入）"""

    def __init__(self, session_id: str, rules, device_status: Dict[str, bool]):
        self.session_id = session_id
        self.state: str = SessionState.S0.value
        self.created_at = _now()
        self.updated_at = _now()
        self.closed_at: Optional[datetime] = None
        self.close_reason: Optional[str] = None

        self.rules = rules
        # ---- 门禁判定所需字段 ----
        self.devices_ready: bool = all(device_status.values()) if device_status else False
        self.device_status: Dict[str, bool] = device_status or {}

        self.session_code: Optional[str] = None      # day / night
        self.items: List[Dict[str, Any]] = []        # [{ticket_type,name,qty,unit_price,amount}]
        self.remaining: Optional[int] = None

        self.id_locked: bool = False
        self.id_count: Optional[int] = None
        self.id_masked: Optional[str] = None
        self.age: Optional[int] = None
        self.eligible_type: Optional[str] = None
        self.age_mismatch_pending: bool = False

        self.order_id: Optional[str] = None
        self.total_amount: Optional[float] = None
        self.confirm_key_pressed: bool = False
        self.speech_matches_display: bool = True

        self.trade_no: Optional[str] = None
        self.pay_status: Optional[str] = None

        self.print_issued: bool = False
        self.ticket_arrived: bool = False
        self.actual_qty: int = 0
        self.paper_empty: bool = False
        self.paper_remaining: Optional[int] = None

        self.retry_counters: Dict[str, int] = {}
        self.history: List[Dict[str, Any]] = []
        self.idempotency_keys: set = set()
        self.timeline: List[Dict[str, Any]] = []     # 交互日志（系统层）
        self.alarm: Optional[Dict[str, Any]] = None

    # ---------- 派生属性 ----------
    @property
    def total_qty(self) -> int:
        return sum(int(i.get("qty", 0)) for i in self.items)

    @property
    def stage(self) -> Optional[str]:
        return StateMachine.stage_of(self.state)

    @property
    def page(self) -> str:
        return self.rules.page_for_state(self.state)

    # ---------- 幂等 ----------
    def idem(self, action: str, ref: str = "") -> bool:
        """以 session_id + 动作 + 引用（订单号/流水号/键序）作为幂等键。
        返回 True 表示首次生效；False 表示重复事件已拦截。"""
        key = f"{action}:{ref}"
        if key in self.idempotency_keys:
            logger.info("[%s] 幂等拦截重复事件 %s", self.session_id, key)
            return False
        self.idempotency_keys.add(key)
        return True

    def bump_retry(self, action: str) -> int:
        n = self.retry_counters.get(action, 0) + 1
        self.retry_counters[action] = n
        return n

    # ---------- 记录 ----------
    def append_history(self, src: str, event: str, dst: str):
        self.updated_at = _now()
        self.history.append({"ts": self.updated_at.isoformat(),
                             "from": src, "event": event, "to": dst})

    def log(self, kind: str, message: str, **extra):
        self.timeline.append({"ts": _now().isoformat(), "kind": kind,
                              "state": self.state, "message": message, **extra})
        logger.info("[%s][%s] %s", self.session_id, kind, message)

    # ---------- 序列化 ----------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "stage": self.stage,
            "page": self.page,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "session_code": self.session_code,
            "items": self.items,
            "total_qty": self.total_qty,
            "total_amount": self.total_amount,
            "remaining": self.remaining,
            "order_id": self.order_id,
            "trade_no": self.trade_no,
            "pay_status": self.pay_status,
            "id_locked": self.id_locked,
            "id_count": self.id_count,
            "id_masked": self.id_masked,
            "age": self.age,
            "actual_qty": self.actual_qty,
            "ticket_arrived": self.ticket_arrived,
            "paper_remaining": self.paper_remaining,
            "close_reason": self.close_reason,
        }


class SessionManager:
    """会话池：创建 / 查询 / 超时清扫 / 崩溃恢复"""

    def __init__(self, rules, device_probe):
        self._lock = threading.RLock()
        self._sessions: Dict[str, Session] = {}
        self._rules = rules
        self._device_probe = device_probe

    # ---------- 生命周期 ----------
    def create(self, session_id: Optional[str] = None) -> Session:
        with self._lock:
            active = self.active()
            if active:
                raise RuntimeError(f"已有进行中会话 {active.session_id}，禁止并发创建")
            sid = session_id or f"S{time.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"
            s = Session(sid, self._rules, self._device_probe())
            self._sessions[sid] = s
            s.log("session", "会话创建，触发设备快速自检", devices=s.device_status)
            return s

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def active(self) -> Optional[Session]:
        with self._lock:
            for s in self._sessions.values():
                if not StateMachine.is_terminal(s.state):
                    return s
            return None

    def close(self, session_id: str, reason: str):
        s = self.get(session_id)
        if not s:
            return
        s.closed_at = _now()
        s.close_reason = reason
        s.log("session", f"会话关闭：{reason}")
        self._persist(s)

    def all(self) -> List[Session]:
        with self._lock:
            return list(self._sessions.values())

    # ---------- 超时清扫 ----------
    def sweep_timeouts(self) -> List[Dict[str, Any]]:
        """按阶段时限清扫超时会话（4.10 节表 4-2）"""
        from .state_machine import GateRejected
        out = []
        with self._lock:
            for s in self._sessions.values():
                if StateMachine.is_terminal(s.state):
                    continue
                limit = self._limit_for_state(s)
                if limit is None:
                    continue
                elapsed = (datetime.now() - s.updated_at).total_seconds()
                if elapsed <= limit:
                    continue
                try:
                    StateMachine.transition(s, "timeout", force=True)
                    s.log("timeout", f"超过 {limit}s 未推进，会话超时关闭", elapsed=round(elapsed))
                    out.append({"session_id": s.session_id, "state": s.state,
                                "elapsed": round(elapsed), "limit": limit})
                except GateRejected as e:
                    s.log("timeout", f"超时迁移失败：{e}")
        return out

    def _limit_for_state(self, s: Session) -> Optional[float]:
        return {
            "S1": self._rules.timeout("P1_wake_wait_sec", 30),
            "S2": self._rules.timeout("session_total_max_sec", 120),
            "S3": self._rules.timeout("P4_order_confirm_wait_sec", 60),
            "S4": self._rules.timeout("P3_card_place_wait_sec", 90),
            "S5": self._rules.timeout("P4_order_confirm_wait_sec", 60),
            "S6": self._rules.timeout("P4_order_confirm_wait_sec", 60),
            "S7": self._rules.timeout("P5_pay_wait_sec", 90),
            "S8": self._rules.timeout("P6_print_wait_sec", 30),
            "S9": self._rules.timeout("P6_print_wait_sec", 30),
            "S10": self._rules.timeout("P7_pickup_wait_sec", 30),
        }.get(s.state)

    # ---------- 持久化（崩溃恢复） ----------
    def _persist(self, s: Session):
        try:
            path = DATA_DIR / f"{s.session_id}.json"
            path.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as e:
            logger.warning("会话持久化失败：%s", e)

    def restore_unfinished(self) -> List[str]:
        """服务启动时从磁盘恢复未完成会话（9.2 节：避免重启导致订单悬挂）"""
        restored = []
        for path in DATA_DIR.glob("S*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("close_reason"):
                continue
            sid = data.get("session_id")
            if not sid or sid in self._sessions:
                continue
            s = Session(sid, self._rules, {})
            s.state = data.get("state", "S0")
            s.order_id = data.get("order_id")
            s.total_amount = data.get("total_amount")
            s.trade_no = data.get("trade_no")
            s.log("restore", "服务重启后恢复未完成会话", restored_state=s.state)
            self._sessions[sid] = s
            restored.append(sid)
        return restored


class EventBus:
    """状态版本总线：为显示终端长轮询提供版本号自增与挂起唤醒（9.5 节）"""

    def __init__(self):
        self._lock = threading.Condition()
        self._version = 0
        self._snapshot: Optional[StateSnapshot] = None

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def publish(self, snapshot: StateSnapshot) -> int:
        with self._lock:
            self._version += 1
            snapshot.version = self._version
            self._snapshot = snapshot
            self._lock.notify_all()
            return self._version

    def current(self) -> Optional[StateSnapshot]:
        with self._lock:
            return self._snapshot

    def wait_for(self, since: int, timeout: float) -> Optional[StateSnapshot]:
        """挂起等待版本号超过 since；超时返回 None（终端收到 304 类空响应）"""
        deadline = time.time() + timeout
        with self._lock:
            if self._version > since:
                return self._snapshot
            remaining = deadline - time.time()
            while remaining > 0:
                self._lock.wait(remaining)
                if self._version > since:
                    return self._snapshot
                remaining = deadline - time.time()
            return None
