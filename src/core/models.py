"""统一数据模型（方案 9.9 节接口清单）

约定：
- 全部对外报文为 JSON，字段命名 snake_case。
- 所有跨系统报文带 session_id / order_id，便于全链路追踪。
- 票务系统 → 后台为**单向推送**，后台不向票务下发任何数据。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ==========================================================================
# 一、会话与状态
# ==========================================================================
class SessionState(str, Enum):
    """方案 4.9 节 状态迁移：S0–S11 正常链路 + E1–E4 异常链路"""

    S0 = "S0"    # 空闲待机
    S1 = "S1"    # 会话已建立
    S2 = "S2"    # 需求采集中
    S3 = "S3"    # 待游客确认
    S4 = "S4"    # 待放入证件
    S5 = "S5"    # 实名已锁定
    S6 = "S6"    # 订单待确认
    S7 = "S7"    # 订单待支付
    S8 = "S8"    # 支付已确认
    S9 = "S9"    # 出票中
    S10 = "S10"  # 票据已就位
    S11 = "S11"  # 已完成（终态）

    E1 = "E1"    # 会话超时关闭
    E2 = "E2"    # 支付状态不明
    E3 = "E3"    # 出票或取递失败
    E4 = "E4"    # 人工介入（终态）


TERMINAL_STATES = {SessionState.S11, SessionState.E1, SessionState.E2,
                   SessionState.E3, SessionState.E4}


class Stage(str, Enum):
    """方案第四章 七阶段"""
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    P6 = "P6"
    P7 = "P7"


STATE_STAGE_MAP: Dict[str, str] = {
    "S0": "P1", "S1": "P1", "S2": "P2", "S3": "P2", "S4": "P3",
    "S5": "P3", "S6": "P4", "S7": "P5", "S8": "P5", "S9": "P6",
    "S10": "P7", "S11": "P7",
}


# ==========================================================================
# 二、票务系统 → 后台服务（单向推送，7 类）
# ==========================================================================
class PushBase(BaseModel):
    """推送公共字段"""
    session_id: Optional[str] = Field(None, description="会话号，首条推送可为空")
    order_id: Optional[str] = Field(None, description="订单号")
    ts: Optional[datetime] = Field(None, description="票务系统事件时间戳")
    seq: Optional[int] = Field(None, description="推送序号，用于去重与乱序检测")


class KeyPressedPush(PushBase):
    """① 按键后的场次与票种信息"""
    type: Literal["key_pressed"] = "key_pressed"
    key: str = Field(..., description="K1–K7")
    key_cap: Optional[str] = Field(None, description="键帽标识，如 成人票")
    session_code: Optional[str] = Field(None, description="场次 day / night")
    ticket_type: Optional[str] = Field(None, description="票种 student / adult / senior / zero")
    qty: Optional[int] = Field(None, description="当前已录入票数")
    echo_ok: Optional[bool] = Field(None, description="键位回读是否与预期一致")


class IdCardReadPush(PushBase):
    """② 游客刷证后的身份证信息"""
    type: Literal["id_card_read"] = "id_card_read"
    id_masked: Optional[str] = Field(None, description="脱敏证件号，前6后2")
    name_masked: Optional[str] = Field(None, description="脱敏姓名")
    age: Optional[int] = Field(None, description="识别年龄")
    eligible_type: Optional[str] = Field(None, description="票种资格判定结果")
    read_status: Literal["success", "fail", "timeout"] = "success"
    locked: bool = Field(False, description="实名信息是否已锁定")
    card_count: Optional[int] = Field(None, description="已刷证件数")


class TicketPricePush(PushBase):
    """③ 票种票数与票价信息"""
    type: Literal["ticket_price"] = "ticket_price"
    session_code: Optional[str] = Field(None)
    items: List[Dict[str, Any]] = Field(default_factory=list,
                                        description="[{ticket_type,name,qty,unit_price,amount}]")
    total_qty: int = 0
    total_amount: float = 0.0
    remaining: Optional[int] = Field(None, description="该场次可售余量")


class PayResultPush(PushBase):
    """④ 扫码支付成功或失败信息"""
    type: Literal["pay_result"] = "pay_result"
    status: Literal["success", "fail", "unknown"] = "unknown"
    trade_no: Optional[str] = Field(None, description="支付流水号")
    amount: Optional[float] = None
    fail_reason: Optional[str] = None


class ConfirmCancelPush(PushBase):
    """⑤ 确认与取消按键消息"""
    type: Literal["confirm_cancel"] = "confirm_cancel"
    key: Literal["confirm", "cancel"] = "confirm"
    effect: Optional[str] = Field(None, description="退格 / 清零 / 锁定 / 中止会话")


class PrintCommandPush(PushBase):
    """⑥ 打印指令下发信息"""
    type: Literal["print_command"] = "print_command"
    qty: int = 1
    ticket_sns: List[str] = Field(default_factory=list)
    action: Literal["print", "reprint"] = "print"


class SoldOutPush(PushBase):
    """⑦ 场次票售完消息"""
    type: Literal["session_sold_out"] = "session_sold_out"
    session_code: str = Field(..., description="day / night")
    night_available: bool = Field(False, description="夜场是否仍有票")



# ==========================================================================
# 三、机器人 → 后台服务
# ==========================================================================
class PrintVerifyIn(BaseModel):
    """机器人回传实际出票状态（9.9 /api/print/verify）

    背景：票务系统下发打印指令后即认为出票成功，不识别打印机异常，
    因此必须由机器人视觉独立识别实际出票状态与数量。
    """
    session_id: str
    order_id: Optional[str] = None
    arrived: bool = Field(..., description="票据是否落到托盘")
    actual_qty: int = Field(0, description="机器人视觉识别到的实际出票张数")
    expected_qty: Optional[int] = Field(None, description="订单张数")
    paper_empty: bool = Field(False, description="是否识别到缺纸")
    paper_remaining: Optional[int] = Field(None, description="纸仓剩余张数估计")
    vision_confidence: float = Field(0.0, ge=0.0, le=1.0)


class RobotEventIn(BaseModel):
    """机器人异常与告警事件异步推送（9.9 robot/alert）"""
    session_id: Optional[str] = None
    event: str = Field(..., description="如 robot_low_battery / arm_fault / estop")
    level: Literal["L1", "L2", "L3"] = "L2"
    detail: Optional[Dict[str, Any]] = None


class RobotHeartbeatIn(BaseModel):
    robot_id: str = "cruzr-s2-01"
    battery: Optional[int] = None
    skill_state: Optional[str] = None
    online: bool = True


# ==========================================================================
# 四、显示终端
# ==========================================================================
class DisplayHeartbeatIn(BaseModel):
    terminal_id: str = "matepad-01"
    online: bool = True


class StateSnapshot(BaseModel):
    """推送给显示终端的状态快照（终端只渲染，不做业务判断）"""
    version: int
    session_id: Optional[str]
    state: str
    stage: Optional[str]
    page: str
    page_name: str
    data: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


# ==========================================================================
# 五、统一响应与错误码
# ==========================================================================
class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Optional[Any] = None


class ErrorCode:
    """统一错误码（方案 9.2 接口层要求）"""
    OK = 0
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    GATE_REJECTED = 409      # 门禁未通过，拒绝状态迁移
    DUPLICATE_IGNORED = 208  # 幂等拦截：重复事件已忽略
    TIMEOUT = 408
    DEVICE_OFFLINE = 503
    INTERNAL = 500
    TICKETING_UPSTREAM = 502
