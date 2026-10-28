"""信息显示终端页面决策（方案 9.5 节 / 11.1 节表 11-1）

原则：页面内容由后台服务按会话状态**统一推送**，终端只渲染、不做业务判断，
      从而保证终端与机器人语音、机器人动作三者一致。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from ..core.models import StateSnapshot
from ..core.session import Session

logger = logging.getLogger("host.display")

PAGE_NAMES = {
    "P-0": "待机欢迎页",
    "P-1": "需求确认页",
    "P-2": "身份核验页",
    "P-3": "订单确认页",
    "P-4": "扫码支付页",
    "P-5": "出票中页面",
    "P-6": "取票完成页",
    "P-7": "异常提示页",
}

STEPS = [
    {"key": "P2", "label": "说需求", "label_en": "Tell us"},
    {"key": "P3", "label": "刷身份证", "label_en": "Scan ID"},
    {"key": "P5", "label": "扫码支付", "label_en": "Pay"},
    {"key": "P7", "label": "取票入园", "label_en": "Collect"},
]


def _type_summary(session: Session, cn: bool = False) -> str:
    """把订单项渲染为「成人票2张、学生票1张」句式。

    cn=True 时数量用中文数字（成人票两张），供 TTS 播报使用，
    避免「2 张」被读成「二张」以外的歧义，与表 11-3 标准话术一致。
    """
    if not session.items:
        return ""
    return "、".join(
        f"{i.get('name', i.get('ticket_type', ''))}"
        f"{_num_cn(int(i.get('qty', 0))) if cn else int(i.get('qty', 0))}张"
        for i in session.items
    )


def _num_cn(n: int) -> str:
    """1–99 的中文数字（话术只需覆盖常见张数）"""
    digits = "零一二三四五六七八九"
    if n <= 0:
        return "零"
    if n == 1:
        return "一"
    if n == 2:
        return "两"      # 话术习惯：两张
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n % 10] if n % 10 else "")
    h, r = n // 10, n % 10
    return f"{digits[h]}十" + (digits[r] if r else "")


def _amount_cn(amount: float) -> str:
    """金额中文大写（用于 TTS，避免「150」被读成「一五零」）"""
    digits = "零一二三四五六七八九"
    units = ["", "十", "百", "千", "万"]
    n = int(round(amount))
    if n == 0:
        return "零元"
    if n < 10:
        return f"{digits[n]}元"
    if n < 20:
        return f"十{digits[n % 10]}元" if n % 10 else "十元"
    if n < 100:
        return f"{digits[n // 10]}十{digits[n % 10]}元" if n % 10 else f"{digits[n // 10]}十元"
    if n < 1000:
        h, r = n // 100, n % 100
        tail = f"{digits[r // 10]}十{digits[r % 10]}" if r >= 10 else (digits[r] if r else "")
        return f"{digits[h]}百{tail}元"
    return f"{n}元"


class DisplayService:
    """把会话状态翻译成终端页面 + 数据负载"""

    def __init__(self, rules, settings):
        self.rules = rules
        self.settings = settings

    # ------------------------------------------------------------------
    def build(self, session: Optional[Session],
              extra: Optional[Dict[str, Any]] = None) -> StateSnapshot:
        extra = extra or {}
        if session is None or session.state in ("S0", "S11", "E1", "E2", "E3", "E4"):
            if session is None:
                page = "P-0"
            else:
                page = self.rules.page_for_state(session.state)
            return StateSnapshot(
                version=0,
                session_id=session.session_id if session else None,
                state=session.state if session else "S0",
                stage=None,
                page=page,
                page_name=PAGE_NAMES.get(page, ""),
                data=self._page_data(page, session, extra),
                updated_at=session.updated_at if session else _now(),
            )
        page = self.rules.page_for_state(session.state)
        return StateSnapshot(
            version=0,
            session_id=session.session_id,
            state=session.state,
            stage=session.stage,
            page=page,
            page_name=PAGE_NAMES.get(page, ""),
            data=self._page_data(page, session, extra),
            updated_at=session.updated_at,
        )

    # ------------------------------------------------------------------
    def _page_data(self, page: str, session: Optional[Session],
                   extra: Dict[str, Any]) -> Dict[str, Any]:
        fn = {
            "P-0": self._p0, "P-1": self._p1, "P-2": self._p2, "P-3": self._p3,
            "P-4": self._p4, "P-5": self._p5, "P-6": self._p6, "P-7": self._p7,
        }.get(page, self._p0)
        return fn(session, extra)

    # ---- P-0 待机欢迎页 ----
    def _p0(self, s: Optional[Session], extra) -> Dict[str, Any]:
        types = self.rules.get("ticket_types", {})
        return {
            "title": "机器人售票",
            "title_en": "Robot Ticketing",
            "welcome": self.rules.prompt("welcome"),
            "service_hours": "日场 09:00-18:00 / 夜场 18:00-21:00",
            "price_overview": [
                {"name": v.get("name"), "name_en": v.get("name_en"),
                 "price": v.get("price"), "note": (v.get("conditions") or [""])[0]}
                for v in types.values()
            ],
            "steps": STEPS,
            "manual_contact": "需帮助请按对讲呼叫按钮，或前往人工售票窗口",
            "footer": "1.3 米以下儿童免票 · 实名购票 一人一票",
        }

    # ---- P-1 需求确认页 ----
    def _p1(self, s: Session, extra) -> Dict[str, Any]:
        sessions = self.rules.get("sessions", {})
        return {
            "title": "请确认您的购票需求",
            "title_en": "Confirm your order",
            "session_code": s.session_code,
            "session_name": (sessions.get(s.session_code or "", {}) or {}).get("name", ""),
            "items": s.items,
            "total_qty": s.total_qty,
            "total_amount": s.total_amount,
            "remaining": s.remaining,
            "tip": "请确认无误后回答是",
            "tip_en": "Please confirm",
            "steps": STEPS,
            "active_step": "P2",
        }

    # ---- P-2 身份核验页 ----
    def _p2(self, s: Session, extra) -> Dict[str, Any]:
        need = max((s.total_qty - (s.id_count or 0)), 0)
        done = s.id_locked and need == 0
        tip = (self.rules.prompt("multi_card_remind", n=need) if need > 0
               else ("身份核验完成，请确认订单信息" if done
                     else self.rules.prompt("id_card_guide")))
        return {
            "title": "请将身份证平放在读卡区",
            "title_en": "Place your ID card here",
            "guide_image": "id_reader_area",
            "animation": "place_card",
            "realname_notice": "实名购票，一人一票",
            "id_masked": s.id_masked,
            "name_masked": extra.get("name_masked"),
            "age": s.age,
            "verified": s.id_locked,
            "verified_text": "已核验" if s.id_locked else "",
            "need_more": need,
            "verified_done": done,
            "tip": tip,
            "steps": STEPS,
            "active_step": "P3",
        }

    # ---- P-3 订单确认页（付款前最后一道关口，不得跳过） ----
    def _p3(self, s: Session, extra) -> Dict[str, Any]:
        amount = s.total_amount or 0
        types_cn = _type_summary(s, cn=True)
        return {
            "title": "订单确认",
            "title_en": "Order Confirmation",
            "main_text": self.rules.prompt("display_order_confirm",
                                           types=types_cn, amount=_amount_cn(amount)),
            "main_text_numeric": f"{_type_summary(s)}，总共 {amount:.0f} 元",
            "amount": amount,
            "items": s.items,
            "total_qty": s.total_qty,
            "tts": self.rules.prompt("order_confirm", types=types_cn,
                                     amount=_amount_cn(amount)),
            "tip": "请确认，如无异议，请扫码支付！",
            "steps": STEPS,
            "active_step": "P5",
        }

    # ---- P-4 扫码支付页 ----
    def _p4(self, s: Session, extra) -> Dict[str, Any]:
        return {
            "title": "请扫码支付",
            "title_en": "Scan to pay",
            "amount": s.total_amount,
            "amount_cn": _amount_cn(s.total_amount or 0),
            "scan_window_image": "scan_window",
            "pay_methods": ["微信付款码", "支付宝付款码"],
            "pay_methods_en": ["WeChat Pay", "Alipay"],
            "countdown_sec": int(self.rules.timeout("P5_pay_wait_sec", 90)),
            "tip": self.rules.prompt("pay_guide", amount=_amount_cn(s.total_amount or 0)),
            "steps": STEPS,
            "active_step": "P5",
        }

    # ---- P-5 出票中页面 ----
    def _p5(self, s: Session, extra) -> Dict[str, Any]:
        return {
            "title": "正在出票，请稍候",
            "title_en": "Printing your ticket",
            "progress": extra.get("progress", "printing"),
            "eta_sec": extra.get("eta_sec", 15),
            "tip": "请勿离开，门票出票后机器人将递交给您",
            "steps": STEPS,
            "active_step": "P7",
        }

    # ---- P-6 取票完成页 ----
    def _p6(self, s: Session, extra) -> Dict[str, Any]:
        return {
            "title": "购票成功，请取票",
            "title_en": "Thank you, enjoy your visit",
            "congrats": "祝您游览愉快！",
            "items": s.items,
            "total_qty": s.total_qty,
            "total_amount": s.total_amount,
            "entry_tip": self.rules.prompt("finish"),
            "gate": "3 号检票口",
            "tts": self.rules.prompt("deliver"),
            "steps": STEPS,
            "active_step": "P7",
        }

    # ---- P-7 异常提示页 ----
    def _p7(self, s: Optional[Session], extra) -> Dict[str, Any]:
        return {
            "title": "抱歉，本次购票未能完成",
            "title_en": "Something went wrong",
            "reason": extra.get("message") or (s.close_reason if s else "") or "服务暂时不可用",
            "action": extra.get("action") or "请前往人工售票窗口办理，或按对讲按钮呼叫工作人员",
            "action_en": "Please go to the manned ticket window",
            "manual_contact": "人工窗口：售票亭右侧 / 对讲按钮呼叫",
            "session_id": s.session_id if s else None,
            "steps": STEPS,
        }


def _now():
    from datetime import datetime
    return datetime.now()
