"""票务状态对接 + 状态联动引擎（方案 9.3 / 9.4 节）

核心职责
1. 接收票务系统**单向推送**的 7 类状态，写入状态缓存并附采集时间戳与置信度；
2. 以事件驱动方式驱动会话状态机（门禁 / 幂等 / 超时）；
3. 状态变更后统一：推送显示终端页面 + 转发机器人 + 记录系统层日志；
4. 出票结果**不采信票务系统**（票务下发打印指令即认为成功），
   必须由机器人视觉独立识别，缺纸/少出票/未出票一律判定异常并声光报警。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from ..core.models import (
    ConfirmCancelPush, IdCardReadPush, KeyPressedPush, PayResultPush,
    PrintCommandPush, SoldOutPush, StateSnapshot, TicketPricePush,
)
from ..core.session import EventBus, Session, SessionManager, mask_id
from ..core.state_machine import GateRejected, StateMachine
from .display_service import DisplayService

logger = logging.getLogger("host.bridge")

CONF_PUSH = 0.95


class BridgeResult:
    def __init__(self, ok: bool, code: int, message: str, state: str = "", page: str = ""):
        self.ok = ok
        self.code = code
        self.message = message
        self.state = state
        self.page = page

    def to_dict(self):
        return {"ok": self.ok, "code": self.code, "message": self.message,
                "state": self.state, "page": self.page}


class TicketingBridge:
    """状态联动引擎：把推送事件翻译成状态迁移"""

    def __init__(self, sessions: SessionManager, bus: EventBus, display: DisplayService,
                 robot, alarm, devices, rules, settings, verification=None):
        self.sessions = sessions
        self.bus = bus
        self.display = display
        self.robot = robot
        self.alarm = alarm
        self.devices = devices
        self.rules = rules
        self.settings = settings
        # 整体业务流程核查服务：持有「确认」门禁的判定权（票务系统键盘只转发，不判定）
        self.verification = verification
        self.push_log: list[Dict[str, Any]] = []

    # ==================================================================
    # 统一入口
    # ==================================================================
    def handle_push(self, payload: Dict[str, Any]) -> BridgeResult:
        ptype = payload.get("type")
        self.push_log.append({"ts": datetime.now().isoformat(), "type": ptype,
                              "payload": payload})
        self.push_log = self.push_log[-200:]

        # 规则热更新检查
        try:
            from ..core.config import rules as global_rules
            global_rules.reload_if_changed()
        except Exception:  # noqa: BLE001
            pass

        handler = {
            "key_pressed": self._on_key_pressed,
            "id_card_read": self._on_id_card,
            "ticket_price": self._on_ticket_price,
            "pay_result": self._on_pay_result,
            "confirm_cancel": self._on_confirm_cancel,
            "print_command": self._on_print_command,
            "session_sold_out": self._on_sold_out,
        }.get(ptype)
        if handler is None:
            return BridgeResult(False, 400, f"未知推送类型：{ptype}")

        sid = payload.get("session_id")
        session = self.sessions.get(sid) if sid else self.sessions.active()
        if session is None:
            return BridgeResult(False, 404,
                                f"未找到会话 {sid or '(活动会话)'}，请先调用 /api/session/create")

        try:
            return handler(session, payload)
        except GateRejected as e:
            session.log("gate", f"门禁拒绝：{e.reason}", event=e.event)
            self._publish(session, {"message": e.reason})
            return BridgeResult(False, 409, str(e), session.state, session.page)
        except Exception as e:  # noqa: BLE001
            logger.exception("处理推送异常")
            return BridgeResult(False, 500, f"内部错误：{e}", session.state, session.page)

    # ==================================================================
    # ① 按键后的场次与票种信息
    # ==================================================================
    def _on_key_pressed(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = KeyPressedPush(**p)
        key = (push.key or "").upper()

        # K1 / K2 也可能经此通道上报，转交控制键处理
        if key in ("K1", "K2"):
            return self._on_confirm_cancel(
                s, {"type": "confirm_cancel", "session_id": s.session_id,
                    "key": "cancel" if key == "K1" else "confirm"})

        keymap = self.rules.get("keymap", {})
        km = keymap.get(key)
        if not km:
            return BridgeResult(False, 400, f"未定义键位 {key}")

        # 回读不一致属异常分支，先走限次重试，不登记幂等键，
        # 否则重试计数被幂等拦截、永远达不到上限（E32 失效）。
        if push.echo_ok is False:
            n = s.bump_retry(f"key_echo:{key}")
            limit = int(self.rules.timeout("P2_key_retry_max", 3))
            s.log("key", f"键位 {key} 回读不一致，第 {n} 次重试", key=key)
            if n >= limit:
                self.alarm.trigger("E32", "L2", f"键位 {key} 回读连续不一致", s.session_id)
                return BridgeResult(False, 409,
                                    f"键位 {key} 回读连续 {n} 次不一致，转人工", s.state, s.page)
            self._publish(s, {"retry": n})
            return BridgeResult(False, 409, f"键位回读不一致，重试 {n}/{limit}", s.state, s.page)

        # 幂等：同一键在同一计数状态下只生效一次
        idem_ref = f"{key}#{s.total_qty}"
        if not s.idem(f"key:{key}", idem_ref):
            return BridgeResult(True, 208, "重复键位事件已忽略（幂等）", s.state, s.page)

        # 场次键
        if key in ("K6", "K7"):
            s.session_code = "day" if key == "K6" else "night"
            s.log("key", f"场次键 {key} → {s.session_code}")
        else:
            # 票种键：按一次记一张（4.5 节计数规则）
            tcode = {"K3": "student", "K4": "adult", "K5": "senior"}.get(key)
            if not tcode:
                return BridgeResult(False, 400, f"键位 {key} 未映射票种")
            meta = self.rules.get(f"ticket_types.{tcode}", {})
            self._add_item(s, tcode, meta.get("name", tcode), 1, meta.get("price", 0))
            s.log("key", f"票种键 {key} → {meta.get('name')} +1，当前 {s.total_qty} 张")

        # 状态推进：S1 → S2
        if s.state == "S1":
            StateMachine.transition(s, "need_start")

        # 回读校验：票务系统返回的票数与本地累计不一致时以推送为准
        if push.qty is not None and push.qty != s.total_qty:
            s.log("key", f"票数回读校正：本地 {s.total_qty} → 票务 {push.qty}")
        self._publish(s, {"last_key": key, "key_cap": km.get("cap")})
        self._forward(s, "ticket_push", {"type": "key_pressed", "key": key,
                                         "session_code": s.session_code,
                                         "total_qty": s.total_qty})
        return BridgeResult(True, 0, f"已录入 {km.get('cap')}，当前 {s.total_qty} 张",
                            s.state, s.page)

    def _add_item(self, s: Session, code: str, name: str, qty: int, price: float):
        for it in s.items:
            if it.get("ticket_type") == code:
                it["qty"] = int(it.get("qty", 0)) + qty
                it["amount"] = round(it["qty"] * it.get("unit_price", price), 2)
                return
        s.items.append({"ticket_type": code, "name": name, "qty": qty,
                        "unit_price": price, "amount": round(qty * price, 2)})

    # ==================================================================
    # ② 刷证后的身份证信息
    # ==================================================================
    def _on_id_card(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = IdCardReadPush(**p)
        if push.read_status != "success":
            n = s.bump_retry("id_read")
            limit = int(self.rules.timeout("P3_card_retry_max", 3))
            s.log("idcard", f"证件读取失败，第 {n} 次", status=push.read_status)
            if n >= limit:
                StateMachine.transition(s, "manual", force=True)
                self._publish(s, {"message": "证件多次读取失败，请到人工窗口办理"})
                return BridgeResult(False, 409, "证件读取连续失败，转人工", s.state, s.page)
            self._publish(s, {"message": "证件读取失败，请重新平放身份证"})
            return BridgeResult(False, 409, f"读取失败 {n}/{limit}，请重新放置", s.state, s.page)

        card_key = push.id_masked or ""
        if not s.idem("id_card", card_key):
            # 重复刷同一张证：若证件数仍少于需求票数，按提醒间隔递增提醒次数；
            # 达上限后按已核验证件锁定票种与数量（4.10 节 E29）
            if s.state == "S4" and (s.id_count or 0) < s.total_qty:
                n = s.bump_retry("multi_card_remind")
                limit = int(self.rules.timeout("P3_multi_card_remind_max", 3))
                need = max(s.total_qty - (s.id_count or 0), 0)
                if n >= limit:
                    s.log("idcard", f"多证件提醒已达 {limit} 次上限，按已核验证件锁定")
                    s.confirm_key_pressed = True
                    StateMachine.transition(s, "card_read_ok", force=True)
                    self._publish(s, {})
                    return BridgeResult(True, 0,
                                        f"提醒 {limit} 次无新证件，按已核验 {s.id_count} 张锁定",
                                        s.state, s.page)
                self._publish(s, {"message": self.rules.prompt("multi_card_remind", n=need)})
                return BridgeResult(True, 208,
                                    f"重复证件，第 {n}/{limit} 次提醒刷下一张", s.state, s.page)
            return BridgeResult(True, 208, "重复证件事件已忽略（幂等，防止覆盖已锁定实名）",
                                s.state, s.page)

        s.id_masked = mask_id(push.id_masked or "")
        s.age = push.age
        s.eligible_type = push.eligible_type
        s.id_locked = bool(push.locked)
        s.id_count = push.card_count if push.card_count is not None else (s.id_count or 0) + 1
        s.log("idcard", f"证件读取成功（{s.id_masked}），年龄 {s.age}，"
                        f"资格 {s.eligible_type}，已刷 {s.id_count} 张")

        # 票种资格与需求不一致 → 标记待澄清（4.6 节）
        if push.eligible_type and s.items:
            declared = {i.get("ticket_type") for i in s.items}
            if push.eligible_type not in declared:
                s.age_mismatch_pending = True
                s.log("idcard", f"票种资格与需求不一致：需求 {declared}，资格 {push.eligible_type}")

        # 门禁：证件数少于需求票数 → 提醒补充，不推进
        if s.state == "S4":
            try:
                StateMachine.transition(s, "card_read_ok")
            except GateRejected as e:
                if "少于需求票数" in e.reason:
                    n = s.bump_retry("multi_card_remind")
                    limit = int(self.rules.timeout("P3_multi_card_remind_max", 3))
                    if n >= limit:
                        s.log("idcard", "多证件提醒已达上限，按已核验证件锁定票种与数量")
                        s.confirm_key_pressed = True
                        StateMachine.transition(s, "card_read_ok", force=True)
                    else:
                        self._publish(s, {"message": self.rules.prompt(
                            "multi_card_remind", n=max(s.total_qty - (s.id_count or 0), 0))})
                        return BridgeResult(True, 0, e.reason, s.state, s.page)
                else:
                    raise

        # P3 完成 → 等待票务系统生成最终订单（S5 → S6 由 order_generated 触发）
        self._publish(s, {"id_masked": s.id_masked, "verified": s.id_locked})
        self._forward(s, "ticket_push", {"type": "id_card_read", "age": s.age,
                                         "eligible_type": s.eligible_type,
                                         "id_count": s.id_count})
        return BridgeResult(True, 0, f"实名核验完成，已刷 {s.id_count} 张", s.state, s.page)

    # ==================================================================
    # ③ 票种票数与票价信息
    # ==================================================================
    def _on_ticket_price(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = TicketPricePush(**p)
        # 票务系统生成最终订单后随票价信息回传订单号（4.6 节）
        if push.order_id:
            s.order_id = push.order_id
        if push.items:
            s.items = push.items
        if push.total_amount is not None:
            s.total_amount = push.total_amount
        else:
            s.total_amount = round(sum(float(i.get("amount", 0)) for i in s.items), 2)
        if push.session_code:
            s.session_code = push.session_code
        if push.remaining is not None:
            s.remaining = push.remaining
        s.log("price", f"票价信息：{s.total_qty} 张 / {s.total_amount} 元，余量 {s.remaining}")

        # S2 → S3：字段完整且余量充足
        if s.state == "S2":
            try:
                StateMachine.transition(s, "need_complete")
            except GateRejected as e:
                self._publish(s, {"message": e.reason})
                return BridgeResult(False, 409, e.reason, s.state, s.page)

        # P3→P4：实名已锁定且票务已回传最终订单号 → 订单待确认（S5 → S6）
        if s.state == "S5" and s.order_id and not s.age_mismatch_pending:
            try:
                StateMachine.transition(s, "order_generated")
                s.log("order", f"最终订单生成 {s.order_id}，进入订单待确认")
            except GateRejected as e:
                s.log("order", f"订单生成门禁未通过：{e.reason}")

        # 余量不足 → E33
        if s.remaining is not None and s.total_qty > s.remaining:
            self._publish(s, {"message": f"可售余量不足，当前仅剩 {s.remaining} 张"})
            return BridgeResult(False, 409, f"余量不足（剩 {s.remaining}）", s.state, s.page)

        self._publish(s, {})
        self._forward(s, "ticket_push", {"type": "ticket_price",
                                         "total_amount": s.total_amount,
                                         "total_qty": s.total_qty})
        return BridgeResult(True, 0, f"订单金额 {s.total_amount} 元", s.state, s.page)

    # ==================================================================
    # ④ 扫码支付成功或失败
    # ==================================================================
    def _on_pay_result(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = PayResultPush(**p)
        s.pay_status = push.status

        if push.status == "success":
            if not s.idem("pay_success", push.trade_no or ""):
                return BridgeResult(True, 208, "重复支付成功事件已忽略（幂等）", s.state, s.page)
            s.trade_no = push.trade_no
            StateMachine.transition(s, "pay_success")
            s.log("pay", f"支付成功，流水号 {push.trade_no}")
            self._publish(s, {})
            self._forward(s, "ticket_push", {"type": "pay_result", "status": "success",
                                             "trade_no": s.trade_no})
            return BridgeResult(True, 0, "支付成功", s.state, s.page)

        if push.status == "fail":
            n = s.bump_retry("pay")
            s.log("pay", f"支付失败：{push.fail_reason}")
            self._publish(s, {"message": push.fail_reason or "支付失败，请更换支付方式"})
            return BridgeResult(False, 409, f"支付失败：{push.fail_reason}", s.state, s.page)

        # unknown：反查优先，禁止重复扣款（12.5 节 E42/E43）
        StateMachine.transition(s, "pay_unknown", force=True)
        self.alarm.trigger("E43", "L1", "支付状态不明，禁止重复扣款，请人工核对流水",
                           s.session_id)
        s.log("pay", "支付状态不明 → E2，等待反查，禁止重复扣款")
        self._publish(s, {"message": "正在确认支付结果，请勿重复付款"})
        self._forward(s, "ticket_push", {"type": "pay_result", "status": "unknown"})
        return BridgeResult(False, 409, "支付状态不明，已转入 E2 等待反查", s.state, s.page)

    # ==================================================================
    # ⑤ 确认与取消按键消息
    # ==================================================================
    def _on_confirm_cancel(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = ConfirmCancelPush(**p)
        if push.key == "confirm":
            # 门禁由「整体业务流程核查服务」判定（票务系统键盘只是外设，只转发、不判定）。
            # 键盘按下即视为机器人已收到游客确认意图；是否放行由核查服务把关。
            if self.verification is not None:
                ok, reason = self.verification.verify_confirm(s)
                if not ok:
                    s.log("verify", f"确认被核查服务拦截：{reason}")
                    self._publish(s, {"message": reason})
                    return BridgeResult(False, 409, reason, s.state, s.page)
            s.confirm_key_pressed = True
            s.speech_matches_display = True
            if s.state == "S3":
                StateMachine.transition(s, "need_confirm")
                s.log("key", "按【确认】锁定库存，进入身份核验")
            elif s.state == "S5":
                # 已锁定实名且票务已生成订单 → 进入订单待确认
                if s.order_id:
                    s.state = "S6"
                    s.append_history("S5", "order_generated", "S6")
                StateMachine.transition(s, "order_confirm")
                s.log("key", "按【确认】锁定票种与数量，进入支付")
            elif s.state == "S6":
                StateMachine.transition(s, "order_confirm")
            else:
                return BridgeResult(False, 409, f"当前状态 {s.state} 不接受【确认】",
                                    s.state, s.page)
            self._publish(s, {})
            self._forward(s, "ticket_push", {"type": "confirm", "state": s.state})
            return BridgeResult(True, 0, f"【确认】生效，进入 {s.state}", s.state, s.page)

        # cancel
        if s.state in ("S2", "S3"):
            # 提交前为退格：最后一类票减一
            if s.items:
                last = s.items[-1]
                last["qty"] = int(last.get("qty", 0)) - 1
                if last["qty"] <= 0:
                    s.items.pop()
                else:
                    last["amount"] = round(last["qty"] * last.get("unit_price", 0), 2)
                s.log("key", f"【取消】退格，当前 {s.total_qty} 张")
            self._publish(s, {})
            return BridgeResult(True, 0, f"退格完成，当前 {s.total_qty} 张", s.state, s.page)
        if s.state == "S6":
            StateMachine.transition(s, "order_reject")
            s.confirm_key_pressed = False
            s.log("key", "【取消】退回需求确认，重做需求与身份核验")
            self._publish(s, {"message": "已取消订单，请重新确认购票需求"})
            return BridgeResult(True, 0, "已退回需求确认 S2", s.state, s.page)
        if s.state in ("S4", "S5", "S7"):
            StateMachine.transition(s, "manual", force=True)
            s.log("key", "【取消】中止会话，释放库存")
            self.sessions.close(s.session_id, "游客或机器人取消，会话中止")
            self._publish(s, {"message": "本次购票已取消"})
            return BridgeResult(True, 0, "会话已取消", s.state, s.page)
        return BridgeResult(False, 409, f"当前状态 {s.state} 不接受【取消】", s.state, s.page)

    # ==================================================================
    # ⑥ 打印指令下发
    # ==================================================================
    def _on_print_command(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = PrintCommandPush(**p)
        # 缺纸预检（11.2 节：一次换纸 500 张，剩余 5 张预警）
        if self.devices.paper_low():
            self.alarm.trigger("E51", "L1", self.rules.prompt("paper_low"), s.session_id)
            self._publish(s, {"message": "门票纸张即将用尽，请工作人员处理"})
            return BridgeResult(False, 503, "纸张将用尽/已缺纸，暂停出票", s.state, s.page)

        try:
            StateMachine.transition(s, "print_command")
        except GateRejected:
            return BridgeResult(True, 208, "打印指令已下发，幂等忽略重复", s.state, s.page)

        s.print_issued = True
        self.devices.consume_paper(push.qty or 1)
        s.paper_remaining = self.devices.paper_remaining
        s.log("print", f"打印指令下发 {push.qty} 张，纸仓剩余 {s.paper_remaining}")
        self._publish(s, {"eta_sec": 15})
        self._forward(s, "ticket_push", {"type": "print_command", "qty": push.qty})
        return BridgeResult(True, 0, f"打印 {push.qty} 张，等待机器人视觉确认",
                            s.state, s.page)

    # ==================================================================
    # ⑦ 场次票售完
    # ==================================================================
    def _on_sold_out(self, s: Session, p: Dict[str, Any]) -> BridgeResult:
        push = SoldOutPush(**p)
        s.remaining = 0
        msg = (self.rules.prompt("session_sold_out_night_available")
               if push.night_available else self.rules.prompt("session_sold_out_all"))
        s.log("soldout", f"{push.session_code} 场次售罄：{msg}")
        self._publish(s, {"message": msg, "sold_out": True})
        self._forward(s, "ticket_push", {"type": "session_sold_out",
                                         "session_code": push.session_code,
                                         "night_available": push.night_available})
        return BridgeResult(False, 409, msg, s.state, s.page)

    # ==================================================================
    # 机器人侧回传：出票视觉识别（9.9 /api/print/verify）
    # ==================================================================
    def handle_print_verify(self, s: Session, body) -> BridgeResult:
        s.ticket_arrived = bool(body.arrived)
        s.actual_qty = int(body.actual_qty or 0)
        s.paper_empty = bool(body.paper_empty)
        if body.paper_remaining is not None:
            s.paper_remaining = body.paper_remaining

        if s.paper_empty:
            self.alarm.trigger("E51", "L1", "打印机缺纸", s.session_id)
            self._publish(s, {"message": "门票纸张已用尽，请工作人员处理"})
            return BridgeResult(False, 503, "缺纸，已启动声光报警", s.state, s.page)

        try:
            StateMachine.transition(s, "ticket_arrived")
        except GateRejected as e:
            n = s.bump_retry("print")
            if "缺纸" in e.reason:
                self.alarm.trigger("E51", "L1", "打印机缺纸，暂停售票", s.session_id)
            elif s.actual_qty < s.total_qty:
                s.log("print", f"少出票：实际 {s.actual_qty} / 订单 {s.total_qty}，需补打")
                self._publish(s, {"message": "正在补打缺失门票，请稍候"})
            if n >= int(self.rules.timeout("P7_tear_retry_max", 2)):
                StateMachine.transition(s, "print_fail", force=True)
                self.alarm.trigger("E59", "L1", "下发打印指令后实际未出票，请人工窗口补打",
                                   s.session_id)
                self._publish(s, {"message": "出票异常，请到人工窗口补票"})
                return BridgeResult(False, 500, "出票异常，转人工", s.state, s.page)
            return BridgeResult(False, 409, e.reason, s.state, s.page)

        s.log("print", f"票据已就位：{s.actual_qty} 张，与订单一致")
        self._publish(s, {})
        self._forward(s, "ticket_push", {"type": "ticket_arrived", "qty": s.actual_qty})
        return BridgeResult(True, 0, "票据已就位，可撕取递票", s.state, s.page)

    # ==================================================================
    # 取票完成
    # ==================================================================
    def handle_ticket_taken(self, s: Session) -> BridgeResult:
        try:
            StateMachine.transition(s, "ticket_taken")
        except GateRejected as e:
            return BridgeResult(False, 409, e.reason, s.state, s.page)
        s.log("deliver", "游客已取票，交易归档，会话复位")
        self.sessions.close(s.session_id, "S11 已完成，交易归档")
        self._publish(s, {})
        return BridgeResult(True, 0, "交易完成，会话复位", s.state, s.page)

    # ==================================================================
    # 通用出口
    # ==================================================================
    def _publish(self, s: Session, extra: Optional[Dict[str, Any]] = None):
        snap = self.display.build(s, extra)
        self.bus.publish(snap)
        logger.info("[%s] 页面推送 → %s（v%d）", s.session_id, snap.page, snap.version)

    def _forward(self, s: Session, kind: str, payload: Dict[str, Any]):
        payload = {**payload, "session_id": s.session_id, "state": s.state}
        self.robot.forward(kind, payload)
