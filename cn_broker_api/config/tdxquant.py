"""桌面客户端驱动的配置：装在哪、凭据从哪来、每天最多试几次密码。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TdxQuantConfig:
    #: 客户端安装根目录。**本服务里唯一写死机器路径的地方**：补丁页面
    #: （`webs/cfg/aireq.html`）、可执行文件都从这里推。两处各写一份的话，换客户端时
    #: 改一处、另一处静默指向不存在的目录——而"文件不在"会被自检读成"补丁没打"。
    tdx_home: Optional[Path] = None
    #: MCP over HTTP 的地址。端口属主是客户端进程自己（`Tdxw.exe`）。
    mcp_url: str = "http://127.0.0.1:17709"
    #: 跟客户端说话走哪条通道：`mcp`（HTTP，端口属主是客户端进程）或 `ctypes`（加载 DLL）。
    #: **不自动回落**：两条通道的故障表现完全不同，自动回落会让"我以为走的是 A、其实
    #: 走的是 B"，而这是交易通道——同一个操作在两条路上可能一个成功一个失败。
    transport: str = "mcp"
    #: 撤单之后最多等几秒去确认它真撤掉了（墙钟）。等不到不算撤成功。
    cancel_confirm_timeout: float = 5.0
    #: 撤单确认的重查间隔（秒）。
    cancel_confirm_interval: float = 1.0
    #: 交易密码从哪来：
    #:   file    ＝ 本服务自己读 `cred_file`（搬家阶段，行为与搬家前逐字节相同）
    #:   request ＝ 后端在调用里带过来，本服务**只在内存留当天一份、绝不落盘**
    #: 见设计稿 §5.6：凭据迁移自带 DB 迁移 + 后端界面 + 真机验证，不混进搬家。
    cred_source: str = "file"
    #: `cred_source = "file"` 时的凭据文件。**必须在仓库之外。**
    cred_file: Optional[Path] = None
    #: 每个账户每天最多提交几次交易密码（含成功的那次），午夜清零。
    #: 它数的是提交次数，**不是防券商锁定的闸**——防锁定的是下面那个。
    #: 它防的是我们自己的代码循环提交。
    max_password_submits_per_day: int = 10
    #: **连续**提交没成功几次之后停手（成功登录清零、**不按天清零**）。
    #: 券商那边的计数通常也是这个口径，按天算的话密码真错了会一天一天地攒。
    #: 券商的锁定阈值本项目未核实 ⇒ 取 3。
    max_consecutive_failures: int = 3
