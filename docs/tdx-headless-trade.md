# 通达信 headless 交易宿主

## 结论

2026-09-03 已在本机实验副本完成动态验证：不启动 `Tdxw.exe`，只运行 64 位 Python
headless 宿主和 `NewTc\TC.exe`，TPyth 的本地 JSON-RPC 服务仍能正常完成资金账户、资产、
持仓和当日委托查询。测试没有发送委托，也没有撤单。

调用链如下：

```text
cn-broker-api
    -> HTTP JSON-RPC（实验端口，例如 17711）
    -> TPyth.dll
    -> 三个兼容回调
    -> TdxCopilot.dll / TCO_Data
    -> TC.exe
    -> 券商柜台
```

headless 宿主代替了 `Tdxw.exe` 中与交易相关的初始化和回调桥接。`TC.exe` 仍然不可省：
它持有交易登录态并连接券商柜台。

最小运行目录的提取步骤、白名单和复验方法见
[tdx-headless-minimal-runtime.md](tdx-headless-minimal-runtime.md)。

## 隔离要求

- 只在完整复制出来的客户端实验目录中运行，生产安装目录保持不变。
- 实验根目录必须有人为创建的 `.trade-lab-marker`；缺少标记时程序拒绝加载 DLL。
- 启动前必须停止生产 `Tdxw.exe`、`TC.exe` 和会自动拉起它们的 broker-api 计划任务。
- 实验副本的 `HQMP`、`PYMP`、`MCP` 端口必须与生产端口错开。
- `HKCU\Software\Microsoft\RWNode` 中，实验目录到 `HQMP` 端口的 Base64 路由必须已配置。
  程序只核对这项，不会静默修改注册表。
- 当前提取的是闭源二进制的兼容宿主，不是源码重写；客户端升级后必须重新做只读验证。

## 启动

先在实验副本的 `TMTconfig.ini` 中配置独立端口。当前验证组合是：

```ini
[HQMP]
Port=13575

[PYMP]
Port=14572
HTTP=1

[MCP]
Port=17711
```

只检查，不启动任何进程：

```powershell
.venv\Scripts\python.exe -m cn_broker_api.tdx_headless `
  --root "D:\Path\To\TdxLabCopy" --check-only
```

启动 DLL 宿主并弹出实验副本的交易登录窗口：

```powershell
.venv\Scripts\python.exe -m cn_broker_api.tdx_headless `
  --root "D:\Path\To\TdxLabCopy" --start --launch-tc
```

登录后，在另一个终端执行只读探测。它的代码中没有下单或撤单方法，也不会打印账号、
账户句柄、资产金额、证券代码等敏感数据：

```powershell
.venv\Scripts\python.exe -m cn_broker_api.tdx_headless_smoke `
  --url http://127.0.0.1:17711/
```

停止宿主使用 `Ctrl+C`。宿主只会结束由自己启动的 `TC.exe`，然后按逆序关闭 TPyth 与
TdxCopilot 服务；不会删除或改写客户端文件。

## broker API 配置

broker API 继续使用已有 MCP transport，只把目录、端口和桌面模式切到实验副本：

```toml
[driver.tdxquant]
tdx_home = "D:/Path/To/TdxLabCopy"
mcp_url = "http://127.0.0.1:17711"
transport = "mcp"
desktop_mode = "headless"

[watchdog]
enabled = false
```

`desktop_mode = "headless"` 会把桌面配方从 `Tdxw.exe + TC.exe` 改为只有 `TC.exe`，因此
`POST /v1/session/ensure` 和看门狗都不会再拉起完整客户端。headless 宿主本身应先独立启动；
第一版不让 broker-api 在进程内加载厂商 DLL，避免原生 DLL 崩溃时把对外 API 一起带走。

## 已验证与未验证

已验证：

- DLL 初始化成功，三个实验端口均由 headless 宿主监听；
- `TC.exe` 能正常显示券商交易登录界面并登录；
- 资金账户、资产、持仓、当日委托四项只读调用成功；
- 测试过程中 `Tdxw.exe` 始终未运行。

另外已从完整实验副本重建出 99 个文件、约 99.63 MB 的最小运行目录。该目录只保留宿主
实际加载的 64 位 DLL 与完整 `NewTc`，根目录 `T0002` 不存在；重复执行四项只读调用仍成功。

## 行情接口

headless 模式只打通了第三个通用回调到 `TCO_Data`。`Tdxw.exe` 原来提供的两个行情/界面
回调目前为空实现，所以 TPyth 行情方法虽然能收到请求，却没有有效行情：

- `get_market_snapshot`、`get_stock_info` 只有未解析的空 `raw`；
- `get_pricevol`、`get_zdt_data`、`get_divid_factors` 返回空结果；
- `get_market_data` 有代码和总数框架，但没有 K 线行。

因此 headless 下不能继续声明这些调用代表“客户端行情可用”。量化项目已经有独立公网 TDX
socket 行情源，可以提供实时报价、五档、K 线和 XDXR；交易链路继续由本宿主承担。具体能力
映射和实测结果见最小运行目录文档。

尚未验证：

- 真实报单、撤单及回执链路；
- 融资融券账户的全部方法；
- 长时间运行、断线重连、跨交易日和客户端升级后的稳定性；
- headless 下依赖行情快照的健康检查改造。完整客户端的行情模块已被移除，这一项当前必然
  取不到有效行情。

在真实报单验证前，只能把当前结果表述为“交易查询链路已打通”，不能表述为“实盘交易已验收”。
