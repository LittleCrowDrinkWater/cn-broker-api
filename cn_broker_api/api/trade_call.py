"""交易路由共用的两件事：排队执行，以及把失败翻译成 HTTP。

翻译表（调用方按它反向映射，见设计稿 §4.2）：

| 状态码 | 含义 |
|---|---|
| 200 + `known: false` | 查不到（**不是空结果**），传输层没出问题 |
| 202 | 委托推给客户端等确认，**结果还没定** |
| 400 | 请求字段不合法 |
| 409 | 定性拒单 |
| 501 | 这个驱动没这个能力（不静默降级） |
| 503 | 通道不可用（含补丁不在所以不受理） |
| 504 | 调用超时，**状态未知**，须重新观测柜台 |
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from flask import jsonify, request

from cn_broker_api.api.context import ApiContext
from cn_broker_api.drivers.broker_login_required import BrokerLoginRequired
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.session_state import SessionState
from cn_broker_api.queue_timeout import QueueTimeout
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.order_pending_confirm import OrderPendingConfirm
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable
from cn_broker_api.trade.wire import unknown

logger = logging.getLogger(__name__)


def account_of(body: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """从请求里取 (资金账号, 账户类别)。

    账号可以是空串＝客户端的默认账户（旧行为）。填了真账号则厂商在连接期校验，
    配错直接连不上——**这是本项目能拿到的最强的一道账户校验**。
    """
    src = body if body is not None else request.args
    raw_account = src.get("account")
    raw_type = src.get("account_type")
    if raw_account is not None and not isinstance(raw_account, str):
        raise ValueError("account 必须是字符串")
    if raw_type is not None and not isinstance(raw_type, str):
        raise ValueError("account_type 必须是字符串")
    account = (raw_account or "").strip()
    account_type = (raw_type or "STOCK").strip().upper()
    if account_type not in {"STOCK", "CREDIT"}:
        raise ValueError("account_type 只能是 STOCK 或 CREDIT")
    return account, account_type


def maps_failures(fn: Callable) -> Callable:
    """把交易层的异常翻译成上面那张表。顺序有讲究：细的在前，`DriverError` 在最后。"""
    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except QueryUnavailable as e:
            return jsonify(unknown(str(e))), 200
        except OrderPendingConfirm as e:
            return jsonify(error="order_pending_confirm", pending_confirm=True,
                           message=str(e), broker_message=e.broker_message), 202
        except OrderRejected as e:
            return jsonify(error="order_rejected", message=str(e),
                           broker_message=e.broker_message), 409
        except CapabilityMissing as e:
            return jsonify(error="capability_missing", message=str(e)), 501
        except BrokerLoginRequired as e:
            return jsonify(
                error="broker_login_required",
                message=str(e),
                session_state=e.session_state,
                login_endpoint="/v1/session/ensure",
                retryable_after_login=True,
            ), 503
        except (AckUnknown, QueueTimeout) as e:
            return jsonify(error="ack_timeout", message=str(e)), 504
        except DriverError as e:
            return jsonify(error="channel_unavailable", message=str(e)), 503
        except ValueError as e:
            return jsonify(error="bad_request", message=str(e)), 400
    return inner


def in_market_queue(ctx: ApiContext, what: str, work: Callable[[Any], Any]) -> Any:
    """行情调用同样排队，但用一个**与账户无关**的键。

    它们不需要账户句柄，所以不该混进某个账户的批里去触发账户切换；但客户端进程只有一个，
    一次 K 线拉取和一次报单在它那侧是同一条路 ⇒ 仍然要排队。
    """
    return ctx.queue.submit("market", lambda: work(ctx.driver.market()), what=what)


def in_queue(ctx: ApiContext, account: str, account_type: str, what: str,
             work: Callable[[Any], Any]) -> Any:
    """按账户排队跑一次交易调用。`work` 收到该账户的交易对象。

    队列的键含账户类别：同一个账号在 STOCK 与 CREDIT 上是两条连接，混成一个键会让
    「同账户接着做完」这条规则失效。
    """
    def invoke() -> Any:
        # 交易热路径只做连接级检查。显式状态接口仍执行完整账户与资产探测；每笔
        # 委托前重复完整探测会与业务调用争用同一条串行连接。
        status_fn = getattr(
            ctx.driver,
            "operation_session_status",
            getattr(ctx.driver, "session_status", None),
        )
        if status_fn is not None:
            status = status_fn(account=account, account_type=account_type)
            state = str(status.get("state") or SessionState.CHANNEL_UNAVAILABLE.value)
            detail = str(status.get("detail") or "交易会话不可用")
            if state == SessionState.LOGIN_REQUIRED.value:
                raise BrokerLoginRequired(detail)
            if state != SessionState.READY.value:
                raise DriverError(f"交易会话状态 {state}：{detail}")
        trading = ctx.driver.trading(account=account, account_type=account_type)
        return work(trading)

    return ctx.queue.submit(f"{account_type}:{account}", invoke, what=what)
