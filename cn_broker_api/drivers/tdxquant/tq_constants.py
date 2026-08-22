"""厂商的委托类型/报价方式编号表。**读厂商那份源码，不抄第二份**。

编号只该有厂商一份，改号我们跟着变——所以这里既不写死一张表，也不 import 厂商模块：

 `tqcenter.py` 是**单个模块**，顶层就 `import numpy` / `import pandas` 并 `ctypes.CDLL`
一个 64 位 DLL。走 mcp 通道时这三样一个都不需要（连接是 HTTP），为了一张全是整数字面量的
常量表把科学计算栈和 DLL 加载拖进这个服务，不划算。⇒ 用 `ast` 把那个类的类体读出来。

 读不出来就**明确失败**，绝不退回一份我们自己写的表：厂商改了编号而我们用旧数字报单，
表现是"报出去被柜台拒"或更糟——报成另一种委托类型（69 融资买入 / 76 卖券还款只差一个数字）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Dict, Optional

from cn_broker_api.drivers.driver_error import DriverError

logger = logging.getLogger(__name__)

#: 厂商源码在 PYPlugins 下的两个位置（`sys` 是随客户端装的那份）。
_CANDIDATES = ("sys/tqcenter.py", "user/tqcenter.py")

#: 类名。厂商把它写成小写类名，别按类名风格"纠正"过来。
_CLASS_NAME = "tqconst"

#: 报单最少要这几个才算表是完整的。缺了就是找错文件或厂商改了结构。
_REQUIRED = ("STOCK_BUY", "STOCK_SELL", "PRICE_MY")


class TqConstants:
    """厂商编号表的一份只读快照。属性名与厂商的 `tqconst` 一字不差。"""

    def __init__(self, values: Dict[str, int], source: Path) -> None:
        self._values = dict(values)
        self.source = source

    def __getattr__(self, name: str) -> int:
        try:
            return self._values[name]
        except KeyError as e:
            raise AttributeError(
                f"厂商编号表里没有 {name}（读自 {self.source}）——客户端版本可能过旧") from e

    def as_dict(self) -> Dict[str, int]:
        return dict(self._values)

    @classmethod
    def load(cls, pyplugins: Optional[Path]) -> "TqConstants":
        """从 PYPlugins 目录下的厂商源码里读编号表。

        Raises:
            DriverError: 找不到源码、找不到那个类、或者类体里有不是字面量的项。
        """
        if not pyplugins:
            raise DriverError("没配 driver.tdxquant.tdx_home ⇒ 读不到厂商编号表")
        root = Path(pyplugins)
        for rel in _CANDIDATES:
            path = root.joinpath(*rel.split("/"))
            if path.is_file():
                return cls(_parse(path), path)
        raise DriverError(f"{root} 下找不到 tqcenter.py（找过 {', '.join(_CANDIDATES)}）"
                          f"⇒ 确认 tdx_home 指的是装了量化模块的那份客户端")


def _parse(path: Path) -> Dict[str, int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError) as e:
        raise DriverError(f"厂商源码读不出来（{path}）：{e}") from e

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == _CLASS_NAME:
            values = _class_body(node, path)
            missing = [k for k in _REQUIRED if k not in values]
            if missing:
                raise DriverError(f"{path} 里的 {_CLASS_NAME} 缺 {missing} ⇒ 结构变了，"
                                  f"别照旧报单")
            logger.info("[tdx] 编号表读自 %s（%d 项）", path, len(values))
            return values
    raise DriverError(f"{path} 里没有 class {_CLASS_NAME} ⇒ 客户端版本对不上")


def _class_body(node: ast.ClassDef, path: Path) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for stmt in node.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue                       # `__setattr__` 那类定义跳过
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or stmt.value is None:
            continue
        try:
            value = ast.literal_eval(stmt.value)
        except ValueError as e:
            # 厂商开始算这些数了 ⇒ ast 这条路不再成立，必须有人看一眼，不能猜。
            raise DriverError(f"{path} 里 {names} 不是字面量（{ast.dump(stmt.value)[:80]}）"
                              f"⇒ 读编号表这条路要重做") from e
        if isinstance(value, int) and not isinstance(value, bool):
            for n in names:
                out[n] = value
    return out
