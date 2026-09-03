# 通达信 headless 最小运行目录

## 目的与结论

本文记录如何从完整通达信整合版的**实验副本**中提取只用于交易的最小运行目录。目标不是修改
日常交易安装目录，而是在生产客户端停止后，从完整副本逐步收缩并用只读调用验证每一步。

2026-09-03 的实测结果是：安全最小集为 99 个文件、约 99.63 MB。该目录不包含
`Tdxw.exe`，也不需要根目录 `T0002`；只运行 64 位 Python headless 宿主与
`NewTc\TC.exe` 时，资金账户、资产、持仓和当日委托查询均成功。

这里的“安全最小集”是指已经通过真实 DLL 初始化、交易登录和只读查询验证的边界，不声称是逐个
字节删除后得到的理论最小值。`NewTc` 继续缩减只能节省很少空间，却会破坏签名校验或券商插件。

## 提取方法

### 1. 建立隔离副本

先停止以下进程和会自动拉起它们的计划任务：

- `Tdxw.exe`；
- `TC.exe`；
- 正在使用正式客户端目录的 `cn-broker-api`。

完整复制客户端到一个新目录，并在新目录根部创建 `.trade-lab-marker`。headless 宿主和
`desktop_mode = "headless"` 的 broker API 都会检查这个标记；缺少标记时拒绝启动。

### 2. 分离实验端口

在实验副本的 `TMTconfig.ini` 中配置与生产环境不同的端口。已验证组合为：

```ini
[HQMP]
Port=13575

[PYMP]
Port=14572
HTTP=1

[MCP]
Port=17711
```

`HKCU\Software\Microsoft\RWNode` 中还必须存在“实验目录绝对路径 → HQMP 端口”的 Base64
路由。headless 程序只验证该路由，不会自行修改注册表。生产与实验不能同时运行。

### 3. 观察实际加载项

先从完整实验副本启动 headless 宿主和 `TC.exe`，记录两个进程从实验目录加载的模块。
64 位宿主实际加载：

```text
TdxCopilot.dll
TCalc64.dll
tdxRpc64.dll
PYPlugins/TPyth.dll
PYPlugins/tdxRpcx64.dll
PYPlugins/mfc100.dll
PYPlugins/msvcp100.dll
PYPlugins/msvcr100.dll
```

32 位 `TC.exe` 会加载 `NewTc` 下的核心 DLL 和大部分 `TCPlugins`。`TCFMod.dat`、
`TCFT.dat`、`TCFE.dat` 还保存了文件指纹和签名。缺少其中列出的组件时，客户端会报
“版本文件不匹配”或缺少组件，不能以“当前测试没有点到某个页面”为理由删除对应插件。

### 4. 按白名单重建目录

不要在完整实验副本上直接批量删除。新建第二个目录，只复制下列白名单：

```text
.trade-lab-marker
TMTconfig.ini
TdxCopilot.dll
TCalc64.dll
tdxRpc64.dll

PYPlugins/
  TPyth.dll
  tdxRpcx64.dll
  mfc100.dll
  msvcp100.dll
  msvcr100.dll
  sys/tqcenter.py

NewTc/
  整个目录
```

`sys/tqcenter.py` 是 2026-09-03 补上的：厂商的委托类型编号表（`STOCK_BUY`、`PRICE_MY`、
`CREDIT_FIN_BUY` 等）由 `tq_constants.py` 用 `ast` 从这个源码文件里读，缺了它驱动那条报单
路径必然抛 `DriverError`。它只被解析、不被 import，所以不会把 numpy / pandas 拖进来。

headless Python 模块和项目虚拟环境属于 `cn-broker-api`，不复制进客户端实验目录。

### 5. 分层验证

每次缩减后按以下顺序验证，前一步不通过就不进入下一步：

