# 自贡彩灯节 · 售票主机后台服务

> 对齐文档：`自贡彩灯节机器人售票技术方案20261025_v2.4.1.docx` 第九章
> 技术栈：Python 3.11+ / FastAPI / Uvicorn　|　部署目标：Windows 10 售票主机 `192.168.10.10:8080`

---

## 1. 定位与边界（方案 9.1 节）

**旁路协同，不替代票务系统。** 后台服务只在票务系统客户端与机器人、外围设备、显示终端之间
建立一条可控的状态通道，把依赖人工判断的环节转换为可判定、可记录、可回放的确定性流程。

| 属于本服务 | 不属于本服务 |
|---|---|
| 会话管理与状态门禁 | 票务业务规则实现 |
| 状态判定与联动 | 票款结算与对账 |
| 外设状态采集 | 票务数据库写入 |
| 页面与语音内容推送 | 支付通道接入 |
| 系统层日志与运行监控 | 机器人动作编排（机器人本体维护） |
| 配置与自检 | 票价计算（票务系统返回为准） |

**三条硬约束（务必遵守）**

1. **票务系统 → 后台服务为单向推送**，后台不向票务系统下发任何数据；
   票务系统的输入只来自定制键盘、付款扫码器、身份证读卡器。
2. **出票结果不采信票务系统**。票务下发打印指令后即认为成功，不识别打印机异常，
   必须由机器人视觉独立识别实际出票张数与缺纸状态（`/api/print/verify`）。
3. **后台服务不启动时，不得影响票务系统与机器人正常作业**（方案 9.8 节批注）。

> ⚠️ 与 `子系统交互设计/子系统拆解方法论与分层建模规范.md` 的冲突：
> 该文档写的是「Host 代理 Ticketing 全部交易能力（锁库存/下单/支付/打印）+ 双向 RESTful + Idempotency-Key」。
> **本实现按技术方案 v2.4.1 落地**（单向推送、旁路协同），方法论文档降级为架构基线参考。
> 如需改回代理模式，改动面集中在 `src/services/ticketing_bridge.py` 与 `src/core/state_machine.py`。

---

## 2. 目录结构

```
售票主机后台服务开发/
├── config/
│   ├── app.yaml          服务配置（端口/超时/设备/机器人/报警/日志）
│   └── rules.json        业务规则文件（票种票价/场次/键位/话术/超时重试）——支持热更新
├── src/
│   ├── main.py           启动入口
│   ├── app.py            FastAPI 装配 + 运维看板
│   ├── logging_setup.py  日志（按日切分，保留 30 天）
│   ├── core/
│   │   ├── config.py     配置与规则加载（mtime 热更新 + 版本留痕）
│   │   ├── models.py     统一数据模型与错误码
│   │   ├── state_machine.py  S0–S11 状态机 + 门禁判定表
│   │   ├── session.py    会话上下文 / 幂等 / 超时清扫 / 崩溃恢复
│   │   ├── context.py    运行时上下文装配
│   │   └── ...
│   ├── services/
│   │   ├── ticketing_bridge.py  票务状态对接 + 状态联动引擎（核心）
│   │   ├── display_service.py   8 套终端页面决策
│   │   ├── robot_gateway.py     机器人转发网关
│   │   ├── alarm_service.py     声光报警
│   │   ├── device_service.py    外设状态与到位传感器
│   │   └── selfcheck.py         三级自检
│   └── api/routers.py    全部对外 REST 接口
├── web/
│   ├── index.html / terminal.css / terminal.js    模块三：信息显示终端（8 套页面）
│   ├── ticketing.html / .css / .js                模块一：票务系统模块（设备控制台）
│   ├── robot.html / .css / .js                    模块二子页：机器人通信接口（预留）调试台
│   ├── verify.html / .css / .js                    模块二子页：整体业务流程核查 + 游客→机器人 信息输入
│   └── simulator.html / .css / .js                三合一演示页（模块一+三同屏）
├── tools/
│   ├── ticketing_simulator.py  票务系统推送模拟器（6 类场景，命令行）
│   ├── TicketingSimulator.postman_collection.json  Postman 集合（29 个请求）
│   └── display_probe.py        显示终端长轮询探针
├── data/    会话持久化（崩溃恢复）
└── logs/    host.log / device.log
```

---

## 2.1 三模块部署架构（按真实应用场景拆分）

将"模拟器"按现场部署形态拆分为三个可独立部署的模块（详见 `部署说明_三模块.md`）：

