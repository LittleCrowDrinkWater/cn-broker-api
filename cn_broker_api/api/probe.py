"""自检那三个小动作：算缓存键、解析 need_times、真去探一次。

单独一个模块因为**两处都要用**：`/v1/health` 要，`/v1/session/ensure` 登完之后也要顺手刷一次。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from flask import request

logger = logging.getLogger(__name__)


def cache_key() -> str:
    return "|".join([(request.args.get("account") or "").strip(),
                     (request.args.get("account_type") or "STOCK").strip().upper(),
                     (request.args.get("need_times") or "").strip()])

def parse_need_times(raw: str):  # noqa: ANN202
    """`"09:23,14:57"` → `[(9,23),(14,57)]`。**认不出的格子直接跳过**：
    自检自己就是兜底件，不该因为一个参数写歪了而整条不跑。"""
    out = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            hh, _, mm = tok.partition(":")
            out.append((int(hh), int(mm)))
        except (TypeError, ValueError):
            logger.warning("[health] need_times 里认不出的格子：%r，跳过", tok)
    return sorted(set(out))

def probe(driver: Any) -> Dict[str, Any]:
    account = (request.args.get("account") or "").strip()
    account_type = (request.args.get("account_type") or "STOCK").strip().upper()
    need_times = parse_need_times(request.args.get("need_times") or "")
    try:
        return driver.health(account=account, account_type=account_type,
                             need_times=need_times)
    except Exception as e:  # noqa: BLE001 — 自检自己就是兜底件，它崩了就什么都不知道了
        logger.exception("[health] 自检本身出错")
        return {"ok": False, "message": f"自检出错：{type(e).__name__}: {str(e)[:200]}",
                "checks": []}
