"""代码归一。裸 6 位码在客户端那侧不抛异常、不会自己暴露，所以这一层必须对。"""
from __future__ import annotations

import pytest

from cn_broker_api.symbols import market_of, to_tq_code


@pytest.mark.parametrize("code,market", [
    ("600519", "SH"),
    ("688111", "SH"),
    ("000001", "SZ"),
    ("300750", "SZ"),
    ("920819", "BJ"),      # 北交所新号段，必须先于「9 -> SH」判
    ("900901", "SH"),      # 老沪 B 股
    ("510300", "SH"),      # 沪市 ETF
    ("430047", "BJ"),
    ("830799", "BJ"),
])
def test_market_of(code, market):
    assert market_of(code) == market


def test_bare_code_gets_a_suffix():
    assert to_tq_code("000761") == "000761.SZ"


def test_suffixed_code_is_left_alone():
    assert to_tq_code("000761.sz") == "000761.SZ"


def test_short_numeric_code_is_zero_padded_for_the_market_judgment():
    assert market_of("1") == "SZ"
