"""客户端进程指纹：探活发现不了「句柄还在但数据冻住」，进程换过就直接重连。

这里钉的是**三分判定**，不是「指纹变了就重连」这一句：

  指纹变了   ⇒ 强制重连，**不问探活**（探活正是有盲区的那个）
  指纹没变   ⇒ 照旧探活
  指纹拿不到 ⇒ 这条判据整个跳过，退回探活

第三种是最容易写错、也最难在真机上发现的一种：非 Windows、枚举失败、客户端没在跑都归它。
把「没拿到答案」读成「变了」，客户端正常跑着也会一直重连；读成「没变」则等于这个函数
白写。两种误读都比不做更糟，所以三条各有一个用例。
"""
from __future__ import annotations

import pytest

from cn_broker_api.drivers.tdxquant import client as C


@pytest.fixture(autouse=True)
def _clean_shared():
    """每个用例前后都把进程级共享状态压平——它是模块级单例，串味会让用例互相影响。"""
    before = dict(C._SHARED)
    C._SHARED.update(tq=None, tqconst=None, account_id=None, key=None, client_fp=None)
    yield
    C._SHARED.update(before)


def _connected_client(monkeypatch, fp_now, fp_stored):
    """造一个「已连上」的把手，并把重连那条路换成记账用的假件。"""
    cli = C.TdxQuantClient(pyplugins_path=".", account="A1", account_type="CREDIT",
                           transport="mcp", mcp_url="http://127.0.0.1:1")
    C._SHARED.update(tq=object(), account_id=1, key=cli._key, client_fp=fp_stored)

    calls = {"probe": 0, "rebuild": 0}

    def fake_probe(_self):
        calls["probe"] += 1
        return True                       # 探活一律说「活着」，好让分歧只来自指纹

    def fake_rebuild(_self):
        calls["rebuild"] += 1
        raise RuntimeError("rebuild")     # 走到重建就抛，用例据此判定

    monkeypatch.setattr(C.TdxQuantClient, "_probe", fake_probe)
    monkeypatch.setattr(C.TdxQuantClient, "hard_reset", fake_rebuild)
    monkeypatch.setattr(C, "client_fingerprint", lambda: fp_now)
    return cli, calls


def test_fingerprint_changed_forces_reconnect_without_trusting_probe(monkeypatch):
    """进程换过 ⇒ 直接重连，且**不看探活脸色**（探活这里是假的「活着」）。"""
    cli, calls = _connected_client(monkeypatch, fp_now="222", fp_stored="111")
    with pytest.raises(RuntimeError, match="rebuild"):
        cli.connect()
    assert calls["rebuild"] == 1
    assert calls["probe"] == 0, "指纹已经判死了，不该再去问探活"


def test_same_fingerprint_still_goes_through_probe(monkeypatch):
    """进程没换 ⇒ 照旧探活；探活说活着就复用，不重建。"""
    cli, calls = _connected_client(monkeypatch, fp_now="111", fp_stored="111")
    cli.connect()
    assert calls["probe"] == 1 and calls["rebuild"] == 0


def test_unknown_fingerprint_falls_back_to_probe(monkeypatch):
    """指纹拿不到（None）＝「没拿到答案」，**不是**「变了」⇒ 退回探活，别重连。"""
    cli, calls = _connected_client(monkeypatch, fp_now=None, fp_stored="111")
    cli.connect()
    assert calls["probe"] == 1 and calls["rebuild"] == 0


def test_no_stored_fingerprint_falls_back_to_probe(monkeypatch):
    """存量连接没记过指纹（升级前建立的）⇒ 同样不能判成「变了」。"""
    cli, calls = _connected_client(monkeypatch, fp_now="111", fp_stored=None)
    cli.connect()
    assert calls["probe"] == 1 and calls["rebuild"] == 0


def test_fingerprint_is_none_when_process_lookup_fails(monkeypatch):
    """枚举进程抛异常 ⇒ None，绝不外抛：它只是个附加信号，不该拖垮连接。"""
    def boom(_names):
        raise OSError("no such api")

    monkeypatch.setattr("cn_broker_api.drivers.tdxquant.login.running_processes", boom)
    assert C.client_fingerprint() is None


def test_fingerprint_is_none_when_client_not_running(monkeypatch):
    """客户端没在跑 ⇒ None（交给 connect 去报它自己那句话），而不是空串那种"新指纹"。"""
    monkeypatch.setattr("cn_broker_api.drivers.tdxquant.login.running_processes",
                        lambda _names: {})
    assert C.client_fingerprint() is None


def test_fingerprint_is_stable_regardless_of_enumeration_order(monkeypatch):
    """同一批进程、枚举顺序不同 ⇒ 指纹必须相同，否则会无缘无故重连。"""
    monkeypatch.setattr("cn_broker_api.drivers.tdxquant.login.running_processes",
                        lambda _names: {2404: "Tdxw.exe", 1200: "Tdxw.exe"})
    first = C.client_fingerprint()
    monkeypatch.setattr("cn_broker_api.drivers.tdxquant.login.running_processes",
                        lambda _names: {1200: "Tdxw.exe", 2404: "Tdxw.exe"})
    assert C.client_fingerprint() == first