| 模块 | 职责 | 形态 | 部署 |
|---|---|---|---|
| **① 票务系统模块** | 连接键盘 / 身份证扫描器 / 付款码扫描器 / 打印机；实现输入、身份识别、扫码、凭证打印指令下发 | 浏览器控制台 `/ticketing/` | 与模块二同机 |
| **② 后台服务模块** | 接收票务单向推送并驱动状态机；推送显示屏页面；**预留人形机器人通信接口** | Python 服务（FastAPI） | 主机 PC |
| **③ 显示屏界面模块** | HTTP 页面实时展示各阶段页面与状态 | 浏览器页面 `/terminal/` | 平板（MatePad 等） |

**部署约束**：模块一与模块二部署在**同一台主机 PC**；模块三运行于**平板浏览器**；三者处于**同一局域网**，通过内部 HTTP/JSON 通信。后台服务是中枢——模块一向其推送 7 类消息，其向模块三长轮询推送页面，并向机器人（预留）转发状态。

```
模块一(票务控制台) ──POST /api/ticket/push──▶ 模块二(后台服务) ──长轮询──▶ 模块三(显示屏)
                                                  │
                                                  └──POST /api/host/forward──▶ 人形机器人(预留)
```

机器人接口已预留：`/api/robot/heartbeat`、`/api/robot/alert`、`/api/print/verify`、`/api/session/ticket-taken` 为机器人→后台入站；后台→机器人出站为 `POST {robot.base_url}/api/host/forward`（事件见 `forward_events`）。查看契约 `GET /api/robot/interface`，查看实际转发 `GET /api/robot/status` 的 `forward_log`。

**确认链路门禁（现场真实形态）**：游客把确认意图**说给人形机器人** → 机器人操作票务系统**外接键盘 K2** → 票务系统收到按键即视为「机器人已收到游客确认」，**不对「该不该确认」做门禁判断**；该门禁由独立的「整体业务流程核查服务」（`VerificationService`）判定。核查项：① 票价已回传（屏显金额）；② 游客已向人形机器人确认（`POST /api/verify/visitor-message` 上报 `intent=confirm`）。核查页面：`/verify/`。为兼容旧演示链路，本会话从未上报过游客消息时采用「宽松回退」（不强制②）；一旦上报过消息即进入严格模式。详见 `src/services/verification.py`。

---

## 3. 快速开始

```bash
pip install -r requirements.txt

# 启动（开发态）
python -m src.main --port 8080

# 另开终端：跑全流程（6 类场景）
python tools/ticketing_simulator.py --all
python tools/ticketing_simulator.py --scenario pay_unknown

# 另开终端：显示终端长轮询探针
python tools/display_probe.py --duration 60
```

- 交互文档：<http://127.0.0.1:8080/docs>
- 运维看板：<http://127.0.0.1:8080/>
- 健康检查：`GET /health`

### 3.1 三模块访问地址

| 模块 | 地址 | 部署位置 |
|---|---|---|
| ① 票务系统模块（设备控制台） | <http://127.0.0.1:8080/ticketing/> | 与后台同机 |
| ② 后台服务（运维看板 / 接口） | <http://127.0.0.1:8080/> · <http://127.0.0.1:8080/docs> | 主机 PC |
| ② 子页 · 机器人通信接口（预留） | <http://127.0.0.1:8080/robot/> | 主机 PC |
| ② 子页 · 整体业务流程核查服务 | <http://127.0.0.1:8080/verify/> | 主机 PC |
| ③ 显示屏界面模块 | <http://127.0.0.1:8080/terminal/?tid=PAD-01> | 平板浏览器 |
| 三合一演示（模块一+三同屏） | <http://127.0.0.1:8080/simulator/> | 与后台同机 |

详见 **`部署说明_三模块.md`**（拓扑图、防火墙/白名单、上线前清单）。

---

## 4. 状态机（方案 4.9 节）

```
S0 空闲待机 ─session_create─▶ S1 会话已建立 ─need_start─▶ S2 需求采集中
  ─need_complete─▶ S3 待游客确认 ─need_confirm─▶ S4 待放入证件
  ─card_read_ok─▶ S5 实名已锁定 ─order_generated─▶ S6 订单待确认
  ─order_confirm─▶ S7 订单待支付 ─pay_success─▶ S8 支付已确认
  ─print_command─▶ S9 出票中 ─ticket_arrived─▶ S10 票据已就位
  ─ticket_taken─▶ S11 已完成（终态）

异常出口：E1 会话超时关闭 / E2 支付状态不明 / E3 出票或取递失败 / E4 人工介入
回环：S6 --order_reject--> S2（按【取消】退回需求确认）
```

