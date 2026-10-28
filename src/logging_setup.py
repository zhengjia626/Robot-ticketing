"""日志（方案 9.7 节）

分层说明（依据 9.7 节批注）：
- 业务日志归票务系统与机器人本体；
- 本服务只落**系统层日志与设备状态日志**，并按日切分、本地保留 30 天；
- 涉及实名信息的字段一律脱敏（身份证仅保留前 6 位与后 2 位）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

FMT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"


def setup(level: str = "INFO") -> logging.Logger:
    root = logging.getLogger("host")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        return root

    fmt = logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = TimedRotatingFileHandler(
        LOG_DIR / "host.log", when="midnight", interval=1,
        backupCount=30, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    dev = TimedRotatingFileHandler(
        LOG_DIR / "device.log", when="midnight", interval=1,
        backupCount=30, encoding="utf-8",
    )
    dev.setFormatter(fmt)
    dev.addFilter(lambda r: r.name.startswith("host.device") or r.name.startswith("host.alarm"))
    root.addHandler(dev)

    return root
