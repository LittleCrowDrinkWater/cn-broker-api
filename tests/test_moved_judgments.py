"""把「搬迁没改判据」焊住。

这些用例的意义不是覆盖率，是**证明搬迁是忠实的**：每一条对应一次真实故障，判据改了就变红。
所以断言写的是具体读数与具体文案，不是 `assert result.ok is True` 这种什么都焊不住的写法。

⭐ 跟客户端有关的都用假件，所以这个文件在 Linux 上也跑得过（CI 靠它）。
"""
from datetime import datetime

import pytest

from cn_broker_api.drivers.tdxquant import health as H
from cn_broker_api.drivers.tdxquant.mcp import has_money_fields


class _Snap:
    """只实现 `query_snapshot` 的行情假件。"""

    def __init__(self, snap):
        self.snap = snap
        self.asked = []
        self.transport = "mcp"
        self.mcp_url = "http://127.0.0.1:17709"

    def query_snapshot(self, code):
        self.asked.append(code)
        return self.snap


# ── ① 「取不到」与「是 0」是两件事 ───────────────────────

def test_success_with_no_content_is_not_zero_equity():
    """厂商有一种「成功但没内容」的形态（`ErrorId=0` 而没有任何资金字段）。
    `if not asset` 拦不住它（dict 非空），照旧往下走会把它读成权益 0 ——
    而「取不到」该报查询失败，「权益是 0」会被当成真实数字去算仓位。"""
    assert has_money_fields({"ErrorId": "0", "Value": []}) is False
    assert has_money_fields({}) is False
    assert has_money_fields(None) is False
    assert has_money_fields({"Asset": "0"}) is True   # 真的 0 也算"取到了"


# ── ② 行情判的是「答不答话」，不是「有没有现价」 ──────────

def test_no_price_before_the_open_is_normal_not_a_failure():
    """2026-08-21 实盘推翻的判据：09:15 自检报「现价为 0 ⇒ 行情侧没数」判红，
    而同一天 09:20 报单两只全部报出、09:26 全部成交 ⇒ 那条红是假警报。
    集合竞价还没撮合，现价本来就是 0。**假警报比不探更糟。**"""
    r = H.check_quote(_Snap({"Now": "0", "LastClose": "11.10"}),
                      now=datetime(2026, 8, 21, 9, 15))
    assert r.ok is True and not r.warn
    assert "昨收 11.1" in r.detail and "09:15" in r.detail


def test_no_price_during_continuous_trading_is_a_failure():
    """反过来：连续竞价时段里没有现价，那才真是行情侧断了。
    这一格与上一格**只差时点**——判据里那一维要是被去掉，两条里必有一条变红。"""
    r = H.check_quote(_Snap({"Now": "0", "LastClose": "11.10"}),
                      now=datetime(2026, 8, 21, 10, 30))
    assert r.ok is False and "连续竞价时段内" in r.detail


def test_a_record_with_neither_price_nor_prev_close_is_a_failure():
    """"答了话但整条记录是空的"才是行情侧没数——与"没有现价"是两件事。"""
    r = H.check_quote(_Snap({"Now": "0", "LastClose": "0"}),
                      now=datetime(2026, 8, 21, 9, 15))
    assert r.ok is False and "现价与昨收都是 0" in r.detail


def test_snapshot_none_means_the_feed_is_down():
    r = H.check_quote(_Snap(None), now=datetime(2026, 8, 21, 10, 30))
    assert r.ok is False and "断开" in r.detail


def test_empty_book_warns_only_during_continuous_trading():
    """封板时盘口本来就可能只有一侧甚至全空 ⇒ 只 warn，不判红。
    但盘前盘口空是常态，连 warn 都是噪音。"""
    live = H.check_quote(_Snap({"Now": "11.23", "Buyp": ["0"], "Sellp": ["0"]}),
                         now=datetime(2026, 8, 21, 10, 30))
    assert live.ok is True and live.warn is True
    pre = H.check_quote(_Snap({"Now": "11.23", "Buyp": ["0"], "Sellp": ["0"]}),
                        now=datetime(2026, 8, 21, 9, 15))
    assert pre.ok is True and pre.warn is False


def test_probe_reads_the_clients_own_feed():
    """⭐ 探的必须是**客户端那条**行情。别处的行情源（自研 socket 直连公网服务器）
    在客户端断了的时候照样绿 ⇒ 只有这条能回答"报单会不会被行情状态挡住"。"""
    c = _Snap({"Now": "11.23", "LastClose": "11.10",
               "Buyp": ["11.22", "0"], "Sellp": ["11.23", "0"]})
    r = H.check_quote(c, probe_symbol="000001.SZ", now=datetime(2026, 8, 21, 10, 30))
    assert r.ok and not r.warn
    assert c.asked == ["000001.SZ"] and "11.23" in r.detail


# ── ③ 端口探针与业务调用是两件事 ─────────────────────────

def test_transport_probe_is_independent_of_login():
    """① 客户端进程在不在，与账号登没登**无关**。端口不通就一定是客户端没开，
    这一格必须能报出它自己那句话，而不是被"连接失败"盖掉。"""
    c = _Snap({})
    c.mcp_url = "http://127.0.0.1:1"      # 几乎不可能有人监听
    r = H.check_transport(c)
    assert r.ok is False and "不通" in r.detail


# ── ④ 没配安装目录要明确失败，不猜 ───────────────────────

def test_missing_tdx_home_fails_loudly():
    """猜一个路径的表现是"补丁没打"这种假警报 ⇒ 宁可明确报错。"""
    H.set_tdx_home(None)
    with pytest.raises(RuntimeError, match="没配客户端安装目录"):
        H.tdx_install_root()


def test_autoconfirm_reports_a_missing_page_rather_than_crashing(tmp_path):
    """④ 补丁那一项是纯读文件。目录对但文件不在 ⇒ 判红并说清是哪个文件，不抛异常
    （自检自己就是兜底件，它崩了就什么都不知道了）。"""
    H.set_tdx_home(tmp_path)
    r = H.check_autoconfirm()
    assert r.ok is False and "找不到页面" in r.detail