**门禁（任一不满足即拒绝迁移，返回 409）**

| 迁移点 | 前置条件 |
|---|---|
| S0→S1 | 打印机/读卡器/扫码器/键盘在线且纸张充足 |
| S2→S3 | 场次、票种、数量完整，且余量充足 |
| S3→S4 | 游客明确确认 + 余量充足 |
| S4→S5 | 读卡成功 + 实名锁定 + 证件数 ≥ 需求票数 |
| S5→S6 | 票务已回传最终订单号 + 年龄票种资格已澄清 |
| S6→S7 | 语音播报与终端显示一致 + 已按【确认】 |
| S7→S8 | 支付成功且流水号落库（幂等） |
| S8→S9 | 支付已确认且未重复打印（幂等） |
| S9→S10 | 视觉识别票据落托 + 数量与订单一致 + 未缺纸 |
| S10→S11 | 托盘递至取票口且游客取走或到达等待时限 |

**幂等**：以 `session_id + 动作 + 引用（证件号/流水号/键序+计数）` 为幂等键。
重复推送返回 `208`，不产生重复订单、不重复扣款、不重复打印。

---

## 5. 接口速查（方案 9.9 节表 9-1）

### 5.1 票务系统 → 后台（单向推送）

`POST /api/ticket/push`　Header：`X-Push-Token`

| type | 含义 | 关键字段 |
|---|---|---|
| `key_pressed` | 按键后的场次与票种信息 | `key`(K1–K7) / `session_code` / `ticket_type` / `qty` / `echo_ok` |
| `id_card_read` | 刷证后的身份证信息 | `id_masked` / `age` / `eligible_type` / `locked` / `card_count` |
| `ticket_price` | 票种票数与票价 | `order_id` / `items[]` / `total_amount` / `remaining` |
| `pay_result` | 支付结果 | `status`(success/fail/unknown) / `trade_no` / `amount` |
| `confirm_cancel` | 确认与取消按键 | `key`(confirm/cancel) |
| `print_command` | 打印指令下发 | `qty` / `ticket_sns[]` |
| `session_sold_out` | 场次售完 | `session_code` / `night_available` |

### 5.2 机器人 ↔ 后台

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/print/verify` | **机器人回传实际出票状态**（`arrived`/`actual_qty`/`paper_empty`），校正票务的出票成功判定 |
| POST | `/api/robot/alert` | 机器人异常告警异步推送 |
| POST | `/api/robot/heartbeat` | 心跳（电量 < 20% 触发 L2 告警） |
| GET | `/api/robot/status` | 在线状态与告警 |
| POST | `/api/session/ticket-taken` | 游客已取票 → S11 归档复位 |
| GET | `/api/session/{id}/next-action` | 按键序列**建议**（编排在机器人本体，后台不强制下发） |

### 5.2b 整体业务流程核查（确认门禁）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/verify/visitor-message` | 游客把意图说给人形机器人，机器人上报（含 `intent=confirm` 解锁确认门禁）。仅记录意图，不碰票务键盘 |
| GET | `/api/verify/state` | 当前会话核查清单（5 项）+ 放行结论 + 严格/宽松模式 |

> 门禁判定在 `TicketingBridge._on_confirm_cancel` 调用 `VerificationService.verify_confirm` 完成；状态机 `need_confirm` 仅保留票务域校验（需求完整+余量），「游客是否确认」交由核查服务。

### 5.3 显示终端

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 首帧全量快照 |
| GET | `/api/state/long?since=n` | 长轮询；无变更挂起 25s 返回 304，有变更立即返回 |
| POST | `/api/heartbeat` | 心跳；连续 3 次丢失判离线，降级为纯语音引导 |

页面：`P-0` 待机 / `P-1` 需求确认 / `P-2` 身份核验 / `P-3` 订单确认 / `P-4` 扫码支付 /
`P-5` 出票中 / `P-6` 取票完成 / `P-7` 异常提示（映射在 `rules.json → state_page_map`）

