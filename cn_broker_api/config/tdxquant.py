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
    #: 交易密码从哪来：
    #:   file    ＝ 本服务自己读 `cred_file`（搬家阶段，行为与搬家前逐字节相同）
    #:   request ＝ 后端在调用里带过来，本服务**只在内存留当天一份、绝不落盘**
    #: 见设计稿 §5.6：凭据迁移自带 DB 迁移 + 后端界面 + 真机验证，不混进搬家。
    cred_source: str = "file"
    #: `cred_source = "file"` 时的凭据文件。**必须在仓库之外。**
    cred_file: Optional[Path] = None
    #: 每个账户每天最多提交几次交易密码。
    #: ⚠️ 券商的锁定策略**未核实**（错几次会怎样、锁多久、要不要人工解锁都不知道）
    #: ⇒ 不拿尝试次数去试。想调大先去券商确认策略，别按感觉调。
    max_password_submits_per_day: int = 1
