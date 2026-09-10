# cn-broker-api

`cn-broker-api` 是运行在 Windows 本机的 A 股交易通道服务。它把通达信交易客户端封装成
带鉴权的 HTTP API，供 QuantTradeDemo 等策略程序查询账户、持仓和委托，并执行报单、撤单。

```text
策略程序 ── HTTP ──> cn-broker-api ──> TC.exe ──> 券商柜台
```

当前正式使用的路径是 **headless + 直接 HQMP**：保留交易内核 `TC.exe`，不启动行情主程序
`Tdxw.exe`。该路径已经在平安证券信用账户上完成登录、账户查询、普通/信用委托及撤单验证。
行情不属于本服务当前的生产职责；headless 模式不声明 `market_data` 能力，调用方应使用自己的
行情源或保持行情接口为空实现。

> `POST /v1/orders` 会向真实资金账户发送真实委托。首次接入必须先使用 `paper` 驱动验证
> HTTP 契约，真实通道测试应使用明确的限价、最小数量，并由操作人承担交易结果。

## 运行模式

| 模式 | 交易链路 | 行情 | 自动确认补丁 | 用途 |
|---|---|---|---|---|
| `paper` | 内存委托簿，不连接客户端 | 模拟 | 不适用 | 开发、CI、接口联调 |
| `tdxquant + headless + hqmp` | 直接连接 `TC.exe` | 不提供 | 不需要 | 当前生产交易通道 |
| `tdxquant + full + mcp` | `Tdxw.exe` 本地 JSON-RPC → `TC.exe` | 提供 | 需要 | 兼容旧部署 |

headless 模式仅允许使用带 `.trade-lab-marker` 的独立客户端目录，并校验目标进程的完整路径。
它不会接管其他通达信安装目录中的进程。`hqmp_reuse_tc=true` 时，服务重启可以重新接管同一
headless 目录中已经运行的单个 `TC.exe`；没有可复用进程时仍可正常冷启动。

## 环境要求

- Windows 10/11，交互式桌面会话
- 64 位 Python 3.11 或更高版本
- 已验证的通达信整合客户端副本
- headless 模式所需的 HQMP 报文模板文件，存放在仓库之外
- 交易登录凭据文件，存放在仓库之外

当前真机验证组合为平安证券、通达信“开心果整合版”、信用账户。其他券商、客户端版本和
普通账户在使用前必须重新验证协议、登录窗口和委托状态字段。

## 安装

```powershell
git clone <repository-url> cn-broker-api
Set-Location cn-broker-api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

先创建仓库外配置，并使用纸面驱动：

```toml
[server]
port = 17710
state_dir = "D:/state/cn-broker-api"

[driver]
name = "paper"
```

```powershell
$env:CN_BROKER_API_CONFIG = "D:\config\cn-broker-api.toml"
.\.venv\Scripts\python.exe -m cn_broker_api.config
.\.venv\Scripts\python.exe -m cn_broker_api
```

服务固定监听 `127.0.0.1`，监听地址不可配置。首次启动会在 `state_dir` 中生成 API token，
启动日志只记录文件路径，不打印 token 内容。

```powershell
$token = (Get-Content "D:\state\cn-broker-api\token").Trim()
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod "http://127.0.0.1:17710/v1/meta" -Headers $headers
```

## headless 生产配置

从 `config.example.toml` 复制一份完整配置到仓库之外。核心配置如下：

```toml
[driver]
name = "tdxquant"

[driver.tdxquant]
tdx_home = "D:/broker/tdx-headless"
desktop_mode = "headless"
transport = "hqmp"

cred_source = "file"
cred_file = "D:/secrets/broker-login.json"
max_password_submits_per_day = 10
max_consecutive_failures = 3

hqmp_port = 13575
hqmp_capture = "D:/private-data/hqmp-template.jsonl"
hqmp_enable_trade = true
hqmp_reuse_tc = true
hqmp_max_order_size = 100
hqmp_max_order_notional = 2000.0

[watchdog]
enabled = false
```

凭据文件格式：

```json
{"account":"资金账号","password":"交易密码","account_type":"CREDIT"}
```

生产注意事项：

- `tdx_home`、`cred_file`、`hqmp_capture` 必须使用仓库外的绝对路径。
- `hqmp_enable_trade=false` 时只允许查询，报单和撤单会被拒绝。
- `hqmp_max_order_size` 与 `hqmp_max_order_notional` 是直接通道的额外单笔上限。
- `hqmp_reuse_tc=true` 只允许复用路径精确匹配的一个 `TC.exe`，发现其他交易客户端时拒绝启动。
- 看门狗只维护桌面进程，不提交交易密码。由 QuantTradeDemo 的登录任务驱动登录时可保持关闭。
- headless 直接通道不经过“交易信号”页面，因此不安装、也不检查自动确认补丁。

启动前执行配置校验：

```powershell
$env:CN_BROKER_API_CONFIG = "D:\config\cn-broker-api.toml"
.\.venv\Scripts\python.exe -m cn_broker_api.config
```

## 登录与会话状态

`GET /v1/session/status` 只观察状态，不启动客户端、不读取密码、不消耗密码提交次数。主要状态：

| 状态 | 含义 | 调用方处理 |
|---|---|---|
| `READY` | 交易通道可用 | 继续调用交易接口 |
| `LOGIN_REQUIRED` | 未启动或已识别到交易登录窗口 | 调用 `POST /v1/session/ensure` |
| `LOGIN_IN_PROGRESS` | 已有登录任务执行中 | 按 `job_id` 查询进度 |
| `MANUAL_ACTION_REQUIRED` | 出现无法自动处理的窗口 | 人工检查客户端 |
| `CHANNEL_UNAVAILABLE` | 证据不足或通道异常 | 不报单，检查服务日志和客户端 |

登录接口是幂等操作。已登录时返回 `acted=false`，不会再次提交密码；未登录时才启动或接管
`TC.exe` 并提交密码。

```powershell
$body = @{
  account = "资金账号"
  account_type = "CREDIT"
  start = $true
  minimize = $true
  wait_seconds = 90
} | ConvertTo-Json