### 5.4 声光报警与运维

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/alarm/trigger` | 触发报警，重复触发自动升级等级 |
| POST | `/api/alarm/cancel` | **人工取消**，取消动作记入日志 |
| GET | `/api/ops/metrics` | 会话分布、平均耗时、设备、报警、规则版本 |
| GET | `/api/ops/selftest` | 自检（full/quick），返回逐项通过与处置建议 |
| GET/POST | `/api/ops/rule`、`/rule/reload` | 规则查询与热更新 |
| POST | `/api/ops/device/status` | 联调用：注入设备在线状态 |

---

## 6. 现场部署（Windows 10）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 用 NSSM 注册为 Windows 服务（崩溃 10s 内自动拉起，方案 9.2 节）
nssm install TicketHost "C:\Python311\python.exe"
nssm set TicketHost AppParameters "-m src.main --port 8080"
nssm set TicketHost AppDirectory "C:\TicketHost"
nssm set TicketHost AppRestartDelay 10000
nssm start TicketHost
```

部署前必做：

- [ ] 关闭 Windows 自动更新与自动休眠（9.10 节）
- [ ] 售票相关进程加入杀软白名单
- [ ] `config/app.yaml`：`ticketing.push_token` 改真实令牌；`robot.require_heartbeat` 改 `true`
- [ ] `config/rules.json`：票种票价、场次容量与业主方确认后填入
- [ ] 串口：`devices.ticket_sensor.serial_port` / `devices.alarm_light.serial_port` 按现场 COM 口修改
- [ ] 双网口隔离：一个接景区票务内网，一个接机器人与显示终端（9.10 节）

---

## 7. 信息显示终端页面（`web/`）

终端访问：`http://<售票主机>:8080/terminal/`（由本服务托管，同源无跨域）

| 文件 | 说明 |
|---|---|
| `web/index.html` | 单页骨架：步骤条区 / 主信息区 / 状态反馈区 |
| `web/terminal.css` | 版式与动效 |
| `web/terminal.js` | 8 套页面渲染 + 长轮询 + 心跳 + 降级遮罩 |

**页面清单（方案表 11-1）**

| 页面 | 名称 | 主要内容 |
|---|---|---|
| P-0 | 待机欢迎页 | 中英文欢迎语、营业时间、四类票价概览、购票四步图示、人工服务联系方式 |
| P-1 | 需求确认页 | 场次、票种/单价/数量/小计、合计金额、可售余量、「请确认无误后回答是」 |
| P-2 | 身份核验页 | 读卡区图示与放卡动画；核验后显示脱敏姓名与证件号 + 「已核验」徽章；多证件时提示还需核验 N 位 |
| P-3 | 订单确认页 | 强制关口：「成人票两张，总共三百元，请确认，如无异议，请扫码支付！」+ 96px 金额 |
| P-4 | 扫码支付页 | 应付金额、付款码对准扫码窗动画、微信/支付宝、支付倒计时 |
| P-5 | 出票中页面 | 打印机吐票落托动画、预计等待时间、「请勿离开」 |
| P-6 | 取票完成页 | 成功对勾、票种数量、检票口指引、祝语 |
| P-7 | 异常提示页 | 通俗原因、游客下一步该做什么、人工服务呼叫方式（中英对照） |

**版式参数**（方案 11.1 节，设计基准 1228×800 = MatePad 11.5 横屏）

- 金额 **120px**（P-3 取 96px 以容纳长句）、操作提示 **48px**、正文 **32px**
- 对比度 ≥ 8:1（深色底 `#0b0f14` + 近白字 `#f0f6fc`，夜间运营不刺眼）
- 页面切换 **280ms**（立即渲染内容 + CSS 入场动画，避免延迟填充导致空帧）
- 整帧按 `min(w/1228, h/800)` 等比 scale，任意屏幅版式一致

**状态映射**：`config/rules.json → state_page_map`
`S4/S5 → P-2`（S5 为已核验态）、`S6 → P-3`、`S7 → P-4`、`S8/S9 → P-5`、`S10/S11 → P-6`、`S0 → P-0`、`E1–E4 → P-7`

**运行机制**

- 首帧 `GET /api/state` → 长轮询 `GET /api/state/long?since=n`（304 立即重发）
- 心跳 `POST /api/heartbeat` 每 10s；连续 3 次丢失后台判离线并降级为纯语音引导
- 服务不可达时显示「正在连接售票服务」遮罩并自动重连，**不白屏**

**评审/演示入口**

- `?preview=P-3` 直接预览指定页面（含 P-2v 已核验态），无需跑业务
- `?debug=1` 底部显示页面切换条，逐页查看
- `?tid=matepad-01` 指定终端编号

---

