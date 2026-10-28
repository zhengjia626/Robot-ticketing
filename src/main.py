"""售票主机后台服务 · 启动入口

本地调试：
    python -m src.main --port 8080 --reload

现场部署（Windows 10，随系统启动 + 看门狗）：
    python -m src.main --port 8080
    建议用 NSSM 注册为 Windows 服务，崩溃后 10s 内自动拉起（方案 9.2 节）
"""
from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main():
    ap = argparse.ArgumentParser(description="自贡彩灯节售票主机后台服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--reload", action="store_true", help="开发态热重载")
    args = ap.parse_args()

    uvicorn.run("src.app:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                reload=args.reload,
                log_level="info",
                access_log=True)


if __name__ == "__main__":
    main()
