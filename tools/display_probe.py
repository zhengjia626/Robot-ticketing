"""信息显示终端探针（9.5 节长轮询验证）

模拟 MatePad 终端：心跳上报 + 长轮询拉页面，打印每次页面切换与到达延迟，
用于验证「页面切换延迟控制在 1s 以内」与「终端离线后降级为纯语音引导」。

用法：
    python tools/display_probe.py                 # 持续监听 120s
    python tools/display_probe.py --duration 30
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_NAME = {
    "P-0": "待机欢迎页", "P-1": "需求确认页", "P-2": "身份核验页",
    "P-3": "订单确认页", "P-4": "扫码支付页", "P-5": "出票中页面",
    "P-6": "取票完成页", "P-7": "异常提示页",
}


def get(url: str, params=None, timeout=40):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"code": e.code}
    except Exception as e:  # noqa: BLE001
        return {"code": -1, "message": str(e)}


def post(url: str, body=None, params=None, timeout=10):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"code": -1, "message": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--terminal-id", default="matepad-01")
    ap.add_argument("--duration", type=int, default=120)
    args = ap.parse_args()

    base = args.url.rstrip("/")
    stop = threading.Event()

    def heartbeat_loop():
        while not stop.wait(10):
            post(f"{base}/api/heartbeat", {"terminal_id": args.terminal_id, "online": True})

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    first = get(f"{base}/api/state", timeout=5)
    since = first.get("version", 0)
    snap = first.get("snapshot", {})
    print(f"[首帧] v{since} 页面 {snap.get('page')} {PAGE_NAME.get(snap.get('page'), '')}")

    deadline = time.time() + args.duration
    print(f"开始长轮询监听 {args.duration}s（另开终端跑模拟器即可看到页面切换）…\n")
    while time.time() < deadline:
        t0 = time.time()
        r = get(f"{base}/api/state/long",
                {"since": since, "terminal_id": args.terminal_id}, timeout=30)
        dt = (time.time() - t0) * 1000
        if r.get("code") == 304:
            continue
        s = r.get("snapshot")
        if not s:
            continue
        since = r.get("version", since)
        print(f"[{time.strftime('%H:%M:%S')}] v{since} 延迟 {dt:6.0f}ms | "
              f"{s.get('state'):<4} {s.get('page')} {PAGE_NAME.get(s.get('page'), '')}")
        data = s.get("data") or {}
        for k in ("main_text", "tip", "amount", "reason", "message"):
            if data.get(k):
                print(f"           └─ {k}: {data[k]}")
    stop.set()
    print("\n监听结束")


if __name__ == "__main__":
    sys.exit(main())