## 8. 模拟票务系统（三种方式）

**模块一（票务系统模块）的标准入口是 <http://127.0.0.1:8080/ticketing/>**：它以设备面板（键盘 / 身份证扫描器 / 付款码扫描器 / 打印机）形式模拟真实票务系统的物理输入端，向后台单向推送 7 类状态，是最贴近现场形态的操作台。

真实票务系统未接入前，还可用以下方式模拟它向后台服务单向推送 7 类状态，跑通完整业务流。

### 方式一 · Web 模拟器控制台（推荐，可视化）

浏览器打开 <http://127.0.0.1:8080/simulator/>

- 左侧：会话状态 + 购票配置 + **12 步推送按钮**（按 P1–P7 顺序，当前步高亮）+ 推送日志
- 右侧：**信息显示终端实时画面**，点一步左侧、右侧页面立刻切换
- `▶ 自动跑完整流程` 按 0.9s 节奏自动推送全流程
- `异常注入` 折叠区：支付不明 / 未出票 / 售罄 / 键位回读不一致 / 缺纸 / 少出票 / 取消键 / 人工取消报警（会**自动补齐前置步骤**到该异常所需状态）
- `自定义推送` 折叠区：Postman 式手填 JSON 发送
- 加 `?tv=P-3` 可让右侧画面停在指定页面

### 方式二 · Postman

导入 `tools/TicketingSimulator.postman_collection.json`（29 个请求，按阶段分组）

1. 确认集合变量 `baseUrl` = `http://127.0.0.1:8080`
2. 从「① P1 接待唤醒」自上而下逐条 Send；创建会话那条会自动把 `session_id` 写入变量 `sessionId`
3. 每步返回体的 `state` / `page` 就是状态机与终端页面，可对照 `/terminal/` 看效果

> 异常注入组需**在对应阶段执行**，否则状态机会以 409 拒绝并返回当前允许的事件列表（每条请求的 Description 里写了时机）。

### 方式三 · 命令行脚本

```bash
python tools/ticketing_simulator.py --scenario normal     # 标准全流程
python tools/ticketing_simulator.py --all                 # 六类场景依次跑完
python tools/ticketing_simulator.py --scenario pay_unknown
python tools/display_probe.py --duration 60               # 终端长轮询探针
```

### 三种方式的关系

| | 用途 | 谁在用 |
|---|---|---|
| Web 控制台 | 演示、联调、给业主看效果 | 现场/产品 |
| Postman | 逐条核对报文、改字段试边界 | 票务厂商、后端 |
| 脚本 | 回归、批量压测 | 开发、测试 |

三者调用的是**同一套对外接口**，与真实票务系统的接入方式完全一致——所以这里跑通，现场接真票务只是把 baseUrl 换成厂商地址。

---

## 9. 当前状态与待办

**已完成**：状态机与门禁、7 类推送接入、8 套页面决策、长轮询、幂等、超时清扫、
崩溃恢复、声光报警、三级自检、规则热更新、运维看板、6 类场景模拟器、终端探针。

**待接入（真实设备/厂商到位后替换）**

| 项 | 位置 | 说明 |
|---|---|---|
| 票据到位传感器 | `device_service.open_sensor` | 当前为可注入状态，串口协议需按对射光电实测定 |
| 声光报警灯 | `alarm_service._write_hw` | 当前为模拟写入，需厂商指令集 |
| 打印机/读卡器/扫码器真实驱动 | `device_service` | 当前以在线状态注入方式联调 |
| 机器人心跳与转发端点 | `robot_gateway` | `robot.base_url` 与 `/api/host/forward` 待厂商确认 |
| 票务系统推送字段最终版 | `models.py` | 以厂商接口文档为准，字段已按方案 9.3 节七类预置 |
| 显示终端真机适配 | `web/` | 已按 MatePad 11.5（2456×1600，3:2）设计，需在真机横屏下复核字号与动效 |

**已修复的缺陷（联调中发现）**

1. 键位回读不一致时先登记幂等键 → 重试计数永远卡在 1，E32「限次 3 次重试」失效。
2. 重复刷同一张证直接幂等忽略 → 多证件提醒计数不递增，E29「提醒 3 次后锁定」无法生效。
3. `/api/state/long` 用 `async def` 做阻塞等待 → 挂死整个 uvicorn 事件循环，已改为同步端点。
4. 话术模板与 `_amount_cn()` 重复拼接「元」→ 输出「共三百元元」，已修正并补中文数字。
