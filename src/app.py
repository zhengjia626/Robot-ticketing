"""FastAPI 应用装配（方案 9.2 接口层）

接口层对外暴露三类接口：机器人侧、显示终端侧、运维管理端。
统一 JSON 报文 / 统一错误码 / OpenAPI 自描述（/docs）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api.routers import (
    router_alarm, router_display, router_ops,
    router_robot, router_session, router_ticket, router_verify,
)
from .core.context import get_context
from .logging_setup import setup

logger = logging.getLogger("host.app")

DESCRIPTION = """
自贡彩灯节 · 售票主机后台服务

**定位**（方案 v2.4.1 第 9.1 节）：不替代票务系统，以「旁路协同」方式在
票务系统客户端与机器人、外围设备、显示终端之间建立可控的状态通道。

**关键约束**
- 票务系统 → 后台服务为**单向推送**，后台不向票务系统下发任何数据；
- 出票结果不采信票务系统，必须由机器人视觉独立识别；
- 后台服务不启动时，不影响票务系统与机器人正常作业。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup("INFO")
    c = get_context()
    report = c.bootstrap()
    logger.info("服务启动完成：%s", report["conclusion"])
    yield
    c.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="售票主机后台服务",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        logger.exception("未处理异常：%s", exc)
        return JSONResponse(status_code=500,
                            content={"code": 500, "message": f"内部错误：{exc}"})

    app.include_router(router_ticket)
    app.include_router(router_session)
    app.include_router(router_robot)
    app.include_router(router_display)
    app.include_router(router_alarm)
    app.include_router(router_ops)
    app.include_router(router_verify)

    # 信息显示终端（MatePad 11.5 横屏）：http://<主机>:8080/terminal/
    WEB_DIR = Path(__file__).resolve().parents[1] / "web"
    if WEB_DIR.exists():
        app.mount("/terminal", StaticFiles(directory=str(WEB_DIR), html=True),
                  name="terminal")

        @app.get("/simulator", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/simulator/", response_class=HTMLResponse, include_in_schema=False)
        async def simulator_page():
            """票务系统模拟器控制台：手动推送 7 类消息，右侧实时显示终端画面"""
            return FileResponse(WEB_DIR / "simulator.html")

        # 模块一：票务系统模块（设备控制台）—— 与后台服务同机部署
        @app.get("/ticketing", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/ticketing/", response_class=HTMLResponse, include_in_schema=False)
        async def ticketing_page():
            return FileResponse(WEB_DIR / "ticketing.html")

        # 模块二子页：机器人通信接口（预留）调试台
        @app.get("/robot", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/robot/", response_class=HTMLResponse, include_in_schema=False)
        async def robot_page():
            return FileResponse(WEB_DIR / "robot.html")

        # 模块二子页：整体业务流程核查 + 游客→人形机器人 信息输入
        @app.get("/verify", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/verify/", response_class=HTMLResponse, include_in_schema=False)
        async def verify_page():
            return FileResponse(WEB_DIR / "verify.html")

    @app.get("/", response_class=HTMLResponse, tags=["运维管理"])
    async def index():
        """运维看板：会话状态 / 设备 / 报警"""
        return _INDEX_HTML

    @app.get("/health", tags=["运维管理"])
    async def health():
        c = get_context()
        return {"code": 0, "status": "ok",
                "sellable": bool(c.selfcheck_report and c.selfcheck_report["sellable"]),
                "display_online": c.displays.online(),
                "robot_online": c.robot.online}

    return app


_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>售票主机后台服务 · 运维看板</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#0f1419;color:#e6edf3;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif}
header{padding:16px 24px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:16px}
h1{margin:0;font-size:18px}
.wrap{padding:20px 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px}
.card h2{margin:0 0 12px;font-size:14px;color:#8b949e;font-weight:600}
.kv{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #21262d}
.kv span:last-child{color:#58a6ff}
pre{background:#0d1117;padding:12px;border-radius:6px;overflow:auto;max-height:420px;font-size:12px}
.bad{color:#f85149}.good{color:#3fb950}.warn{color:#d29922}
a{color:#58a6ff}
</style></head><body>
<header><h1>售票主机后台服务 · 运维看板</h1>
<a href="/ticketing/" target="_blank">票务系统模块</a>
<a href="/terminal/" target="_blank">显示屏模块</a>
<a href="/robot/" target="_blank">机器人接口</a>
<a href="/verify/" target="_blank">核查服务</a>
<a href="/simulator/" target="_blank">三合一演示</a>
<a href="/docs" target="_blank">OpenAPI 文档</a>
<a href="/api/ops/metrics" target="_blank">指标</a>
<a href="/api/ops/selftest" target="_blank">自检</a></header>
<div class="wrap">
  <div class="card"><h2>会话状态</h2><div id="s1">加载中…</div></div>
  <div class="card"><h2>设备状态</h2><div id="s2">加载中…</div></div>
  <div class="card"><h2>机器人 / 终端</h2><div id="s3">加载中…</div></div>
  <div class="card"><h2>报警</h2><div id="s4">加载中…</div></div>
  <div class="card" style="grid-column:1/-1"><h2>最近推送</h2><pre id="s5">加载中…</pre></div>
</div>
<script>
function kv(o){return Object.entries(o||{}).map(([k,v])=>
  `<div class="kv"><span>${k}</span><span>${typeof v==='object'?JSON.stringify(v):v}</span></div>`).join('')}
async function poll(){
  try{
    const m = await (await fetch('/api/ops/metrics')).json();
    const d = m.data;
    document.getElementById('s1').innerHTML = kv({
      '会话总数':d.session_total,'已完成':d.session_completed,
      '平均时长(s)':d.avg_session_sec,'当前状态分布':JSON.stringify(d.session_by_state),
      '规则版本':d.rules_version,'运行时长(s)':d.uptime_sec,'可售票':d.selfcheck.sellable
    });
    document.getElementById('s2').innerHTML = kv({...d.devices,'纸张剩余':d.paper_remaining});
    document.getElementById('s3').innerHTML = kv({...d.robot,'显示终端在线':d.display_online});
    const a = d.alarm_active;
    document.getElementById('s4').innerHTML = a
      ? `<div class="bad">${a.level} ${a.code} ${a.message}</div>` : '<div class="good">无激活报警</div>';
    const st = await (await fetch('/api/session/state')).json();
    document.getElementById('s5').textContent = JSON.stringify(st,null,2);
  }catch(e){document.getElementById('s1').textContent='轮询失败 '+e}
}
poll(); setInterval(poll,3000);
</script></body></html>"""
