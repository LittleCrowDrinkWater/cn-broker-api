"""闩的用例：两个计数器职责不同，别混。

每一条都对应一个具体的坏结果，不是覆盖率。
"""
from __future__ import annotations

from datetime import date

import pytest

from cn_broker_api.state import SubmitBlocked, SubmitLatch

D1 = date(2026, 8, 21)
D2 = date(2026, 8, 22)


def _latch(tmp_path, per_day=10, fails=3):
    return SubmitLatch(tmp_path, max_per_day=per_day, max_consecutive_failures=fails)


class TestConsecutiveFailures:
    """防券商锁定的那个闸。**跨天累计，成功才清零。**"""

    def test_blocks_after_the_limit(self, tmp_path):
        L = _latch(tmp_path, fails=2)
        L.claim("A")
        L.claim("A")
        with pytest.raises(SubmitBlocked) as ei:
            L.claim("A")
        assert "连续" in str(ei.value)

    def test_a_success_clears_the_count(self, tmp_path):
        """⭐ 成功登录清零——券商那边的计数也是这么清的。"""
        L = _latch(tmp_path, fails=2)
        L.claim("A")
        L.settle("A", ok=True)
        assert L.consecutive_failures("A") == 0
        L.claim("A")            # 清零之后还能再来
        L.claim("A")
        with pytest.raises(SubmitBlocked):
            L.claim("A")

    def test_a_failure_does_not_double_count(self, tmp_path):
        """🔴 `claim()` 已经先按失败记过了，`settle(ok=False)` 不该再加一次。"""
        L = _latch(tmp_path, fails=3)
        L.claim("A")
        L.settle("A", ok=False)
        assert L.consecutive_failures("A") == 1

    def test_it_has_no_day_dimension_at_all(self, tmp_path):
        """⭐⭐ 这一条是整个改动的理由：**这个计数器不能有"天"这一维**。

        券商那边的锁定计数只在成功登录时清零 ⇒ 按天算的话，密码真错了会一天一天地攒。

        🔴 判据落在**落盘的形状**上，不是落在「传不同的 day 会怎样」上：
        `consecutive_failures()` 压根没有 day 参数（刻意的），所以传 day 变化根本模拟不出
        跨天。我第一版就是那么写的，而把落盘改成按天分文件之后**那条用例照样绿**——
        它测的不是它名字说的那件事。
        ⇒ 直接断言「装它的文件名里没有日期」。
        """
        L = _latch(tmp_path, fails=2)
        L.claim("A", day=D1)
        L.claim("A", day=D2)         # 跨了一天，照样累计
        with pytest.raises(SubmitBlocked):
            L.claim("A", day=D2)
        assert L.consecutive_failures("A") == 2

        # 落盘形状：装连续失败的文件**有且只有一个，且文件名里没有日期**。
        fail_files = [f.name for f in tmp_path.iterdir()
                      if "fail" in f.name.lower()]
        assert fail_files == ["latch-failures.json"], (
            f"连续失败计数被拆成了按天的文件（{fail_files}）⇒ 又变成按天清零了")
        assert not any(ch.isdigit() for ch in fail_files[0]), (
            "文件名里带数字（多半是日期）⇒ 这个计数器又长出「天」这一维了")

    def test_it_counts_per_account(self, tmp_path):
        L = _latch(tmp_path, fails=1)
        L.claim("A")
        L.claim("B")                 # 另一个账户不受影响
        assert L.consecutive_failures("A") == 1
        assert L.consecutive_failures("B") == 1

    def test_an_unreadable_file_is_treated_as_used_up(self, tmp_path):
        """坏文件的两种解释里取保守那边：代价是要人工登一次。"""
        L = _latch(tmp_path)
        (tmp_path / "latch-failures.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(SubmitBlocked):
            L.claim("A")


class TestDailyCap:
    """防我们自己的代码循环提交的那个闸。**按天清零。**"""

    def test_blocks_after_the_daily_limit(self, tmp_path):
        L = _latch(tmp_path, per_day=2, fails=99)
        L.claim("A")
        L.settle("A", ok=True)       # 成功也算一次
        L.claim("A")
        L.settle("A", ok=True)
        with pytest.raises(SubmitBlocked) as ei:
            L.claim("A")
        assert "今天" in str(ei.value)

    def test_a_success_still_consumes_the_daily_budget(self, tmp_path):
        """⭐ 与连续失败那个闸相反：这个数的是**提交**，成功也算。

        原来只有这一个闸、还设成 1，后果就是「09:00 成功登了一次，
        11:00 客户端掉了就再登不回去」。
        """
        L = _latch(tmp_path, per_day=1, fails=99)
        L.claim("A")
        L.settle("A", ok=True)
        assert L.used("A") == 1 and L.remaining("A") == 0

    def test_it_resets_the_next_day(self, tmp_path):
        L = _latch(tmp_path, per_day=1, fails=99)
        L.claim("A", day=D1)
        L.claim("A", day=D2)         # 新的一天，日额度重新给
        assert L.used("A", day=D2) == 1

    def test_the_two_counters_are_independent(self, tmp_path):
        """把它们混成一个计数器正是 2026-08-21 那个错。"""
        L = _latch(tmp_path, per_day=5, fails=2)
        L.claim("A")
        L.settle("A", ok=True)       # 清了连续失败，但日计数留着
        assert L.consecutive_failures("A") == 0
        assert L.used("A") == 1

    def test_it_survives_a_restart(self, tmp_path):
        """放内存的话「重启三次 ＝ 无声无息地试了三次密码」。"""
        _latch(tmp_path, per_day=1, fails=99).claim("A")
        with pytest.raises(SubmitBlocked):
            _latch(tmp_path, per_day=1, fails=99).claim("A")
