"""运行自检（方案 9.8 节）

三个执行时机：
1. 服务启动：全量自检，不通过则拒绝进入售票模式；
2. 每笔会话创建前：快速自检，确认关键外设可用；
3. 每日开园前：例行自检并生成自检报告。
"""
from __future__ import annotations

import logging
import platform
import shutil
import socket
import time
from typing import Any, Dict, List

logger = logging.getLogger("host.selfcheck")


class SelfCheck:
    def __init__(self, settings, devices, robot, display_online_probe):
        self.settings = settings
        self.devices = devices
        self.robot = robot
        self.display_online_probe = display_online_probe

    # ---------- 单项 ----------
    def _items(self, full: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        # 关键外设
        for name, label in [("printer", "门票打印机 ZT211"),
                            ("id_reader", "身份证读卡器"),
                            ("scanner", "支付二维码扫码器"),
                            ("keypad", "定制键盘")]:
            ok = bool(self.devices.status().get(name))
            out.append({
                "item": label, "key": name, "ok": ok,
                "advice": "" if ok else "检查 USB 连接与供电（H1–H4），确认设备在系统中可见",
            })

        # 纸张余量
        pr = self.devices.paper_remaining
        low = self.devices.paper_low()
        out.append({
            "item": "门票纸余量", "key": "paper", "ok": not low,
            "value": pr,
            "advice": "" if not low else f"剩余 {pr} 张，请及时换纸（一次换纸 500 张）",
        })

        # 机器人连通性
        r_ok = self.robot.online
        out.append({
            "item": "人形机器人在线", "key": "robot", "ok": r_ok,
            "value": f"电量 {self.robot.battery}%" if self.robot.battery is not None else "",
            "advice": "" if r_ok else f"检查机器人网络（192.168.10.20）与心跳，"
                                      f"超过 {self.robot.heartbeat_timeout}s 未上报",
        })

        # 显示终端
        d_ok = bool(self.display_online_probe())
        out.append({
            "item": "信息显示终端在线", "key": "display", "ok": d_ok,
            "advice": "" if d_ok else "终端离线不影响售票，将降级为纯语音引导（9.5 节）",
        })

        if full:
            # 磁盘余量（日志保留 30 天）
            try:
                total, used, free = shutil.disk_usage(str(self.settings.get("runtime.data_dir", ".")))
                free_gb = free / (1024 ** 3)
                out.append({"item": "磁盘可用空间", "key": "disk", "ok": free_gb >= 5,
                            "value": f"{free_gb:.1f} GB",
                            "advice": "" if free_gb >= 5 else "清理历史日志，保证 30 天留存"})
            except Exception as e:  # noqa: BLE001
                out.append({"item": "磁盘可用空间", "key": "disk", "ok": False,
                            "advice": f"检查失败：{e}"})
            out.append({"item": "操作系统", "key": "os", "ok": True,
                        "value": f"{platform.system()} {platform.release()}"})
            out.append({"item": "主机名", "key": "hostname", "ok": True,
                        "value": socket.gethostname()})
        return out

    # ---------- 入口 ----------
    def run(self, full: bool = True) -> Dict[str, Any]:
        items = self._items(full)
        failed = [i for i in items if not i["ok"]]
        # 「显示终端离线」属可降级项，不阻断进入售票模式
        blocking = [i for i in failed if i["key"] != "display"]
        result = {
            "ts": time.time(),
            "mode": "full" if full else "quick",
            "passed": not blocking,
            "sellable": not blocking,
            "degraded": any(i["key"] == "display" for i in failed),
            "items": items,
            "failed_count": len(failed),
            "blocking_count": len(blocking),
            "conclusion": "自检通过，可进入售票模式" if not blocking
                          else f"自检未通过（{len(blocking)} 项阻断），拒绝进入售票模式，"
                               f"机器人改为引导游客至人工窗口",
        }
        logger.info("自检[%s]：%s", result["mode"], result["conclusion"])
        return result

    def quick(self) -> Dict[str, Any]:
        return self.run(full=False)
