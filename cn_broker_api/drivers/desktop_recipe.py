"""一份桌面监护配方——**数据，不是代码**。

- **机制**（与厂商无关）：找窗口、分类是哪道门、填密码框、按颜色找按钮、量星号总宽、最小化。
- **配方**（每个驱动一份数据）：保证哪几个进程在跑、可执行文件相对路径、
  登录框怎么认、「登上了没有」用哪个调用验。

⇒ 换一个更精简的客户端版本，多半是**加一份配方**，不是写新代码。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass(frozen=True)
class DesktopRecipe:
    """一份桌面监护配方——**数据，不是代码**（见模块 docstring）。

    换客户端版本时改这里；`desktop/` 那套机制一行不动。
    """

    #: 必须在跑的进程（按顺序保证）。⚠️ 交易那一半往往**不会自己起来**：主程序的自动登录
    #: 只登行情，交易模块要人点一下才拉起，缺它的表现是「既没登上、也没有登录框」的僵局。
    processes: Tuple[str, ...] = ()
    #: 进程名 → 安装目录下的相对路径。起进程都不带参数（客户端自己拉起它们时也不带）。
    executables: Dict[str, str] = field(default_factory=dict)
    #: 客户端（行情）登录窗的判别标志。⭐ 两个窗口都含密码框，光看"有没有密码框"会把它们
    #: 混成一个 ⇒ **必须先分类再动手**。
    client_login_markers: Tuple[str, ...] = ()
    #: 密码框的类名（厂商的安全输入控件）。标准 Edit 带 `ES_PASSWORD` 位也算，两条判据并列。
    password_classes: Tuple[str, ...] = ()
