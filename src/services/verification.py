"""整体业务流程核查服务（模块二 · 独立于票务系统的把关服务）

设计依据（用户澄清的现场真实链路）：
- 「确认」不是票务系统键盘自己判定的，而是：游客把确认信息**说给人形机器人**
  → 机器人操作票务系统的**外接键盘**确认键 → 票务系统收到按键。
- 键盘只是票务系统的**外设**：票务系统收到确认键即视为「机器人已收到游客确认」，
  **不对「该不该确认」做门禁判断**；这个门禁由「整体业务流程核查服务」判定。

本服务持有确认门禁的判定权：
1. 票价必须已回传（屏上已显示金额）——未报价不允许确认；
2. 游客必须已向人形机器人确认（由「游客→机器人 信息输入」界面上报 intent=confirm）。

为兼容既有演示（一键演示 / Postman / 模拟器未走机器人消息界面），采用「宽松回退」：
本会话若**从未记录过游客消息**，则第 2 条不强制，避免旧链路全红。
一旦本会话使用过游客消息界面，即进入严格核查模式。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


class VerificationService:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        # session_id -> {"messages": [...], "visitor_confirmed": bool}
        self._store: Dict[str, Dict[str, Any]] = {}

    # ---------- 游客 → 机器人 消息 ----------
    def record_message(self, session_id: str, text: str,
                       intent: Optional[str] = None) -> Dict[str, Any]:
        """记录游客对人形机器人说的内容；intent=confirm 置位「游客已确认」。

        intent 取值（可选）：confirm / cancel / query / other
        """
        with self._lock:
            rec = self._store.setdefault(session_id, {"messages": [], "visitor_confirmed": False})
            entry = {"ts": time.time(), "text": text or "", "intent": intent or "other"}
            rec["messages"].append(entry)
            rec["messages"] = rec["messages"][-50:]
            if intent == "confirm":
                rec["visitor_confirmed"] = True
        return self.status(session_id)

    # ---------- 确认门禁判定（核心） ----------
    def verify_confirm(self, session) -> Tuple[bool, str]:
        """判定当前是否允许「确认」推进。返回 (ok, reason)。

        判定权归本服务：票务系统键盘只负责转发按键，不判定此门禁。
        """
        # ① 票价必须已回传（屏上已显示金额）
        if getattr(session, "total_amount", None) is None:
            return False, "整体业务流程核查未通过：票价尚未回传，屏上未显示金额，无法确认"

        # ② 游客须已向人形机器人确认（严格模式：本会话已有游客消息记录时强制）
        with self._lock:
            rec = self._store.get(session.session_id)
            strict = bool(rec and rec["messages"])
            visitor_ok = bool(rec and rec["visitor_confirmed"]) if strict else True
        if strict and not visitor_ok:
            return False, "整体业务流程核查未通过：游客尚未向人形机器人确认购票需求"
        return True, ""

    # ---------- 核查清单（供界面展示） ----------
    def status(self, session_id: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            rec = self._store.get(session_id, {"messages": [], "visitor_confirmed": False})
            messages = list(rec["messages"])
            visitor_confirmed = rec["visitor_confirmed"]
        return {
            "session_id": session_id,
            "visitor_confirmed": visitor_confirmed,
            "messages": messages,
            "strict_mode": bool(messages),
        }

    def build_checklist(self, session) -> Dict[str, Any]:
        """结合会话状态生成核查清单与放行结论（供 /verify/ 界面）。"""
        if not session:
            return {"items": [], "conclusion": "no_session", "pass_": False,
                    "reason": "当前无活跃会话"}
        items = [
            {"key": "session", "label": "会话已建立", "ok": True},
            {"key": "session_code", "label": "场次已选",
             "ok": bool(getattr(session, "session_code", None))},
            {"key": "items", "label": "票种/数量已定",
             "ok": bool(getattr(session, "items", None))},
            {"key": "price", "label": "票价已回传（屏显金额）",
             "ok": getattr(session, "total_amount", None) is not None},
            {"key": "visitor", "label": "游客已向人形机器人确认",
             "ok": self.status(session.session_id)["visitor_confirmed"]},
        ]
        ok, reason = self.verify_confirm(session)
        return {
            "session_id": session.session_id,
            "state": session.state,
            "page": session.page,
            "items": items,
            "pass_": ok,
            "reason": reason or ("核查通过，机器人可代按确认键" if ok else ""),
            "strict_mode": self.status(session.session_id)["strict_mode"],
        }

    def clear(self, session_id: str):
        with self._lock:
            self._store.pop(session_id, None)
