"""票务系统推送模拟器（开发联调用）

用途：在真实票务系统接口尚未开放前，模拟其按 P1–P7 顺序向后台服务
单向推送 7 类状态消息，验证状态机门禁、页面切换、报警与异常分支。

用法：
    python tools/ticketing_simulator.py                    # 正常全流程
    python tools/ticketing_simulator.py --scenario pay_unknown
    python tools/ticketing_simulator.py --scenario print_fail
    python tools/ticketing_simulator.py --scenario sold_out
    python tools/ticketing_simulator.py --scenario id_short   # 证件数少于票数
    python tools/ticketing_simulator.py --scenario echo_fail  # 键位回读不一致
    python tools/ticketing_simulator.py --url http://192.168.10.10:8080
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_URL = "http://127.0.0.1:8080"

PAGE_NAME = {
    "P-0": "待机欢迎页", "P-1": "需求确认页", "P-2": "身份核验页",
    "P-3": "订单确认页", "P-4": "扫码支付页", "P-5": "出票中页面",
    "P-6": "取票完成页", "P-7": "异常提示页",
}


class Sim:
    def __init__(self, url: str, verbose: bool = True):
        self.url = url.rstrip("/")
        self.verbose = verbose
        self.session_id: Optional[str] = None

    # ---------- HTTP ----------
    def _req(self, method: str, path: str, body: Optional[Dict] = None,
             params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self.url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"code": e.code, "message": raw}

    def post(self, path: str, body: Optional[Dict] = None,
             params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._req("POST", path, body, params)

    def get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._req("GET", path, None, params)

    # ---------- 展示 ----------
    def step(self, title: str, resp: Dict[str, Any]):
        if not self.verbose:
            return
        code = resp.get("code", 0)
        flag = "OK " if code in (0, 208) else "!! "
        state = resp.get("state", "")
        page = resp.get("page", "")
        page_txt = f"{page} {PAGE_NAME.get(page, '')}" if page else ""
        msg = resp.get("message", "")
        print(f"{flag} {title:<28} → code={code:<4} {state:<4} {page_txt:<18} {msg}")
        if code not in (0, 208):
            print(f"     └─ 详情：{json.dumps(resp, ensure_ascii=False)[:300]}")

    def show_session(self):
        r = self.get("/api/session/state")
        s = r.get("session")
        if not s:
            print("     └─ 无活动会话")
            return
        print(f"     └─ 会话 {s['session_id']} | 状态 {s['state']} | 场次 {s['session_code']} "
              f"| 票数 {s['total_qty']} | 金额 {s['total_amount']} | 订单 {s['order_id']}")

    # ---------- 会话清理 ----------
    def reset(self):
        """关闭残留会话，保证场景之间互不干扰"""
        r = self.get("/api/session/state")
        s = r.get("session")
        if s:
            self.post("/api/session/close", None,
                      {"session_id": s["session_id"], "reason": "模拟器清理"})
        self.session_id = None
        self.post("/api/alarm/cancel", None, {"operator": "simulator"})

    # ---------- 推送封装 ----------
    def push(self, ptype: str, **kw) -> Dict[str, Any]:
        body = {"type": ptype, "session_id": self.session_id, **kw}
        return self.post("/api/ticket/push", body)


def run_normal(sim: Sim):
    print("\n=== 场景：标准散客售票（日场成人票 2 张）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话·设备自检", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)

    sim.step("P2 按【日场票】K6", sim.push("key_pressed", key="K6", session_code="day"))
    sim.step("P2 按【成人票】K4 ×1", sim.push("key_pressed", key="K4", ticket_type="adult"))
    sim.step("P2 按【成人票】K4 ×2", sim.push("key_pressed", key="K4", ticket_type="adult"))
    sim.step("P2 票务回传票价", sim.push(
        "ticket_price", session_code="day", total_amount=300, remaining=12000,
        items=[{"ticket_type": "adult", "name": "成人票", "qty": 2,
                "unit_price": 150, "amount": 300}]))
    sim.step("P2→P3 按【确认】锁库存", sim.push("confirm_cancel", key="confirm"))

    sim.step("P3 刷第 1 张身份证", sim.push(
        "id_card_read", id_masked="510302199001011234", age=36,
        eligible_type="adult", locked=True, card_count=1))
    sim.step("P3 刷第 2 张身份证", sim.push(
        "id_card_read", id_masked="510302199202025678", age=34,
        eligible_type="adult", locked=True, card_count=2))
    sim.step("P3 生成最终订单", sim.push(
        "ticket_price", order_id="TZ20261027000123", session_code="day",
        total_amount=300, remaining=11998,
        items=[{"ticket_type": "adult", "name": "成人票", "qty": 2,
                "unit_price": 150, "amount": 300}]))
    sim.step("P4 按【确认】锁票种数量", sim.push("confirm_cancel", key="confirm"))

    sim.step("P5 支付成功", sim.push("pay_result", status="success",
                                     trade_no="4200001234202610270001", amount=300))
    sim.step("P6 打印指令下发", sim.push("print_command", qty=2, action="print"))
    sim.step("P6 机器人视觉确认出票", sim.post(
        "/api/print/verify",
        {"session_id": sim.session_id, "arrived": True, "actual_qty": 2,
         "expected_qty": 2, "paper_empty": False, "paper_remaining": 480,
         "vision_confidence": 0.93}))
    sim.step("P7 游客取票·归档复位", sim.post(
        "/api/session/ticket-taken", None, {"session_id": sim.session_id}))
    sim.show_session()


def run_pay_unknown(sim: Sim):
    print("\n=== 场景：支付状态不明（E42/E43，禁止重复扣款）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)
    sim.push("key_pressed", key="K6", session_code="day")
    sim.push("key_pressed", key="K4", ticket_type="adult")
    sim.step("P2 票务回传票价", sim.push("ticket_price", session_code="day",
                                         total_amount=150, remaining=8000,
                                         items=[{"ticket_type": "adult", "name": "成人票",
                                                 "qty": 1, "unit_price": 150, "amount": 150}]))
    sim.push("confirm_cancel", key="confirm")
    sim.push("id_card_read", id_masked="510302199001011234", age=36,
             eligible_type="adult", locked=True, card_count=1)
    sim.push("ticket_price", order_id="TZ20261027000124", total_amount=150,
             items=[{"ticket_type": "adult", "name": "成人票", "qty": 1,
                     "unit_price": 150, "amount": 150}])
    sim.push("confirm_cancel", key="confirm")
    sim.step("P5 支付状态不明 → E2", sim.push("pay_result", status="unknown"))
    sim.step("P5 重复推送成功事件（幂等）", sim.push(
        "pay_result", status="success", trade_no="4200001234202610270002", amount=150))
    sim.show_session()
    a = sim.get("/api/alarm/active")
    print(f"     └─ 激活报警：{json.dumps(a.get('data'), ensure_ascii=False)}")
    sim.post("/api/alarm/cancel", None, {"operator": "staff"})
    print("     └─ 已人工取消报警")


def run_print_fail(sim: Sim):
    print("\n=== 场景：下发打印指令后实际未出票（E59）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)
    sim.push("key_pressed", key="K7", session_code="night")
    sim.push("key_pressed", key="K3", ticket_type="student")
    sim.step("P2 票务回传票价", sim.push("ticket_price", session_code="night",
                                         total_amount=80, remaining=5000,
                                         items=[{"ticket_type": "student", "name": "学生票",
                                                 "qty": 1, "unit_price": 80, "amount": 80}]))
    sim.push("confirm_cancel", key="confirm")
    sim.push("id_card_read", id_masked="510302201001011234", age=16,
             eligible_type="student", locked=True, card_count=1)
    sim.push("ticket_price", order_id="TZ20261027000125", total_amount=80,
             items=[{"ticket_type": "student", "name": "学生票", "qty": 1,
                     "unit_price": 80, "amount": 80}])
    sim.push("confirm_cancel", key="confirm")
    sim.push("pay_result", status="success", trade_no="4200001234202610270003", amount=80)
    sim.step("P6 打印指令下发", sim.push("print_command", qty=1))
    sim.step("P6 视觉未识别到票据", sim.post(
        "/api/print/verify",
        {"session_id": sim.session_id, "arrived": False, "actual_qty": 0,
         "expected_qty": 1, "paper_empty": False, "vision_confidence": 0.9}))
    sim.step("P6 重试后仍未出票 → E3", sim.post(
        "/api/print/verify",
        {"session_id": sim.session_id, "arrived": False, "actual_qty": 0,
         "expected_qty": 1, "paper_empty": False, "vision_confidence": 0.9}))
    sim.show_session()
    print(f"     └─ 激活报警：{json.dumps(sim.get('/api/alarm/active').get('data'), ensure_ascii=False)}")


def run_sold_out(sim: Sim):
    print("\n=== 场景：场次售罄（E39）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)
    sim.push("key_pressed", key="K6", session_code="day")
    sim.push("key_pressed", key="K4", ticket_type="adult")
    sim.step("P2 日场售罄推送", sim.push("session_sold_out", session_code="day",
                                         night_available=True))
    sim.show_session()


def run_id_short(sim: Sim):
    print("\n=== 场景：证件数少于需求票数（E29）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)
    sim.push("key_pressed", key="K6", session_code="day")
    sim.push("key_pressed", key="K4", ticket_type="adult")
    sim.push("key_pressed", key="K4", ticket_type="adult")
    sim.push("ticket_price", session_code="day", total_amount=300, remaining=9000,
             items=[{"ticket_type": "adult", "name": "成人票", "qty": 2,
                     "unit_price": 150, "amount": 300}])
    sim.push("confirm_cancel", key="confirm")
    sim.step("P3 只刷 1 张（需求 2 张）", sim.push(
        "id_card_read", id_masked="510302199001011234", age=36,
        eligible_type="adult", locked=True, card_count=1))
    # 游客只有一张证件：每次提醒后仍刷同一张证，第 3 次达上限 → 按已核验锁定
    sim.step("P3 提醒 2/3（仍刷同一张）", sim.push(
        "id_card_read", id_masked="510302199001011234", age=36,
        eligible_type="adult", locked=True, card_count=1))
    sim.step("P3 提醒 3/3 → 按已核验锁定", sim.push(
        "id_card_read", id_masked="510302199001011234", age=36,
        eligible_type="adult", locked=True, card_count=1))
    sim.show_session()


def run_echo_fail(sim: Sim):
    print("\n=== 场景：键位回读不一致限次重试（E32）===\n")
    r = sim.post("/api/session/create")
    sim.session_id = r.get("session_id")
    sim.step("P1 创建会话", r)
    if not sim.session_id:
        print(f"会话创建失败：{json.dumps(r, ensure_ascii=False)}")
        sys.exit(2)
    for i in range(1, 5):
        sim.step(f"P2 按【成人票】K4（回读不一致 第 {i} 次）",
                 sim.push("key_pressed", key="K4", ticket_type="adult", echo_ok=False))
    sim.show_session()
    a = sim.get("/api/alarm/active")
    print(f"     └─ 激活报警：{json.dumps(a.get('data'), ensure_ascii=False)}")


SCENARIOS = {
    "normal": run_normal,
    "pay_unknown": run_pay_unknown,
    "print_fail": run_print_fail,
    "sold_out": run_sold_out,
    "id_short": run_id_short,
    "echo_fail": run_echo_fail,
}


def main():
    ap = argparse.ArgumentParser(description="票务系统推送模拟器")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--scenario", default="normal", choices=list(SCENARIOS))
    ap.add_argument("--all", action="store_true", help="依次跑完全部场景")
    args = ap.parse_args()

    try:
        health = json.loads(urllib.request.urlopen(
            f"{args.url}/health", timeout=3).read().decode())
        print(f"服务健康：{health}")
    except Exception as e:  # noqa: BLE001
        print(f"无法连接服务 {args.url}：{e}")
        print("请先启动：python -m src.main --port 8080")
        sys.exit(1)

    sim = Sim(args.url)
    sim.reset()
    if args.all:
        for name, fn in SCENARIOS.items():
            sim.reset()
            fn(sim)
            time.sleep(0.5)
    else:
        SCENARIOS[args.scenario](sim)

    print("\n--- 最终指标 ---")
    print(json.dumps(sim.get("/api/ops/metrics").get("data"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