1. `python -m cn_broker_api.tdx_headless --root <实验目录> --check-only`；
2. 启动 headless 宿主，确认 HQMP、PYMP、MCP 三个端口由同一个 64 位 Python 进程监听；
3. 确认只有实验副本的 `TC.exe`，没有 `Tdxw.exe`；
4. 登录交易模块；
5. 执行 `python -m cn_broker_api.tdx_headless_smoke`；
6. 只检查成功状态和条数，不输出账号、资产金额、证券代码或委托明细。

最后单独删除空的根目录 `T0002` 再重复验证。实测宿主不会重建该目录，四项只读查询仍成功，
所以它不属于最小集。这里说的是客户端**根目录**的 `T0002`；`NewTc\T0002` 保存交易模块的
券商配置和账户缓存，不能删除。

## 可以移除的完整客户端内容

下列内容不在交易最小集内：

- `Tdxw.exe` 及完整行情主程序 DLL；
- 根目录 `T0002`；
- `chrome`、`vipdoc`、`webs`、`jspages`；
- `QHPlugins`、`GNPlugins`、`SEPlugins`、`SEPlugins64`、`ZDPlugins`；
- 行情缓存、K 线历史、公式、板块、完整客户端界面资源；
- 更新包、顶栏皮肤及其他只被完整客户端使用的目录。

保留完整 `NewTc` 后，交易目录约 82.56 MB；其余约 17 MB 是 headless 宿主实际加载的 64 位
DLL 和运行库。继续清理 `NewTc\T0002` 中少量历史日志最多节省几百 KB，不值得以登录和柜台
配置损坏为代价。

## 敏感信息与版本约束

`NewTc\T0002` 可能包含资金账号、营业部、柜台地址、交易缓存和会话相关数据。即使密码不是
明文，它也必须按凭据目录管理：不提交 Git、不压缩外发、不作为公开测试夹具。

该最小集只对本次验证的客户端版本成立。升级客户端后应重新从完整副本执行“观察加载项 → 白名单
重建 → 只读验证”，不能直接把旧白名单套到新版本。

## 行情能力边界与报单阻塞

最小目录只保留交易链路。TPyth 会注册三个宿主回调，但当前 headless 实现只有第三个通用回调
桥接到 `TCO_Data`；前两个由 `Tdxw.exe` 提供的行情/界面回调是安全空实现。因此：

| 能力 | headless MCP 实测 | 处理方式 |
|---|---|---|
| 资金、持仓、委托 | 正常 | 继续走 headless 交易宿主 |
| 实时快照、五档 | 无有效字段 | 宿主需自己提供（见下） |
| 批量现价 | 空结果 | 同上 |
| K 线 | 只有代码和总数框架，无行数据 | 同上 |
| 除权除息 | 空结果 | 同上 |
| 涨跌停状态 | 空结果 | 同上 |
| 标的静态信息 | 无有效字段 | 同上 |
| **报单、撤单** | **失败**（`Value=0`「提交交易失败。」） | 阻塞，见下 |
| 券商融资标的资格 | 不可用 | 保持未知并交给券商柜台裁决 |

2026-09-03 晚间的实测推翻了此前「交易与行情解耦、行情交给独立公网 TDX socket 源」这个
分工：**报单路径本身要通过宿主回调拿标的数据**。报单期间通用回调收到函数号 81、82、80、82
（func 80 的入参里就是证券代码），当前桥接转给 `TCO_Data` 后一律回 `rc=1` 而输出为空，
报单于是在到 `TC.exe` 之前就失败。完整证据与回执对照见
[tdx-headless-trade.md](tdx-headless-trade.md) 的「报单实测」一节。

⇒ 要让最小目录承担**交易**通道，必须在宿主里实现这些函数号的数据提供者。数据可以来自
`QuantTradeDemo/backend/gateway/cn/sources/_tdx_proto.py` 那份零第三方依赖的公网 TDX
socket 实现（2026-09-03 实测可连 7709 行情服务器，取到实时报价、K 线与 XDXR），但**喂进去
的路径必须是宿主回调**，不能停留在「外部另有一条行情」这个层面。
