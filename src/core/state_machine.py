"""会话状态机（方案 4.9 节 状态迁移与前置条件）

三条核心机制（9.4 节）：
1. 门禁判定：每个迁移点有前置条件集合，全部满足才允许迁移，否则进异常分支。
2. 幂等控制：以「订单号 + 动作编号」为幂等键，重复触发只执行一次。
3. 超时与释放：每个阶段设置等待时限，超时关闭会话并释放库存。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

from .models import STATE_STAGE_MAP, SessionState, TERMINAL_STATES

logger = logging.getLogger("host.state")


class GateRejected(Exception):
    """门禁未通过"""

    def __init__(self, from_state: str, event: str, reason: str):
        self.from_state = from_state
        self.event = event
        self.reason = reason
        super().__init__(f"{from_state} --{event}--> 拒绝：{reason}")


# --------------------------------------------------------------------------
# 迁移表： (当前态, 事件) -> (目标态, 门禁说明)
# --------------------------------------------------------------------------
TRANSITIONS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # P1 接待唤醒
    ("S0", "session_create"): ("S1", "无会话占用且设备自检通过"),

    # P2 需求确认
    ("S1", "need_start"):     ("S2", "会话已建立、显示终端在线或已降级语音"),
    ("S2", "need_input"):     ("S2", "键位回读一致（不一致则限次重试回环）"),
    ("S2", "need_complete"):  ("S3", "场次/票种/数量完整且余量充足"),
    ("S2", "need_cancel"):    ("S0", "游客放弃或超时，释放库存"),

    # P2→P3：按【确认】锁定库存，进入身份核验
    # 注：「游客是否确认」门禁移交「整体业务流程核查服务」(VerificationService)，
    # 由 TicketingBridge 在迁移前判定；状态机此处仅校验需求完整 + 余量充足。
    ("S3", "need_confirm"):   ("S4", "需求完整 + 余量充足（游客确认由核查服务判定）"),

    # P3 身份核验
    ("S4", "card_read_ok"):   ("S5", "读卡成功 + 实名信息锁定"),
    ("S4", "card_read_fail"): ("S4", "限次重试（3 次），仍失败转人工"),
    ("S4", "no_id_card"):     ("E4", "未携带身份证，转人工窗口"),

    # P3→P4：票务系统生成最终订单
    ("S5", "order_generated"): ("S6", "票种资格与年龄匹配一致（或已澄清）"),

    # P4 订单确认
    ("S6", "order_confirm"):   ("S7", "语音播报与终端显示一致 + 已按【确认】锁定"),
    ("S6", "order_reject"):    ("S2", "游客有异议，按【取消】退回需求确认"),

    # P5 扫码支付
    ("S7", "pay_success"):     ("S8", "支付平台返回成功且流水落库（幂等）"),
    ("S7", "pay_fail"):        ("S7", "明确失败：告知原因，可重试"),
    ("S7", "pay_unknown"):     ("E2", "状态不明：反查优先，禁止重复扣款"),
    ("S7", "pay_timeout"):     ("E1", "超时取消订单并释放库存"),

    # P6 打印出票
    ("S8", "print_command"):   ("S9", "支付已确认且未重复打印（幂等）"),
    ("S9", "ticket_arrived"):  ("S10", "视觉识别票据就位且数量与订单一致"),
    ("S9", "print_fail"):      ("E3", "超时未出票 / 缺纸 / 卡纸 → 声光报警转人工"),

    # P7 取票递票
    ("S10", "ticket_taken"):   ("S11", "托盘递至取票口且游客取走（或到达等待时限）"),
    ("S10", "deliver_fail"):   ("E3", "撕取/递送失败，限次重试后转人工"),

    # 通用异常出口
    ("S1", "timeout"): ("E1", "会话超时关闭"),
    ("S2", "timeout"): ("E1", "会话超时关闭"),
    ("S3", "timeout"): ("E1", "会话超时关闭"),
    ("S4", "timeout"): ("E1", "会话超时关闭"),
    ("S5", "timeout"): ("E1", "会话超时关闭"),
    ("S6", "timeout"): ("E1", "会话超时关闭"),
    ("S7", "timeout"): ("E1", "会话超时关闭"),
    ("S1", "manual"):  ("E4", "人工介入"),
    ("S2", "manual"):  ("E4", "人工介入"),
    ("S3", "manual"):  ("E4", "人工介入"),
    ("S4", "manual"):  ("E4", "人工介入"),
    ("S5", "manual"):  ("E4", "人工介入"),
    ("S6", "manual"):  ("E4", "人工介入"),
    ("S7", "manual"):  ("E4", "人工介入"),
    ("S8", "manual"):  ("E4", "人工介入"),
    ("S9", "manual"):  ("E4", "人工介入"),
    ("S10", "manual"): ("E4", "人工介入"),
}


# --------------------------------------------------------------------------
# 门禁判定：事件 -> 上下文校验函数
# 返回 (通过, 原因)
# --------------------------------------------------------------------------
def _g_session_create(ctx) -> Tuple[bool, str]:
    if not ctx.devices_ready:
        return False, "设备自检未通过（打印机/读卡器/扫码器在线状态与纸张余量）"
    return True, ""


def _g_need_complete(ctx) -> Tuple[bool, str]:
    if not ctx.session_code:
        return False, "场次缺失"
    if not ctx.items:
        return False, "票种与数量缺失"
    if ctx.remaining is not None and sum(i.get("qty", 0) for i in ctx.items) > ctx.remaining:
        return False, "所选场次票种余量不足"
    return True, ""


def _g_need_confirm(ctx) -> Tuple[bool, str]:
    # 「游客是否确认」的门禁已整体移交给「整体业务流程核查服务」
    # （VerificationService），由 TicketingBridge._on_confirm_cancel 在调用本迁移前
    # 先行判定（verification.verify_confirm）。状态机此处只校验票务系统自身域：
    # 需求完整 + 余量充足。这样符合「票务系统不对确认门禁做判断」的现场链路约束。
    ok, why = _g_need_complete(ctx)
    if not ok:
        return ok, why
    return True, ""


def _g_card_read_ok(ctx) -> Tuple[bool, str]:
    if not ctx.id_locked:
        return False, "实名信息未锁定"
    if ctx.id_count is not None and ctx.id_count < ctx.total_qty:
        return False, f"证件数({ctx.id_count})少于需求票数({ctx.total_qty})，需补充刷证"
    return True, ""


def _g_order_generated(ctx) -> Tuple[bool, str]:
    if not ctx.order_id:
        return False, "票务系统未生成最终订单号"
    if ctx.age_mismatch_pending:
        return False, "票种资格与年龄不匹配，待澄清"
    return True, ""


def _g_order_confirm(ctx) -> Tuple[bool, str]:
    if ctx.total_amount is None:
        return False, "应付金额未知，不得进入支付"
    if not ctx.confirm_key_pressed:
        return False, "机器人未按【确认】锁定票种与数量"
    if not ctx.speech_matches_display:
        return False, "语音播报与显示终端内容不一致"
    return True, ""


def _g_pay_success(ctx) -> Tuple[bool, str]:
    if not ctx.trade_no:
        return False, "支付流水号未落库"
    return True, ""


def _g_print_command(ctx) -> Tuple[bool, str]:
    if ctx.print_issued:
        return False, "打印指令已下发，幂等拦截重复打印"
    return True, ""


def _g_ticket_arrived(ctx) -> Tuple[bool, str]:
    if not ctx.ticket_arrived:
        return False, "票据未落托"
    if ctx.actual_qty != ctx.total_qty:
        return False, f"出票数量({ctx.actual_qty})与订单({ctx.total_qty})不一致"
    if ctx.paper_empty:
        return False, "打印机缺纸"
    return True, ""


GATES: Dict[str, Callable] = {
    "session_create": _g_session_create,
    "need_complete": _g_need_complete,
    "need_confirm": _g_need_confirm,
    "card_read_ok": _g_card_read_ok,
    "order_generated": _g_order_generated,
    "order_confirm": _g_order_confirm,
    "pay_success": _g_pay_success,
    "print_command": _g_print_command,
    "ticket_arrived": _g_ticket_arrived,
}


class StateMachine:
    """状态推进入口：所有状态变更必须经 transition()，禁止直接赋值"""

    @staticmethod
    def stage_of(state: str) -> Optional[str]:
        return STATE_STAGE_MAP.get(state)

    @staticmethod
    def is_terminal(state: str) -> bool:
        return state in {s.value for s in TERMINAL_STATES}

    @classmethod
    def peek(cls, current: str, event: str) -> Optional[str]:
        t = TRANSITIONS.get((current, event))
        return t[0] if t else None

    @classmethod
    def transition(cls, session, event: str, force: bool = False) -> str:
        """执行状态迁移。返回目标态；门禁不通过抛 GateRejected。"""
        current = session.state
        row = TRANSITIONS.get((current, event))
        if not row:
            raise GateRejected(current, event,
                               f"当前状态不允许该事件（合法事件："
                               f"{sorted(e for s, e in TRANSITIONS if s == current)}）")
        target, gate_desc = row

        gate = GATES.get(event)
        if gate and not force:
            ok, why = gate(session)
            if not ok:
                reason = why or gate_desc
                logger.warning("[%s] 门禁拦截 %s --%s--> %s：%s",
                               session.session_id, current, event, target, reason)
                raise GateRejected(current, event, reason)

        logger.info("[%s] %s --%s--> %s（门禁：%s）",
                    session.session_id, current, event, target, gate_desc)
        session.state = target
        session.append_history(current, event, target)
        return target
