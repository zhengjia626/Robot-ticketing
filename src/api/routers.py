"""对外 REST 接口（方案 9.9 节表 9-1）

统一约定：
- 全部 JSON 报文，统一错误码（models.ErrorCode）；
- 票务系统 → 后台为单向推送，本服务不向票务系统下发任何数据；
- 页面切换由后台主动推送，终端不做业务判断。
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Annotated, Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..core.context import HostContext, get_context
from ..core.models import (
    ConfirmCancelPush, DisplayHeartbeatIn, ErrorCode, IdCardReadPush,
    KeyPressedPush, PayResultPush, PrintCommandPush, PrintVerifyIn,
    RobotEventIn, RobotHeartbeatIn, SoldOutPush, StateSnapshot, TicketPricePush,
)
from ..core.state_machine import StateMachine, GateRejected
from ..services.display_service import _amount_cn, _type_summary

logger = logging.getLogger("host.api")


def ctx() -> HostContext:
    return get_context()


# ==========================================================================
# 一、票务系统单向推送
# ==========================================================================
router_ticket = APIRouter(prefix="/api/ticket", tags=["票务系统推送"])


def _check_source(request: Request, c: HostContext):
    allowed = c.settings.get("ticketing.allowed_sources", []) or []
    client = request.client.host if request.client else ""
    for rule in allowed:
        try:
            if ipaddress.ip_address(client) in ipaddress.ip_network(rule, strict=False):
                return True
        except ValueError:
            continue
    if client in allowed:
        return True
    logger.warning("拒绝非白名单来源的推送：%s", client)
    raise HTTPException(ErrorCode.FORBIDDEN, f"来源 {client} 不在白名单内")


# 7 类推送的联合模型：/docs（Swagger UI）据此生成可切换的请求体示例，
# 票务系统厂商可直接在文档里试调。按 type 字段判别具体类型。
TicketPushPayload = Annotated[
    Union[KeyPressedPush, IdCardReadPush, TicketPricePush, PayResultPush,
          ConfirmCancelPush, PrintCommandPush, SoldOutPush],
    Field(discriminator="type"),
]


def _push_guard(request: Request, x_push_token: Optional[str] = Header(None),
                c: HostContext = Depends(ctx)) -> HostContext:
    """推送前置校验：来源白名单 → push_token。

    做成依赖使其在 body 解析之前执行：
    非法来源即使 body 错误也先收到 403，不泄露校验细节。
    """
    _check_source(request, c)
    token = c.settings.get("ticketing.push_token")
    if token and token != "changeme-local-only" and x_push_token != token:
        raise HTTPException(ErrorCode.UNAUTHORIZED, "push token 无效")
    return c


@router_ticket.post("/push")
async def ticket_push(payload: TicketPushPayload,
                      c: HostContext = Depends(_push_guard)):
    """票务系统 → 后台服务（单向）。

    7 类推送：key_pressed / id_card_read / ticket_price /
              pay_result / confirm_cancel / print_command / session_sold_out

    - 请求体按 `type` 字段自动判别为对应模型，字段校验失败返回 422；
    - 业务层校验（门禁 / 幂等 / 设备状态）仍由状态机处理，返回 200 + code。
    """
    r = c.bridge.handle_push(payload.model_dump(mode="json"))
    return {"code": r.code, "message": r.message, "state": r.state, "page": r.page,
            "ok": r.ok}


# ==========================================================================
# 二、会话管理
# ==========================================================================
router_session = APIRouter(prefix="/api/session", tags=["会话管理"])


@router_session.post("/create")
async def session_create(c: HostContext = Depends(ctx)):
    """创建售票会话，返回会话号并触发设备快速自检（9.9 session/create）"""
    quick = c.selfcheck.quick()
    if not quick["sellable"]:
        raise HTTPException(ErrorCode.DEVICE_OFFLINE,
                            {"message": "设备快速自检未通过，拒绝创建会话",
                             "report": quick})
    try:
        s = c.sessions.create()
    except RuntimeError as e:
        raise HTTPException(ErrorCode.GATE_REJECTED, str(e))
    StateMachine.transition(s, "session_create")
    c.bridge._publish(s, {})
    return {"code": 0, "session_id": s.session_id, "state": s.state,
            "page": s.page, "selfcheck": quick}


@router_session.post("/close")
async def session_close(session_id: str, reason: str = "主动关闭",
                        c: HostContext = Depends(ctx)):
    s = c.sessions.get(session_id)
    if not s:
        raise HTTPException(ErrorCode.NOT_FOUND, "会话不存在")
    c.sessions.close(session_id, reason)
    if not StateMachine.is_terminal(s.state):
        StateMachine.transition(s, "manual", force=True)
    c.bridge._publish(s, {"message": reason})
    return {"code": 0, "session_id": session_id, "state": s.state}


@router_session.get("/state")
async def session_state(c: HostContext = Depends(ctx)):
    """查询当前会话状态与下一步动作建议（9.9 session/state）"""
    s = c.sessions.active()
    if not s:
        return {"code": 0, "session": None, "state": "S0", "page": "P-0"}
    return {"code": 0, "session": s.to_dict(), "state": s.state,
            "stage": s.stage, "page": s.page,
            "next_actions": _suggest_actions(s)}


@router_session.get("/{session_id}")
async def session_detail(session_id: str, c: HostContext = Depends(ctx)):
    s = c.sessions.get(session_id)
    if not s:
        raise HTTPException(ErrorCode.NOT_FOUND, "会话不存在")
    return {"code": 0, "session": s.to_dict(), "history": s.history,
            "timeline": s.timeline[-100:]}


@router_session.get("/{session_id}/next-action")
async def session_next_action(session_id: str, c: HostContext = Depends(ctx)):
    """按键序列建议。

    注：按 9.6 节批注，动作编排由机器人本体维护；本接口仅提供建议，
    机器人可拉取参考，后台不强制下发动作指令。
    """
    s = c.sessions.get(session_id)
    if not s:
        raise HTTPException(ErrorCode.NOT_FOUND, "会话不存在")
    return {"code": 0, "session_id": session_id, "state": s.state,
            "stage": s.stage, "actions": _suggest_actions(s)}


def _suggest_actions(s) -> Dict[str, Any]:
    """按状态给出下一步建议（语音 / 页面 / 按键序列）"""
    rules = s.rules
    types = _type_summary(s, cn=True)
    amount_cn = _amount_cn(s.total_amount or 0)
    base = {"state": s.state, "stage": s.stage, "page": s.page}
    if s.state in ("S1", "S2"):
        keys = []
        if s.session_code == "day":
            keys.append("K6")
        elif s.session_code == "night":
            keys.append("K7")
        for i in s.items:
            kmap = {"student": "K3", "adult": "K4", "senior": "K5", "zero": "K5"}
            keys += [kmap.get(i.get("ticket_type"), "K4")] * int(i.get("qty", 0))
        return {**base, "key_sequence": keys, "key_interval_ms":
                rules.get("keymap.key_interval_ms", 1000),
                "tts": rules.prompt("need_confirm", session=s.session_code or "",
                                    qty=s.total_qty, amount=s.total_amount or 0)}
    if s.state == "S3":
        return {**base, "key_sequence": ["K2"], "tts": "请确认您的购票需求"}
    if s.state == "S4":
        return {**base, "key_sequence": [], "tts": rules.prompt("id_card_guide")}
    if s.state == "S6":
        return {**base, "key_sequence": ["K2"],
                "tts": rules.prompt("order_confirm", types=types, amount=amount_cn)}
    if s.state == "S7":
        return {**base, "key_sequence": [], "tts": rules.prompt("pay_guide", amount=amount_cn)}
    if s.state in ("S9", "S10"):
        return {**base, "key_sequence": [], "tts": rules.prompt("deliver")}
    if s.state == "S11":
        return {**base, "key_sequence": [], "tts": rules.prompt("finish")}
    return {**base, "key_sequence": [], "tts": ""}


# ==========================================================================
# 三、机器人适配
# ==========================================================================
router_robot = APIRouter(prefix="/api", tags=["机器人适配"])


@router_robot.post("/print/verify")
async def print_verify(body: PrintVerifyIn, c: HostContext = Depends(ctx)):
    """机器人回传实际出票状态与出票数量、缺纸状态。

    票务系统下发打印指令后即认为出票成功，不识别打印机异常，
    因此出票结果必须由机器人视觉独立识别（9.3 / 11.2 节）。
    """
    s = c.sessions.get(body.session_id)
    if not s:
        raise HTTPException(ErrorCode.NOT_FOUND, "会话不存在")
    r = c.bridge.handle_print_verify(s, body)
    return {"code": r.code, "message": r.message, "state": r.state, "page": r.page}


@router_robot.post("/robot/alert")
async def robot_alert(body: RobotEventIn, c: HostContext = Depends(ctx)):
    c.robot.push_alert(body.event, body.level, body.detail)
    mapping = {"robot_low_battery": "E67", "arm_fault": "E65", "estop": "E66"}
    if body.level == "L1":
        c.alarm.trigger(mapping.get(body.event, body.event), "L1",
                        f"机器人告警：{body.event}", body.session_id)
    return {"code": 0, "message": "告警已记录"}


@router_robot.post("/robot/heartbeat")
async def robot_heartbeat(body: RobotHeartbeatIn, c: HostContext = Depends(ctx)):
    c.robot.heartbeat(body.model_dump())
    if body.battery is not None and body.battery < 20:
        c.alarm.trigger("E67", "L2", f"机器人电量 {body.battery}%，完成当前会话后停止接单")
    return {"code": 0, "online": True}


@router_robot.get("/robot/status")
async def robot_status(c: HostContext = Depends(ctx)):
    """机器人连接状态 + 后台→机器人 转发日志（验证预留接口可用）"""
    return {"code": 0, "data": {**c.robot.status(), "alerts": c.robot.alerts(10),
                                "forward_log": c.robot.forward_log(10)}}


@router_robot.get("/robot/interface")
async def robot_interface(c: HostContext = Depends(ctx)):
    """预留的人形机器人通信接口契约（9.6 节）。

    现场接入人形机器人后，机器人 SDK 按此契约与本服务交互：
    - 后台 → 机器人：状态转发（POST {robot.base_url}/api/host/forward），事件类型见 forward_events
    - 机器人 → 后台：心跳 / 告警 / 出票视觉确认 / 取票，对应下列 inbound 端点
    """
    cfg = c.settings.get("robot", {})
    return {"code": 0, "data": {
        "description": "后台不下发动作指令，仅转发票务状态；动作编排由机器人本体维护",
        "outbound": {
            "target": (cfg.get("base_url") or "http://192.168.10.20:8000") + "/api/host/forward",
            "method": "POST",
            "forward_events": cfg.get("forward_events", []),
            "push_enabled": cfg.get("push_enabled", True),
        },
        "inbound": [
            {"endpoint": "POST /api/robot/heartbeat", "purpose": "机器人心跳，base_url 侧可达性"},
            {"endpoint": "POST /api/robot/alert", "purpose": "机器人异常告警（臂故障/急停/低电量）"},
            {"endpoint": "POST /api/print/verify", "purpose": "出票视觉确认（方案要求不采信票务系统）"},
            {"endpoint": "POST /api/session/ticket-taken", "purpose": "游客取票完成，会话归档复位"},
        ],
        "require_heartbeat": cfg.get("require_heartbeat", False),
        "status_endpoint": "GET /api/robot/status",
        "note": "联调阶段 require_heartbeat=false，未收到心跳不阻断售票；现场联调完成后置 true",
    }}


@router_robot.post("/session/ticket-taken")
async def ticket_taken(session_id: str, c: HostContext = Depends(ctx)):
    """游客已取票（或到达等待时限）→ S11 归档复位"""
    s = c.sessions.get(session_id)
    if not s:
        raise HTTPException(ErrorCode.NOT_FOUND, "会话不存在")
    r = c.bridge.handle_ticket_taken(s)
    return {"code": r.code, "message": r.message, "state": r.state, "page": r.page}


# ==========================================================================
# 四、信息显示终端（长轮询）
# ==========================================================================
router_display = APIRouter(prefix="/api", tags=["信息显示终端"])


@router_display.get("/state/long")
def state_long(since: int = Query(0, ge=0),
               terminal_id: Optional[str] = Query(None),
               c: HostContext = Depends(ctx)):
    """长轮询获取状态变更（9.5 节）。

    无变更时挂起最长 25s，超时返回 304 类空响应，终端立即重新发起；
    一旦状态变更立即返回新版本号与页面数据，页面切换延迟控制在 1s 内。

    注意：此处必须声明为同步端点，FastAPI 会将其放入线程池执行。
    若写成 async def，阻塞式 Condition.wait() 会挂死整个 uvicorn 事件循环。
    """
    if terminal_id:
        c.displays.heartbeat(terminal_id)
    timeout = int(c.settings.get("display.long_poll_timeout_sec", 25))
    snap = c.bus.wait_for(since, timeout)
    if snap is None:
        return {"code": 304, "message": "no change", "version": c.bus.version}
    return {"code": 0, "version": snap.version, "snapshot": snap.model_dump(mode="json")}


@router_display.get("/state")
async def state_current(c: HostContext = Depends(ctx)):
    """首次全量状态请求：获取当前页面编号与数据快照"""
    snap = c.bus.current()
    if snap is None:
        snap = c.display_svc.build(None)
        c.bus.publish(snap)
    return {"code": 0, "version": snap.version, "snapshot": snap.model_dump(mode="json")}


@router_display.post("/heartbeat")
async def display_heartbeat(body: DisplayHeartbeatIn, c: HostContext = Depends(ctx)):
    c.displays.heartbeat(body.terminal_id)
    return {"code": 0, "online": c.displays.online(body.terminal_id),
            "degrade": "voice_only" if not c.displays.online() else "normal"}


# ==========================================================================
# 四·五、整体业务流程核查 + 游客→人形机器人 消息
# ==========================================================================
router_verify = APIRouter(prefix="/api/verify", tags=["整体业务流程核查"])


class VisitorMessageIn(BaseModel):
    """游客对人形机器人说的购票意图。

    intent 取值：confirm（确认购票）/ cancel（取消）/ query（咨询）/ other（其他）。
    仅 intent=confirm 会解除严格核查模式下的「确认」门禁。
    session_id 缺省时取当前活动会话。
    """
    session_id: Optional[str] = None
    text: str = Field(default="", description="游客说的话（自然语言）")
    intent: Optional[str] = Field(default=None, description="confirm/cancel/query/other")


@router_verify.post("/visitor-message")
async def visitor_message(body: VisitorMessageIn, c: HostContext = Depends(ctx)):
    """游客把购票意图说给人形机器人，机器人上报至此。

    本接口只负责记录游客侧意图；是否放行「确认」由核查服务独立判定。
    intent=confirm 置位「游客已确认」，解除严格核查模式下的确认门禁。
    """
    sid = body.session_id or (c.sessions.active().session_id
                              if c.sessions.active() else None)
    st = c.verification.record_message(sid, body.text, body.intent)
    return {"code": 0, "data": st}


@router_verify.get("/state")
async def verify_state(c: HostContext = Depends(ctx)):
    """当前会话的整体业务流程核查清单 + 放行结论（供 /verify/ 界面）。"""
    s = c.sessions.active()
    checklist = c.verification.build_checklist(s)
    return {"code": 0, "data": checklist}


# ==========================================================================
# 五、声光报警
# ==========================================================================
router_alarm = APIRouter(prefix="/api/alarm", tags=["声光报警"])


@router_alarm.post("/trigger")
async def alarm_trigger(code: str, level: str = "L2", message: str = "",
                        session_id: Optional[str] = None,
                        c: HostContext = Depends(ctx)):
    rec = c.alarm.trigger(code, level, message, session_id)
    return {"code": 0, "data": rec}


@router_alarm.post("/cancel")
async def alarm_cancel(operator: str = "staff", c: HostContext = Depends(ctx)):
    """人工取消报警（取消动作记入日志）"""
    rec = c.alarm.cancel(operator)
    if rec is None:
        return {"code": 0, "message": "当前无激活报警"}
    return {"code": 0, "data": rec}


@router_alarm.get("/active")
async def alarm_active(c: HostContext = Depends(ctx)):
    return {"code": 0, "data": c.alarm.active(), "history": c.alarm.history(20)}


# ==========================================================================
# 六、运维管理端
# ==========================================================================
router_ops = APIRouter(prefix="/api/ops", tags=["运维管理"])


@router_ops.get("/metrics")
async def ops_metrics(c: HostContext = Depends(ctx)):
    return {"code": 0, "data": c.metrics()}


@router_ops.get("/devices")
async def ops_devices(c: HostContext = Depends(ctx)):
    return {"code": 0, "data": {"status": c.devices.status(),
                                "detail": c.devices.detail(),
                                "paper_remaining": c.devices.paper_remaining,
                                "paper_low": c.devices.paper_low()}}


@router_ops.post("/device/status")
async def ops_device_status(name: str, online: bool, note: str = "",
                            c: HostContext = Depends(ctx)):
    """联调用：注入设备在线状态（真实驱动接入前用于全链路验证）"""
    try:
        c.devices.set_status(name, online, note)
    except KeyError as e:
        raise HTTPException(ErrorCode.BAD_REQUEST, str(e))
    return {"code": 0, "data": c.devices.status()}


@router_ops.post("/device/paper/refill")
async def ops_paper_refill(c: HostContext = Depends(ctx)):
    c.devices.refill_paper()
    return {"code": 0, "paper_remaining": c.devices.paper_remaining}


@router_ops.get("/selftest")
async def ops_selftest(full: bool = True, c: HostContext = Depends(ctx)):
    return {"code": 0, "data": c.selfcheck.run(full=full)}


@router_ops.get("/config")
async def ops_config_get(c: HostContext = Depends(ctx)):
    return {"code": 0, "data": {"service": c.settings.get("service"),
                                "display": c.settings.get("display"),
                                "devices": c.settings.get("devices"),
                                "robot": c.settings.get("robot"),
                                "ticketing": c.settings.get("ticketing")}}


@router_ops.get("/rule")
async def ops_rule_get(c: HostContext = Depends(ctx)):
    return {"code": 0, "version": c.rules.version, "data": c.rules.data,
            "history": c.rules.history[-20:]}


@router_ops.post("/rule/reload")
async def ops_rule_reload(c: HostContext = Depends(ctx)):
    """规则文件热更新（9.8 节：修改后无需重启服务即可生效）"""
    changed = c.rules.load(force=True)
    return {"code": 0, "changed": changed, "version": c.rules.version}


@router_ops.post("/rule")
async def ops_rule_replace(request: Request, operator: str = "ops",
                           c: HostContext = Depends(ctx)):
    """整体替换业务规则文件（票种票价 / 适用条件 / 场次 / 按键 / 话术）"""
    body = await request.json()
    if not isinstance(body, dict) or "version" not in body:
        raise HTTPException(ErrorCode.BAD_REQUEST, "规则文件需包含 version 字段")
    v = c.rules.replace(body, operator)
    return {"code": 0, "version": v}
