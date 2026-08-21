"""入口：起服务。

    python -m cn_broker_api

## 为什么只绑 127.0.0.1 而且不做成配置项

这个端口能下单。让它监听外网不该是一个"选项"——一个配置项的存在本身就是在邀请别人去改它。

## 为什么 stdout/stderr 都要归一到 utf-8

本机控制台是 GBK，日志里的中文（和那几个 emoji 标记）会在最不该崩的时候崩——
比如登录流程正走到一半、正要打印"密码框里星号总宽"那一行。
⭐ **两个都要归一**：logging 默认写 stderr，只归一 stdout 的话，真出问题那次的日志全是乱码，
而那几行就是全部线索。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

#: ⚠️ 只绑回环，且**刻意不从配置读**（见模块 docstring）。
BIND_HOST = "127.0.0.1"


def _init_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass          # 被重定向到不支持 reconfigure 的对象时不该因此起不来


def _init_logging(state_dir: Path) -> None:
    """同时写控制台与文件。**文件是必须的**：无人值守那一趟没人看着控制台，
    而出问题时那几行就是全部线索。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(state_dir / "cn-broker-api.log",
                                            encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def build_driver(cfg):  # noqa: ANN001, ANN201
    """按配置造驱动。**惰性 import**：真驱动是 Windows 专属，
    在别的平台上跑纸面驱动时不该因为 import 就起不来。"""
    from cn_broker_api.state import PasswordVault, SubmitLatch

    if cfg.driver == "paper":
        from cn_broker_api.drivers.paper import PaperDriver

        return PaperDriver()

    from cn_broker_api.drivers.tdxquant.driver import TdxQuantDriver

    latch = SubmitLatch(cfg.server.state_dir,
                        max_per_day=cfg.tdxquant.max_password_submits_per_day)
    return TdxQuantDriver(cfg.tdxquant, latch=latch, vault=PasswordVault())


def main() -> int:
    _init_stdio()
    from cn_broker_api.config import CONTRACT_VERSION, ConfigError, load

    try:
        cfg = load()
    except ConfigError as e:
        print(f"配置有问题：{e}", file=sys.stderr)
        return 2

    _init_logging(cfg.server.state_dir)
    log = logging.getLogger("cn_broker_api")

    driver = build_driver(cfg)
    from cn_broker_api.http_app import create_app, load_or_create_token

    token = load_or_create_token(cfg.token_file)
    app = create_app(cfg, driver, token=token)

    log.info("契约 v%s | 驱动 %s | 能力 %s", CONTRACT_VERSION, driver.name,
             ",".join(driver.capabilities()))
    log.info("配置 %s", cfg.source_path or "(没找到配置文件，全套默认值)")
    log.info("token %s", cfg.token_file)
    log.info("监听 http://%s:%s  ——  诊断页在 /", BIND_HOST, cfg.server.port)
    if cfg.driver == "paper":
        log.warning("当前是**纸面驱动**：不连任何客户端，四项检查恒绿且都标着 warn")

    from waitress import serve

    # threads=8：本服务的并发极低（一个使用者 + 一个诊断页），但登录那一趟会占住一个线程
    # 几十秒，所以不能只给 1~2 个，否则那期间连 /v1/health 都打不动。
    serve(app, host=BIND_HOST, port=cfg.server.port, threads=8,
          ident="cn-broker-api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
