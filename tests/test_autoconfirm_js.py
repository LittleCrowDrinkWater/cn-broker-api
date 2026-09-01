"""注入客户端那段自动确认脚本的行为测试。

被测的是 `autoconfirm.INJECT` 本身（剥掉 `<script>` 标签后交给 node 跑），假件照
`aireq.html` 的真实语义办事，细节见 `tests/autoconfirm_harness.js` 的文件头。

为什么值得为一段 40 行的 JS 架 node：这段代码跑在券商客户端的 CEF 里，报出去的是真钱，
而它的失败形态是**静默漏笔**——2026-08-28 漏过一批（同步连发被浏览器吞掉），
2026-09-01 又漏了一笔卖券还款（load 重建 list 让行引用失配），两次都是事后翻
localStorage 才发现的。这种东西必须有个能在改代码时立刻说话的门。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cn_broker_api.drivers.tdxquant.autoconfirm import INJECT

HARNESS = Path(__file__).with_name("autoconfirm_harness.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跳过注入脚本的行为测试")


def _patch_js() -> str:
    """把 INJECT 里那段自己写的脚本正文取出来（丢掉 <script src> 那行和标签）。"""
    bodies = re.findall(r"<script>(.*?)</script>", INJECT, re.S)
    assert len(bodies) == 1, f"注入块里应当只有一段内联脚本，找到 {len(bodies)} 段"
    return bodies[0]


def _run(scenario: str, tmp_path: Path) -> dict:
    js = tmp_path / "inject.js"
    js.write_text(_patch_js(), encoding="utf-8")
    p = subprocess.run(["node", str(HARNESS), str(js), scenario],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        pytest.fail(f"台架跑失败（returncode={p.returncode}）：{p.stderr[-800:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_a_batch_survives_the_page_reloading_its_list(tmp_path):
    """一批四笔、`load()` 一直在中间重建 list ⇒ 四笔都要发出去，一笔都不许漏。

    这是 2026-09-01 那次事故的形状：14:57:07 四笔卖券还款同时进队列，编号 6 那笔的行引用
    在 100ms 后已经不在 list 里，`indexOf` 返回 -1 静默不发，而幂等标记当时写在排期处
    ⇒ 后面每一轮都跳过它，差点让融资负债过夜。
    """
    r = _run("batch_with_reloads", tmp_path)
    assert sorted(r["sent"]) == sorted(r["expectSent"]), (
        f"漏笔了：发出去的是 {r['sent']}，应当是 {r['expectSent']}")


def test_no_signal_is_sent_twice(tmp_path):
    """重排不许换来重复报单——同一笔重复发出去就是双份成交。"""
    r = _run("batch_with_reloads", tmp_path)
    assert len(r["sent"]) == len(set(r["sent"])), f"有笔被重复发送：{r['sent']}"


def test_the_index_handed_to_send_points_at_that_row(tmp_path):
    """交给 `send(row, i)` 的索引必须指向那一行。

    页面的 `remove(t, e)` 是按索引给 `list[e]` 打 `ACTED` 标记的 ⇒ 索引错了会让**另一行**的
    【发送】【取消】按钮消失，那一笔既发不出去、人也点不了。
    """
    r = _run("batch_with_reloads", tmp_path)
    assert r["misindexed"] == [], f"索引与行对不上：{r['misindexed']}"


def test_the_idempotency_mark_only_covers_what_really_went_out(tmp_path):
    """localStorage 里记下的必须**恰好**是真发出去的那些。

    记多了就是「记了账没干活」——那一笔从此每轮都被跳过，永久卡在队列里；
    记少了则会重复报单。这个等式是这段代码唯一的自证据。
    """
    r = _run("batch_with_reloads", tmp_path)
    assert sorted(r["marked"]) == sorted(r["sent"]), (
        f"幂等标记 {r['marked']} 与真发出去的 {r['sent']} 不一致")


def test_the_five_gates_still_hold(tmp_path):
    """别的账户、市价单、超股数、超金额、陈旧信号、已发送的历史行，一个都不许碰。"""
    r = _run("gates", tmp_path)
    assert r["sent"] == r["expectSent"], (
        f"闸漏了：发出去的是 {r['sent']}，只该发 {r['expectSent']}")


def test_the_badge_says_how_many_were_left_behind(tmp_path):
    """该发而没发出去的笔数要出现在徽标上。

    这是漏笔唯一的**运行期**读数：行龄闸一过，自动那侧再也不会碰它（这条闸不能为了重试
    放宽——早上的陈旧信号在下午被顺手发出去是队列形态里最危险的一格），所以只能让人看见。
    """
    r = _run("gates", tmp_path)
    assert "漏" in r["badge"], f"徽标没报漏笔：{r['badge']!r}"