Invoke-RestMethod "http://127.0.0.1:17710/v1/session/ensure" `
  -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

`wait_seconds` 必须是 1 到 600 的 JSON 整数，`start` 和 `minimize` 必须是 JSON 布尔值。
交易接口发现明确未登录时返回：

```json
{
  "error": "broker_login_required",
  "session_state": "LOGIN_REQUIRED",
  "login_endpoint": "/v1/session/ensure",
  "retryable_after_login": true
}
```

调用方应先完成登录，再重新发起原业务请求。状态未知的写请求不得自动重试。

## HTTP API

当前契约版本为 **5**。调用方启动时应读取 `GET /v1/meta` 并严格校验 `contract`。

```text
GET    /v1/meta
GET    /v1/health
POST   /v1/health/refresh
GET    /v1/session/status
POST   /v1/session/ensure
GET    /v1/jobs/{job_id}
GET    /v1/state

POST   /v1/orders
DELETE /v1/orders/{order_id}?symbol=000001.SZ
GET    /v1/orders/{order_id}
GET    /v1/orders
GET    /v1/positions
GET    /v1/positions/sellable
GET    /v1/account

GET    /v1/quotes                     # full + mcp
GET    /v1/instruments/{code}         # full + mcp
GET    /v1/klines                     # full + mcp
GET    /v1/prices                     # full + mcp
GET    /v1/limit-status               # full + mcp
GET    /v1/dividends/{code}           # full + mcp
POST   /v1/session/minimize
GET    /v1/diag/screenshot
```

报单示例：

```json
{
  "account": "资金账号",
  "account_type": "CREDIT",
  "symbol": "000001.SZ",
  "side": "buy",
  "size": 100,
  "price": 10.50,
  "order_type": "limit",
  "credit_kind": "fin_buy"
}
```

`size` 必须是正的 JSON 整数；`price` 必须是有限正数。字符串数值、布尔值、浮点数量、
`NaN` 和无穷大均返回 `400 bad_request`。证券身份只由 `symbol` 确定，不要求调用方提供证券名称。

信用委托类型以代码中的 `CreditOrderKind` 和接口测试为准。调用前可通过 `GET /v1/meta` 检查
`credit_order` 能力。

### 结果语义

| HTTP/字段 | 含义 |
|---|---|
| `201` | 委托已被交易通道接受 |
| `202` | 结果未定，必须按委托簿重新确认 |
| `400 bad_request` | 请求字段不合法 |
| `409 order_rejected` | 柜台明确拒绝 |
| `503 broker_login_required` | 明确未登录，可调用登录接口 |
| `503 channel_unavailable` | 通道不可用，不应报单 |
| `504 ack_timeout` | 写请求确认超时，状态未知，禁止直接重试 |
| `known=false` | 查询结果不可判定，不等于空列表或数值 0 |

撤单返回的 `outcome` 为 `canceled`、`filled` 或 `timeout`。只有 `canceled` 表示已确认撤销；
`timeout` 表示状态仍未知，调用方必须继续查询。

## 计划任务部署

仓库提供 `install_task.ps1`，用于安装登录触发和每日定时启动任务。计划任务必须运行在交互式
用户会话中；Windows 会话 0 无法完成窗口识别和密码输入。

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
Get-ScheduledTask -TaskName cn-broker-api
Start-ScheduledTask -TaskName cn-broker-api
```

正式切换顺序：

1. 停止并禁用旧任务，确认 17710、13575 未被占用。
2. 校验生产配置及 headless 客户端目录。
3. 更新代码并运行测试。
4. 启用并启动 `cn-broker-api` 计划任务。
5. 检查 `/v1/meta`、`/v1/session/status` 和 `/v1/health/refresh`。
6. 通过 QuantTradeDemo 的登录 CLI 完成一次无人干预登录，再执行只读账户检查。

日志写入 `<state_dir>/cn-broker-api.log`。启动日志采用结构化键值表达，并标记每个配置项来自
配置文件还是默认值；凭据内容不会写入日志。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q cn_broker_api tests
git diff --check
```

与 Windows 客户端有关的用例会按平台和环境自动跳过；CI 使用纸面驱动。修改真实交易路径时，
必须保留以下约束：

- 写请求在确认超时后不得自动重试。
- 查询不可用不得伪装成空列表或数值 0。
- 账户不匹配时拒绝操作。
- 不在多个交易通道之间自动回退。
- 报单和撤单必须经过同一账户串行队列。

## 相关文档

- [headless 直接交易现状与验证记录](docs/tdx-headless-trade-collateral-20260907.md)
- [headless 架构及历史实验](docs/tdx-headless-trade.md)
- [最小运行目录](docs/tdx-headless-minimal-runtime.md)
- [配置模板](config.example.toml)

## 免责声明

本项目是个人自用工具，与券商及软件厂商无关联。它会使用真实资金账户向券商柜台发送委托。
第三方整合客户端不是券商或厂商的官方分发渠道，其合规性、安全性及由使用产生的交易结果均由
使用者自行判断和承担。
