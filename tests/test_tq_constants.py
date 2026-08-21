"""厂商编号表：读厂商源码，不抄第二份，读不出来就明确失败。"""
from __future__ import annotations

import pytest

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.tdxquant.tq_constants import TqConstants

VENDOR = '''
import numpy as np
import pandas as pd
import ctypes

dll = ctypes.CDLL("does-not-exist.dll")


class ConstMeta(type):
    def __setattr__(cls, name, value):
        raise AttributeError(name)


class tqconst(metaclass=ConstMeta):
    STOCK_BUY = 0
    STOCK_SELL = 1
    CREDIT_FIN_BUY = 69
    CREDIT_STK_REPAY = 76
    PRICE_MY = 0

    def __setattr__(self, name, value):
        raise AttributeError(name)
'''


def _pyplugins(tmp_path, source=VENDOR, where="sys"):
    d = tmp_path / where
    d.mkdir(parents=True, exist_ok=True)
    (d / "tqcenter.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_reads_the_numbers_without_importing_the_vendor_module(tmp_path):
    """⭐ 关键在**没有 import**：厂商那个模块顶层要 numpy/pandas 并加载 64 位 DLL
    （这份假件里那个 DLL 压根不存在），而 mcp 通道一样都不需要。"""
    c = TqConstants.load(_pyplugins(tmp_path))
    assert (c.STOCK_BUY, c.STOCK_SELL, c.PRICE_MY) == (0, 1, 0)
    assert (c.CREDIT_FIN_BUY, c.CREDIT_STK_REPAY) == (69, 76)


def test_user_directory_also_counts(tmp_path):
    assert TqConstants.load(_pyplugins(tmp_path, where="user")).STOCK_BUY == 0


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(DriverError, match="tqcenter.py"):
        TqConstants.load(tmp_path)


def test_unknown_name_fails_loudly(tmp_path):
    c = TqConstants.load(_pyplugins(tmp_path))
    with pytest.raises(AttributeError, match="NO_SUCH_KIND"):
        _ = c.NO_SUCH_KIND


def test_a_shrunken_table_is_refused(tmp_path):
    """缺 `PRICE_MY` 这类必备项 ⇒ 多半是找错文件或厂商改了结构，**别照旧报单**。"""
    src = VENDOR.replace("    PRICE_MY = 0\n", "")
    with pytest.raises(DriverError, match="PRICE_MY"):
        TqConstants.load(_pyplugins(tmp_path, src))


def test_computed_constants_are_refused_not_guessed(tmp_path):
    """厂商开始算这些数了 ⇒ ast 这条路不再成立，要有人看一眼，不能猜一个值继续报单。"""
    src = VENDOR.replace("    CREDIT_FIN_BUY = 69", "    CREDIT_FIN_BUY = 60 + 9")
    with pytest.raises(DriverError, match="不是字面量"):
        TqConstants.load(_pyplugins(tmp_path, src))
