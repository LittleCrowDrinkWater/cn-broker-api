"""入口：`python -m cn_broker_api`。

 只绑 127.0.0.1，且**刻意不做成配置项**：这个端口能下单，一个配置项的存在本身就是
在邀请别人去改它。
 stdout 与 stderr **都**归一到 utf-8：本机控制台是 GBK，中文会在最不该崩的时候崩
（登录流程正走到一半）；logging 默认写 stderr，只归一 stdout 等于没归一。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from cn_broker_api.stdio import init_stdio

#: 只绑回环，且**刻意不从配置读**（见模块 docstring）。
BIND_HOST = "127.0.0.1"


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
                        max_per_day=cfg.tdxquant.max_password_submits_per_day,
                        max_consecutive_failures=cfg.tdxquant.max_consecutive_failures)
    return TdxQuantDriver(cfg.tdxquant, latch=latch, vault=PasswordVault())


def _log_config(log, cfg) -> None:  # noqa: ANN001
    """记录全部生效配置，并区分显式配置值与默认值。"""
    log.info("配置来源 | file=%s", cfg.source_path or "<defaults>")
    for key, value, from_file in cfg.describe():
        source = "configured" if from_file else "default"
        log.info("配置项 | source=%-10s key=%-44s value=%s", source, key, value)


def main() -> int:
    init_stdio()
    from cn_broker_api.config import CONTRACT_VERSION, ConfigError, load

    try:
        cfg = load()
    except ConfigError as e:
        print(f"配置校验失败：{e}", file=sys.stderr)
        return 2

    _init_logging(cfg.server.state_dir)
    log = logging.getLogger("cn_broker_api")

    driver = build_driver(cfg)
    from cn_broker_api.http_app import SingleFlight, create_app, load_or_create_token
    from cn_broker_api.state import WatchdogState
    from cn_broker_api.watchdog import Watchdog

    token = load_or_create_token(cfg.token_file)
    # 一把单飞锁，看门狗与 `/v1/session/ensure` 共用：各拿一把的表现是两个客户端进程。
    flight = SingleFlight()
    dog = Watchdog(driver, cfg.watchdog, flight=flight,
                   state=WatchdogState(state_dir=cfg.server.state_dir,
                                       max_starts_per_day=cfg.watchdog.max_starts_per_day))
    app = create_app(cfg, driver, token=token, flight=flight, watchdog=dog)

    log.info("服务配置 | contract=%s driver=%s capabilities=%s",
             CONTRACT_VERSION, driver.name, ",".join(driver.capabilities()))
    _log_config(log, cfg)
    log.info("HTTP 服务监听 | url=http://%s:%s diagnostics=/",
             BIND_HOST, cfg.server.port)
    if cfg.driver == "paper":
        log.warning("纸面驱动已启用 | 不连接交易客户端；健康检查结果仅用于接口联调")
    dog.start()

    from waitress import serve

    # threads=8：并发极低，但登录那一趟会占住一个线程几十秒，给 1~2 个的话那期间
    # 连 /v1/health 都打不动。
    try:
        serve(app, host=BIND_HOST, port=cfg.server.port, threads=8,
              ident="cn-broker-api")
    finally:
        dog.stop()
        close = getattr(driver, "close", None)
        if close is not None:
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
