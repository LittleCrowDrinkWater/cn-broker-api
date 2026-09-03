"""配置与模板的用例。

这里防的是一类很具体的退化：**加了一个配置项，忘了写进模板**。
表现是使用者压根不知道有这个开关，而代码里它已经在起作用了。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from cn_broker_api.config import ConfigError, load

NL = chr(10)
REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "config.example.toml"


def test_every_setting_appears_in_the_template():
    """`describe()` 列出的每一项都要能在模板里找到——活跃项或注释掉的示例都算。

    🔴 这条用例的价值在于它**只会因为"人忘了"而红**：加配置项时顺手写一段注释就过，
    而漏掉的话，使用者要靠读源码才知道有这个开关。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    cfg = load(TEMPLATE)
    missing = [key for key, _v, _given in cfg.describe()
               if key.rsplit(".", 1)[-1] not in text]
    assert not missing, f"这些配置项没写进 config.example.toml：{missing}"


#: 只属于某一台机器的东西：模板里出现任何一条就是泄露。
#: ⭐ 这份名单是**具体串**而不是「像路径的东西」：后者会把 `D:/YourBroker/TdxClient`
#: 这种占位符也判成泄露，于是没人再理它。
BS = chr(92)
MACHINE_SPECIFIC = ("TDXV2026X64", "new_tdx_mock", "D:/Agent", "D:" + BS + "Agent")

#: 资金账号长这样：连续 8 位以上的数字。
#: ⭐ 用**形状**而不是把真账号写进来——这个仓库要公开，
#: 「为了检测泄露而把要检测的那个串写进仓库」是自相矛盾的。
LONG_DIGITS = re.compile(r"\d{8,}")


def test_the_template_carries_no_machine_specific_values():
    """🔴 模板要进 git，而配置里装的是客户端装在哪、凭据文件路径这类只属于一台机器的东西。

    这条盯的是一次真实的泄露：模板原来是照着本机那份抄的，四处真实路径就那么进了仓库
    （客户端安装目录、状态目录）。真值现在放同目录的 `config.toml`，
    被 `.gitignore` 挡着。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    leaked = [s for s in MACHINE_SPECIFIC if s in text]
    assert not leaked, f"模板里出现了只属于某台机器的串：{leaked}"
    digits = LONG_DIGITS.findall(text)
    assert not digits, f"模板里有长数字串，像是真的资金账号：{digits}"


def test_the_real_config_is_not_tracked_by_git():
    """真值那份必须被 .gitignore 挡着。

    ⚠️ 判据落在 `.gitignore` 的规则上而不是「文件在不在」：文件在别人机器上可能压根不存在，
    而规则必须一直在。
    """
    rules = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.toml" in rules, "*.toml 这条挡不住的话，真配置会被推上去"
    assert "!config.example.toml" in rules, "模板必须放行，否则没人知道有哪些可配置"


def test_the_template_loads_as_is():
    """模板本身必须是能读的合法配置——一个读不出来的模板会让人以为是自己抄错了。"""
    cfg = load(TEMPLATE)
    assert cfg.server.port == 17710
    assert cfg.watchdog.enabled is False, "模板里看门狗必须是关的（它会真起进程）"


def test_describe_marks_defaults_apart_from_written_values(tmp_path):
    """⭐ 漏写一项和写了一项写错值，现象完全不同，而只看最终值分不出这两种。"""
    p = tmp_path / "c.toml"
    p.write_text(NL.join(["[server]", "port = 18888"]), encoding="utf-8")
    rows = {k: (v, given) for k, v, given in load(p).describe()}
    assert rows["server.port"] == ("18888", True)
    assert rows["health.cache_seconds"][1] is False, "没写的项必须标成在吃默认值"


@pytest.mark.parametrize("bad", ["9:0:0", "24:00", "上午九点", "0900"])
def test_a_malformed_window_fails_loudly(tmp_path, bad):
    """🔴 时段写歪了**当场报错**，不许静默退回默认值：
    静默的表现是「看门狗在我没让它工作的时段动手了」，而那时候人已经不记得自己写过什么。"""
    p = tmp_path / "c.toml"
    p.write_text(NL.join(["[watchdog]", f'window_start = "{bad}"']),
                 encoding="utf-8")
    with pytest.raises(ConfigError):
        load(p)


def test_an_unknown_cred_source_fails_loudly(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(NL.join(["[driver.tdxquant]", 'cred_source = "env"']),
                 encoding="utf-8")
    with pytest.raises(ConfigError):
        load(p)


def test_an_unknown_desktop_mode_fails_loudly(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(NL.join(["[driver.tdxquant]", 'desktop_mode = "tiny"']),
                 encoding="utf-8")
    with pytest.raises(ConfigError):
        load(p)


def test_a_missing_config_file_still_starts(tmp_path):
    """缺文件不是错：全套默认值也起得来。"""
    cfg = load(tmp_path / "nope.toml")
    assert cfg.source_path is None and cfg.server.port == 17710
