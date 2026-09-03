# 通达信 headless 交易宿主

## 结论

2026-09-03 已在本机实验副本完成动态验证：不启动 `Tdxw.exe`，只运行 64 位 Python
headless 宿主和 `NewTc\TC.exe`，TPyth 的本地 JSON-RPC 服务仍能正常完成资金账户、资产、
持仓和当日委托查询。

**当晚的真实报单实测同时证明：headless 下报不出单**，全模式同参数可以。原因是报单路径
会经通用回调向宿主索要标的数据，而当前宿主给不出 ⇒ 现阶段 headless 只是一条**查询**
通道，不是交易通道。详见[报单实测](#报单实测2026-09-03)一节。

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

已证伪：

- **报单**——`order_stock` 回 `Value=0`「提交交易失败。」，全模式同参数回 `Value=1`；
- **交易与行情可以解耦**这个设想——报单路径要通过宿主回调拿标的数据。

另外已从完整实验副本重建出 99 个文件、约 99.63 MB 的最小运行目录。该目录只保留宿主
实际加载的 64 位 DLL 与完整 `NewTc`，根目录 `T0002` 不存在；重复执行四项只读调用仍成功。

## 行情接口与报单路径

headless 模式只打通了第三个通用回调到 `TCO_Data`。`Tdxw.exe` 原来提供的两个行情/界面
回调目前为空实现，所以 TPyth 行情方法虽然能收到请求，却没有有效行情：

- `get_market_snapshot`、`get_stock_info` 只有未解析的空 `raw`；
- `get_pricevol`、`get_zdt_data`、`get_divid_factors` 返回空结果；
- `get_market_data` 有代码和总数框架，但没有 K 线行。

2026-09-03 晚间的报单实测证明**报单路径与行情不可解耦**，此前「交易走宿主、行情交给
外部 TDX socket 源」的分工救不了报单：报单期间 TPyth 会经第三个通用回调向宿主索要
标的数据（其中一个函数号的入参里就是证券代码），宿主给不出数据，报单就失败。详见下节。

## 报单实测（2026-09-03）

**headless 下报不出单，全模式同参数可以。** 对照条件完全一致：同一信用账户、同一账户
句柄、`000001.SZ` 买入 100 股限价 10.72、相隔约 3 分钟：

| 通道 | `order_stock` 回执 |
|---|---|
| 生产全模式（17709） | `Value=1`「已发送信号至客户端，待用户确认！」 |
| headless（17711） | `Value=0`「提交交易失败。」 |

四次尝试全部没有进柜台：`TC.exe` 的日志里没有痕迹，当日委托查询始终返回空数组。
收市、价格合法性、账户类别三个可能的混淆因素都已排除——`account_type` 取 `STOCK` 或
`CREDIT` 在 headless 下都返回同一个句柄，回执一字不差。

**根本原因在宿主回调层。** 给三个回调加日志后，报单那一刻的调用序列是：

```text
GENERIC func=81   入参含 "CREDIT"    -> rc=1  outlen=0  输出为空
GENERIC func=82                      -> rc=1  outlen=0  输出为空
GENERIC func=80   入参含 "000001"    -> rc=1  outlen=0  输出为空
GENERIC func=82                      -> rc=1  outlen=0  输出为空
```

`func 80` **就是报单请求本身**，不是行情查询。定长记录的字段位置已经从 hex 转存里认出来：

| 偏移 | 内容 | 本次实测值 |
|---|---|---|
| +33 | 证券代码，6 字节 ASCII | `000001` |
| +99 | 限价，小端 float32 | `1F 85 2B 41` ＝ 10.72 |
| +103 | 委托数量 | `64` ＝ 100 |

三个请求各自要求宿主写回多少字节，厂商在 `out_size` 的入值里给了：

| func | 入参长度 | 关键入参字段 | 宿主应写回 | 判读 |
|---|---|---|---|---|
| 80 | 179 B | 代码 +33、价 +99、量 +103 | 160 B | 报单请求与应答 |
| 81 | 201 B | 账户类别 `CREDIT` @+56 | 178 B | 按类别取账户记录 |
| 82 | 201 B | `FF FF` @+31 | 420000 B | 通配的列表查询 |
| 83 | 201 B | — | 585000 B | 另一类列表查询 |
| 90 | 201 B | — | 498 B | 单条记录查询 |

⇒ 全模式下 `Tdxw.exe` 接过报单请求、转给交易端、再写回那 160 字节应答，TPyth 照着应答
拼三态。headless 下宿主什么都没写回，所以 `Value=0`。

**两个现成的转发目标都不成立**（2026-09-03 实测）：

- `TCO_Data(func, 0, in, in_size, out, out_size, flags)`——当前分支用的这条，一律回
  `rc=1` 而 `out_size` 写回 0、输出缓冲区不动；
- `TCO_Func(short, void*, void*, int)`——签名看着更吻合（第一个参数是 16 位，装得下这些
  小编号），但按 `(func, in, out, in_size)` 调用会**阻塞不返回**，`stock_account` 因此一直
  收不到答复。

另外要注意：`func 81` 在**每次** `stock_account` 时都会被调用，而它得到空答复时查询
仍然成功——资金、持仓、委托四项查询都不依赖宿主的这份数据。**只有报单依赖。**

**确认闸这道题在 headless 下还没轮到**：报单没走到那一步。另外已确立确认闸在收市也会
触发（它是本地判决，在柜台之前），所以这类实验不必等开盘。

「交易信号」窗口属于 `Tdxw.exe`，内部是纯 CEF（`CefBrowserWindow` →
`Chrome_WidgetWin_0`），一个 Win32 按钮都没有——这印证了自动确认只能靠注入 JS，
也说明 headless 下这个窗口根本不存在。

⇒ 要让 headless 承担交易通道，必须让宿主自己回答 `func 80`：把请求转到交易端并按 160
字节的应答格式写回。这一项落在交易通道的关键路径上，字段偏移错一位就是一笔方向或价格错的
委托，必须单独验收。

## 厂商入口点：已还原的签名（2026-09-03）

两个 DLL 都导出了当前宿主**从未调用**的方法。签名是从导出函数入口处的机器码还原的
（x64 前四个整型参数依次在 `rcx / rdx / r8 / r9`，指令宽度就指明了参数宽度）：

| 导出 | 还原签名 | 依据与实测 |
|---|---|---|
| `TPY_SetMainWnd` | `(void* hwnd, int)` | `mov rbx,rcx` + `mov edi,edx`，两者都存入全局；**无解引用，所以第一个参数是句柄而非 MFC 对象**。按单参数调用会把垃圾值存进全局，随后 `TPY_StartClientSrv` 段错误——这是 2026-09-03 那次崩溃的原因，不是「参数是 CWnd\*」 |
| `TCO_SetMainWnd` | `(void* hwnd, int, int, int)` | 入口把 `rcx/edx/r8d/r9d` 四个参数全存栈 |
| `TCO_SetParendWnd` | `(int, void*, int)` | `mov [rsp+8],ecx` 是 32 位、`mov [rsp+0x10],rdx` 是 64 位 |
| `TCO_Func` | `(short, void*, void*, int)` | `mov [rsp+8],cx` 是 16 位。按 `(func,in,out,in_size)` 调用会阻塞 |
| `TPY_ShowDlg` | `(int nType, void* p)` | `test rdi,rdi` 判空后 `cmp ebx,1` |
| `TPY_CreateBrowerView` | `(int, void*)` | `mov ebp,ecx` / `mov rsi,rdx` |
| `TPY_Func_Data` | `()` 恒回 1 | 无参数，`mov eax,1; ret` |

其余未调用的导出还有 `TPY_DestroyBrowerView`、`TPY_DataCallBack`、`TPY_UpdataCallBack`、
`TPY_GetState`、`TPY_GetAllStrategy`、`TPY_RunStop`。

`TPY_CreateBrowerView` 值得单独一提：「交易信号」窗口是 TPyth 侧创建的 CEF 视图，
如果宿主能把它建出来，现有那套注入 `aireq.html` 的自动确认机制在 headless 下也许能沿用。
前提是最小目录要补回 `webs` 与 `chrome`（CEF 运行时）。

### 已证伪：补一个宿主窗口并不足以让报单通

2026-09-03 实测。在完整实验副本上给宿主加了真实顶层窗口（独立线程 + 消息循环），并按
还原出的签名依次调用 `TCO_SetMainWnd(hwnd,0,0,0)`（回 0）、`TCO_SetParendWnd(0,hwnd,0)`、
`TPY_SetMainWnd(hwnd,0)`（回 1）、`TPY_GetState(buf)`（回 1，内容为空）。宿主正常起来、
`TC.exe` 自动登录、四项查询照常成功，但同一笔报单**仍然回 `Value=0`**，回调序列与之前
一字不差，宿主窗口在报单期间也没收到任何相关消息。

⇒ 报单完全走在回调里：TPyth 通过 `func 80` 请宿主代办，并等一个 160 字节的应答。
宿主窗口与这件事无关。

顺带纠正一处：先前把段错误归给 `TPY_StartClientSrv`，实际是 `TPY_GetState` —— 它要一个
640 字节输出缓冲区（机器码里 `mov rbx,rcx` / `test rbx,rbx` / `mov r8d,0x280`），
按无参数调用就崩。`TPY_SetMainWnd` 本身从未崩过。

## 下一步的思路（2026-09-03 定）

### 问题的定性

headless 缺的是**宿主这个角色**，不是缺文件、不是缺窗口、也不是确认闸。报单的判决与执行
都在 `TC.exe` 一侧，TPyth 只是转述层，而它转述之前要先问宿主（`func 80`）。全模式下
`Tdxw.exe` 接过请求、递给 TC、再把结果写回缓冲区。headless 宿主什么都没写回，
所以拿到 `Value=0`。

### 路径甲：补齐宿主角色

在宿主里实现 `func 80` 的应答。**不推荐先做**：要逆两层——既要知道那 160 字节应答的格式，
还要知道怎么把单子递给 TC；而且是定长二进制，字段偏移错一位就是一笔价格或方向错的委托，
而它落在交易通道上。`TCO_Data` 与 `TCO_Func` 都不是现成的出口（见上一节实测）。

### 路径乙：绕过 TPyth，直接冒充 Tdxw 跟 TC 对话（推荐）

`TC.exe` 连到 HQMP 端口之后说的是**明文 JSON**，本次已抓到 `RegisterClient`、
`GetInjectHwnd`、`NotifyMsgClient`、`RawExternSwitch`、`ReturnValueComp` 等方法名。
所以不必逆定长二进制，只要把「`Tdxw.exe` 如何让 `TC.exe` 报单」这段请求录成明文。

选它的三个理由：

1. **明文可读，而且有正确答案可对照**——同一笔单在全模式下录一次，就有一份正确样本；
2. **顺带绕开「待用户确认」那道闸**，也就不再需要往 `aireq.html` 注入脚本。那套注入是整个
   方案里最脆的一环：客户端升级会把页面冲掉，`health.py` 专门为它留了第四项自检；
3. **走到底可以连 `TPyth.dll` 与 `TdxCopilot.dll` 都不要**，客户端只剩 `NewTc`。现在最小
   目录 100 MB 里有 82 MB 是 `NewTc`，剩下约 17 MB 全是这两个 DLL 及其运行库。

### 路径乙的做法

1. 把完整实验副本的 `TMTconfig.ini` 改成 `[HQMP] Port = 13576`；
2. 自建一个记录型 TCP 代理监听 **13575**、转发到 13576；
3. 注册表里 `D:\Common\TDXV2026X64_trade_lab_20260903\ → 13575` 已经是这个值，所以 TC 会
   连到代理。要盯一下 `Tdxw.exe` 启动时会不会把这条路由改写成 13576，会的话在起 TC 前改回去；
4. 在**实验副本的全模式**下报一笔，把双向 JSON 录下来；
5. 认出报单请求，在 headless 宿主里复现，用同一笔单比对回执。

全程只动实验副本的一个端口号和自建代理，不碰生产目录，也不碰生产的注册表路由
（生产那条是 `D:\Common\TDXV2026X64\ → 13573`）。

### 验收上必须卡死的一点

这条路最危险的失败不是「报不出去」，而是**「报出去了但字段认错了」**。所以顺序必须是：
先用远离盘口的价格报一笔，再去柜台的委托明细里把代码、方向、数量、价格、委托类型**逐个
核对上**，才谈自动化。协议是私有的，客户端升级可能变，所以还要加一道与最小目录白名单
同样性质的版本核对步骤。

### 一个可以利用的便利

确认闸是本地判决、在柜台之前，所以**收市也会触发**（2026-09-03 22:00 实测，全模式回
`Value=1`）。这意味着这类实验不必占用盘中时间，晚上就能迭代；只有最后核对柜台委托明细
那一步要放在交易时段。

### 如果路径乙也不通

那就只剩「headless 只承担查询、报单回全模式」。按 2026-09-03 与使用者确认的口径，
**只读通道不作为交付形态**，所以那种情况下这条路是暂时不落地，而不是降级交付。

## 其他实测发现（2026-09-03）

- **最小运行目录缺 `PYPlugins/sys/tqcenter.py`**：厂商委托类型编号表是用 `ast` 从这个
  源码文件里读的，最小副本没有它 ⇒ 驱动那条正规报单路径在最小目录上必然抛
  `DriverError`。白名单要补上这个文件。
- **HQMP 13575 与 PYMP 14572 绑在 `0.0.0.0`**，只有 MCP 17711 绑 `127.0.0.1`。这是厂商
  DLL 自己的绑定行为，与本服务「只绑回环且不做成配置项」的口径冲突，需要在宿主之外用
  防火墙规则堵掉。
- **生产 `Tdxw.exe` 会自己带起 `TC.exe`**，而且 TC 起来后主交易窗口是隐藏的、不弹
  `#32770` 登录框 ⇒ `ensure_logged_in` 会走到「交易模块在跑但不弹框」那个超时分支。
  这与 `login.py` 中 `start_trade_module` 的实测注释已经不一致，配方的拉起假设要复核。
- 实验副本的 `TC.exe` 第二次启动时会自动登录，不再要密码。
